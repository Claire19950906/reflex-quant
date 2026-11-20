#!/usr/bin/env python3
"""金十直连行情：黄金 / 白银 / WTI 报价 + 1 分钟 K 线。

为什么需要：
  · XAU / WTI 是这套系统的主战场，但它们的报价长期靠腾讯、雅虎或二道贩子转口。
    雅虎已断 36 天，腾讯只给快照。WTI 的 1 分钟 K 线此前停在 41 天前
    （1m 最后一条是 10-03），意味着缺 1m 数据的策略与延迟统计全部失明。
  · 金十 MCP 的 get_quote / get_kline 是国内直连的真实现货报价（XAUUSD/USOIL/XAGUSD），
    带 open/high/low/close/涨跌幅/成交量，一次拿全，不经过任何换算。

写入：
  external_indicators（source='jin10_quote'）：
    jq_price / jq_change_pct / jq_high / jq_low / jq_volume
  kline_bars（timeframe='1m'）：缺失的分钟补进去（只写比库内最大 ts 更新的）

限制：金十 MCP 有配额与 token 轮换（jin10_mcp 内部已处理），120 秒一轮，
      每轮 3 个品种各 1 次 quote + 1 次 kline（count=100）。
"""
from ._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(SRC_DIR))
import db
import external_data
from feeds import jin10_mcp

ASSETS = [("XAU", "XAUUSD"), ("XAG", "XAGUSD"), ("WTI", "USOIL")]


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _write_kline(conn, symbol: str, klines: List[Dict[str, Any]]) -> int:
    if not klines:
        return 0
    cur = conn.cursor()
    last = cur.execute(
        "SELECT max(ts) FROM kline_bars WHERE symbol=? AND timeframe='1m'", (symbol,)
    ).fetchone()[0] or 0
    rows = []
    for k in klines:
        ts = int(k.get("time") or 0) * 1000  # 金十给秒，表里是毫秒
        if ts <= int(last):
            continue
        o, h, l, c = (_num(k.get(x)) for x in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        rows.append((symbol, "1m", ts, o, h, l, c, _num(k.get("volume")),
                     time.strftime("%Y-%m-%d %H:%M:%S")))
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def main() -> int:
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    ts = int(time.time())
    written = 0
    klines_written = 0

    for asset, code in ASSETS:
        quote = jin10_mcp.get_quote(code)
        data = (quote or {}).get("data") or {}
        # 金十外层再包一层 data 的情况也兼容
        if isinstance(data.get("data"), dict):
            data = data["data"]
        if quote.get("status") != "ok" or not data:
            print(f"[WARN] {asset} 报价不可用: {quote.get('error')}", flush=True)
            continue

        price = _num(data.get("close"))
        if price is None:
            print(f"[WARN] {asset} 报价缺 close", flush=True)
            continue

        rows = [
            ("jq_price", price, {"unit": "quote", "code": code}),
            ("jq_change_pct", _num(data.get("ups_percent")), {"unit": "%", "code": code}),
            ("jq_high", _num(data.get("high")), {"unit": "quote", "code": code}),
            ("jq_low", _num(data.get("low")), {"unit": "quote", "code": code}),
            ("jq_volume", _num(data.get("volume")), {"unit": "lot", "code": code}),
        ]
        for indicator, value, meta in rows:
            if value is None:
                continue
            try:
                external_data.ingest_external(
                    asset=asset, indicator=indicator,
                    records=[{"ts": ts, "value": value}],
                    source="jin10_quote", meta=meta,
                )
                written += 1
            except Exception as exc:
                print(f"[WARN] 写入 {indicator}/{asset} 失败: {exc}", flush=True)

        k = jin10_mcp.get_kline(code, 100)
        kd = (k or {}).get("data") or {}
        added = _write_kline(conn, asset, kd.get("klines") or [])
        klines_written += added

        print(f"[{time.strftime('%H:%M:%S')}] {asset} {price} "
              f"({data.get('ups_percent')}%) 1m+{added}", flush=True)

    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 金十行情 写 {written} 条，K线补 {klines_written} 根",
          flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
