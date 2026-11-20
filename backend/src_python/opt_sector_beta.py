# -*- coding: utf-8 -*-
"""opt_sector_beta.py — v336 板块 beta 剥离（把"板块集体漂移"从异动里摘出来）

问题（实测）:
    CHANGELOG v334 已经承认: 83 条慢异动里 68 条与**同板块品种同向漂移**,
    本质是板块 beta 而不是独立事件。但归因仍把它们当"有事件驱动"去解释,
    于是产出大量看起来成立、实际是 beta 的假因果。

做法（纯统计，不调 LLM）:
    1. `peer_median_pct()` —— 同一时间窗内, 同板块其他品种的**中位涨跌幅**;
    2. `classify_beta()`   —— |peer| / |move| ≥ 0.6 且同向 → sector_beta；
                              ≥ 0.3 → beta_tilted；否则 idiosyncratic；
    3. 统计: 异动里 beta 占比、归因主表里被 beta 污染的比例。

边界:
    · 只读, 不写库;
    · 分钟线缺失时回退日线; 两者都缺 → 标 no_data, 绝不猜。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"

# 板块划分（只放本系统实际交易的品种，宁缺毋滥）
SECTORS: Dict[str, Tuple[str, ...]] = {
    "CRYPTO": ("BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE"),
    "METALS": ("XAU", "SILVER", "GOLD"),
    "ENERGY": ("WTI", "OIL", "NG"),
    "US_EQ": ("NVDA", "MU", "TSLA", "SKHYNIX", "ES", "NQ"),
    "FX": ("EUR", "CAD", "AUD"),
}

ASSET_TO_SECTOR = {a: s for s, members in SECTORS.items() for a in members}

BETA_STRONG = 0.6   # 板块解释了六成以上 → 判 beta
BETA_MILD = 0.3     # 三成以上 → beta 倾斜，但可能仍有自身事件


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _close(conn: sqlite3.Connection, asset: str, ts: int,
           prefer_intraday: bool = True, tolerance: int = 1800) -> Optional[float]:
    """取 ts 之前(含)最近收盘价。分钟线优先, 日线回退。"""
    if prefer_intraday:
        r = conn.execute(
            "SELECT close, ts FROM ohlcv_intraday WHERE asset=? AND interval='1m' AND ts<=? "
            "ORDER BY ts DESC LIMIT 1", (asset, int(ts))).fetchone()
        if r and int(r["ts"]) >= int(ts) - tolerance:
            return float(r["close"])
    r = conn.execute(
        "SELECT close, ts FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (asset, int(ts))).fetchone()
    if r:
        return float(r["close"])
    return None


def window_pct(conn: sqlite3.Connection, asset: str, ts: int, duration_min: int) -> Optional[float]:
    """窗口涨跌幅 (%)。

    口径：critical_price_moves.ts 是**异动终点（峰值时刻）**，
    窗口取 [ts - duration_min, ts]，不是 [ts, ts+duration]。
    """
    end_ts = int(ts)
    start_ts = end_ts - int(duration_min) * 60
    start = _close(conn, asset, start_ts)
    end = _close(conn, asset, end_ts)
    if not start or not end or start <= 0:
        return None
    return round((end - start) / start * 100, 3)


def peer_median_pct(conn: sqlite3.Connection, asset: str, ts: int,
                    duration_min: int, min_peers: int = 2) -> Tuple[Optional[float], List[str]]:
    """同板块其他品种的窗口涨跌幅中位数。返回 (中位数, 参与计算的品种)。"""
    sector = ASSET_TO_SECTOR.get(str(asset or "").upper())
    if not sector:
        return None, []
    vals, used = [], []
    for peer in SECTORS[sector]:
        if peer == str(asset or "").upper():
            continue
        v = window_pct(conn, peer, ts, duration_min)
        if v is not None:
            vals.append(v)
            used.append(peer)
    if len(vals) < min_peers:
        return None, used
    return round(median(vals), 3), used


def classify_beta(move_pct: float, peer_pct: Optional[float]) -> Dict[str, Any]:
    """判定这条异动有多少是板块带来的。"""
    if peer_pct is None or move_pct == 0:
        return {"label": "no_data", "ratio": None}
    same_sign = (move_pct > 0) == (peer_pct > 0)
    ratio = abs(peer_pct) / abs(move_pct)
    if same_sign and ratio >= BETA_STRONG:
        label = "sector_beta"
    elif same_sign and ratio >= BETA_MILD:
        label = "beta_tilted"
    else:
        label = "idiosyncratic"
    return {"label": label, "ratio": round(ratio, 3), "same_sign": same_sign}


def scan_moves(conn: sqlite3.Connection, limit: int = 0) -> List[Dict[str, Any]]:
    sql = ("SELECT id, asset, ts, move_pct, move_kind, duration_min FROM critical_price_moves "
           "ORDER BY ts DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for r in conn.execute(sql):
        peer, used = peer_median_pct(conn, r["asset"], r["ts"], int(r["duration_min"] or 0))
        c = classify_beta(float(r["move_pct"]), peer)
        out.append({**dict(r), "peer_pct": peer, "peers": used, **c})
    return out


def summarize(scanned: List[Dict[str, Any]], attributed_move_ids: Optional[set] = None) -> Dict[str, Any]:
    by_kind: Dict[str, Dict[str, int]] = {}
    label_total: Dict[str, int] = {}
    for m in scanned:
        k = m.get("move_kind") or "?"
        b = by_kind.setdefault(k, {})
        b[m["label"]] = b.get(m["label"], 0) + 1
        label_total[m["label"]] = label_total.get(m["label"], 0) + 1
    res: Dict[str, Any] = {
        "n": len(scanned),
        "labels": label_total,
        "peer_data": sum(1 for m in scanned if m.get("peer_pct") is not None),
        "same_sign": sum(1 for m in scanned if m.get("same_sign")),
        "same_sign_share_of_known": round(
            100.0 * sum(1 for m in scanned if m.get("same_sign"))
            / max(1, sum(1 for m in scanned if m.get("peer_pct") is not None)), 1),
        "beta_share": round(100.0 * (label_total.get("sector_beta", 0)
                                    + label_total.get("beta_tilted", 0)) / len(scanned), 1)
        if scanned else None,
        "by_kind": by_kind,
        "by_kind_same_sign_pct": {
            k: round(100.0 * sum(1 for m in scanned
                                 if m.get("move_kind") == k and m.get("same_sign"))
                     / max(1, sum(1 for m in scanned
                                   if m.get("move_kind") == k and m.get("peer_pct") is not None)), 1)
            for k in {m.get("move_kind") for m in scanned}
        },
    }
    if attributed_move_ids is not None:
        att = [m for m in scanned if m["id"] in attributed_move_ids]
        beta_att = [m for m in att if m["label"] == "sector_beta"]
        res["attribution_rows"] = len(att)
        res["attribution_beta_contaminated"] = len(beta_att)
        res["attribution_beta_share"] = round(100.0 * len(beta_att) / len(att), 1) if att else None
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = _ro()
    try:
        scanned = scan_moves(conn, args.limit)
        att_ids = {r["move_id"] for r in conn.execute(
            "SELECT DISTINCT move_id FROM event_move_attribution")}
        rep = summarize(scanned, att_ids)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"异动 {rep['n']} 条 · beta 相关占比 {rep['beta_share']}%")
    for k, v in rep["labels"].items():
        print(f"  {k:<16} {v}")
    print("按形态：")
    for k, v in rep["by_kind"].items():
        print(f"  {k:<10} {v}")
    print(f"归因主表 {rep['attribution_rows']} 条中，板块 beta 解释的 {rep['attribution_beta_contaminated']} 条"
          f"（{rep['attribution_beta_share']}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
