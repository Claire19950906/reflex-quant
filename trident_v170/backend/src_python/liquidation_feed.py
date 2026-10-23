"""
liquidation_feed.py — v303: 币安合约强平(爆单)热度采集
====================================================
为什么需要 API key
------------------
实测(2026-10) 结论:
  * `GET /fapi/v1/allForceOrders` (公开强平历史) → **404 已下线**
  * `GET /fapi/v1/forceOrders`  → **401 需签名**
  * WebSocket `btcusdt@forceOrder` → 本环境连接被阻断
  * `GET /fapi/v1/openInterest` / `klines` → 200 免密钥可用
因此强平热度必须走**签名接口**, 需要 API key。

严格约束(用户要求)
------------------
  * **绝不伪造**: 没有 key / 接口失败 → 写 0 行, 明确 skipped
  * **不跨品类套用**: 只写入 symbol 真实存在的币对
  * **不写陈旧数据**: 每行记录 fetched_at, 读取端按新鲜度过滤
  * **可审计**: raw_json 保留原始响应

用法
----
    export BINANCE_API_KEY=xxx
    export BINANCE_API_SECRET=yyy
    python3 liquidation_feed.py --probe
    python3 liquidation_feed.py --symbols BTCUSDT ETHUSDT --hours 24
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_tech_signals import migrate  # noqa: E402

DB_PATH = BACKEND / "trident_event_bus.db"
BASE = "https://fapi.binance.com"
TIMEOUT = 15

DDL = """
CREATE TABLE IF NOT EXISTS liquidation_heat (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    ts            INTEGER NOT NULL,      -- 聚合桶起始时间(epoch秒)
    long_liq_usd  REAL,
    short_liq_usd REAL,
    total_usd     REAL,
    long_qty      REAL,
    short_qty     REAL,
    avg_price     REAL,
    raw_json      TEXT,
    fetched_at    INTEGER NOT NULL,
    UNIQUE(symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_liq_symbol_ts
    ON liquidation_heat(symbol, ts DESC);
"""


def _api_key() -> tuple[str | None, str | None]:
    return (os.environ.get("BINANCE_API_KEY"),
            os.environ.get("BINANCE_API_SECRET"))


def _signed_get(path: str, params: dict):
    """HMAC-SHA256 签名请求。失败抛异常, 绝不返回假数据。"""
    key, secret = _api_key()
    if not key or not secret:
        raise RuntimeError("缺少 BINANCE_API_KEY / BINANCE_API_SECRET")
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{BASE}{path}?{qs}&signature={sig}",
        headers={"X-MBX-APIKEY": key, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def probe() -> dict:
    """不落库的连通性诊断。"""
    key, _ = _api_key()
    out: dict = {"has_key": bool(key)}
    try:
        with urllib.request.urlopen(
                urllib.request.Request(f"{BASE}/fapi/v1/ping"),
                timeout=TIMEOUT) as r:
            out["ping"] = r.status
    except Exception as e:
        out["ping"] = f"{type(e).__name__}: {e}"
    if key:
        try:
            d = _signed_get("/fapi/v1/forceOrders",
                            {"symbol": "BTCUSDT", "limit": 5})
            out["forceOrders"] = ("ok, n=%d" % len(d)) if isinstance(d, list) else d
            if isinstance(d, list) and d:
                oo = d[0].get("o", d[0])
                out["sample_keys"] = sorted(oo.keys())
        except Exception as e:
            out["forceOrders"] = f"{type(e).__name__}: {e}"
    else:
        out["forceOrders"] = "skipped: 无 API key"
    return out
def fetch_symbol(conn: sqlite3.Connection, symbol: str, hours: int = 24) -> int:
    """抓取单 symbol 强平并按小时聚合成"热力值"。失败返回 0, 绝不写推测值。"""
    now_ms = int(time.time() * 1000)
    raw = _signed_get("/fapi/v1/forceOrders",
                      {"symbol": symbol, "limit": 1000,
                       "startTime": now_ms - hours * 3600 * 1000,
                       "endTime": now_ms})
    if not isinstance(raw, list) or not raw:
        return 0

    buckets: dict[int, dict] = {}
    for o in raw:
        oo = o.get("o", o)
        ts = int(oo.get("T", 0) or 0) // 1000
        if not ts:
            continue
        try:
            qty = float(oo.get("q", 0) or 0)
            price = float(oo.get("ap", 0) or 0)
        except (TypeError, ValueError):
            continue
        usd = qty * price
        if usd <= 0:
            continue
        b = buckets.setdefault(ts // 3600 * 3600,
                               {"long_usd": 0.0, "short_usd": 0.0,
                                "long_qty": 0.0, "short_qty": 0.0,
                                "px_sum": 0.0, "px_n": 0, "raw": []})
        # Binance 强平单 side=SELL 通常为多头被强平(强制卖出)
        if oo.get("S") == "SELL":
            b["long_usd"] += usd
            b["long_qty"] += qty
        else:
            b["short_usd"] += usd
            b["short_qty"] += qty
        b["px_sum"] += price
        b["px_n"] += 1
        b["raw"].append(oo)

    fetched_at = int(time.time())
    rows = [(
        symbol, ts,
        round(b["long_usd"], 2), round(b["short_usd"], 2),
        round(b["long_usd"] + b["short_usd"], 2),
        b["long_qty"], b["short_qty"],
        round(b["px_sum"] / b["px_n"], 4) if b["px_n"] else None,
        json.dumps(b["raw"][:200], ensure_ascii=False),
        fetched_at,
    ) for ts, b in buckets.items()]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO liquidation_heat "
        "(symbol,ts,long_liq_usd,short_liq_usd,total_usd,long_qty,short_qty,"
        " avg_price,raw_json,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,ts) DO UPDATE SET "
        "long_liq_usd=excluded.long_liq_usd, "
        "short_liq_usd=excluded.short_liq_usd, "
        "total_usd=excluded.total_usd, long_qty=excluded.long_qty, "
        "short_qty=excluded.short_qty, avg_price=excluded.avg_price, "
        "raw_json=excluded.raw_json, fetched_at=excluded.fetched_at",
        rows,
    )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只诊断连通性")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT")
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args()

    if a.probe:
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
        return 0

    if not _api_key()[0]:
        print("[SKIP] 未配置 BINANCE_API_KEY / BINANCE_API_SECRET")
        print("       强平热度需签名接口; 无 key 时不写入任何数据(不伪造)")
        print("       设置: export BINANCE_API_KEY=... / BINANCE_API_SECRET=...")
        return 2

    conn = sqlite3.connect(DB_PATH)
    try:
        migrate(conn)
        conn.executescript(DDL)
        conn.commit()
        total = 0
        for sym in [s.strip().upper() for s in a.symbols.split(",") if s.strip()]:
            try:
                n = fetch_symbol(conn, sym, a.hours)
                total += n
                print(f"  {sym:<12} 写入 {n} 个小时桶"
                      if n else f"  {sym:<12} 无强平数据 → 不写入")
            except Exception as e:
                print(f"  {sym:<12} 失败 {type(e).__name__}: {e} → 跳过, 不伪造")
        conn.commit()
        print(f"[OK] 共写入 {total} 行")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())