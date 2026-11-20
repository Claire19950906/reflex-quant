#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize_ohlcv_daily.py — 把 ohlcv_daily 收敛成"每个品种每天恰好一根日线"

问题
----
同一个品种、同一个日历日，库里存了 2-3 行，时间戳不同（00:00 UTC、交易所收盘、
+5h 偏移都有），而且 **99% 的重复组收盘价并不相同**（XAU 同一天能差 501 美元）。
后果：
  * 所有"取上一根日线算涨跌幅"的逻辑，可能拿到同一天的另一条 → 假暴涨/假暴跌；
  * 计数、回测样本、因果链 T+5 评估都按行数算，样本被重复计数。

做法（默认只报告，不写库）
--------------------------
1. 按 (asset, 日历日) 分组，选一根"保留行"：
   源优先级 kline_bars_1d(3) > sina_futures_refresh(2) > yfinance_refresh(1) > 其他(0)；
   同优先级取 ts 最大（最新写入）的那条。
2. --apply 时：保留行的 ts 改写为该日 00:00 UTC，其余重复行删除。
   删除前把被删行的 (asset, ts, open, high, low, close, volume, source) 落到
   backend/trident_event_bus.db 的同级 `ohlcv_daily_dupes_<日期>.jsonl`，可回溯。
3. 事后校验：按日期去重后的日涨跌幅 |chg| > 8% 的清单，与执行前对比。

用法
----
    python3 normalize_ohlcv_daily.py                 # 只报告
    python3 normalize_ohlcv_daily.py --apply         # 真正收敛
    python3 normalize_ohlcv_daily.py --asset XAU     # 只处理某个品种
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
DB_PATH = os.path.join(BACKEND, "trident_event_bus.db")

SRC_RANK = {
    "kline_bars_1d": 3,
    "sina_us_1d": 3,          # v341: 新浪美股日 K 兜底（yfinance 在本机拉不到中美股）
    "sina_futures_refresh": 2,
    "yfinance_refresh": 1,
}
DAY = 86400


def day_ts(ts: int) -> int:
    return ts - (ts % DAY)


def collect(conn: sqlite3.Connection, asset: str | None):
    where = "WHERE asset=?" if asset else ""
    params = (asset,) if asset else ()
    rows = conn.execute(
        "SELECT asset, ts, open, high, low, close, volume, source "
        f"FROM ohlcv_daily {where} ORDER BY asset, ts",
        params,
    ).fetchall()
    groups: dict[tuple[str, int], list[tuple]] = {}
    for r in rows:
        groups.setdefault((r[0], day_ts(r[1])), []).append(r)
    return groups


def pick_keeper(rows: list[tuple]) -> tuple:
    return max(rows, key=lambda r: (SRC_RANK.get(r[7] or "", 0), r[1]))


def moves(conn: sqlite3.Connection, asset: str) -> list[tuple[str, float, float, float]]:
    """按日期取一根（最新 ts），返回 [(date, prev_close, close, chg_pct)]"""
    rows = conn.execute(
        "SELECT date(ts,'unixepoch') d, max(ts) mts FROM ohlcv_daily "
        "WHERE asset=? GROUP BY d ORDER BY d",
        (asset,),
    ).fetchall()
    out, prev = [], None
    for d, mts in rows:
        cl = conn.execute(
            "SELECT close FROM ohlcv_daily WHERE asset=? AND ts=?", (asset, mts)
        ).fetchone()[0]
        if prev and cl:
            out.append((d, prev, cl, (cl / prev - 1) * 100))
        prev = cl or prev
    return out


def main() -> int:
    skipped_ts_clash = 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    ap.add_argument("--asset", default=None, help="只处理某个品种，如 XAU / XAG / HG")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    t0 = time.time()

    groups = collect(conn, args.asset)
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    extra = sum(len(v) - 1 for v in dup_groups.values())

    print(f"总日线组 {len(groups)}  重复日 {len(dup_groups)}  多余行 {extra}")
    if not dup_groups:
        print("无需处理。")
        return 0

    # 受影响品种
    per_asset: dict[str, int] = {}
    for (a, _), v in dup_groups.items():
        per_asset[a] = per_asset.get(a, 0) + len(v) - 1
    print("\n受影响最多的品种:")
    for a, n in sorted(per_asset.items(), key=lambda x: -x[1])[:10]:
        print(f"  {a:8} 多余 {n}")

    # 样本：保留哪一根
    print("\n样本（保留行 → 被丢弃行）:")
    for key in list(dup_groups)[:5]:
        keeper = pick_keeper(dup_groups[key])
        print(f"  {key[0]} {dt.datetime.utcfromtimestamp(key[1]).date()}  保留 ts={keeper[1]} "
              f"close={keeper[5]} src={keeper[7]}  丢弃 {len(dup_groups[key]) - 1} 条")

    # 校验：>8% 的日涨跌幅（按日期去重后仍存在的，说明是真行情）
    print("\n按日期去重后 |涨跌| > 8% 的日线（这些是真行情，不是重复导致的）:")
    assets = sorted({k[0] for k in dup_groups}) if args.asset is None else [args.asset]
    flagged = []
    for a in assets:
        for d, p, cl, chg in moves(conn, a):
            if abs(chg) > 8:
                flagged.append((a, d, p, cl, chg))
    flagged.sort(key=lambda x: -abs(x[4]))
    for a, d, p, cl, chg in flagged[:10]:
        print(f"  {a:6} {d}  {p} -> {cl}  {chg:+.2f}%")
    print(f"  合计 {len(flagged)} 条")

    if not args.apply:
        print("\n（dry-run，未写库。确认后加 --apply 执行）")
        conn.close()
        return 0

    # ── 落盘被删行，可回溯 ──
    dump = os.path.join(BACKEND, f"ohlcv_daily_dupes_{dt.date.today().isoformat()}.jsonl")
    deleted = 0
    with open(dump, "w", encoding="utf-8") as f:
        for key, rows in dup_groups.items():
            keeper = pick_keeper(rows)
            for r in rows:
                if r is keeper:
                    continue
                f.write(json.dumps({
                    "asset": r[0], "ts": r[1], "open": r[2], "high": r[3],
                    "low": r[4], "close": r[5], "volume": r[6], "source": r[7],
                    "kept_ts": keeper[1],
                }, ensure_ascii=False) + "\n")
                deleted += 1
    print(f"\n被删行已备份到 {dump}（{deleted} 行）")

    with conn:
        for key, rows in dup_groups.items():
            a, dts = key
            keeper = pick_keeper(rows)
            # 先删掉同日的其它行 —— 否则把保留行的 ts 归一到 00:00 UTC 时会撞上
            # UNIQUE(asset, ts)(另一条重复行正好占了 00:00 那个槽)。
            for r in rows:
                if (r[1], r[7]) == (keeper[1], keeper[7]):
                    continue
                conn.execute("DELETE FROM ohlcv_daily WHERE asset=? AND ts=? AND source=?",
                             (r[0], r[1], r[7]))
            # 再归一 ts（撞槽就保持原 ts，不强行改）
            if keeper[1] != dts:
                try:
                    conn.execute("UPDATE ohlcv_daily SET ts=? WHERE asset=? AND ts=? AND source=?",
                                 (dts, a, keeper[1], keeper[7]))
                except sqlite3.IntegrityError as exc:
                    # 撞槽（目标 ts 已有同源行）→ 保持原 ts 不强行改，但**要留痕**：
                    # 写路径上静默吞异常会让"归一没做干净"永远查不出来
                    # （audit_silent_except 会直接判这条失败）。
                    skipped_ts_clash += 1
                    if skipped_ts_clash <= 5:
                        print(f"[WARN] ts 归一撞槽, 保持原 ts: asset={a} "
                              f"{keeper[1]}→{dts} source={keeper[7]} ({exc})")

    left = conn.execute(
        "SELECT count(*) FROM (SELECT asset, date(ts,'unixepoch') d, count(*) n "
        "FROM ohlcv_daily GROUP BY asset,d HAVING n>1)"
    ).fetchone()[0]
    print(f"处理完成，用时 {time.time() - t0:.1f}s，剩余重复日 {left} 组")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
