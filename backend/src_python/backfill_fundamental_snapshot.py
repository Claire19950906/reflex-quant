#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_fundamental_snapshot.py — 基本面快照估值/宏观倾向回填（v490 P1-3）

背景（2026-11-19 实测）：decision_fundamental_snapshot 20 行里
valuation（估值分位）、macro_tilt（宏观倾向）几乎全空。根因：
  · 宏观倾斜 macro_equity_regime 只映射股票/加密股板块，BTC/WTI/XAU 这类
    大宗/加密标的返回"板块未映射" → macro_tilt=NULL；
  · 估值分位需要 PE/PB 历史样本，采集窗口不足时直接留空。

本脚本做**只读现有库**的幂等回填，不联网、不改分数：
  · macro_tilt：股票/加密股走 macro_equity_regime；大宗/加密按品种兜底分类
    取对应板块倾斜（crypto_asset / 风险资产背景）；取不到保持 NULL；
  · valuation：仅当 cn_valuation_snapshots / 本地 PE-PB 样本可得时回填，
    无样本保持 NULL（不伪造估值分位）。

用法:
    python3 backfill_fundamental_snapshot.py            # dry-run
    python3 backfill_fundamental_snapshot.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db  # noqa: E402

# 大宗/加密品种 → 宏观板块兜底（macro_equity_regime.SECTOR_SENSITIVITY 内的桶）
_ASSET_FALLBACK_SECTOR = {
    "BTC": "crypto_asset",
    "ETH": "crypto_asset",
    "SOL": "crypto_asset",
    "XRP": "crypto_asset",
    "BNB": "crypto_asset",
    "DOGE": "crypto_asset",
    "ADA": "crypto_asset",
    "COIN": "crypto_miner",
    "MSTR": "crypto_miner",
    "CLSK": "crypto_miner",
    "RIOT": "crypto_miner",
}


def _tilt_for(conn, asset: str, ticker: str, regime) -> tuple[float | None, str | None]:
    """返回 (macro_tilt, sector)。股票走 sector_of；大宗/加密走兜底。"""
    import macro_equity_regime as mr
    t = (ticker or "").strip().upper()
    a = (asset or "").strip().upper()
    # 先按 ticker 走正式板块映射
    if t:
        res = mr.tilt_for(t, conn, regime=regime)
        if res.get("available") and res.get("tilt") is not None:
            return float(res["tilt"]), res.get("sector")
    # 兜底：按品种映射到风险资产板块
    sector = _ASSET_FALLBACK_SECTOR.get(a)
    if sector and regime:
        tilt = (regime.get("tilt") or {}).get(sector)
        if tilt is not None:
            return float(tilt), sector
    return None, None


def _valuation_for(conn, ticker: str, asset: str) -> tuple[float | None, str | None]:
    """从本地 PE/PB 快照算一个粗估值分位；无样本返回 (None, None)。"""
    t = (ticker or "").strip().upper()
    if not t:
        return None, None
    try:
        rows = conn.execute(
            "SELECT pe, pb FROM cn_valuation_snapshots WHERE ticker=? "
            "AND pe IS NOT NULL ORDER BY ts DESC LIMIT 120",
            (t,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    pes = [float(r[0]) for r in rows if r[0] is not None]
    if len(pes) < 20:
        return None, None
    pe_now = pes[0]
    # 估值分位：当前 PE 在历史样本里的位置（0=最便宜，1=最贵）；反向映射成
    # "便宜程度" 0~1，与 fundamental_factor 的 valuation 口径同向。
    rank = sum(1 for p in pes if p <= pe_now)
    pct_pos = rank / len(pes)
    cheap = round(max(0.0, min(1.0, 1.0 - pct_pos)), 4)
    return cheap, "pe_percentile"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(db.get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    try:
        import macro_equity_regime as mr
        regime = mr.cached_build(conn)
    except Exception as exc:
        print(f"宏观 regime 不可用，跳过 macro_tilt 回填: {type(exc).__name__}: {exc}")
        regime = None

    rows = conn.execute(
        """SELECT decision_id, asset, ticker, macro_tilt, valuation, valuation_basis
             FROM decision_fundamental_snapshot"""
    ).fetchall()

    plan: list[tuple] = []
    stats = {"tilt_filled": 0, "valuation_filled": 0, "skipped": 0}
    for r in rows:
        did = int(r["decision_id"])
        asset = r["asset"] or ""
        ticker = r["ticker"] or ""
        tilt, sector = _tilt_for(conn, asset, ticker, regime) if regime else (None, None)
        val, basis = _valuation_for(conn, ticker, asset)

        new_tilt = r["macro_tilt"]
        new_val = r["valuation"]
        new_basis = r["valuation_basis"]
        if new_tilt is None and tilt is not None:
            new_tilt = round(tilt, 4)
            stats["tilt_filled"] += 1
        if new_val is None and val is not None:
            new_val = val
            new_basis = basis
            stats["valuation_filled"] += 1
        if new_tilt == r["macro_tilt"] and new_val == r["valuation"]:
            stats["skipped"] += 1
            continue
        plan.append((new_tilt, new_val, new_basis, did))

    print(f"扫描 {len(rows)} 行；可补 macro_tilt {stats['tilt_filled']}，"
          f"valuation {stats['valuation_filled']}，无需改动 {stats['skipped']}")

    if not plan:
        conn.close()
        return 0
    if not args.apply:
        print("(dry-run，未写库 — 加 --apply 生效)")
        conn.close()
        return 0

    now = int(time.time())
    for new_tilt, new_val, new_basis, did in plan:
        conn.execute(
            """UPDATE decision_fundamental_snapshot
                  SET macro_tilt=?, valuation=?, valuation_basis=?
                WHERE decision_id=?""",
            (new_tilt, new_val, new_basis, did),
        )
    conn.commit()
    print(f"已回填 {len(plan)} 行")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
