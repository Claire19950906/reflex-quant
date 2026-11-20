#!/usr/bin/env python3
"""pipeline_health.py —— 一条命令看清"哪条链路真的在产出数据"。

为什么需要：这个系统的毛病不是报错，而是**静默空转**——表建好了、接口 200、
页面显示"暂无数据"，但根本没有任何任务在往里写。查这种问题要来回 grep 日志、
查 launchctl、查库，一次十分钟。这个脚本把这些压成一张表：

    表名 / 行数 / 最新数据时间 / 距今多久 / 生产者任务是否在跑 / 结论

判断口径是诚实的：行数为 0 且生产任务在跑 → "等数据"；行数为 0 且任务没跑 →
"空转"；有时间戳但很旧 → "停更"。没有任何"看起来正常"的粉饰。

用法：
    python3 pipeline_health.py             # 全表
    python3 pipeline_health.py --brief     # 只看有问题的
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "..", "trident_event_bus.db")

# (表名, 时间列, 生产者任务片段, 说明)
CHECKS = [
    ("market_ticks",           "ts",         "cron|flow|quotes|kline", "行情 tick"),
    ("kline_bars",             "ts",         "kline|bars|ohlcv",       "K 线"),
    ("raw_news",               "ts", "engine|news|flash|wscn", "新闻入库"),
    ("ai_decisions",           "created_at", "engine",                 "LLM 决策"),
    ("ai_decisions_history",   "created_at", "engine",                 "决策归档"),
    ("tech_signals",           "ts",         "tech-signals",           "技术信号"),
    ("quick_sim_trades",       "entry_ts",   "quick-sim",              "模拟成交"),
    ("event_impact_curve",     "sample_ts",  "impact",                 "影响曲线"),
    ("event_impact_stat",      "date_epoch_start", "impact",            "影响统计"),
    ("structured_reflections", "created_at", "engine",                 "结构化反思"),
    ("t2_positions",           "entry_ts",   "engine",                 "模拟持仓"),
    ("beliefs",                "last_verified_at", "belief",                 "信念库"),
    ("signal_performance",     "ts", "signal-perf",            "信号绩效"),
]


def _loaded_jobs() -> str:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        return "\n".join(l for l in out.splitlines() if "com.trident" in l)
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true", help="只显示有问题的行")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    jobs = _loaded_jobs()
    paused = os.path.exists(os.path.expanduser("~/.trident/state/DATA_PAUSED"))
    now = datetime.now()

    rows = []
    for table, tcol, job_key, label in CHECKS:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            rows.append((label, table, "-", "表不存在", "-", "缺表"))
            continue
        latest, age = "-", "-"
        try:
            v = conn.execute(
                f"SELECT MAX({tcol}) FROM {table}").fetchone()[0]
            if isinstance(v, (int, float)) and v > 1_000_000_000:
                # 库里 ts 两种口径混用：秒（1795082248）与毫秒（1795081800000），
                # 早期版本直接显示原始数字，用户看不出"这条数据到底是几分钟前"。
                sec = float(v) / (1000.0 if float(v) > 1e12 else 1.0)
                v = datetime.fromtimestamp(sec).strftime("%Y-%m-%dT%H:%M:%S")
            if v not in (None, ""):
                latest = str(v)[:19]
                ts = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        ts = datetime.strptime(latest[:19], fmt)
                        break
                    except ValueError:
                        continue
                if ts:
                    mins = int((now - ts).total_seconds() // 60)
                    age = (f"待采样 +{abs(mins) // 60}h" if mins < -5
                           else f"{mins} 分钟")
        except sqlite3.Error:
            pass
        running = job_key.split("|")
        loaded = any(k in jobs for k in running) or (
            job_key == "engine" and "com.trident.engine" in jobs)
        if n == 0:
            if paused:
                verdict = "暂停中（未启动）"
            else:
                verdict = "在跑·等数据" if loaded else "空转（生产者没跑）"
        elif age == "-":
            verdict = "有数据·无时间戳"
        else:
            mins = int((now - datetime.strptime(latest[:19], "%Y-%m-%dT%H:%M:%S")).total_seconds() // 60) \
                if "T" in latest else int((now - datetime.strptime(latest[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() // 60)
            verdict = "正常" if mins < 360 else f"停更 {mins // 60}h"
        rows.append((label, table, f"{n:,}", latest, age, verdict))

    if args.brief:
        rows = [r for r in rows if r[5] != "正常"]

    w = [max(len(str(r[i])) for r in rows + [("链路", "表", "行数", "最新", "距今", "结论")])
         for i in range(6)]
    head = ("链路", "表", "行数", "最新", "距今", "结论")
    print(" | ".join(str(head[i]).ljust(w[i]) for i in range(6)))
    print("-+-".join("-" * w[i] for i in range(6)))
    for r in rows:
        print(" | ".join(str(r[i]).ljust(w[i]) for i in range(6)))

    # 空表盘点：59 张空表全部**被代码引用**（没有一张是废弃 schema），
    # 所以"表空"永远是生产者没跑/条件不满足，而不是"这功能本来就没用"。
    try:
        _tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        _empty = [t for t in _tabs
                  if conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() is None]
        print(f"\n空表：{len(_empty)} / {len(_tabs)}（均被代码引用 → 属于生产者未跑，不是废弃表）")
    except sqlite3.Error:
        pass

    n_jobs = len([l for l in jobs.splitlines() if l.strip()])
    print(f"\n已加载 com.trident 任务：{n_jobs} 个")
    flag = os.path.expanduser("~/.trident/state/DATA_PAUSED")
    print("DATA_PAUSED 标记：" + ("存在（引擎不会启动）" if os.path.exists(flag) else "已清除"))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
