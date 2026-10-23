#!/usr/bin/env python3
"""金十数据 MCP 客户端 — 快讯/财经日历/实时行情（官方 MCP 端点）。

端点: https://mcp.jin10.com/mcp （SSE 流式 JSON-RPC，Bearer token 鉴权）
流程: initialize → tools/call

工具:
  - list_flash    快讯分页列表（cursor 翻页, items: time/title/content/url + next_cursor）
  - list_calendar 本周财经日历（actual/consensus/previous/star/title）
  - list_news     文章分页列表
  - get_quote     实时行情（code → 价格/涨跌幅）
  - get_kline     分钟级 K 线
  - search_flash / search_news 关键词搜索

兼容性: fetch_jin10_flash() 与 jin10_feed.fetch_jin10_flash() 同签名返回
{"status","items","error"}，items 元素 {id,title,summary,source,published_at,url}，
因此 api_server 的 /api/news/jin10 与 news_source_watcher 无需改动即可接入。

金十 MCP 免费体验 token 在 .env 的 JIN10_MCP_TOKEN；买会员后同端点、
token 权限/配额升级（接口不变）。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

import config
import source_status

# ── 端点与鉴权 ────────────────────────────────────────────────────────────
MCP_URL = config.JIN10_MCP_URL
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_JSONRPC_ID = 0

# ── 多 token 轮换：配额用完自动切下一个 ───────────────────────────────────
_TOKENS = list(config.JIN10_MCP_TOKENS)
_token_idx = 0  # 当前活跃 token 下标（轮换推进，不回落）


def _current_token() -> str:
    return _TOKENS[_token_idx] if _TOKENS else ""


def _rotate_token() -> bool:
    """配额耗尽 → 切到下一个 token（重置会话）。单 token 或已到最后则不切换。"""
    global _token_idx
    if len(_TOKENS) <= 1:
        return False
    _token_idx = (_token_idx + 1) % len(_TOKENS)
    _reset_session()
    print(f"[JIN10-MCP] 配额耗尽 → 切换到第 {_token_idx + 1}/{len(_TOKENS)} 个 token", flush=True)
    return True


# 金十配额耗尽/限流错误特征（HTTP 200 但 JSON-RPC error）：
#   {"code": -32000, "message": "今日该工具的调用次数已达上限..."}
_QUOTA_HINTS = ("上限", "调用次数", "已达", "limit", "quota", "rate limit", "429", "频率")


def _is_quota_error(err_text: str) -> bool:
    lowered = (err_text or "").lower()
    return any(hint in lowered for hint in _QUOTA_HINTS)

# ── 会话（initialize 只做一次，线程安全）──────────────────────────────────
_session_lock = threading.Lock()
_session: Optional[requests.Session] = None
_session_inited_at = 0.0
_SESSION_TTL = 300.0  # 5 分钟重建一次会话，防 token 过期/连接老化


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": "Bearer " + _current_token(),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Accept-Encoding": "identity",   # 关键：gzip 解码后 content 乱码/截断
        "User-Agent": UA,
    })
    return s


def _get_session() -> Optional[requests.Session]:
    """获取（必要时初始化）MCP 会话。无 token 时返回 None。"""
    global _session, _session_inited_at
    if not _TOKENS:
        return None
    with _session_lock:
        now = time.monotonic()
        if _session is not None and now - _session_inited_at < _SESSION_TTL:
            return _session
        s = _new_session()
        try:
            r = s.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "trident", "version": "1.0"}}},
                timeout=15,
            )
            r.raise_for_status()
        except Exception as exc:
            print(f"[JIN10-MCP] initialize 失败: {type(exc).__name__}: {exc}")
            try:
                s.close()
            except Exception:
                pass
            return None
        _session = s
        _session_inited_at = now
        return s


def _reset_session() -> None:
    global _session, _session_inited_at
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
        _session = None
        _session_inited_at = 0.0


def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None, _retry: int = 0) -> Dict[str, Any]:
    """调用 MCP 工具，返回解析后的 JSON（data: 行解码 + content text 解析）。

    当前 token 配额耗尽/限流（_is_quota_error）时自动切换到下一个 token 重试一次。
    """
    global _JSONRPC_ID
    s = _get_session()
    if s is None:
        return {"error": "no token", "data": None}
    with _session_lock:
        _JSONRPC_ID += 1
        req_id = _JSONRPC_ID
    payload = {
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    try:
        r = s.post(MCP_URL, json=payload, timeout=20)
        r.raise_for_status()
        # 关键：content 解码，r.text 会被 Latin-1 误解码成乱码
        text = r.content.decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[JIN10-MCP] {name} 请求失败: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}", "data": None}

    # SSE 流式响应：逐行找 data: {...}
    result: Any = None
    err_text = ""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                msg = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if "result" in msg:
                result = msg["result"]
            elif "error" in msg:
                err_text = json.dumps(msg["error"], ensure_ascii=False)[:300]
    if result is None:
        # 配额耗尽/限流 → 轮换 token 重试一次（HTTP 200 但 JSON-RPC error）
        if err_text and _is_quota_error(err_text):
            print(f"[JIN10-MCP] {name} 配额/限流: {err_text[:120]}")
            # 防死循环：所有 token 均耗尽（轮换一圈）时停止，返回配额错误供上层降级，
            # 否则多 token 场景下 _rotate_token 永远 True → 无限递归把 watcher 卡死。
            if _retry < len(_TOKENS) and _rotate_token():
                return call_tool(name, arguments, _retry=_retry + 1)
        return {"error": err_text or "no result in response", "data": None}

    # MCP 工具结果: result.content[].text 是 JSON 字符串
    content = result.get("content") or []
    for part in content:
        txt = part.get("text") or part.get("content") or ""
        if not txt:
            continue
        try:
            return {"error": "", "data": json.loads(txt)}
        except json.JSONDecodeError:
            return {"error": "", "data": txt}
    return {"error": "empty content", "data": None}


def _iso_to_local(iso: str) -> str:
    """'2026-08-31T11:06:03+08:00' → '2026-08-31 11:06:03'（统一换算为北京时间）。

    走 config.fmt_bj：带时区换算到北京 / 无时区按北京时间——保证入库的
    published_at 与全项目同一时区口径（防把 UTC 当北京差 8 小时）。
    """
    if not iso:
        return ""
    return config.fmt_bj(iso)


def _age_hours(iso: str) -> Optional[float]:
    """条目发布时间距现在多少小时（统一北京时间口径）；无法解析返回 None（视为新鲜放行）。"""
    dt = config.parse_time_to_bj(iso)
    if dt is None:
        return None
    now = datetime.now(config.TZ_SHANGHAI)
    return (now - dt).total_seconds() / 3600.0


# ── 策略相关关键词白名单（与 jin10_feed 一致：金十快讯 80% 是 A股/公司公告等无关内容）──
_RELEVANT_KEYWORDS = (
    # 加密
    "比特币", "btc", "以太坊", "eth", "sol", "加密", "etf", "稳定币", "usdt", "usdc",
    "币安", "binance", "coinbase", "爆仓", "清算", "减半", "矿工", "defi",
    "比特币现货", "山寨币", "数字货币", "token",
    # 贵金属
    "黄金", "金价", "白银", "铂金", "xau", "gold", "贵金属",
    # 原油 / 能源
    "原油", "油价", "石油", "wti", "布伦特", "brent", "opec", "欧佩克", "天然气", "能源危机",
    # 宏观
    "美联储", "鲍威尔", "fomc", "央行", "加息", "降息", "利率", "cpi", "ppi", "pce",
    "非农", "通胀", "失业率", "gdp", "pmi", "ism", "美债", "国债", "美元指数", "美元",
    "dxy", "收益率", "量化宽松", "缩表", "货币政策", "点阵图", "联邦基金",
    # 地缘 / 风险
    "地缘", "战争", "冲突", "制裁", "导弹", "核武", "核武器", "核试验", "核设施",
    "核电站", "核潜艇", "中东", "伊朗", "以色列", "俄乌",
    "台海", "关税", "贸易战", "避险", "恐慌", "挤兑", "违约", "破产", "黑天鹅",
)


def _is_relevant(text: str) -> bool:
    lowered = (text or "").lower()
    return any(kw in lowered for kw in _RELEVANT_KEYWORDS)


# ── 快讯采集 ──────────────────────────────────────────────────────────────

def fetch_jin10_flash(pages: int = 2) -> Dict[str, Any]:
    """拉取金十最新快讯（list_flash，cursor 翻页），过滤出策略相关条目。

    返回 {"status","items","error"}（与 jin10_feed.fetch_jin10_flash 兼容）：
      items: [{id,title,summary,source,published_at,url}]
    失败返回 status="unavailable"。
    """
    t0 = time.time()
    first = call_tool("list_flash", {})
    if first.get("error"):
        # 401/限流等：重建会话重试一次（可能 token 过期/会话老化）
        _reset_session()
        first = call_tool("list_flash", {})
        if first.get("error"):
            source_status.report("jin10_mcp", False, error=str(first["error"])[:400],
                                 latency_ms=(time.time() - t0) * 1000)
            return {"status": "unavailable", "items": [], "error": first["error"]}
    data = first.get("data") or {}
    payload = data.get("data") if isinstance(data, dict) else {}
    if not isinstance(payload, dict):
        payload = {}

    items_raw: List[Dict[str, Any]] = []
    items_raw.extend(payload.get("items") or [])
    # 翻页：跟着 next_cursor 拉满 pages 页（免费 token 限流，最多 2 页）
    cursor = payload.get("next_cursor")
    for _ in range(max(0, pages - 1)):
        if not cursor:
            break
        nxt = call_tool("list_flash", {"cursor": cursor})
        nxt_data = nxt.get("data") or {}
        nxt_payload = nxt_data.get("data") if isinstance(nxt_data, dict) else {}
        if not isinstance(nxt_payload, dict):
            break
        items_raw.extend(nxt_payload.get("items") or [])
        cursor = nxt_payload.get("next_cursor")
        if not cursor:
            break

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    max_age_hours = float(getattr(config, "JIN10_FLASH_MAX_AGE_HOURS", 24) or 24)
    stale = 0
    newest_seen = None  # 诊断：MCP 返回条目里最新的发布时间（判断 feed 是否滞后）
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        content = str(it.get("content") or it.get("title") or "").strip()
        title = str(it.get("title") or "").strip()
        if not content and not title:
            continue
        full_text = f"{title} {content}".strip()
        if not _is_relevant(full_text):
            continue
        raw_time = str(it.get("time") or "")
        # 新鲜度闸：发布时间超过窗口（默认 24h）→ 旧闻不收入库（防 MCP 免费层返回历史缓存）
        age = _age_hours(raw_time)
        if age is not None:
            newest_seen = min(newest_seen, age) if newest_seen is not None else age
            if age > max_age_hours:
                stale += 1
                continue
        # 去重：内容哈希
        h = re.sub(r"\s+", "", full_text)[:200]
        if h in seen:
            continue
        seen.add(h)
        # 金十快讯条目通常没有 title 字段（只有 content）——正文即标题，
        # 保证 _ingest_external_news_sync（要求 title 非空）能正常入库。
        out_title = title if title else content[:300]
        items.append({
            "id": str(it.get("id") or it.get("uuid") or h)[:64],
            "title": out_title,
            "summary": content[:500],
            "source": "jin10",
            "published_at": _iso_to_local(raw_time),
            "url": str(it.get("url") or ""),
        })
        if len(items) >= 60:
            break

    if stale:
        print(
            f"[JIN10-MCP] 新鲜度过滤 {stale} 条旧闻"
            + (f"（最新条目距今约 {newest_seen:.1f} 小时——MCP feed 疑似滞后）" if newest_seen is not None and newest_seen > max_age_hours else ""),
            flush=True,
        )
    if not items:
        err = ("金十 feed 返回内容均为旧闻（超过新鲜度窗口），已过滤不入库"
               if stale else "no relevant items")
        source_status.report("jin10_mcp", False, error=err,
                             latency_ms=(time.time() - t0) * 1000)
        return {"status": "unavailable", "items": [], "error": err}
    source_status.report("jin10_mcp", True, detail=f"{len(items)} 条",
                         latency_ms=(time.time() - t0) * 1000)
    return {"status": "ok", "items": items, "error": ""}


def fetch_jin10_calendar() -> Dict[str, Any]:
    """本周财经日历（list_calendar）。items: {pub_time,star,title,actual,consensus,previous}。"""
    res = call_tool("list_calendar", {})
    if res.get("error"):
        return {"status": "unavailable", "items": [], "error": res["error"]}
    data = res.get("data") or {}
    rows = data.get("data") if isinstance(data, dict) else None
    items = []
    for it in rows or []:
        if not isinstance(it, dict):
            continue
        items.append({
            "pub_time": str(it.get("pub_time") or ""),
            "star": it.get("star"),
            "title": str(it.get("title") or ""),
            "actual": it.get("actual"),
            "consensus": it.get("consensus"),
            "previous": it.get("previous"),
        })
    return {"status": "ok" if items else "unavailable", "items": items, "error": ""}


def _fmt_cal_val(v: Any) -> str:
    """日历数值显示：None/空 → '—'，否则原文。"""
    if v is None or v == "":
        return "—"
    return str(v)


def calendar_events(min_star: int = 2, max_age_hours: Optional[float] = None) -> List[Dict[str, Any]]:
    """财经日历发布事件检测（供金十日历 watcher 轮询调用）。

    规则：只对「已公布」（actual 有值）且「星级 ≥ min_star」的条目生成事件——
    ★1 是无关紧要的小数据（各国小指标），★2 中等（PMI/零售/GDP 等），
    ★3 高影响（CPI/非农/利率/央行决议等，直接影响行情）。

    max_age_hours：公布时间窗口（小时）。非 None 时只生成"公布时间距今 ≤ 该值"
    的条目——即只推送"最新公布"的数据，防止重置/首次启动/feed 滞后把几天前
    已公布的旧条目当新事件推入（如 8/31 的韩国数据不应在 9/4 推送）。
    None = 不过滤（保留旧行为，供测试/兼容）。

    事件 id = jin10-cal-{title}-{pub_time}：稳定不变，新闻管道按 [hash:] 前缀去重
    （同一条目只入库一次，actual 修订不会重复触发）；新公布的条目首次出现时

    返回: [{id, source, title, summary, published_at, fresh_event}]；MCP 失败 → []。
    """
    res = fetch_jin10_calendar()
    if res.get("status") != "ok":
        return []
    events: List[Dict[str, Any]] = []
    for it in res.get("items") or []:
        actual = it.get("actual")
        if actual is None or actual == "":
            continue  # 未公布
        star = it.get("star")
        try:
            star_int = int(star)
        except (TypeError, ValueError):
            star_int = 0
        if star_int < min_star:
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        pub_time_full = str(it.get("pub_time") or "").strip()
        pub_time = pub_time_full[:16]  # 展示/去重用（分钟精度）
        # ── 最新公布窗口：公布时间距今 > max_age_hours → 旧条目不推送 ──
        # （防重置/首次启动/feed 滞后把早已公布的旧数据当新事件推入）
        if max_age_hours is not None and pub_time_full:
            pub_dt = config.parse_time_to_bj(pub_time_full)
            if pub_dt is not None:
                age_h = (datetime.now(config.TZ_SHANGHAI) - pub_dt).total_seconds() / 3600.0
                if age_h > float(max_age_hours):
                    print(
                        f"[JIN10-CAL] 跳过旧日历条目（公布 {age_h:.1f}h > {max_age_hours:.1f}h 窗口）:"
                        f" {title[:50]}",
                        flush=True,
                    )
                    continue
        actual_s = _fmt_cal_val(actual)
        consensus_s = _fmt_cal_val(it.get("consensus"))
        previous_s = _fmt_cal_val(it.get("previous"))
        events.append({
            "id": f"jin10-cal-{title}-{pub_time}",
            "source": "jin10_calendar",
            "title": f"[金十公布] {title} 实际 {actual_s}",
            # 平台发布时间 = 日历公布时间（计划/实际时刻），保留完整原文
            # 由入库链路 config.fmt_bj 统一换算为北京时间（带时区换算/无时区按北京）。
            # 与采集时间对比即可判断日历通道是否滞后（正常公布后分钟级入库）。
            "published_at": pub_time_full,
            # 公布检测事件：actual 刚出现即生成（检测到公布=新鲜），
            # pub_time 是计划公布时刻可能早于检测时刻 → 入库新鲜度闸豁免。
            "fresh_event": True,
            "summary": (
                f"金十财经日历（★{star_int}）{title} 数据公布："
                f"实际 {actual_s}，预测 {consensus_s}，前值 {previous_s}，"
                f"公布时间 {pub_time}。"
            ),
        })
    return events


def upcoming_events(min_star: int = 2, horizon_hours: float = 24.0) -> List[Dict[str, Any]]:
    """未来 horizon 小时内「即将公布」的财经日历事件（纯数据，供 AI prompt / 前端提示）。

    与 next_upcoming_release（只返回调度用时间戳）不同，本函数返回事件明细
    {pub_time, star, title, consensus, previous}，按公布时间升序。
    只统计 actual 为空（尚未公布）且 pub_time 落在 [now, now+horizon_hours] 的条目。
    MCP 不可用 → []（调用方静默降级，不阻塞主链路）。
    """
    res = fetch_jin10_calendar()
    if res.get("status") != "ok":
        return []
    now_ts = time.time()
    out: List[Dict[str, Any]] = []
    for it in res.get("items") or []:
        actual = it.get("actual")
        if actual is not None and actual != "":
            continue  # 已公布
        try:
            star_int = int(it.get("star"))
        except (TypeError, ValueError):
            star_int = 0
        if star_int < min_star:
            continue
        pub_raw = str(it.get("pub_time") or "").strip()
        if not pub_raw:
            continue
        dt = config.parse_time_to_bj(pub_raw)
        if dt is None:
            continue
        ts = dt.timestamp()
        if ts <= now_ts or ts > now_ts + horizon_hours * 3600:
            continue
        out.append({
            "pub_time": pub_raw,
            "star": star_int,
            "title": str(it.get("title") or ""),
            "consensus": it.get("consensus"),
            "previous": it.get("previous"),
        })
    out.sort(key=lambda e: e["pub_time"])
    return out


def get_quote(code: str) -> Dict[str, Any]:
    """实时行情报价（get_quote）。"""
    res = call_tool("get_quote", {"code": code})
    if res.get("error"):
        return {"status": "unavailable", "data": None, "error": res["error"]}
    return {"status": "ok", "data": res.get("data"), "error": ""}


# ── 分钟级 K 线（get_kline）：品种代码映射（quote://codes 资源实测所得）─────
# 金十 get_kline 仅提供「1 分钟 K 线」，count 上限 100、时间范围近 24h 内。
# 用于补 XAU/WTI/XAG 在 OKX/Yahoo 被墙时的分钟级真实行情（国内 mcp.jin10.com 直连）。
JIN10_KLINE_CODE: Dict[str, str] = {
    "XAU": "XAUUSD", "GOLD": "XAUUSD",
    "XAG": "XAGUSD",
    "WTI": "USOIL", "OIL": "USOIL",
}


def get_kline(code: str, count: int = 100, start_ts: Optional[int] = None) -> Dict[str, Any]:
    """分钟级 K 线（get_kline）。code 如 XAUUSD/USOIL/XAGUSD。

    返回 {"status": "ok|unavailable", "data": {code,name,klines:[{time,open,high,low,close,volume}]}, "error": "..."}。
    klines 为 1m 级、升序、秒级时间戳；count 兼容截断到 [1, 100]。
    """
    args: Dict[str, Any] = {"code": code, "count": max(1, min(int(count or 100), 100))}
    if start_ts:
        args["time"] = int(start_ts)
    res = call_tool("get_kline", args)
    if res.get("error"):
        return {"status": "unavailable", "data": None, "error": res["error"]}
    d = res.get("data") or {}
    # 金十返回 {data: {code,name,klines}, message, status} — 内层 data 才是品种数据
    inner: Any = d.get("data") if isinstance(d, dict) else None
    if isinstance(inner, dict) and inner.get("klines"):
        return {"status": "ok", "data": inner, "error": ""}
    return {"status": "unavailable", "data": None, "error": "no klines"}


# ── 计划时刻感知调度（金十日历 watcher 用）：按条目计划公布时间掐点轮询 ──
# 金十日历每条目带 planned pub_time（actual 为空 = 未公布）。调度逻辑：
#   · 低频轮询时解析"未来 horizon 小时内将公布的高星级条目"（★ ≥ min_star）
#   · 若有：返回最近一条计划时间 → watcher 据此在公布前 ~2min 起密集轮询 actual
#   · 无（本周无临近高影响数据）→ 返回 None，watcher 保持低频
# 相比固定 30s 全天轮询：非发布时段几乎不消耗 MCP 配额（曾 1500 次/日耗尽）。

def next_upcoming_release(min_star: int = 2, horizon_hours: float = 12.0) -> Optional[float]:
    """未来 horizon 小时内最近一条将公布的高星级日历条目的计划时间戳（Unix 秒）。

    仅统计 actual 为空（未公布）且 pub_time 在未来窗口内的条目；
    解析失败/无匹配 → None。供 watcher 计算"距下次公布还有多久"，掐点密集轮询。
    """
    res = fetch_jin10_calendar()
    if res.get("status") != "ok":
        return None
    now_ts = time.time()
    best: Optional[float] = None
    for it in res.get("items") or []:
        actual = it.get("actual")
        if actual is not None and actual != "":
            continue  # 已公布，不参与"即将公布"调度
        try:
            star_int = int(it.get("star"))
        except (TypeError, ValueError):
            continue
        if star_int < min_star:
            continue
        pub_raw = str(it.get("pub_time") or "").strip()
        if not pub_raw:
            continue
        dt = config.parse_time_to_bj(pub_raw)
        if dt is None:
            continue
        ts = dt.timestamp()
        if ts <= now_ts or ts > now_ts + horizon_hours * 3600:
            continue  # 已过或太远
        if best is None or ts < best:
            best = ts
    return best


if __name__ == "__main__":
    t0 = time.time()
    result = fetch_jin10_flash()
    print(f"status={result['status']} items={len(result['items'])} in {time.time()-t0:.1f}s")
    for item in result["items"][:5]:
        print(f"  [{item['published_at']}] {item['summary'][:60]}")
    cal = fetch_jin10_calendar()
    print(f"\ncalendar status={cal['status']} items={len(cal['items'])}")
    for item in cal["items"][:5]:
        print(f"  [{item['pub_time'][:16]}★{item['star']}] {item['title'][:30]} 预测{item['consensus']} 前值{item['previous']} 公布{item['actual']}")
