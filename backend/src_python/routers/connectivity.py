# -*- coding: utf-8 -*-
"""全局联通中心路由（结构切片，2026-11-17）。

从 api_server.py 搬来 4 条端点 + 2 个 LLM 探针助手：
  /api/connectivity/status（数据源 / LLM 通道 / 告警通道 / 跨资产传导四维聚合）
  /test-channel（单通道真实发一条 hello）
  /dispatch（全 configured 通道派发）
  /reset-channel（清零某通道健康状态）
LLM 通道状态优先读 worker_heartbeat（v375 起探测挪到后台，冷启动读心跳立刻有值），
其次读 ~/.trident/state/llm_probe.json，都过期就如实 unknown。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

import config
import db
import notification_channels
from feeds import source_status

def _api():
    """延迟回引 api_server（_probe_model_channels / _now_iso 仍在原文件）。"""
    import api_server
    return api_server


router = APIRouter()


def _llm_probe_from_file() -> Optional[Dict[str, Any]]:
    """读 ~/.trident/state/llm_probe.json（上次探测结果快照），10 分钟内有效。"""
    try:
        import json as _json
        import os as _os
        fp = _os.path.join(_os.path.expanduser("~"), ".trident", "state", "llm_probe.json")
        with open(fp, encoding="utf-8") as f:
            d = _json.load(f)
        if time.time() - float(d.get("ts") or 0) > 600:
            return None
        if not int(d.get("total") or 0):
            return None
        return {
            "ok": int(d.get("ok") or 0), "err": int(d.get("err") or 0),
            "total": int(d.get("total") or 0),
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(d.get("ts") or 0))),
            "channels": d.get("channels") or [],
        }
    except Exception:
        return None


def _llm_last_probe_from_heartbeat() -> Optional[Dict[str, Any]]:
    """从 worker_heartbeat 读上一次 LLM 通道探测的结果。

    v375: 探测本身已挪到后台（不占请求路径），代价是**刚重启时缓存为空**，
    面板会先显示 llm=unknown 再回填，看起来像"LLM 全挂了"。
    其实每次探测都会写 worker_heartbeat（detail="ok=x err=y total=z"），
    冷启动直接读它就能立刻给出真实状态。
    """
    try:
        import re as _re
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT last_tick_at, last_status, processed, detail FROM worker_heartbeat"
                " WHERE worker_name='llm_channels'"
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    at, status, processed, detail = row
    m = _re.search(r"ok=(\d+) err=(\d+) total=(\d+)", detail or "")
    if not m:
        return None
    # 时效保护：心跳只在上一次探测时写，若已陈旧(>10 分钟)就不能当作当前状态，
    # 否则会拿"上次全挂"的旧结论冒充实时值 —— 比 unknown 更误导。
    try:
        import datetime as _dt
        _at = _dt.datetime.strptime(str(at)[:19], "%Y-%m-%d %H:%M:%S")
        if (_dt.datetime.now() - _at).total_seconds() > 600:
            return None
    except Exception:
        return None
    return {
        "ok": int(m.group(1)),
        "err": int(m.group(2)),
        "total": int(m.group(3)),
        "at": str(at or ""),
        "status": str(status or ""),
        "processed": int(processed or 0),
    }


@router.get("/api/connectivity/status")
async def get_connectivity_status() -> Dict[str, Any]:
    """全局联通中心：聚合 4 大维度
      1. data_sources  — 行情/宏观/新闻 数据源（来自 source_status）
      2. llm_channels  — LLM 通道（来自 /api/health/models 30s 缓存）
      3. notify_channels — 出站告警通道（来自 notification_channels）
      4. asset_edges  — 跨资产传导边汇总（来自 config.ASSET_DRIVEN_EDGES）

    每个维度都返回 status: ok / degraded / down，方便前端计算总评分。
    """
    # 1) 数据源
    sources = source_status.snapshot()
    src_tally: Dict[str, int] = {"online": 0, "error": 0, "stale": 0, "unknown": 0}
    for s in sources:
        st = s.get("status", "unknown")
        src_tally[st] = src_tally.get(st, 0) + 1
    # v367: 健康度分母只算「真在采集」的源 —— 主动停用/缺凭据/从未接线的源
    # (status=disabled) 不该稀释健康度。原口径把 40 个从未上报的源算进分母,
    # 导致 11 个在线源被摊成 18%, 联通总分被压到 15.2。
    _active = [s for s in sources if s.get("status") != "disabled"]
    src_total = max(len(_active), 1)
    src_health_pct = round((src_tally.get("online", 0) / src_total) * 100, 1)
    src_status = "ok" if src_tally.get("error", 0) == 0 else (
        "degraded" if src_tally.get("online", 0) > src_tally.get("error", 0) else "down"
    )

    # 2) LLM 通道
    try:
        llm_payload = await asyncio.to_thread(_api()._probe_model_channels)
    except Exception:
        llm_payload = {"channels": [], "ts": _api()._now_iso()}
    llm_channels = llm_payload.get("channels", []) if isinstance(llm_payload, dict) else []
    # v378: 被额度/速率闸门拦下的通道**没真正测过**，不能算作"通道挂了"。
    # 原来只要闸门一拦(决策高峰抢速率名额时必拦)就 available=False →
    # 面板 10 个通道瞬间变 6/10、总分跟着掉 10 分，而实际通道全都好好的。
    _tested = [c for c in llm_channels if not c.get("gate_blocked")]
    llm_ok = sum(1 for c in _tested if c.get("available") is True)
    llm_err = sum(1 for c in _tested if c.get("available") is False)
    # v367: 探测缓存为空(刚重启、后台刷新还没跑)时不能按 0% 计 —— 那会把
    # 联通总分瞬间压掉 35 分, 看起来像"系统崩了", 其实只是还没探测。
    llm_total = len(_tested)
    llm_health_pct = round((llm_ok / llm_total) * 100, 1) if llm_total else 0.0
    llm_status = ("unknown" if llm_total == 0
                  else ("ok" if llm_err == 0 else ("degraded" if llm_ok > llm_err else "down")))
    # v375: 冷启动回落 —— 探测缓存空时用上一次落库的探测结果，不再显示 unknown
    _probe_source = "live_cache"
    _probe_at = ""
    if llm_total == 0:
        try:
            _hb = await asyncio.to_thread(_llm_last_probe_from_heartbeat)
        except Exception:
            _hb = None
        if not _hb:
            try:
                _hb = await asyncio.to_thread(_llm_probe_from_file)
            except Exception:
                _hb = None
        if _hb and _hb.get("total"):
            llm_total = int(_hb["total"])
            llm_ok = int(_hb["ok"])
            llm_err = int(_hb["err"])
            llm_health_pct = round((llm_ok / llm_total) * 100, 1)
            llm_status = ("ok" if llm_err == 0
                          else ("degraded" if llm_ok > llm_err else "down"))
            _probe_source = "file_snapshot" if _hb.get("channels") else "heartbeat"
            _probe_at = _hb.get("at") or ""

    # 3) 通知通道
    notify = notification_channels.snapshot()
    n_tally = {"online": 0, "error": 0, "stale": 0, "unknown": 0, "disabled": 0}
    for c in notify:
        st = c.get("status", "unknown")
        n_tally[st] = n_tally.get(st, 0) + 1
    n_configured = sum(1 for c in notify if c.get("configured"))
    n_online = n_tally.get("online", 0)
    notify_status = "ok" if n_online >= 1 else ("degraded" if n_configured > 0 else "down")

    # 4) 跨资产传导边（汇总）
    asset_edges = []
    try:
        for src_asset, edges in getattr(config, "ASSET_DRIVEN_EDGES", {}).items():
            for e in edges:
                asset_edges.append({"source": src_asset, "target": e.get("target", ""), "lag_s": e.get("lag_s")})
    except Exception:
        pass
    # v368: config 里根本没有 ASSET_DRIVEN_EDGES —— 面板因此恒显示"0 条"，
    # 而库里 asset_driver_edges 实有 1200+ 条。改为以库为准，config 只作兜底。
    _edge_total = len(asset_edges)
    if not asset_edges:
        try:
            _c = db.get_connection()
            try:
                _edge_total = int(_c.execute(
                    "SELECT COUNT(*) FROM asset_driver_edges").fetchone()[0] or 0)
                for row in _c.execute(
                    "SELECT from_id, to_id, kind, weight, updated_at FROM asset_driver_edges"
                    " ORDER BY COALESCE(weight,0) DESC LIMIT 40"
                ).fetchall():
                    asset_edges.append({
                        "source": str(row[0]), "target": str(row[1]),
                        "kind": row[2], "weight": row[3], "updated_at": row[4],
                    })
            finally:
                _c.close()
        except Exception as exc:
            # v399：允许失败，但不许不留痕（v322 审计门禁要求）
            print(f"[HEALTH-SCORE] 健康分维度读取失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)

    # 总体评分（0-100）：四维度加权（数据源 35% + LLM 35% + 通知 20% + 跨资产边 10%）
    # v367: 只对「有实测数据」的维度加权 —— 冷启动时 LLM 探测缓存为空、
    # 通知通道一个都没配时，不该按 0 分惩罚（否则面板开局就 39 分，误导成系统挂了）。
    _dims = [(0.35, src_health_pct)]
    if llm_total:
        _dims.append((0.35, llm_health_pct))
    if n_configured:
        _dims.append((0.20, (min(n_online, 3) / max(min(n_configured, 3), 1)) * 100))
    _dims.append((0.10, 100.0))
    _wsum = sum(w for w, _ in _dims) or 1.0
    overall = round(sum(w * p for w, p in _dims) / _wsum, 1)

    return {
        "ts": time.time(),
        "overall_score": overall,
        "data_sources": {
            "status": src_status,
            "health_pct": src_health_pct,
            "tally": src_tally,
            "total": len(sources),
            "items": sources,
        },
        "llm_channels": {
            "status": llm_status,
            "health_pct": llm_health_pct,
            "ok": llm_ok,
            "err": llm_err,
            "total": llm_total,
            "items": llm_channels,
            "probe_source": _probe_source,
            "probe_at": _probe_at,
            "engine_openrouter": llm_payload.get("engine_openrouter") if isinstance(llm_payload, dict) else None,
            "engine_model_pool": llm_payload.get("engine_model_pool") if isinstance(llm_payload, dict) else None,
        },
        "notify_channels": {
            "status": notify_status,
            "configured": n_configured,
            "online": n_online,
            "tally": n_tally,
            "total": len(notify),
            "items": notify,
        },
        "asset_edges": {
            "total": _edge_total,
            "items": asset_edges[:30],
        },
    }


@router.post("/api/connectivity/test-channel")
async def test_notify_channel(payload: Dict[str, Any]) -> Dict[str, Any]:
    """测试单个通知通道：真实发送一条 hello 消息。
    Body: {"channel_id": "slack", "title": "...", "body": "..."}
    返回该通道的 (ok, info, latency_ms)。
    """
    channel_id = str(payload.get("channel_id") or "").strip()
    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id required")
    title = str(payload.get("title") or "[Trident 测试] 全局联通中心")
    body = str(payload.get("body") or f"这是一条来自 /connectivity 测试的连接验证消息（{channel_id}）。")
    t0 = time.time()
    ok, info = notification_channels.send(channel_id, title, body)
    latency_ms = round((time.time() - t0) * 1000.0, 1)
    return {
        "channel_id": channel_id,
        "ok": ok,
        "info": info,
        "latency_ms": latency_ms,
        "ts": time.time(),
    }


@router.post("/api/connectivity/dispatch")
async def test_dispatch_all(payload: Dict[str, Any]) -> Dict[str, Any]:
    """对所有 configured 通道派发同一条测试消息，返回每通道结果。
    Body: {"title": "...", "body": "...", "only": ["slack"], "exclude": []}
    """
    title = str(payload.get("title") or "[Trident 全联通] 健康度测试")
    body = str(payload.get("body") or "这是一条来自 /connectivity 派发的全通道测试消息。")
    only = payload.get("only")
    exclude = payload.get("exclude")
    results = await asyncio.to_thread(
        notification_channels.dispatch, title, body,
        only=only, exclude=exclude, min_status="any",
    )
    return {
        "title": title,
        "results": results,
        "ts": time.time(),
    }


@router.post("/api/connectivity/reset-channel")
async def reset_notify_channel(payload: Dict[str, Any]) -> Dict[str, Any]:
    """重置某个 channel 的健康状态（清零 last_error 与连续失败计数）。"""
    channel_id = str(payload.get("channel_id") or "").strip()
    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id required")
    notification_channels.reset_channel(channel_id)
    return {"channel_id": channel_id, "ok": True, "ts": time.time()}
