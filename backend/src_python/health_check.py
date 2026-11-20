# -*- coding: utf-8 -*-
"""health_check.py — 一条命令看清"数据链路现在活不活"。

为什么需要
----------
前面逐个修的问题（慢涨阈值、归因覆盖率、AI 复核、写锁、数据源断档）都会以
同一种方式复发：**某张表悄悄停更，没人发现**。每次都要人工去查
``SELECT MAX(ts)`` 才知道断了，成本太高。

本脚本只做体检，不改数据：
    1) 每张关键表的最新时间 + 落后多少分钟（按各自的正常刷新节奏给红线）；
    2) launchd 任务里上一轮退出码非 0 的（最近一次运行失败）；
    3) 最近 1 小时日志里 ``database is locked`` 的出现次数。

用法:
    python3 health_check.py            # 人读
    python3 health_check.py --json     # 机读（给页面/告警用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trident_event_bus.db"
)
LOG_DIR = "/Users/claire/.trident/log"

# (表名, 时间列, 正常刷新间隔秒, 说明)
CHECKS = [
    ("market_ticks", "ts", 300, "秒级行情（异动检测输入）"),
    ("ohlcv_intraday", "ts", 1800, "分钟K线（多周期上下文）"),
    ("kline_bars", "ts", 7200, "1h/4h/1d K线（ts 为毫秒）"),
    ("ohlcv_daily", "ts", 90000, "日线（形态信号输入）"),
    ("tech_signals", "ts", 90000, "K线形态信号"),
    ("news_events", "ts", 3600, "新闻事件"),
    ("critical_price_moves", "ts", 3600, "异动（快/慢涨跌）"),
    ("event_move_attribution", "created_at", 7200, "行情归因"),
    ("flow_snapshots", "ts", 3600, "资金流快照"),
]

MS_TABLES = {"kline_bars"}


def _norm_ts(table: str, value) -> int:
    if value is None:
        return 0
    try:
        v = int(value)
    except (TypeError, ValueError):
        try:  # created_at 可能是文本时间
            return int(dt.datetime.fromisoformat(str(value)).timestamp())
        except Exception:
            return 0
    if table in MS_TABLES and v > 10_000_000_000:
        v //= 1000
    return v


def table_report(conn: sqlite3.Connection) -> list[dict]:
    now = int(time.time())
    out: list[dict] = []
    for table, col, expect, note in CHECKS:
        try:
            row = conn.execute(f"SELECT MAX({col}), COUNT(*) FROM {table}").fetchone()
        except sqlite3.Error as exc:
            out.append({"table": table, "note": note, "status": "missing",
                        "detail": str(exc)[:60]})
            continue
        last = _norm_ts(table, row[0] if row else None)
        age_min = (now - last) / 60 if last else None
        if not last:
            status = "empty"
        elif age_min <= expect / 60:
            status = "ok"
        elif age_min <= 3 * expect / 60:
            status = "late"
        else:
            status = "stale"
        out.append({
            "table": table, "note": note, "status": status,
            "rows": int(row[1] or 0) if row else 0,
            "last": dt.datetime.fromtimestamp(last).strftime("%m-%d %H:%M") if last else "-",
            "age_min": round(age_min, 1) if age_min is not None else None,
            "expect_min": round(expect / 60, 1),
        })
    return out


def launchd_report() -> list[dict]:
    """上一次退出码非 0 的任务（launchctl list 第二列是 last exit status）。"""
    try:
        raw = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return []
    bad = []
    for line in raw.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, status, label = parts[0], parts[1], parts[2]
        if label.startswith("com.trident.") and status not in ("0", "-"):
            bad.append({"label": label, "last_exit": status})
    return bad


def lock_report(minutes: int = 60, tail_lines: int = 400) -> dict:
    """粗略统计最近的写锁报错。

    只读每个日志**末尾 400 行**：日志是追加写的，末尾才是最近发生的事；
    整文件扫描会把几周前的历史报错也算进"最近 1 小时"（实测虚报 1.8 万次）。
    """
    cutoff = time.time() - minutes * 60
    hits = 0
    files = 0
    for path in glob.glob(os.path.join(LOG_DIR, "*.log")):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            files += 1
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 400 * 120))   # 约 400 行 * 120B
                tail = fh.read().decode("utf-8", errors="ignore")
            hits += tail.count("database is locked")
        except OSError:
            continue
    return {"window_min": minutes, "log_files": files, "locked_hits": hits}


def collect(minutes: int = 60) -> dict:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    try:
        tables = table_report(conn)
    finally:
        conn.close()
    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tables": tables,
        "launchd_failed": launchd_report(),
        "locks": lock_report(minutes),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--minutes", type=int, default=60, help="锁日志回看窗口")
    args = ap.parse_args()
    rep = collect(args.minutes)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    icon = {"ok": "OK  ", "late": "SLOW", "stale": "STALE", "empty": "EMPTY",
            "missing": "MISS"}
    print(f"数据链路体检  {rep['generated_at']}")
    print("-" * 76)
    for t in rep["tables"]:
        age = f"{t['age_min']}min" if t.get("age_min") is not None else "-"
        print(f"  {icon.get(t['status'], '?'):<5} {t['table']:<24} "
              f"last={str(t.get('last')):<12} age={age:<8} "
              f"expect≤{t.get('expect_min', '-')}min  {t['note']}")
    bad = rep["launchd_failed"]
    print("-" * 76)
    if bad:
        print(f"  上次运行失败的任务 {len(bad)} 个:")
        for b in bad[:12]:
            print(f"    {b['label']:<44} exit={b['last_exit']}")
    else:
        print("  launchd: 全部任务上次退出码为 0")
    lk = rep["locks"]
    print(f"  写锁: 最近 {lk['window_min']} 分钟内 {lk['log_files']} 个日志文件里 "
          f"出现 'database is locked' {lk['locked_hits']} 次")
    stale = [t["table"] for t in rep["tables"] if t["status"] in ("stale", "empty", "missing")]
    print("-" * 76)
    print("  结论: " + ("链路在跑" if not stale else f"需要处理 -> {', '.join(stale)}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
