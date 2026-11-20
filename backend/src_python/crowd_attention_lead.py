#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v317 Crowd Attention Lead —— 注意力是否**先于**有意义的行情移动。

命题（可证伪）：
    「多个独立平台同时提到某个标的」这种注意力信号，是否出现在价格明显移动**之前**？

做法（全部基于真实抓取的数据，不做任何补值）：
    1. 注意力事件（Attention Event）
       从 crowd_source_items 取**带真实发布时间戳**（published_at，UTC）的条目，
       用标题/话题键匹配出可交易标的 → 归并成「某标的在 T 时刻被 N 个平台提到」。
       没有真实时间戳的条目一律不参与（采集时刻 ≠ 出现时刻，混用会让领先量变成噪声）。
    2. 价格移动（Meaningful Price Move）
       从 kline_bars 取该标的在 T 之后的分钟级 bar，以 T 时刻的收盘为基准，
       找到第一根 |收益| >= 阈值 的 bar → 该时刻就是"有意义的价格移动"。
    3. 领先量（Lead）
       lead_minutes = 价格移动时刻 − 注意力时刻。
       lead > 0 → 注意力领先价格；lead < 0 → 价格先动、注意力后知（追热点）。

三个出口：
    evaluate_lead()        —— 主统计：命中率 / 平均领先分钟 / 逐事件明细
    pending_attention()    —— 先行漏检候选：注意力已起、价格未动、系统也没信号
    attribution_evidence() —— 归因证据：某次行情异动之前有没有跨平台注意力

诚实性约定：
    · 样本不足时返回空明细 + note，绝不用占位数据冒充结论。
    · 每条结论都带 explain，写明用了哪条数据、哪个阈值。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import statistics
from typing import Any, Dict, List, Optional, Tuple

import config

# ─────────────────────────── 可交易标的识别 ───────────────────────────
# 标题 → kline_bars.symbol。key 是 kline 里的 symbol，value 是识别用的正则。
# 顺序有意义：先匹配更具体的（silver/crude），再匹配通用的（gold）。
SYMBOL_PATTERNS: List[Tuple[str, str]] = [
    ("XAG", r"\b(XAG|Silver|silver price|silver market)\b"),
    ("XAU", r"\b(XAU|Gold|gold price|gold market)\b"),
    ("WTI", r"\b(WTI|Crude|crude oil|oil price)\b"),
    ("BTC", r"\b(BTC|Bitcoin|bitcoin)\b"),
    ("ETH", r"\b(ETH|Ethereum|ethereum)\b"),
    ("SOL", r"\b(SOL|Solana|solana)\b"),
]

_COMPILED_SYMBOLS: List[Tuple[str, "re.Pattern[str]"]] = [
    (sym, re.compile(pat)) for sym, pat in SYMBOL_PATTERNS
]

# topic_key 形如 asset:CRYPTO:BTC / asset:沪A:600519 —— 直接可用的优先
_TOPIC_ASSET_RE = re.compile(r"^asset:[^:]*:([A-Za-z0-9._\-]{2,20})$")

# 行情表里实际存在的 symbol（跑一次 evaluate 时探测，避免对不存在的标的做空比较）


def detect_symbols(text: str) -> List[str]:
    """从自由文本里识别可交易标的。识别不到就返回空列表（不猜）。"""
    if not text:
        return []
    out: List[str] = []
    for sym, pat in _COMPILED_SYMBOLS:
        if pat.search(text) and sym not in out:
            out.append(sym)
    return out


def symbol_of_item(topic_key: str, title: str) -> str:
    """单条条目的标的。优先用 topic_key（结构化），退化到标题正则。"""
    m = _TOPIC_ASSET_RE.match(str(topic_key or ""))
    if m:
        sym = m.group(1).upper()
        if sym in {s for s, _ in SYMBOL_PATTERNS}:
            return sym
    hits = detect_symbols(title or "")
    return hits[0] if hits else ""


# ─────────────────────────── 时间处理 ───────────────────────────
# published_at 存的是 UTC（crowd_sources._rfc822_to_iso 已把 RFC822 时区归一到 UTC）。
# kline_bars.ts 是 epoch 毫秒（UTC）。两者同一基准，可直接比较。
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def parse_utc_ms(s: str) -> int:
    """'YYYY-MM-DD HH:MM:SS'(UTC) → epoch ms。解析失败返回 0（不编造时间）。"""
    s = str(s or "").strip()
    if not s:
        return 0
    for fmt in (_TS_FMT, "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = _dt.datetime.strptime(s[:19], fmt).replace(tzinfo=_dt.timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return 0


def fmt_utc(ms: int) -> str:
    if not ms:
        return ""
    return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).strftime(_TS_FMT)


def known_symbols(conn: sqlite3.Connection) -> set:
    """kline_bars 里真实存在的 symbol 集合。

    刻意**不做跨连接缓存**：这个缓存一旦是全局的，换一个连接（比如测试里的
    :memory: 库）就会拿到上一个库的 symbol 集合，表现为"明明有行情却匹配不上"。
    DISTINCT symbol 的查询代价很小（kline_bars 十万行级也就几十毫秒），
    而 collect_attention_events 每次只调一次，不值得为它引入缓存一致性问题。
    """
    try:
        rows = conn.execute("SELECT DISTINCT symbol FROM kline_bars").fetchall()
        return {str(r[0]).upper() for r in rows}
    except Exception:
        return set()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False



# ─────────────────────────── 注意力事件 ───────────────────────────
def collect_attention_events(conn: sqlite3.Connection,
                             lookback_hours: int = 72,
                             min_platforms: int = 1,
                             require_price_symbol: bool = False,
                             anchor_ms: Optional[int] = None,
                             ) -> List[Dict[str, Any]]:
    """从 crowd_source_items 提取注意力事件。

    事件定义：某个标的在某个时间窗口内，被 >= min_platforms 个**独立平台**提到，
    且至少有 1 条带**真实发布时间戳**（published_at）的条目。

    require_price_symbol:
        False（默认）= 收集**全部**可识别的注意力事件。
            「这个标的有没有行情」是另一件事，由 price 对照独立判断 ——
            这样"有注意力事件但该时段没行情"才是可观测状态，
            而不是被提前过滤掉、导致 events_with_price 永远等于 events_total。
        True  = 只保留在 kline_bars 里有行情的标的。

    anchor_ms:
        回看窗口的**锚点**（epoch 毫秒）。None = 用 wall-clock now（默认，
        「最近 N 小时有哪些注意力」这种实时语义）。
        非 None = 窗口是 [anchor_ms - lookback_hours, anchor_ms]。

        为什么必须有这个参数（v318 修）：
        `attribution_evidence()` 是**给历史异动找注意力证据**用的 ——
        它拿到的是「异动发生时刻 ts_ms」。如果回看窗口锚在 wall-clock now，
        那么给 30 天前的一次异动做归因时，窗口落在「今天往前 N 小时」，
        异动当时的注意力条目全部落在窗口外 → 归因恒为空。
        （实测：2026-10-30 的两条 attention 条目在 2026-10-31 之后
          再跑 `attribution_evidence` 就再也找不到了。）

        锚定到 ts_ms 之后，语义变成「异动之前 N 小时内有没有注意力」——
        这才是归因该问的问题，而且天然满足 no-lookahead：
        只看异动**之前**的条目。

    返回按注意力时刻升序排列的事件列表。
    """
    if not _has_table(conn, "crowd_source_items"):
        return []
    if anchor_ms is None:
        now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    else:
        now_ms = int(anchor_ms)
    since_ms = now_ms - max(1, int(lookback_hours)) * 3600 * 1000
    live = known_symbols(conn) if require_price_symbol else None

    try:
        rows = conn.execute(
            """
            SELECT source, title, url, media_channel, published_at, collected_at,
                   topic_key, day, heat
              FROM crowd_source_items
             WHERE published_at <> ''
             ORDER BY published_at ASC
            """
        ).fetchall()
    except Exception:
        return []

    # symbol → 整点窗口 → 聚合体
    buckets: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for r in rows:
        pub = str(r[4] or "")
        ts_ms = parse_utc_ms(pub)
        if not ts_ms or ts_ms < since_ms or ts_ms > now_ms + 60_000:
            continue
        sym = symbol_of_item(str(r[6] or ""), str(r[1] or ""))
        if not sym:
            continue
        if live is not None and sym not in live:
            continue
        win = (ts_ms // (60 * 60 * 1000)) * (60 * 60 * 1000)  # 按整点归桶
        bucket = buckets.setdefault(sym, {}).setdefault(win, {
            "symbol": sym,
            "attention_ts": ts_ms,
            "platforms": {},
            "sources": {},
            "items": [],
        })
        if ts_ms < bucket["attention_ts"]:
            bucket["attention_ts"] = ts_ms
        platform = str(r[0] or "")
        bucket["platforms"][platform] = bucket["platforms"].get(platform, 0) + 1
        bucket["sources"][platform] = bucket["sources"].get(platform, 0) + 1
        bucket["items"].append({
            "source": platform,
            "title": str(r[1] or "")[:180],
            "url": str(r[2] or ""),
            "media_channel": str(r[3] or ""),
            "published_at": pub,
            "heat": float(r[8] or 0.0),
        })

    events: List[Dict[str, Any]] = []
    for sym, wins in buckets.items():
        for win, b in wins.items():
            if len(b["platforms"]) < max(1, int(min_platforms)):
                continue
            b["platform_count"] = len(b["platforms"])
            b["source_count"] = len(b["sources"])
            b["item_count"] = len(b["items"])
            b["attention_at"] = fmt_utc(b["attention_ts"])
            b["platform_list"] = sorted(b["platforms"].keys())
            events.append(b)
    events.sort(key=lambda e: e["attention_ts"])
    return events


# ─────────────────────────── 价格移动 ───────────────────────────
def price_move_after(conn: sqlite3.Connection, symbol: str, ts_ms: int,
                     horizon_minutes: int = 180, move_pct: float = 0.35,
                     timeframe: str = "1m") -> Dict[str, Any]:
    """注意力时刻之后，是否出现了"有意义的价格移动"。

    基准价 = 注意力时刻当刻（或之前最近一根）的 close。
    移动判定 = 窗口内第一根使 |收益| >= move_pct 的 bar。
    返回 base_close / has_move / move_ts / move_pct / lead_minutes / max_abs_move_pct。
    """
    out: Dict[str, Any] = {
        "symbol": symbol, "base_close": None, "has_move": False, "move_ts": 0,
        "move_at": "", "move_pct": 0.0, "lead_minutes": None,
        "max_abs_move_pct": 0.0, "bars": 0, "timeframe": timeframe,
        "horizon_minutes": horizon_minutes, "move_pct_threshold": move_pct,
    }
    if not ts_ms or not symbol:
        return out
    end_ms = ts_ms + max(1, int(horizon_minutes)) * 60 * 1000
    try:
        bars = conn.execute(
            """
            SELECT ts, close FROM kline_bars
             WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ?
             ORDER BY ts ASC
            """,
            (symbol.upper(), timeframe, ts_ms - 60 * 1000, end_ms),
        ).fetchall()
    except Exception:
        return out
    if not bars:
        return out

    base = None
    for ts, close in bars:
        if ts <= ts_ms and close:
            base = float(close)
    if base is None:
        base = float(bars[0][1] or 0.0)
    if not base:
        return out

    out["base_close"] = base
    out["bars"] = len(bars)
    for ts, close in bars:
        if not close:
            continue
        ret = (float(close) - base) / base * 100.0
        if abs(ret) > abs(out["max_abs_move_pct"]):
            out["max_abs_move_pct"] = round(ret, 4)
        if not out["has_move"] and abs(ret) >= abs(move_pct):
            out["has_move"] = True
            out["move_ts"] = int(ts)
            out["move_at"] = fmt_utc(int(ts))
            out["move_pct"] = round(ret, 4)
            out["lead_minutes"] = int(round((int(ts) - ts_ms) / 60000.0))
    return out


def price_move_before(conn: sqlite3.Connection, symbol: str, ts_ms: int,
                      horizon_minutes: int = 180, move_pct: float = 0.35,
                      timeframe: str = "1m") -> Dict[str, Any]:
    """反向对照：注意力时刻**之前**的窗口里，价格是否已经在动了？

    这是必要的证伪装置 —— 媒体往往是被价格波动触发去写报道的。
    如果绝大多数事件在注意力之前价格就已经动过，那么"注意力领先价格移动"
    就只是反向因果造成的假象（其实是价格先动、媒体后知）。
    两种结果都会如实报告，不做美化。

    返回 prior_move(bool) / prior_move_ts / reverse_lead_minutes（正数=价格先动）。
    """
    out: Dict[str, Any] = {
        "symbol": symbol, "prior_move": False, "prior_move_ts": 0,
        "prior_move_at": "", "prior_move_pct": 0.0,
        "reverse_lead_minutes": None, "bars": 0,
    }
    if not ts_ms or not symbol:
        return out
    start_ms = ts_ms - max(1, int(horizon_minutes)) * 60 * 1000
    try:
        bars = conn.execute(
            """
            SELECT ts, close FROM kline_bars
             WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ?
             ORDER BY ts ASC
            """,
            (symbol.upper(), timeframe, start_ms, ts_ms),
        ).fetchall()
    except Exception:
        return out
    if len(bars) < 2:
        return out
    base = float(bars[0][1] or 0.0)
    if not base:
        return out
    out["bars"] = len(bars)
    for ts, close in bars:
        if not close:
            continue
        ret = (float(close) - base) / base * 100.0
        if abs(ret) >= abs(move_pct):
            out["prior_move"] = True
            out["prior_move_ts"] = int(ts)
            out["prior_move_at"] = fmt_utc(int(ts))
            out["prior_move_pct"] = round(ret, 4)
            out["reverse_lead_minutes"] = int(round((ts_ms - int(ts)) / 60000.0))
            break
    return out




# ─────────────────────────── 主统计 ───────────────────────────
def evaluate_lead(conn: sqlite3.Connection,
                  lookback_hours: Optional[int] = None,
                  min_platforms: Optional[int] = None,
                  move_pct: Optional[float] = None,
                  horizon_minutes: Optional[int] = None,
                  timeframe: Optional[str] = None,
                  ) -> Dict[str, Any]:
    """主统计：注意力事件 → 价格移动 → 领先量。

    返回 {'params', 'summary', 'events', 'note'}。
    样本不足时 events 为空、summary 的命中率为 None —— 不用占位数据冒充结论。
    """
    p = {
        "lookback_hours": int(lookback_hours if lookback_hours is not None
                              else config.CROWD_LEAD_LOOKBACK_HOURS),
        "min_platforms": int(min_platforms if min_platforms is not None
                             else config.CROWD_LEAD_MIN_PLATFORMS),
        "move_pct": float(move_pct if move_pct is not None
                          else config.CROWD_LEAD_MOVE_PCT),
        "horizon_minutes": int(horizon_minutes if horizon_minutes is not None
                               else config.CROWD_LEAD_HORIZON_MINUTES),
        "timeframe": str(timeframe or config.CROWD_LEAD_TIMEFRAME),
    }

    events = collect_attention_events(
        conn, lookback_hours=p["lookback_hours"], min_platforms=p["min_platforms"])
    rows: List[Dict[str, Any]] = []
    leads: List[int] = []
    reverse_leads: List[int] = []
    with_move = 0
    with_price = 0
    with_prior = 0
    for ev in events:
        pm = price_move_after(conn, ev["symbol"], ev["attention_ts"],
                               horizon_minutes=p["horizon_minutes"],
                               move_pct=p["move_pct"], timeframe=p["timeframe"])
        rev = price_move_before(conn, ev["symbol"], ev["attention_ts"],
                                horizon_minutes=p["horizon_minutes"],
                                move_pct=p["move_pct"], timeframe=p["timeframe"])
        if pm["bars"]:
            with_price += 1
        if pm["has_move"]:
            with_move += 1
            if pm["lead_minutes"] is not None:
                leads.append(int(pm["lead_minutes"]))
        if rev.get("prior_move"):
            with_prior += 1
            if rev.get("reverse_lead_minutes") is not None:
                reverse_leads.append(int(rev["reverse_lead_minutes"]))
        ev = dict(ev)
        ev["price"] = pm
        ev["reverse"] = rev
        ev["explain"] = _explain_event(ev, pm, rev, p)
        rows.append(ev)

    hit_rate = (round(with_move / with_price, 4) if with_price else None)
    prior_ratio = (round(with_prior / with_price, 4) if with_price else None)
    fwd_median = (round(statistics.median(leads), 1) if leads else None)
    rev_median = (round(statistics.median(reverse_leads), 1) if reverse_leads else None)

    # ── 干净子集（clean subset）──
    # 剔除「注意力之前价格就已经动过」的事件后，剩下的才是能回答
    # "注意力本身有无增量预测力" 的样本。前瞻信号的真实价值在这里，
    # 而不是被事后追新闻的样本稀释后的总体命中率。
    clean_total = 0
    clean_hit = 0
    clean_leads: List[int] = []
    for ev in rows:
        if ev.get("reverse", {}).get("prior_move"):
            continue
        clean_total += 1
        if ev.get("price", {}).get("has_move"):
            clean_hit += 1
            lm = ev["price"].get("lead_minutes")
            if lm is not None:
                clean_leads.append(int(lm))
    clean_hit_rate = (round(clean_hit / clean_total, 4) if clean_total else None)

    summary = {
        "events_total": len(events),
        "events_with_price": with_price,
        "events_with_move": with_move,
        "hit_rate": hit_rate,
        "avg_lead_minutes": (round(statistics.mean(leads), 1) if leads else None),
        "median_lead_minutes": fwd_median,
        "lead_leads_count": len([x for x in leads if x > 0]),
        "lead_lags_count": len([x for x in leads if x < 0]),
        # 反向对照
        "events_with_prior_move": with_prior,
        "prior_move_ratio": prior_ratio,
        "reverse_median_lead_minutes": rev_median,
        # 干净子集
        "clean_events": clean_total,
        "clean_hit_rate": clean_hit_rate,
        "clean_avg_lead_minutes": (round(statistics.mean(clean_leads), 1)
                                   if clean_leads else None),
        "verdict": _verdict(with_price, with_prior, prior_ratio, fwd_median, rev_median),
        "symbols": sorted({e["symbol"] for e in events}),
        "generated_at": fmt_utc(int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)),
    }
    note = _lead_note(summary, p)
    return {"params": p, "summary": summary, "events": rows, "note": note}


def _verdict(with_price: int, with_prior: int, prior_ratio: Optional[float],
             fwd_median: Optional[float], rev_median: Optional[float]) -> str:
    """判定注意力与价格移动的方向关系（结论必须经得起反向对照）。"""
    if with_price == 0:
        return "no_data"
    if prior_ratio is not None and prior_ratio >= 0.5 and rev_median is not None:
        if fwd_median is None or rev_median >= fwd_median:
            return "attention_lags_price"
    if fwd_median is not None and fwd_median > 0:
        return "attention_leads_price"
    return "inconclusive"


_VERDICT_TEXT = {
    "attention_leads_price": "注意力领先价格移动（反向对照未推翻）",
    "attention_lags_price": "价格先动、注意力后知 —— 注意力不领先，多数情况是媒体追价格",
    "inconclusive": "证据不足，无法判定方向",
    "no_data": "无可对照的行情数据",
}


def _explain_event(ev: Dict[str, Any], pm: Dict[str, Any], rev: Dict[str, Any],
                   p: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append("%s 在 %s UTC 被 %d 个平台（%s）提到 %d 条"
                 % (ev["symbol"], ev["attention_at"], ev["platform_count"],
                    "/".join(ev["platform_list"]), ev["item_count"]))
    if not pm["bars"]:
        parts.append("窗口内无 %s 行情数据，无法验证" % p["timeframe"])
        return "；".join(parts)
    if pm["has_move"]:
        parts.append("其后 %d 分钟出现 %.2f%% 的价格移动（阈值 %.2f%%）→ 注意力领先 %d 分钟"
                     % (pm["lead_minutes"], pm["move_pct"],
                        p["move_pct"], pm["lead_minutes"]))
    else:
        parts.append("其后 %d 分钟内最大波动 %.2f%%，未达阈值 %.2f%% → 未验证到价格跟随"
                     % (p["horizon_minutes"], pm["max_abs_move_pct"], p["move_pct"]))
    # 反向对照（证伪装置）
    if rev.get("prior_move"):
        parts.append("但注意力之前 %d 分钟价格已动过 %.2f%% → 该事件不能证明注意力领先"
                     % (rev.get("reverse_lead_minutes") or 0, rev.get("prior_move_pct") or 0.0))
    else:
        parts.append("注意力之前同长度窗口内价格未达阈值 → 未发现「价格先动」的迹象")
    return "；".join(parts)


def _lead_note(summary: Dict[str, Any], p: Dict[str, Any]) -> str:
    total = summary["events_total"]
    if total == 0:
        return ("回看 %d 小时内没有任何带真实发布时间戳的注意力事件（可能是语料池尚未覆盖"
                "带行情的标的，或采集刚启动）。不做任何推断。"
                % p["lookback_hours"])
    if summary["events_with_price"] == 0:
        return ("识别到 %d 个注意力事件，但都对应不到 %s 行情，无法验证领先关系。"
                % (total, p["timeframe"]))
    verdict = _VERDICT_TEXT.get(summary.get("verdict") or "", "")
    prior_txt = ("其中 %d 个（%.0f%%）在注意力之前价格就已经动过"
                 % (summary["events_with_prior_move"],
                    (summary["prior_move_ratio"] or 0) * 100))
    if summary["events_with_price"] < 5:
        return ("仅 %d 个注意力事件有行情可对照，样本过小，命中率不具备统计意义，"
                "仅作线索参考。%s" % (summary["events_with_price"], verdict))
    return ("%d 个注意力事件中有行情对照 %d 个，其中 %d 个在 %d 分钟内出现 ≥%.2f%% 的价格移动；"
            "%s。判定：%s"
            % (total, summary["events_with_price"], summary["events_with_move"],
               p["horizon_minutes"], p["move_pct"], prior_txt, verdict))



# ─────────────────── 出口 2：先行漏检候选（喂 missed-signals） ───────────────────
def _latest_system_signal(conn: sqlite3.Connection, symbol: str,
                          since_ms: int) -> Optional[Dict[str, Any]]:
    """该标的在 since_ms 之后系统给出的最新决策（用于判断"系统是否已经动手"）。"""
    if not _has_table(conn, "ai_decisions"):
        return None
    since_s = int(since_ms / 1000)
    try:
        row = conn.execute(
            """
            SELECT ts, suggested_action, target_asset, created_at
              FROM ai_decisions
             WHERE UPPER(target_asset) = ? AND ts >= ?
             ORDER BY ts DESC LIMIT 1
            """,
            (symbol.upper(), since_s),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {
        "ts": int(row[0] or 0),
        "suggested_action": str(row[1] or ""),
        "target_asset": str(row[2] or ""),
        "created_at": str(row[3] or ""),
    }


def pending_attention(conn: sqlite3.Connection,
                      lookback_hours: Optional[int] = None,
                      min_platforms: Optional[int] = None,
                      move_pct: Optional[float] = None,
                      horizon_minutes: Optional[int] = None,
                      timeframe: Optional[str] = None,
                      ) -> Dict[str, Any]:
    """先行漏检候选：注意力已经起来，但价格还没动（系统也还没出手）。

    这正是漏检信号里「还没发生异动、但已具备先行条件」的那一类 ——
    传统漏检检测只能等价格异动之后回看，这里能在异动**之前**给出候选。
    """
    res = evaluate_lead(conn, lookback_hours=lookback_hours,
                        min_platforms=min_platforms, move_pct=move_pct,
                        horizon_minutes=horizon_minutes, timeframe=timeframe)
    p = res["params"]
    pending: List[Dict[str, Any]] = []
    for ev in res["events"]:
        pm = ev.get("price") or {}
        if not pm.get("bars"):
            continue          # 没有行情可对照 → 不做判断
        if pm.get("has_move"):
            continue          # 已经兑现了 → 不算「待兑现」
        sig = _latest_system_signal(conn, ev["symbol"], ev["attention_ts"])
        already_acted = bool(sig and str(sig.get("suggested_action") or "").upper()
                              in ("BUY", "SELL"))
        pending.append({
            "symbol": ev["symbol"],
            "attention_ts": ev["attention_ts"],
            "attention_at": ev["attention_at"],
            "platform_count": ev["platform_count"],
            "platform_list": ev["platform_list"],
            "item_count": ev["item_count"],
            "items": ev["items"][:5],
            "max_abs_move_pct": pm.get("max_abs_move_pct", 0.0),
            "move_pct_threshold": p["move_pct"],
            "horizon_minutes": p["horizon_minutes"],
            "system_signal": sig,
            "already_acted": already_acted,
            "explain": ("%s 在 %s UTC 被 %d 个平台（%s）提到 %d 条；其后 %d 分钟价格最大波动 "
                        "%.2f%%，未达 %.2f%% 的「有意义移动」阈值；系统最近决策=%s → "
                        "注意力已起、价格未动，属于**先行漏检候选**"
                        % (ev["symbol"], ev["attention_at"], ev["platform_count"],
                           "/".join(ev["platform_list"]), ev["item_count"],
                           p["horizon_minutes"], pm.get("max_abs_move_pct", 0.0),
                           p["move_pct"],
                           (sig.get("suggested_action") if sig else "无"))),
        })
    res["pending"] = pending
    res["pending_note"] = (
        "%d 个注意力事件里，%d 个尚未兑现为价格移动。"
        % (res["summary"]["events_total"], len(pending))
        + ("" if pending else "当前没有先行候选。"))
    return res


# ─────────────────── 出口 3：归因证据（喂 attribution） ───────────────────
def attribution_evidence(conn: sqlite3.Connection, asset: str, ts_ms: int,
                          window_minutes: int = 240,
                          min_platforms: Optional[int] = None,
                          ) -> Optional[Dict[str, Any]]:
    """给一次行情异动找「之前的注意力证据」。

    在 [ts_ms - window_minutes, ts_ms] 内找该标的的注意力事件，
    返回离异动最近的那条（含领先分钟数），没有就返回 None。
    """
    asset_u = str(asset or "").upper()
    if not asset_u or not ts_ms:
        return None
    mp = int(min_platforms if min_platforms is not None
             else config.CROWD_LEAD_MIN_PLATFORMS)
    lookback_h = max(1, (window_minutes + 120) // 60)
    # require_price_symbol=False —— 归因证据不要求该标的有分钟级行情，
    # 否则 A 股/美股这类没有分钟级 kline 的资产永远拿不到注意力证据。
    #
    # anchor_ms=ts_ms（v318 修）—— 回看窗口必须**锚在被归因的异动时刻**，
    # 而不是 wall-clock now。否则给历史异动做归因时窗口落在「今天往前 N 小时」，
    # 异动当时的注意力条目全部在窗口外 → 归因恒为 None（历史异动永远没有证据）。
    # 锚定到 ts_ms 同时天然满足 no-lookahead：只看异动**之前**的条目。
    events = collect_attention_events(conn, lookback_hours=lookback_h,
                                      min_platforms=mp, require_price_symbol=False,
                                      anchor_ms=int(ts_ms))
    best: Optional[Dict[str, Any]] = None
    for ev in events:
        if ev["symbol"].upper() != asset_u:
            continue
        delta = (ts_ms - ev["attention_ts"]) / 60000.0
        if delta < 0 or delta > window_minutes:
            continue
        if best is None or delta < best["lead_minutes"]:
            best = {
                "symbol": ev["symbol"],
                "attention_ts": ev["attention_ts"],
                "attention_at": ev["attention_at"],
                "lead_minutes": int(round(delta)),
                "platform_count": ev["platform_count"],
                "platform_list": ev["platform_list"],
                "item_count": ev["item_count"],
                "items": ev["items"][:3],
                "kind": "crowd_attention",
                "explain": ("行情异动前 %d 分钟，%s 已被 %d 个独立平台（%s）提及 %d 条"
                            % (int(round(delta)), ev["symbol"], ev["platform_count"],
                               "/".join(ev["platform_list"]), ev["item_count"])),
            }
    return best
