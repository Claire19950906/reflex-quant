#!/usr/bin/env python3
"""永续合约资金费率 (perpetual futures funding rate) — OKX 公共 API。

OKX 公共 API 无需 API key, 即可拉取永续合约当前资金费率。
  - API: https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP
  - 缓存 TTL 60s

资金费率含义:
  - 正费率 (+0.01%/8h): 多头付空头, 市场偏多
  - 负费率 (-0.01%/8h): 空头付多头, 市场偏空
  - 极端费率 (±0.1%+): 套利机会 / 顶部信号

注: 仅拉 USDT 永续 (最常用)。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import http_retry

_CACHE: Dict[str, Any] = {}
_TTL = 300.0
_TTL_PARTIAL = 120.0   # v359: 有品种没取到 rate 时只用 30s 缓存, 否则一次抖动会钉死 5 分钟
_SESSION = None


def _get_session():
    """延迟初始化共享 session（http_retry 唯一暴露的工厂）。"""
    global _SESSION
    if _SESSION is None:
        import http_retry as _hr
        _SESSION = _hr.new_retry_session()
    return _SESSION

# 默认拉取的永续品种
_DEFAULT_INSTRUMENTS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "XAU-USDT-SWAP",  # OKX 现在有 XAU 永续
]

_OKX_URL = "https://www.okx.com/api/v5/public/funding-rate"



_BINANCE_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
_BIN_MAP = {"BTCUSDT": "BTC-USDT-SWAP", "ETHUSDT": "ETH-USDT-SWAP",
            "SOLUSDT": "SOL-USDT-SWAP", "PAXGUSDT": "XAU-USDT-SWAP"}


_BYBIT_URL = "https://api.bybit.com/v5/market/tickers"
_BYBIT_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XAU": "PAXGUSDT"}


def _fill_from_bybit(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bybit 优先（逐品种请求：全量 ticker 一次拉太大，实测 10s 超时）。"""
    need = [it for it in items if it.get("funding_rate") is None]
    if not need:
        return items
    try:
        import requests as _rq
        sess = _rq.Session()
        sess.trust_env = False           # launchd 进程里的 env 代理端口全是死的
    except Exception:
        return items
    for it in need:
        base = str(it.get("instrument", "")).split("-")[0]
        sym = _BYBIT_MAP.get(base)
        if not sym:
            continue
        for attempt in range(2):
            try:
                r = sess.get(_BYBIT_URL, params={"category": "linear", "symbol": sym},
                             timeout=5.0, headers={"User-Agent": "Mozilla/5.0"})
                row = ((r.json().get("result") or {}).get("list") or [{}])[0]
                rate = row.get("fundingRate")
                if rate is not None:
                    it["funding_rate"] = float(rate)
                    it["next_funding_time"] = row.get("nextFundingTime")
                    it["source"] = "bybit"
                    it.pop("error", None)
                    it["ts"] = int(time.time())
                    break
            except Exception:
                continue
        time.sleep(0.15)                 # 连发会被限流
    return items


def _fetch_binance_batch() -> Dict[str, Dict[str, Any]]:
    """OKX 域名在这台机器上 DNS 解析不了时的降级源（实测 Binance 可达）。"""
    try:
        # 注意：launchd 拉起的进程会从 ~/.trident/env 读到代理设置，而那几个本地代理
        # 端口现在都是死的（000）。Binance 直连可达，所以这里必须绕开 env 代理。
        import requests as _rq
        sess = _rq.Session()
        sess.trust_env = False
        r = sess.get(_BINANCE_URL, timeout=8.0, headers={"User-Agent": "Mozilla/5.0"})
        rows = r.json() or []
        return {row["symbol"]: row for row in rows if isinstance(row, dict)}
    except Exception:
        return {}


def _fill_from_binance(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    need = [it for it in items if it.get("funding_rate") is None]
    if not need:
        return items
    rows = _fetch_binance_batch()
    if not rows:
        return items
    for it in need:
        inst = it.get("instrument", "")
        base = inst.split("-")[0]            # BTC-USDT-SWAP -> BTC
        row = rows.get(base + "USDT")
        if not row:
            continue
        try:
            it["funding_rate"] = float(row.get("lastFundingRate"))
            it["next_funding_time"] = row.get("nextFundingTime")
            it["source"] = "binance"
            it.pop("error", None)
            it["ts"] = int(time.time())
        except Exception:
            continue
    return items


def _fetch_one(inst_id: str) -> Optional[Dict[str, Any]]:
    """拉单个永续合约当前费率。"""
    try:
        sess = _get_session()
        resp = sess.get(_OKX_URL, params={"instId": inst_id}, timeout=2.0)
        data = resp.json() if hasattr(resp, "json") else {}
        if data.get("code") == "0" and data.get("data"):
            d = data["data"][0]
            return {
                "instrument": inst_id,
                "funding_rate": float(d.get("fundingRate", 0)),
                "next_funding_time": d.get("nextFundingTime"),
                "interest_rate": float(d.get("interestRate", 0)),
                "max_funding_rate": float(d.get("maxFundingRate", 0)),
                "min_funding_rate": float(d.get("minFundingRate", 0)),
                "settle_funding_rate": float(d.get("settleFundingRate", 0)),
                "ts": int(time.time()),
            }
        return {"instrument": inst_id, "error": data.get("msg", "no data"),
                "ts": int(time.time())}
    except Exception as exc:
        return {"instrument": inst_id,
                "error": f"{type(exc).__name__}: {exc}",
                "ts": int(time.time())}


def fetch_funding_rates(
    instruments: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """批量拉永续合约资金费率。

    返回 {ok, ts, items: [...]}，每个 item 含 funding_rate / next_funding_time。
    """
    insts = instruments or _DEFAULT_INSTRUMENTS
    now = time.time()
    cached = _CACHE.get("batch")
    ttl = _TTL_PARTIAL if (cached or {}).get("partial") else _TTL
    if cached and now - cached["ts"] < ttl and cached.get("insts") == insts:
        return cached["data"]
    # 先问 Binance：一次请求拿全表，实测 1.5s 且稳定可达。
    # OKX 在这台机器上域名解析直接失败，只在 Binance 拿不到时才回去试。
    now_ts = int(time.time())
    items: List[Dict[str, Any]] = [{"instrument": i, "ts": now_ts} for i in insts]
    items = _fill_from_bybit(items)
    if not any(it.get("funding_rate") is not None for it in items):
        items = _fill_from_binance(items)
    if not any(it.get("funding_rate") is not None for it in items):
        items = [_fetch_one(i) for i in insts]

    if not any(it.get("funding_rate") is not None for it in items):
        # 三个源同时拿不到时，保留上一次成功的结果（费率 8 小时才变一次），
        # 而不是把页面打成空 —— 但绝不编造新数值。
        prev = _CACHE.get("batch")
        if prev and any(i.get("funding_rate") is not None for i in prev["data"].get("items", [])):
            return prev["data"]
    out = {"ok": True, "ts": int(now), "items": items}
    # 极端费率告警
    extreme = [it for it in items
               if it.get("funding_rate") is not None
               and abs(it["funding_rate"]) >= 0.0005]  # ≥ 0.05%/8h
    out["extreme_count"] = len(extreme)
    out["extreme"] = [{"inst": e["instrument"],
                        "rate_pct": round(e["funding_rate"] * 100, 4)}
                       for e in extreme]
    # v360: 缺失品种先沿用上一轮真实值（标 stale），既有数又不会因为反复重抓把接口拖超时
    prev = (cached or {}).get("data") or {}
    prev_items = {p.get("instrument"): p for p in (prev.get("items") or [])}
    for it in items:
        if it.get("funding_rate") is None:
            pv = prev_items.get(it.get("instrument")) or {}
            if pv.get("funding_rate") is not None:
                it["funding_rate"] = pv["funding_rate"]
                it["next_funding_time"] = pv.get("next_funding_time")
                it["source"] = pv.get("source") or "bybit"
                it["stale"] = True
    # v359: 部分品种缺失 → 短缓存, 让下一轮尽快补齐（宁可多请求, 不要缺 5 分钟）
    missing = any(it.get("funding_rate") is None for it in items)
    _CACHE["batch"] = {"ts": now, "data": out, "insts": insts,
                       "partial": missing}
    return out


def fetch_btc_funding() -> Optional[Dict[str, Any]]:
    """单 BTC 永续 — 套利最常用。"""
    now = time.time()
    cached = _CACHE.get("btc")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    out = _fetch_one("BTC-USDT-SWAP")
    _CACHE["btc"] = {"ts": now, "data": out}
    return out


if __name__ == "__main__":
    import json
    print("BTC:", json.dumps(fetch_btc_funding(), ensure_ascii=False, indent=2))
    print()
    print("Batch:", json.dumps(fetch_funding_rates(), ensure_ascii=False, indent=2))