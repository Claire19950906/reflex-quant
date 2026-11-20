#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed initial data into dim_asset and dim_event_type (PR-1).

Idempotent: re-running is safe (INSERT OR IGNORE).
Event type codes use English slugs to be SQL-friendly;
name_zh keeps the original Chinese from event_struct._EVENT_RULES.
Direction bias follows event_struct._DIRECTION_BIAS but converted
to bullish/bearish/neutral (English) for downstream SQL aggregation.
"""
from __future__ import annotations

import sys
from typing import Dict, Tuple

# Allow running as `python seed_event_dim.py` from backend dir
import os
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db
import dim_tables


# ────────────────────────────────────────────────────────────────────
# Initial assets
# ────────────────────────────────────────────────────────────────────

INITIAL_ASSETS: list[Tuple[str, str, str, str]] = [
    # (code, name, kind, venue)
    ("BTC", "Bitcoin", "crypto", "Binance"),
    ("ETH", "Ethereum", "crypto", "Binance"),
    ("SOL", "Solana", "crypto", "Binance"),
    ("XAU", "Gold", "commodity", "COMEX"),
    ("WTI", "WTI Crude", "commodity", "NYMEX"),
    ("SPX", "S&P 500", "equity_index", "CME"),
    ("NDX", "Nasdaq 100", "equity_index", "CME"),
    ("DXY", "US Dollar Index", "fx", "ICE"),
    ("USDCNH", "USD/CNH", "fx", ""),
    ("000300.SS", "CSI 300", "equity_index", "SSE"),
]


def seed_assets(connection=None) -> int:
    """Idempotent seed for dim_asset. Returns inserted count (excludes existing)."""
    before = connection.execute("SELECT COUNT(*) FROM dim_asset").fetchone()[0] if connection else 0
    for code, name, kind, venue in INITIAL_ASSETS:
        dim_tables.get_or_create_asset(
            code, name=name, kind=kind, venue=venue, connection=connection
        )
    after = connection.execute("SELECT COUNT(*) FROM dim_asset").fetchone()[0] if connection else 0
    return after - before


# ────────────────────────────────────────────────────────────────────
# Initial event types
# Codes mirror event_struct._EVENT_RULES (Chinese names); direction_bias
# mirrors event_struct._DIRECTION_BIAS but in bullish/bearish.
# ────────────────────────────────────────────────────────────────────

# code -> (name_zh, name_en, category, direction_bias)
INITIAL_EVENT_TYPES: Dict[str, Tuple[str, str, str, Dict[str, str]]] = {
    "rate_hike": ("加息", "Rate Hike", "macro", {
        "XAU": "bearish", "BTC": "bearish", "ETH": "bearish", "WTI": "bearish",
    }),
    "rate_cut": ("降息", "Rate Cut", "macro", {
        "XAU": "bullish", "BTC": "bullish", "ETH": "bullish", "WTI": "bullish",
    }),
    "cpi": ("CPI", "CPI", "macro", {
        "XAU": "bearish", "BTC": "bearish",
    }),
    "nfp": ("非农", "Non-Farm Payrolls", "macro", {
        "XAU": "bearish", "BTC": "bearish",
    }),
    "opec_cut": ("OPEC减产", "OPEC Production Cut", "commodity", {
        "WTI": "bullish",
    }),
    "opec_increase": ("OPEC增产", "OPEC Production Increase", "commodity", {
        "WTI": "bearish",
    }),
    "oil_inventory": ("原油库存", "Crude Oil Inventory", "commodity", {
        # v198: 库存数据有增/减双方向（EIA 库存大减→WTI bullish / 大增→bearish）。
        # event_struct 字典规则不区分方向，因此 direction_bias 走 neutral 兜底。
        # 下游 ai_worker 可基于库存变化的实际量级和方向独立判断。
        "WTI": "neutral",
    }),
    "geopolitical_conflict": ("地缘冲突", "Geopolitical Conflict", "geopolitics", {
        "XAU": "bullish", "WTI": "bullish",
    }),
    "geopolitical_easing": ("地缘缓和", "Geopolitical Easing", "geopolitics", {
        "XAU": "bearish", "WTI": "bearish",
    }),
    "central_bank_gold": ("央行购金", "Central Bank Gold Buying", "macro", {
        "XAU": "bullish",
    }),
    "crypto_etf": ("加密ETF", "Crypto Spot ETF Approval", "crypto", {
        "BTC": "bullish", "ETH": "bullish",
    }),
    "dxy": ("美元指数", "US Dollar Index Move", "macro", {
        "XAU": "bearish", "BTC": "bearish",
    }),
    "tips": ("TIPS", "Real Rates / TIPS", "macro", {
        "XAU": "bearish", "BTC": "bearish",
    }),
    # ── v181 P2: 新增 12 类事件类型 ──
    "technology_breakthrough": ("技术突破", "Technology Breakthrough", "tech", {
        "000300.SS": "bullish", "NDX": "bullish",
    }),
    "chip_capacity": ("芯片量产", "Chip Capacity / Memory", "tech", {
        "000300.SS": "bullish", "NDX": "bullish",
    }),
    "earnings_release": ("财报", "Earnings Release", "equity", {
        "000300.SS": "neutral", "NDX": "neutral",
    }),
    "merger_acquisition": ("并购重组", "M&A", "equity", {
        "000300.SS": "neutral", "NDX": "neutral",
    }),
    "hack_attack": ("安全事件", "Hack / Security Incident", "crypto", {
        "BTC": "bearish", "ETH": "bearish",
    }),
    "regulation_policy": ("监管政策", "Regulation / Policy", "macro", {
        "BTC": "neutral", "ETH": "neutral",
    }),
    "ipo_listing": ("IPO上市", "IPO / Listing", "equity", {
        "000300.SS": "neutral", "NDX": "neutral",
    }),
    "macro_speech": ("央行表态", "Central Bank Speech", "macro", {
        "XAU": "neutral", "BTC": "neutral",
    }),
    "natural_disaster": ("自然灾害", "Natural Disaster", "macro", {
        "WTI": "bearish", "000300.SS": "bearish",
    }),
    "pandemic_outbreak": ("疫情", "Pandemic / Outbreak", "macro", {
        "WTI": "bearish", "000300.SS": "bearish",
    }),
    "supply_chain": ("供应链中断", "Supply Chain Disruption", "macro", {
        "WTI": "bearish", "000300.SS": "bearish",
    }),
    "trade_tariff": ("关税与制裁", "Tariff / Sanction", "macro", {
        "XAU": "bullish", "000300.SS": "bearish",
    }),
}


def seed_event_types(connection=None) -> int:
    """Idempotent seed for dim_event_type. Returns inserted count."""
    before = connection.execute("SELECT COUNT(*) FROM dim_event_type").fetchone()[0] if connection else 0
    for code, (name_zh, name_en, category, bias) in INITIAL_EVENT_TYPES.items():
        dim_tables.get_or_create_event_type(
            code, name_zh=name_zh, name_en=name_en,
            category=category, direction_bias=bias, connection=connection,
        )
    after = connection.execute("SELECT COUNT(*) FROM dim_event_type").fetchone()[0] if connection else 0
    return after - before


def seed_all(connection=None) -> Dict[str, int]:
    """Seed both tables in one transaction. Returns inserted counts."""
    conn = connection or db.get_connection()
    try:
        a = seed_assets(conn)
        e = seed_event_types(conn)
        conn.commit()
        return {"assets": a, "event_types": e}
    finally:
        if connection is None:
            conn.close()


if __name__ == "__main__":
    result = seed_all()
    print(f"[seed_event_dim] inserted: assets={result['assets']}, event_types={result['event_types']}")