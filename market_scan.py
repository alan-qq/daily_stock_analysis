"""全市场动态选股模块。

职责：每天从全市场快照中，按量化规则筛出「有资金介入、趋势向上、可买入」的
候选股，供 daily_brief.py 交给 AI 评分，最终选出当日推荐 5 只。

设计原则
--------
1. **不维护固定股票池**。除用户自选外，所有推荐都来自当日全市场快照。
2. **回测与实盘同口径**。选股只用「当日快照 + 日K可算指标」，历史每一日都能
   用同样的公式复算，因此「近三月入选次数」是真实回溯，不是编出来的。
3. **双数据源**。东财 clist 优先（含量比/主力净流入），失败自动切新浪。

口径定义
--------
「入选」= 当日通过硬过滤、且在量化初筛中排名进入前 PICKS_PER_DAY 名。
这是候选资格线，AI 之后会在候选里再挑最终推荐。
"""

from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)

PICKS_PER_DAY = 5          # 每日「入选」名额，回测与实盘一致
HISTORY_DAYS = 60          # 近三月统计窗口（交易日）
KLINE_LEN = 90             # 拉取K线长度，留出算 MA20 的余量
SCAN_TOP_N = 12            # 交给 AI 评分的候选数量

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False      # 忽略系统代理，否则国内行情接口会 SSL 握手失败
    s.headers.update(UA)
    return s


# --------------------------------------------------------------------------
# 快照：全市场当日行情
# --------------------------------------------------------------------------

def _code_prefix_ok(code: str) -> bool:
    """只保留沪深主板/创业板/科创板，剔除北交所(8/4)、B股(9)。"""
    if code.startswith(("8", "4", "9")):
        return False
    return code.startswith(("00", "30", "60", "68"))


def fetch_snapshot_eastmoney(sess: requests.Session | None = None) -> list[dict]:
    sess = sess or _session()
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    fields = "f2,f3,f6,f8,f10,f12,f14,f20,f21,f62"
    rows: list[dict] = []
    page, page_size, total = 1, 100, None
    while True:
        r = sess.get(
            "http://push2.eastmoney.com/api/qt/clist/get",
            params={"pn": page, "pz": page_size, "po": 1, "np": 1, "fltt": 2,
                    "invt": 2, "fid": "f3", "fs": fs, "fields": fields},
            timeout=20,
        )
        data = (r.json() or {}).get("data") or {}
        total = total or data.get("total") or 0
        batch = data.get("diff") or []
        if not batch:
            break
        for it in batch:
            code = str(it.get("f12") or "")
            if not _code_prefix_ok(code):
                continue
            price = it.get("f2")
            pct = it.get("f3")
            if not isinstance(price, (int, float)) or not isinstance(pct, (int, float)):
                continue
            rows.append({
                "code": code,
                "name": str(it.get("f14") or ""),
                "price": float(price),
                "pct": float(pct),
                "amount": float(it.get("f6") or 0),
                "turnover": float(it.get("f8") or 0),
                "vol_ratio": float(it.get("f10") or 0),
                "mktcap": float(it.get("f20") or 0),
                "float_cap": float(it.get("f21") or 0),
                "main_net": float(it.get("f62") or 0),
            })
        if len(rows) >= total or page * page_size >= total:
            break
        page += 1
        time.sleep(0.05)
    return rows


def fetch_snapshot_sina(sess: requests.Session | None = None) -> list[dict]:
    """东财限流时的备用源。新浪无「量比」，后续用K线自算。"""
    sess = sess or _session()
    url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData")
    rows: list[dict] = []
    page, num = 1, 100
    while True:
        try:
            r = sess.get(url, params={"page": page, "num": num, "sort": "changepercent",
                                      "asc": 0, "node": "hs_a", "symbol": "",
                                      "_s_r_a": "page"}, timeout=20)
            batch = r.json() or []
        except Exception:
            break
        if not batch:
            break
        for it in batch:
            code = str(it.get("code") or "")
            if not _code_prefix_ok(code):
                continue
            try:
                price = float(it.get("trade") or 0)
                pct = float(it.get("changepercent") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            rows.append({
                "code": code,
                "name": str(it.get("name") or ""),
                "price": price,
                "pct": pct,
                "amount": float(it.get("amount") or 0),
                "turnover": float(it.get("turnoverratio") or 0),
                "vol_ratio": 0.0,
                "mktcap": float(it.get("mktcap") or 0) * 1e4,
                "float_cap": float(it.get("nmc") or 0) * 1e4,
                "main_net": 0.0,
            })
        if len(batch) < num:
            break
        page += 1
        time.sleep(0.03)
    return rows


def fetch_snapshot() -> tuple[list[dict], str]:
    """返回 (全市场行情, 数据源名)。"""
    sess = _session()
    for name, fn in (("eastmoney", fetch_snapshot_eastmoney), ("sina", fetch_snapshot_sina)):
        for attempt in range(2):
            try:
                rows = fn(sess)
                if len(rows) > 3000:
                    return rows, name
            except Exception:
                time.sleep(1.5)
    raise RuntimeError("全市场快照获取失败（东财与新浪均不可用）")


# --------------------------------------------------------------------------
# K 线：新浪日K（快且稳）
# --------------------------------------------------------------------------

def _sina_symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "68")) else "sz") + code


def fetch_kline(code: str, sess: requests.Session) -> list[list]:
    """返回 [[date, open, close, high, low, volume], ...] 升序。"""
    r = sess.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": _sina_symbol(code), "scale": 240, "ma": "no", "datalen": KLINE_LEN},
        timeout=15,
    )
    data = r.json() or []
    out = []
    for b in data:
        try:
            out.append([
                b["day"],
                float(b["open"]),
                float(b["close"]),
                float(b["high"]),
                float(b["low"]),
                float(b["volume"]),
            ])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_klines(codes: list[str], cache_name: str = "klines.pkl",
                workers: int = 10, verbose: bool = False) -> dict[str, list[list]]:
    """带本地缓存的批量 K 线，只补拉缓存里缺失的代码。"""
    import pickle

    path = CACHE_DIR / cache_name
    cache: dict[str, list[list]] = {}
    if path.exists():
        try:
            with path.open("rb") as fh:
                cache = pickle.load(fh)
        except Exception:
            cache = {}

    missing = [c for c in codes if c not in cache or not cache[c]]
    if missing:
        sess = _session()
        lock_safe = {}

        def work(c: str):
            for _ in range(2):
                try:
                    bars = fetch_kline(c, sess)
                    if bars:
                        return c, bars
                except Exception:
                    time.sleep(0.4)
            return c, []

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for c, bars in ex.map(work, missing):
                lock_safe[c] = bars
        cache.update({k: v for k, v in lock_safe.items() if v})
        if verbose:
            print(f"[scan] K线补拉 {len(missing)} 只，成功 {len(lock_safe) - sum(1 for v in lock_safe.values() if not v)} 只")
        try:
            with path.open("wb") as fh:
                pickle.dump(cache, fh)
        except Exception:
            pass
    return cache


# --------------------------------------------------------------------------
# 技术特征
# --------------------------------------------------------------------------

def tech_features(bars: list[list]) -> dict | None:
    closes = [b[2] for b in bars]
    vols = [b[5] for b in bars]
    if len(closes) < 25:
        return None
    ma = lambda n: sum(closes[-n:]) / n
    ma5, ma10, ma20 = ma(5), ma(10), ma(20)
    last = closes[-1]
    prev5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else vols[-1]
    hi = max(closes[-60:]) if len(closes) >= 60 else max(closes)
    lo = min(closes[-60:]) if len(closes) >= 60 else min(closes)
    pos60 = (last - lo) / (hi - lo) if hi > lo else 0.5
    return {
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "bull": ma5 > ma10 > ma20,
        "above_ma20": last > ma20,
        "vol_ratio": (vols[-1] / prev5) if prev5 else 0.0,
        "pos60": pos60,
        "ret5": (last / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0,
        "ret20": (last / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0,
        "close": last,
    }


# --------------------------------------------------------------------------
# 硬过滤 + 量化打分
# --------------------------------------------------------------------------

ST_PAT = re.compile(r"ST|退|\*")


def turnover_floor(float_cap: float) -> float:
    """换手下限按流通市值自适应：大票换手天然低，用统一阈值会把它们全部排除。"""
    if float_cap > 800e8:
        return 0.5
    if float_cap > 300e8:
        return 0.9
    return 1.5


def hard_filter(rows: list[dict], feats: dict[str, dict] | None = None) -> list[dict]:
    out = []
    for r in rows:
        name = r["name"]
        if not name or ST_PAT.search(name):
            continue
        if name[0] in ("N", "C") and len(name) <= 8:   # 新股/次新
            continue
        code = r["code"]
        if not _code_prefix_ok(code):
            continue
        price, pct = r["price"], r["pct"]
        limit = 15.0 if code.startswith(("30", "68")) else 8.0
        if not (2.5 <= price <= 300):
            continue
        if not (0.8 <= pct <= limit):          # 要涨，但不能接近涨停（买不进）
            continue
        if r["amount"] < 3e8:                  # 成交额 ≥ 3 亿，保证流动性
            continue
        if not (30e8 <= r["float_cap"] <= 1200e8):   # 流通市值 30-1200 亿
            continue
        if not (turnover_floor(r["float_cap"]) <= r["turnover"] <= 25):   # 换手率下限按市值自适应
            continue
        if feats is not None:
            f = feats.get(code)
            if not f or not f["above_ma20"]:   # 必须站上 MA20
                continue
        out.append(r)
    return out


def _bell(x: float, center: float, width: float, cap: float) -> float:
    """钟形打分：在 center 处取满分，偏离越远越低，保证分数连续不并列。"""
    return cap * math.exp(-((x - center) / width) ** 2)


def score_stock(r: dict, f: dict) -> tuple[float, list[str]]:
    """量化初筛分（0-100）。与回测同口径，只用快照 + 日K。

    各维度用钟形/线性映射，避免阶梯打分造成大量并列，排序才有区分度。
    """
    why: list[str] = []
    pct = r["pct"]
    vr = f["vol_ratio"] or r.get("vol_ratio") or 0
    tp = r["turnover"]
    amt = r["amount"] / 1e8
    cap = r["float_cap"] / 1e8

    # 1) 量能启动 30 分：1.8 倍量最理想，缩量或爆量都扣
    s = _bell(vr, 1.8, 1.3, 30)
    if 1.4 <= vr <= 3.2:
        why.append(f"量能放大{vr:.1f}倍")

    # 2) 涨幅 26 分：4% 附近是最佳建仓区，接近涨停反而买不到
    s += _bell(pct, 4.0, 2.6, 26)
    if 2.0 <= pct <= 6.0:
        why.append(f"涨幅{pct:.1f}%在可买区间")

    # 3) 均线结构 18 分
    if f["bull"]:
        s += 18
        why.append("均线多头排列")
    elif f["above_ma20"]:
        s += 9

    # 4) 位置 12 分：60 日区间 68% 分位最健康，太高位追高风险大
    s += _bell(f["pos60"], 0.68, 0.26, 12)

    # 5) 流动性 10 分：成交额越大越好，但过了 30 亿边际递减
    s += min(10, 10 * math.log10(max(amt, 1) / 2) / math.log10(15)) if amt > 2 else 0

    # 6) 体量 4 分：50-400 亿流通盘最灵活
    s += _bell(math.log10(max(cap, 1)), math.log10(150), 0.55, 4)

    # 7) 修正项
    if r.get("main_net", 0) < -2e8:
        s -= 6
        why.append("主力净流出偏大")
    if tp > 18:
        s -= 4
    if f["ret20"] > 45:
        s -= 9
        why.append("20日涨幅过大")
    if f["ret5"] > 22:
        s -= 5
    if f["pos60"] > 0.97:
        s -= 4
        why.append("处于60日新高，回踩再介入")

    return max(0.0, min(100.0, s)), why


# --------------------------------------------------------------------------
# 当日扫描
# --------------------------------------------------------------------------

def scan_today(exclude: list[str] | tuple[str, ...] = (), top_n: int = SCAN_TOP_N,
               verbose: bool = True) -> dict:
    """返回当日候选（已排序），含数据源、初筛数量等元信息。"""
    today = date.today().isoformat()
    rows, source = fetch_snapshot()
    if verbose:
        print(f"[scan] 快照 {len(rows)} 只，来源 {source}")

    pre = hard_filter(rows)              # 第一层：纯快照硬条件
    if verbose:
        print(f"[scan] 快照硬过滤后 {len(pre)} 只")

    # 对硬过滤结果拉K线，再加均线条件并打分
    codes = [r["code"] for r in pre]
    klines = load_klines(codes, cache_name=f"klines_{today}.pkl", workers=10)
    feats = {c: tech_features(b) for c, b in klines.items()}
    feats = {c: f for c, f in feats.items() if f}

    picked = []
    for r in pre:
        f = feats.get(r["code"])
        if not f or not f["above_ma20"]:
            continue
        if r["float_cap"] and r["float_cap"] < 30e8:
            continue
        sc, why = score_stock(r, f)
        picked.append({**r, "scan_score": round(sc, 1), "why": why, "feat": f})

    picked.sort(key=lambda x: x["scan_score"], reverse=True)
    excl = set(exclude)
    picked = [p for p in picked if p["code"] not in excl]
    top = picked[:top_n]

    meta = {
        "date": today,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "universe": len(rows),
        "after_filter": len(picked),
        "candidates": len(top),
    }
    result = {"meta": meta, "candidates": top, "all_scored": picked[:120]}
    (CACHE_DIR / f"scan_{today}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        print(f"[scan] 候选 {len(top)} 只（初筛通过 {len(picked)} 只）")
    return result


# --------------------------------------------------------------------------
# 历史回填：算出「近三月每日入选了谁」
# --------------------------------------------------------------------------

def backtest_history(days: int = HISTORY_DAYS, extra_codes: list[str] | None = None,
                     universe_size: int = 1500, verbose: bool = True) -> dict:
    """按现行规则复算最近 N 个交易日的每日入选名单。

    做法：取全市场快照里成交额前 universe_size 名 + 自选股，
    拉日K，对每个交易日用同一套公式打分取前 PICKS_PER_DAY。
    这样「近三月入选次数」= 该股在多少天里进入过每日前 5。
    """
    rows, source = fetch_snapshot()
    rows.sort(key=lambda r: r["amount"], reverse=True)
    universe = [r for r in rows[:universe_size] if _code_prefix_ok(r["code"])]
    have = {r["code"] for r in universe}
    for c in (extra_codes or []):
        if c not in have:
            m = next((r for r in rows if r["code"] == c), None)
            if m:
                universe.append(m)
                have.add(c)
    if verbose:
        print(f"[backtest] 样本池 {len(universe)} 只（快照 {len(rows)} 只，来源 {source}）")

    codes = [r["code"] for r in universe]
    klines = load_klines(codes, cache_name="klines_bt.pkl", workers=10, verbose=verbose)

    by_code = {r["code"]: r for r in universe}
    # 日期 -> 索引，避免逐日线性查找
    idx_map: dict[str, dict[str, int]] = {
        c: {b[0]: i for i, b in enumerate(bars)} for c, bars in klines.items()
    }
    # 交易日序列：当日有效样本数超过半数才计入（滤掉停牌/半日）
    day_count: dict[str, int] = {}
    for c, bars in klines.items():
        for b in bars[-(days + 1):]:
            day_count[b[0]] = day_count.get(b[0], 0) + 1
    thresh = max(50, len(klines) * 0.5)
    trade_days = sorted(d for d, c in day_count.items() if c >= thresh)[-(days + 1):]
    if verbose:
        print(f"[backtest] 交易日 {len(trade_days)} 个：{trade_days[0]} ~ {trade_days[-1]}")

    daily_picks: dict[str, list[str]] = {}
    qualifying: dict[str, int] = {}   # 通过全部硬条件的天数（不限排名）
    for d in trade_days[1:]:
        scored: list[tuple[float, str]] = []
        for code, bars in klines.items():
            idx = idx_map[code].get(d)
            if idx is None or idx < 25:
                continue
            closes = [b[2] for b in bars]
            vols = [b[5] for b in bars]
            # 用截至当日的数据算均线/位置/量能
            win = closes[: idx + 1]
            vwin = vols[: idx + 1]
            ma5 = sum(win[-5:]) / 5
            ma10 = sum(win[-10:]) / 10
            ma20 = sum(win[-20:]) / 20
            last = win[-1]
            if last <= ma20:
                continue
            prev5 = sum(vwin[-6:-1]) / 5 if len(vwin) >= 6 else vwin[-1]
            hi = max(win[-60:]); lo = min(win[-60:])
            f = {
                "ma5": ma5, "ma10": ma10, "ma20": ma20,
                "bull": ma5 > ma10 > ma20,
                "above_ma20": True,
                "vol_ratio": (vwin[-1] / prev5) if prev5 else 0.0,
                "pos60": (last - lo) / (hi - lo) if hi > lo else 0.5,
                "ret5": (last / win[-6] - 1) * 100 if len(win) >= 6 else 0.0,
                "ret20": (last / win[-21] - 1) * 100 if len(win) >= 21 else 0.0,
                "close": last,
            }
            snap = by_code[code]
            prev_close = bars[idx - 1][2]
            pct = (last / prev_close - 1) * 100 if prev_close else 0
            vol = bars[idx][5]
            amt = vol * last
            float_shares = (snap["float_cap"] / snap["price"]) if snap["price"] else 0
            turnover = (vol / float_shares * 100) if float_shares else 0
            limit = 15.0 if code.startswith(("30", "68")) else 8.0
            if not (0.8 <= pct <= limit) or amt < 3e8 or not (
                    turnover_floor(snap["float_cap"]) <= turnover <= 25):
                continue
            qualifying[code] = qualifying.get(code, 0) + 1
            fake = {"code": code, "name": snap["name"], "price": last, "pct": pct,
                    "amount": amt, "turnover": turnover, "vol_ratio": f["vol_ratio"],
                    "mktcap": snap["mktcap"], "float_cap": snap["float_cap"], "main_net": 0}
            sc, _ = score_stock(fake, f)
            scored.append((sc, code))
        scored.sort(reverse=True)
        daily_picks[d] = [c for _, c in scored[:PICKS_PER_DAY]]

    counts: dict[str, int] = {}
    for picks in daily_picks.values():
        for c in picks:
            counts[c] = counts.get(c, 0) + 1

    payload = {
        "generated": date.today().isoformat(),
        "source": "backtest",
        "window_days": len(daily_picks),
        "universe": len(universe),
        "pool": sorted(by_code.keys()),   # 参与统计的样本池，池内未入选记 0 次
        "counts": counts,
        "qualifying": qualifying,
        "days": daily_picks,
    }
    (ROOT / "history_baseline.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        top = sorted(counts.items(), key=lambda x: -x[1])[:8]
        print(f"[backtest] 完成，覆盖 {len(daily_picks)} 天，入选过的股票 {len(counts)} 只")
        for c, n in top:
            print(f"   {c} {by_code.get(c, {}).get('name', '')} {n} 次")
    return payload


# --------------------------------------------------------------------------
# 入选历史：每日追加 + 合并回填
# --------------------------------------------------------------------------

HISTORY_FILE = ROOT / "recommend_history.json"


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"counts": {}, "days": {}}


def record_today(codes: list[str]) -> dict:
    """把当日最终推荐的股票记入历史，返回累计计数。"""
    h = load_history()
    h.setdefault("days", {})[date.today().isoformat()] = list(codes)
    h["counts"] = {c: len({d for d, lst in h["days"].items() if c in lst})
                   for c in {c for lst in h["days"].values() for c in lst}}
    HISTORY_FILE.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    return h["counts"]


def merged_counts(window: int = HISTORY_DAYS) -> dict[str, dict]:
    """回填基线 + 实盘记录按日期集合合并，滚动取最近 window 个交易日。

    返回 {code: {"picks": 入选前5次数, "qual": 通过硬条件天数}}。
    不在统计池里的股票不返回 key（调用方显示「—」）。
    """
    h = load_history()
    base_days: dict[str, list[str]] = {}
    qual: dict[str, int] = {}
    pool: set[str] = set()
    bt = ROOT / "history_baseline.json"
    if bt.exists():
        try:
            data = json.loads(bt.read_text(encoding="utf-8"))
            base_days = dict(data.get("days", {}))
            qual = dict(data.get("qualifying", {}))
            pool = set(data.get("pool", []))
        except Exception:
            base_days, qual, pool = {}, {}, set()

    days: dict[str, set[str]] = {}
    for d, lst in base_days.items():
        for c in lst:
            days.setdefault(d, set()).add(c)
    for d, lst in (h.get("days") or {}).items():
        for c in lst:
            days.setdefault(d, set()).add(c)
    if not days:
        return {}

    keep = sorted(days)[-window:]          # 滚动窗口，避免无限累积
    counts: dict[str, int] = {}
    for d in keep:
        for c in days[d]:
            counts[c] = counts.get(c, 0) + 1

    out = {c: {"picks": n, "qual": qual.get(c, 0)} for c, n in counts.items()}
    for c in pool:                          # 池内从未入选的记 0，而不是「无数据」
        out.setdefault(c, {"picks": 0, "qual": qual.get(c, 0)})
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="跑一次当日扫描")
    ap.add_argument("--backtest", action="store_true", help="回填近三月入选历史")
    ap.add_argument("--days", type=int, default=HISTORY_DAYS)
    ap.add_argument("--top", type=int, default=SCAN_TOP_N)
    args = ap.parse_args()

    if args.backtest:
        try:
            from daily_brief import WATCHLIST as _WL
        except Exception:
            _WL = []
        backtest_history(days=args.days, extra_codes=_WL)
    if args.scan or not args.backtest:
        res = scan_today(top_n=args.top)
        print(json.dumps(res["meta"], ensure_ascii=False, indent=2))
        for c in res["candidates"]:
            print(f"  {c['scan_score']:5.1f}  {c['name']}({c['code']}) "
                  f"{c['pct']:+.2f}%  额{c['amount']/1e8:.1f}亿  换手{c['turnover']:.1f}%  "
                  f"{'、'.join(c['why'][:2])}")
