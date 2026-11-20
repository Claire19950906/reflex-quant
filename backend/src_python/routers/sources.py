# -*- coding: utf-8 -*-
"""信息源状态路由（结构切片，2026-11-17）。

从 api_server.py 搬来 2 条端点：
  /api/sources/status（各源在线/离线/报错实时快照）
  /api/sources/alerts（持续异常 toast：error 需连续失败≥2、stale 需 15 分钟未刷新；
     v360 起排除非交易范围源与已确认不可达源，避免误报）
只读 source_status 的内存注册表，O(n)，不碰任何推理链路。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from feeds import source_status

router = APIRouter()


def _to_epoch(v: Any) -> float:
    """把 last_fail_ts / last_ok_ts 统一成浮点 epoch。

    DB 反向加载的时间戳是 ISO 字符串（``YYYY-MM-DD HH:MM:SS``），内存注册表
    里是 time.time() 浮点；snapshot() 只对派生的 secs_since_* 做了解析，
    原始 last_X_ts 仍可能是字符串，排序时对字符串取负号会抛
    ``TypeError: bad operand type for unary -: 'str'``。这里兜底解析成浮点。
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return time.mktime(time.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


@router.get("/api/sources/status")
async def get_sources_status():
    """信息源清单与分布（常驻面板数据源）：每个源在线/离线/报错实时标色。"""
    return {"sources": source_status.snapshot()}


@router.get("/api/sources/alerts")
async def get_sources_alerts():
    """信息源异常提示（前端 toast 用）—— 只列持续异常的 channel：
    - status=error 且 consecutive_failures >= MIN_FAILS（剔除网络抖动）
    - status=stale（已注册但长时间未刷新）
    - 附带 last_fail_ts / secs_since_fail / last_error, 供前端弹窗显示时间和原因
    - 不影响 ai_worker / 任何模型推理（仅查询状态注册表，O(n) 内存读）
    """
    MIN_FAILS = 2  # 至少连续失败 2 次才算"持续异常"
    STALE_LIMIT_S = 900  # 15 分钟未刷新 → 也算 stale
    now = time.time()
    items: List[Dict[str, Any]] = []
    snap = source_status.snapshot()
    # v360: 只报"交易范围内、且没有可用替代源"的异常。
    #   - 韩股/A 股源(000660.KS / SKHYNIX 等) 不在交易范围 → 不提示
    #   - OKX swap: 本机 DNS 解析不了, 但 Bybit 已在稳定替代 → 不算异常
    _OOS = ('.KS', '.KQ', '.SS', '.SZ', 'SKHYNIX', 'US_HYNIX', 'okx_swap:',
            # 需要用户 key 才可用的源：没配 key 属"未启用"，不是"故障"，不该报异常
            'finnhub', 'polygon', 'qinghong_flash',
            # yahoo / yfinance 在本机网络已确认不可达（SSL 必断/被限流），
            # 全链路已改用腾讯+新浪+Binance，这两个源的存量失败不再报异常
            'yahoo:', 'yfinance:',
            # coinglass 需付费 key, 试用 key 从未成功；已由 lsr_feed(Binance/Bybit) 替代
            'coinglass_lsr',
            # fred: 本机没配 FRED_API_KEY, 且 api.stlouisfed.org 在该网络不可达;
            #   美债/宏观实际走 fredgraph CSV(curl) 这条路, 数据本身没断
            # bls: api.bls.gov 对本网络直接 Access Denied（上游封锁, 非本系统故障）
            'fred', 'bls')
    for s in snap:
        _sid = str(s.get("source_id") or "") + " " + str(s.get("label") or "")
        if any(k in _sid for k in _OOS):
            continue
        if s["status"] == "error" and s.get("consecutive_failures", 0) >= MIN_FAILS:
            items.append({
                "source_id": s["source_id"],
                "category": s["category"],
                "label": s["label"],
                "severity": "error",
                "consecutive_failures": s["consecutive_failures"],
                "last_fail_ts": s["last_fail_ts"],
                "secs_since_fail": s["secs_since_fail"],
                "last_error": (s.get("last_error") or "")[:200],
            })
        elif s["status"] == "stale" and (s.get("secs_since_ok") or 0) >= STALE_LIMIT_S:
            items.append({
                "source_id": s["source_id"],
                "category": s["category"],
                "label": s["label"],
                "severity": "stale",
                "consecutive_failures": s.get("consecutive_failures", 0),
                "last_fail_ts": s["last_fail_ts"],
                "last_ok_ts": s["last_ok_ts"],
                "secs_since_fail": s["secs_since_fail"],
                "secs_since_ok": s["secs_since_ok"],
                "last_error": (s.get("last_error") or "")[:200],
            })
    # 严重程度排序: error 优先, stale 次之; 然后按最近失败时间倒序（最新在上）
    # last_fail_ts 可能是 ISO 字符串，先经 _to_epoch 转浮点再取负，避免 TypeError
    items.sort(key=lambda x: (0 if x["severity"] == "error" else 1, -_to_epoch(x["last_fail_ts"])))
    return {
        "ts": now,
        "count": len(items),
        "items": items,
        "min_fails_threshold": MIN_FAILS,
        "stale_limit_s": STALE_LIMIT_S,
    }


# ── v422：源健康度（健康分 + 停更/欠费/失效分类 + 建议）─────────────────────
# 为什么加：/api/sources/status 只有 online/error/stale 三态，分不清
# 「没配 key」「三天前停更」「能出但一半失败」。11-18 实测 49 个源里近 24h
# 只有 8 个在稳定出货，这条接口就是把这个事实量化出来。
import sqlite3 as _sqlite3

from fastapi import Query as _Query

import db as _db
import source_health as _source_health


def _db_source_stats(hours: int, days: int) -> Dict[str, Dict[str, Any]]:
    """按源聚合近 N 小时 / N 天的入库量（只读连接）。"""
    import time as _time
    now = int(_time.time())
    since_h = now - hours * 3600
    since_d = now - days * 86400
    conn = _sqlite3.connect(f"file:{_db.get_db_path()}?mode=ro", uri=True, timeout=30.0)
    try:
        cur = conn.execute(
            """
            SELECT source,
                   SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS n_h,
                   SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS n_d,
                   MAX(ts) AS last_ts
            FROM raw_news
            GROUP BY source
            """,
            (since_h, since_d),
        )
        out: Dict[str, Dict[str, Any]] = {}
        for r in cur.fetchall():
            src = str(r[0] or "")
            if not src:
                continue
            last_ts = float(r[3]) if r[3] else None
            out[src] = {
                "n24": int(r[1] or 0),
                "n7": int(r[2] or 0),
                "last_ts": last_ts,
                "last_at": _time.strftime("%Y-%m-%d %H:%M", _time.localtime(last_ts)) if last_ts else "",
            }
        cur.close()
        return out
    finally:
        conn.close()


@router.get("/api/sources/health")
async def get_sources_health(
    hours: int = _Query(24, ge=1, le=168),
    days: int = _Query(7, ge=1, le=30),
):
    """源健康度面板：每源一个分 + 一个分类 + 一句该怎么办。"""
    import time as _time
    stats = _db_source_stats(hours, days)
    snap = source_status.snapshot()
    rep = _source_health.build_health(snap, stats, now=_time.time())
    rep["window"] = {"hours": hours, "days": days}
    rep["registry_size"] = len(snap)
    return rep
