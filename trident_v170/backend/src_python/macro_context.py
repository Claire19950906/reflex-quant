#!/usr/bin/env python3
"""新闻之外的宏观/盘面数据源。

五层（全部公开接口，失败标 unavailable，禁止造数）：
  structure  盘面结构 — 资金费率、基差、持仓量、多空比
  sentiment  舆情情绪 — Crypto Fear & Greed
  fed        美联储预期 — NY Fed EFFR + 联邦基金期货隐含利率
  fred       官方宏观指标 — FRED CPI/非农/利率/M2（无新闻，仅指标）
  flow       资金流向 — 永续主动买卖比 / 可选 ETF 净流入
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import blockchain_feed
import coingecko_feed
import coinpaprika_feed
import config
import db
import eia_feed
import equity_feed
import fred_feed

HttpGet = Callable[[str, int], Any]
HttpGetText = Callable[[str, int], str]

_TIMEOUT = 8
# 结构层「币安永续」币种（BTC/ETH）。XAU 走 OKX、WTI 无永续 —— 见 fetch_structure 内路由。
_SYMBOLS = (
    ("BTC", "BTCUSDT"),
    ("ETH", "ETHUSDT"),
)
_BINANCE_FAPI = "https://fapi.binance.com"
_OKX_PUBLIC = "https://www.okx.com"
_BYBIT_FAPI = "https://api.bybit.com"
_BITGET_FAPI = "https://api.bitget.com"
_FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"
_NYFED_EFFR = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json"
_YAHOO_ZQ = "https://query1.finance.yahoo.com/v8/finance/chart/ZQ=F?interval=1d&range=5d"
_CFTC_COT = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
_SPDR_GLD_CSV = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
_DERIBIT_PUBLIC = "https://www.deribit.com/api/v2/public"


def _now() -> int:
    return int(time.time())


def _urlopen_retry(url: str, headers: Dict[str, str], timeout: int = _TIMEOUT) -> bytes:
    """带轻量重试的网络拉取——缓解 LetsVPN TUN 分流对国外域名的瞬时握手失败。

    只对「纯连接失败」（URLError 且非 HTTPError，即没连上/被重置/超时/SSL EOF）退避重试，
    首次建隧道时 LetsVPN 偶发 SSL UNEXPECTED_EOF 抖动，故至多重试 2 次（0.6s / 1.5s）；
    收到 HTTP 响应（含 4xx/5xx）说明已连上，不重试。
    """
    last_exc: Optional[Exception] = None
    retry_delays = (0.6, 1.5)
    for attempt in range(len(retry_delays) + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            if isinstance(exc, urllib.error.HTTPError):
                raise
            last_exc = exc
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            raise
    raise last_exc  # type: ignore[misc]


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    raw = _urlopen_retry(
        url,
        {"User-Agent": "TridentAgentMVP/1.0", "Accept": "application/json"},
        timeout,
    )
    return json.loads(raw.decode("utf-8"))


def _default_get_text(url: str, timeout: int = _TIMEOUT) -> str:
    """拉取文本类源（如 SPDR 持仓 CSV），走系统代理，失败由调用方兜底。"""
    raw = _urlopen_retry(
        url,
        {"User-Agent": "TridentAgentMVP/1.0", "Accept": "text/csv,*/*"},
        timeout,
    )
    return raw.decode("utf-8", "replace")


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _metric(
    category: str,
    source: str,
    metric_key: str,
    value: Optional[float],
    *,
    unit: str = "",
    label: str = "",
    status: str = "ok",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = extra or {}
    if label:
        payload["label"] = label
    return {
        "category": category,
        "source": source,
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": status if value is not None or status != "ok" else "unavailable",
        "payload": payload,
    }


def _fail(category: str, source: str, metric_key: str, note: str) -> Dict[str, Any]:
    return _metric(category, source, metric_key, None, status="unavailable", extra={"note": note})


def _cftc_cot_url(commodity_name: str, market: Optional[str]) -> str:
    base = f"{_CFTC_COT}?commodity_name={urllib.parse.quote(commodity_name)}"
    if market:
        base += f"&market_and_exchange_names={urllib.parse.quote(market)}"
    return base + "&$order=report_date_as_yyyy_mm_dd%20desc&$limit=1"


def _cftc_latest(commodity_name: str, market: Optional[str] = None,
                 get: HttpGet = _default_get) -> Optional[Dict[str, Any]]:
    """拉取 CFTC COT 最新一条记录（按 commodity_name + 可选 market 精确匹配）。失败返回 None。"""
    try:
        body = get(_cftc_cot_url(commodity_name, market), _TIMEOUT)
        recs = body if isinstance(body, list) else []
        rec = recs[0] if recs and isinstance(recs[0], dict) else None
        return rec or None
    except Exception:
        return None


def _cftc_spec_ratio(rec: Optional[Dict[str, Any]]) -> Optional[float]:
    """投机(非商业)多空持仓比 = noncomm_long / noncomm_short（大户/基金方向代理）。"""
    long_v = _num(rec.get("noncomm_positions_long_all")) if rec else None
    short_v = _num(rec.get("noncomm_positions_short_all")) if rec else None
    if not long_v or not short_v:
        return None
    return round(long_v / short_v, 4)


def fetch_structure(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for asset, symbol in _SYMBOLS:
        try:
            prem = get(f"{_BINANCE_FAPI}/fapi/v1/premiumIndex?symbol={symbol}", _TIMEOUT)
            last = _num((prem or {}).get("markPrice"))
            index = _num((prem or {}).get("indexPrice"))
            funding = _num((prem or {}).get("lastFundingRate"))
            basis_bps = None
            if last and index and index != 0:
                basis_bps = (last / index - 1.0) * 10000.0
            rows.append(_metric("structure", "binance_fapi", f"{asset}.mark", last, unit="USDT", label=asset))
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.funding",
                funding * 100 if funding is not None else None,
                unit="%", label=asset,
            ))
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.basis_bps",
                basis_bps, unit="bp", label=asset,
            ))
        except Exception as exc:
            rows.append(_fail("structure", "binance_fapi", f"{asset}.premium", f"{type(exc).__name__}"))
        try:
            oi = get(f"{_BINANCE_FAPI}/fapi/v1/openInterest?symbol={symbol}", _TIMEOUT)
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.oi",
                _num((oi or {}).get("openInterest")), unit="contracts", label=asset,
            ))
        except Exception as exc:
            rows.append(_fail("structure", "binance_fapi", f"{asset}.oi", f"{type(exc).__name__}"))
        try:
            ratio = get(
                f"{_BINANCE_FAPI}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=1",
                _TIMEOUT,
            )
            item = ratio[0] if isinstance(ratio, list) and ratio else {}
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.ls_ratio",
                _num(item.get("longShortRatio")), unit="x", label=asset,
                extra={"longAccount": _num(item.get("longAccount")), "shortAccount": _num(item.get("shortAccount"))},
            ))
        except Exception as exc:
            rows.append(_fail("structure", "binance_fapi", f"{asset}.ls_ratio", f"{type(exc).__name__}"))
        try:
            # 大户持仓多空比（top trader 持仓，区别于全体账户 ls_ratio）
            ratio = get(
                f"{_BINANCE_FAPI}/futures/data/topLongShortPositionRatio?symbol={symbol}&period=1h&limit=1",
                _TIMEOUT,
            )
            item = ratio[0] if isinstance(ratio, list) and ratio else {}
            rows.append(_metric(
                "structure", "binance_fapi", f"{asset}.top_ls_ratio",
                _num(item.get("longShortRatio")), unit="x", label=asset,
                extra={"longAccount": _num(item.get("longAccount")), "shortAccount": _num(item.get("shortAccount"))},
            ))
        except Exception as exc:
            rows.append(_fail("structure", "binance_fapi", f"{asset}.top_ls_ratio", f"{type(exc).__name__}"))
    # 1.5) 多所资金费率/OI 对比 — Bybit/Bitget 免 key 公开 API（补币安单所盲区，费率分歧→资金面信号）
    for asset, symbol in _SYMBOLS:
        rows.extend(_multi_venue_structure_rows(asset, symbol, get))

    # 2) 黄金 XAU — OKX 永续（真实合约：funding-rate / open-interest / ticker）
    rows.extend(_okx_structure_rows("XAU", "XAU-USDT-SWAP", get))
    # 3) 原油 WTI — 无免费永续合约；费率/基差无源保持 N/A，持仓指标改用 CFTC 原油期货（真实周度）
    # 注意：WTI 主力物理原油的 COT 市场名是 "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE"
    # （2022-02 起 "CRUDE OIL, LIGHT SWEET - Nymex" 停报，勿用旧名以免拉到过期数据）
    wti_cot = _cftc_latest("CRUDE OIL", "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE", get)
    wti_spec = _cftc_spec_ratio(wti_cot)
    wti_oi = _num((wti_cot or {}).get("open_interest_all")) if wti_cot else None
    wti_note = {"source_note": "CFTC WTI 原油期货持仓（WTI-PHYSICAL NYMEX，周度）；无免费永续合约，费率/基差无源", "freshness": "weekly"}
    rows.extend([
        _fail("structure", "none", "WTI.mark", "WTI 无免费永续合约"),
        _fail("structure", "none", "WTI.funding", "WTI 无免费永续合约"),
        _fail("structure", "none", "WTI.basis_bps", "WTI 无免费永续合约"),
    ])
    if wti_oi is not None:
        rows.append(_metric("structure", "cftc", "WTI.oi", wti_oi, unit="contracts", label="WTI", extra=wti_note))
    else:
        rows.append(_fail("structure", "cftc", "WTI.oi", "CFTC 原油持仓不可用"))
    if wti_spec is not None:
        rows.append(_metric("structure", "cftc", "WTI.ls_ratio", wti_spec, unit="x", label="WTI", extra=wti_note))
        rows.append(_metric("structure", "cftc", "WTI.top_ls_ratio", wti_spec, unit="x", label="WTI", extra=wti_note))
    else:
        rows.append(_fail("structure", "cftc", "WTI.ls_ratio", "CFTC 原油持仓不可用"))
        rows.append(_fail("structure", "cftc", "WTI.top_ls_ratio", "CFTC 原油持仓不可用"))
    # 4) IG Markets 客户情绪（散户多空比）—— XAU/WTI/BTC 的 IG 散户多空百分比。
    # 需登录（IG_IDENTIFIER+IG_PASSWORD），未配凭据/登录失败时逐标的 unavailable（不造数）。
    try:
        import ig_feed
        rows.extend(ig_feed.fetch_ig_sentiment())
    except Exception as exc:
        rows.append(_fail("structure", "ig", "IG.sentiment", f"{type(exc).__name__}"))
    return rows


def _multi_venue_structure_rows(asset: str, symbol: str, get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """多所永续资金费率/OI — Bybit / Bitget（免 key 公开 API，补币安单所盲区）。

    各所费率分歧可反映跨所资金面；失败标 unavailable（不造数）。
    """
    rows: List[Dict[str, Any]] = []
    # Bybit：tickers 一次拿到 funding + OI + mark/index（→ 基差）
    try:
        body = get(f"{_BYBIT_FAPI}/v5/market/tickers?category=linear&symbol={symbol}", _TIMEOUT)
        item = (((body or {}).get("result") or {}).get("list") or [{}])[0] or {}
        funding = _num(item.get("fundingRate"))
        oi = _num(item.get("openInterest"))
        oi_usd = _num(item.get("openInterestValue"))
        mark = _num(item.get("markPrice"))
        index = _num(item.get("indexPrice"))
        basis_bps = None
        if mark and index and index != 0:
            basis_bps = (mark / index - 1.0) * 10000.0
        rows.append(_metric("structure", "bybit_fapi", f"{asset}.funding_bybit",
                            funding * 100 if funding is not None else None, unit="%", label=asset))
        rows.append(_metric("structure", "bybit_fapi", f"{asset}.oi_bybit", oi, unit="BTC", label=asset))
        rows.append(_metric("structure", "bybit_fapi", f"{asset}.oi_usd_bybit", oi_usd, unit="usd", label=asset))
        rows.append(_metric("structure", "bybit_fapi", f"{asset}.basis_bybit", basis_bps, unit="bp", label=asset))
    except Exception as exc:
        rows.append(_fail("structure", "bybit_fapi", f"{asset}.funding", f"{type(exc).__name__}"))
    # Bitget：current-fund-rate 拿资金费率（费率是跨所分歧核心）
    try:
        body = get(f"{_BITGET_FAPI}/api/v2/mix/market/current-fund-rate?symbol={symbol}&productType=USDT-FUTURES", _TIMEOUT)
        item = (((body or {}).get("data") or [{}])[0]) or {}
        funding = _num(item.get("fundingRate"))
        rows.append(_metric("structure", "bitget_fapi", f"{asset}.funding_bitget",
                            funding * 100 if funding is not None else None, unit="%", label=asset))
    except Exception as exc:
        rows.append(_fail("structure", "bitget_fapi", f"{asset}.funding", f"{type(exc).__name__}"))
    return rows


def _okx_structure_rows(asset: str, inst_id: str, get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """黄金 XAU 盘面结构 — OKX 永续（funding-rate / open-interest / ticker）。"""
    import quote_sources
    rows: List[Dict[str, Any]] = []
    funding = quote_sources.fetch_okx_funding(inst_id)
    rows.append(_metric("structure", "okx_swap", f"{asset}.funding",
                        funding * 100 if funding is not None else None,
                        unit="%", label=asset))
    tick = quote_sources.fetch_okx_ticker(inst_id)
    mark = tick.get("last") if tick else None
    rows.append(_metric("structure", "okx_swap", f"{asset}.mark", mark, unit="USDT", label=asset))
    oi = quote_sources.fetch_okx_open_interest(inst_id)
    rows.append(_metric("structure", "okx_swap", f"{asset}.oi", oi, unit="contracts", label=asset))
    # 多空持仓比：OKX 无 XAU 账户多空比公开接口 → 改用 CFTC 黄金期货投机(非商业)持仓比
    # （真实免费周度源，source 明确标 cftc，标注“替代”，不造数）
    gold_cot = _cftc_latest("GOLD", get=get)
    spec_ratio = _cftc_spec_ratio(gold_cot)
    cot_note = {"source_note": "CFTC 黄金期货投机(非商业)多空持仓比（周度），替代不存在的 OKX 账户多空比", "freshness": "weekly"}
    if spec_ratio is not None:
        rows.append(_metric("structure", "cftc", f"{asset}.ls_ratio", spec_ratio, unit="x", label=asset, extra=cot_note))
        rows.append(_metric("structure", "cftc", f"{asset}.top_ls_ratio", spec_ratio, unit="x", label=asset, extra=cot_note))
    else:
        rows.append(_fail("structure", "cftc", f"{asset}.ls_ratio", "CFTC 黄金持仓不可用"))
        rows.append(_fail("structure", "cftc", f"{asset}.top_ls_ratio", "CFTC 黄金持仓不可用"))
    # OKX XAU 无 index 基差接口 → 保持 unavailable（不造数）
    rows.append(_fail("structure", "okx_swap", f"{asset}.basis_bps", "OKX XAU 无 index 基差接口"))
    return rows


def fetch_sentiment(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """舆情情绪层 — Crypto Fear & Greed（alternative.me，免 key、可直连、日度）。

    属慢变量情绪背景：仅作趋势/情绪交叉验证（同 EIA/COT/SPDR 级别），不当即时时点信号。
    """
    try:
        body = get(_FNG_URL, _TIMEOUT)
        data = (body.get("data") or []) if isinstance(body, dict) else None
        if not data or not isinstance(data, list) or not isinstance(data[0], dict):
            raise ValueError("fng 数据为空")
        cur = data[0]
        value = _num(cur.get("value"))
        classification = cur.get("value_classification") or ""
        return [_metric(
            "sentiment", "alternative.me", "crypto_fng",
            value, unit="index",
            extra={"classification": classification, "label": "BTC", "scope": "日度情绪背景"},
        )]
    except Exception as exc:
        return [_metric(
            "sentiment", "alternative.me", "crypto_fng",
            None, unit="index",
            extra={"note": f"{type(exc).__name__}", "label": "BTC"},
        )]


def fetch_fed(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    effr = None
    try:
        body = get(_NYFED_EFFR, _TIMEOUT)
        ref = ((body or {}).get("refRates") or [None])[0] or {}
        effr = _num(ref.get("percentRate"))
        rows.append(_metric(
            "fed", "nyfed", "effr",
            effr, unit="%",
            extra={"effectiveDate": ref.get("effectiveDate"), "type": ref.get("type")},
        ))
    except Exception as exc:
        rows.append(_fail("fed", "nyfed", "effr", f"{type(exc).__name__}"))

    implied = None
    try:
        body = get(_YAHOO_ZQ, _TIMEOUT)
        result = (((body or {}).get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        implied_price = _num(meta.get("regularMarketPrice"))
        if implied_price is None:
            closes = (((result.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
            for item in reversed(closes):
                implied_price = _num(item)
                if implied_price is not None:
                    break
        if implied_price is not None:
            implied = 100.0 - implied_price
        rows.append(_metric(
            "fed", "yahoo_zq", "implied_ff",
            implied, unit="%",
            extra={"futures_price": implied_price, "symbol": "ZQ=F"},
        ))
    except Exception as exc:
        rows.append(_fail("fed", "yahoo_zq", "implied_ff", f"{type(exc).__name__}"))

    if effr is not None and implied is not None:
        spread_bp = (implied - effr) * 100.0
        if spread_bp >= 12.5:
            next_move = "hike"
        elif spread_bp <= -12.5:
            next_move = "cut"
        else:
            next_move = "hold"
        rows.append(_metric(
            "fed", "derived", "next_move_bp",
            spread_bp, unit="bp",
            extra={"next_move": next_move, "method": "ZQ implied − EFFR；非 CME FedWatch 官方概率"},
        ))
    else:
        rows.append(_fail("fed", "derived", "next_move_bp", "缺少 EFFR 或隐含利率"))
    return rows


def fetch_flow(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # XAU/WTI 无免费永续 taker 买卖比/CVD 数据源 → unavailable（不造数，拆掉跨币种错配）
    rows.append(_fail("flow", "none", "XAU.taker_buy_sell", "XAU 无免费 taker 买卖比数据源"))
    rows.append(_fail("flow", "none", "XAU.cvd", "XAU 无免费 CVD 数据源"))
    rows.append(_fail("flow", "none", "WTI.taker_buy_sell", "WTI 无免费 taker 买卖比数据源"))
    rows.append(_fail("flow", "none", "WTI.cvd", "WTI 无免费 CVD 数据源"))
    for asset, symbol in _SYMBOLS:
        try:
            body = get(
                f"{_BINANCE_FAPI}/futures/data/takerlongshortRatio?symbol={symbol}&period=1h&limit=1",
                _TIMEOUT,
            )
            item = body[0] if isinstance(body, list) and body else {}
            buy = _num(item.get("buyVol"))
            sell = _num(item.get("sellVol"))
            ratio = _num(item.get("buySellRatio"))
            net = None
            if buy is not None and sell is not None:
                net = buy - sell
            rows.append(_metric(
                "flow", "binance_taker", f"{asset}.taker_buy_sell",
                ratio, unit="x", label=asset,
                extra={"buyVol": buy, "sellVol": sell, "netVol": net},
            ))
            # ── CVD 资金流向（近 1h 主动买卖净额占比，复用同一响应零额外请求）──
            # CVD = 主动买量 − 主动卖量；归一为占比 %（跨资产可比，正值=净买盘）。
            # 供 [跨资产资金] 段判断：净买盘强 → 支撑方向；净卖盘强 → 压制方向。
            if buy is not None and sell is not None and (buy + sell) > 0:
                cvd_pct = (buy - sell) / (buy + sell) * 100.0
                rows.append(_metric(
                    "flow", "binance_taker", f"{asset}.cvd",
                    round(cvd_pct, 1), unit="%", label=asset,
                    extra={"buyVol": buy, "sellVol": sell, "netVol": net,
                           "note": "CVD=主动买−主动卖（近1h）"},
                ))
            else:
                rows.append(_fail("flow", "binance_taker", f"{asset}.cvd", "无成交数据"))
        except Exception as exc:
            rows.append(_fail("flow", "binance_taker", f"{asset}.taker_buy_sell", f"{type(exc).__name__}"))
            rows.append(_fail("flow", "binance_taker", f"{asset}.cvd", f"{type(exc).__name__}"))

    etf_url = str(getattr(config, "SOSOVALUE_ETF_URL", "") or "").strip()
    if not etf_url:
        rows.append(_fail("flow", "sosovalue", "btc_etf_net", "未配置 SOSOVALUE_ETF_URL"))
        return rows
    try:
        body = get(etf_url, _TIMEOUT)
        items = body
        if isinstance(body, dict):
            items = body.get("data") or body.get("list") or body.get("items") or []
        if not isinstance(items, list):
            items = []
        picked = None
        for item in items:
            if not isinstance(item, dict):
                continue
            blob = json.dumps(item, ensure_ascii=False).upper()
            if "BTC" in blob or "BITCOIN" in blob:
                picked = item
                break
        if picked is None and items:
            picked = items[0] if isinstance(items[0], dict) else None
        value = None
        if isinstance(picked, dict):
            for key in ("netInflow", "dailyNetInflow", "netflow", "netFlow", "value"):
                value = _num(picked.get(key))
                if value is not None:
                    break
        rows.append(_metric(
            "flow", "sosovalue", "btc_etf_net",
            value, unit="usd",
            extra={"raw_keys": sorted(picked.keys())[:12] if isinstance(picked, dict) else []},
        ))
    except Exception as exc:
        rows.append(_fail("flow", "sosovalue", "btc_etf_net", f"{type(exc).__name__}"))
    return rows


def fetch_liquidation(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """爆仓层 — OKX 公共 API（免 key、国内直连）：最近已成交强平单。

    返回每标的 24h 强平聚合：总笔数、多/空被平张数、最大单笔。
    OKX 仅 BTC/ETH 等主流有永续强平流；XAU/WTI 无 → 不拉。
    """
    rows: List[Dict[str, Any]] = []
    # OKX 永续支持 BTC/ETH/XAU（XAU 刚验证可拉）；WTI 无 OKX 指数 → 不拉
    okx_symbols = {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "XAU": "XAU-USDT"}
    for asset, uly in okx_symbols.items():
        try:
            body = get(
                f"{_OKX_PUBLIC}/api/v5/public/liquidation-orders"
                f"?instType=SWAP&uly={uly}&state=filled&limit=10",
                _TIMEOUT,
            )
            details = []
            if isinstance(body, dict) and body.get("code") == "0":
                for item in body.get("data") or []:
                    if isinstance(item, dict):
                        details.extend(item.get("details") or [])
            if not details:
                rows.append(_fail("liquidation", "okx", f"{asset}.liq", "无强平记录"))
                continue
            n_buy = 0  # 空头被平（buy 强平 = 空单被平）
            n_sell = 0  # 多头被平（sell 强平 = 多单被平）
            max_sz = 0.0
            for d in details:
                if not isinstance(d, dict):
                    continue
                side = str(d.get("side") or "").lower()
                if side == "buy":
                    n_buy += 1
                elif side == "sell":
                    n_sell += 1
                sz = _num(d.get("sz"))
                if sz is not None:
                    max_sz = max(max_sz, sz)
            total = n_buy + n_sell
            rows.append(_metric(
                "liquidation", "okx", f"{asset}.liq",
                float(total), unit="单",
                label=asset,
                extra={
                    "long_liq": n_sell,   # 多头被平
                    "short_liq": n_buy,   # 空头被平
                    "max_single_sz": max_sz,
                    "note": f"近批强平 {total} 单（多头被平 {n_sell}/空头被平 {n_buy}）" if total else "无",
                },
            ))
        except Exception as exc:
            rows.append(_fail("liquidation", "okx", f"{asset}.liq", f"{type(exc).__name__}"))
    return rows


def _coinglass_get(path: str, timeout: int = _TIMEOUT) -> Any:
    """Coinglass V4 专用 GET：需要 CG-API-KEY 认证头（与默认无 key 的 _default_get 不同）。"""
    url = f"{config.COINGLASS_BASE_URL}{path}"
    raw = _urlopen_retry(
        url,
        {
            "User-Agent": "TridentAgentMVP/1.0",
            "Accept": "application/json",
            "CG-API-KEY": config.COINGLASS_API_KEY,
        },
        timeout,
    )
    return json.loads(raw.decode("utf-8"))


def fetch_coinglass(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """Coinglass 聚合衍生品层 — 全网爆仓 / 全市场 OI / 资金费率 / 恐惧贪婪。

    实事求是：Coinglass 免费 tier 对所有数据端点返回 {"code":"401","msg":"Upgrade plan"}
    （付费墙）。本层探测 fear-greed 端点确认权限，未付费则三个聚合指标统一
    降级为 unavailable 并标注「需付费计划」，不阻塞、不造数；付费后自动生效。
    """
    metric_keys = ("agg.liquidation", "agg.oi", "agg.funding", "crypto_fng")
    if not config.COINGLASS_API_KEY:
        return [_fail("coinglass", "coinglass", k, "COINGLASS_API_KEY 未配置") for k in metric_keys]
    try:
        body = _coinglass_get("/api/index/fear-greed-history?interval=1d&limit=2")
        code = str((body or {}).get("code", ""))
        msg = str((body or {}).get("msg", ""))
        if code != "0" or msg.lower() == "upgrade plan":
            note = f"Coinglass 需付费计划（{msg or code or 'free tier'}）"
            return [_fail("coinglass", "coinglass", k, note) for k in metric_keys]
        # 已开通付费：fear-greed 已可读，其余聚合端点后续接入（解析逻辑已备好，此处不回填不造数）
        data = body.get("data") or []
        value = _num((data[0] or {}).get("value")) if data and isinstance(data[0], dict) else None
        rows = [_metric("coinglass", "coinglass", "crypto_fng", value, unit="index",
                        extra={"note": "V4 fear-greed 已接入"})]
        rows.extend(_fail("coinglass", "coinglass", k, "待接入（V4 端点已备）")
                    for k in ("agg.liquidation", "agg.oi", "agg.funding"))
        return rows
    except Exception as exc:
        note = f"{type(exc).__name__}"
        return [_fail("coinglass", "coinglass", k, note) for k in metric_keys]


def _coinglass_free_fallback(
    structure: List[Dict[str, Any]], liquidation: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Coinglass 付费墙降级：全网聚合 → 免 key 单所口径（币安/OKX），诚实标注不造数。

    Coinglass 免费 tier 对所有端点返回 401「Upgrade plan」，即便有免费 key 也用不了。
    它聚合的爆仓/OI/资金费率在币安/OKX 公开 API 已有等价单所口径，故降级为单所口径。
    crypto_fng 由 sentiment 层（alternative.me，免 key 直连）独占，此处不重复输出。
    """
    def _pick(rows: List[Dict[str, Any]], *suffixes: str) -> Optional[Dict[str, Any]]:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if r.get("status") == "ok" and r.get("value") is not None:
                mk = str(r.get("metric_key") or "")
                if any(mk.endswith(s) for s in suffixes):
                    return r
        return None

    fund = _pick(structure, ".funding")
    oi = _pick(structure, ".oi")
    liq = _pick(liquidation, ".liq")
    note = "Coinglass 付费墙，免费单所口径替代"

    def _m(key: str, src: Optional[Dict[str, Any]], default_unit: str) -> Dict[str, Any]:
        unit = (src.get("unit") or default_unit) if src else default_unit
        return _metric(
            "coinglass", "binance/okx(代替)", key,
            src.get("value") if src else None,
            unit=unit, label="BTC",
            extra={"note": note, "src_metric": src.get("metric_key") if src else None},
        )

    return [
        _m("agg.funding", fund, "%"),
        _m("agg.oi", oi, "张"),
        _m("agg.liquidation", liq, "张"),
    ]


def fetch_fred(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """FRED 官方宏观指标层 — CPI/非农/利率/M2（无新闻，仅指标；失败标 unavailable）。"""
    return fred_feed.fetch_snapshot(get)


def fetch_eia(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """EIA 原油/汽油周度库存层（官方供需，每周发布；失败标 unavailable）。"""
    return eia_feed.fetch_snapshot(get)


def fetch_rotation(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """加密资金轮动层 — CoinPaprika BTC 市占率/总市值/24h量（实时，失败标 unavailable）。"""
    return coinpaprika_feed.fetch_snapshot(get)


def fetch_onchain(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """BTC 链上基本面层 — Blockchain.com 哈希率/难度（日度，失败标 unavailable）。"""
    return blockchain_feed.fetch_snapshot(get)


def fetch_breadth(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """加密市场宽度层 — CoinGecko 涨跌家数/ETH市占率/全市场涨跌（实时，失败标 unavailable）。"""
    return coingecko_feed.fetch_snapshot(get)


def fetch_equity(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """美股基本面层 — FMP 主力 + Alpha Vantage 逐标的补齐（FMP 直连间歇被墙时用 AV 补；低频缓存，失败标 unavailable）。"""
    return equity_feed.fetch_snapshot(get)


# COT 全品类品种表：(metric 前缀, CFTC commodity_name, 精确市场名或 None, 品类分组, 中文标签)
# 覆盖贵金属/能源/股指/债券利率/外汇/加密 —— CFTC 官方免费的“全网机构持仓资金流”（周度）。
# 单品种独立拉取：某品种失败只标 unavailable，不拖累其它品种，禁止造数。
# market 精确名失效时自动回落为裸 commodity_name 兜底（记录实际市场到 payload.market）。
_COT_INSTRUMENTS = (
    # —— 贵金属 ——
    ("gold", "GOLD", None, "贵金属", "黄金"),
    ("silver", "SILVER", None, "贵金属", "白银"),
    ("copper", "COPPER", None, "贵金属", "铜"),
    ("platinum", "PLATINUM", None, "贵金属", "铂金"),
    ("palladium", "PALLADIUM", None, "贵金属", "钯金"),
    # —— 能源 ——
    ("wti", "CRUDE OIL", "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE", "能源", "原油WTI"),
    ("natgas", "NATURAL GAS", None, "能源", "天然气"),
    ("gasoline", "GASOLINE", None, "能源", "汽油Rbob"),
    ("heatoil", "HEATING OIL-DIESEL-GASOIL", None, "能源", "取暖油/柴油"),
    # —— 股指 ——
    ("sp500", "S&P BROAD BASED STOCK INDICES", None, "股指", "标普500"),
    ("nasdaq", "NASDAQ  BROADBASED INDICES", None, "股指", "纳斯达克100"),
    ("russell", "RUSSELL INDEX", None, "股指", "罗素指数"),
    ("nikkei", "NIKKEI STOCK AVERAGE", None, "股指", "日经225"),
    ("msci", "MSCI INDICES", None, "股指", "MSCI新兴"),
    # —— 债券 / 利率 ——
    ("tbond", "T-BONDS", None, "债券利率", "美债30Y"),
    ("tnote10y", "T-NOTES, 6.5-10 YEAR", None, "债券利率", "美债10Y"),
    ("tnote5y", "T-NOTES, 4-6 YEAR", None, "债券利率", "美债5Y"),
    ("tnote2y", "T-NOTES, 1-2 YEAR", None, "债券利率", "美债2Y"),
    # —— 外汇 ——
    ("jpy", "JAPANESE YEN", None, "外汇", "日元"),
    ("gbp", "POUND STERLING", None, "外汇", "英镑"),
    ("chf", "SWISS FRANC", None, "外汇", "瑞郎"),
    ("aud", "AUSTRALIAN DOLLAR", None, "外汇", "澳元"),
    ("cad", "CANADIAN DOLLAR", None, "外汇", "加元"),
    ("mxn", "MEXICAN PESO", None, "外汇", "墨西哥比索"),
    ("nzd", "NEW ZEALAND DOLLAR", None, "外汇", "纽元"),
    ("dxy", "U.S. DOLLAR INDEX", None, "外汇", "美元指数"),
    # —— 加密 ——
    ("btc", "BITCOIN", None, "加密", "比特币"),
    ("sol", "SOLANA", None, "加密", "索拉纳"),
)

_cot_cache: Dict[str, Any] = {"ts": 0.0, "rows": []}
_COT_TTL_S = 6 * 3600  # COT 每周发布一次，缓存 6 小时避免频繁拉取拖慢宏观聚合


def _build_cot_rows(get: HttpGet) -> List[Dict[str, Any]]:
    def _net(long_v: Optional[float], short_v: Optional[float]) -> Optional[float]:
        if long_v is None or short_v is None:
            return None
        return long_v - short_v

    rows: List[Dict[str, Any]] = []
    for prefix, cname, market, group, label_cn in _COT_INSTRUMENTS:
        try:
            rec = _cftc_latest(cname, market, get)
            if not rec and market:
                rec = _cftc_latest(cname, None, get)  # market 精确名失效时回落裸名兜底
            if not rec:
                rows.append(_fail("cot", "cftc", f"{prefix}.cot_noncomm_net", f"CFTC 无 {label_cn}({cname}) 记录"))
                continue
            long_v = _num(rec.get("noncomm_positions_long_all"))
            short_v = _num(rec.get("noncomm_positions_short_all"))
            comm_long = _num(rec.get("comm_positions_long_all"))
            comm_short = _num(rec.get("comm_positions_short_all"))
            chg_long = _num(rec.get("change_in_noncomm_long_all"))
            chg_short = _num(rec.get("change_in_noncomm_short_all"))
            chg_comm_long = _num(rec.get("change_in_comm_long_all"))
            chg_comm_short = _num(rec.get("change_in_comm_short_all"))
            oi = _num(rec.get("open_interest_all"))
            net = _net(long_v, short_v)
            chg = _net(chg_long, chg_short)
            comm_net = _net(comm_long, comm_short)
            comm_chg = _net(chg_comm_long, chg_comm_short)
            ratio = (round(long_v / short_v, 4)) if long_v is not None and short_v and short_v != 0 else None
            extra = {
                "report_date": str(rec.get("report_date_as_yyyy_mm_dd", "") or "")[:10],
                "freshness": "weekly",
                "group": group,
                "market": str(rec.get("market_and_exchange_names", "") or ""),
            }
            rows.extend([
                _metric("cot", "cftc", f"{prefix}.cot_noncomm_net", net, unit="张", label=label_cn, extra=extra),
                _metric("cot", "cftc", f"{prefix}.cot_noncomm_chg", chg, unit="张", label=label_cn, extra=extra),
                _metric("cot", "cftc", f"{prefix}.cot_comm_net", comm_net, unit="张", label=label_cn, extra=extra),
                _metric("cot", "cftc", f"{prefix}.cot_comm_chg", comm_chg, unit="张", label=label_cn, extra=extra),
                _metric("cot", "cftc", f"{prefix}.cot_oi", oi, unit="张", label=label_cn, extra=extra),
                _metric("cot", "cftc", f"{prefix}.cot_spec_ratio", ratio, unit="x", label=label_cn, extra=extra),
            ])
        except Exception as exc:
            rows.append(_fail("cot", "cftc", f"{prefix}.cot_noncomm_net", f"{type(exc).__name__}"))
    return rows


def fetch_cot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """COT 持仓层 — CFTC 全品类机构持仓（贵金属/能源/股指/债券利率/外汇/加密，每周发布）。

    投机(非商业)净头寸 = 拥挤度；跨资产横向可比 → 判资金在 [商品/股指/债券/外汇/加密] 间轮动。
    品种独立拉取 + 6 小时缓存：单品种失败只标 unavailable，拖不垮其余品种，禁止造数。

    测试/调试场景若显式传入 get 函数（非默认实现）则跳过缓存，避免不同测试间相互污染
    （例如前一个测试成功缓存数据，后一个测试传 boom/[] 想验证 unavailable 时直接命中缓存）。
    """
    now = time.time()
    is_default = get is _default_get
    if is_default and now - float(_cot_cache["ts"]) < _COT_TTL_S and _cot_cache["rows"]:
        return list(_cot_cache["rows"])
    rows = _build_cot_rows(get)
    if is_default:
        _cot_cache["ts"] = now
        _cot_cache["rows"] = rows
    return list(rows)


def fetch_gold_etf(get_text: HttpGetText = _default_get_text) -> List[Dict[str, Any]]:
    """黄金 ETF 持仓层 — SPDR Gold Shares (GLD) 每日 gold in trust（走系统代理）。

    持仓吨数 + 环比日变化（正=流入 / 负=流出）→ 供 [水位博弈]/[叙事逻辑] 判断
    机构避险买盘是否坐实。CSV 列名随上游版本变化，按关键词动态匹配，失败标 unavailable。
    """
    try:
        etf_url = getattr(config, "GOLD_ETF_CSV_URL", "") or _SPDR_GLD_CSV
        raw = get_text(etf_url, _TIMEOUT)
    except Exception as exc:
        return [_fail("gold_etf", "spdr", "gold.etf_holdings", f"{type(exc).__name__}")]

    try:
        reader = csv.DictReader(io.StringIO(raw))
        header = [c for c in (reader.fieldnames or [])]
        rows = [r for r in reader if isinstance(r, dict)]

        def _pick(*keywords: str) -> Optional[str]:
            for col in header:
                low = col.lower()
                if all(k in low for k in keywords):
                    return col
            return None

        if not header or not rows:
            return [_fail("gold_etf", "spdr", "gold.etf_holdings", "CSV 无数据行")]
        date_col = _pick("date") or header[0]
        tonnes_col = _pick("tonne") or _pick("ton")
        ounces_col = _pick("ounce") or _pick("troy")
        if tonnes_col is None and ounces_col is None:
            return [_fail("gold_etf", "spdr", "gold.etf_holdings", "未找到持仓数量列")]

        def _tonnes(row: Dict[str, Any]) -> Optional[float]:
            if tonnes_col:
                return _num(row.get(tonnes_col))
            oz = _num(row.get(ounces_col))
            return None if oz is None else oz / 32150.7465  # troy oz → metric tonne

        latest = rows[-1]
        prev = rows[-2] if len(rows) > 1 else None
        cur = _tonnes(latest)
        prev_v = _tonnes(prev) if prev else None
        chg = None
        if cur is not None and prev_v is not None:
            chg = cur - prev_v
        extra = {
            "as_of": str(latest.get(date_col) or ""),
            "note": f"最新持仓 {cur:.1f} 吨" if cur is not None else "",
            "freshness": "daily",
        }
        return [
            _metric("gold_etf", "spdr", "gold.etf_holdings", cur, unit="吨", label="GLD", extra=extra),
            _metric("gold_etf", "spdr", "gold.etf_holdings_chg", chg, unit="吨", label="GLD", extra=extra),
        ]
    except Exception as exc:
        return [_fail("gold_etf", "spdr", "gold.etf_holdings", f"{type(exc).__name__}")]


def fetch_options(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """期权市场层 — Deribit 公共 API（免 key、可直连）：BTC/ETH 期权情绪与持仓结构。

    补齐「价格/持仓/新闻」三角之外最稀缺的期权维度：
      {asset}.dvol             隐含波动率指数 DVOL（日度，情绪/波动率背景）
      {asset}.opt_call_put_oi  Call 总持仓 / Put 总持仓（>1 看涨拥挤，<1 看跌拥挤）
      {asset}.opt_max_pain     最大痛点（卖权交割吸磁价位，价格有向此靠拢的引力）
    call/put 持仓比与 max pain 由近月期权 book_summary 聚合（实时快照），
    解析失败只标该项 unavailable，不整体失败。
    """
    import datetime

    def _parse_contract(name: str):
        # 形如 "BTC-13SEP26-85000-P" → (到期日, 行权价, C/P)
        parts = name.split("-")
        if len(parts) != 4:
            return None
        try:
            dt = datetime.datetime.strptime(parts[1], "%d%b%y")
        except ValueError:
            return None
        strike = _num(parts[2])
        opt = parts[3].upper()
        if strike is None or opt not in ("C", "P"):
            return None
        return (dt, strike, opt)

    rows: List[Dict[str, Any]] = []
    for asset in ("BTC", "ETH"):
        # 1) DVOL 隐含波动率指数（日度）
        try:
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - 2 * 86400000
            body = get(
                f"{_DERIBIT_PUBLIC}/get_volatility_index_data?currency={asset}"
                f"&start_timestamp={start_ms}&end_timestamp={now_ms}&resolution=1D",
                _TIMEOUT,
            )
            dvol = None
            if isinstance(body, dict):
                data = (body.get("result") or {}).get("data") or []
                if data and isinstance(data[-1], list) and len(data[-1]) >= 5:
                    dvol = _num(data[-1][4])  # 数据项 [ts, open, high, low, close]
            rows.append(_metric("options", "deribit", f"{asset}.dvol", dvol, unit="%",
                                label=asset, extra={"freshness": "daily"}))
        except Exception as exc:
            rows.append(_fail("options", "deribit", f"{asset}.dvol", f"{type(exc).__name__}"))

        # 2) book_summary 聚合 Call/Put OI 比 + max pain（近月，实时快照）
        try:
            body = get(f"{_DERIBIT_PUBLIC}/get_book_summary_by_currency?currency={asset}&kind=option", _TIMEOUT)
            contracts = (body.get("result") or []) if isinstance(body, dict) else []
            parsed = []
            for c in contracts:
                if not isinstance(c, dict):
                    continue
                p = _parse_contract(str(c.get("instrument_name") or ""))
                oi = _num(c.get("open_interest"))
                iv = _num(c.get("mark_iv"))
                if p is not None and oi is not None and oi > 0:
                    parsed.append((p, oi, iv))
            if not parsed:
                rows.append(_fail("options", "deribit", f"{asset}.opt_call_put_oi", "近月期权无持仓"))
                rows.append(_fail("options", "deribit", f"{asset}.opt_max_pain", "近月期权无持仓"))
                continue
            expiries = sorted({p[0] for p, _oi, _iv in parsed})
            nearest = expiries[0]
            near = [(p, oi, iv) for (p, oi, iv) in parsed if p[0] == nearest]
            call_oi = sum(oi for (p, oi, _iv) in near if p[2] == "C")
            put_oi = sum(oi for (p, oi, _iv) in near if p[2] == "P")
            ratio = (call_oi / put_oi) if put_oi > 0 else None
            opt_extra = {"expiry": nearest.strftime("%Y-%m-%d"), "call_oi": call_oi, "put_oi": put_oi}
            rows.append(_metric("options", "deribit", f"{asset}.opt_call_put_oi", ratio, unit="x",
                                label=asset, extra=opt_extra))
            # max pain：使所有期权卖方(按 OI 加权)总损失最小的行权价
            strikes = sorted({p[1] for p, _oi, _iv in near})
            best_strike: Optional[float] = None
            best_pain: Optional[float] = None
            for s_val in strikes:
                pain = 0.0
                for (p, oi, _iv) in near:
                    if p[2] == "C":
                        if s_val > p[1]:
                            pain += oi * (s_val - p[1])
                    else:
                        if p[1] > s_val:
                            pain += oi * (p[1] - s_val)
                if best_pain is None or pain < best_pain:
                    best_pain, best_strike = pain, s_val
            rows.append(_metric("options", "deribit", f"{asset}.opt_max_pain", best_strike, unit="USD",
                                label=asset, extra=opt_extra))

            # 3) 期权墙（近月 OI 最集中的行权价）+ 偏度（call IV − put IV，风险逆转近似）
            call_iv_pairs = [(iv, oi) for (p, oi, iv) in near if p[2] == "C" and iv is not None]
            put_iv_pairs = [(iv, oi) for (p, oi, iv) in near if p[2] == "P" and iv is not None]

            def _iv_avg(pairs):
                w = sum(oi for _iv, oi in pairs)
                return sum(iv * oi for iv, oi in pairs) / w if w > 0 else None

            call_iv_avg = _iv_avg(call_iv_pairs)
            put_iv_avg = _iv_avg(put_iv_pairs)
            skew = (call_iv_avg - put_iv_avg) if (call_iv_avg is not None and put_iv_avg is not None) else None
            call_wall = max(((oi, p[1]) for (p, oi, _iv) in near if p[2] == "C"), default=None)
            put_wall = max(((oi, p[1]) for (p, oi, _iv) in near if p[2] == "P"), default=None)
            wall_extra = {
                "expiry": nearest.strftime("%Y-%m-%d"),
                "call_wall_oi": call_wall[0] if call_wall else None,
                "put_wall": put_wall[1] if put_wall else None,
                "put_wall_oi": put_wall[0] if put_wall else None,
                "call_iv": call_iv_avg,
                "put_iv": put_iv_avg,
            }
            rows.append(_metric(
                "options", "deribit", f"{asset}.opt_wall",
                call_wall[1] if call_wall else None, unit="USD", label=asset,
                extra={"wall_type": "call_oi_max（上方阻力）", **wall_extra},
            ))
            rows.append(_metric(
                "options", "deribit", f"{asset}.opt_skew",
                skew, unit="ΔIV", label=asset,
                extra={"method": "call加权IV − put加权IV（风险逆转近似，非严格25Δ）",
                       "call_iv": call_iv_avg, "put_iv": put_iv_avg},
            ))
        except Exception as exc:
            rows.append(_fail("options", "deribit", f"{asset}.opt_call_put_oi", f"{type(exc).__name__}"))
            rows.append(_fail("options", "deribit", f"{asset}.opt_max_pain", f"{type(exc).__name__}"))
            rows.append(_fail("options", "deribit", f"{asset}.opt_wall", f"{type(exc).__name__}"))
            rows.append(_fail("options", "deribit", f"{asset}.opt_skew", f"{type(exc).__name__}"))
    return rows


def collect_layers(get: HttpGet = _default_get) -> Dict[str, List[Dict[str, Any]]]:
    structure = fetch_structure(get)
    sentiment = fetch_sentiment(get)
    liquidation = fetch_liquidation(get)
    coinglass = fetch_coinglass(get)
    # Coinglass 免费 tier 全 401（付费墙）→ 降级为免 key 单所口径，不空红、不造数
    if coinglass and all(it.get("status") != "ok" for it in coinglass):
        coinglass = _coinglass_free_fallback(structure, liquidation)
    return {
        "structure": structure,
        "sentiment": sentiment,
        "fed": fetch_fed(get),
        "fred": fetch_fred(get),
        "flow": fetch_flow(get),
        "liquidation": liquidation,
        "coinglass": coinglass,
        "cot": fetch_cot(get),
        "gold_etf": fetch_gold_etf(),
        "options": fetch_options(get),
        "eia": fetch_eia(get),
        "rotation": fetch_rotation(get),
        "onchain": fetch_onchain(get),
        "breadth": fetch_breadth(get),
        "equity": fetch_equity(get),
    }


def flatten(layers: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key in ("structure", "sentiment", "fed", "fred", "flow", "liquidation", "coinglass", "cot", "gold_etf", "options", "eia", "rotation", "onchain", "breadth", "equity"):
        rows.extend(layers.get(key) or [])
    return rows


def _report_layers(rows: List[Dict[str, Any]]) -> None:
    """把宏观各数据源最新状态 report 到信息源面板（按 category 聚合，实时标色）。"""
    import source_status
    cmap = {
        "fred": "fred", "cot": "cftc_cot", "gold_etf": "spdr_gld",
        "sentiment": "alternative_me", "options": "deribit_options",
        "eia": "eia", "rotation": "coinpaprika", "onchain": "blockchain", "breadth": "coingecko", "equity": "fmp", "coinglass": "coinglass",
    }
    for cat, sid in cmap.items():
        cat_rows = [r for r in rows if r.get("category") == cat]
        if not cat_rows:
            continue
        ok = any(r.get("status") == "ok" and r.get("value") is not None for r in cat_rows)
        err = ""
        if not ok:
            notes = []
            for r in cat_rows:
                note = (r.get("payload") or {}).get("note") or ""
                if note:
                    notes.append(note)
            err = "; ".join(notes)[:300]
        source_status.report(sid, ok, error=err)


def persist_layers(
    layers: Dict[str, List[Dict[str, Any]]],
    *,
    ts: Optional[int] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    rows = flatten(layers)
    if not rows:
        return 0
    _report_layers(rows)
    stamp = int(ts or _now())
    owned = connection is None
    conn = connection or db.get_connection()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macro_snapshots'"
        ).fetchone()
        if exists is None:
            return 0
        payload = [
            (
                stamp,
                item["category"],
                item["source"],
                item["metric_key"],
                item.get("value"),
                item.get("unit") or "",
                item.get("status") or "unavailable",
                json.dumps(item.get("payload") or {}, ensure_ascii=False),
            )
            for item in rows
        ]
        cursor = conn.executemany(
            """INSERT INTO macro_snapshots(ts, category, source, metric_key, value, unit, status, payload)
               VALUES(?,?,?,?,?,?,?,?)""",
            payload,
        )
        if owned:
            conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        if owned:
            conn.close()


# 来源标注：渲染进 prompt 时给每条数据标上游来源（防 LLM 误当模型自造/鼓励溯源）
_SOURCE_LABELS = {
    "binance_fapi": "Binance",
    "binance_taker": "Binance",
    "bybit_fapi": "Bybit",
    "bitget_fapi": "Bitget",
    "nyfed": "NY Fed",
    "yahoo_zq": "Yahoo",
    "fred": "FRED",
    "sosovalue": "SoSoValue",
    "okx": "OKX",
    "cftc": "CFTC",
    "spdr": "SPDR",
    "deribit": "Deribit",
    "eia": "EIA",
    "coinpaprika": "CoinPaprika",
    "coingecko": "CoinGecko",
    "blockchain": "Blockchain.com",
    "fmp": "FMP",
    "alphavantage": "AlphaVantage",
    "derived": "推演",
    "alternative.me": "AltMe",
}

_FRESH_SUFFIX = {"daily": "日度", "weekly": "周度", "delayed": "延迟"}


def _src_label(item: Dict[str, Any]) -> str:
    src = str(item.get("source") or "")
    return _SOURCE_LABELS.get(src, src)


# 各宏源约定的数据报告期/观测日期字段 → 统一提取，供 no-lookahead 防"用未来数据"。
_AS_OF_KEYS = ("observation_date", "report_date", "observation_period", "as_of")


def report_date_of(item: Dict[str, Any]) -> str:
    """提取宏源数据的报告期/观测日期（FRED/EIA/BLS/COT/SPDR 约定字段）。

    no-lookahead 校验入口：结论锚点时间不得早于该数据报告期。无字段返回空串。
    """
    payload = item.get("payload") or {}
    for key in _AS_OF_KEYS:
        val = payload.get(key)
        if val:
            return str(val).strip()
    return ""


def _norm_as_of(s: str) -> str:
    """归一化报告期：BLS observation_period '2026-M07' → '2026-07'；其余原样。"""
    s = (s or "").strip()
    if len(s) >= 6 and "-" in s:
        head, _sep, tail = s.partition("-")
        if len(head) == 4 and head.isdigit() and tail.startswith("M") and tail[1:].isdigit():
            return f"{head}-{int(tail[1:]):02d}"
    return s


def _as_of_suffix(item: Dict[str, Any]) -> str:
    """渲染后缀：报告期标注（有则 @归一化日期）；未来日期追加 ⚠未来 防前视。"""
    import datetime as _dt
    rd = _norm_as_of(report_date_of(item))
    if not rd:
        return ""
    # no-lookahead：报告期晚于当前时刻 → 视为未发布/脏数据，标注防前视（勿当已知喂给 AI）
    try:
        if _dt.datetime.strptime(rd[:10], "%Y-%m-%d").date() > _dt.datetime.now().date():
            return f"@{rd}⚠未来(勿用)"
    except (ValueError, TypeError):
        pass
    return f"@{rd}"


def _fmt(item: Dict[str, Any]) -> str:
    if item.get("status") != "ok" or item.get("value") is None:
        note = (item.get("payload") or {}).get("note") or "unavailable"
        return f"{item['metric_key']}=N/A ({note})"
    value = item["value"]
    unit = item.get("unit") or ""
    extra = item.get("payload") or {}
    src = _src_label(item)
    # 爆仓：展示多空被平明细（note 由 fetch 侧组装，含数字）
    if item["metric_key"].endswith(".liq"):
        note = extra.get("note") or ""
        return f"{item['metric_key']}={note}（{src}）" if note else f"{item['metric_key']}={value}{unit}（{src}）"
    # CVD 资金流向：净占比 + 方向标注（正值=净买盘 / 负值=净卖盘）
    if item["metric_key"].endswith(".cvd"):
        side = "净买盘" if value >= 0 else "净卖盘"
        label = extra.get("label") or item["metric_key"].split(".")[0]
        return f"{label}.CVD={value:+.1f}%（{side}，近1h 主动买卖，{src}）"
    # COT 持仓（CFTC 黄金/日元期货）：净头寸方向 + 周变化
    if item["metric_key"].endswith(".cot_noncomm_net") or item["metric_key"].endswith(".cot_comm_net"):
        role = "投机" if ".cot_noncomm_net" in item["metric_key"] else "商业"
        side = "净多" if value > 0 else ("净空" if value < 0 else "持平")
        lbl = extra.get("label") or "GOLD"
        return f"{lbl}.COT({role})净持仓={value:+,.0f}张（{side}，{src}）"
    if item["metric_key"].endswith(".cot_noncomm_chg") or item["metric_key"].endswith(".cot_comm_chg"):
        role = "投机" if ".cot_noncomm_chg" in item["metric_key"] else "商业"
        direction = "增持多头" if value > 0 else ("增持空头" if value < 0 else "持平")
        lbl = extra.get("label") or "GOLD"
        return f"{lbl}.COT({role})周变化={value:+,.0f}张（{direction}，{src}）"
    if item["metric_key"].endswith(".cot_oi"):
        lbl = extra.get("label") or "GOLD"
        return f"{lbl}.COT未平仓={value:,.0f}张（{src}）"
    if item["metric_key"].endswith(".cot_spec_ratio"):
        lbl = extra.get("label") or "GOLD"
        return f"{lbl}.COT投机多空比={value:.2f}x（{src}）"
    # 期权市场（Deribit）：DVOL 波动率指数 / Call-Put 持仓比 / 最大痛点
    if item["metric_key"].endswith(".dvol"):
        lbl = extra.get("label") or ""
        return f"{lbl}.DVOL={value:.1f}%（{src}）"
    if item["metric_key"].endswith(".opt_call_put_oi"):
        lbl = extra.get("label") or ""
        side = "看涨拥挤" if value > 1 else ("看跌拥挤" if value < 1 else "均衡")
        return f"{lbl}.期权Call/Put持仓比={value:.2f}x（{side}，{src}）"
    if item["metric_key"].endswith(".opt_max_pain"):
        lbl = extra.get("label") or ""
        return f"{lbl}.期权最大痛点={value:,.0f}USD（{src}）"
    # 多所永续费率/OI 对比（Bybit/Bitget）+ 期权墙/偏度（Deribit）
    if item["metric_key"].endswith(".funding_bybit"):
        lbl = extra.get("label") or ""
        return f"{lbl}.Bybit资金费率={value:+.4g}%（{src}）"
    if item["metric_key"].endswith(".funding_bitget"):
        lbl = extra.get("label") or ""
        return f"{lbl}.Bitget资金费率={value:+.4g}%（{src}）"
    if item["metric_key"].endswith(".oi_bybit"):
        lbl = extra.get("label") or ""
        return f"{lbl}.Bybit持仓OI={value:,.0f}BTC（{src}）"
    if item["metric_key"].endswith(".oi_usd_bybit"):
        lbl = extra.get("label") or ""
        return f"{lbl}.Bybit持仓OI(USD)={value/1e9:,.1f}十亿美元（{src}）"
    if item["metric_key"].endswith(".basis_bybit"):
        lbl = extra.get("label") or ""
        return f"{lbl}.Bybit基差={value:+.1f}bp（{src}）"
    if item["metric_key"].endswith(".opt_wall"):
        lbl = extra.get("label") or ""
        pw = extra.get("put_wall")
        put_txt = f"{pw:,.0f}" if isinstance(pw, (int, float)) else "N/A"
        return f"{lbl}.期权墙Call_OI最大={value:,.0f}USD，Put墙={put_txt}USD（{src}）"
    if item["metric_key"].endswith(".opt_skew"):
        lbl = extra.get("label") or ""
        return f"{lbl}.期权偏度(callIV−putIV)={value:+.4g}（{src}）"
    # 美股保证金债务（margin debt，杠杆资金总量，季度滞后）：美元 + 环比/同比（判杠杆扩张/收缩）
    if item["metric_key"] == "margin_debt":
        date = extra.get("observation_date") or ""
        qoq = extra.get("qoq")
        yoy = extra.get("yoy")
        qoq_txt = f"环比{qoq:+.1f}%" if isinstance(qoq, (int, float)) else "环比N/A"
        yoy_txt = f"同比{yoy:+.1f}%" if isinstance(yoy, (int, float)) else "同比N/A"
        date_txt = f"截至{date}" if date else ""
        return (
            f"美股保证金债务(margin_debt)={value/1e3:,.0f}十亿美元"
            f"（{qoq_txt}、{yoy_txt}，杠杆资金总量，{date_txt}，{src}，季度滞后）"
        )
    # 美股现货总市值（公开股权，季度滞后）：作美股杠杆/现货占比的分母
    if item["metric_key"] == "us_equity_mcap":
        date = extra.get("observation_date") or ""
        date_txt = f"截至{date}" if date else ""
        return f"美股总市值(us_equity_mcap)={value/1e3:,.0f}十亿美元（公开股权现货，{date_txt}，{src}，季度滞后）"
    # 黄金 ETF 持仓（SPDR GLD）：吨数 + 日流入/流出
    if item["metric_key"] == "gold.etf_holdings":
        as_of = (item.get("payload") or {}).get("as_of") or ""
        suffix = f"（截止{as_of}）" if as_of else ""
        return f"GOLD.ETF持仓={value:,.1f}吨{suffix}（{src}）"
    if item["metric_key"] == "gold.etf_holdings_chg":
        direction = "流入" if value > 0 else ("流出" if value < 0 else "持平")
        return f"GOLD.ETF日变化={value:+,.1f}吨（{direction}，{src}）"
    if item["metric_key"] == "crypto_fng":
        return f"Fear&Greed {value:.0f} {extra.get('classification') or ''}".strip()
    if item["metric_key"] == "next_move_bp":
        return f"期货隐含相对 EFFR {value:+.1f}bp → {extra.get('next_move')}"
    # EIA 周度石油库存（官方原油/汽油库存 + 周环比，累库偏空 / 去库偏多）
    if item["metric_key"] == "oil.crude_stocks":
        return f"美国原油商业库存={value:,.0f}千桶（{src}）"
    if item["metric_key"] == "oil.crude_stocks.chg":
        direction = "累库" if value > 0 else ("去库" if value < 0 else "持平")
        return f"美国原油库存周环比={value:+,.0f}千桶（{direction}，{src}）"
    if item["metric_key"] == "oil.gasoline_stocks":
        return f"美国汽油总库存={value:,.0f}千桶（{src}）"
    if item["metric_key"] == "oil.gasoline_stocks.chg":
        direction = "累库" if value > 0 else ("去库" if value < 0 else "持平")
        return f"美国汽油库存周环比={value:+,.0f}千桶（{direction}，{src}）"
    # CoinPaprika 加密全局（BTC 市占率=资金轮动 / 总市值 / 24h 量）
    if item["metric_key"] == "crypto.btc_dominance":
        return f"BTC市占率={value:.2f}%（{src}）"
    if item["metric_key"] == "crypto.total_mcap":
        return f"加密总市值={value/1e12:,.2f}万亿美元（{src}）"
    if item["metric_key"] == "crypto.vol_24h":
        return f"加密24h成交量={value/1e9:,.1f}十亿美元（{src}）"
    # Blockchain.info 链上基本面（哈希率=算力安全、难度，日度）
    if item["metric_key"] == "btc.hashrate":
        return f"BTC哈希率={value/1e6:,.1f} EH/s（{src}）"
    if item["metric_key"] == "btc.difficulty":
        return f"BTC挖矿难度={value/1e12:,.2f} T（{src}）"
    # CoinGecko 市场宽度（涨跌家数 + ETH 市占率 + 全市场 24h 涨跌）
    if item["metric_key"] == "crypto.advancers":
        return f"加密上涨家数={value:.0f}（头部250，{src}）"
    if item["metric_key"] == "crypto.decliners":
        return f"加密下跌家数={value:.0f}（头部250，{src}）"
    if item["metric_key"] == "crypto.eth_dominance":
        return f"ETH市占率={value:.2f}%（{src}）"
    if item["metric_key"] == "crypto.market_chg_24h":
        return f"加密全市场24h涨跌={value:+.2f}%（{src}）"
    # 美股基本面（FMP/Alpha Vantage）：label=标的，TTM 估值慢变量，仅背景/估值对照
    if item["metric_key"] == "eq.mktcap":
        lbl = extra.get("label") or ""
        return f"{lbl}.市值={value:,.2f}十亿美元（{src}）"
    if item["metric_key"] == "eq.pe":
        lbl = extra.get("label") or ""
        return f"{lbl}.市盈率PE={value:.2f}x（{src}）"
    if item["metric_key"] == "eq.peg":
        lbl = extra.get("label") or ""
        return f"{lbl}.PEG={value:.3f}（{src}）"
    if item["metric_key"] == "eq.net_margin":
        lbl = extra.get("label") or ""
        return f"{lbl}.净利率={value:.2f}%（{src}）"
    if item["metric_key"] == "eq.debt_to_equity":
        lbl = extra.get("label") or ""
        return f"{lbl}.负债股权比={value:.3f}（{src}）"
    if isinstance(value, float) and abs(value) >= 1000:
        text = f"{value:,.0f}"
    else:
        text = f"{value:.4g}"
    if unit in ("", "index"):
        return f"{item['metric_key']}={text}{_as_of_suffix(item)}（{src}）"
    return f"{item['metric_key']}={text}{unit}{_as_of_suffix(item)}（{src}）"


def _flow_usd(v: float) -> str:
    """资金流向金额格式化：万亿美元 / 十亿 / 百万，紧凑可读。"""
    a = abs(v)
    if a >= 1e12:
        return f"${v/1e12:,.2f}T"
    if a >= 1e9:
        return f"${v/1e9:,.1f}B"
    if a >= 1e6:
        return f"${v/1e6:,.1f}M"
    return f"${v:,.0f}"


def _flow_overview(layers: Dict[str, List[Dict[str, Any]]]) -> Optional[str]:
    """聚合跨层资金流向数据为单段速览，供八段式 [跨资产资金] 段直接引用。

    只做跨层交叉计算（杠杆/现货占比、品类净头寸方向、加密内部轮动、ETF 净流入），
    不再打任何外部源；任一字段缺失就标 N/A，绝不造数。
    """
    items = flatten(layers)

    def _find(key: str) -> Optional[Dict[str, Any]]:
        for it in items:
            if it.get("metric_key") == key and it.get("status") == "ok" and it.get("value") is not None:
                return it
        return None

    parts: List[str] = []

    # 1) 美股杠杆/现货占比 = 保证金债务 ÷ 公开股权总市值（FRED 同为百万美元）
    debt = _find("margin_debt")
    eq_mcap = _find("us_equity_mcap")
    if debt and eq_mcap:
        usd_debt = float(debt["value"]) * 1e6
        usd_eq = float(eq_mcap["value"]) * 1e6
        qoq = (debt.get("payload") or {}).get("qoq")
        qoq_txt = f"，环比{qoq:+.1f}%" if isinstance(qoq, (int, float)) else ""
        parts.append(
            f"美股杠杆/现货：保证金债务 {_flow_usd(usd_debt)} ÷ 总市值 {_flow_usd(usd_eq)}"
            f" = {usd_debt / usd_eq * 100:.2f}%{qoq_txt}"
        )
    else:
        parts.append("美股杠杆/现货：N/A（缺 margin_debt 或 us_equity_mcap）")

    # 2) 加密杠杆/现货占比 = Σ永续O/I(USD, Bybit) ÷ 加密总市值
    oi_rows = [it for it in items if it.get("metric_key", "").endswith(".oi_usd_bybit")
               and it.get("status") == "ok" and it.get("value") is not None]
    oi_total = sum(float(it["value"]) for it in oi_rows)
    mcap = _find("crypto.total_mcap")
    if mcap and oi_total > 0:
        names = "、".join(it["metric_key"].split(".")[0] for it in oi_rows)
        parts.append(
            f"加密杠杆/现货：{names}永续OI {_flow_usd(oi_total)} ÷ 加密总市值 {_flow_usd(float(mcap['value']))}"
            f" = {oi_total / float(mcap['value']) * 100:.3f}%"
        )
    else:
        parts.append("加密杠杆/现货：N/A（缺 *.oi_usd_bybit 或 crypto.total_mcap）")

    # 3) BTC 现货 ETF 净流入（加密增量资金）
    etf = _find("btc_etf_net")
    if etf:
        parts.append(f"BTC现货ETF净流入 {_flow_usd(float(etf['value']))}")
    else:
        parts.append("BTC现货ETF净流入：N/A")

    # 4) 加密内部资金轮动（BTC/ETH 市占率）
    btc_dom = _find("crypto.btc_dominance")
    eth_dom = _find("crypto.eth_dominance")
    if btc_dom and eth_dom:
        alt = max(0.0, 100.0 - (float(btc_dom["value"]) + float(eth_dom["value"])))
        parts.append(
            f"加密内部轮动：BTC市占 {float(btc_dom['value']):.2f}% / ETH市占 {float(eth_dom['value']):.2f}%"
            f" / 山寨+稳定币 {alt:.2f}%"
        )
    else:
        parts.append("加密内部轮动：N/A（缺 crypto.btc_dominance 或 crypto.eth_dominance）")

    # 5) 品类资金流向（COT 投机净头寸方向）
    cot_rows = [it for it in items if it.get("metric_key", "").endswith(".cot_noncomm_net")
                and it.get("status") == "ok" and it.get("value") is not None]
    if cot_rows:
        bulls = sorted((it for it in cot_rows if float(it["value"]) > 0),
                       key=lambda it: float(it["value"]), reverse=True)
        bears = sorted((it for it in cot_rows if float(it["value"]) < 0),
                       key=lambda it: float(it["value"]))

        def _desc(it: Dict[str, Any]) -> str:
            v = float(it["value"])
            lbl = (it.get("payload") or {}).get("label") or it["metric_key"].split(".")[0]
            side = "净多" if v > 0 else "净空"
            return f"{lbl}{side}{abs(v):,.0f}张"

        segs = []
        if bulls:
            segs.append("最强净多 " + "、".join(_desc(it) for it in bulls[:3]))
        if bears:
            segs.append("最强净空 " + "、".join(_desc(it) for it in bears[:3]))
        if segs:
            parts.append("品类资金流向(COT投机净头寸)：" + "；".join(segs))
        else:
            parts.append("品类资金流向(COT)：N/A（无正负净头寸）")
    else:
        parts.append("品类资金流向(COT)：N/A")

    return "；".join(parts)


def render_prompt_block(layers: Dict[str, List[Dict[str, Any]]]) -> str:
    titles = {
        "structure": "盘面结构",
        "sentiment": "舆情情绪",
        "fed": "美联储加息预期",
        "fred": "官方宏观指标(FRED)",
        "flow": "资金流向",
        "liquidation": "爆仓",
        "cot": "COT持仓(CFTC)",
        "gold_etf": "黄金ETF持仓(SPDR)",
        "options": "期权市场(Deribit)",
        "eia": "原油库存(EIA)",
        "rotation": "资金轮动(CoinPaprika)",
        "onchain": "链上基本面(Blockchain.com)",
        "breadth": "市场宽度(CoinGecko)",
        "equity": "美股基本面(FMP/AV)",
    }
    lines = ["── 非新闻数据层（每项标注来源；[周度]/[日度] 者仅作背景/趋势交叉验证，勿当即时信号）──"]
    any_ok = False
    for key, title in titles.items():
        items = layers.get(key) or []
        ok_items = [item for item in items if item.get("status") == "ok" and item.get("value") is not None]
        if not ok_items:
            lines.append(f"{title}: 当前不可用，不得臆造")
            continue
        any_ok = True
        # 全量展示（不再截断）：structure/fred/flow 各项都是"跨资产资金"段的可用素材，
        # 截断会把 XAU/ETH 费率、OI、多空比、TIPS 实际利率等关键项挡在 prompt 之外
        parts = []
        for item in ok_items:
            text = _fmt(item)
            fresh = (item.get("payload") or {}).get("freshness") or "realtime"
            if fresh in _FRESH_SUFFIX:
                text += f"（{_FRESH_SUFFIX[fresh]}）"
            parts.append(text)
        lines.append(f"{title}: " + " | ".join(parts))
    if not any_ok:
        return "── 非新闻数据层 ──\n全部外部源不可用，仅基于新闻与已有快照判断，禁止补造宏观数字。"
    flow_overview = _flow_overview(layers)
    if flow_overview:
        lines.append("── 跨资产资金速览（聚合，供 [跨资产资金] 段直接引用；底层源不稳定或缺失(N/A)时弱化、勿盲采作强依据）──")
        lines.append(flow_overview)
    lines.append("以上仅作交叉验证；缺失字段标 N/A，不得补造；标「周度/日度」的数据存在滞后，只能作背景趋势、勿作即时方向依据。最终仍输出 20 字段 JSON。")
    return "\n".join(lines)


def build_context(get: HttpGet = _default_get, persist: bool = True) -> Dict[str, Any]:
    layers = collect_layers(get)
    written = persist_layers(layers) if persist else 0
    ok = sum(1 for item in flatten(layers) if item.get("status") == "ok" and item.get("value") is not None)
    total = len(flatten(layers))
    status = "ok" if ok == total and total else ("partial" if ok else "unavailable")
    return {
        "status": status,
        "ok": ok,
        "total": total,
        "written": written,
        "layers": layers,
        "summary": render_prompt_block(layers),
        "ts": _now(),
    }


def latest_snapshot(limit: int = 0, connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """返回最新一轮完整快照（同 MAX(ts) 的全部 metric_key）；limit>0 回退为最近 N 条。

    此前 ORDER BY id DESC LIMIT n 在单轮 build 行数 > n 时会截断先写入的层
    （structure/sentiment/fed/fred/flow/liquidation/coinglass 被 cot/equity 挤掉），
    导致 flows panel 多个 section 空数据；改为按最新 ts 整轮返回，覆盖全部层。
    """
    owned = connection is None
    conn = connection or db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macro_snapshots'"
        ).fetchone()
        if exists is None:
            return []
        if limit and limit > 0:
            rows = conn.execute(
                """SELECT ts, category, source, metric_key, value, unit, status, payload
                   FROM macro_snapshots
                   ORDER BY id DESC
                   LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ts, category, source, metric_key, value, unit, status, payload
                   FROM macro_snapshots
                   WHERE ts = (SELECT MAX(ts) FROM macro_snapshots)
                   ORDER BY id""",
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            out.append(item)
        return out
    finally:
        if owned:
            conn.close()
