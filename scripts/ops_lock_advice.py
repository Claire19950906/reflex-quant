#!/usr/bin/env python3
"""写锁体检 + 合并建议：按「谁在抢锁、多久抢一次」给可执行清单。

背景：`audit_schedule_collisions.py` 只回答"同一分钟有几个作业醒"，
结论是"撞车不严重"；但实际日志里 `database is locked` 遍布 100+ 个作业。
两者不矛盾 —— **不是同时醒，而是有人开着写事务不提交**（事务里夹网络等待），
把别人全堵在 busy_timeout 上。所以这里按**每个作业的锁错误次数 × 频率**排序，
而不是按分钟桶。

只读：不修改任何 plist，只产出报告。
用法：python3 scripts/ops_lock_advice.py [--write]
"""
from __future__ import annotations

import glob
import os
import plistlib
import re
import sys

LOG_DIR = os.path.expanduser("~/.trident/log")
AGENTS = os.path.expanduser("~/Library/LaunchAgents")
REPORT = "backend/reports/ops/job_lock_advice.md"


def lock_counts() -> dict:
    out = {}
    for path in glob.glob(os.path.join(LOG_DIR, "*.log")):
        job = os.path.basename(path)
        for suffix in (".err.log", ".out.log", ".log"):
            if job.endswith(suffix):
                job = job[: -len(suffix)]
                break
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                n = f.read().count("database is locked")
        except Exception:
            continue
        if n:
            out[job] = out.get(job, 0) + n
    return out


def _norm(name: str) -> str:
    n = str(name).lower().replace("-", "_").replace(".", "_")
    while n.endswith("_cron"):
        n = n[: -len("_cron")]
    return n


def schedules() -> dict:
    out = {}
    for path in glob.glob(os.path.join(AGENTS, "com.trident.*.plist")):
        try:
            d = plistlib.loads(open(path, "rb").read())
        except Exception:
            continue
        label = str(d.get("Label", "")).replace("com.trident.", "")
        if not label:
            continue
        interval = d.get("StartInterval")
        cal = d.get("StartCalendarInterval")
        if interval:
            out[_norm(label)] = f"{interval}s"
        elif cal:
            if isinstance(cal, list):
                out[_norm(label)] = "日历:" + ",".join(
                    f"{c.get('Hour', '*')}:{c.get('Minute', 0):02d}" for c in cal)
            else:
                out[_norm(label)] = f"日历{cal.get('Hour', '*')}:{cal.get('Minute', 0):02d}"
    return out


def advice(n: int, every: str) -> str:
    sec = int(re.match(r"(\d+)s", every).group(1)) if re.match(r"(\d+)s", every) else None
    if n >= 1000:
        return "**P0**：事务里夹了网络/取价等待，先缩短事务（按批 commit），再谈降频"
    if sec is not None and sec <= 60 and n >= 100:
        return "**P1**：每分钟都醒还撞锁 → 合并进 ensure-core 的同一事务，或降到 5 分钟"
    if sec is not None and sec <= 60:
        return "P2：频率高但锁错误少，可暂时不动"
    if n >= 100:
        return "P1：低频也撞锁，说明别人持有长事务 → 给它加 execute_with_retry"
    return "P3：偶发，观察即可"


def main() -> int:
    locks = lock_counts()
    sched = schedules()
    rows = sorted(locks.items(), key=lambda kv: -kv[1])

    lines = [
        "# 写锁体检与合并建议",
        "",
        f"数据源：`~/.trident/log/*.log` 全量计数（**含历史累积**，不是今日增量）；"
        f"调度来自 `~/Library/LaunchAgents`（{len(sched)} 个作业）。",
        "",
        "> 口径提醒：锁错误次数高 ≠ 今天还在发生。要看今日增量，用 "
        "`overnight_report.py`（它按基线算差值）。",
        "",
        "| 作业 | 锁错误(累计) | 调度 | 建议 |",
        "|---|---|---|---|",
    ]
    for job, n in rows[:20]:
        every = sched.get(_norm(job), "未注册")
        lines.append(f"| {job} | {n} | {every} | {advice(n, every)} |")

    hot = [(j, n) for j, n in rows if n >= 100]
    lines += ["", f"## 汇总", f"- 有锁错误的作业：**{len(rows)} 个**；其中累计 ≥100 次：**{len(hot)} 个**"]
    lines.append(f"- 锁错误总量：**{sum(n for _j, n in rows)} 次**（历史累积）")
    lines += [
        "",
        "## 先做哪三件（按性价比）",
        "1. 对 `n>=1000` 的作业改「按批 commit」：一次事务里不做网络请求，"
        "写完立刻提交。锁错误通常能降一个数量级。",
        "2. 对 30s/45s/60s 三个采集作业（ensure-core / domestic-refresh / equity-quote-cron）"
        "合并成一个 batcher：三次短事务并成一次，WAL 下写者切换成本直接砍掉 2/3。",
        "3. 给所有批处理加 `execute_with_retry`（已有工具函数），"
        "不要各自写 `busy_timeout` —— 长事务场景下调 timeout 只是让人等更久。",
    ]

    text = "\n".join(lines)
    print(text)
    if "--write" in sys.argv:
        os.makedirs(os.path.dirname(os.path.abspath(REPORT)), exist_ok=True)
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
