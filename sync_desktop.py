#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开机补跑：把云端（GitHub Actions）当天生成的简报同步到桌面。

逻辑：
1. 通过 GitHub Contents API 拉取仓库 reports/ 下最近的简报（api.github.com 本机可达，
   不依赖 git，也不需要 token —— 仓库是公开的）
2. 桌面「每日股票分析」里缺失的日期，逐份补齐
3. 若今天是工作日、时间已过 19:00、且当天简报仍缺失（说明云端跑失败或未触发），
   则在本地补跑一次完整分析

用法：
    python sync_desktop.py            # 同步 + 必要时本地补跑
    python sync_desktop.py --sync-only # 只同步，绝不本地跑分析
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent
REPO_FILE = Path(__file__).resolve().parent / "cloud_repo.txt"
DESKTOP_DIR = Path(
    os.getenv("DSA_BRIEF_DESKTOP_DIR", str(Path.home() / "Desktop" / "每日股票分析"))
)
LOG = Path(os.getenv("DSA_SYNC_LOG", str(ROOT / "sync_desktop.log")))
KEEP_DAYS = 10
LOCAL_RUN_AFTER_HOUR = 20


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def api(path: str) -> dict | list | None:
    url = f"https://api.github.com{path}"
    req = request.Request(url, headers={"Accept": "application/vnd.github+json",
                                        "User-Agent": "dsa-sync"})
    try:
        with request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        if exc.code != 404:
            log(f"API {path} -> HTTP {exc.code}")
        return None
    except Exception as exc:  # noqa: BLE001
        log(f"API {path} 失败: {exc}")
        return None


def repo_slug() -> str | None:
    if REPO_FILE.exists():
        slug = REPO_FILE.read_text(encoding="utf-8").strip()
        if "/" in slug:
            return slug
    return None


def download_briefs(slug: str) -> dict[str, str]:
    """返回 {YYYY-MM-DD: 内容}"""
    listing = api(f"/repos/{slug}/contents/reports")
    if not isinstance(listing, list):
        log("读取云端 reports 目录失败，跳过同步")
        return {}
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    out: dict[str, str] = {}
    for item in listing:
        name = item.get("name", "")
        if not name.startswith("brief_") or not name.endswith(".md"):
            continue
        stamp = name[len("brief_"):-3]
        try:
            day = datetime.strptime(stamp, "%Y%m%d").date()
        except ValueError:
            continue
        if day < cutoff:
            continue
        blob = api(f"/repos/{slug}/contents/reports/{name}")
        if not isinstance(blob, dict) or "content" not in blob:
            continue
        try:
            out[day.isoformat()] = base64.b64decode(blob["content"]).decode("utf-8")
        except Exception:  # noqa: BLE001
            continue
    return out


def sync_to_desktop(slug: str) -> set[str]:
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    briefs = download_briefs(slug)
    added: set[str] = set()
    for day, text in sorted(briefs.items()):
        target = DESKTOP_DIR / f"{day}_简报.md"
        if target.exists() and target.stat().st_size > 200:
            if target.read_text(encoding="utf-8", errors="ignore") == text:
                continue
            target.write_text(text, encoding="utf-8")
            added.add(day)
            log(f"已更新 {target.name}（云端版本更新）")
            continue
        target.write_text(text, encoding="utf-8")
        added.add(day)
        log(f"已补齐 {target.name}（{len(text)} 字）")
    if not added:
        log(f"桌面已是最新，云端共 {len(briefs)} 份可同步简报")
    return {d for d in briefs}


def run_local_brief() -> None:
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    log("云端无当日简报，开始本地补跑（约 15-20 分钟）")
    proc = subprocess.run(
        [sys.executable, "daily_brief.py", "--push"],
        cwd=str(ROOT),
        env=env,
    )
    log(f"本地补跑结束，返回码 {proc.returncode}")


def main() -> int:
    sync_only = "--sync-only" in sys.argv
    slug = repo_slug()
    if not slug:
        log("未配置 cloud_repo.txt（owner/repo），跳过云端同步")
        return 0

    have = sync_to_desktop(slug)
    today = date.today()
    if sync_only:
        return 0
    if today.weekday() >= 5:
        log("今天是周末，不做本地补跑")
        return 0
    if today.isoformat() in have:
        log("当日云端简报已在，无需本地补跑")
        return 0
    if datetime.now().hour < LOCAL_RUN_AFTER_HOUR:
        log(f"当日云端简报尚未生成（云端 18:20 触发），当前未到 {LOCAL_RUN_AFTER_HOUR}:00，暂不补跑")
        return 0
    run_local_brief()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
