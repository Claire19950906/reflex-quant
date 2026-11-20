#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用行情源体检：每个品种当前从哪个源、拿到什么价、多久。

    python3 scripts/check_quote_sources.py
    python3 scripts/check_quote_sources.py --assets WTI,NVDA,TSLA --json

只读外网，不写库。用于回答"这个品种到底有没有可用数据源"，避免再出现
`okx_swap:NVDA` 这种 785 次全失败的假源。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BACKEND, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "XAU", "XAG", "WTI", "OIL",
                  "NVDA", "TSLA", "AAPL", "MSTR", "MSFT", "MU", "SKHYNIX"]


def probe_price(asset: str) -> dict:
    from feeds import domestic_quotes as dq
    t = time.monotonic()
    try:
        got = dq.fetch_all([asset])
        row = got.get(asset)
        if row:
            return {"ok": True, "price": row.get("price"), "source": row.get("source"),
                    "note": row.get("note", ""), "ms": int((time.monotonic() - t) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:80]}",
                "ms": int((time.monotonic() - t) * 1000)}
    return {"ok": False, "error": "no_price", "ms": int((time.monotonic() - t) * 1000)}


def probe_candles(asset: str, interval: str = "1d", limit: int = 5) -> dict:
    from feeds import quote_sources as qs
    t = time.monotonic()
    mkt = qs.market_of(asset)
    try:
        if mkt == "okx_swap" and qs.symbol_of(asset):
            rows = qs.fetch_okx_candles(qs.symbol_of(asset), qs.okx_bar(interval), limit)
            src = "okx_swap"
        elif mkt == "binance_fapi":
            from feeds import domestic_quotes as dq
            pair = dq._GATE_PAIRS.get(asset)
            rows = dq.gate_klines(pair, "1h", limit) if pair else None
            src = "gate_klines"
        elif mkt in ("yahoo", "domestic_spot"):
            rows = qs.fetch_daily_multisource(asset, interval=interval, limit=limit)
            src = "yahoo→tencent"
        else:
            rows, src = None, f"skip:{mkt}"
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:80]}",
                "ms": int((time.monotonic() - t) * 1000)}
    ok = bool(rows)
    return {"ok": ok, "source": src, "bars": len(rows or []),
            "last": (rows[-1] if ok else None),
            "ms": int((time.monotonic() - t) * 1000)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    assets = [x.strip().upper() for x in a.assets.split(",") if x.strip()]

    rows = []
    for asset in assets:
        p = probe_price(asset)
        c = probe_candles(asset, a.interval)
        rows.append({"asset": asset, "price": p, "candles": c})

    bad = [r for r in rows if not (r["price"]["ok"] or r["candles"]["ok"])]
    if a.json:
        print(json.dumps({"rows": rows, "no_source": [r["asset"] for r in bad]},
                         ensure_ascii=False, indent=2, default=str))
    else:
        w = max(len(r["asset"]) for r in rows) + 2
        print("\n行情源体检（price=报价 / candles=K 线）")
        print("-" * 96)
        for r in rows:
            p, c = r["price"], r["candles"]
            p_txt = (f"{p['price']} [{p['source']}] {p['ms']}ms" if p["ok"]
                     else f"FAIL {p.get('error', '')}")
            c_txt = (f"{c['bars']}根 [{c['source']}] {c['ms']}ms" if c["ok"]
                     else f"FAIL {c.get('error', '')}")
            print(f"{r['asset']:<{w}} {p_txt:<46} {c_txt}")
        print("-" * 96)
        print(f"共 {len(rows)} 个品种，完全无源 {len(bad)} 个"
              + (f"：{', '.join(r['asset'] for r in bad)}" if bad else ""))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
