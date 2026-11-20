#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性修复 ai_decisions.ts 口径漂移。

背景
----
ai_decisions.ts 列设计口径是 **UTC 秒 INTEGER**（v303+ 回填 + autots 触发器兜底），
replay / targets / actionable / reflection / events-today 等端点都按整数 epoch 与
lo/hi/fresh_lo 比较。但历史主路径曾把 `_ts()` 返回的**无时区 ISO 北京串**（如
'2026-11-19T15:15:46'）写进 ts，导致该列混合两种形态：
  · 67 行  integer  UTC 秒（正确，不动）
  · 289 行 text     无时区 ISO 北京串（漂移，需回填）

漂移后果：events/today 带 from/to_ts 窗口时，文本串与整数 epoch 比较类型错位，
signal_count / live_signal_count 从 79 掉到 2，前端活跃信号卡片显示 0。

修复
----
只把 typeof(ts)='text' 的行，用 created_at 解析回 UTC 秒整数写回 ts；
已是 integer/real 的行原样保留。created_at 不可解析的行跳过并记录，不硬写。

用法
----
    python3 scripts/fix_ai_decisions_ts.py            # dry-run，只统计不写
    python3 scripts/fix_ai_decisions_ts.py --apply    # 真正回填
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "trident_event_bus.db"


def _parse_to_utc(raw: str) -> int | None:
    """无时区 ISO/朴素串按北京 +08:00 解读 → UTC 秒；含偏移则按偏移解析。失败返 None。"""
    s = str(raw).strip()
    if not s:
        return None
    try:
        # 含显式时区（+/- 偏移或 Z）→ fromisoformat 直接识别
        if any(ch in s[10:] for ch in ("+", "Z")) or (len(s) > 10 and "-" in s[10:]):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=BJ)
            return int(dt.astimezone(timezone.utc).timestamp())
        # 无时区 → 按北京墙钟
        s2 = s[:19].replace("T", " ")
        dt = datetime.strptime(s2, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ)
        return int(dt.astimezone(timezone.utc).timestamp())
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写回；缺省 dry-run")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[ERR] 库不存在: {DB_PATH}")
        return 1

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        rows = conn.execute(
            """SELECT id, ts, created_at FROM ai_decisions
               WHERE typeof(ts) = 'text'"""
        ).fetchall()
        print(f"[INFO] 待检查 text 形态 ts 行数: {len(rows)}")

        fixable: list[tuple[int, int]] = []
        skipped: list[tuple[int, str]] = []
        for row_id, _ts, created_at in rows:
            utc = _parse_to_utc(created_at)
            if utc is None:
                skipped.append((row_id, str(created_at)[:30]))
                continue
            fixable.append((row_id, utc))

        print(f"[INFO] 可回填: {len(fixable)}  跳过(不可解析): {len(skipped)}")
        for row_id, raw in skipped[:10]:
            print(f"  跳过 id={row_id} created_at={raw!r}")

        if not args.apply:
            print("[DRY-RUN] 未写库。加 --apply 执行回填。")
            # 抽样展示回填前后对照
            for row_id, utc in fixable[:5]:
                bj_str = datetime.fromtimestamp(utc, BJ).strftime("%Y-%m-%d %H:%M:%S")
                print(f"  样例 id={row_id} → ts={utc} (北京 {bj_str})")
            return 0

        cur = conn.cursor()
        batch = 0
        for row_id, utc in fixable:
            cur.execute("UPDATE ai_decisions SET ts = ? WHERE id = ?", (utc, row_id))
            batch += 1
            if batch % 500 == 0:
                conn.commit()
                print(f"  ...已回填 {batch}/{len(fixable)}")
        conn.commit()

        # 复核
        remain_text = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE typeof(ts)='text'"
        ).fetchone()[0]
        n_int = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE typeof(ts) IN ('integer','real')"
        ).fetchone()[0]
        print(f"[DONE] 回填 {len(fixable)} 行；剩余 text={remain_text} integer/real={n_int}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
