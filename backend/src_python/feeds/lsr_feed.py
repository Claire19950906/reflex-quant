#!/usr/bin/env python3
"""lsr_feed.py — 真实多空持仓比（Long/Short Ratio）采集。

背景：`coinglass_lsr` 那条源需要付费 key，试用 key 从未成功过，
coinglass_lsr_samples 一直是 0 行 —— 面板等于长期缺失。
这里换成本机已验证可达的免费源：

  · Binance 合约：`/futures/data/globalLongShortAccountRatio`（全市场账户多空比）
  · Bybit 合约：`/v5/market/account-ratio`（账户买卖比，作备份）

两者都是真实交易所数据，不做任何推算或补值。取不到就不写，前端按缺失显示。

写库幂等：UNIQUE(source, symbol, period, ts)。

用法：
  python3 src_python/lsr_feed.py            # BTC/ETH/SOL 全市场多头空头比
  python3 src_python/lsr_feed.py --show     # 只打印最新，不写库
"""
from __future__ import annotations
from ._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = str(BACKEND_DIR)
DB_PATH = os.path.join(BACKEND_DIR, "trident_event_bus.db")

SYMBOLS = ("BTC", "ETH", "SOL")
PERIOD = "1h"

BINANCE_URL = ("https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
               "?symbol={pair}&period={period}&limit={limit}")
BYBIT_URL = ("https://api.bybit.com/v5/market/account-ratio"
             "?category=linear&symbol={pair}&period={period}&limit={limit}")

_CREATE = """
CREATE TABLE IF NOT EXISTS lsr_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,   -- binance | bybit
    symbol        TEXT    NOT NULL,   -- BTC / ETH / SOL
    period        TEXT    NOT NULL,   -- 1h
    ts            INTEGER NOT NULL,  -- 交易所该点位的口径时间 (epoch s)
    long_ratio    REAL,               -- 多头占比 0~1
    short_ratio   REAL,               -- 空头占比 0~1
    ls_ratio      REAL,               -- 多空比 = 多/空
    raw_json      TEXT,
    created_at    INTEGER NOT NULL,
    UNIQUE(source, symbol, period, ts)
);
"""


def _curl_json(url: str, timeout: int = 12) -> Optional[Any]:
    """用系统 curl 取 JSON —— 本机 Python(OpenSSL) 打交易所常被 TLS 中止，curl 稳定。"""
    try:
        out = subprocess.run(
            ["/usr/bin/curl", "-s", "-m", str(timeout), "--noproxy", "*",
             "-A", "Mozilla/5.0", url],
            capture_output=True, text=True, timeout=timeout + 3,
        ).stdout.strip()
        if not out:
            return None
        return json.loads(out)
    except Exception:
        return None


def fetch_binance(pair: str, limit: int = 24) -> List[Dict[str, Any]]:
    """返回 [{ts, long_ratio, short_ratio, ls_ratio, raw}]"""
    for _ in range(3):
        data = _curl_json(BINANCE_URL.format(pair=pair, period=PERIOD, limit=limit))
        if isinstance(data, list) and data:
            out = []
            for r in data:
                try:
                    lr = float(r["longAccount"])
                    sr = float(r["shortAccount"])
                    ts = int(r["timestamp"]) // 1000
                except Exception:
                    continue
                out.append({"ts": ts, "long_ratio": lr, "short_ratio": sr,
                            "ls_ratio": float(r.get("longShortRatio") or (lr / sr if sr else 0)),
                            "raw": json.dumps(r, ensure_ascii=False)})
            return out
        time.sleep(1.5)
    return []


def fetch_bybit(pair: str, limit: int = 24) -> List[Dict[str, Any]]:
    data = _curl_json(BYBIT_URL.format(pair=pair, period=PERIOD, limit=limit))
    if not data or data.get("retCode") != 0:
        return []
    out = []
    for r in (data.get("result") or {}).get("list") or []:
        try:
            br = float(r["buyRatio"])
            sr = float(r["sellRatio"])
            ts = int(int(r["timestamp"]) // 1000)
        except Exception:
            continue
        out.append({"ts": ts, "long_ratio": br, "short_ratio": sr,
                    "ls_ratio": br / sr if sr else 0,
                    "raw": json.dumps(r, ensure_ascii=False)})
    return out


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lsr_symbol_ts ON lsr_samples(symbol, ts)")


def run(show_only: bool = False) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_table(conn)
    conn.commit()
    total = 0
    try:
        for sym in SYMBOLS:
            pair = f"{sym}USDT"
            for src, fn in (("binance", fetch_binance), ("bybit", fetch_bybit)):
                rows = fn(pair)
                if not rows:
                    print(f"  {sym:4} {src:8} 无数据（不写）")
                    continue
                if show_only:
                    last = rows[-1]
                    print(f"  {sym:4} {src:8} 多头 {last['long_ratio']:.2%} "
                          f"空头 {last['short_ratio']:.2%} 比 {last['ls_ratio']:.2f}")
                    continue
                now = int(time.time())
                cur = conn.executemany(
                    "INSERT OR IGNORE INTO lsr_samples "
                    "(source,symbol,period,ts,long_ratio,short_ratio,ls_ratio,raw_json,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    [(src, sym, PERIOD, r["ts"], r["long_ratio"], r["short_ratio"],
                     r["ls_ratio"], r["raw"], now) for r in rows])
                total += cur.rowcount or 0
            conn.commit()
        if not show_only:
            print(f"[lsr_feed] 新增 {total} 行")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(show_only="--show" in sys.argv))
