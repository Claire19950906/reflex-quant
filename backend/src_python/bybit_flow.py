#!/usr/bin/env python3
"""Bybit 资金流/杠杆/多空比采集器（v323）。

为什么加这个：
    原本 external_indicators 靠 Yahoo / FRED 取数，本机到这两个源全部不通
    （SSL/429/Connection reset），数据停更十余小时 → 归因页的"资金流向、杠杆、
    市场情绪"整段断粮，"谁在买、杠杆是否过度、是否有套利离场"无法判断。

为什么选 Bybit：
    实测本机可达（api.bybit.com 返回 200；OKX/Yahoo/FRED/Binance 全部 000）。
    它直接提供三类关键量：
      · fundingRate        资金费率 → 杠杆多空拥挤度（正=多头付费，过热易反转）
      · openInterest       持仓量   → 杠杆总水位（暴增=加杠杆，骤降=去杠杆/离场）
      · account-ratio      多空账户比 → 散户心理预期（极端偏多常是反向信号）

写入口径与既有表一致（external_indicators），source 标记 bybit，便于和
yfinance 历史数据区分，不覆盖、不伪造。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "https://api.bybit.com"
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
TIMEOUT = 8


def _get(path: str, params: str = "", retries: int = 3) -> dict:
    """带重试：实测本机到 bybit 会偶发 SSL UNEXPECTED_EOF（链路抖动），
    单次失败不代表源不可用，重试 3 次能把 2/3 的偶发失败吃掉。"""
    url = f"{BASE}{path}" + (f"?{params}" if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": "trident/1.0"})
    last: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last if last else RuntimeError("unknown")


def collect() -> int:
    from external_data import ingest_external

    now = int(time.time())
    written = 0

    for asset, sym in SYMBOLS.items():
        # 1) 资金费率 + 持仓量
        try:
            d = _get("/v5/market/tickers", f"category=linear&symbol={sym}")
            item = (d.get("result") or {}).get("list") or []
            if item:
                it = item[0]
                fr = it.get("fundingRate")
                oi = it.get("openInterest")
                if fr not in (None, ""):
                    written += ingest_external(
                        asset, "bybit_funding_rate",
                        [{"ts": now, "value": float(fr) * 100.0}],
                        source="bybit", regime_tags=["leverage"],
                        meta={"symbol": sym, "unit": "pct_per_8h"},
                    )["written"]
                if oi not in (None, ""):
                    written += ingest_external(
                        asset, "bybit_open_interest",
                        [{"ts": now, "value": float(oi)}],
                        source="bybit", regime_tags=["leverage"],
                        meta={"symbol": sym, "unit": "base_coin"},
                    )["written"]
        except Exception as e:
            print(f"[bybit] {sym} ticker 失败: {type(e).__name__}: {e}", flush=True)

        # 2) 多空账户比（心理预期）；1h 粒度
        try:
            d = _get(
                "/v5/market/account-ratio",
                f"category=linear&symbol={sym}&period=1h&limit=1",
            )
            item = (d.get("result") or {}).get("list") or []
            if item:
                ratio = item[0].get("buyRatio")
                if ratio not in (None, ""):
                    written += ingest_external(
                        asset, "bybit_long_short_ratio",
                        [{"ts": now, "value": float(ratio) * 100.0}],
                        source="bybit", regime_tags=["sentiment"],
                        meta={"symbol": sym, "unit": "pct_long_accounts"},
                    )["written"]
        except Exception as e:
            print(f"[bybit] {sym} account-ratio 失败: {type(e).__name__}: {e}", flush=True)

    return written


if __name__ == "__main__":
    try:
        n = collect()
        print(f"[bybit] 写入 {n} 条外部指标 @ {time.strftime('%H:%M:%S')}", flush=True)
    except Exception as exc:
        print(f"[bybit] 采集失败: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
