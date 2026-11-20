# -*- coding: utf-8 -*-
"""cn_prices.py — 国内直连行情源 (新浪 hq.sinajs.cn + 腾讯 qt.gtimg.cn)

背景 (v324 实测 2026-11-10, **无代理**环境)
--------------------------------------------
`feed_probe.py probe` 实测: 行情三源 yahoo / yfinance / asia 的失败率 **46.7%**
(success 12 / failed 14 / degraded 4)。错误集中在:
    yahoo    → HTTP 429 Too Many Requests / SSL handshake timeout
    yfinance → history returned empty rows (cookie/crumb 取不到)
    asia     → 同样只走 yfinance (asia_prices.py 的 docstring 写明"数据源: Yahoo Finance")

而同一台机器上, **国内源直连实测全通**:
    腾讯 qt.gtimg.cn    → 200 (280~684ms)  A股/港股/美股/期货/黄金/白银
    新浪 hq.sinajs.cn  → 200 (1263~1293ms) **但必须带 Referer 头**, 否则 403
    新浪 A股指数稳定; hf_/gb_/rt_ 前缀偶发 SSL EOF (干扰型, 非封禁) → **必须重试**

所以本模块做两件事:
    1. 提供**国内双源**取价, 覆盖 A股指数 / 港股 / 美股 / 原油 / 黄金 / 白银;
    2. 一次请求**批量**取多个 symbol —— 实测批量请求比逐个请求稳得多
       (逐个请求时 hf_/gb_/rt_ 频繁 SSL EOF, 批量一次拿到 7 个品种)。

格式 (实测)
-----------
腾讯 `v_<sym>="..."`:
    · 指数/美股/港股 用 `~` 分隔 → **现价 = 第 4 段 (索引 3)**
      v_usNVDA="200~英伟达~NVDA.OQ~178.67~186.64~..."
    · 期货 hf_* 用 `,` 分隔     → **现价 = 第 1 段 (索引 0)**
      v_hf_CL="85.27,-1.82,85.31,..."
新浪 `var hq_str_<sym>="..."`:
    · A股指数 sh/sz  → 名称,今开,昨收,**现价**,最高,最低  → **索引 3**
    · 美股 gb_*      → 名称,**现价**,涨跌幅,时间,涨跌额...  → **索引 1**
    · 期货 hf_*      → **现价**,买价,卖价,...                → **索引 0**
    · 港股 rt_*      → 代码,名称,今开,昨收,最高,最低,**现价** → **索引 6**

铁律
----
· 拉不到返回 None —— 行情只是上下文, 绝不阻塞主流程 (与 asia_prices/stock_prices 一致)。
· 只读, 不写任何表。
· 纯解析函数不碰网络, 可离线单测。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_SINA_HEADERS = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"}

TENCENT_URL = "https://qt.gtimg.cn/q=%s"
SINA_URL = "https://hq.sinajs.cn/list=%s"

DEFAULT_TIMEOUT = 12.0
RETRY_TIMES = 2          # 干扰型 SSL EOF 需要重试 (实测偶发)
CACHE_TTL_S = 60.0       # 行情快照 60s 缓存

# 资产代码 → (腾讯 symbol, 新浪 symbol)
# 腾讯美股不带交易所后缀 (usNVDA); 新浪美股用 gb_ + 小写 (gb_nvda)
_ASSET_CN_SYMBOLS: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    # ── A股指数 ──
    "SHCOMP":   ("sh000001", "sh000001"),
    "CN_STOCK": ("sh000001", "sh000001"),
    "SZCOMP":   ("sz399001", "sz399001"),
    "CSI300":   ("sh000300", "sh000300"),
    # ── 港股 ──
    "HSI":      ("hkHSI", None),
    # ── 美股 ──
    "NVDA":     ("usNVDA", "gb_nvda"),
    "AAPL":     ("usAAPL", "gb_aapl"),
    "TSLA":     ("usTSLA", "gb_tsla"),
    "MSFT":     ("usMSFT", "gb_msft"),
    "GOOGL":    ("usGOOGL", "gb_googl"),
    "AMZN":     ("usAMZN", "gb_amzn"),
    "META":     ("usMETA", "gb_meta"),
    "COIN":     ("usCOIN", "gb_coin"),
    "PLTR":     ("usPLTR", "gb_pltr"),
    "SMCI":     ("usSMCI", "gb_smci"),
    "MARA":     ("usMARA", "gb_mara"),
    "RIOT":     ("usRIOT", "gb_riot"),
    "MSTR":     ("usMSTR", "gb_mstr"),
    "IBM":      ("usIBM", "gb_ibm"),
    "MU":       ("usMU", "gb_mu"),
    # 美股口径别名 (policy 里 US_HYNIX 就是 MU 的口径, 见 feed_redund_policy 注释)
    "US_HYNIX": ("usMU", "gb_mu"),
    # ── 商品/期货 ──
    "XAU":      ("hf_GC", "hf_XAU"),
    "GOLD":     ("hf_GC", "hf_XAU"),
    "XAG":      ("hf_SI", "hf_SI"),
    "SILVER":   ("hf_SI", "hf_SI"),
    "WTI":      ("hf_CL", "hf_CL"),
    "OIL":      ("hf_CL", "hf_CL"),
    "BRENT":    ("hf_OIL", None),
}

# 资产代码 → 中文展示名
_ASSET_LABELS: Dict[str, str] = {
    "SHCOMP": "上证指数", "CN_STOCK": "上证指数", "SZCOMP": "深证成指",
    "CSI300": "沪深300", "HSI": "恒生指数",
    "XAU": "黄金", "GOLD": "黄金", "XAG": "白银", "SILVER": "白银",
    "WTI": "WTI原油", "OIL": "WTI原油", "BRENT": "布伦特原油",
}



# ══════════════════════════════════════════════════════════════════════════
# 纯解析函数 (不碰网络, 可离线单测)
# ══════════════════════════════════════════════════════════════════════════
def _to_float(text: str) -> Optional[float]:
    """把字符串转成正数 float; 空串/非数字/非正数 → None。"""
    s = (text or "").strip()
    if not s or s in ("-", "--", "null", "None"):
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _to_signed(text: str) -> Optional[float]:
    """把字符串转成 float, **允许负数** (涨跌额/涨跌幅专用)。

    `_to_float` 只收正数 (价格不能是负), 但涨跌额可以是负: 跌 -7.97。
    v324 修复: gb_ 源用 _to_float 解析涨跌额 → 恒为 None → 前收算不出。
    """
    s = (text or "").strip()
    if not s or s in ("-", "--", "null", "None"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_tencent(text: str) -> Dict[str, float]:
    """解析腾讯 qt.gtimg.cn 的响应 → {symbol: 现价}。

    两种载荷:
        v_usNVDA="200~英伟达~NVDA.OQ~178.67~..."   → 现价索引 3
        v_hf_CL="85.27,-1.82,85.31,..."             → 现价索引 0
    """
    out: Dict[str, float] = {}
    if not text:
        return out
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk.startswith("v_"):
            continue
        sym, _, payload = chunk[2:].partition("=")
        payload = payload.strip().strip('"')
        if not sym or not payload:
            continue
        if "~" in payload:
            parts = payload.split("~")
            price = _to_float(parts[3]) if len(parts) > 3 else None
        else:
            parts = payload.split(",")
            price = _to_float(parts[0]) if parts else None
        if price is not None:
            out[sym] = price
    return out


def parse_sina(text: str) -> Dict[str, float]:
    """解析新浪 hq.sinajs.cn 的响应 → {symbol: 现价}。

    按 symbol 前缀选索引:
        sh/sz (A股指数) → 3     gb_ (美股) → 1
        hf_ (期货)      → 0     rt_ (港股) → 6
    """
    out: Dict[str, float] = {}
    if not text:
        return out
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if "hq_str_" not in chunk:
            continue
        sym, _, payload = chunk.partition("=")
        sym = sym.split("hq_str_")[-1].strip()
        payload = payload.strip().strip('"')
        if not sym or not payload:
            continue
        parts = payload.split(",")
        if sym.startswith("hf_"):
            idx = 0
        elif sym.startswith("gb_"):
            idx = 1
        elif sym.startswith("rt_"):
            # 新浪港股: 代码,名称,今开,昨收,最高,最低,**现价**,涨跌额,涨跌幅
            # (实测: 24522.990 - 24448.951 = 74.040 = 涨跌额, 自洽)
            idx = 6
        else:
            idx = 3
        price = _to_float(parts[idx]) if len(parts) > idx else None
        if price is not None:
            out[sym] = price
    return out


def parse_tencent_pairs(text: str) -> Dict[str, Tuple[float, Optional[float]]]:
    """解析腾讯响应 → {symbol: (现价, 前收价)}。

    前收价来源 (实测 2026-11-10):
        `~` 载荷 (指数/美股/港股): parts[3]=现价, **parts[4]=昨收**
            v_usNVDA="200~英伟达~NVDA.OQ~178.67~186.64~173.07~..."
        `,` 载荷 (期货 hf_*):
            v_hf_GC="4090.72,-0.58,4091.00,4091.20,4156.20,4088.20,18:10:00,4114.50,4149.00,..."
            parts[0]=现价, parts[1]=**涨跌幅%**, parts[2]=买价, parts[3]=卖价,
            parts[4]=最高, parts[5]=最低, parts[6]=时间, **parts[7]=昨收**, parts[8]=开盘
            ⚠️ parts[1] 是**百分比**不是涨跌额: (4090.72-4114.50)/4114.50 = -0.578% ≈ -0.58 ✅
    前收拿不到 → None (调用方自行决定是否跳过 24h 涨跌)。
    """
    out: Dict[str, Tuple[float, Optional[float]]] = {}
    if not text:
        return out
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk.startswith("v_"):
            continue
        sym, _, payload = chunk[2:].partition("=")
        payload = payload.strip().strip('"')
        if not sym or not payload:
            continue
        prev: Optional[float] = None
        if "~" in payload:
            parts = payload.split("~")
            price = _to_float(parts[3]) if len(parts) > 3 else None
            if price is not None and len(parts) > 4:
                prev = _to_float(parts[4])
        else:
            parts = payload.split(",")
            price = _to_float(parts[0]) if parts else None
            if price is not None and len(parts) > 7:
                prev = _to_float(parts[7])
        if price is not None:
            out[sym] = (price, prev)
    return out


def parse_sina_pairs(text: str) -> Dict[str, Tuple[float, Optional[float]]]:
    """解析新浪响应 → {symbol: (现价, 前收价)}。

    前收价来源 (实测):
        gb_ (美股): parts[1]=现价, parts[4]=**涨跌额** → 昨收 = 现价 - 涨跌额
            实测 178.6700 / -7.9700 → 昨收 186.64 (与腾讯 parts[4] 完全一致 ✓)
            注: parts[2] 是**涨跌幅%** (与腾讯 hf_ 同构)
        sh/sz (A股指数): parts[3]=现价, parts[2]=**昨收**
        rt_ (港股): parts[6]=现价, parts[3]=**昨收**
        hf_ (期货): parts[0]=现价, **parts[7]=昨收**
            实测 "85.289,,85.330,85.340,86.930,84.830,16:05:26,86.850"
            → parts[4]=最高, parts[5]=最低, parts[6]=时间, parts[7]=昨收(86.850)
            ⚠️ parts[6] 是时间串不是昨收 —— 曾误取导致前收恒为 None (v324 已修)
    """
    out: Dict[str, Tuple[float, Optional[float]]] = {}
    if not text:
        return out
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if "hq_str_" not in chunk:
            continue
        sym, _, payload = chunk.partition("=")
        sym = sym.split("hq_str_")[-1].strip()
        payload = payload.strip().strip('"')
        if not sym or not payload:
            continue
        parts = payload.split(",")
        prev: Optional[float] = None
        if sym.startswith("hf_"):
            price = _to_float(parts[0]) if parts else None
            if price is not None and len(parts) > 7:
                prev = _to_float(parts[7])
        elif sym.startswith("gb_"):
            price = _to_float(parts[1]) if len(parts) > 1 else None
            if price is not None and len(parts) > 4:
                chg = _to_signed(parts[4])          # 涨跌额, 可为负
                if chg is not None:
                    prev = price - chg
        elif sym.startswith("rt_"):
            price = _to_float(parts[6]) if len(parts) > 6 else None
            if price is not None and len(parts) > 3:
                prev = _to_float(parts[3])
        else:
            price = _to_float(parts[3]) if len(parts) > 3 else None
            if price is not None and len(parts) > 2:
                prev = _to_float(parts[2])
        if price is not None:
            out[sym] = (price, prev)
    return out


def asset_symbols(asset: str) -> Tuple[Optional[str], Optional[str]]:
    """资产代码 → (腾讯 symbol, 新浪 symbol); 不支持 → (None, None)。"""
    return _ASSET_CN_SYMBOLS.get((asset or "").strip().upper(), (None, None))


def stock_symbol(code: str, market: str = "") -> Optional[str]:
    """个股代码 → 腾讯 symbol：`600519.SH`→`sh600519`，`002384.SZ`→`sz002384`，`00700.HK`→`hk00700`。

    为什么单独开一个：`_ASSET_CN_SYMBOLS` 里只有 28 个指数/商品代码，个股一律查不到
    （v454 探针实测 `asset_symbols("600519.SH")` → (None, None)，看着像"腾讯不支持个股 K 线"，
    但其 K 线接口对 `sh600519` 实测返 321 根日线）。所以这里按后缀/位数补一层映射。
    """
    raw = (code or "").strip().upper()
    suffix = ""
    if "." in raw:
        raw, suffix = raw.rsplit(".", 1)
    if not raw:
        return None
    if suffix == "HK":
        return "hk" + raw.zfill(5)
    if suffix in ("SH", "SS"):
        return "sh" + raw
    if suffix == "SZ":
        return "sz" + raw
    if suffix:
        return None
    # 无后缀：6 位数字猜 A股（6/9 开头沪、0/2/3 开头深），5 位以内猜港股
    if raw.isdigit() and len(raw) == 6:
        return ("sh" if raw[0] in ("6", "9") else "sz") + raw
    if raw.isdigit() and len(raw) <= 5:
        return "hk" + raw.zfill(5)
    return None


def fetch_stock_history(code: str, market: str = "", interval: str = "1d",
                        limit: int = 300, timeout: float = DEFAULT_TIMEOUT
                        ) -> Optional[List[List[float]]]:
    """个股日线（腾讯前复权）→ [[ts_ms, o, h, l, c, v], ...] 升序。取不到 None。

    与 `fetch_history` 分开是因为后者只认 `_ASSET_CN_SYMBOLS`（28 个指数/商品）。
    实测 `sh600519` / `sz002384` / `hk00700` 都能返 320 根以上日线，够算一年分位。
    """
    period = kline_period(interval)
    sym = stock_symbol(code, market)
    if not sym or not period:
        return None
    count = max(1, min(int(limit or 300) + 5, 800))
    try:
        raw = _http_get(TENCENT_KLINE_URL % ("%s,%s,,,%d,qfq" % (sym, period, count)),
                        {"User-Agent": _UA}, timeout)
    except Exception:
        return None
    rows = parse_tencent_kline(raw or "", period)
    if not rows:
        return None
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


# ══════════════════════════════════════════════════════════════════════════
# K 线 (腾讯 web.ifzq.gtimg.cn) —— v324
#
# 实测 (2026-11-10):
#   A股指数 sh000001 / 港股 hkHSI  → param=<sym>,day,,,<n>,qfq  ✅
#   美股 usNVDA                        → 必须带交易所后缀, 不带只返回 2 根
#                                      (usNVDA.OQ 纳斯达克 / usIBM.N 纽交所) ✅
#   分钟线 m60 / 期货 hf_GC             → param error ❌ (本模块不覆盖)
#
# 行格式: [日期, **开, 收, 高, 低**, 量]  ← 注意是「开,收,高,低」不是 OHLC 顺序!
#   实测 sh000001 末根: ['2026-11-10','3682.040','3663.720','3683.730','3660.720','404865177.000']
#   与现价 3663.7234 / 今开 3682.0369 完全自洽 ✅
# ══════════════════════════════════════════════════════════════════════════
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s"

_KLINE_PERIODS = {
    "1d": "day", "1day": "day", "day": "day", "d": "day",
    "1wk": "week", "1w": "week", "week": "week", "w": "week",
    "1mo": "month", "1m": "month", "month": "month", "mo": "month",
}


def kline_period(interval: str) -> Optional[str]:
    """interval → 腾讯周期名; 不支持 (分钟线) → None。"""
    return _KLINE_PERIODS.get((interval or "").strip().lower())


def _kline_symbols(tx_symbol: str) -> List[str]:
    """腾讯 K 线 symbol 候选。美股必须带交易所后缀 (.OQ 纳斯达克 / .N 纽交所)。"""
    if tx_symbol.startswith("us"):
        return [tx_symbol + ".OQ", tx_symbol + ".N"]
    return [tx_symbol]


def parse_tencent_kline(raw: str, period: str = "day") -> List[List[float]]:
    """腾讯 K 线 JSON → [[ts_ms, o, h, l, c, v], ...] 升序。

    ts 用**本地时区当日 00:00**（与项目内 Naver/KRX 源同口径，见 quote_sources
    `fetch_naver_candles`：`time.mktime(time.strptime(ts_s, "%Y%m%d"))`）。
    """
    import json
    out: List[List[float]] = []
    if not raw:
        return out
    try:
        j = json.loads(raw)
    except (ValueError, TypeError):
        return out
    data = j.get("data") or {}
    if not isinstance(data, dict):
        return out
    rows: List[List[float]] = []
    for obj in data.values():
        if not isinstance(obj, dict):
            continue
        bars = None
        for key in (period, "qfq" + period, "hfq" + period):
            v = obj.get(key)
            if isinstance(v, list) and v and isinstance(v[0], list):
                bars = v
                break
        if bars is None:
            continue
        for bar in bars:
            if not isinstance(bar, (list, tuple)) or len(bar) < 6:
                continue
            date_s = str(bar[0])
            o = _to_float(str(bar[1]))
            c = _to_float(str(bar[2]))
            h = _to_float(str(bar[3]))
            lo = _to_float(str(bar[4]))
            v = _to_float(str(bar[5]))
            if not (o and c and h and lo):
                continue
            try:
                _dt = time.strptime(date_s[:10], "%Y-%m-%d")
                ts = time.mktime(_dt) * 1000
            except (ValueError, TypeError):
                continue
            # v335: 丢掉"占位日线"。腾讯在周末/收盘前会塞一根 O=H=L=C、量为 0 的行，
            #   美股周六那根实测就是平盘假数据（AMD 534.06/534.06/534.06/534.06, V=None）。
            #   判据：日线 + 量为 0 + 四价完全相等（真实交易日不可能四价一模一样且零成交）。
            if period == "day" and (v or 0) <= 0 and o == c == h == lo:
                continue
            if period == "day" and _dt.tm_wday >= 5 and (v or 0) <= 0:
                continue
            rows.append([float(int(ts)), o, h, lo, c, v or 0.0])
        break
    rows.sort(key=lambda r: r[0])
    return rows


def fetch_history(asset: str, interval: str = "1d", limit: int = 60,
                  timeout: float = DEFAULT_TIMEOUT
                  ) -> Optional[List[List[float]]]:
    """国内直连 K 线 → [[ts_ms, o, h, l, c, v], ...] 升序。取不到 None。

    v324: 无代理环境下 yahoo/yfinance 的 K 线全断 → 前端只能「模拟反推」，
    回测/复盘失真。这里用腾讯 K 线补上 A股指数 / 港股 / 美股。
    """
    a = (asset or "").strip().upper()
    period = kline_period(interval)
    if not a or not period:
        return None
    tx, _sn = asset_symbols(a)
    if not tx:
        return None
    count = max(1, min(int(limit or 60) + 5, 800))
    for sym in _kline_symbols(tx):
        try:
            raw = _http_get(TENCENT_KLINE_URL % ("%s,%s,,,%d,qfq" % (sym, period, count)),
                             {"User-Agent": _UA}, timeout)
        except Exception:
            continue
        rows = parse_tencent_kline(raw or "", period)
        if not rows:
            continue
        # 美股不带后缀只返 2 根 → 视为无效, 试下一个后缀
        if len(rows) < 2:
            continue
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return rows
    return None

def asset_label(asset: str) -> str:
    a = (asset or "").strip().upper()
    return _ASSET_LABELS.get(a, a)


# ══════════════════════════════════════════════════════════════════════════
# 网络层
# ══════════════════════════════════════════════════════════════════════════
_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, float]] = {}         # asset -> (price, ts)
_pair_cache: Dict[str, Tuple[float, Optional[float], float]] = {}  # asset -> (price, prev, ts)


def _http_get(url: str, headers: Dict[str, str], timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """GET 一次 (带重试)。干扰型 SSL EOF 会偶发, 重试即可过。"""
    import urllib.error
    import urllib.request
    last: Optional[Exception] = None
    for _ in range(RETRY_TIMES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("gbk", "replace")
        except urllib.error.HTTPError:
            raise
        except Exception as exc:      # SSL EOF / 超时 → 重试
            last = exc
            time.sleep(0.4)
    if last is not None:
        raise last
    return None


def fetch_batch(symbols: List[str], source: str = "tencent",
                timeout: float = DEFAULT_TIMEOUT) -> Dict[str, float]:
    """一次请求批量取价。source ∈ {"tencent", "sina"}。"""
    syms = [s for s in symbols if s]
    if not syms:
        return {}
    if source == "sina":
        url = SINA_URL % ",".join(syms)
        headers = _SINA_HEADERS
    else:
        url = TENCENT_URL % ",".join(syms)
        headers = {"User-Agent": _UA}
    raw = _http_get(url, headers, timeout)
    if not raw:
        return {}
    if source == "sina":
        return parse_sina(raw)
    return parse_tencent(raw)


def fetch_prices(assets: List[str], timeout: float = DEFAULT_TIMEOUT) -> Dict[str, float]:
    """按资产代码批量取价 (腾讯主源 + 新浪备源, 同一次请求批量)。"""
    assets = [a for a in (assets or []) if a]
    if not assets:
        return {}
    tx_syms, sn_syms, tx_assets, sn_assets = [], [], [], []
    for a in assets:
        tx, sn = asset_symbols(a)
        if tx:
            tx_syms.append(tx)
            tx_assets.append(a)
        if sn:
            sn_syms.append(sn)
            sn_assets.append(a)
    out: Dict[str, float] = {}
    if tx_syms:
        try:
            prices = fetch_batch(tx_syms, "tencent", timeout)
        except Exception:
            prices = {}
        for a, sym in zip(tx_assets, tx_syms):
            if sym in prices:
                out[a] = prices[sym]
    missing = [a for a in assets if a not in out]
    if missing and sn_syms:
        need = [(a, s) for a, s in zip(sn_assets, sn_syms) if a in missing]
        try:
            prices = fetch_batch([s for _a, s in need], "sina", timeout)
        except Exception:
            prices = {}
        for a, sym in need:
            if sym in prices:
                out[a] = prices[sym]
    return out


def fetch_price(asset: str, timeout: float = DEFAULT_TIMEOUT,
                use_cache: bool = True) -> Optional[float]:
    """按资产代码取现价 (带 60s 缓存)。取不到返回 None。"""
    a = (asset or "").strip().upper()
    if not a:
        return None
    if use_cache:
        with _cache_lock:
            hit = _cache.get(a)
            if hit and time.time() - hit[1] < CACHE_TTL_S:
                return hit[0]
    tx, sn = asset_symbols(a)
    if not tx and not sn:
        return None
    price = None
    try:
        if tx:
            got = fetch_batch([tx], "tencent", timeout)
            price = got.get(tx)
        if price is None and sn:
            got = fetch_batch([sn], "sina", timeout)
            price = got.get(sn)
    except Exception:
        price = None
    if price is not None:
        with _cache_lock:
            _cache[a] = (price, time.time())
    return price


def fetch_price_pair(asset: str, timeout: float = DEFAULT_TIMEOUT,
                     use_cache: bool = True
                     ) -> Tuple[Optional[float], Optional[float]]:
    """按资产代码取 (现价, 前收价)。取不到返回 (None, None)。

    v324: multi_source_fetcher 的 PRICE 链需要 (price, prev_close) 来算 24h 涨跌。
    腾讯主源直接带昨收 (parts[4]); 新浪备源用「现价 − 涨跌额」反推。
    """
    a = (asset or "").strip().upper()
    if not a:
        return (None, None)
    if use_cache:
        with _cache_lock:
            hit = _pair_cache.get(a)
            if hit and time.time() - hit[2] < CACHE_TTL_S:
                return (hit[0], hit[1])
    tx, sn = asset_symbols(a)
    if not tx and not sn:
        return (None, None)
    price: Optional[float] = None
    prev: Optional[float] = None
    try:
        if tx:
            raw = _http_get(TENCENT_URL % tx, {"User-Agent": _UA}, timeout)
            got = parse_tencent_pairs(raw or "")
            if tx in got:
                price, prev = got[tx]
        if price is None and sn:
            raw = _http_get(SINA_URL % sn, _SINA_HEADERS, timeout)
            got = parse_sina_pairs(raw or "")
            if sn in got:
                price, prev = got[sn]
    except Exception:
        price = None
        prev = None
    if price is not None:
        with _cache_lock:
            _pair_cache[a] = (price, prev, time.time())
    return (price, prev)


