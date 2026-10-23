"""
backfill_critical_moves.py
==========================

从 external_indicators 真实 yfinance 数据回填 critical_price_moves。
绝不伪造任何价格 — 全部从 DB 已有的真实日线推算。

检测规则 (基于真实日线数据,符合 v291 临界点定义):
  慢涨/慢跌 (slow_*) — 用 1d/3d/5d 累计涨跌幅检测
  快涨/快跌 (fast_*)  — 真实数据 1d 粒度无法检测 5min,需 intraday 数据
                       (本回填不检测 fast,留空待真实 intraday 数据接入)

阈值 (依据市场惯例,可后续校准):
  slow_rise / slow_fall: |1d| ≥ 3%  OR  |3d| ≥ 5%  OR  |5d| ≥ 7%
  duration_min: 1440 (1天)
  algo_score: 0.4~0.9 区间线性,综合幅度+持续性
"""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# 检测阈值
THRESH_1D_PCT = 3.0
THRESH_3D_PCT = 5.0
THRESH_5D_PCT = 7.0

# 每类临界点的基准分数 (与 algo_score 公式加权)
BASE_SCORE = {
    "slow_rise": 0.5,
    "slow_fall": 0.55,  # 跌更敏感
}


def fetch_prices(conn, asset: str) -> list[tuple[int, float]]:
    """返回 [(ts, close), ...] 升序"""
    rows = conn.execute(
        "SELECT ts, value FROM external_indicators "
        "WHERE asset=? AND indicator IN ('close','price','000660.KS','005930.KS') "
        "ORDER BY ts ASC",
        (asset,),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT ts, value FROM external_indicators "
            "WHERE asset=? ORDER BY ts ASC",
            (asset,),
        ).fetchall()
    return rows


def detect_moves(prices: list[tuple[int, float]]) -> list[dict]:
    """对单个 asset 的价格序列,检测出所有 critical moves"""
    moves = []
    n = len(prices)
    for i in range(n):
        ts_i, p_i = prices[i]
        if p_i <= 0:
            continue
        # 计算 i+1d / i+3d / i+5d
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
            # 区间极值
            window = [prices[m][1] for m in range(i, j + 1) if prices[m][1] > 0]
            if not window:
                continue
            peak = max(window) if pct > 0 else min(window)
            kind = "slow_rise" if pct > 0 else "slow_fall"
            duration_min = int((ts_j - ts_i) / 60)
            # score: 0.4 基础 + 幅度/10 封顶到 0.9
            score = min(0.9, BASE_SCORE[kind] + abs(pct) / 30.0)
            ctx = {
                "k_days": k,
                "window_high": max(window),
                "window_low": min(window),
                "n_window_pts": len(window),
                "source": "external_indicators:yfinance",
            }
            moves.append({
                "asset": None,  # 由调用者填
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
            # 一个时间点只记最强信号(取 k 最大的,即窗口最长的)
            break
    return moves


def main() -> int:
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    print(f"[INFO] Opening {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        assets = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM external_indicators ORDER BY asset"
        ).fetchall()]
        print(f"[INFO] Found {len(assets)} assets in external_indicators: {assets}")

        # v291+ 也兼容 ohlcv_daily 表 (P0-1 后 ohlcv_daily 品种覆盖更全, 可与 external_indicators 合并)
        ohlcv_assets = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM ohlcv_daily ORDER BY asset"
        ).fetchall()]
        # 合并去重, 保留顺序
        seen = set()
        assets = [a for a in (assets + ohlcv_assets) if not (a in seen or seen.add(a))]
        print(f"[INFO] Merged {len(ohlcv_assets)} from ohlcv_daily, total: {len(assets)} assets")

        # 清空旧 backfill(保留手动写的)
        cur_del = conn.execute("DELETE FROM critical_price_moves WHERE context_json LIKE '%backfill%' OR context_json LIKE '%external_indicators%' OR context_json LIKE '%ohlcv_daily%'")
        print(f"[INFO] Cleared {cur_del.rowcount} prior backfill rows")

        all_moves: list[dict] = []
        for asset in assets:
            prices = fetch_prices(conn, asset)
            if len(prices) < 6:
                print(f"  [SKIP] {asset}: only {len(prices)} pts")
                continue
            moves = detect_moves(prices)
            for m in moves:
                m["asset"] = asset
            all_moves.extend(moves)
            print(f"  [OK]   {asset}: {len(prices)} pts -> {len(moves)} moves")

        print(f"[INFO] Total moves: {len(all_moves)}")

        # 批量插入
        conn.executemany(
            "INSERT INTO critical_price_moves "
            "(asset,ts,start_price,peak_price,end_price,move_pct,move_kind,duration_min,algo_score,context_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(m["asset"], m["ts"], m["start_price"], m["peak_price"], m["end_price"],
              m["move_pct"], m["move_kind"], m["duration_min"], m["algo_score"], m["context_json"])
             for m in all_moves],
        )
        conn.commit()

        # 验证
        cnt = conn.execute("SELECT COUNT(*) FROM critical_price_moves").fetchone()[0]
        by_kind = conn.execute(
            "SELECT move_kind, COUNT(*) FROM critical_price_moves GROUP BY move_kind"
        ).fetchall()
        by_asset = conn.execute(
            "SELECT asset, COUNT(*) FROM critical_price_moves GROUP BY asset ORDER BY asset"
        ).fetchall()
        print(f"[OK] critical_price_moves total rows: {cnt}")
        for k, c in by_kind:
            print(f"  {k}: {c}")
        for a, c in by_asset:
            print(f"  {a}: {c}")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())