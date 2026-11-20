# -*- coding: utf-8 -*-
"""impact_curve_fill.py — 影响曲线「真实价格回填」增量作业 (v477)

问题
----
`event_impact_curve` 的曲线点是在事件落库时**预先排好的采样时刻**
(sample_ts = 事件时间 + 5m/30m/2h/1d), 当时只有 LLM 预测的 delta_pct,
`price` 一律为 NULL, note='llm_predicted'。

之后**没有任何进程回来补真实价格**, 所以:
  · `event_impact_stat.real_filled` 恒为 0
  · `avg_impact_pct` / `direction_hit_rate` 恒为 0.0
  · 归因页「新闻落在 K 线上、影响多大、多久」实际展示的是 LLM 画的图, 不是市场事实

本模块做一件事: 把**已经到期**(sample_ts <= now) 且 price 仍为 NULL 的点,
用 `kline_bars` 的真实收盘价回填:

    基准价 = 事件时刻附近最近一根 K 线收盘价
    采样价 = sample_ts 附近最近一根 K 线收盘价
    delta_pct = (采样价 - 基准价) / 基准价 × 100
    volume_ratio = 采样 bar 成交量 / 事件前一小时均量

取不到真实行情的行**保持 NULL**(不编造), note 写明原因, 供上游如实归零。

用法:
    python3 impact_curve_fill.py            # 回填到期点 + 刷新统计
    python3 impact_curve_fill.py --no-stat  # 只回填, 不算统计
    python3 impact_curve_fill.py --dry-run  # 只报告
"""
from __future__ import annotations

import argparse
import bisect
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or str(BACKEND / "trident_event_bus.db")

# 窗口 → (采样容忍秒, 基准容忍秒)。窗口越宽容忍越大, 与 oneoff 回填口径一致。
WINDOW_TOL = {
    # v479：补 20m —— 决策的结算 horizon 是 QUICK_SIM_HORIZON_MINUTES=20 分钟，
    # 而影响曲线原本只有 5m/30m/2h/1d，没有任何一个窗口和"实际持仓到期点"对齐，
    # 于是"事件冲击"和"这笔单赚没赚"永远对不上账。20m 就是决策自己的度量尺。
    "20m": (240, 600),
    # v479：持有期已重标为 high=240 分钟，4h 才是真正的主结算点，曲线要跟着对齐
    "4h": (3600, 1800),
    "5m": (180, 600),
    "30m": (600, 600),
    "2h": (1800, 900),
    "1d": (7200, 1800),
}

Bar = Tuple[int, float, float]


def load_bars(conn: sqlite3.Connection) -> Dict[str, List[Bar]]:
    """symbol → [(ts_ms, close, volume)]，按 ts 升序。"""
    out: Dict[str, List[Bar]] = {}
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


def nearest_bar(bars: List[Bar], ts_ms: int, tol_ms: int) -> Optional[Bar]:
    if not bars:
        return None
    ts_list = [b[0] for b in bars]
    i = bisect.bisect_left(ts_list, ts_ms)
    best: Optional[Bar] = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(bars):
            d = abs(bars[j][0] - ts_ms)
            if d <= tol_ms and (best is None or d < abs(best[0] - ts_ms)):
                best = bars[j]
    return best


def avg_volume(bars: List[Bar], ts_ms: int, lookback_ms: int = 3_600_000) -> Optional[float]:
    lo, hi = ts_ms - lookback_ms, ts_ms
    vols = [b[2] for b in bars if lo <= b[0] <= hi and b[2] > 0]
    if not vols:
        return None
    return sum(vols) / len(vols)


def _asset_codes(conn: sqlite3.Connection) -> Dict[int, str]:
    out: Dict[int, str] = {}
    try:
        for r in conn.execute("SELECT id, code FROM dim_asset").fetchall():
            if r[1]:
                out[int(r[0])] = str(r[1]).upper()
    except sqlite3.Error:
        pass
    return out


def fill_due(conn: sqlite3.Connection, dry_run: bool = False,
             lag_sec: int = 60) -> Dict[str, int]:
    """回填所有已到期、price 仍为 NULL 的曲线点。

    返回 {"due": 到期行数, "filled": 回填数, "skipped": 跳过数}
    """
    now = int(time.time())
    cutoff = now - lag_sec
    codes = _asset_codes(conn)
    try:
        rows = conn.execute(
            """SELECT c.id, c.asset_id, c.window_label, c.sample_ts,
                      e.event_time, e.asset_id AS ea_asset
               FROM event_impact_curve c
               LEFT JOIN event_atom e ON e.id = c.event_atom_id
               WHERE c.price IS NULL AND c.sample_ts <= ?""",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return {"due": 0, "filled": 0, "skipped": 0, "error": str(exc)[:80]}
    if not rows:
        return {"due": 0, "filled": 0, "skipped": 0}

    bars = load_bars(conn)
    filled = skipped = 0
    touched_atoms: set[int] = set()
    for r in rows:
        code = codes.get(int(r["asset_id"] or 0)) or codes.get(int(r["ea_asset"] or 0))
        sym_bars = bars.get(code or "") or []
        ev_ts = int(r["event_time"] or 0) or int(r["sample_ts"] or 0)
        if not sym_bars or not ev_ts:
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
        vol_ratio = round(sample[2] / v_base, 4) if (v_base and sample[2] > 0) else None
        if dry_run:
            filled += 1
            continue
        conn.execute(
            "UPDATE event_impact_curve SET price=?, delta_pct=?, volume_ratio=?, "
            "note=? WHERE id=?",
            (round(sample[1], 8), delta_pct, vol_ratio,
             "backfilled_real:%s" % code, int(r["id"])),
        )
        filled += 1
        # 记录受影响的 (资产, 事件日) 供统计按天重算 —— 统计表按天聚合,
        # 这里只标记即可, 具体重算交给 compute_and_store 全量重跑(行数很小)。
        try:
            touched_atoms.add(int(r["asset_id"] or r["ea_asset"] or 0))
        except (TypeError, ValueError):
            pass
    if not dry_run:
        conn.commit()
    return {"due": len(rows), "filled": filled, "skipped": skipped}


def run(conn: sqlite3.Connection, do_stat: bool = True, dry_run: bool = False) -> Dict[str, int]:
    out = fill_due(conn, dry_run=dry_run)
    if do_stat and not dry_run:
        try:
            from impact_statistics import compute_and_store
            stat = compute_and_store(conn, days=120)
            out["stat_buckets"] = int(stat.get("buckets", 0))
            out["stat_written"] = int(stat.get("written", 0))
        except Exception as exc:
            out["stat_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:80])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--no-stat", action="store_true", help="只回填曲线, 不刷新 event_impact_stat")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        res = run(conn, do_stat=not args.no_stat, dry_run=args.dry_run)
        print("[IMPACT-FILL] " + ", ".join("%s=%s" % (k, v) for k, v in res.items()), flush=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
