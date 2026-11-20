"""opt_ts_audit.py —— 时间戳单位审计（只读）

背景：库里同时存在**秒**（1.7e9）与**毫秒**（1.7e12）两种时间戳。
跨表 JOIN / 区间比较时若混用，条件恒为假或恒为真 —— 而且**不会报错**，
只会让"覆盖率""命中率"莫名其妙地低。

本脚本扫全部表的 ts / *_ts / *_time 列，按数量级归类，列出混用最严重的表，
供逐一核对代码里的比较逻辑。

用法：python3 opt_ts_audit.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "trident_event_bus.db"


def unit_of(v: float) -> str:
    if v >= 1e17:
        return "ns"
    if v >= 1e14:
        return "µs"
    if v >= 1e11:
        return "ms"
    if v >= 1e8:
        return "s"
    return "?"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

    buckets = {"s": 0, "ms": 0, "µs": 0, "ns": 0, "?": 0, "iso": 0}
    rows = []
    for t in tables:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        except sqlite3.Error:
            continue
        ts_cols = [c for c in cols
                   if c == "ts" or c.endswith("_ts") or c.endswith("_time")
                   or c.endswith("_at")]
        if not ts_cols:
            continue
        for c in ts_cols:
            try:
                v = conn.execute(
                    f"SELECT MAX({c}) FROM {t} WHERE {c} IS NOT NULL AND {c}>0"
                ).fetchone()[0]
            except sqlite3.Error:
                continue
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                # ISO 文本时间（如 2026-11-13T00:16:31+08:00）—— 另一种口径，
                # 与数值时间戳比较时永远不等，单独归到 "iso"
                buckets["iso"] = buckets.get("iso", 0) + 1
                rows.append(("iso", t, c, 0.0))
                continue
            u = unit_of(float(v))
            buckets[u] += 1
            rows.append((u, t, c, float(v)))

    print("时间戳列单位分布:", buckets)
    print("\n毫秒/微秒/纳秒 的列（容易被当秒用）:")
    for u, t, c, v in rows:
        if u in ("ms", "µs", "ns"):
            print(f"  {u:<3} {t}.{c}  max={v:.0f}")
    print("\nISO 文本时间列（与数值比较恒不相等）:")
    for u, t, c, v in rows:
        if u == "iso":
            print(f"  iso {t}.{c}")
    print("\n秒级的列（容易被当毫秒用）—— 只列非 ai_decisions/news 类:")
    for u, t, c, v in rows:
        if u == "s" and t not in ("ai_decisions", "raw_news"):
            print(f"  s   {t}.{c}  max={v:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
