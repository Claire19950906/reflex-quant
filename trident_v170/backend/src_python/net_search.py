# -*- coding: utf-8 -*-
"""免费全网搜索——给 LLM 提供「全网最早来源/时间」的真实证据，替代凭记忆猜。

无需任何 API key。按可用性并发尝试多个免费 HTML 源，任一命中即返回；全部失败返回空。

数据源（按国内可达性排序）：
  1. cn.bing.com（必应中国，国内可直连）
  2. lite.duckduckgo.com（DuckDuckGo Lite，需代理）
  3. html.duckduckgo.com（DuckDuckGo HTML，需代理）

设计约束：
  - 同步阻塞 + 短超时，只在 LLM 分析的 executor 线程里调用，不拖慢主循环。
  - 失败静默（返回空列表），绝不因搜索不可用阻塞新闻分析。
  - 结果去重、收敛到 topN，供 LLM 填 cross_net_first_seen_ts / cross_net_source。
"""

from __future__ import annotations

import html as _html
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_TIMEOUT_S = 6.0
_MAX_RESULTS = 6

# 直连 opener（忽略 env 代理）—— cn.bing.com 国内可直连。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return _html.unescape(_TAG_RE.sub(" ", s))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _strip_tags(s or "")).strip()


_BING_HOSTS = ("www.bing.com", "cn.bing.com")


def _ok_url(url_s: str) -> bool:
    if not url_s or not url_s.startswith(("http://", "https://")):
        return False
    low = url_s.lower()
    return not any(b in low for b in ("bing.com", "microsoft.com", "msn.com", "go.microsoft", "live.com"))


def _extract_ts(s: str) -> str:
    m = re.search(r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})", s or "")
    return m.group(1) if m else ""


def _search_bing(query: str) -> List[Dict[str, str]]:
    """必应国际/中国 HTML 抓取（直连，不走 env 代理）。优先 www.bing.com，失败回退 cn.bing.com。"""
    out: List[Dict[str, str]] = []
    for host in _BING_HOSTS:
        if len(out) >= _MAX_RESULTS:
            break
        url = "https://" + host + "/search?" + urllib.parse.urlencode({"q": query, "count": str(_MAX_RESULTS)})
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
            resp = _NO_PROXY_OPENER.open(req, timeout=_TIMEOUT_S)
            body = resp.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        # Bing 结果项：<li class="b_algo"> ... <h2><a href="URL">TITLE</a></h2> ... <p>SNIPPET</p>
        blocks = body.split('<li class="b_algo"')
        if len(blocks) > 1:
            for block in blocks[1:]:
                m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
                if not m or not _ok_url(m.group(1)):
                    continue
                title = _clean(m.group(2))
                sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
                snippet = _clean(sm.group(1))[:400] if sm else ""
                out.append({
                    "title": title[:120], "url": m.group(1),
                    "snippet": snippet, "ts": _extract_ts(snippet + " " + block[:200]),
                })
                if len(out) >= _MAX_RESULTS:
                    break
        else:
            # 兜底：新版结构里全局找 <h2><a href> 链接
            for m in re.finditer(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
                if not _ok_url(m.group(1)):
                    continue
                out.append({"title": _clean(m.group(2))[:120], "url": m.group(1), "snippet": "", "ts": ""})
                if len(out) >= _MAX_RESULTS:
                    break
    return out


def _search_ddg(query: str, base: str, use_proxy: bool = True) -> List[Dict[str, str]]:
    """DuckDuckGo HTML / Lite 抓取。lite 更轻，html 结果更全。"""
    path = "/html/" if "html" in base else "/lite/"
    url = f"https://{base}{path}?" + urllib.parse.urlencode({"q": query})
    opener = urllib.request.build_opener() if use_proxy else _NO_PROXY_OPENER
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        resp = opener.open(req, timeout=_TIMEOUT_S)
        body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    # lite 结果项：<a rel="nofollow" href="...">title</a>
    # html 结果项：<a class="result__a" href="...">title</a>
    pat = re.compile(
        r'<a[^>]+(?:rel="nofollow"|class="result__a")[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.S,
    )
    for m in pat.finditer(body):
        url_s, title = m.group(1), _clean(m.group(2))
        if not title or not url_s or "duckduckgo.com" in url_s:
            continue
        if url_s.startswith("//"):
            url_s = "https:" + url_s
        out.append({"title": title[:120], "url": url_s, "snippet": "", "ts": ""})
        if len(out) >= _MAX_RESULTS:
            break
    return out


def search(query: str, max_results: int = _MAX_RESULTS) -> List[Dict[str, str]]:
    """全网搜索入口。并发尝试多个免费源，返回去重后的 topN 结果。失败返回 []。"""
    q = (query or "").strip()
    if not q:
        return []
    if max_results <= 0:
        max_results = _MAX_RESULTS

    targets = [
        lambda: _search_bing(q),
        lambda: _search_ddg(q, "lite.duckduckgo.com", use_proxy=True),
        lambda: _search_ddg(q, "html.duckduckgo.com", use_proxy=True),
        # v176：先搜 Reuters / Bloomberg 权威源，结果排在前面 — 解决 LLM 不知道权威首发时
        # 也能给出有依据的 cross_net_first_seen_ts（路透/彭博是全球新闻首发的主源）
        lambda: _search_bing(f"site:reuters.com {q}"),
        lambda: _search_bing(f"site:bloomberg.com {q}"),
    ]
    merged: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        for res in ex.map(lambda f: f(), targets):
            merged.extend(res)
    # 按 URL 去重
    seen = set()
    deduped: List[Dict[str, str]] = []
    for r in merged:
        u = r.get("url", "")
        if not u or u in seen:
            continue
        seen.add(u)
        deduped.append(r)
    return deduped[:max_results]


def _build_origin_query(text: str) -> str:
    """从新闻文本提炼溯源搜索词：优先主体关键词，截断到合理长度。"""
    clean = re.sub(r"\s+", " ", re.sub(r"\[hash:[^\]]+\]", "", text or "")).strip()
    if not clean:
        return ""
    # 取首 120 字作为搜索词（标题/导语通常含主体 + 事件 + 时间）
    return clean[:120]


def origin_evidence_block(text: str) -> str:
    """生成注入 LLM prompt 的「全网搜索证据」块。无结果返回空串。"""
    q = _build_origin_query(text)
    if not q:
        return ""
    results = search(q)
    if not results:
        return ""
    lines = ["═══ 全网搜索证据（实时抓取，用于填写 cross_net_* 溯源字段）═══"]
    for i, r in enumerate(results, 1):
        ts = f" [{r['ts']}]" if r.get("ts") else ""
        snip = f" — {r['snippet'][:160]}" if r.get("snippet") else ""
        lines.append(f"{i}. {r['title']}{ts} · {r['url']}{snip}")
    lines.append(
        "请基于上述真实搜索结果，客观填写 net_origin_info / cross_net_first_seen_ts / "
        "cross_net_source；搜索结果里没有可靠时间/来源时如实填 '无法确认' 或留空，严禁编造。"
    )
    return "\n".join(lines)