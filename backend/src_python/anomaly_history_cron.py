#!/usr/bin/env python3
"""把"日线真异常点"归档进 anomaly_history（默认只打印，--apply 才写库）。

为什么做：
    anomaly_history 一直是 0 行。查下来不是没有异常（abnormal_signals 有 953 条、
    critical_price_moves 有 29 条），而是**归档这一步只有 HTTP 接口能触发**
    （bridge_server 的 /api/tech/anomaly?persist=1 才会调 intraday_store.save_anomalies）。
    没有任何定时作业去调它 → 表永远是空的，前端"历史异常"面板也就永远没数据。

做法：
    · 按品种读 ohlcv_daily 的日线 OHLCV
    · 用 true_anomaly.detect()（与接口同一套判据）检测真异常点
    · 幂等写入 anomaly_history（同 asset/interval/ts/kind 只留一条）

用法：
    python3 anomaly_history_cron.py            # 试运行，只打印统计
    python3 anomaly_history_cron.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import db
import intraday_store
from true_anomaly import detect as detect_anomaly


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只试运行）")
    ap.add_argument("--days", type=int, default=120, help="只看最近多少天")
    ap.add_argument("--include-history", action="store_true",
                    help="允许处理纪元之前的行（默认不允许：会污染'当前'口径）")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    epoch = int(conn.execute(
        "SELECT strftime('%s', started_at) FROM data_epoch ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0])
    cutoff = int(conn.execute("SELECT strftime('%s','now')").fetchone()[0]) - args.days * 86400
    if not args.include_history:
        # 纪元口径：只归档纪元之后的异常点。实测 ohlcv_daily 里 9 月那批是清空前的历史，
        # 归档进来会让"历史异常"面板把纪元前的旧行情当成当前样本。
        cutoff = max(cutoff, epoch)

    assets = [r[0] for r in conn.execute(
        "SELECT asset, COUNT(*) n FROM ohlcv_daily WHERE ts >= ? "
        "GROUP BY asset HAVING n >= 60 ORDER BY n DESC", (cutoff,)).fetchall()]

    total_hits = total_written = 0
    for asset in assets:
        raw = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM ohlcv_daily "
            "WHERE asset=? AND ts >= ? ORDER BY ts ASC", (asset, cutoff)).fetchall()
        bars = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                 "close": r[4], "volume": r[5]} for r in raw]
        hits = detect_anomaly(bars)
        if not hits:
            continue
        total_hits += len(hits)
        if args.apply:
            total_written += intraday_store.save_anomalies(asset, "1d", hits)
        print(f"  {asset:<10} bars={len(bars):<5} 异常点 {len(hits)}")

    print(f"\n合计异常点 {total_hits} · 写入 {total_written}（interval=1d）")
    if not args.apply:
        print("(试运行，未写库 — 加 --apply 生效)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
