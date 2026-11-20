# -*- coding: utf-8 -*-
"""news 数据整理路由（结构切片，2026-11-17）。

从 api_server.py 搬来 /news/exports*（批次列表/延迟/设置/运行/打开/下载）
与 /news/sources（数据源开关）。逻辑逐字未改，只把 `@app.` 换成 `@router.`。

BACKUP_DIR 在这里按同一口径重算（config.BASE_DIR/backups），
避免为了一个常量回引 api_server 形成循环依赖。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

import aiosqlite

import config

# 与 api_server 同一口径

from fastapi import APIRouter, HTTPException

# 抓取助手（TechFlow/东财/BlockBeats/金十）已下沉到 feeds/platform_news.py，
# 这批端点只是薄壳：扔进线程池调同步抓取函数
from common.time_utils import format_time as _format_time
from common.llm import agent_llm_client as _agent_llm_client
from common.llm import agent_llm_model as _agent_llm_model

import decision_guard
import evidence
import timeseries
import db
import event_atom as _event_atom
from repository import news as _news_repo

from feeds.platform_news import (
    _fetch_blockbeats_sync,
    _fetch_eastmoney_news_sync,
    _fetch_jin10_calendar_sync,
    _fetch_jin10_sync,
    _fetch_techflow_sync,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from feeds import news_organizer
from feeds import news_sources

BACKUP_DIR = os.path.join(config.BASE_DIR, "backups")

router = APIRouter()


def _api():
    """延迟回引 api_server（采集/报表助手仍在原文件）。"""
    import api_server
    return api_server


# -- 新闻数据整理（按平台分文档导出）----------------------------------------

@router.get("/api/news/exports")
async def list_news_exports():
    """列出新闻数据整理的所有批次（含各平台文件与新闻数），供前端管理页展示。"""
    try:
        batches = await asyncio.to_thread(news_organizer.list_batches)
        settings = await asyncio.to_thread(news_organizer.get_export_settings)
        return {
            "export_dir": config.NEWS_EXPORT_DIR,
            "settings": settings,
            "batches": batches,
        }
    except Exception as exc:
        return {"export_dir": config.NEWS_EXPORT_DIR, "batches": [], "error": str(exc)}


@router.get("/api/news/exports/latency")
async def get_news_source_latency():
    """各新闻源采集时效统计：轮询间隔 / 平均滞后 / 样本数（供前端可视化对比）。

    滞后 = 平台声称的发布时间 → 我们入库时刻。仅统计带平台时间的样本。
    """
    try:
        stats = await asyncio.to_thread(news_organizer.source_latency_stats, 24)
        return {"ok": True, "window_hours": 24, "sources": stats}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "sources": []}


@router.get("/api/news/exports/settings")
async def get_news_export_settings():
    """读新闻整理参数（间隔/窗口/保留批数）——供页面输入框回显。"""
    try:
        settings = await asyncio.to_thread(news_organizer.get_export_settings)
        return {"ok": True, "settings": settings}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


class NewsExportSettingsBody(BaseModel):
    interval_minutes: Optional[int] = None
    window_hours: Optional[int] = None
    keep_batches: Optional[int] = None


@router.put("/api/news/exports/settings")
async def put_news_export_settings(body: NewsExportSettingsBody):
    """保存新闻整理参数（页面输入框 → 立即生效，无需重启）。"""
    try:
        settings = await asyncio.to_thread(
            news_organizer.set_export_settings,
            interval_minutes=body.interval_minutes,
            window_hours=body.window_hours,
            keep_batches=body.keep_batches,
        )
        return {"ok": True, "settings": settings}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/news/exports/run")
async def run_news_export_now():
    """立即整理一批（手动触发，不受"有新新闻才导出"限制）。"""
    try:
        result = await asyncio.to_thread(news_organizer.export_news_batch)
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/news/exports/open")
async def open_news_exports_dir():
    """在服务器本机打开新闻整理文件夹（资源管理器）。仅当 API 与桌面同机时有效。"""
    os.makedirs(config.NEWS_EXPORT_DIR, exist_ok=True)
    try:
        os.startfile(config.NEWS_EXPORT_DIR)  # Windows 资源管理器
        return {"ok": True, "export_dir": config.NEWS_EXPORT_DIR}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "export_dir": config.NEWS_EXPORT_DIR}


@router.get("/api/news/exports/download/{batch}/{filename:path}")
async def download_news_export(batch: str, filename: str):
    """下载某批次里的平台文件（CSV）。路径做防穿越校验。"""
    safe_batch = os.path.basename(batch)
    safe_file = os.path.basename(filename)
    if safe_batch != batch or not safe_batch.startswith("batch_") or safe_file != filename:
        raise HTTPException(status_code=400, detail="bad path")
    path = os.path.join(config.NEWS_EXPORT_DIR, safe_batch, safe_file)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=safe_file,
    )


class NewsSourceBody(BaseModel):
    enabled: Optional[bool] = None
    daily_target: Optional[int] = None
    sources: Optional[Dict[str, bool]] = None


@router.get("/api/news/sources")
async def get_news_sources():
    return await asyncio.to_thread(news_sources.get_settings)


@router.post("/api/news/sources")
async def update_news_sources(body: NewsSourceBody):
    return await asyncio.to_thread(
        news_sources.set_settings,
        enabled=body.enabled,
        daily_target=body.daily_target,
        sources=body.sources,
    )


# ── 平台新闻抓取（2026-11-17 从 api_server 搬来，逻辑逐字未改） ──


@router.get("/api/news/techflow")
async def get_techflow_news():
    return await asyncio.to_thread(_fetch_techflow_sync)


@router.get("/api/news/eastmoney")
async def get_eastmoney_news():
    return await asyncio.to_thread(_fetch_eastmoney_news_sync)


@router.get("/api/news/blockbeats")
async def get_blockbeats_news():
    return await asyncio.to_thread(_fetch_blockbeats_sync)


@router.get("/api/news/jin10")
async def get_jin10_news():
    return await asyncio.to_thread(_fetch_jin10_sync)


@router.get("/api/news/jin10/calendar")
async def get_jin10_calendar():
    return await asyncio.to_thread(_fetch_jin10_calendar_sync)


# ── 事件原子序列化（从 api_server 搬来，api_server 侧仍按原名导入复用） ──



def _serialize_event_atom_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """event_atom SQLite row → 前端友好 JSON dict（已 join dim 字段）。"""
    return {
        "id":                 row.get("id"),
        "raw_news_id":        row.get("raw_news_id"),
        "event_type_id":      row.get("event_type_id"),
        "event_type_code":    row.get("event_type_code") or "",
        "event_type_zh":      row.get("event_type_zh") or "",
        "event_category":     row.get("event_category") or "",
        "asset_id":           row.get("asset_id"),
        "asset_code":         row.get("asset_code") or "",
        "event_time":         row.get("event_time"),
        "event_time_iso":     _format_time(row.get("event_time")),
        "event_time_confidence":   row.get("event_time_confidence"),
        "event_time_source":  row.get("event_time_source") or "",
        "magnitude_value":    row.get("magnitude_value"),
        "magnitude_unit":     row.get("magnitude_unit") or "",
        "actor":              row.get("actor") or "",
        "direction":          row.get("direction") or "",
        "direction_confidence": row.get("direction_confidence"),
        "direction_source":   row.get("direction_source") or "",
        "evidence_count":     row.get("evidence_count"),
        "parser_version":     row.get("parser_version") or "",
        "cluster_id":         row.get("cluster_id"),
        "reasoning":          (row.get("reasoning") or "")[:300],
        "created_at":         _format_time(row.get("created_at")),
    }


# ── mark-all-read 与单条新闻的 atoms / event_timeline / structured（2026-11-17 搬来） ──


@router.post("/api/news/mark-all-read")
async def mark_all_news_read():
    """把 raw_news.status IN (PROCESSING/FAILED/PENDING) 批量改为 DONE。

    用户点 dashboard 的"全部已读"按钮后调用。不动 status='DONE' 已读的（避免覆盖时间戳）。
    返回：{marked_count: N}
    """
    try:
        async with aiosqlite.connect(config.DB_PATH, timeout=10) as conn:
            cur = await conn.execute(
                "UPDATE raw_news SET status='DONE', processing_at=CURRENT_TIMESTAMP "
                "WHERE status IN ('PROCESSING','FAILED','PENDING')"
            )
            marked = cur.rowcount if hasattr(cur, "rowcount") else 0
            await conn.commit()
        return {"marked_count": marked}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"mark-all-read failed: {e}")


@router.get("/api/news/{news_id}/atoms")
async def get_news_atoms(news_id: int):
    """单新闻对应的事件原子列表（按 event_time 排序）。"""
    rows = await asyncio.to_thread(_event_atom.get_event_atoms_by_news, int(news_id))
    return {"items": [_serialize_event_atom_row(r) for r in rows],
            "count": len(rows), "news_id": int(news_id)}


@router.get("/api/news/{news_id}/event_timeline")
async def get_news_event_timeline(news_id: int):
    """单新闻的事件时间线（所有 event_time_candidates，含 source/confidence）。"""
    rows = await asyncio.to_thread(_event_atom.get_event_time_candidates, int(news_id))
    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append({
            **r,
            "candidate_ts": r.get("candidate_ts"),
            "candidate_ts_iso": _format_time(r.get("candidate_ts")),
            "created_at":   _format_time(r.get("created_at")),
            "confidence":   round(float(r["confidence"]), 3) if r.get("confidence") is not None else None,
            "is_primary":   bool(r.get("is_primary")),
        })
    primary = next((it for it in items if it.get("is_primary")), items[0] if items else None)
    return {"primary": primary, "candidates": items, "count": len(items), "news_id": int(news_id)}


@router.get("/api/news/{news_id}/structured")
async def get_news_structured(news_id: int):
    """单新闻的 LLM 结构化输入 + 反向引用校验（v172 重点 endpoint）。
    返回:
      - structured_input: dict（6 字段 meta/events/snapshot/relations/baseline/text_summary）
      - structured_check: dict（mentioned/missing/fabricated/check_at + verdict）
      - event_type_code / snapshot_proximity: v174 强制字段
    """
    # 2026-11-16 结构样板：本条 SQL 已搬到 repository/news.py（逐字搬移，不改行为）。
    # 意义不在省几行代码，而在「表结构改了只改一处」从口号变成结构。
    row = await asyncio.to_thread(
        _news_repo.get_news_with_latest_decision, int(news_id))
    if row is None:
        raise HTTPException(status_code=404, detail="news not found")
    data = dict(row)
    def _parse(s):
        if not s:
            return None
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {"_raw": s}
        except (json.JSONDecodeError, TypeError):
            return {"_raw": s, "_parse_error": True}
    si = _parse(data.get("structured_input"))
    sc = _parse(data.get("structured_check"))
    verdict = sc.get("verdict") if isinstance(sc, dict) else None
    # v172 边界字段归一化：structured_check 落库键名为 events_mentioned/events_missing/
    # entities_fabricated，前端结构化面板读的是 mentioned/missing/fabricated。在此做一次
    # 别名映射，保证前端三块校验内容都能命中（不改 stored_check 原始键名，不影响其他消费方）。
    if isinstance(sc, dict):
        aliases = {
            "mentioned":  sc.get("events_mentioned") if "mentioned" not in sc else sc.get("mentioned"),
            "missing":    sc.get("events_missing") if "missing" not in sc else sc.get("missing"),
            "fabricated": sc.get("entities_fabricated") if "fabricated" not in sc else sc.get("fabricated"),
        }
        for k, v in aliases.items():
            if v is not None:
                sc[k] = v
    return {
        "news_id": int(news_id),
        "decision_id": data.get("decision_id"),
        "created_at": _format_time(data.get("created_at")),
        "structured_input": si,
        "structured_check": sc,
        "event_type_code":  data.get("event_type_code") or "",
        "snapshot_proximity": data.get("snapshot_proximity") or "",
        "verdict": verdict,
    }


# ── 单条新闻的 AI 分析 + 策略参数建议（2026-11-17 从 api_server 整簇搬来，逻辑逐字未改） ──
# 这一簇自带 _clamp / _context_asset / _factor_result / _settled_stats_sync / _rule_advice /
# _llm_strategy_advice_sync / _ADVICE_BOUNDS，段外零调用点，所以整体搬而不是只搬端点。

def _clamp(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _context_asset(context: Dict[str, Any], asset: str) -> Dict[str, Any]:
    assets = context.get("assets") if isinstance(context.get("assets"), dict) else {}
    value = assets.get(asset) or assets.get("BTC") or {}
    return value if isinstance(value, dict) else {}


def _factor_result(row: Dict[str, Any]) -> Dict[str, Any]:
    # v342: 预期差注入（只读 econ_events；任何失败都不影响主判定）
    try:
        import sqlite3 as _sqlite3
        import surprise_factor as _suf
        _row = dict(row)
        if not _row.get("news_title"):
            _row["news_title"] = _row.get("content") or ""
        _when = _row.get("news_ts")
        if not _when:
            for _k in ("timestamp", "published_at"):
                _v = _row.get(_k)
                if isinstance(_v, (int, float)):
                    _when = int(_v)
                    break
        _row["news_ts"] = _when
        with _sqlite3.connect(config.DB_PATH, timeout=3) as _ec:
            _ec.row_factory = _sqlite3.Row
            row["surprise"] = _suf.enrich(_ec, _row)
    except Exception as _suf_exc:
        print(f"[FACTOR] 预期差跳过: {type(_suf_exc).__name__}: {str(_suf_exc)[:80]}",
              flush=True)
    return decision_guard.evaluate_decision(row)


async def _news_analysis(news_id: int) -> Dict[str, Any]:
    # v176：访问详情 = 已读，把 raw_news.status 强制 DONE（防 dashboard 一直显示 UNREAD）
    try:
        async with aiosqlite.connect(config.DB_PATH, timeout=5) as _mc:
            await _mc.execute(
                "UPDATE raw_news SET status='DONE', processing_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('PROCESSING','FAILED','PENDING')",
                (news_id,),
            )
            await _mc.commit()
    except Exception as _mark_exc:
        # v322: 不影响主响应，但必须留痕 —— 否则前端一直显示 UNREAD / 待分析，
        # 而没有任何线索说明是"标记已读"这一步失败了。
        print(f"[NEWS-ANALYSIS] ⚠️ 标记已读失败 news=#{news_id}: "
              f"{type(_mark_exc).__name__}: {str(_mark_exc)[:100]}", flush=True)
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            """
            SELECT rn.id AS news_id, rn.source, rn.content, rn.timestamp,
                   rn.published_at,
                   ad.id AS decision_id, ad.created_at, ad.sentiment_score,
                   UPPER(ad.suggested_action) AS suggested_action, ad.reasoning,
                   ad.reasoning_path, ad.asset_reasoning, ad.market_category, ad.target_asset,
                   ad.market_confirmation, ad.decision_context, ad.cluster_size,
                   ad.event_struct, ad.structured_input, ad.structured_check,
                   ad.event_type_code, ad.snapshot_proximity,
                   (SELECT COUNT(*) FROM ai_decisions h WHERE h.settled=1 AND UPPER(h.target_asset)=UPPER(ad.target_asset)) AS history_total,
                   (SELECT COUNT(*) FROM ai_decisions h WHERE h.settled=1 AND h.forward_pnl > 0 AND UPPER(h.target_asset)=UPPER(ad.target_asset)) AS history_wins
            FROM raw_news rn
            LEFT JOIN ai_decisions ad ON ad.id=(SELECT MAX(x.id) FROM ai_decisions x
                                                WHERE x.news_id=rn.id AND (x.is_sibling=0 OR x.is_sibling IS NULL))
            WHERE rn.id=?
            """,
            (news_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
    if row is None:
        raise HTTPException(status_code=404, detail="news not found")
    data = dict(row)
    analysis = _factor_result(data)
    await asyncio.to_thread(timeseries.record_factor_snapshot, news_id,
                            str(data.get("target_asset") or "NONE"), analysis)
    return {
        "news": {"id": data["news_id"], "source": data["source"], "content": re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', data["content"] or ""), "published_at": data["timestamp"], "source_published_at": _format_time(data.get("published_at")) if data.get("published_at") else ""},
        "decision": None if data["decision_id"] is None else {key: data[key] for key in ("decision_id", "created_at", "sentiment_score", "suggested_action", "reasoning", "reasoning_path", "asset_reasoning", "market_category", "target_asset", "market_confirmation", "event_struct", "structured_input", "structured_check", "event_type_code", "snapshot_proximity")},
        "analysis": analysis,
        "strategy": {
            "action": analysis["action"],
            "raw_action": analysis["raw_action"],
            "confidence": analysis["confidence"],
            "passed_gate": analysis["confidence_detail"]["passed_gate"],
            "verdict": analysis["verdict"],
            "note": "仅供策略研究，不执行交易",
        },
    }


@router.get("/api/news/{news_id}/analysis")
async def get_news_analysis(news_id: int):
    return await _news_analysis(news_id)


def _settled_stats_sync(asset: str) -> Dict[str, Any]:
    connection = db.get_connection()
    connection.row_factory = __import__("sqlite3").Row
    try:
        sample = evidence.collect_settled_sample(asset, connection=connection)
        tokens = evidence.query_assets(sample["assets"]) if sample["assets"] else ()
        if tokens:
            placeholders = ",".join("?" * len(tokens))
            row = connection.execute(
                f"""SELECT AVG(forward_pnl) avg_pnl FROM ai_decisions
                    WHERE settled=1 AND UPPER(is_correct) IN ('WIN','LOSS')
                      AND UPPER(target_asset) IN ({placeholders})""",
                tokens,
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT AVG(forward_pnl) avg_pnl FROM ai_decisions
                   WHERE settled=1 AND UPPER(is_correct) IN ('WIN','LOSS')"""
            ).fetchone()
        return {
            "total": sample["total"],
            "wins": sample["wins"],
            "avg_pnl": None if row is None else row["avg_pnl"],
            "scope": sample["scope"],
            "supplemented": sample["supplemented"],
        }
    finally:
        connection.close()


_ADVICE_BOUNDS = {
    "signal_threshold": (0.0, 0.9),
    "notional_multiplier": (0.25, 1.5),
    "trailing_callback_rate": (0.1, 5.0),
    "holding_horizon_minutes": (15, 240),
}


def _rule_advice(analysis: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    confidence = analysis["analysis"]["confidence"] / 100.0
    winrate = (stats.get("wins") or 0) / max(stats.get("total") or 1, 1)
    contradictions = len(analysis["analysis"]["contradictions"])
    significance = analysis["analysis"]["significance"]
    return {
        "signal_threshold": round(_clamp(0.65 - confidence * 0.2 + contradictions * 0.05, 0.0, 0.9), 2),
        "notional_multiplier": round(_clamp(0.5 + confidence * 0.6 + max(winrate - 0.5, 0) - contradictions * 0.15, 0.25, 1.5), 2),
        "trailing_callback_rate": round(_clamp(0.5 + (1 - confidence) * 0.5, 0.1, 5), 2),
        "holding_horizon_minutes": int(_clamp(60 + confidence * 120, 15, 240)),
        "reason": f"统一置信度 {analysis['analysis']['confidence']}/100，卡方 p={significance['p_value']}，矛盾项 {contradictions} 个",
        "risk_notes": "样本不足、p 值过大或因子冲突时维持较高阈值和较低仓位倍率",
        "rollback_condition": "连续 3 条已结算信号亏损或滚动胜率低于 45% 时回滚",
    }


def _llm_strategy_advice_sync(analysis: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    response = _agent_llm_client("advisor").chat.completions.create(
        model=_agent_llm_model("advisor"), temperature=0.0, max_tokens=700,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "你是策略参数顾问。只输出严格 JSON，不交易、不修改配置。只能引用输入 JSON 中已有的证据（evidence 为 0-10 归一分、significance 为卡方检验结果、contradictions 为矛盾项），禁止凭空假设任何未给出的数据。p_value 越大或 contradictions 越多，参数越保守。字段只能是 signal_threshold, notional_multiplier, trailing_callback_rate, holding_horizon_minutes, reason, risk_notes, rollback_condition。"},
            {"role": "user", "content": json.dumps({"bounds": _ADVICE_BOUNDS, "analysis": analysis["analysis"], "settled_performance": stats}, ensure_ascii=False)},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    allowed = set(_ADVICE_BOUNDS) | {"reason", "risk_notes", "rollback_condition"}
    if not isinstance(result, dict) or set(result) - allowed or not all(key in result for key in allowed):
        raise ValueError("invalid advice JSON")
    for key, (low, high) in _ADVICE_BOUNDS.items():
        result[key] = _clamp(result[key], low, high)
    result["holding_horizon_minutes"] = int(result["holding_horizon_minutes"])
    return result


@router.post("/api/news/{news_id}/strategy-advice")
async def get_strategy_advice(news_id: int):
    analysis = await _news_analysis(news_id)
    asset = (analysis.get("decision") or {}).get("target_asset") or "NONE"
    stats = await asyncio.to_thread(_settled_stats_sync, asset)
    try:
        advice = await asyncio.to_thread(_llm_strategy_advice_sync, analysis, stats)
        mode = "llm"
    except Exception:
        advice = _rule_advice(analysis, stats)
        mode = "rules"
    current_values = {
        "signal_threshold": config.BINANCE_SIGNAL_THRESHOLD,
        "notional_multiplier": 1.0,
        "trailing_callback_rate": config.BINANCE_TRAILING_CALLBACK_RATE,
        "holding_horizon_minutes": 60,
    }
    return {"mode": mode, "current_values": current_values, "advice": advice, "bounds": {key: {"min": value[0], "max": value[1]} for key, value in _ADVICE_BOUNDS.items()}, "settled_performance": stats, "validation": {"confidence": analysis["analysis"]["confidence"], "significance": analysis["analysis"]["significance"], "contradictions": analysis["analysis"]["contradictions"], "gated_action": analysis["analysis"]["action"], "verdict": analysis["analysis"]["verdict"]}, "applied": False}


# ─── 2026-11-17 从 api_server 追加 ──────────

@router.get("/api/news/qinghong")
async def get_qinghong_news():
    return await asyncio.to_thread(_api()._fetch_qinghong_sync)


@router.get("/api/news/report")
async def get_news_report():
    return await _api()._news_report()

def __getattr__(name: str):
    """懒读库路径（v475）：`DB_PATH` 不再在 import 时快照。

    原来这里写的是 `DB_PATH = config.DB_PATH` —— 只要本模块是在**别人把
    config.DB_PATH 改坏的那一刻**被首次 import，之后整场测试/整个进程就一直在
    读那个库（典型症状：test_structured_api_v172 单跑绿、全量 6 条全红 count=0）。
    改成属性访问时现读，`db.get_db_path()` 的语义与之一致。
    """
    if name == "DB_PATH":
        return config.DB_PATH
    raise AttributeError(name)
