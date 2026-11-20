#!/usr/bin/env python3
"""导出前端/后端指标一致性基准（parity fixture）。

背景：技术指标有两套实现 —— 后端 `technical_indicators.py`（决策链在用）
与前端 `frontend/lib/technical-indicators.ts`（K 线图渲染用）。两套并行必然漂移，
历史上已经漂过（布林带样本/总体标准差、RSI/ATR 暖机位相差一根）。

这里的做法：拿**真实 BTC 4h K 线**在后端算一遍，把结果连同输入 bars 写成 JSON，
由前端 `technical-indicators.parity.test.ts` 逐点比对。漂移不再是靠人眼发现，
而是变成一条会红的测试。

用法：
  python3 scripts/export_indicator_parity_fixture.py --asset BTC --timeframe 4h --bars 240
"""
import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import technical_indicators as ti

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "..", "frontend", "lib", "__fixtures__", "indicator-parity.json")


def load_bars(db: str, asset: str, timeframe: str, bars: int) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM kline_bars "
        "WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
        (asset, timeframe, bars),
    ).fetchall()
    conn.close()
    out = []
    for ts, o, h, l, c, v in reversed(rows):
        out.append({"time": int(ts) // 1000, "open": o, "high": h, "low": l,
                    "close": c, "volume": v if v is not None else 0})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--bars", type=int, default=240)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    bars = load_bars(args.db, args.asset, args.timeframe, args.bars)
    if len(bars) < 60:
        print(f"[ERR] 只有 {len(bars)} 根，样本不足", flush=True)
        return 1

    closes = [b["close"] for b in bars]
    # 后端 candles 约定：[ts, open, high, low, close, vol]（technical_indicators 里 TS/OPEN/HIGH/LOW/CLOSE/VOL）
    candles = [[b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"]] for b in bars]

    bb = ti.bollinger(closes, 20, 2.0)
    payload = {
        "meta": {"asset": args.asset, "timeframe": args.timeframe,
                 "generated_by": "backend/scripts/export_indicator_parity_fixture.py"},
        "bars": bars,
        "expected": {
            "sma20": ti.sma(closes, 20),
            "ema20": ti.ema(closes, 20),
            "rsi14": ti.rsi(closes, 14),
            "atr14": ti.atr(candles, 14),
            "bb20": {"upper": bb["upper"], "mid": bb["mid"], "lower": bb["lower"]},
            "macd": ti.macd(closes, 12, 26, 9),
            "kdj": ti.kdj(closes, [b["high"] for b in bars], [b["low"] for b in bars], 9, 3, 3),
            "mfi14": ti.mfi(candles, 14),
            "obv": ti.obv(candles),
            "stoch_rsi14": ti.stoch_rsi(closes, 14, 3, 3),
            "vwap": ti.vwap(candles),
            "donchian20": ti.donchian(candles, 20),
            "supertrend10": ti.supertrend(candles, 10, 3.0),
            "cci20": ti.cci(candles, 20),
            "aroon25": ti.aroon(candles, 25),
            "adx14": ti.adx(candles, 14),
            "realized_vol20": ti.realized_vol(candles, 20),
            "ichimoku": ti.ichimoku(candles),
            "stoch14": ti.stoch(candles, 14, 3),
            "bb20_width_pct": [None if u is None or m in (None, 0) or l is None
                               else (u - l) / m * 100 for u, m, l in zip(bb["upper"], bb["mid"], bb["lower"])],
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"[OK] {len(bars)} 根 → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
