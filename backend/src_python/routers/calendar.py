# -*- coding: utf-8 -*-
"""经济日历路由（结构切片，2026-11-17）。

从 api_server.py 搬来 3 条端点 + 2 个缓存常量 + 1 个 DB 兜底助手：
  /api/v189/calendar/events-for-asset（±N 小时 ★n+ 日历事件，已公布/未公布分类）
  /api/v275/calendar/impact-by-asset（蓝色事件 → 5m K 线影响验证，5 分钟缓存）
  /api/calendar/status（日历通道自探；金十 MCP 挂了回落到 ForexFactory 日历库）
金十条数抓取走 feeds.platform_news._fetch_jin10_calendar_sync（60s 缓存），
与 /api/news/jin10/calendar 共用同一份缓存，不额外打 MCP。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import config
import db
from feeds.platform_news import _fetch_jin10_calendar_sync

router = APIRouter()


def _api():
    """延迟回引 api_server（_history_sync 等仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/v189/calendar/events-for-asset")
async def calendar_events_for_asset(asset: str = "BTC", hours: int = 24, star_min: int = 2):
    """归因页 K 线专用：拉 ±N 小时 ★{star_min}+ 日历事件，已公布/未公布分类。

    前端 kline-relation 用：把日历事件标到 K 线时间轴（公布时刻）；
    ◆ 颜色按 actual vs consensus surprise 强度；未公布标灰。
    """
    try:
        cal = _fetch_jin10_calendar_sync()
        if cal.get("status") != "ok":
            return {"ok": False, "events": [], "error": (cal.get("error") or "")[:200]}
        items = cal.get("items") or []
        now_dt = datetime.now(config.TZ_SHANGHAI)
        events = []
        for it in items:
            try:
                star = int(it.get("star") or 0)
            except (TypeError, ValueError):
                star = 0
            if star < star_min:
                continue
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            pub_time_full = str(it.get("pub_time") or "").strip()
            if not pub_time_full:
                continue
            pub_dt = config.parse_time_to_bj(pub_time_full)
            if pub_dt is None:
                continue
            age_h = (now_dt - pub_dt).total_seconds() / 3600.0
            if age_h < -hours or age_h > hours:
                continue
            actual_s = str(it.get("actual") or "").strip()
            consensus_s = str(it.get("consensus") or "").strip()
            previous_s = str(it.get("previous") or "").strip()
            is_released = bool(actual_s)
            surprise = ""
            if is_released and consensus_s:
                try:
                    a = float(actual_s.replace("%", "").replace(",", ""))
                    c = float(consensus_s.replace("%", "").replace(",", ""))
                    surprise = f"{a - c:+.3f}"
                except (TypeError, ValueError):
                    surprise = ""
            events.append({
                "ts_bj": int(pub_dt.timestamp()),
                "ts_iso": pub_dt.isoformat(),
                "title": title[:80],
                "star": star,
                "actual": actual_s or None,
                "consensus": consensus_s or None,
                "previous": previous_s or None,
                "is_released": is_released,
                "surprise": surprise,
                "age_hours": round(age_h, 2),
            })
        events.sort(key=lambda e: e["ts_bj"])
        return {"ok": True, "events": events, "count": len(events),
                "asset": asset, "horizon_hours": hours, "star_min": star_min}
    except Exception as exc:
        return {"ok": False, "events": [], "error": f"{type(exc).__name__}: {exc}"}


_CAL_IMPACT_CACHE: Dict[str, Dict[str, Any]] = {}


_CAL_IMPACT_TTL_S = 300


@router.get("/api/v275/calendar/impact-by-asset")
async def calendar_impact_by_asset(
    asset: str = "BTC",
    hours: int = 24,
    star_min: int = 3,
    window_min: int = 30,
):
    """v275: 蓝色事件 → K 线影响验证 (归因页专用)。

    返回每个事件追加 kline_impact 字段:
      pre_close / max_high / min_low / impact_pct / direction /
      severity / verdict / has_data
    """
    asset_up = (asset or "").strip().upper()
    if asset_up == "OIL":
        asset_up = "WTI"
    elif asset_up == "GOLD":
        asset_up = "XAU"

    cache_key = f"{asset_up}:{hours}:{star_min}:{window_min}"
    now_m = time.monotonic()
    hit = _CAL_IMPACT_CACHE.get(cache_key)
    if hit and now_m - hit["ts"] < _CAL_IMPACT_TTL_S:
        return hit["data"]

    cal_resp = await calendar_events_for_asset(asset_up, hours=hours, star_min=star_min)
    events = (cal_resp or {}).get("events") or []

    try:
        hist = await asyncio.to_thread(_api()._history_sync, asset_up, "5m", 288, 2)
        raw_bars = hist.get("bars") or []
    except Exception:
        raw_bars = []

    bars_by_ts: Dict[int, Dict[str, Any]] = {}
    for b in raw_bars:
        try:
            t = int(b.get("time") or 0)
            if t > 0:
                bars_by_ts[t] = b
        except (TypeError, ValueError):
            continue
    sorted_ts = sorted(bars_by_ts.keys())

    out_events: List[Dict[str, Any]] = []
    for ev in events:
        ev_ts = int(ev.get("ts_bj") or 0)
        if not ev_ts:
            out_events.append({**ev, "kline_impact": {"has_data": False}})
            continue

        pre_close: Optional[float] = None
        for t in sorted_ts:
            if t <= ev_ts:
                pre_close = bars_by_ts[t].get("close")
            else:
                break
        if pre_close is None or pre_close <= 0:
            out_events.append({**ev, "kline_impact": {"has_data": False,
                                                       "reason": "no_pre_close"}})
            continue

        window_end = ev_ts + window_min * 60
        max_high = pre_close
        min_low = pre_close
        found_post = False
        for t in sorted_ts:
            if t <= ev_ts:
                continue
            if t > window_end:
                break
            bar = bars_by_ts[t]
            h = bar.get("high")
            l = bar.get("low")
            if h is not None and h > max_high:
                max_high = h
            if l is not None and l < min_low:
                min_low = l
            found_post = True

        if not found_post:
            out_events.append({**ev, "kline_impact": {"has_data": False,
                                                       "reason": "no_post_bars",
                                                       "pre_close": pre_close}})
            continue

        up_pct = (max_high - pre_close) / pre_close
        dn_pct = (pre_close - min_low) / pre_close
        impact_pct = max(up_pct, dn_pct)
        direction = "up" if up_pct > dn_pct else ("down" if dn_pct > up_pct else "neutral")

        if impact_pct >= 0.008:
            severity = "high"
        elif impact_pct >= 0.003:
            severity = "medium"
        else:
            severity = "low"

        surprise_s = str(ev.get("surprise") or "").strip()
        verdict = "neutral"
        try:
            if surprise_s and impact_pct >= 0.002:
                sv = float(surprise_s)
                if sv > 0 and direction == "up":
                    verdict = "follow"
                elif sv < 0 and direction == "down":
                    verdict = "follow"
                elif sv > 0 and direction == "down":
                    verdict = "reverse"
                elif sv < 0 and direction == "up":
                    verdict = "reverse"
        except (TypeError, ValueError):
            verdict = "neutral"

        if not surprise_s:
            if impact_pct >= 0.003:
                verdict = "follow"
            elif impact_pct >= 0.001:
                verdict = "neutral"
            else:
                verdict = "no_reaction"

        out_events.append({
            **ev,
            "kline_impact": {
                "has_data": True,
                "pre_close": round(pre_close, 6),
                "max_high": round(max_high, 6),
                "min_low": round(min_low, 6),
                "impact_pct": round(impact_pct * 100, 4),
                "direction": direction,
                "severity": severity,
                "verdict": verdict,
                "window_min": window_min,
                "up_pct": round(up_pct * 100, 4),
                "dn_pct": round(dn_pct * 100, 4),
            },
        })

    payload = {
        "ok": True,
        "asset": asset_up,
        "horizon_hours": hours,
        "star_min": star_min,
        "window_min": window_min,
        "events": out_events,
        "count": len(out_events),
        "checked_at": int(time.time()),
    }
    _CAL_IMPACT_CACHE[cache_key] = {"ts": now_m, "data": payload}
    return payload


def _fill_calendar_from_db(out: Dict[str, Any],
                           note: str = "金十通道不可用，已回落到 ForexFactory 日历库") -> None:
    """用本地 macro_calendar 补 events。

    金十 MCP 有两种失败形态：报错，和"成功但 0 条"。两种都不该让前端横幅空着——
    ForexFactory 的日历一直写在这张表里，够用就先给用户看。
    """
    try:
        now_ts = time.time()
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT event_time, country, title, star, forecast, previous"
                " FROM macro_calendar WHERE star>=? AND event_time>? AND event_time<=?"
                " ORDER BY event_time LIMIT 50",
                (out["star_threshold"], int(now_ts),
                 int(now_ts + float(out["horizon_hours"]) * 3600)),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return
        out["events"] = [
            {
                "time": time.strftime("%m-%d %H:%M", time.localtime(et)),
                "star": int(star or 0),
                "title": str(title or "")[:80],
                "forecast": fc or "",
                "previous": prev or "",
                "country": country or "",
            }
            for et, country, title, star, fc, prev in rows
        ]
        first = out["events"][0]
        out["count"] = len(out["events"])
        out["ok"] = True
        out["sample_event"] = f"{first['time']} \u2605{first['star']} {first['title']}"[:120]
        out["fallback_source"] = "macro_calendar(ff)"
        out["note"] = note
    except Exception as exc:
        out["fallback_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"


@router.get("/api/calendar/status")
async def get_calendar_status():
    """日历通道状态（前端可观测「日历提示是否成功注入」）。

    自探式（不依赖 engine 进程内存 —— Python 模块 dict 不跨进程共享）：
    1) 调 fetch_jin10_calendar 探测 MCP 通道；
    2) 调 upcoming_events 算出 ★2+ 24h 事件数。
    """
    out: Dict[str, Any] = {
        "ok": False,
        "count": 0,
        "error": "",
        "sample_event": "",
        "events": [],
        "quota_exhausted": False,
        "star_threshold": 2,
        "horizon_hours": 24,
        "checked_at": time.time(),
    }
    try:
        # 复用 /api/news/jin10/calendar 的 60s 缓存（不打 MCP）
        cal = _fetch_jin10_calendar_sync()
        err = (cal.get("error") or "").strip()
        if err or cal.get("status") != "ok":
            out["quota_exhausted"] = ("上限" in err or "已达" in err or "limit" in err.lower())
            out["error"] = err[:200] or f"status={cal.get('status')}"
            # 金十 MCP 额度用尽时原逻辑直接给 count=0，前端横幅就一片空白。
            # 但 macro_calendar 里 ForexFactory 的日历一直在写，有数据就该用。
            _fill_calendar_from_db(out, note="金十 MCP 不可用，已回落到 ForexFactory 日历库")
            return out
        # 拿 events 用 items 计算 ★2+ 24h
        import time as _t
        now_ts = _t.time()
        events_count = 0
        sample = ""
        events_list: list = []
        for it in (cal.get("items") or []):
            try:
                star = int(it.get("star") or 0)
            except (TypeError, ValueError):
                star = 0
            if star < out["star_threshold"]:
                continue
            actual = (it.get("actual") or "")
            if actual not in ("", None):
                continue  # 已公布
            pub = str(it.get("pub_time") or "").strip()
            dt = config.parse_time_to_bj(pub)
            if dt is None:
                continue
            ts = dt.timestamp()
            if ts <= now_ts or ts > now_ts + float(out["horizon_hours"]) * 3600:
                continue
            if not sample:
                sample = f"{pub[:16]} ★{star} {it.get('title', '')[:60]}"[:120]
            events_list.append({
                "time": dt.strftime("%m-%d %H:%M"),
                "star": star,
                "title": str(it.get("title", "") or "")[:80],
                "forecast": it.get("forecast", ""),
                "previous": it.get("previous", ""),
            })
            events_count += 1
        out["count"] = events_count
        out["ok"] = events_count > 0
        out["sample_event"] = sample
        out["events"] = events_list
        # 金十回"成功但 0 条"这种更隐蔽的情况也要回落：
        # 前端要的是"未来 24h 有没有值得看的会"，不是"金十通道通不通"。
        if events_count == 0:
            _fill_calendar_from_db(out)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return out
