#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bars_sina_global.py — 用新浪外盘期货日 K 补 WTI 日线缺口 (v337)

为什么
------
`decision_truth` 结算"决策后真实涨跌"要在 kline_bars 里找 t0 / t1 两个时刻的价。
WTI 的 1d 只有 148 根（2026-05-18 起、中间大量缺日），4h 261 根、1m 882 根 ——
结果 22 条 WTI 方向决策永远算不出真值，S8 卡在 88.2%（目标 90%）。

Yahoo v8（CL=F）在当前网络下返回空、stooq 被 JS 挑战挡住，
新浪 `GlobalFuturesService.getGlobalFuturesDailyKLine` 可用且给全历史。

写入 kline_bars（symbol=WTI, timeframe=1d, ts=当日收盘 epoch ms），
UNIQUE(symbol,timeframe,ts) 保证幂等重跑。

用法:
    python3 bars_sina_global.py --dry-run
    python3 bars_sina_global.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

URL = ("https://stock2.finance.sina.com.cn/futures/api/json.php/"
       "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}")

# 新浪代码 → 本系统符号
_SYMBOLS = {"CL": "WTI", "GC": "XAU", "SI": "XAG", "HG": "COPPER"}


def _fetch(sym: str) -> list:
    out = subprocess.run(
        ["curl", "-s", "-m", "25", "-H", "Referer: https://finance.sina.com.cn",
         URL.format(sym=sym)],
        capture_output=True, text=True,
    )
    try:
        return json.loads(out.stdout or "[]") or []
    except json.JSONDecodeError:
        return []


def run(apply: bool = False, symbols=("CL",)) -> dict:
    conn = db.get_connection()
    stats = {"symbols": {}, "rows": 0, "inserted": 0}
    try:
        cur = conn.cursor()
        for sym in symbols:
            bars = _fetch(sym)
            target = _SYMBOLS.get(sym, sym)
            ins = 0
            for b in bars:
                try:
                    d = datetime.strptime(b["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    ts_ms = int(d.timestamp() * 1000)
                    close = float(b["close"])
                except (KeyError, ValueError, TypeError):
                    continue
                if close <= 0:
                    continue
                stats["rows"] += 1
                if apply:
                    cur.execute(
                        """INSERT OR IGNORE INTO kline_bars
                             (symbol, timeframe, ts, open, high, low, close, volume)
                           VALUES (?, '1d', ?, ?, ?, ?, ?, ?)""",
                        (target, ts_ms, float(b.get("open") or close),
                         float(b.get("high") or close), float(b.get("low") or close),
                         close, float(b.get("volume") or 0)),
                    )
                    ins += cur.rowcount
            stats["symbols"][target] = {"fetched": len(bars), "inserted": ins}
            stats["inserted"] += ins
            if apply:
                conn.commit()
    finally:
        conn.close()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--symbols", default="CL")
    a = ap.parse_args()
    st = run(apply=a.apply, symbols=tuple(s.strip() for s in a.symbols.split(",") if s.strip()))
    print(f"[sina-global] fetched={st['rows']} inserted={st['inserted']} apply={a.apply} {st['symbols']}")
