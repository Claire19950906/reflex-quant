#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_schedule_collisions.py — 看 87 个 launchd 作业的触发时刻是否撞车。

为什么
------
`database is locked` 反复出现在多个作业里，db_retry 加了退避也只是"排队等更久"。
真正的结构性问题是：**这些作业是不是都挤在同一分钟醒过来**。
如果 20 个作业都在 :00 醒，那 20 个写入者会同时抢同一把写锁，
再长的退避也只能让其中一个先过、其余慢慢排队。

输出
----
  · 每个分钟桶上的作业数量（按数量倒序）
  · StartInterval（固定周期）的作业单独列出，它们会在任意时刻插入
  · 建议错峰值：把同一桶里"互不依赖"的作业往后挪 1~2 分钟

用法：
    python3 scripts/audit_schedule_collisions.py
    python3 scripts/audit_schedule_collisions.py --json
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import plistlib

AGENT_DEG = os.path.expanduser("~/Library/LaunchAgents")
PATTERN = "com.trident.*.plist"


def load_jobs() -> list[dict]:
    jobs = []
    for path in sorted(glob.glob(os.path.join(AGENT_DEG, PATTERN))):
        try:
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue
        jobs.append({
            "label": data.get("Label") or os.path.basename(path).replace(".plist", ""),
            "calendar": data.get("StartCalendarInterval"),
            "interval": data.get("StartInterval"),
        })
    return jobs


def calendar_slots(cal) -> list[tuple[int, int]]:
    """StartCalendarInterval 可能是 dict 或 list[dict]；缺省字段按"每分钟"处理。"""
    if not cal:
        return []
    items = cal if isinstance(cal, list) else [cal]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        hour = it.get("Hour")
        minute = it.get("Minute", 0)
        if hour is None:
            continue
        out.append((int(hour), int(minute)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    jobs = load_jobs()
    buckets: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
    intervals: list[tuple[str, int]] = []

    for job in jobs:
        slots = calendar_slots(job["calendar"])
        for slot in slots:
            buckets[slot].append(job["label"])
        if isinstance(job["interval"], int):
            intervals.append((job["label"], job["interval"]))

    total_slots = sum(len(v) for v in buckets.values())
    payload = {
        "jobs": len(jobs),
        "calendar_entries": total_slots,
        "interval_jobs": len(intervals),
        "buckets": {f"{h:02d}:{m:02d}": sorted(v) for (h, m), v in
                    sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))},
        "intervals": sorted(intervals, key=lambda x: x[1]),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"作业总数 {len(jobs)}（含日历触发 {total_slots} 条、固定周期 {len(intervals)} 个）\n")
    print("撞车最严重的分钟桶：")
    for (h, m), labels in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:10]:
        print(f"  {h:02d}:{m:02d}  {len(labels):>2} 个作业")
        print("        " + ", ".join(lb.replace("com.trident.", "") for lb in sorted(labels)[:12])
              + (" …" if len(labels) > 12 else ""))

    if intervals:
        print("\n固定周期作业（会在任意时刻插入，天然与上面的桶抢锁）：")
        for label, sec in sorted(intervals, key=lambda x: x[1])[:12]:
            print(f"  {label.replace('com.trident.', ''):<30} 每 {sec}s")

    worst = max((len(v) for v in buckets.values()), default=0)
    print(f"\n结论：同一分钟最多 {worst} 个写入者同时醒。"
          + ("建议把同桶里非关键作业错开 1~2 分钟。" if worst >= 5 else "撞车不严重。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
