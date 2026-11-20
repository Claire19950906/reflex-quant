# -*- coding: utf-8 -*-
"""观测层路由（结构切片，2026-11-17）。

从 api_server.py 搬来「观测层」整段 4 条端点：obs/sources、obs/cot、obs/staleness、
obs/llm，外加 _obs_pct / _obs_parse_ts 两个纯函数助手（段外无调用点）。

刻意没搬 /api/debug/mem：它直接读 api_server 内的 7 个模块级缓存对象
（_EVENTS_15D_CACHE / _MOVERS_CACHE 等），搬走就得反向 import，留在原地更合理。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import db

router = APIRouter()


# ── 观测层：把库里已经采到、但页面没露出来的细节拿出来 ──────────────
# 全部只读、不改任何判定；目的是"让系统能看见自己的状态"，而不是改结论。

def _obs_pct(values: List[float], q: float) -> Optional[float]:
    """简易分位数（最近秩法），values 已排序。"""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    idx = int(round(q * (len(values) - 1)))
    return float(values[max(0, min(len(values) - 1, idx))])


def _obs_parse_ts(v: Any) -> Optional[int]:
    """把库里多种写法的时间统一成 epoch 秒。

    feed_health.ts 存的是 '2026-10-06 01:49:54' 文本（不是 unix 整数），
    直接按整数比较会 TypeError/ValueError —— 这里吃 int / float / ISO 文本三种。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    s19 = s[:19].replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(time.mktime(time.strptime(s19, fmt)))
        except Exception:
            continue
    try:
        return int(time.mktime(time.strptime(s19[:10], "%Y-%m-%d")))
    except Exception:
        return None


@router.get("/api/obs/sources", tags=["meta"])
async def get_obs_sources(days: int = 7):
    """每个数据源的真实可用率：成功/失败数、延迟分位、最后成功距今、主要报错。"""
    days = min(max(1, int(days)), 30)

    def _sync() -> Dict[str, Any]:
        since = int(time.time()) - days * 86400
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT source, status, latency_ms, error, ts FROM feed_health"
            ).fetchall()
            st_rows = conn.execute(
                "SELECT source_id, status, last_ok_ts, last_fail_ts, consecutive_failures, "
                "total_ok, total_fail, last_error FROM source_status"
            ).fetchall()
        agg: Dict[str, Dict[str, Any]] = {}
        skipped = 0
        for r in rows:
            ts = _obs_parse_ts(r[4])
            if not ts or ts < since:
                skipped += 1
                continue
            src = str(r[0] or "?")
            a = agg.setdefault(src, {"ok": 0, "fail": 0, "lat": [], "errs": {}, "last_ok": 0, "last_any": 0})
            if str(r[1]) == "success":
                a["ok"] += 1
                if ts > a["last_ok"]:
                    a["last_ok"] = ts
            else:
                a["fail"] += 1
                e = str(r[3] or "")[:70]
                if e:
                    a["errs"][e] = a["errs"].get(e, 0) + 1
            a["last_any"] = max(a["last_any"], ts)
            if r[2]:
                try:
                    a["lat"].append(float(r[2]))
                except (TypeError, ValueError):
                    pass
        now = int(time.time())
        out = []
        for src, a in sorted(agg.items(), key=lambda kv: -(kv[1]["ok"] + kv[1]["fail"])):
            lat = sorted(a["lat"])
            total = a["ok"] + a["fail"]
            out.append({
                "source": src,
                "ok": a["ok"],
                "fail": a["fail"],
                "success_rate": round(a["ok"] / total, 3) if total else None,
                "latency_p50_ms": _obs_pct(lat, 0.5),
                "latency_p95_ms": _obs_pct(lat, 0.95),
                "last_seen_age_s": (now - a["last_any"]) if a["last_any"] else None,
                "last_ok_age_s": (now - a["last_ok"]) if a["last_ok"] else None,
                "top_error": max(a["errs"].items(), key=lambda kv: kv[1])[0] if a["errs"] else "",
            })
        live = [
            {"source": r[0], "status": r[1], "last_ok_ts": r[2], "last_fail_ts": r[3],
             "consecutive_failures": r[4], "total_ok": r[5], "total_fail": r[6],
             "last_error": str(r[7] or "")[:80]}
            for r in st_rows
        ]
        return {"ok": True, "window_days": days, "rows_outside_window": skipped,
                "by_feed_health": out[:40], "source_status": live}

    return await asyncio.to_thread(_sync)


@router.get("/api/obs/cot", tags=["meta"])
async def get_obs_cot(asset: Optional[str] = None, weeks: int = 12):
    """CFTC 持仓报告：商业/非商业净头寸、周度变化、投机指数（黄金/美元/原油）。"""
    weeks = min(max(2, int(weeks)), 52)
    a = (asset or "").strip().upper() or None

    def _sync() -> Dict[str, Any]:
        with db.get_connection() as conn:
            if a:
                rows = conn.execute(
                    "SELECT report_date, asset, cftc_contract_name, open_interest, comm_net, "
                    "noncomm_net, change_noncomm_long, change_noncomm_short, spec_index "
                    "FROM cftc_cot_data WHERE UPPER(asset)=? ORDER BY report_date DESC LIMIT ?",
                    (a, weeks),
                ).fetchall()
            else:
                latest = conn.execute(
                    "SELECT MAX(report_date) FROM cftc_cot_data"
                ).fetchone()[0]
                rows = conn.execute(
                    "SELECT report_date, asset, cftc_contract_name, open_interest, comm_net, "
                    "noncomm_net, change_noncomm_long, change_noncomm_short, spec_index "
                    "FROM cftc_cot_data WHERE report_date=? ORDER BY asset",
                    (latest,),
                ).fetchall()
        items = [
            {"report_date": r[0], "asset": r[1], "contract": r[2], "open_interest": r[3],
             "comm_net": r[4], "noncomm_net": r[5],
             "chg_noncomm_long": r[6], "chg_noncomm_short": r[7], "spec_index": r[8]}
            for r in rows
        ]
        return {"ok": True, "asset": a, "count": len(items), "items": items}

    return await asyncio.to_thread(_sync)


@router.get("/api/obs/staleness", tags=["meta"])
async def get_obs_staleness(limit: int = 30):
    """指标新鲜度：哪些指标已经落后于自己的常规更新节奏。

    口径修正（2026-11-16）：indicator_freshness 的 median_interval_seconds 是
    "抓取间隔"而不是"数据本身多久更新一次"。frankfurter/fred/treasury 这类
    日频发布源抓得很勤（中位 31s），真实数据仍是每天一版 —— 直接按 3×31s 判
    会把它们全部标成 dead（实测 145 条 dead，其中大量是日频源的误报）。
    这里给"慢节奏源"一个 36 小时的地板，只影响**观测层这套判定**，
    不改 indicator_freshness 表本身，也不影响任何交易/判定逻辑。
    """
    limit = min(max(5, int(limit)), 100)
    slow_sources = ("frankfurter", "fred_csv", "fred", "treasury", "treasury_gov",
                    "sge", "sge_silver", "cftc", "cftc_cot", "eia", "eia_api",
                    "fed_h41", "fed", "bls", "coinmetrics")
    slow_floor_s = 36 * 3600

    def _sync() -> Dict[str, Any]:
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT source, asset, indicator, last_ts, age_seconds, median_interval_seconds, status, note "
                "FROM indicator_freshness"
            ).fetchall()
        stale, ok_cnt, relaxed = [], 0, 0
        for r in rows:
            med = r[5] or 0
            age = r[4] or 0
            floor = slow_floor_s if (str(r[0] or "").lower() in slow_sources or not med) else 300
            threshold = max(3 * med if med else 0, floor)
            if age > threshold:
                stale.append({
                    "source": r[0], "asset": r[1], "indicator": r[2],
                    "age_s": int(age), "median_interval_s": int(med) if med else None,
                    "threshold_s": int(threshold),
                    "age_in_thresholds": round(age / threshold, 1) if threshold else None,
                    "status": r[6], "note": str(r[7] or "")[:60],
                })
            else:
                if med and age > 3 * med:
                    relaxed += 1
                ok_cnt += 1
        stale.sort(key=lambda x: -(x["age_in_thresholds"] or 0))
        return {"ok": True, "total": len(rows), "fresh": ok_cnt,
                "stale": len(stale), "relaxed_by_floor": relaxed,
                "items": stale[:limit]}

    return await asyncio.to_thread(_sync)


@router.get("/api/obs/llm", tags=["meta"])
async def get_obs_llm(days: int = 1):
    """LLM 调用真实画像：按 key/用途的成功率与延迟分位（含失败码）。"""
    days = min(max(1, int(days)), 7)

    def _sync() -> Dict[str, Any]:
        since = int(time.time()) - days * 86400
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT key_alias, purpose, latency_ms, status, error_code, tokens_in, tokens_out "
                "FROM ai_call_stats WHERE ts >= ?",
                (since,),
            ).fetchall()
        agg: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            key = f"{r[0] or '?'}/{r[1] or '?'}"
            a = agg.setdefault(key, {"calls": 0, "fail": 0, "lat": [], "tin": 0, "tout": 0, "errs": {}})
            a["calls"] += 1
            if str(r[3]).lower() not in ("ok", "success", "200"):
                a["fail"] += 1
                if r[4]:
                    a["errs"][str(r[4])[:40]] = a["errs"].get(str(r[4])[:40], 0) + 1
            if r[2]:
                try:
                    a["lat"].append(float(r[2]))
                except (TypeError, ValueError):
                    pass
            a["tin"] += int(r[5] or 0)
            a["tout"] += int(r[6] or 0)
        out = []
        for key, a in sorted(agg.items(), key=lambda kv: -kv[1]["calls"]):
            lat = sorted(a["lat"])
            out.append({
                "channel": key, "calls": a["calls"], "fail": a["fail"],
                "fail_rate": round(a["fail"] / a["calls"], 3) if a["calls"] else None,
                "latency_p50_ms": _obs_pct(lat, 0.5),
                "latency_p95_ms": _obs_pct(lat, 0.95),
                "tokens_in": a["tin"], "tokens_out": a["tout"],
                "top_error": max(a["errs"].items(), key=lambda kv: kv[1])[0] if a["errs"] else "",
            })
        return {"ok": True, "window_days": days, "channels": out}

    return await asyncio.to_thread(_sync)
