#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日精简简报：跑分析 -> 上期复盘 -> 桌面存档 -> 可选推送。

用法：
    python daily_brief.py              # 运行分析并生成简报
    python daily_brief.py --no-run     # 跳过分析，用已有报告生成简报
    python daily_brief.py --push       # 生成后推送到配置的渠道
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
DESKTOP_DIR = Path(
    os.getenv("DSA_BRIEF_DESKTOP_DIR", str(Path.home() / "Desktop" / "每日股票分析"))
)

# 用户自选（20 只）
WATCHLIST = [
    "300364",  # 中文在线 —— 传媒/AI 应用
    "603986",  # 兆易创新 —— 存储芯片
    "300251",  # 光线传媒 —— 影视传媒
    "603533",  # 掌阅科技 —— 数字阅读
    "002463",  # 沪电股份 —— PCB/AI 算力
    "688981",  # 中芯国际 —— 半导体制造
    "688256",  # 寒武纪 —— AI 芯片
    "601138",  # 工业富联 —— AI 服务器
    "002475",  # 立讯精密 —— 消费电子
    "300274",  # 阳光电源 —— 光伏逆变
    "300750",  # 宁德时代 —— 锂电池
    "002594",  # 比亚迪 —— 新能源车
    "600276",  # 恒瑞医药 —— 创新药
    "603259",  # 药明康德 —— CXO
    "600519",  # 贵州茅台 —— 白酒
    "600036",  # 招商银行 —— 银行
    "601318",  # 中国平安 —— 保险
    "600900",  # 长江电力 —— 水电红利
    "600760",  # 中航沈飞 —— 军工
    "300124",  # 汇川技术 —— 工控/机器人
]
WATCH_NAMES = {
    "300364": "中文在线", "603986": "兆易创新", "300251": "光线传媒",
    "603533": "掌阅科技", "002463": "沪电股份", "688981": "中芯国际",
    "688256": "寒武纪", "601138": "工业富联", "002475": "立讯精密",
    "300274": "阳光电源", "300750": "宁德时代", "002594": "比亚迪",
    "600276": "恒瑞医药", "603259": "药明康德", "600519": "贵州茅台",
    "600036": "招商银行", "601318": "中国平安", "600900": "长江电力",
    "600760": "中航沈飞", "300124": "汇川技术",
}

# 推荐候选池（与自选不重叠，每天按评分取前 5）
POOL = [
    "002371",  # 北方华创 —— 半导体设备
    "300308",  # 中际旭创 —— 光模块
    "300502",  # 新易盛 —— 光模块
    "000977",  # 浪潮信息 —— AI 服务器
    "601689",  # 拓普集团 —— 汽车零部件
    "002747",  # 埃斯顿 —— 工业机器人
    "300059",  # 东方财富 —— 券商
    "601899",  # 紫金矿业 —— 有色/黄金
    "601088",  # 中国神华 —— 煤炭
    "000858",  # 五粮液 —— 白酒
    "300760",  # 迈瑞医疗 —— 医疗器械
    "600436",  # 片仔癀 —— 中药
    "002179",  # 中航光电 —— 军工连接器
    "300442",  # 润泽科技 —— IDC/算力
    "002555",  # 三七互娱 —— 游戏
    "300418",  # 昆仑万维 —— AI 应用
    "002241",  # 歌尔股份 —— 消费电子
    "000568",  # 泸州老窖 —— 白酒
    "601166",  # 兴业银行 —— 银行
]

POSITIVE = ("买入", "加仓", "看多")
NEGATIVE = ("卖出", "减仓", "清仓", "看空")
SIGNAL_ORDER = {"买入": 0, "加仓": 1, "持有": 2, "观望": 3, "减仓": 4, "卖出": 5}


def _signal_rank(row: dict) -> int:
    for key, rank in SIGNAL_ORDER.items():
        if key in row["signal"]:
            return rank
    return 9

SUMMARY_RE = re.compile(
    r"^(?:🟢|🟡|🔴|⚪|🟠)\s*\*\*(.+?)\((\w+)\)\*\*:\s*(.+?)\s*\|\s*评分\s*(\d+)\s*\|\s*(.+)$",
    re.M,
)


def _clean(text: str) -> str:
    return re.sub(r"[*`]", "", text).strip()


def _short(text: str, limit: int) -> str:
    """压缩成表内可读的一小段：取第一个价格片段，去掉括号解释。"""
    head = re.split(r"[（(，,、；;]", _clean(text))[0].strip()
    return head[:limit]


def _qt_symbol(code: str) -> str:
    if code.startswith(("60", "68", "90", "51", "58")):
        return "sh" + code
    if code.startswith(("00", "30", "20", "15", "16")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def kline_hits(code: str, window: int = 60) -> int | None:
    """近 window 个交易日中，满足多头结构（收盘>MA20 且 MA5>MA10>MA20）的天数。

    数据源为腾讯前复权日 K，用于衡量该股近三个月的趋势稳定性。
    """
    import urllib.request

    sym = _qt_symbol(code)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={sym},day,,,{window + 40},qfq"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
        node = (payload.get("data") or {}).get(sym) or {}
        rows = node.get("qfqday") or node.get("day") or []
        closes = [float(r[2]) for r in rows if len(r) >= 3]
    except Exception:  # noqa: BLE001
        return None
    if len(closes) < 25:
        return None

    def ma(seq: list[float], i: int, n: int) -> float:
        return sum(seq[i - n + 1: i + 1]) / n

    flags = []
    for i in range(19, len(closes)):
        m5, m10, m20 = ma(closes, i, 5), ma(closes, i, 10), ma(closes, i, 20)
        flags.append(closes[i] > m20 and m5 > m10 > m20)
    return sum(flags[-window:])


def parse_sectors(path: Path) -> tuple[list, list]:
    """从当日大盘复盘里提取行业板块领涨/领跌 Top5。"""
    if not path.exists():
        return [], []
    text = path.read_text(encoding="utf-8", errors="ignore")

    def grab(title: str) -> list:
        m = re.search(r"#+\s*" + re.escape(title) + r"(.*?)(?=\n#{2,}|\Z)", text, re.S)
        if not m:
            return []
        out = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3 or cells[0] in ("排名", "") or set(cells[0]) <= {"-", ":"}:
                continue
            out.append((cells[1], cells[2]))
        return out[:5]

    return grab("行业板块领涨 Top 5"), grab("行业板块领跌 Top 5")


def run_analysis() -> int:
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    codes = ",".join(dict.fromkeys(WATCHLIST + POOL))
    print(f"[brief] 开始分析 {len(codes.split(','))} 只标的…", flush=True)
    proc = subprocess.run(
        [sys.executable, "main.py", "--no-notify", "--stocks", codes],
        cwd=str(ROOT),
        env=env,
    )
    return proc.returncode


def parse_report(path: Path) -> dict[str, dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    rows: dict[str, dict] = {}
    for m in SUMMARY_RE.finditer(text):
        name, code, signal = m.group(1), m.group(2), _clean(m.group(3))
        rows[code] = {
            "name": name,
            "code": code,
            "signal": signal,
            "score": int(m.group(4)),
            "trend": _clean(m.group(5)),
        }

    for m in re.finditer(r"^## .*?\((\w+)\)\s*$", text, re.M):
        code = m.group(1)
        if code not in rows:
            continue
        start = m.end()
        nxt = re.search(r"^## .*?\(\w+\)\s*$", text[start:], re.M)
        block = text[start : start + nxt.start()] if nxt else text[start:]
        row = rows[code]
        one = re.search(r"\*\*一句话决策\*\*:\s*(.+)", block)
        if one:
            row["one_liner"] = _clean(one.group(1))[:70]
        buy = re.search(r"\| 🎯 理想买入点 \| (.+?) \|", block)
        if buy:
            row["buy"] = _short(buy.group(1), 24)
        stop = re.search(r"\| 🛑 止损位 \| (.+?) \|", block)
        if stop:
            row["stop"] = _short(stop.group(1), 18)
        close = re.search(r"^\|\s*(\d+\.\d+)\s*\|\s*(\d+\.\d+)\s*\|\s*(\d+\.\d+)\s*\|", block, re.M)
        if close:
            row["close"] = float(close.group(1))
    return rows


def report_files() -> list[tuple[date, Path]]:
    out = []
    for p in REPORTS.glob("report_*.md"):
        m = re.search(r"report_(\d{8})", p.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if d < date.today():
            out.append((d, p))
    return sorted(out)


def review(prev: dict[str, dict], today: dict[str, dict]) -> list[str]:
    lines = []
    hit = total = quiet = 0
    for code, cur in today.items():
        old = prev.get(code)
        if not old or "close" not in old or "close" not in cur:
            continue
        old_sig = old["signal"]
        change = (cur["close"] - old["close"]) / old["close"] * 100
        arrow = "↑" if change > 0 else ("↓" if change < 0 else "→")
        has_call = any(k in old_sig for k in POSITIVE) or any(k in old_sig for k in NEGATIVE)
        moved = old_sig != cur["signal"]

        verdict = ""
        if any(k in old_sig for k in POSITIVE):
            total += 1
            hit += 1 if change > 0 else 0
            verdict = " ✅兑现" if change > 0 else " ❌偏离"
        elif any(k in old_sig for k in NEGATIVE):
            total += 1
            hit += 1 if change < 0 else 0
            verdict = " ✅兑现" if change < 0 else " ❌偏离"

        # 只保留：上次给出方向判断的、或信号发生变化的；其余只计数
        if not has_call and not moved:
            quiet += 1
            continue
        lines.append(
            f"- {cur['name']}({code})：{old_sig} → **{cur['signal']}**，期间 {change:+.1f}% {arrow}{verdict}"
        )

    head = []
    if total:
        head.append(f"**方向命中：{hit}/{total}**")
    if quiet:
        head.append(f"另有 {quiet} 只维持原判断、无信号变化，未单列")
    if head:
        lines.insert(0, "；".join(head) + "\n")
    return lines


def build_brief(
    today_rows: dict[str, dict],
    prev_rows: dict[str, dict],
    prev_date: date | None,
    hits: dict[str, int | None] | None = None,
    sectors: tuple[list, list] | None = None,
) -> str:
    today = date.today().isoformat()
    hits = hits or {}
    leading, lagging = sectors or ([], [])

    def hit_txt(code: str) -> str:
        val = hits.get(code)
        return "—" if val is None else f"{val}/60"
    watch = [today_rows[c] for c in WATCHLIST if c in today_rows]
    pool = [today_rows[c] for c in POOL if c in today_rows and c not in WATCHLIST]
    pool.sort(key=lambda r: (_signal_rank(r), -r["score"]))
    picks = pool[:5]
    buy_signals = [r for r in picks if _signal_rank(r) <= 1]

    buy_n = sum(1 for r in watch if any(k in r["signal"] for k in ("买入", "加仓")))
    out = [f"# 每日股票简报 · {today}", ""]
    out.append(
        f"**自选 {len(watch)} 只中 {buy_n} 只给出买入/加仓信号**；"
        f"推荐池 {len(pool)} 只中取评分前 5 作为候选。"
    )
    out.append("")

    out.append("## 一、自选股")
    out.append("")
    out.append("| 股票 | 评分 | 信号 | 收盘 | 近三月入选 | 买入区间 | 止损 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in watch:
        out.append(
            f"| {r['name']} {r['code']} | {r['score']} | {r['signal']} | "
            f"{r.get('close','-')} | {hit_txt(r['code'])} | {r.get('buy','-')} | {r.get('stop','-')} |"
        )
    out.append("")
    out.append("**要点：**")
    for r in watch:
        out.append(f"- **{r['name']}**：{r.get('one_liner', r['signal'])}")
    missing = [c for c in WATCHLIST if c not in today_rows]
    if missing:
        names = "、".join(f"{WATCH_NAMES.get(c, '')}{c}" for c in missing)
        out.append("")
        out.append(f"> ⚠️ 今日 {len(missing)} 只自选未能生成分析：{names}"
                   "（多为模型返回格式异常或数据源失败），明日会重新纳入。")
    out.append("")

    out.append("## 二、今日推荐候选（候选池 5 只）")
    out.append("")
    if buy_signals:
        out.append(f"其中 **{len(buy_signals)} 只**给出买入/加仓信号，可直接进入观察或试仓名单。")
    else:
        out.append("**今日候选池无明确买入信号**，以下为相对最优的 5 只，适合跟踪，不宜追高。")
    out.append("")
    out.append("| 股票 | 评分 | 信号 | 近三月入选 | 买入区间 | 止损 |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for r in picks:
        out.append(
            f"| {r['name']} {r['code']} | {r['score']} | {r['signal']} | "
            f"{hit_txt(r['code'])} | {r.get('buy','-')} | {r.get('stop','-')} |"
        )
    out.append("")
    out.append("> 候选池为固定观察名单（跨赛道龙头，与自选不重叠），非全市场扫描。评分来自本地分析引擎的"
               "技术/基本面/舆情/环境四维加权，仅供筛选线索。")
    out.append("")

    out.append("## 三、重点板块")
    out.append("")
    if leading or lagging:
        out.append("| 领涨板块 | 涨幅 | 领跌板块 | 跌幅 |")
        out.append("| --- | --- | --- | --- |")
        for i in range(max(len(leading), len(lagging))):
            a = leading[i] if i < len(leading) else ("-", "-")
            b = lagging[i] if i < len(lagging) else ("-", "-")
            out.append(f"| {a[0]} | {a[1]} | {b[0]} | {b[1]} |")
        out.append("")
        out.append("> 领涨集中在低位小市值方向时不追；领跌若是同大类多个子行业同步下挫（链条式扩散），"
                   "相关自选股即使当日红盘也应减仓。")
    else:
        out.append("- 今日大盘复盘的板块数据缺失，未取到行业排行。")
    out.append("")

    out.append("## 四、上期复盘")
    out.append("")
    if prev_date and prev_rows:
        out.append(f"对比基准：{prev_date.isoformat()} 报告")
        out.append("")
        lines = review(prev_rows, today_rows)
        out.extend(lines if lines else ["- 上期无可对比的同标的记录。"])
    else:
        out.append("- 暂无历史报告，今日为首期，明日起自动生成复盘。")
    out.append("")

    out.append("## 五、风险提示")
    out.append("")
    out.append("**「近三月入选」口径**：近 60 个交易日中，该股满足「收盘站上 MA20 且 MA5>MA10>MA20」"
               "多头结构的天数（满分 60）。数值越高说明近三个月多数时间处于强势结构，"
               "低于 20 说明长期弱势、反弹多为技术性修复。")
    out.append("")
    out.append("以上内容由程序 + AI 模型自动生成，仅供研究参考，不构成投资建议。")
    out.append(f"\n生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(out)


def push(markdown: str, title: str) -> None:
    import urllib.request

    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    hook = os.getenv("WECHAT_WEBHOOK_URL", "").strip()
    sent = False

    def post(url: str, payload: dict) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"[push] {url.split('/')[2]} -> {resp.status}")

    if token:
        try:
            post(
                "https://www.pushplus.plus/send",
                {
                    "token": token,
                    "title": title,
                    "content": markdown,
                    "template": "markdown",
                },
            )
            sent = True
        except Exception as exc:  # noqa: BLE001
            print(f"[push] PushPlus 失败: {exc}")

    if hook:
        try:
            post(
                hook,
                {"msgtype": "markdown", "markdown": {"content": markdown[:4000]}},
            )
            sent = True
        except Exception as exc:  # noqa: BLE001
            print(f"[push] 企业微信失败: {exc}")

    if not sent:
        print("[push] 未配置 PUSHPLUS_TOKEN 或 WECHAT_WEBHOOK_URL，跳过推送")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-run", action="store_true", help="跳过分析，使用已有报告")
    ap.add_argument("--push", action="store_true", help="生成后推送")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass

    if not args.no_run:
        code = run_analysis()
        if code != 0:
            print(f"[brief] 分析进程返回 {code}，继续尝试解析已有报告", flush=True)

    today_file = REPORTS / f"report_{date.today():%Y%m%d}.md"
    if not today_file.exists():
        print(f"[brief] 未找到今日报告：{today_file}", file=sys.stderr)
        return 2

    today_rows = parse_report(today_file)
    history = report_files()
    prev_date, prev_rows = None, {}
    if history:
        prev_date, prev_path = history[-1]
        prev_rows = parse_report(prev_path)

    codes = list(dict.fromkeys(WATCHLIST + POOL))
    print(f"[brief] 统计近三月入选天数（{len(codes)} 只）…", flush=True)
    hits = {c: kline_hits(c) for c in codes}
    ok = sum(1 for v in hits.values() if v is not None)
    print(f"[brief] 成功 {ok}/{len(codes)}", flush=True)

    sectors = parse_sectors(REPORTS / f"market_review_{date.today():%Y%m%d}.md")

    brief = build_brief(today_rows, prev_rows, prev_date, hits=hits, sectors=sectors)

    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"brief_{date.today():%Y%m%d}.md").write_text(brief, encoding="utf-8")

    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    desktop_file = DESKTOP_DIR / f"{date.today().isoformat()}_简报.md"
    desktop_file.write_text(brief, encoding="utf-8")
    print(f"[brief] 已保存：{desktop_file}")

    print("\n" + "=" * 50 + "\n")
    print(brief)

    if args.push:
        push(brief, f"每日股票简报 {date.today().isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
