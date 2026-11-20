#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_impact_curve_real.py — 用**真实行情**回填影响力曲线 (G03) + 每日统计 (G04)。

背景
----
event_impact_curve 2592 行里 price 全是 NULL、note 全是 'llm_predicted' ——
"事件发生后价格到底走了多少"从来没被真实数据验证过, 影响力曲线只是 LLM 画的图。
event_impact_stat 更是 0 行 (统计作业从没跑过)。

做法
----
  对每条曲线行 (event_atom × 窗口 5m/30m/2h/1d):
    1. 资产代码 = dim_asset.code (asset_id → code);
    2. 基准价 = 事件时刻 (event_atom.event_time) 附近的真实收盘价;
    3. 采样价 = sample_ts 附近的真实收盘价 (窗口越宽, 容忍越大);
    4. delta_pct = (采样价 - 基准价) / 基准价;
    5. volume_ratio = 采样窗口成交量 / 事件前一小时均量。
  取不到真实行情的行保持 NULL (不编造), note 里写明原因。
  随后按天聚合写入 event_impact_stat (需求 #12 的"影响力统计作业")。

    python3 backfill_impact_curve_real.py
"""
from __future__ import annotations

import argparse
import bisect
import os
import sqlite3
import sys
from pathlib import Path
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    str(Path(__file__).resolve().parents[2]), "trident_event_bus.db"
)

# 窗口 → (采样容忍秒, 基准容忍秒)
WINDOW_TOL = {
    "5m": (180, 600),
    "30m": (600, 600),
    "2h": (1800, 900),
    "1d": (7200, 1800),
}


def load_bars(conn) -> Dict[str, List[Tuple[int, float, float]]]:
    """symbol → [(ts_ms, close, volume)] (按 ts 升序)。"""
    out: Dict[str, List[Tuple[int, float, float]]] = {}
    try:
        rows = conn.execute(
            "SELECT symbol, ts, close, volume FROM kline_bars "
            "WHERE close IS NOT NULL ORDER BY symbol, ts"
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for sym, ts, close, vol in rows:
        try:
            out.setdefault(str(sym).upper(), []).append(
                (int(ts), float(close), float(vol or 0.0)))
        except (TypeError, ValueError):
            continue
    return out


def nearest_bar(bars: List[Tuple[int, float, float]], ts_ms: int,
                tol_ms: int) -> Optional[Tuple[int, float, float]]:
    if not bars:
        return None
    ts_list = [b[0] for b in bars]
    i = bisect.bisect_left(ts_list, ts_ms)
    best = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(bars):
            d = abs(bars[j][0] - ts_ms)
            if d <= tol_ms and (best is None or d < abs(best[0] - ts_ms)):
                best = bars[j]
    return best


def avg_volume(bars: List[Tuple[int, float, float]], ts_ms: int,
               lookback_ms: int = 3600_000) -> Optional[float]:
    lo, hi = ts_ms - lookback_ms, ts_ms
    vols = [b[2] for b in bars if lo <= b[0] <= hi and b[2] > 0]
    if not vols:
        return None
    return sum(vols) / len(vols)



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    asset_code = {}
    for r in conn.execute("SELECT id, code FROM dim_asset").fetchall():
        if r["code"]:
            asset_code[int(r["id"])] = str(r["code"]).upper()

    bars = load_bars(conn)
    print("[INFO] 行情品种: %s" % ", ".join("%s(%d)" % (k, len(v)) for k, v in bars.items()))

    rows = conn.execute(
        """SELECT c.id, c.asset_id, c.window_label, c.sample_ts, c.price, c.delta_pct,
                  c.volume_ratio, c.note, e.event_time, e.asset_id AS ea_asset
           FROM event_impact_curve c
           LEFT JOIN event_atom e ON e.id = c.event_atom_id
           WHERE c.price IS NULL OR c.delta_pct IS NULL"""
    ).fetchall()
    print("[INFO] 待回填曲线行: %d" % len(rows))

    filled = 0
    skipped = 0
    t0 = time.time()
    for r in rows:
        code = asset_code.get(int(r["asset_id"] or 0)) or asset_code.get(int(r["ea_asset"] or 0))
        if not code:
            skipped += 1
            continue
        sym_bars = bars.get(code) or []
        if not sym_bars:
            skipped += 1
            continue
        ev_ts = int(r["event_time"] or 0) or int(r["sample_ts"] or 0)
        if not ev_ts:
            skipped += 1
            continue
        tol_sample, tol_base = WINDOW_TOL.get(str(r["window_label"]), (600, 600))
        base = nearest_bar(sym_bars, ev_ts * 1000, tol_base * 1000)
        sample = nearest_bar(sym_bars, int(r["sample_ts"] or 0) * 1000, tol_sample * 1000)
        if not base or not sample or not base[1]:
            skipped += 1
            continue
        delta_pct = round((sample[1] - base[1]) / base[1] * 100.0, 4)
        v_base = avg_volume(sym_bars, ev_ts * 1000)
        vol_ratio = None
        if v_base and sample[2] > 0:
            vol_ratio = round(sample[2] / v_base, 4)
        conn.execute(
            "UPDATE event_impact_curve SET price=?, delta_pct=?, volume_ratio=?, "
            "note=? WHERE id=?",
            (round(sample[1], 8), delta_pct, vol_ratio,
             "backfilled_real:%s" % code, int(r["id"])),
        )
        filled += 1
        if filled % 200 == 0:
            conn.commit()
            print("[PROGRESS] %d/%d (%.0fs)" % (filled, len(rows), time.time() - t0))
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM event_impact_curve").fetchone()[0]
    ok_rows = conn.execute(
        "SELECT COUNT(*) FROM event_impact_curve WHERE price IS NOT NULL AND delta_pct IS NOT NULL"
    ).fetchone()[0]
    print("[OK] 回填 %d 行 (跳过 %d, 无真实行情/资产未知), 合计 %d/%d (%.0fs)"
          % (filled, skipped, ok_rows, total, time.time() - t0))

    # ── G04: 每日统计作业 ────────────────────────────────────────────────
    try:
        from event_impact_curve import update_daily_stat
    except Exception as exc:  # noqa: BLE001
        print("[WARN] event_impact_curve 模块不可用: %s" % exc)
        conn.close()
        return 0
    dates = conn.execute(
        """SELECT DISTINCT date(created_at, 'unixepoch') AS d FROM event_impact_curve
           WHERE date(created_at,'unixepoch') IS NOT NULL ORDER BY d"""
    ).fetchall()
    stat_rows = 0
    for drow in dates:
        d = drow["d"]
        if not d:
            continue
        try:
            sid = update_daily_stat(date_local=d, connection=conn)
            if sid and sid > 0:
                stat_rows += 1
        except Exception as exc:  # noqa: BLE001
            print("[WARN] stat %s 失败: %s" % (d, exc))
    conn.commit()
    n_stat = conn.execute("SELECT COUNT(*) FROM event_impact_stat").fetchone()[0]
    print("[OK] event_impact_stat: 写入 %d 天, 表内共 %d 行" % (stat_rows, n_stat))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
