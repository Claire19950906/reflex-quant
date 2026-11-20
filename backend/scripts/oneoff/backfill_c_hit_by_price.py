#!/usr/bin/env python3
"""按真实行情重算 C 维"错因是否成立"（hit），修正历史上恒为 0 的记录。

为什么要回填：
    c_technical_reflections 判定"断流期间有没有强异动"时，只查了
    `critical_price_moves` —— 那张表全库只有 34 行（只有被判成"关键异动"的事件才进）。
    结果 506 条 C 维全部 hit=0（"断流期间无强异动"），错因成立率永远是 0%，
    下游看到的结论是"C 维从没抓到过真问题"，而实际库里躺着 3 万条 failed 记录、
    28 万根分钟线。

做法：
    对每条 data_feed_timeout / feed_missing 记录：
      · 从 text 里取"断流 Nh"窗口（created_at 起 Nh 小时）
      · 用 ohlcv_intraday（没有则 kline_bars 1m）算窗口内相对首根收盘的最大涨跌幅
      · |涨幅| ≥ STRONG_MOVE_PCT(2%) → hit=1，否则 hit=0
      · 只改 hit / actual_outcome_pct / text，不动 created_at（原始时间不篡改）

用法：
    python3 backfill_c_hit_by_price.py            # 试运行，只打印分布
    python3 backfill_c_hit_by_price.py --apply
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db
from c_technical_reflections import STRONG_MOVE_PCT, _max_move_in_window


def _parse_created(text: str | None) -> int | None:
    """解析 created_at。库里两种格式并存：
    'YYYY-MM-DDTHH:MM:SS+00:00'（带时区）与 'YYYY-MM-DD HH:MM:SS'（裸时间串=北京时间）。"""
    if not text:
        return None
    raw = str(text).strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:          # 裸时间串按北京时间解释
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(dt.timestamp())

_GAP_RE = re.compile(r"断流\s*([\d.]+)\s*h")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")

    rows = conn.execute(
        "SELECT id, asset, created_at, hit, actual_outcome_pct, reflection_text "
        "FROM trade_reflections_v2 WHERE dimension='C' AND subcategory IN "
        "('data_feed_timeout','feed_missing')").fetchall()

    changed = measured_none = 0
    flips = []
    for rid, asset, created, old_hit, old_pct, text in rows:
        start = _parse_created(created)
        if not start:
            measured_none += 1
            continue
        m = _GAP_RE.search(text or "")
        gap_h = float(m.group(1)) if m else 1.0
        end = int(start + gap_h * 3600)
        mv = _max_move_in_window(conn, asset, int(start), end)
        if mv is None:
            measured_none += 1
            continue
        new_hit = 1 if mv >= STRONG_MOVE_PCT else 0
        new_pct = round(mv, 2)
        if new_hit != old_hit or abs((old_pct or 0) - new_pct) > 0.01:
            changed += 1
            flips.append((asset, gap_h, old_hit, new_hit, new_pct))
            if args.apply:
                note = (f"断流期间最大波动 {new_pct:+.2f}%" if new_hit
                        else f"断流期间最大波动仅 {new_pct:.2f}%")
                conn.execute(
                    "UPDATE trade_reflections_v2 SET hit=?, actual_outcome_pct=?, "
                    "reflection_text=? WHERE id=?",
                    (new_hit, new_pct,
                     f"行情断流 {gap_h}h：{note}", rid))

    print(f"C 维断流类记录 {len(rows)} 条 · 可测量 {len(rows) - measured_none} · "
          f"取值有变化 {changed}")
    hit_new = sum(1 for f in flips if f[3] == 1)
    print(f"  重算后 hit=1 的有 {hit_new} 条（即'断流期间确实错过强异动'）")
    for asset, gap, oh, nh, pct in sorted(flips, key=lambda x: -abs(x[4]))[:6]:
        print(f"   {asset:<8} 断流 {gap:>5.1f}h  hit {oh}→{nh}  窗口最大波动 {pct:+.2f}%")
    if not args.apply:
        print("\n(试运行，未写库 — 加 --apply 生效)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
