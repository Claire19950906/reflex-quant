#!/usr/bin/env python3
"""行情数据源路由（单一事实来源）—— 把每个交易标的绑定到“真实存在该合约”的数据源。

背景（全盘扫描结论）：
  - 币安永续 fapi 只上市加密币（BTCUSDT/ETHUSDT/SOLUSDT…），**没有** XAUUSDT / CLUSDT
    （原油）/ NVDAUSDT 等美股、贵金属合约。此前把这些映射到币安 fapi，每次请求必然 400，
    再被“静默兜底”到别的币种/造假 K 线 —— 这是错价的根因。
  - OKX 永续上市黄金 XAU-USDT-SWAP、白银 XAG-USDT-SWAP（实测可直连），但**没有**原油。
  - 原油 WTI 无免费永续合约 → 只能用国内现货价（新浪/东财），无 funding/oi/K线。

路由表 ASSET_ROUTE（唯一事实来源，market_snapshot / macro_context / api_server /
engine.prices 统一切到这里，杜绝各自硬编码）：
  market 取值：
    "binance_fapi" 币安永续（走代理）
    "okx_swap"     OKX 永续（国内直连）
    "domestic_spot" 国内现货（新浪/东财）
    "yahoo"        Yahoo Finance（美股，币安无美股合约）
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    _HAS_REQUESTS = False

import source_status
import config

# ── 资产 → (market, symbol/instId) ─────────────────────────────────────────
# symbol 缺失（None）表示该资产在此 market 无永续合约（如原油无 OKX、美股无币安）。
ASSET_ROUTE: Dict[str, Dict[str, str]] = {
    # 加密 —— 币安永续 fapi（真实存在）
    "BTC":  {"market": "binance_fapi", "symbol": "BTCUSDT"},
    "ETH":  {"market": "binance_fapi", "symbol": "ETHUSDT"},
    "SOL":  {"market": "binance_fapi", "symbol": "SOLUSDT"},
    # 贵金属 —— OKX 永续（真实存在，国内直连）
    "XAU":  {"market": "okx_swap", "symbol": "XAU-USDT-SWAP"},
    "GOLD": {"market": "okx_swap", "symbol": "XAU-USDT-SWAP"},
    "XAG":  {"market": "okx_swap", "symbol": "XAG-USDT-SWAP"},
    # 原油 —— 无免费永续合约，走国内现货
    "WTI":  {"market": "domestic_spot", "symbol": None},
    "OIL":  {"market": "domestic_spot", "symbol": None},
    # 美股 —— 币安无美股永续合约，走 Yahoo Finance
    "NVDA": {"market": "yahoo", "symbol": "NVDA"},
    "TSLA": {"market": "yahoo", "symbol": "TSLA"},
    "AAPL": {"market": "yahoo", "symbol": "AAPL"},
    "MSTR": {"market": "yahoo", "symbol": "MSTR"},
    "MSFT": {"market": "yahoo", "symbol": "MSFT"},
    "AMZN": {"market": "yahoo", "symbol": "AMZN"},
    "GOOGL": {"market": "yahoo", "symbol": "GOOGL"},
    "COIN": {"market": "yahoo", "symbol": "COIN"},
    "PLTR": {"market": "yahoo", "symbol": "PLTR"},
    "META": {"market": "yahoo", "symbol": "META"},
    "SMCI": {"market": "yahoo", "symbol": "SMCI"},
    "MARA": {"market": "yahoo", "symbol": "MARA"},
    "RIOT": {"market": "yahoo", "symbol": "RIOT"},
    # 海力士 cap（v??）：归因页 K 线 + 全盘 movers 都新增韩国海力士与美国海力士监控
    #   - SKHYNIX   → 000660.KS（KOSPI），韩股海力士本体
    #   - US_HYNIX  → MU（Micron），中文圈所称"美国海力士"（与 SKHYNIX 同行业对照）
    #   - MU        → 与 US_HYNIX 同物（保留独立代码，方便 paper-trading 等按 USD target 用）
    "SKHYNIX":  {"market": "yahoo", "symbol": "000660.KS"},
    "US_HYNIX": {"market": "yahoo", "symbol": "MU"},
    "MU":       {"market": "yahoo", "symbol": "MU"},
}


def route(asset_id: str) -> Optional[Dict[str, str]]:
    """资产代码 → 路由条目。未知资产返回 None（调用方应标 unavailable，绝不兜底到别币种）。"""
    key = (asset_id or "").upper()
    return ASSET_ROUTE.get(key)


def market_of(asset_id: str) -> str:
    r = route(asset_id)
    return r["market"] if r else "unknown"


def symbol_of(asset_id: str) -> Optional[str]:
    r = route(asset_id)
    return r["symbol"] if r else None


# ── OKX 公共行情（默认直连；MARKET_USE_PROXY=true 时走 env 代理绕过 SNI 阻断）──
_OKX_BASE = "https://www.okx.com"
_okx_lock = threading.Lock()
_okx_session: Any = None
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# ── OKX 熔断：连续失败后短时间短路，避免网络阻断（如 TLS 被重置）时每请求等满超时 ──
# 根因诊断：www.okx.com:443 在 TLS 握手被 GFW SNI 阻断（SSL_ERROR_SYSCALL），且非全局 VPN 未覆盖。
# 配好代理并设 config.MARKET_USE_PROXY=true 可绕开；若不设，熔断可避免每请求等满 timeout。
_okx_breaker_lock = threading.Lock()
_okx_fail_streak = 0
_okx_cooldown_until = 0.0            # 熔断打开（直接短路返回 None）的截止时间戳

_OKX_BREAKER_AFTER = 2               # 连续失败几次后打开熔断
_OKX_BREAKER_COOLDOWN_S = 60.0       # 熔断冷却时长（秒）
_OKX_TIMEOUT = 3.0                   # connect/read 共用秒数；OKX TLS 被阻断时快速失败，靠熔断+国内兜底


# ── Yahoo Finance v8 chart 公共行情（无需 API key、国内直连、免代理）───────────────
# 用途：OKX SNI 阻断且无代理时，作为 XAU/XAG/WTI/BTC/ETH/SOL 等的「免费真实 K 线」二级数据源。
# 限制：15-20 分钟延迟；非 24h（CME 交易时段；周末/节假日留空）；合约价 vs 现货有 ~$1-3 基差。
# 实测：v2026.10.05 query1.finance.yahoo.com 直连成功率 ~50%（SNI 间歇+429 限速），
#       内置 3s 限速 + 指数退避后实际成功率 >> 90%。
_YAHOO_BASE = "https://query1.finance.yahoo.com"
_YAHOO_CHART_PATH = "/v8/finance/chart"
_yahoo_lock = threading.Lock()
_yahoo_session: Any = None
_yahoo_last_call_ts = 0.0             # 上次调用时间戳（用于跨请求最小间隔限速）
_yahoo_cooldown_until = 0.0           # 熔断打开的截止时间戳（连续失败 + 429）
_yahoo_fail_streak = 0
_YAHOO_MIN_GAP_S = 3.0               # 同一进程两次 Yahoo 调用最小间隔（秒）—— 避免触发 429 限速
_YAHOO_BREAKER_AFTER = 3              # 连续失败几次后打开熔断
_YAHOO_BREAKER_COOLDOWN_S = 90.0      # 熔断冷却时长（秒）
_YAHOO_TIMEOUT = 10.0                 # connect/read 共用秒数
_YAHOO_RETRY_MAX = 4                  # 单次调用内重试次数

# v307: query2 是 query1 的等价备用域，IP 被 query1 限流(429)时轮换过去，
#   实测 429 时换域可直接成功（query2 不共享 query1 的配额）。
_YAHOO_BASES: List[str] = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]
# v307: 被限流时各域轮换尝试的次数上限（而不是 _YAHOO_RETRY_MAX × 域名数）。
#   实测 IP 被 Yahoo 整体限流时 query1/query2 一律 429，等待再久也无用 →
#   快速失败让调用方走下一级兜底（本地 kline_bars），避免一个请求阻塞 3 分钟。
_YAHOO_429_BUDGET = 2                 # 最多撞 2 次 429 就放弃（换域各试一次）
_YAHOO_429_COOLDOWN_S = 2.0           # 429 之间的退避（秒）—— 短退避，仅给瞬时限流留机会

# 资产 → Yahoo symbol 映射（v2026.10.05）
YAHOO_SYMBOL_FOR_ASSET: Dict[str, str] = {
    "XAU":   "GC=F",       # COMEX 黄金期货（最具代表性的"美元标黄金"连续合约）
    "GOLD":  "GC=F",
    "XAG":   "SI=F",       # COMEX 白银期货
    "SILVER":"SI=F",
    "WTI":   "CL=F",       # WTI 原油期货（NYMEX）
    "OIL":   "CL=F",
    "HG":    "HG=F",       # COMEX 铜期货
    "COPPER":"HG=F",
    "BTC":   "BTC-USD",    # 现货基准
    "ETH":   "ETH-USD",
    "SOL":   "SOL-USD",
    "DXY":   "DX-Y.NYB",   # 美元指数（ICE）
    "VIX":   "^VIX",       # CBOE VIX
    # 海力士 cap（v??）：归因页 K 线 + 全盘 movers 都新增韩国/美国海力士监控
    "SKHYNIX":  "000660.KS",  # KOSPI SK Hynix（Yahoo Finance 支持 .KS 后缀）
    "US_HYNIX": "MU",         # Micron（中文圈"美国海力士"）
    "MU":       "MU",
}

# OKX bar → Yahoo interval 映射（Yahoo 支持：1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1wk/1mo/3mo）
_OKX_TO_YAHOO_INTERVAL = {
    "1m": "1m", "3m": "5m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "60m", "1h": "60m", "4h": "60m", "1D": "1d", "1d": "1d", "1W": "1wk", "1w": "1wk",
}


def _yahoo() -> Any:
    """Yahoo Finance 单例 Session（直连，无代理；trust_env=False 不走 env HTTP_PROXY）。"""
    global _yahoo_session
    if _yahoo_session is None:
        with _yahoo_lock:
            if _yahoo_session is None:
                s = requests.Session()
                s.trust_env = False  # 直连 Yahoo；不走 env HTTP_PROXY
                s.headers.update({
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Connection": "close",
                })
                _yahoo_session = s
    return _yahoo_session


def _yahoo_throttle() -> None:
    """跨请求最小间隔限速（Yahoo 实测 429 频度高；同进程两次调用至少间隔 _YAHOO_MIN_GAP_S）。"""
    global _yahoo_last_call_ts
    with _yahoo_lock:
        gap = time.time() - _yahoo_last_call_ts
        if gap < _YAHOO_MIN_GAP_S:
            time.sleep(_YAHOO_MIN_GAP_S - gap)
        _yahoo_last_call_ts = time.time()


def _yahoo_get_json(params: Dict[str, str]) -> Optional[Any]:
    """Yahoo chart GET：熔断检查 + 限速 + 重试 + 报告 source_status。"""
    global _yahoo_fail_streak, _yahoo_cooldown_until
    if not _HAS_REQUESTS:
        return None
    now = time.time()
    with _yahoo_lock:
        if now < _yahoo_cooldown_until:
            return None
    _yahoo_throttle()
    last_exc: Optional[Exception] = None
    last_status = 0
    j: Optional[Any] = None
    ok = False
    total_attempts = _YAHOO_RETRY_MAX * len(_YAHOO_BASES)
    n_429 = 0
    for attempt in range(total_attempts):
        # v307: 每次重试轮换域名（query1 被 429 时换 query2）
        base = _YAHOO_BASES[attempt % len(_YAHOO_BASES)]
        try:
            r = _yahoo().get(base + _YAHOO_CHART_PATH, params=params, timeout=_YAHOO_TIMEOUT)
            last_status = r.status_code
            if r.status_code == 200:
                try:
                    j = r.json()
                    err = (((j or {}).get("chart") or {}).get("error") or {}) if j else {}
                    if isinstance(err, dict) and err.get("code"):
                        ok = False
                    else:
                        ok = True
                except (ValueError, json.JSONDecodeError):
                    ok = False
            elif r.status_code in (429, 403, 503):
                n_429 += 1
                last_exc = RuntimeError(f"HTTP {r.status_code} @ {base}")
                print(f"[YAHOO-429] {r.status_code} from {base} (第 {n_429} 次限流/"
                      f"预算 {_YAHOO_429_BUDGET})", flush=True)
                # v307: 限流是 IP 级问题，继续撞墙无意义 —— 耗尽预算就快速失败走下一级兜底
                if n_429 >= _YAHOO_429_BUDGET:
                    print(f"[YAHOO-429] 限流预算耗尽，快速失败（交由调用方降级）", flush=True)
                    break
                if attempt < total_attempts - 1:
                    time.sleep(_YAHOO_429_COOLDOWN_S)
                    continue
            else:
                break  # 业务错误（404 等）不重试
        except Exception as _e:
            last_exc = _e
            last_status = 0
            if attempt < total_attempts - 1:
                time.sleep(0.6 * (2 ** (attempt % 3)))  # 0.6/1.2/2.4s 退避
    with _yahoo_lock:
        if ok:
            _yahoo_fail_streak = 0
            _yahoo_cooldown_until = 0.0
        else:
            _yahoo_fail_streak += 1
            if _yahoo_fail_streak >= _YAHOO_BREAKER_AFTER:
                _yahoo_cooldown_until = now + _YAHOO_BREAKER_COOLDOWN_S
            if _yahoo_fail_streak == _YAHOO_BREAKER_AFTER and last_exc:
                print(
                    f"[YAHOO-BREAKER] 连续失败 {_yahoo_fail_streak} 次（params={params}），"
                    f"熔断 {_YAHOO_BREAKER_COOLDOWN_S}s | err={type(last_exc).__name__}: "
                    f"{str(last_exc)[:80]} status={last_status}",
                    flush=True,
                )
    return j if ok else None


def yahoo_interval(okx_bar: str) -> str:
    """OKX bar (1m/3m/5m/15m/1H/4H/1D) → Yahoo interval。"""
    return _OKX_TO_YAHOO_INTERVAL.get(okx_bar, "60m")


def fetch_yahoo_candles(yahoo_symbol: str, interval: str = "60m",
                        range_or_period: str = "5d",
                        period1: Optional[int] = None,
                        period2: Optional[int] = None,
                        limit: Optional[int] = None) -> Optional[list]:
    """Yahoo Finance v8 chart K 线 → [[ts_ms, o, h, l, c, vol], ...] 升序。

    Args:
        yahoo_symbol:    Yahoo 标的（'GC=F' / 'BTC-USD' / '^VIX'；'^' 内部转义为 %5E）。
        interval:        '1m'/'5m'/'15m'/'30m'/'60m'/'90m'/'1h'/'1d'/'1wk'/'1mo'/'3mo'
        range_or_period: range 关键字（'1d'/'5d'/'1mo'/'3mo'/'6mo'/'1y'/'2y'/'5y'/'10y'/'ytd'/'max'）
        period1/period2: 自定义时间窗（unix 秒；两者都给时覆盖 range）
        limit:           只返回最后 N 根（None=全部；Yahoo 单次最多 ~15000 根 1h）

    Returns:
        升序 [[ts_ms, o, h, l, c, vol], ...]；失败返回 None。

    v2026.10.05 新增：作为 OKX SNI 阻断时的 XAU/XAG/WTI/BTC/ETH/SOL 免费真实 K 线源。
    """
    safe_symbol = yahoo_symbol.replace("^", "%5E")
    params: Dict[str, str] = {"interval": interval, "symbol": safe_symbol}
    if period1 is not None and period2 is not None:
        params["period1"] = str(int(period1))
        params["period2"] = str(int(period2))
    else:
        params["range"] = range_or_period
    j = _yahoo_get_json(params)
    sid = f"yahoo:{yahoo_symbol}:{interval}"
    if j is None:
        source_status.report(sid, False, error="请求失败/被限速/SNI阻断")
        return None
    try:
        result = ((j.get("chart") or {}).get("result") or [])
        if not result:
            source_status.report(sid, False, error="chart.result 为空")
            return None
        r = result[0]
        ts_list = r.get("timestamp") or []
        quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        oo = quote.get("open") or []
        hh = quote.get("high") or []
        ll = quote.get("low") or []
        cc = quote.get("close") or []
        vv = quote.get("volume") or []
    except (KeyError, IndexError, TypeError) as e:
        source_status.report(sid, False, error=f"JSON 结构异常: {type(e).__name__}")
        return None
    rows: list = []
    n_dropped = 0
    for i, t in enumerate(ts_list):
        try:
            o, h, l, c = oo[i], hh[i], ll[i], cc[i]
        except (IndexError, TypeError):
            n_dropped += 1
            continue
        # None → 非交易时段（CME 闭市 / 节假日）→ 丢弃（避免污染 verifier）
        if c is None or o is None or h is None or l is None or c <= 0:
            n_dropped += 1
            continue
        try:
            v = float(vv[i]) if i < len(vv) and vv[i] is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
        try:
            rows.append([float(int(t) * 1000), float(o), float(h), float(l), float(c), v])
        except (TypeError, ValueError):
            n_dropped += 1
            continue
    if len(rows) < 2:
        source_status.report(sid, False, error=f"有效 bar <2（raw={len(ts_list)}, dropped={n_dropped}）")
        return None
    if limit is not None and len(rows) > limit:
        rows = rows[-limit:]
    source_status.report(sid, True, detail=f"bars={len(rows)} raw={len(ts_list)} dropped={n_dropped}")
    return rows


def fetch_asset_candles_with_yahoo_fallback(asset_id: str, okx_bar: str,
                                            limit: int = 96) -> Optional[list]:
    """XAU/XAG/WTI 等 okx_swap 资产的「OKX 优先 → Yahoo 兜底」统一 K 线获取。

    用法：替换所有 call sites 中直接的 `quote_sources.fetch_okx_candles(sym, okx_bar, limit)`。
    OKX SNI 阻断时仍能拿到真实 K 线（带合约价 vs 现货的小基差 + 15-20min 延迟）。

    路由规则（v2026.10.05）：
      1) OKX 永续 fetch_okx_candles（实时优先；OKX 恢复后零延迟用真值）
      2) OKX 失败 → Yahoo Finance 同品种期货合约（兜底）
      3) 都失败 → None（不合成扁平假 K 线——保留 verifier 诚实性）
    """
    rt = route(asset_id)
    if not rt:
        return None
    sym = rt.get("symbol")
    if not sym:
        return None
    # 1) OKX 优先
    rows = fetch_okx_candles(sym, okx_bar, limit)
    if rows:
        return rows
    # 2) Yahoo 兜底（仅当 OKX 真失败，且 Yahoo symbol 已知）
    yahoo_sym = YAHOO_SYMBOL_FOR_ASSET.get(asset_id.upper())
    if not yahoo_sym:
        return None
    iv = yahoo_interval(okx_bar)
    if iv in ("1d", "1wk", "1mo", "3mo"):
        rng = "6mo"
    elif iv in ("60m", "90m"):
        rng = "1mo" if limit <= 96 else "2y"
    else:  # 1m/5m/15m/30m
        rng = "5d"
    try:
        rows = fetch_yahoo_candles(yahoo_sym, interval=iv, range_or_period=rng, limit=limit)
    except Exception:
        rows = None
    return rows


# ── Naver Finance（韩国本土源，KRX 韩股唯一稳定的免费分钟线来源）───────────────
# 背景（v307）：Yahoo 对 000660.KS 持续 429 限流，腾讯/东财/新浪/商用源(Finnhub 403、
#   Polygon 404、FMP 404、AlphaVantage 空) 全部拿不到韩股分钟线 → SKHYNIX 量能异动一条都产不出。
#   实测只有 Naver 能给 KRX 分钟数据（单日 325 根 1m），且国内直连无需 key。
# 响应格式（JS 数组包在 header 里）:
#   [["날짜","시가","고가","저가","종가","거래량","외국인소진율"],
#    ["202610221421", null, null, null, 1212000, 1878163, null], ...]
#   注意：分钟线的 open/high/low 为 null，只有 close + 累计成交量(거래량)。
#   因此：OHLC 用相邻 close 补齐(近似)，成交量按相邻累计量差分成每分钟量。
#   量能识别(_vol_ratio / vol_z)只用 volume 序列 → 差分后完全可用。
_NAVER_BASE = "https://api.finance.naver.com/siseJson.naver"
_naver_lock = threading.Lock()
_naver_session: Any = None
_NAVER_TIMEOUT = 8.0
_NAVER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 资产 → Naver/KRX 代码（6 位数字，无前后缀）
NAVER_SYMBOL_FOR_ASSET: Dict[str, str] = {
    "SKHYNIX": "000660",
    "SAMSUNG": "005930",
    "HYNIX": "000660",
}


def _naver() -> Any:
    global _naver_session
    with _naver_lock:
        if _naver_session is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": _NAVER_UA,
                "Referer": "https://m.stock.naver.com/",
                "Accept": "*/*",
            })
            _naver_session = s
        return _naver_session


def _naver_parse(body: str) -> list:
    """解析 Naver 的 JS 数组包裹响应 → list[list]（丢掉表头行）。"""
    import json as _json
    txt = (body or "").replace("\n", "").replace("\t", "").strip()
    if txt.startswith("callback"):
        txt = txt[txt.find("(") + 1: txt.rfind(")")]
    if "|" in txt:
        txt = txt.split("|", 1)[1]
    txt = txt.replace("'", '"')
    try:
        rows = _json.loads(txt)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, list) and len(r) >= 6 and r[0] != "날짜"]


def fetch_naver_candles(symbol: str, timeframe: str = "minute",
                        days_back: int = 0, limit: int = 288) -> Optional[list]:
    """Naver/KRX K 线 → [[ts_ms, o, h, l, c, v], ...] 升序（带重试）。

    timeframe: "minute"（1 分钟粒度）或 "day"。
    分钟线只有 close + 累计量 → OHLC 用 close 近似，volume 做差分。

    v307：本机 SSL 层有间歇性 UNEXPECTED_EOF（冷 session 首次请求最易中招，
    见 probe_sources.py 顶部的方法论注释）→ 必须重试，否则会被误判成「源不可用」。
    """
    if not symbol:
        return None
    last_exc = ""
    for attempt in range(3):
        try:
            return _fetch_naver_candles_once(symbol, timeframe, days_back, limit)
        except Exception as e:  # noqa: BLE001
            last_exc = f"{type(e).__name__}: {str(e)[:60]}"
            time.sleep(1.5 * (attempt + 1))
    print(f"[naver] {symbol} {timeframe} 连续 3 次失败 → {last_exc}", flush=True)
    return None


def _fetch_naver_candles_once(symbol: str, timeframe: str, days_back: int,
                              limit: int) -> Optional[list]:
    if not symbol:
        return None
    try:
        sess = _naver()
        now = int(time.time())
        tm = time.localtime(now)
        if timeframe == "day":
            s = time.strftime("%Y%m%d", time.localtime(now - 60 * 86400 * max(days_back, 1)))
            e = time.strftime("%Y%m%d", tm)
        else:
            start_day = now - max(days_back, 0) * 86400
            s = time.strftime("%Y%m%d", time.localtime(start_day))
            e = time.strftime("%Y%m%d", tm)
        params = {"symbol": symbol, "requestType": "1", "startTime": s,
                  "endTime": e, "timeframe": timeframe}
        r = sess.get(_NAVER_BASE, params=params, timeout=_NAVER_TIMEOUT)
        if r.status_code != 200:
            return None
        raw = _naver_parse(r.text)
        if len(raw) < 2:
            return None
        raw_bars: List[tuple] = []
        for row in raw:
            try:
                ts_s = str(row[0])
                close = float(row[4])
                cum = float(row[5]) if row[5] is not None else 0.0
            except (TypeError, ValueError, IndexError):
                continue
            if close <= 0:
                continue
            # 时间戳: 分钟线 "202610221421" → 2026-10-22 14:21:00 KST(UTC+9)
            try:
                if len(ts_s) > 8:
                    base = time.mktime(time.strptime(ts_s[:8], "%Y%m%d")) - 9 * 3600
                    hh, mm = int(ts_s[8:10]), int(ts_s[10:12])
                    ts = base + hh * 3600 + mm * 60
                else:
                    ts = time.mktime(time.strptime(ts_s, "%Y%m%d"))
            except Exception:
                continue
            # 日线有真实 OHLC；分钟线用 close 近似（量能识别不依赖 OHLC）
            o = float(row[1]) if row[1] not in (None, 0) else close
            h = float(row[2]) if row[2] not in (None, 0) else close
            lo = float(row[3]) if row[3] not in (None, 0) else close
            raw_bars.append((int(ts * 1000), o, h, lo, close, cum, ts_s[:8]))
        # Naver 返回「最新在前」→ 必须先按时间升序，否则相邻差分方向是反的。
        raw_bars.sort(key=lambda x: x[0])
        bars: List[List[Any]] = []
        prev_cum = None
        prev_day = ""
        for ts_ms, o, h, lo, close, cum, day in raw_bars:
            if timeframe == "day":
                v = cum                       # 日线本身即当日量，不是累计量
            elif prev_cum is None or day != prev_day:
                v = cum                       # 首根 / 换日首根：累计量本身就是该分钟量
            else:
                # 累计量每日开盘归零 → 跨日那根不能做差分，否则算出巨额负数。
                v = (cum - prev_cum) if cum >= prev_cum else cum
            bars.append([ts_ms, o, h, lo, close, max(v, 0.0)])
            prev_cum, prev_day = cum, day
        if limit and len(bars) > limit:
            bars = bars[-limit:]
        return bars or None
    except Exception as e:
        print(f"[naver] {symbol} {timeframe} 单次尝试失败: {type(e).__name__}: {str(e)[:70]}", flush=True)
        raise


def _okx() -> Any:
    global _okx_session
    if _okx_session is None:
        with _okx_lock:
            if _okx_session is None:
                s = requests.Session()
                s.trust_env = config.MARKET_USE_PROXY  # 默认直连；配好代理设 true 走 env HTTP_PROXY
                # Connection: close —— OKX 网络偶发时，Session keep-alive 连接池会复用到已被
                # 静默关闭的死连接，导致 read 无限挂起（timeout 失效）。关闭复用后等价裸请求，
                # 每次独立建连、有界快速失败。
                s.headers.update({"User-Agent": _UA, "Accept-Encoding": "identity", "Connection": "close"})
                # 不挂 Retry 适配器：OKX 网络偶发 read 挂起时，Retry 会把超时放大重试
                # 表现为“卡住”；重试/短路一律交给 _okx_get 上方的熔断机制处理。
                _okx_session = s
    return _okx_session


def _okx_get(path: str, params: Dict[str, str], timeout: Any = _OKX_TIMEOUT) -> Optional[Any]:
    global _okx_fail_streak, _okx_cooldown_until
    if not _HAS_REQUESTS:
        return None
    now = time.time()
    with _okx_breaker_lock:
        if now < _okx_cooldown_until:
            return None  # 熔断中：直接短路，避免再次等满超时
    # ── P0 修复：SSL/网络抖动重试 2 次（指数退避 0.3/0.6s） ──
    # 解决 OKX TLS EOF 间歇性失败，1.5h 没新 XAU K 线的根因
    last_exc = None
    j = None
    ok = False
    for attempt in range(3):
        try:
            r = _okx().get(_OKX_BASE + path, params=params, timeout=timeout)
            j = r.json()
            ok = j.get("code") == "0"
            if ok:
                last_exc = None
                break
            # OKX 业务错误（非网络）不重试
            if j.get("code") not in (None, "", "0"):
                break
        except Exception as _e:
            last_exc = _e
            j = None
            ok = False
            if attempt < 2:
                time.sleep(0.3 * (2 ** attempt))
    with _okx_breaker_lock:
        if ok:
            _okx_fail_streak = 0
            _okx_cooldown_until = 0.0
        else:
            _okx_fail_streak += 1
            if _okx_fail_streak >= _OKX_BREAKER_AFTER:
                _okx_cooldown_until = now + _OKX_BREAKER_COOLDOWN_S
            if last_exc:
                # 仅打首次熔断的错，避免噪音
                if _okx_fail_streak == _OKX_BREAKER_AFTER:
                    print(
                        f"[OKX-BREAKER] 连续失败 {_okx_fail_streak} 次（{path}），"
                        f"熔断 {_OKX_BREAKER_COOLDOWN_S}s | err={type(last_exc).__name__}: {str(last_exc)[:80]}",
                        flush=True,
                    )
    if not ok:
        return None
    return j.get("data")


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 币安 interval → OKX bar 映射
BINANCE_TO_OKX_BAR = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D",
}


def okx_bar(interval: str) -> str:
    return BINANCE_TO_OKX_BAR.get(interval, "1H")


def fetch_okx_ticker(inst_id: str) -> Optional[Dict[str, Any]]:
    """OKX 永续 ticker → {last, percentage, has_change, info}，失败 None。

    v176：对 XAU-USDT-SWAP / XAG-USDT-SWAP 加 ticker 兜底（如果主 ticker 失败，回退到国内现货价）
    → 避免因 OKX timeout 让 dashboard 一直显示"XAU 获取失败"。
    v176.7：WTI-USDT-SWAP 在 OKX 实际不存在（原油没有永续合约），直接返回 None 不报 error。
    """
    # v176.7 P0-fix：WTI 短路
    if inst_id == "WTI-USDT-SWAP":
        return None
    data = _okx_get("/api/v5/market/ticker", {"instId": inst_id})
    sid = f"okx_swap:{inst_id}"
    if not data:
        source_status.report(sid, False, error="ticker 请求失败/为空")
        # ── P1-12 兜底：XAU/XAG ticker 失败 → 用国内现货价合成 ticker ──
        if inst_id in ("XAU-USDT-SWAP", "XAG-USDT-SWAP"):
            try:
                spot = _fetch_xau_xag_spot_price(inst_id)
                if spot is not None:
                    source_status.report(sid, True, detail=f"fallback-spot-ticker-{spot}")
                    return {
                        "last": float(spot),
                        "percentage": None,
                        "has_change": False,
                        "info": {"last": str(spot), "source": "domestic-spot-fallback"},
                    }
            except Exception:
                pass
        return None
    t = data[0]
    last = _f(t.get("last"))
    open24h = _f(t.get("open24h"))
    pct = None
    if last is not None and open24h:
        pct = (last - open24h) / open24h * 100.0
    source_status.report(sid, True, detail=f"last={last}")
    return {"last": last, "percentage": pct, "has_change": pct is not None, "info": t}


def _fetch_xau_xag_spot_price(inst_id: str) -> Optional[float]:
    """v176 单点价兜底：OKX ticker 失败时取一次国内现货 XAU/XAG 当前价。

    用于 ticker / 推理 / 行情台共用兜底。失败 None。
    """
    try:
        sym = "XAU" if inst_id.startswith("XAU") else "XAG"
        try:
            from engine.prices import _fetch_gold_price_domestic  # type: ignore
            return _fetch_gold_price_domestic()
        except Exception:
            pass
        # 退一步：新浪直拉
        import urllib.request
        code = "hf_XAU" if sym == "XAU" else "hf_XAG"
        req = urllib.request.Request(
            f"https://hq.sinajs.cn/list={code}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            txt = resp.read().decode("gbk", errors="replace")
        # 字段 = name, open, prev_close, last, ...
        parts = txt.split('"')[1].split(",")
        last = float(parts[3])
        return last
    except Exception:
        return None


def fetch_okx_candles(inst_id: str, bar: str, limit: int = 96,
                     after: Optional[str] = None) -> Optional[list]:
    """OKX 永续 K 线 → [[ts_ms, open, high, low, close, volume], ...]（升序）。失败 None。

    v176.7：WTI-USDT-SWAP 在 OKX 实际不存在，直接返回 None 不报 error。
    v2026.10.05: 可选 after=<ts_ms-1> 用于历史回填分页（OKX 返回 latest→oldest，
                 传 after 取更早的页；不传=取最新一页，向后兼容）。
    """
    # v176.7 P0-fix：WTI 短路
    if inst_id == "WTI-USDT-SWAP":
        return None
    params: Dict[str, str] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    if after is not None:
        params["after"] = str(after)
    data = _okx_get("/api/v5/market/candles", params)
    sid = f"okx_swap:{inst_id}"
    if not data:
        source_status.report(sid, False, error="candles 请求失败/为空")
        # 数据诚实：OKX 永久 K 线失败时，XAU/XAG 不再合成扁平假 K 线（open=high=low=close=spot
        # 会污染 insight_verifier 事后方向判定 → 造成「假 verified」）。诚实标 unavailable，
        # 等 OKX 恢复（或配好 MARKET_USE_PROXY）后由真实 swap bars 补齐。
        return None
    rows = []
    # OKX candles 最新在前（倒序）→ 反转为升序；行 = [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    for r in reversed(data):
        try:
            rows.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
        except (TypeError, ValueError, IndexError):
            continue
    if len(rows) < 2:
        source_status.report(sid, False, error="candles 样本 <2")
        return None
    source_status.report(sid, True, detail=f"candles={len(rows)}")
    return rows


def _xau_xag_spot_fallback(inst_id: str, bar: str, limit: int) -> Optional[list]:
    """（已停用合成）OKX 永续 K 线失败时，XAU/XAG 不再合成扁平假 K 线。

    根因：国内现货源只有「当前点价」，无法给出真实历史 OHLC。之前用点价合成
    open=high=low=close=spot 的扁平序列，这些假 bar 流入 kline_bars，令
    insight_verifier 事后方向判定拿到 0 振幅数据 → 造成「假 verified」，违背
    事实正确/第一性原理。故改为返回 None（诚实标 unavailable），等 OKX 恢复
    （或配好代理）后由真实 swap bars 补齐。掉 ticker 级备胎不受影响（另见
    _fetch_xau_xag_spot_price，仅用于单点现价展示，且明确标 domestic-spot-fallback）。
    """
    return None


def fetch_okx_funding(inst_id: str) -> Optional[float]:
    """OKX 永续当前资金费率（小数，未 ×100）。失败 None。

    v176.7：WTI-USDT-SWAP 在 OKX 实际不存在，直接返回 None 不报 error。
    """
    # v176.7 P0-fix：WTI 短路
    if inst_id == "WTI-USDT-SWAP":
        return None
    data = _okx_get("/api/v5/public/funding-rate", {"instId": inst_id})
    sid = f"okx_swap:{inst_id}"
    if not data:
        source_status.report(sid, False, error="funding-rate 请求失败/为空")
        return None
    v = _f(data[0].get("fundingRate"))
    if v is None:
        source_status.report(sid, False, error="fundingRate 字段缺失")
        return None
    source_status.report(sid, True, detail=f"funding={v}")
    return v


def fetch_okx_open_interest(inst_id: str) -> Optional[float]:
    """OKX 永续持仓量（张数）。失败 None。

    v176.7：WTI-USDT-SWAP 在 OKX 实际不存在（原油没有永续合约），直接返回 None
    且不报 source_status error——避免累计 200+ 次假失败污染面板。
    """
    # v176.7：WTI-USDT-SWAP 不在 OKX 永续清单（原油只走国内现货 domestic_spot）
    if inst_id == "WTI-USDT-SWAP":
        return None
    data = _okx_get("/api/v5/public/open-interest", {"instId": inst_id})
    sid = f"okx_swap:{inst_id}"
    if not data:
        source_status.report(sid, False, error="open-interest 请求失败/为空")
        return None
    v = _f(data[0].get("oi"))
    if v is None:
        source_status.report(sid, False, error="oi 字段缺失")
        return None
    source_status.report(sid, True, detail=f"oi={v}")
    return v


# ── OKX 衍生品交割/到期日历（交易日历）─────────────────────────────────────
# 用途（市场风格感知的「日历锚」）：交割日/期权到期日前后，资金费率、基差、持仓往往
# 出现收敛/异常，是 regime 换挡的领先信号之一。OKX 无「节假日休市」概念（7x24），
# 因此「交易日历」= FUTURES 交割日 + OPTION 到期日 的时间表。
_OKX_CALENDAR_ULYS = ("BTC-USD", "ETH-USD", "SOL-USD")


def _ms_to_gmdate(ms: int) -> str:
    """毫秒时间戳 → UTC 'YYYY-MM-DD'（OKX 所有时间戳为 UTC，无本地时区歧义）。非正数/异常返回空串。"""
    if not ms or ms <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000.0))
    except (OverflowError, ValueError, OSError):
        return ""


def _uly_from_inst_id(inst_id: str) -> str:
    """从 instId 兜底推断标的：'BTC-USD-260626' → 'BTC-USD'；期权 'BTC-USD-260626-85000-C' → 'BTC-USD'。"""
    parts = str(inst_id or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ""


def aggregate_expiry_events(rows: Any, inst_type: str, now_ms: int, horizon_ms: int) -> list:
    """从 OKX /api/v5/public/instruments 的 data 列表提取未来窗口内到期/交割事件（纯函数）。

    - rows: instruments data 列表（每项 dict，含 expTime/uly/instId）
    - inst_type: 'FUTURES'（交割日）| 'OPTION'（到期日）
    - 窗口 = (now_ms, now_ms + horizon_ms]
    - 返回按时间戳升序事件列表，每项：{'ts_ms', 'date', 'type', 'underlyings'(去重升序), 'inst_count'}
    过滤规则：expTime 非未来、超出窗口、标的缺失（uly 与 instId 都空）的条目丢弃。
    """
    if not isinstance(rows, list):
        return []
    end_ms = now_ms + horizon_ms
    groups: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ts = int(row.get("expTime") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= now_ms or ts > end_ms:
            continue
        uly = str(row.get("uly") or "").strip() or _uly_from_inst_id(str(row.get("instId") or ""))
        if not uly:
            continue
        if ts not in groups:
            groups[ts] = {
                "ts_ms": ts,
                "date": _ms_to_gmdate(ts),
                "type": "futures_delivery" if inst_type == "FUTURES" else "option_expiry",
                "underlyings": [],
                "inst_count": 0,
            }
        g = groups[ts]
        if uly not in g["underlyings"]:
            g["underlyings"].append(uly)
        g["inst_count"] += 1
    events = sorted(groups.values(), key=lambda e: e["ts_ms"])
    for e in events:
        e["underlyings"].sort()
    return events


def _merge_by_date(events: list) -> list:
    """把 futures_delivery 与 option_expiry 按日期合并为「交易日历条目」（纯函数）。

    同一日期可能既有交割又有到期，合并为一条，types 并列、underlyings 去重。
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for e in events:
        d = e.get("date")
        if not d:
            continue
        if d not in merged:
            merged[d] = {
                "ts_ms": e["ts_ms"],
                "date": d,
                "types": [e["type"]],
                "underlyings": list(e["underlyings"]),
                "inst_count": e["inst_count"],
            }
        else:
            m = merged[d]
            if e["type"] not in m["types"]:
                m["types"].append(e["type"])
            for u in e["underlyings"]:
                if u not in m["underlyings"]:
                    m["underlyings"].append(u)
            m["inst_count"] += e["inst_count"]
    out = sorted(merged.values(), key=lambda x: x["ts_ms"])
    for m in out:
        m["types"].sort()
        m["underlyings"].sort()
    return out


def fetch_okx_instruments(inst_type: str, uly: str = "") -> Optional[list]:
    """OKX 交易产品列表（/api/v5/public/instruments）。inst_type: FUTURES/OPTION/SWAP/SPOT。失败 None。"""
    params: Dict[str, str] = {"instType": inst_type}
    if uly:
        params["uly"] = uly
    data = _okx_get("/api/v5/public/instruments", params)
    sid = f"okx_instruments:{inst_type}" + (f":{uly}" if uly else "")
    if not data:
        source_status.report(sid, False, error=f"instruments({inst_type}{'/'+uly if uly else ''}) 请求失败/为空")
        return None
    source_status.report(sid, True, detail=f"instType={inst_type} count={len(data)}")
    return data


def fetch_okx_delivery_calendar(horizon_days: int = 30) -> Optional[Dict[str, Any]]:
    """OKX 衍生品交割/到期日历（交易日历）：未来 horizon_days 天内的
    交割合约(FUTURES)交割日 + 期权(OPTION)到期日，升序、按日期聚合。

    返回:
      {"source":"okx", "as_of_ms":int, "horizon_days":int,
       "events":[{"ts_ms","date","types":["futures_delivery","option_expiry"],
                  "underlyings":[str,...],"inst_count":int}, ...],
       "next_event": <events[0]> | None,
       "covered_underlyings":[str,...]}
    单个 instType/标的失败只降级该类型，不整体失败；两类 instType 都失败返回 None。
    """
    now_ms = int(time.time() * 1000)
    horizon_ms = int(horizon_days) * 86400000
    collected: list = []
    any_ok = False
    for inst_type in ("FUTURES", "OPTION"):
        for uly in _OKX_CALENDAR_ULYS:
            rows = fetch_okx_instruments(inst_type, uly)
            if rows is None:
                continue
            any_ok = True
            collected.extend(aggregate_expiry_events(rows, inst_type, now_ms, horizon_ms))
    if not any_ok:
        source_status.report("okx_calendar", False, error="交割/期权 instruments 均拉取失败")
        return None
    events = _merge_by_date(collected)
    covered = sorted({u for e in events for u in e["underlyings"]})
    source_status.report(
        "okx_calendar", True,
        detail=f"events={len(events)} next={events[0]['date'] if events else '-'}",
    )
    return {
        "source": "okx",
        "as_of_ms": now_ms,
        "horizon_days": int(horizon_days),
        "events": events,
        "next_event": events[0] if events else None,
        "covered_underlyings": covered,
    }


# ── 币安现货（USDT）K 线 — data-api.binance.vision 官方数据镜像，国内可直连 ──
# 币安永续 fapi 直连被墙（走代理）、OKX 被 GFW SNI 阻断（熔断），两者在无代理环境
# 都不可靠。现货镜像已实测国内稳定直连，用于分钟级 K 线历史下载与实时兜底。
_VISION_BASE = "https://data-api.binance.vision"
_vision_lock = threading.Lock()
_vision_session: Any = None


def _vision() -> Any:
    """币安现货直连 session（trust_env=False，强制不走 env 代理）。"""
    global _vision_session
    if _vision_session is None:
        with _vision_lock:
            if _vision_session is None:
                try:
                    from http_retry import new_retry_session
                    _vision_session = new_retry_session(trust_env=False)
                except Exception:
                    if requests is None:
                        return None
                    _vision_session = requests.Session()
                    _vision_session.trust_env = False
    return _vision_session


def _vision_get_json(path: str, params: Dict[str, str], attempts: int = 3) -> Any:
    """现货镜像 GET（带限流退避重试）。

    data-api.binance.vision 高并发（快照多资产并行 ticker+4周期K线）偶发 429/连接抖动；
    单次失败即回退 fapi 会令 BTC/ETH 价格被标 unavailable，故对 429/连接失败退避重试。
    """
    session = _vision()
    if session is None:
        return None
    for i in range(attempts):
        try:
            # P1 修复: 3 次 × 15s = 45s 太长, 国内网络 vision 偶尔慢, dashboard 价格静止.
            # 改 3s 单次, 退避间隔 0.35s → 总耗时 ≤ 3*3 + 0.7 ≈ 10s, 仍 3 次重试保证 429.
            resp = session.get(f"{_VISION_BASE}{path}", params=params, timeout=3)
        except Exception:
            if i < attempts - 1:
                time.sleep(0.35 * (i + 1))
                continue
            return None
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        if resp.status_code in (429, 418, 403):
            if i < attempts - 1:
                time.sleep(0.35 * (i + 1))
                continue
        return None
    return None


def fetch_binance_spot_candles(symbol: str, interval: str, limit: int = 96) -> Optional[list]:
    """币安现货(USDT) K 线 → [[ts_ms, open, high, low, close, volume], ...] 升序。

    与 fetch_okx_candles 同构（第 6 列为基础资产成交量）。国内直连，失败 None。
    """
    data = _vision_get_json(
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": str(limit)},
    )
    if not isinstance(data, list) or len(data) < 2:
        source_status.report(f"binance_spot:{symbol}", False, error="candles 样本 <2 或无响应")
        return None
    rows = []
    for r in data:
        try:
            rows.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
        except (TypeError, ValueError, IndexError):
            continue
    if len(rows) < 2:
        source_status.report(f"binance_spot:{symbol}", False, error="candles 样本 <2")
        return None
    source_status.report(f"binance_spot:{symbol}", True, detail=f"candles={len(rows)} {interval}")
    return rows


def fetch_binance_spot_ticker(symbol: str) -> Optional[Dict[str, Any]]:
    """币安现货 24h ticker → {last, percentage, has_change, info}，国内直连。

    币安 fapi 被住宅代理(美国 IP)地区封禁，现货镜像 data-api.binance.vision 实测
    国内直连稳定，作 BTC/ETH 价格主源。失败 None（调用方回退 fapi 或标 unavailable）。
    """
    data = _vision_get_json("/api/v3/ticker/24hr", {"symbol": symbol})
    if not isinstance(data, dict):
        source_status.report(f"binance_spot_ticker:{symbol}", False, error="响应非 dict 或无响应")
        return None
    last = _f(data.get("lastPrice"))
    pct = _f(data.get("priceChangePercent"))
    if last is None or last <= 0:
        source_status.report(f"binance_spot_ticker:{symbol}", False, error="last 缺失")
        return None
    source_status.report(f"binance_spot_ticker:{symbol}", True, detail=f"last={last} pct={pct}")
    return {"last": last, "percentage": pct, "has_change": pct is not None, "info": data}


# ── 实时订单簿深度（v278） ────────────────────────────────────────────────
# 用途：归因页 K 线叠加「实时订单簿深度」柱状堆积图，让用户看到当前买卖盘
#       堆积在哪些价位，识别「上方卖盘墙」「下方买盘支撑」等结构。
# 公开免费、Binance/OKX 公共 API，国内直连 / 镜像。
# 限制：仅「当前快照」而非「历史 OI 分布」，每 10s 轮询一次就够用。

def fetch_binance_spot_orderbook(symbol: str, limit: int = 100) -> Optional[Dict[str, Any]]:
    """币安现货订单簿深度 → {bids: [[price, qty], ...], asks: [[price, qty], ...]}。
    价格升序、量升序；返回前 limit 档；失败 None。
    """
    data = _vision_get_json(
        "/api/v3/depth",
        {"symbol": symbol, "limit": str(min(max(limit, 5), 100))},
        attempts=2,
    )
    if not isinstance(data, dict):
        source_status.report(f"binance_spot_depth:{symbol}", False, error="响应非 dict 或无响应")
        return None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        source_status.report(f"binance_spot_depth:{symbol}", False, error="bids/asks 为空")
        return None
    # 校验 + 转 float
    out_bids: List[List[float]] = []
    for row in bids:
        try:
            out_bids.append([float(row[0]), float(row[1])])
        except (TypeError, ValueError, IndexError):
            continue
    out_asks: List[List[float]] = []
    for row in asks:
        try:
            out_asks.append([float(row[0]), float(row[1])])
        except (TypeError, ValueError, IndexError):
            continue
    if not out_bids or not out_asks:
        source_status.report(f"binance_spot_depth:{symbol}", False, error="bids/asks 解析后为空")
        return None
    source_status.report(
        f"binance_spot_depth:{symbol}", True,
        detail=f"bids={len(out_bids)} asks={len(out_asks)}",
    )
    return {"bids": out_bids, "asks": out_asks}


def fetch_okx_orderbook(inst_id: str, sz: int = 20) -> Optional[Dict[str, Any]]:
    """OKX 订单簿深度 → {bids: [[price, qty, _, _], ...], asks: [[price, qty, _, _], ...]}。
    OKX 返回 4 元组 [price, qty, _, _]；失败 None。
    """
    sz = min(max(sz, 1), 400)
    data = _okx_get(
        "/api/v5/market/books",
        {"instId": inst_id, "sz": str(sz)},
        timeout=2.0,
    )
    if not data or not isinstance(data, list) or not data[0].get("bids"):
        source_status.report(f"okx_swap_depth:{inst_id}", False, error="响应空或无 bids/asks")
        return None
    row0 = data[0]
    out_bids: List[List[float]] = []
    for r in row0.get("bids", []):
        try:
            out_bids.append([float(r[0]), float(r[1])])
        except (TypeError, ValueError, IndexError):
            continue
    out_asks: List[List[float]] = []
    for r in row0.get("asks", []):
        try:
            out_asks.append([float(r[0]), float(r[1])])
        except (TypeError, ValueError, IndexError):
            continue
    if not out_bids or not out_asks:
        source_status.report(f"okx_swap_depth:{inst_id}", False, error="bids/asks 解析后为空")
        return None
    source_status.report(
        f"okx_swap_depth:{inst_id}", True,
        detail=f"bids={len(out_bids)} asks={len(out_asks)}",
    )
    return {"bids": out_bids, "asks": out_asks}