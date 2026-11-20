"""Crowd Pulse — 真实数据源适配器（只做抓取与解析，不做任何推断）。

设计原则
--------
1. **只采真实数据**：每个条目都带 source + external_id + url + collected_at，
   可逐条回溯到原始页面；解析失败一律返回空列表，绝不用占位/编造内容填充。
2. **统一出口**：所有源都产出 list[RawItem]，下游聚合层与源解耦。
3. **离线可测**：解析逻辑都是纯函数（parse_*(payload) -> RawItem[]），
   网络只在 fetch_* 里发生，测试直接喂真实抓下来的样本即可。

已验证可用的源（2026-10-30 实测 HTTP 200）：
    baidu_hot        百度热搜·实时榜    top.baidu.com/api/board?tab=realtime
    baidu_finance   百度热搜·财经榜    top.baidu.com/api/board?tab=finance
    ths_hot         同花顺·热门股票榜  dq.10jqka.com.cn/fuyao/hot_list_data/...
    coingecko       CoinGecko 趋势     api.coingecko.com/api/v3/search/trending
    gnews           Google News RSS    news.google.com/rss/search?q=...

已知被反爬拦截（不纳入，保证不做"假数据"）：
    reddit / stocktwits / 雪球 / 知乎 / 微博 —— 均返回 401/403。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 15


@dataclass
class RawItem:
    """一条真实抓取到的条目。"""

    source: str                    # 源标识
    external_id: str               # 源内唯一标识（去重键）
    kind: str                      # asset | theme | news | coin
    title: str
    body: str = ""
    url: str = ""
    author: str = ""               # 媒体名 / 频道 / 社区
    heat: float = 0.0              # 源给出的热度原值
    rank_no: int = 0               # 源榜单排名（0 = 无排名）
    published_at: str = ""         # ISO8601，空串表示源未提供
    collected_at: str = ""         # 抓取时刻 ISO8601
    heat_norm: float = 0.0         # 源内排名归一后的可比热度（由 crowd_pulse 计算）
    meta: Dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class SourceError(RuntimeError):
    """某个数据源抓取失败。带上真实原因（HTTP 状态码 / 网络异常），供 collect() 如实记录。"""


def _http_get(url: str, timeout: int = _DEFAULT_TIMEOUT,
              headers: Optional[Dict[str, str]] = None,
              attempts: int = 3) -> Optional[str]:
    """GET 文本。彻底失败时抛 SourceError（带真实原因），绝不伪造空数据当成功。

    注意（2026-10-30 实测）：必须走 requests 并显式带 Accept 头 ——
    urllib 默认不发 Accept，百度/Google/CoinGecko 的反爬会直接回空体，
    表现为「HTTP 200 但 body 为空」，用 urllib 时表现为解析失败返回 []。
    这里保留 urllib 作兜底（requests 不可用时）。

    v317：CoinGecko 免费接口限流（429）会间歇性空体，
    原来的 2 次重试 + 固定 1 秒退避不足以扛住，且把失败原因吞成 None。
    现在改为指数退避（1s/3s/7s）+ 尊重 Retry-After，并把真实状态码带出去。
    """
    hdrs = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        hdrs.update(headers)
    last_err = "unknown"
    try:
        import requests # 局部导入：测试里不碰网络
    except ImportError:
        requests = None  # type: ignore[assignment]

    if requests is not None:
        backoff = 1.0
        for attempt in range(max(1, attempts)):
            try:
                resp = requests.get(url, headers=hdrs, timeout=timeout)
                if resp.status_code == 200 and resp.text:
                    return resp.text
                last_err = "HTTP %s (body %d bytes)" % (
                    resp.status_code, len(resp.text or ""))
                if resp.status_code == 429:
                    ra = resp.headers.get("Retry-After")
                    try:
                        backoff = max(backoff, float(ra))
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                last_err = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            if attempt < max(1, attempts) - 1:
                time.sleep(backoff)
                backoff = min(backoff * 3, 10.0)
        raise SourceError("%s → %s" % (url[:120], last_err))

    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise SourceError("%s → %s: %s" % (url[:120], type(exc).__name__, str(exc)[:120]))



def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# ─────────────────────────────── 百度热搜 ───────────────────────────────

def _baidu_rows(data: Any) -> List[Dict[str, Any]]:
    """百度热搜真实条目数组。

    实测（2026-10-30）：真正的榜单在 data.cards[0].content[0].content
    （实时 51 条 / 财经 30 条）；data.currentBoard.rule.content 是「榜单介绍」
    说明文字（首项 {"type":"title","text":"一、榜单介绍"}），不是榜单本身。
    这里主走 cards 路径，rule 路径仅作回退并过滤掉非词条项。
    """
    if not isinstance(data, dict):
        return []
    for card in data.get("cards") or []:
        if not isinstance(card, dict):
            continue
        for c1 in card.get("content") or []:
            if not isinstance(c1, dict):
                continue
            inner = c1.get("content")
            if isinstance(inner, list):
                rows = [r for r in inner if isinstance(r, dict) and r.get("word")]
                if rows:
                    return rows
    rule = (data.get("currentBoard") or {}).get("rule") or {}
    return [r for r in (rule.get("content") or [])
            if isinstance(r, dict) and r.get("word")]


def parse_baidu_hot(payload: Any) -> List[RawItem]:
    """百度热搜榜 → RawItem[]。

    字段（实测）：word / url / index(排名) / desc / hotScore / hotTag / isTop。
    百度多数榜单不给 hotScore，缺失时用「排名分」作热度代理：
    heat = 1.0 / rank（rank=1 → 1.0），并在 meta 里标注 heat_basis，
    保证下游能区分「源给的原始热度」与「由排名派生的代理热度」。
    """
    out: List[RawItem] = []
    if not isinstance(payload, dict):
        return out
    data = payload.get("data") or {}
    board_text = str((data.get("currentBoard") or {}).get("text") or "")
    source = "baidu_finance" if "财经" in board_text else "baidu_hot"
    rows = _baidu_rows(data)
    for idx, it in enumerate(rows):
        if not isinstance(it, dict):
            continue
        word = str(it.get("word") or "").strip()
        if not word:
            continue
        desc = str(it.get("desc") or it.get("description") or "")
        is_top = bool(it.get("isTop"))
        idx_val = _to_int(it.get("index") or 0)
        raw_heat = it.get("hotScore")
        if raw_heat is not None:
            heat = _to_float(raw_heat)
            heat_basis = "source_hot_score"
            rank = idx_val if idx_val > 0 else (idx + 1)
        elif is_top:
            # 置顶条目没有 index（百度把置顶词单独排在最前），
            # 若按出现序号赋 rank 会与后面 index=1 的条目撞号。
            # 这里给它 rank=0 并单独标 heat_basis=top_pinned。
            rank = 0
            heat = 1.0
            heat_basis = "top_pinned"
        else:
            rank = idx_val if idx_val > 0 else (idx + 1)
            heat = round(1.0 / max(1, rank), 6)
            heat_basis = "rank_proxy"
        out.append(RawItem(
            source=source,
            external_id="%s:%s" % (source, word),
            kind="theme",
            title=word,
            body=desc,
            url=str(it.get("url") or
                     ("https://www.baidu.com/s?wd=" + urllib.parse.quote(word))),
            heat=heat,
            rank_no=rank,
            meta={
                "desc": desc,
                "board": board_text,
                "heat_basis": heat_basis,
                "is_top": is_top,
                "hot_tag": str(it.get("hotTag") or ""),
            },
        ))
    return out


def fetch_baidu_hot(tab: str = "realtime") -> List[RawItem]:
    url = ("https://top.baidu.com/api/board?platform=wise&tab=%s"
           % urllib.parse.quote(tab))
    text = _http_get(url)
    if not text:
        raise SourceError("baidu_hot(%s) → 空响应体" % tab)
    try:
        return parse_baidu_hot(json.loads(text))
    except Exception as exc:
        raise SourceError("baidu_hot(%s) → 解析失败 %s: %s"
                          % (tab, type(exc).__name__, str(exc)[:120]))


# ─────────────────────────────── 同花顺热门股 ───────────────────────────────

# 同花顺 market 码 → 市场名（实测 market=33 等）
_HS_MARKET = {
    "17": "沪A", "33": "深A", "34": "创业板", "35": "科创板",
    "11": "沪B", "12": "深B", "44": "北交所", "48": "港股", "50": "美股",
    "60": "基金", "13": "转债",
    # 22: 实测为 600745 *ST闻泰（沪市主板）→ 沪A
    "22": "沪A",
}


def parse_ths_hot(payload: Any) -> List[RawItem]:
    """同花顺热门股票榜 → RawItem[]。

    结构（实测）：data.stock_list = [ {code, name, market, order, rate,
                                     rise_and_fall, tag:{concept_tag, popularity_tag}}, ... ]
    """
    out: List[RawItem] = []
    if not isinstance(payload, dict):
        return out
    data = payload.get("data") or {}
    rows = data.get("stock_list") or []
    if not isinstance(rows, list):
        return out
    for it in rows:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip()
        name = str(it.get("name") or "").strip()
        if not code or not name:
            continue
        mkt = _HS_MARKET.get(str(it.get("market")), str(it.get("market") or ""))
        tag = it.get("tag") or {}
        concepts = tag.get("concept_tag") or []
        if not isinstance(concepts, list):
            concepts = [concepts] if concepts else []
        out.append(RawItem(
            source="ths_hot",
            external_id="ths_hot:%s:%s" % (mkt, code),
            kind="asset",
            title="%s %s" % (code, name),
            body=str(tag.get("popularity_tag") or ""),
            url="https://q.10jqka.com.cn/%s" % code,
            author="同花顺",
            heat=_to_float(it.get("rate")),
            rank_no=_to_int(it.get("order")),
            meta={
                "code": code,
                "name": name,
                "market": mkt,
                "change_pct": _to_float(it.get("rise_and_fall")),
                "concepts": [str(c) for c in concepts],
                "popularity": str(tag.get("popularity_tag") or ""),
                "rank_chg": _to_int(it.get("hot_rank_chg")),
            },
        ))
    return out


def fetch_ths_hot(stock_type: str = "a", period: str = "hour") -> List[RawItem]:
    url = ("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
           "?stock_type=%s&type=%s&list_type=normal"
           % (urllib.parse.quote(stock_type), urllib.parse.quote(period)))
    text = _http_get(url)
    if not text:
        raise SourceError("ths_hot(%s/%s) → 空响应体" % (stock_type, period))
    try:
        return parse_ths_hot(json.loads(text))
    except Exception as exc:
        raise SourceError("ths_hot(%s/%s) → 解析失败 %s: %s"
                          % (stock_type, period, type(exc).__name__, str(exc)[:120]))


# ─────────────────────────────── CoinGecko 趋势 ───────────────────────────────

def parse_coingecko_trending(payload: Any) -> List[RawItem]:
    """CoinGecko trending → RawItem[]（coins + rwas，含黄金 XAU）。"""
    out: List[RawItem] = []
    if not isinstance(payload, dict):
        return out

    def _mk(group: str, it: Dict[str, Any], kind: str) -> Optional[RawItem]:
        sym = str(it.get("symbol") or "").upper()
        name = str(it.get("name") or "")
        if not sym and not name:
            return None
        data = it.get("data") or {}
        chg = _to_float(data.get("price_change_percentage_24h"), 0.0)
        return RawItem(
            source="coingecko",
            external_id="coingecko:%s:%s" % (group, it.get("id") or sym),
            kind=kind,
            title="%s %s" % (sym, name) if sym else name,
            body=name,
            url="https://www.coingecko.com/en/coins/%s" % (it.get("slug") or it.get("id") or ""),
            author="CoinGecko",
            heat=_to_float(it.get("market_cap_rank")),
            rank_no=0,
            meta={
                "group": group,
                "symbol": sym,
                "name": name,
                "market_cap_rank": _to_float(it.get("market_cap_rank")),
                "change_24h_pct": chg,
            },
        )

    for it in payload.get("coins") or []:
        if isinstance(it, dict) and isinstance(it.get("item"), dict):
            r = _mk("coins", it["item"], "coin")
            if r:
                out.append(r)
    for it in payload.get("rwas") or []:
        if isinstance(it, dict):
            r = _mk("rwas", it, "asset")
            if r:
                out.append(r)
    for it in payload.get("categories") or []:
        if isinstance(it, dict):
            r = _mk("categories", it, "theme")
            if r:
                out.append(r)
    return out


def fetch_coingecko_trending() -> List[RawItem]:
    text = _http_get("https://api.coingecko.com/api/v3/search/trending")
    if not text:
        raise SourceError("coingecko → 空响应体")
    try:
        return parse_coingecko_trending(json.loads(text))
    except Exception as exc:
        raise SourceError("coingecko → 解析失败 %s: %s"
                          % (type(exc).__name__, str(exc)[:120]))


# ─────────────────────────────── Google News RSS ───────────────────────────────

_RSS_DATE = re.compile(
    r"(\d{2})\s+(\w{3})\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s+([+-]\d{4}|\w+)"
)
_MON = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
        "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}


def _rfc822_to_iso(s: str) -> str:
    """RSS pubDate → 'YYYY-MM-DD HH:MM:SS'。解析不了就返回空串（不编造时间）。"""
    m = _RSS_DATE.search(s or "")
    if not m:
        return ""
    day, mon, year, hh, mm, ss, tz = m.groups()
    if mon not in _MON:
        return ""
    iso = "%s-%s-%s %s:%s:%s" % (year, _MON[mon], day, hh, mm, ss)
    if tz and tz.upper() != "GMT":
        try:
            import datetime as _dt
            sign = 1 if tz[0] == "+" else -1
            off = int(tz[1:3]) * 60 + int(tz[3:5])
            dt = _dt.datetime.strptime(iso, "%Y-%m-%d %H:%M:%S") - _dt.timedelta(minutes=sign * off)
            iso = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return iso


def parse_gnews(payload: str, source: str = "gnews") -> List[RawItem]:
    """Google News RSS 文本 → RawItem[]（source 元素即媒体名，用于媒体权威度分层）。

    source 参数：同一服务不同检索式视为**不同平台**（语料池不同）。
    例如 gnews(A股中文财经) 与 gnews_crypto(英文加密/商品) 各自独立，
    二者在同一资产上同时出现才构成真正的跨平台共识。
    """
    out: List[RawItem] = []
    if not payload:
        return out
    try:
        root = ET.fromstring(payload)
    except Exception:
        return out
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        channel = (src_el.text or "").strip() if src_el is not None else ""
        src_url = src_el.get("url") if src_el is not None else None
        out.append(RawItem(
            source=source,
            external_id="%s:%s" % (source, link or title),
            kind="news",
            title=title,
            body="",
            url=link,
            author=channel,
            heat=0.0,
            rank_no=0,
            published_at=_rfc822_to_iso(pub),
            meta={"channel": channel, "channel_url": src_url or "", "pub_date_raw": pub},
        ))
    return out


def fetch_gnews(query: str = "股市 OR 美股 OR A股",
                hl: str = "zh-CN", gl: str = "CN", ceid: str = "CN:zh-Hans") -> List[RawItem]:
    url = ("https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s"
           % (urllib.parse.quote(query), urllib.parse.quote(hl),
              urllib.parse.quote(gl), urllib.parse.quote(ceid)))
    text = _http_get(url)
    if not text:
        raise SourceError("gnews(%s) → 空响应体" % query[:40])
    try:
        return parse_gnews(text)
    except Exception as exc:
        raise SourceError("gnews(%s) → 解析失败 %s: %s"
                          % (query[:40], type(exc).__name__, str(exc)[:120]))


# v317: 加密资产 / 商品语料池（英文）。
# 存在的理由 —— 上面那条中文财经检索式覆盖的资产（A 股/美股）在本系统里
# **没有分钟级行情**，无法做"注意力是否先于价格移动"的验证；
# 而 BTC/ETH/SOL/XAU/XAG/WTI 既有分钟级 kline，又有真实的 pubDate，
# 是当前唯一能对上的「注意力 → 价格」可检验对。
# 同一服务不同语料池 → 记为独立平台，避免与中文 gnews 形成虚假共识。
GNEWS_CRYPTO_QUERY = ("Bitcoin OR Ethereum OR Solana OR \"gold price\" OR "
                       "\"silver price\" OR \"crude oil\"")


def fetch_gnews_crypto() -> List[RawItem]:
    url = ("https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s"
           % (urllib.parse.quote(GNEWS_CRYPTO_QUERY), "en-US", "US", "US:en"))
    text = _http_get(url)
    if not text:
        raise SourceError("gnews_crypto → 空响应体")
    try:
        return parse_gnews(text, source="gnews_crypto")
    except Exception as exc:
        raise SourceError("gnews_crypto → 解析失败 %s: %s"
                          % (type(exc).__name__, str(exc)[:120]))


# ─────────────────────────────── 采集编排 ───────────────────────────────

SOURCE_FETCHERS = {
    "baidu_hot": lambda: fetch_baidu_hot("realtime"),
    "baidu_finance": lambda: fetch_baidu_hot("finance"),
    "ths_hot": fetch_ths_hot,
    "coingecko": fetch_coingecko_trending,
    "gnews": fetch_gnews,
    "gnews_crypto": fetch_gnews_crypto,
}

DEFAULT_SOURCES = ("baidu_hot", "baidu_finance", "ths_hot", "coingecko",
                   "gnews", "gnews_crypto")


def collect(sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """依次抓取各源。返回 {'items': [...], 'per_source': {...}, 'errors': {...}}。

    失败源记入 errors（源名 → 错误描述），不静默吞掉，也不伪造空条目。
    """
    names = list(sources or DEFAULT_SOURCES)
    items: List[RawItem] = []
    per_source: Dict[str, int] = {}
    errors: Dict[str, str] = {}
    for name in names:
        fn = SOURCE_FETCHERS.get(name)
        if fn is None:
            errors[name] = "unknown source"
            continue
        try:
            got = fn() or []
        except Exception as exc:  # 单源异常不影响其它源
            errors[name] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            got = []
        if not got:
            errors.setdefault(name, "empty result")
        per_source[name] = len(got)
        for it in got:
            if not it.collected_at:
                it.collected_at = _now_iso()
            items.append(it)
    return {"items": items, "per_source": per_source, "errors": errors}
