# -*- coding: utf-8 -*-
"""discovery 发现/探索域路由（结构切片，2026-11-17）。

从 api_server.py 整段搬来 /api/discovery/* 端点（v270+ 探索研究），连带三个已读状态
助手（_ensure_read_state_table / _get_last_read / _set_last_read）和 DDL 常量 —— 这三个
助手在本段内自用、段外无调用点，所以一并搬走，不留回引。

逻辑逐字未改，只把 `@app.` 换成 `@router.`；块内原来的 try/except discovery 导入
收敛成模块级 import。段内那条已被注释掉的旧 fed-watch 注解照原样保留。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException

import db
import discovery

router = APIRouter()


# 段 0 · 发现/探索研究(v270+) — 5 个端点
# 不修改主路径 / ai_worker / reflection_ontology / db.py;只读启发式 + 提案
# ─────────────────────────────────────────────────────────────────────────────
discovery_mod = discovery


@router.get("/api/discovery/fed-impact", tags=["discovery"])
async def discovery_fed_impact():
    """Fed 路径 vs 跨资产历史相关性(启发式)。

    v496 修复：`fed_impact_handler` 是同步函数（内部只有 sqlite 读 + 相关性计算），
    这里 `await` 它只会拿到 TypeError: 'dict' object can't be awaited → 面板报错。
    改成 `asyncio.to_thread`：既保留 async 端点，也不让同步计算阻塞事件循环。
    """
    try:
        return await asyncio.to_thread(discovery_mod.fed_impact_handler)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/summary", tags=["discovery"])
async def discovery_summary(period: str = "24h"):
    """v302+ /discover 页 L0 概览条数据 (4 张卡 + 8 个 pillar + 闭环统计)。"""
    try:
        return await discovery_mod.discovery_summary_handler(period)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/fed-watch", tags=["discovery"])
async def discovery_fed_watch():
    """v276: Fed 全景监控 — 当前路径 + 下次 FOMC + 7d 事件日历 + 自适应阈值 + 历史相关性。

    数据源(只读聚合):
      - fed_rate_expectation 当前路径(2Y-EFFR 推导)
      - macro_calendar 下次 FOMC + 7d Fed 事件
      - external_indicators VIX(自适应阈值)
      - treasury_yield_feed 收益率曲线(13W/5Y/10Y/30Y)
      - fed_rate_history 24h 时序
      - kline_bars BTC/SPX/DXY/GLD 历史相关性回测
    """
    try:
        import fed_watch as _fw
        return await asyncio.to_thread(_fw.fetch_fed_watch)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# v2026-10-08: 周度观察 — ICSA + Treasury Auction 摘要（修复发现页 weekly-watch 404）
# 数据源: external_indicators (ICSA 由 FRED 周期 ingest), treasury_auction 摘要尚未 ingest
# 前端 weekly-observations-panel 期望 status ∈ {live, pending, error, not_implemented}
@router.get("/api/discovery/weekly-watch", tags=["discovery"])
def discovery_weekly_watch():
    """周度观察 — 月度数据前的方向校验 (ICSA + Treasury Auction 摘要)."""
    items: List[Dict[str, Any]] = []

    # ── 1. ICSA (Initial Claims, Seasonally Adjusted) ─────────────────
    icsa_item: Dict[str, Any] = {
        "key": "icsa",
        "title": "美国初请失业金 (ICSA)",
        "source": "FRED · Department of Labor",
        "release_day_cn": "每周四 20:30 (北京) — 上上周数据",
        "meaning": "高频就业的先行观察 · 与非农互证",
        "hint": ("连续 4 周走高且 > 30 万 = 就业松动信号；"
                 "连续下行 = 就业仍强 (给 Fed 加息底气)。"),
        "link": "https://fred.stlouisfed.org/series/ICSA",
    }
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT value, ts, source FROM external_indicators "
                "WHERE indicator = 'ICSA' ORDER BY ts DESC LIMIT 5"
            ).fetchall()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if len(rows) >= 2:
            latest = float(rows[0]["value"])
            prev = float(rows[1]["value"])
            delta = round(latest - prev, 0)
            ma4 = None
            if len(rows) >= 4:
                ma4 = round(
                    sum(float(r["value"]) for r in rows[:4]) / 4, 0
                )
            icsa_item.update({
                "status": "live",
                "latest": latest,
                "previous": prev,
                "delta": delta,
                "ma4": ma4,
                "ts": int(rows[0]["ts"]),
                "source_detail": str(rows[0]["source"] or "fred"),
            })
        elif len(rows) == 1:
            icsa_item.update({
                "status": "pending",
                "latest": float(rows[0]["value"]),
                "ts": int(rows[0]["ts"]),
                "reason": "external_indicators 表中 ICSA 只有 1 行, 等待下次 ingest 累积 ≥ 2 行才能计算 delta",
            })
        else:
            icsa_item.update({
                "status": "pending",
                "reason": "external_indicators 表中无 ICSA 记录 (FRED 配置后启动 ingest 即可)",
            })
    except Exception as exc:
        icsa_item.update({
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        })
    items.append(icsa_item)

    # ── 2. Treasury Auction (尚未 ingest, 先占位) ─────────────────────
    # v300.5 P0+：状态从 "pending"(语义"等下次 ingest"=ICSA 单行)
    # 改为 "not_implemented" —— 这是"压根没接"的状态,与 pending 明确区分。
    # TODO: 接 US Treasury Quarterly Refunding RSS + WSFS feed
    #       主要关注: bid-to-cover, tail (中标利率 vs WI), indirect ratio
    items.append({
        "key": "auction",
        "title": "美债拍卖 (Treasury Auction)",
        "source": "US Treasury · Quarterly Refunding + WSFS",
        "release_day_cn": "每周 — 周一/二/三/四多档 (2Y/5Y/7Y/10Y/30Y/20Y 滚动)",
        "meaning": "美债供给压力 — 长债上行风险的关键变量",
        "hint": ("tail (中标利率 - WI 预期 ≥ 2bp) + indirect ratio 下行 = 需求疲软, "
                 "长债上行压力加大; 反之需求强劲, 长债支撑。"),
        "status": "not_implemented",
        "reason": "财政部拍卖结果尚未自动 ingest; 接 US Treasury Quarterly Refunding RSS / WSFS feed 后可上线",
        "link": "https://home.treasury.gov/resource-center/data-chart-center/quarterly-refunding",
    })

    return {
        "ok": True,
        "ts": int(time.time()),
        "items": items,
        "note": "周度观察 = 月度数据前的方向校验; 1-2 个数据源未上线时其它项仍可用",
    }


@router.get("/api/discovery/unread-alerts", tags=["discovery"])
def discovery_unread_alerts():
    """v276: 发现页 in-app 未读异动计数(给 banner 红点用)。

    返回 5 类未读数 + 每类最新一条详情 + 最后清零时间戳:
      1. fed_anomalies: fed_notify_dedup 中比 last_read_at 新的异动
      2. cross_asset_high: abnormal_attributions 中比 last_read_at 新的高危多因子
      3. cycle_signals: cycle_reversal_signals 中比 last_read_at 新的已推送信号
      4. fed_pre_event: 未来 24h 重大 Fed 相关事件(未按 last_read_at 过滤)
      5. missed_signals: ai_decisions.missed_signal=1 中比 last_read_at 新的漏检

    v276+: 引入 discovery_alerts_read_state 表 — 每个 section 一个 last_read_at,
    用户 mark-alerts-read 后,红点只会显示比上次标记更新的记录,不会永远累积。
    任一子查询失败 → 那一项给 ok=false 但不影响其它项
    """
    import db as _db
    _ensure_read_state_table()  # v276+
    out: Dict[str, Any] = {"ok": True, "sections": {}, "ts": int(time.time())}
    today_start = int(time.time()) - 86400

    def _count(sql: str, params: tuple = ()) -> int:
        conn = sqlite3.connect(_db.get_db_path())
        try:
            return int(conn.execute(sql, params).fetchone()[0] or 0)
        finally:
            try: conn.close()
            except Exception: pass

    # 1) Fed 异动
    try:
        # fed_notify_dedup 表只在 fed_rate_notifier 推送异常时才创建,这里主动 ensure
        conn = sqlite3.connect(_db.get_db_path())
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fed_notify_dedup ("
                " key TEXT PRIMARY KEY, ts INTEGER"
                ")"
            )
        finally:
            try: conn.close()
            except Exception: pass
        n = _count("SELECT COUNT(*) FROM fed_notify_dedup WHERE ts >= ?", (today_start,))
        last_row = _count("SELECT MAX(ts) FROM fed_notify_dedup WHERE ts >= ?", (today_start,)) or 0
        # v276+: 用 last_read_at 过滤未读(避免累积)
        last_read_fed = _get_last_read("fed_anomalies")
        n_unread = _count(
            "SELECT COUNT(*) FROM fed_notify_dedup WHERE ts > ?",
            (last_read_fed,),
        )
        out["sections"]["fed_anomalies"] = {
            "ok": True, "count": n_unread, "today_total": n,
            "last_ts": int(last_row), "last_read_at": last_read_fed,
        }
    except Exception as exc:
        out["sections"]["fed_anomalies"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 2) 跨资产高严重度
    try:
        last_read_ca = _get_last_read("cross_asset_high")
        n_unread = _count(
            "SELECT COUNT(*) FROM abnormal_attributions "
            "WHERE anchor_ts > ? AND severity_hint >= 2 "
            "AND signal_kind IN ('multi_factor','cross_asset','multi_asset')",
            (last_read_ca,),
        )
        out["sections"]["cross_asset_high"] = {
            "ok": True, "count": n_unread, "last_read_at": last_read_ca,
        }
    except Exception as exc:
        out["sections"]["cross_asset_high"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 3) 周期反转已推送信号
    try:
        last_read_cs = _get_last_read("cycle_signals")
        n_unread = _count(
            "SELECT COUNT(*) FROM cycle_reversal_signals "
            "WHERE ts > ? AND sent = 1",
            (last_read_cs,),
        )
        out["sections"]["cycle_signals"] = {
            "ok": True, "count": n_unread, "last_read_at": last_read_cs,
        }
    except Exception as exc:
        out["sections"]["cycle_signals"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 4) 未来 24h Fed 事件 (从 fed_pre_event_dedup 看上次推送时间,从 macro_calendar 看未来事件)
    try:
        # 看 dedup 表里 next_run_at 字段作为下次推送时机;否则粗略算"未来 24h 事件数 - 已推"
        conn = sqlite3.connect(_db.get_db_path())
        try:
            horizon_end = int(time.time()) + 86400
            upcoming = conn.execute(
                "SELECT COUNT(*) FROM macro_calendar "
                "WHERE event_time >= ? AND event_time <= ? "
                "AND (LOWER(title) LIKE '%fomc%' OR LOWER(title) LIKE '%fed%' "
                "OR LOWER(title) LIKE '%powell%' OR LOWER(title) LIKE '%cpi%' "
                "OR LOWER(title) LIKE '%pce%' OR LOWER(title) LIKE '%nfp%' "
                "OR LOWER(title) LIKE '%nonfarm%' OR LOWER(title) LIKE '%unemployment%') "
                "AND impact IN ('high', 'medium', 'med')",
                (int(time.time()), horizon_end),
            ).fetchone()[0]
            out["sections"]["fed_pre_event"] = {"ok": True, "upcoming_24h": int(upcoming or 0)}
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        out["sections"]["fed_pre_event"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 5) 漏检信号 (missed_signal=1 且 ts > last_read_at)
    try:
        last_read_ms = _get_last_read("missed_signals")
        # ai_decisions 表可能没有 ts 列;这里通过 created_at 列判断(找最近的 schema)
        conn = sqlite3.connect(_db.get_db_path())
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()]
            ts_col = "ts" if "ts" in cols else ("created_at" if "created_at" in cols else None)
            if ts_col:
                if ts_col == "ts":
                    n = conn.execute(
                        "SELECT COUNT(*) FROM ai_decisions WHERE ts > ? AND missed_signal = 1",
                        (last_read_ms,),
                    ).fetchone()[0]
                else:
                    # created_at 是 'YYYY-MM-DD HH:MM:SS' 字符串
                    if last_read_ms > 0:
                        from datetime import datetime as _dt
                        cutoff_str = _dt.fromtimestamp(last_read_ms).strftime("%Y-%m-%d %H:%M:%S")
                        n = conn.execute(
                            "SELECT COUNT(*) FROM ai_decisions "
                            "WHERE created_at > ? AND missed_signal = 1",
                            (cutoff_str,),
                        ).fetchone()[0]
                    else:
                        today_str = time.strftime("%Y-%m-%d", time.localtime())
                        n = conn.execute(
                            "SELECT COUNT(*) FROM ai_decisions "
                            "WHERE created_at LIKE ? AND missed_signal = 1",
                            (today_str + "%",),
                        ).fetchone()[0]
                out["sections"]["missed_signals"] = {
                    "ok": True, "count": int(n or 0),
                    "last_read_at": last_read_ms,
                }
            else:
                out["sections"]["missed_signals"] = {"ok": False, "error": "no ts column"}
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        out["sections"]["missed_signals"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 汇总总数(用于 banner 红点)
    total = 0
    for k, v in out["sections"].items():
        if v.get("ok") and "count" in v:
            total += int(v["count"] or 0)
        elif v.get("ok") and "upcoming_24h" in v:
            # pre_event 不算 unread(那是未来事件提示),不计入红点
            pass
    out["total_unread"] = total
    return out


# v276+: 各 section 已读时间戳(给一键已读用)
_DISCOVERY_ALERTS_READ_DDL = """
CREATE TABLE IF NOT EXISTS discovery_alerts_read_state (
    section TEXT PRIMARY KEY,
    last_read_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""
_DISCOVERY_ALERTS_SECTIONS = (
    "fed_anomalies", "cross_asset_high", "cycle_signals",
    "missed_signals", "fed_pre_event",
)


def _ensure_read_state_table() -> None:
    """v276+: 首次访问时建表。"""
    import db as _db
    conn = sqlite3.connect(_db.get_db_path())
    try:
        conn.execute(_DISCOVERY_ALERTS_READ_DDL)
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass


def _get_last_read(section: str) -> int:
    """读 section 的最后已读时间戳(epoch s);无记录 → 0。"""
    import db as _db
    conn = sqlite3.connect(_db.get_db_path())
    try:
        row = conn.execute(
            "SELECT last_read_at FROM discovery_alerts_read_state WHERE section=?",
            (section,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        try: conn.close()
        except Exception: pass


def _set_last_read(section: str, ts: int) -> None:
    """upsert section 的已读时间戳。"""
    import db as _db
    conn = sqlite3.connect(_db.get_db_path())
    try:
        conn.execute(
            "INSERT INTO discovery_alerts_read_state (section, last_read_at, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(section) DO UPDATE SET last_read_at=excluded.last_read_at, "
            "updated_at=excluded.updated_at",
            (section, int(ts), int(time.time())),
        )
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass


@router.post("/api/discovery/mark-alerts-read", tags=["discovery"])
async def discovery_mark_alerts_read(payload: Dict[str, Any] = Body(default={})):
    """v276+: 标一个或全部 section 为已读。body: {"section": "fed_anomalies"|"all"}。

    返回: {"ok": true, "marked": [...], "ts": epoch}
    """
    _ensure_read_state_table()
    section = (payload.get("section") or "all").strip()
    now_ts = int(time.time())
    if section == "all":
        for s in _DISCOVERY_ALERTS_SECTIONS:
            _set_last_read(s, now_ts)
        marked = list(_DISCOVERY_ALERTS_SECTIONS)
    elif section in _DISCOVERY_ALERTS_SECTIONS:
        _set_last_read(section, now_ts)
        marked = [section]
    else:
        return {"ok": False, "error": f"unknown section: {section}"}
    return {"ok": True, "marked": marked, "ts": now_ts}


@router.get("/api/discovery/unread-alerts/details", tags=["discovery"])
def discovery_unread_alerts_details(limit: int = 5):
    """v276+: 返回每个 section top N 条未读详情,供前端 modal 渲染。

    按 discovery_alerts_read_state 表过滤 — 只返回比 last_read_at 新的记录。
    """
    import db as _db
    _ensure_read_state_table()
    out: Dict[str, Any] = {"ok": True, "sections": {}, "ts": int(time.time())}
    cap = max(1, min(int(limit), 20))

    def _conn():
        return sqlite3.connect(_db.get_db_path())

    # 1) fed_anomalies
    try:
        last = _get_last_read("fed_anomalies")
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT key, ts FROM fed_notify_dedup "
                "WHERE ts > ? ORDER BY ts DESC LIMIT ?",
                (last, cap),
            ).fetchall()
            items = []
            for r in rows:
                key = r[0]; ts = int(r[1])
                parts = (key or "").split("|")
                items.append({
                    "key": key, "ts": ts,
                    "asset": parts[0] if len(parts) > 0 else "?",
                    "metric": parts[1] if len(parts) > 1 else "?",
                    "direction": parts[2] if len(parts) > 2 else "?",
                    "threshold_bp": parts[3] if len(parts) > 3 else "?",
                    "change_bp": parts[4] if len(parts) > 4 else "?",
                    "regime": parts[5] if len(parts) > 5 else "static",
                })
            out["sections"]["fed_anomalies"] = {
                "ok": True, "count": len(items), "items": items,
                "last_read_at": last,
            }
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        out["sections"]["fed_anomalies"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 2) cross_asset_high
    try:
        last = _get_last_read("cross_asset_high")
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT id, asset, severity_hint, signal_kind, summary, anchor_ts "
                "FROM abnormal_attributions "
                "WHERE anchor_ts > ? AND severity_hint >= 2 "
                "AND signal_kind IN ('multi_factor','cross_asset','multi_asset') "
                "ORDER BY anchor_ts DESC LIMIT ?",
                (last, cap),
            ).fetchall()
            items = [
                {
                    "id": int(r[0]), "asset": r[1],
                    "severity": int(r[2]), "signal_kind": r[3],
                    "summary": (r[4] or "")[:160], "ts": int(r[5]),
                }
                for r in rows
            ]
            out["sections"]["cross_asset_high"] = {
                "ok": True, "count": len(items), "items": items,
                "last_read_at": last,
            }
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        out["sections"]["cross_asset_high"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 3) cycle_signals
    try:
        last = _get_last_read("cycle_signals")
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT id, asset, direction, prob, votes, days_to, hurst, rsi, phase, ts "
                "FROM cycle_reversal_signals "
                "WHERE ts > ? AND sent = 1 ORDER BY ts DESC LIMIT ?",
                (last, cap),
            ).fetchall()
            items = [
                {
                    "id": int(r[0]), "asset": r[1], "direction": r[2],
                    "prob": round(float(r[3] or 0), 3),
                    "votes": int(r[4] or 0),
                    "days_to": r[5],
                    "hurst": round(float(r[6] or 0), 3) if r[6] is not None else None,
                    "rsi": round(float(r[7] or 0), 2) if r[7] is not None else None,
                    "phase": r[8], "ts": int(r[9]),
                }
                for r in rows
            ]
            out["sections"]["cycle_signals"] = {
                "ok": True, "count": len(items), "items": items,
                "last_read_at": last,
            }
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        out["sections"]["cycle_signals"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 4) fed_pre_event
    try:
        last = _get_last_read("fed_pre_event")
        conn = _conn()
        try:
            # macro_calendar 实际列名是 country(非 region)
            rows = conn.execute(
                "SELECT event_time, title, impact, country "
                "FROM macro_calendar "
                "WHERE event_time >= ? AND event_time <= ? "
                "AND (LOWER(title) LIKE '%fomc%' OR LOWER(title) LIKE '%fed%' "
                "OR LOWER(title) LIKE '%powell%' OR LOWER(title) LIKE '%cpi%' "
                "OR LOWER(title) LIKE '%pce%' OR LOWER(title) LIKE '%nfp%' "
                "OR LOWER(title) LIKE '%nonfarm%' OR LOWER(title) LIKE '%unemployment%') "
                "AND impact IN ('high', 'medium', 'med') "
                "ORDER BY event_time ASC LIMIT ?",
                (int(time.time()), int(time.time()) + 86400, cap),
            ).fetchall()
            items = [
                {
                    "event_time": int(r[0]),
                    "title": (r[1] or "")[:120],
                    "impact": r[2], "region": r[3],
                }
                for r in rows
            ]
            out["sections"]["fed_pre_event"] = {
                "ok": True, "upcoming_24h": len(items), "items": items,
                "last_read_at": last,
            }
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        out["sections"]["fed_pre_event"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 5) missed_signals
    try:
        last = _get_last_read("missed_signals")
        conn = _conn()
        try:
            # ai_decisions 实际列名是 target_asset(非 symbol)
            if last > 0:
                rows = conn.execute(
                    "SELECT id, target_asset, suggested_action, sentiment_score, ts "
                    "FROM ai_decisions "
                    "WHERE missed_signal = 1 AND ts > ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (last, cap),
                ).fetchall()
            else:
                today_str = time.strftime("%Y-%m-%d", time.localtime())
                rows = conn.execute(
                    "SELECT id, target_asset, suggested_action, sentiment_score, ts "
                    "FROM ai_decisions "
                    "WHERE missed_signal = 1 AND created_at LIKE ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (today_str + "%", cap),
                ).fetchall()
            items = [
                {
                    "id": int(r[0]), "symbol": r[1],
                    "decision": r[2], "ai_score": r[3],
                    "ts": int(r[4] or 0),
                }
                for r in rows
            ]
            out["sections"]["missed_signals"] = {
                "ok": True, "count": len(items), "items": items,
                "last_read_at": last,
            }
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        out["sections"]["missed_signals"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return out



@router.get("/api/health/data-gaps", tags=["health"])
async def health_data_gaps():
    """数据缺口清单（v497）：发现页顶部红黄条的数据源。

    与 /api/health 的分工：/api/health 说的是"某项资产的价格源新鲜度"，
    这里说的是"系统级缺口"——黄金基本面 0 分量、关键宏观序列断供、
    LSR 无样本、异常信号重复率、影子账本未结算、背离样本不足。
    """
    try:
        return await asyncio.to_thread(discovery_mod.data_gaps_handler)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/asset-scout", tags=["discovery"])
async def discovery_asset_scout():
    """强信息驱动资产扫描(消息股/山寨币/技术超跌)。

    v496 修复：同 fed-impact —— handler 是同步函数，`await` 会 TypeError。
    改走线程池（该扫描首次约 50s，有 600s 缓存，不能占住事件循环）。
    """
    try:
        return await asyncio.to_thread(discovery_mod.asset_scout_handler)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/undervalued-detector", tags=["discovery"])
async def discovery_undervalued_detector():
    """v227 P1-2：发现被严重低估 / 刚被严重砸的标的（类似海力士）。

    启发式评分：7d 跌幅 + 30d 跌幅 + cycle=accumulation + reversal_prob≥0.5 + 情绪转暖。
    返回 top 30 按 undervalued_score 排序。
    """
    try:
        return await discovery_mod.undervalued_detector_handler()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/cross-asset-rankings", tags=["discovery"])
async def discovery_cross_asset_rankings(
    timeframe: str = "24h",
    limit: int = 30,
    crash_threshold_pct: float = -10.0,
):
    """v227 P1-3：跨资产全网涨跌排行（含严重下跌标记）。

    timeframe: 1h/24h/7d/30d — 涨跌幅计算窗口
    crash_threshold_pct: 跌幅 ≤ 此值视为"刚刚被严重砸"，进入 crashes 列表
    """
    try:
        return await discovery_mod.cross_asset_rankings_handler(
            timeframe=timeframe, limit=limit, crash_threshold_pct=crash_threshold_pct,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ⚠ 注意: line 14729 已注册 /api/discovery/fed-watch (走 fed_watch.fetch_fed_watch)。
# 此处的 v227 P1-5 handler 与之重复，FastAPI 会用先注册的版本，
# 本 handler 永远跑不到。保留注释 + 路由占位在 git 历史，避免重新引入重复。
# 删除内容（2026-10-08 修复发现页「数据源断裂」时）:
# @router.get("/api/discovery/fed-watch", tags=["discovery"])
# async def discovery_fed_watch():
#     """v227 P1-5: Fed 加减息预期推导链透明化页面."""
#     try:
#         return await discovery_mod.fed_watch_handler()
#     except Exception as exc:
#         return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/cycle-scan", tags=["discovery"])
async def discovery_cycle_scan():
    """周期维度扫描(大中小周期共振)。"""
    try:
        # cycle_scan_handler 为 async 协程；await 拿结果（缓存命中时也是协程内同步返回）
        return await discovery_mod.cycle_scan_handler()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/cycle-analysis/{asset}", tags=["discovery"])
def discovery_cycle_analysis(asset: str, timeframe: str = "1d"):
    """v226+ 单标的深度周期分析。

    返回 {asset, phase, confidence, reversal_prob, reversal_days_to, direction,
          hurst, cycle_len_days, mtf_alignment, mtf_score, hint, kline_count}。

    数据：从 kline_bars 取最近 60 根 1d + 可选 4h K 线 → 调 cycle_analyst
    (detect_phase / predict_reversal / detect_cycle_length / multi_timeframe_signal)。
    K 线 < 10 根时返回 ok=False。
    """
    try:
        import cycle_analyst as ca
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            bars1d = conn.execute(
                "SELECT close FROM kline_bars "
                "WHERE symbol=? AND timeframe='1d' AND close IS NOT NULL "
                "ORDER BY ts DESC LIMIT 60",
                (asset,),
            ).fetchall()
            closes_1d = [float(b['close']) for b in reversed(bars1d)]

            mtf_input: Dict[str, List[float]] = {}
            if closes_1d and len(closes_1d) >= 10:
                mtf_input["daily"] = closes_1d

            bars4h = conn.execute(
                "SELECT close FROM kline_bars "
                "WHERE symbol=? AND timeframe='4h' AND close IS NOT NULL "
                "ORDER BY ts DESC LIMIT 120",
                (asset,),
            ).fetchall()
            closes_4h = [float(b['close']) for b in reversed(bars4h)]
            if len(closes_4h) >= 10:
                mtf_input["4h"] = closes_4h
        finally:
            conn.close()

        if not mtf_input.get("daily"):
            return {"ok": False, "error": "insufficient_data",
                    "message": f"kline_bars {asset} 1d < 10 根"}

        phase = ca.detect_phase(closes_1d, window=20)
        reversal = ca.predict_reversal(closes_1d, reversal_threshold_pct=3.0)
        cycle_len = ca.detect_cycle_length(closes_1d, min_pivots=3)
        mtf = ca.multi_timeframe_signal(mtf_input)

        return {
            "ok": True,
            "asset": asset,
            "kline_count": len(closes_1d),
            "phase": phase.get("phase"),
            "phase_cn": discovery_mod._phase_cn(phase.get("phase", "unknown")),
            "confidence": phase.get("confidence"),
            "rsi": phase.get("rsi"),
            "hurst": phase.get("hurst"),
            "trend_pct": phase.get("trend_pct"),
            "cycle_len_days": cycle_len,
            "reversal_prob": reversal.get("prob"),
            "reversal_dir": reversal.get("direction"),
            "reversal_days_to": reversal.get("days_to"),
            "reversal_votes": reversal.get("votes"),
            "mtf_alignment": mtf.get("alignment"),
            "mtf_score": mtf.get("alignment_score"),
            "mtf_dirs": mtf.get("phase_by_tf", {}),
            "hint": discovery_mod._phase_hint(phase, reversal),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/cycle-narrative/{asset}", tags=["discovery"])
def discovery_cycle_narrative(asset: str):
    """v302+ · 把 cycle-analysis 的硬指标 + 近期新闻喂给 LLM, 生成周期叙事。

    返回: {ok, asset, narrative, sources_used, elapsed_sec, llm_error?, metrics}
    """
    try:
        import cycle_analyst as ca
        # 复用 analyze 端点的指标计算
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            bars1d = conn.execute(
                "SELECT close FROM kline_bars "
                "WHERE symbol=? AND timeframe='1d' AND close IS NOT NULL "
                "ORDER BY ts DESC LIMIT 60",
                (asset,),
            ).fetchall()
            closes_1d = [float(b['close']) for b in reversed(bars1d)]
        finally:
            conn.close()
        if len(closes_1d) < 10:
            return {"ok": False, "error": "insufficient_data",
                    "message": f"kline_bars {asset} 1d < 10 根"}

        phase = ca.detect_phase(closes_1d, window=20)
        reversal = ca.predict_reversal(closes_1d, reversal_threshold_pct=3.0)
        metrics = {
            "phase": phase.get("phase"),
            "rsi": phase.get("rsi"),
            "hurst": phase.get("hurst"),
            "prob": reversal.get("prob"),
            "direction": reversal.get("direction"),
            "days_to": reversal.get("days_to"),
        }
        # 拉 5 条近期新闻作背景
        recent_news: list = []
        try:
            conn = sqlite3.connect(db.get_db_path())
            conn.row_factory = sqlite3.Row
            try:
                news_rows = conn.execute(
                    "SELECT content, gate_main_asset, source FROM raw_news "
                    "WHERE (gate_main_asset=? OR content LIKE '%'||?||'%') "
                    "AND gate_verdict='material' "
                    "ORDER BY timestamp DESC LIMIT 5",
                    (asset, asset),
                ).fetchall()
                recent_news = [
                    {
                        "title": (r[0] or "")[:250],
                        "asset": r[1] or "",
                        "source": r[2] or "",
                    }
                    for r in news_rows
                ]
            finally:
                conn.close()
        except Exception:
            pass

        narrative = ca.extract_cycle_narrative(asset, metrics, recent_news)
        narrative["asset"] = asset
        narrative["metrics"] = metrics
        return narrative
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/discovery/event-radar", tags=["discovery"])
async def discovery_event_radar():
    """事件驱动快速因果雷达。"""
    try:
        return await discovery_mod.event_radar_handler()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/discovery/meta-cognition", tags=["discovery"])
async def discovery_meta_cognition(payload: dict):
    """AI 元认知自检 — 用户输入假想 → 7 步质询 → 接受/拒绝/待证据。"""
    try:
        return await discovery_mod.meta_cognition_handler(payload or {})
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
