"""
backfill_critical_moves_late.py
================================

补 critical_price_moves 2026-10-16/17/18 缺口 (现有最新 2026-10-15).
从 ohlcv_daily 真实日线扫描,不伪造.

阈值与 backfill_critical_moves.py 一致:
  slow_rise/fall: |1d| >= 3% OR |3d| >= 5% OR |5d| >= 7%

用法:
  python3 backend/src_python/backfill_critical_moves_late.py [--days 5]
"""
from __future__ import annotations
import json
import sqlite3
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

THRESH_1D_PCT = 3.0
THRESH_3D_PCT = 5.0
THRESH_5D_PCT = 7.0
BASE_SCORE = {"slow_rise": 0.5, "slow_fall": 0.55}


def fetch_prices(conn, asset: str, since_ts: int) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND ts>=? ORDER BY ts ASC",
        (asset, since_ts),
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows if r[1] and r[1] > 0]


def detect_moves(prices: list[tuple[int, float]]) -> list[dict]:
    moves = []
    n = len(prices)
    for i in range(n):
        ts_i, p_i = prices[i]
        for k, thresh in [(1, THRESH_1D_PCT), (3, THRESH_3D_PCT), (5, THRESH_5D_PCT)]:
            j = i + k
            if j >= n:
                continue
            ts_j, p_j = prices[j]
            if p_j <= 0:
                continue
            pct = (p_j - p_i) / p_i * 100.0
            if abs(pct) < thresh:
                continue
            window = [p for _, p in prices[i:j + 1] if p > 0]
            if not window:
                continue
            peak = max(window) if pct > 0 else min(window)
            kind = "slow_rise" if pct > 0 else "slow_fall"
            duration_min = int((ts_j - ts_i) / 60)
            score = min(0.9, BASE_SCORE[kind] + abs(pct) / 30.0)
            ctx = {
                "k_days": k,
                "window_high": max(window),
                "window_low": min(window),
                "n_window_pts": len(window),
                "source": "ohlcv_daily:backfill_late",
            }
            moves.append({
                "asset": None,
                "ts": ts_i,
                "start_price": p_i,
                "peak_price": peak,
                "end_price": p_j,
                "move_pct": round(pct, 3),
                "move_kind": kind,
                "duration_min": duration_min,
                "algo_score": round(score, 3),
                "context_json": json.dumps(ctx, ensure_ascii=False),
            })
            break
    return moves


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5, help="回填最近 N 天")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    since_ts = int(datetime.now(tz=timezone.utc).timestamp()) - args.days * 86400
    print(f"[INFO] Backfilling critical_price_moves since {datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        assets = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM ohlcv_daily ORDER BY asset"
        ).fetchall()]
        print(f"[INFO] Found {len(assets)} assets: {assets}")

        latest_existing = conn.execute(
            "SELECT MAX(ts) FROM critical_price_moves"
        ).fetchone()[0]
        print(f"[INFO] Latest existing critical_price_moves.ts = {latest_existing} ({datetime.fromtimestamp(latest_existing, tz=timezone.utc).isoformat()})")

        all_moves: list[dict] = []
        for asset in assets:
            prices = fetch_prices(conn, asset, since_ts)
            if len(prices) < 6:
                print(f"  [SKIP] {asset}: only {len(prices)} pts in window")
                continue
            moves = detect_moves(prices)
            for m in moves:
                m["asset"] = asset
            all_moves.extend(moves)
            print(f"  [OK]   {asset}: {len(prices)} pts -> {len(moves)} new moves")

        if not all_moves:
            print("[INFO] No new moves to insert")
            return 0

        # 幂等: 过滤已存在 (asset, ts, move_kind)
        existing = set(
            (r[0], r[1], r[2]) for r in conn.execute(
                "SELECT asset, ts, move_kind FROM critical_price_moves"
            ).fetchall()
        )
        before = conn.execute("SELECT COUNT(*) FROM critical_price_moves").fetchone()[0]
        to_insert = [m for m in all_moves if (m["asset"], m["ts"], m["move_kind"]) not in existing]
        skipped = len(all_moves) - len(to_insert)
        if not to_insert:
            print(f"[INFO] All {len(all_moves)} detected moves already exist (no-op)")
            return 0
        conn.executemany(
            "INSERT INTO critical_price_moves "
            "(asset,ts,start_price,peak_price,end_price,move_pct,move_kind,duration_min,algo_score,context_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(m["asset"], m["ts"], m["start_price"], m["peak_price"], m["end_price"],
              m["move_pct"], m["move_kind"], m["duration_min"], m["algo_score"], m["context_json"])
             for m in to_insert],
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM critical_price_moves").fetchone()[0]
        print(f"[OK] Inserted {after - before} new rows ({before} -> {after}), skipped {skipped} duplicates")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())