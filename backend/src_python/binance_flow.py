#!/usr/bin/env python3
"""Binance 永续合约资金流采集器。

为什么需要：
  · 现有杠杆数据来自 Bybit 与 OKX，两家体量都小于 Binance。
    Binance 是全球最大的永续合约交易所，它的资金费率、多空账户比、
    吃单买卖比，才是"全市场杠杆情绪"的主刻度。
  · 三家同看还能互相校验：如果 Binance 与 Bybit 方向背离，说明是局部
    而非全市场，决策 prompt 里就该标成"分歧"而不是直接采信。

写入 external_indicators，source='binance'：
  bn_funding_rate       当期资金费率（小数，负=空头付钱给多头）
  bn_long_short_ratio   多空账户比（>1 偏多）
  bn_open_interest      未平仓合约张数
  bn_open_interest_usd  未平仓名义价值（美元）
  bn_taker_ratio        吃单买卖比（>1 主动买占优）

限制：公共端点，权重限制宽松；5 分钟一轮、3 个币共 12 次请求，安全。
"""
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

# 本机存在 DOUBAO_* 转发代理变量，requests 默认 trust_env=True 会误命中代理并断连；
# 实测关闭后同一请求稳定返回 200。
requests.Session.trust_env = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

BASE = "https://fapi.binance.com"
SPOT_BASE = "https://api.binance.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Accept": "application/json"}
TIMEOUT = 10
SYMBOLS = [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT"), ("SOL", "SOLUSDT")]


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(BASE + path, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    print(f"[WARN] {path} 失败: {type(last).__name__}: {last}", flush=True)
    return None



def _get_spot(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """现货端点与合约端点不同域，分开取；失败不影响合约数据。"""
    last = None
    for attempt in range(3):
        try:
            r = requests.get(SPOT_BASE + path, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    print(f"[WARN] spot {path} 失败: {type(last).__name__}: {last}", flush=True)
    return None


# ── 历史回补 ────────────────────────────────────────────────────────────────
# 实时采集每 5 分钟一条，攒够"7 日持仓变化"要等一周；而币安的 /futures/data/*
# 端点直接给**最近 30 天**的历史（period 4h × limit 180 = 30 天），可以一次补齐。
# 这直接决定了技术状态读数里的"杠杆/主动性/持仓分歧"三维在历史回放中有没有数据。
BACKFILL = {
    "bn_open_interest": ("/futures/data/openInterestHist", "sumOpenInterest", "contract"),
    "bn_open_interest_usd": ("/futures/data/openInterestHist", "sumOpenInterestValue", "USD"),
    "bn_long_short_ratio": ("/futures/data/globalLongShortAccountRatio", "longShortRatio", "ratio"),
    "bn_top_position_ratio": ("/futures/data/topLongShortPositionRatio", "longShortRatio", "ratio"),
    "bn_taker_ratio": ("/futures/data/takerlongshortRatio", "buySellRatio", "ratio"),
}


def backfill(period: str = "4h", limit: int = 180) -> int:
    """回补 OI / 多空比 / 大户比 / 吃单比的最近 30 天历史（幂等，重复跑不会写重）。"""
    written = 0
    for asset, symbol in SYMBOLS:
        for indicator, (path, key, unit) in BACKFILL.items():
            data = _get(path, {"symbol": symbol, "period": period, "limit": limit})
            if not isinstance(data, list) or not data:
                print(f"[WARN] 回补 {indicator}/{asset} 无数据（{path}）", flush=True)
                continue
            records = []
            for row in data:
                try:
                    records.append({"ts": int(row["timestamp"]) // 1000,
                                    "value": float(row[key])})
                except (KeyError, TypeError, ValueError):
                    continue
            if not records:
                continue
            try:
                external_data.ingest_external(
                    asset=asset, indicator=indicator, records=records,
                    source="binance_backfill",
                    meta={"unit": unit, "window": period, "backfill": True},
                )
                written += len(records)
                print(f"[OK] {asset} {indicator}: {len(records)} 条"
                      f"（{time.strftime('%m-%d %H:%M', time.localtime(records[0]['ts']))}"
                      f" → {time.strftime('%m-%d %H:%M', time.localtime(records[-1]['ts']))}）",
                      flush=True)
            except Exception as exc:
                print(f"[WARN] 回补写入 {indicator}/{asset} 失败: {exc}", flush=True)
    print(f"[{time.strftime('%H:%M:%S')}] 回补共写 {written} 条", flush=True)
    return written


def main() -> int:
    ts = int(time.time())
    written = 0

    for asset, symbol in SYMBOLS:
        # 1) 资金费率 + 标记价
        premium = _get("/fapi/v1/premiumIndex", {"symbol": symbol})
        rate = mark = basis = None
        if isinstance(premium, dict):
            try:
                rate = float(premium.get("lastFundingRate"))
                mark = float(premium.get("markPrice"))
                index_px = float(premium.get("indexPrice"))
                if index_px:
                    # 基差 = (标记价 - 指数价) / 指数价。正=期货溢价（多头拥挤），
                    # 负=折价（恐慌或交割压力）；与资金费率方向不一致时是背离信号。
                    basis = (mark - index_px) / index_px * 100
            except (TypeError, ValueError):
                pass

        # 2) 多空账户比（1 小时粒度）
        ls = _get("/futures/data/globalLongShortAccountRatio",
                  {"symbol": symbol, "period": "1h", "limit": 1})
        ls_ratio = None
        if isinstance(ls, list) and ls:
            try:
                ls_ratio = float(ls[-1].get("longShortRatio"))
            except (TypeError, ValueError):
                pass

        # 3) 未平仓
        oi = _get("/fapi/v1/openInterest", {"symbol": symbol})
        oi_qty = oi_usd = None
        if isinstance(oi, dict):
            try:
                oi_qty = float(oi.get("openInterest"))
                if oi_qty and mark:
                    oi_usd = oi_qty * mark
            except (TypeError, ValueError):
                pass

        # 4) 吃单买卖比
        taker = _get("/futures/data/takerlongshortRatio",
                     {"symbol": symbol, "period": "1h", "limit": 1})
        taker_ratio = None
        if isinstance(taker, list) and taker:
            try:
                taker_ratio = float(taker[-1].get("buySellRatio"))
            except (TypeError, ValueError):
                pass

        # 5) 大户持仓比：与上面的“账户数比”不同，看的是持仓量而非人数，
        #    散户可能人多钱少，大户才是真正能把价格推走的那批。
        top = _get("/futures/data/topLongShortPositionRatio",
                   {"symbol": symbol, "period": "1h", "limit": 1})
        top_ratio = None
        if isinstance(top, list) and top:
            try:
                top_ratio = float(top[-1].get("longShortRatio"))
            except (TypeError, ValueError):
                pass

        # 6) 现货 24h 成交额（体量刻度，用于判断合约杠杆相对现货的倍数）
        spot = _get("/fapi/v1/ticker/24hr", {"symbol": symbol})
        spot_vol = None
        if isinstance(spot, dict):
            try:
                spot_vol = float(spot.get("quoteVolume"))
            except (TypeError, ValueError):
                pass

        rows: List[tuple] = [
            ("bn_funding_rate", rate, {"unit": "ratio"}),
            ("bn_basis_pct", basis, {"unit": "%"}),
            ("bn_long_short_ratio", ls_ratio, {"unit": "ratio"}),
            ("bn_top_position_ratio", top_ratio, {"unit": "ratio", "note": "whale"}),
            ("bn_open_interest", oi_qty, {"unit": "contract"}),
            ("bn_open_interest_usd", oi_usd, {"unit": "USD"}),
            ("bn_taker_ratio", taker_ratio, {"unit": "ratio", "window": "1h"}),
            ("bn_perp_vol_24h", spot_vol, {"unit": "USD", "window": "24h", "note": "perpetual_turnover"}),
        ]
        for indicator, value, meta in rows:
            if value is None:
                continue
            try:
                external_data.ingest_external(
                    asset=asset, indicator=indicator,
                    records=[{"ts": ts, "value": float(value)}],
                    source="binance", meta=meta,
                )
                written += 1
            except Exception as exc:
                print(f"[WARN] 写入 {indicator}/{asset} 失败: {exc}", flush=True)

        print(f"[{time.strftime('%H:%M:%S')}] {asset}: funding={rate} ls={ls_ratio} "
              f"oi={oi_qty} taker={taker_ratio}", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] Binance 写 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Binance 永续资金流采集")
    ap.add_argument("--backfill", action="store_true", help="只跑 30 天历史回补")
    ap.add_argument("--period", default="4h", help="回补粒度：1h/4h/1d")
    ap.add_argument("--limit", type=int, default=180, help="回补条数（30 天 4h ≈ 180）")
    args = ap.parse_args()
    if args.backfill:
        sys.exit(0 if backfill(args.period, args.limit) else 1)
    sys.exit(main())
