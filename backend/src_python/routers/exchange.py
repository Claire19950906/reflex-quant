# -*- coding: utf-8 -*-
"""实盘交易通道管理路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v190 的 9 条 exchange 端点：status、mode/start、mode/stop、
stop_all、credentials、audit、daily_summary、daily_resets、signal_archive，
连带 _build_executor_from_env 与 _trade_executor_status_payload 两个助手
（段外零调用点，随段搬走）。逻辑逐字未改。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

import license_auth

import config
import db

# 与 api_server 同一口径（api_server 的 config.DB_PATH 就是 config.config.DB_PATH）
# full 模式默认要求 API Key；仅回环地址在 TRIDENT_LOCAL_TRUST=1 时免鉴权。
router = APIRouter(dependencies=[Depends(license_auth.require_api_key)])


# v190: 实盘交易通道管理 endpoints
#   GET  /api/v190/exchange/status            当前 mode / adapter / 统计 / 风险快照
#   POST /api/v190/exchange/mode/start        启动 worker（写入环境变量+spawn task）
#   POST /api/v190/exchange/mode/stop         停止 worker（取消 task + 清环境变量）
#   POST /api/v190/exchange/stop_all          kill_switch 一键熔断 + 撤销 Binance 挂单
#   POST /api/v190/exchange/credentials       更新 BINANCE_API_KEY/SECRET（进程内）
#   GET  /api/v190/exchange/audit             trade_audit_log 查询
# 进程内仅一个 TradeExecutor singleton；切换 mode 必须先 stop 再 start，
# 避免两个 worker 同时抢同一行 ai_decisions。
# ──────────────────────────────────────────────────────────────────────────────
# v433（2026-11-18）：api 重启后自动恢复纸面执行器。
# 事故：launchd kickstart / memory-guard 重启 api 后，mode 仍是 paper 但 task 为 None，
# status 显示 running=false，只能靠 keep_executor.sh 每 5 分钟兜底 —— 中间这几分钟
# 正好错过建仓窗口，用户看到的就是"有决策不下单"。
# 这里挂在 router 的 startup 事件上（比 lifespan 尾部更早执行，且不依赖托管方式）。
@router.on_event("startup")
async def _autostart_trade_executor() -> None:
    try:
        mode = (os.getenv("TRIDENT_EXCHANGE_MODE") or "paper").strip().lower()
        if mode not in ("paper", "live_dry"):
            return
        if _TRADE_EXECUTOR_STATE.get("task") is not None:
            return
        await _start_trade_executor(mode)
        print(f"[EXCHANGE] trade_executor 自启 mode={mode}", flush=True)
    except Exception as exc:
        print(f"[EXCHANGE] trade_executor 自启失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)


_TRADE_EXECUTOR_STATE = {
    "task": None,             # asyncio.Task | None
    "executor": None,         # TradeExecutor 实例
    "stop_all_in_progress": False,
}


def _build_executor_from_env():
    """从当前 os.environ 构建 TradeExecutor。"""
    try:
        from trade_executor import build_default_executor
    except Exception as exc:
        return None, f"import_failed: {exc}"
    try:
        ex = build_default_executor(config.DB_PATH, env=os.environ)
        return ex, None
    except Exception as exc:
        return None, f"build_failed: {exc}"


def _trade_executor_status_payload() -> Dict[str, Any]:
    ex = _TRADE_EXECUTOR_STATE["executor"]
    task = _TRADE_EXECUTOR_STATE["task"]
    running = task is not None and not task.done()
    base = {
        "mode": os.getenv("TRADE_EXECUTOR_MODE", "off"),
        "running": running,
        "kill_switch": False,
        "has_api_key": bool(os.getenv("BINANCE_API_KEY", "").strip()),
        "use_testnet": (os.getenv("BINANCE_USE_TESTNET", "false").lower()
                        in ("1", "true", "yes")),
        "whitelist": os.getenv("WHITELIST_ASSETS", "*"),
        "max_position_usdt": float(os.getenv("MAX_POSITION_USDT", "5000") or 5000.0),
    }
    if ex is None:
        return {**base, "adapter": None, "snapshot": None}
    snap = ex.snapshot() if hasattr(ex, "snapshot") else {}
    rm_snap = snap.get("risk", {})
    return {
        **base,
        "mode": ex.cfg.mode,
        "adapter": ex.adapter.name,
        "snapshot": snap,
        "kill_switch": rm_snap.get("kill_switch", False),
        "use_testnet": bool(ex.cfg.testnet),
    }


@router.get("/api/v190/exchange/status")
def v190_exchange_status() -> Dict[str, Any]:
    return _trade_executor_status_payload()


async def _stop_trade_executor() -> None:
    task = _TRADE_EXECUTOR_STATE.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _TRADE_EXECUTOR_STATE["task"] = None
    _TRADE_EXECUTOR_STATE["executor"] = None


async def _start_trade_executor(mode: str) -> Dict[str, Any]:
    await _stop_trade_executor()
    os.environ["TRADE_EXECUTOR_MODE"] = mode
    ex, err = _build_executor_from_env()
    if ex is None:
        return {"ok": False, "error": err, "mode": mode}
    task = asyncio.create_task(ex.run(), name=f"trade_executor_{mode}")
    _TRADE_EXECUTOR_STATE["task"] = task
    _TRADE_EXECUTOR_STATE["executor"] = ex
    return {
        "ok": True,
        "mode": ex.cfg.mode,
        "adapter": ex.adapter.name,
        "poll_interval": ex.cfg.poll_interval,
        "notional_usdt": ex.cfg.notional_usdt,
    }


@router.post("/api/v190/exchange/mode/start")
async def v190_exchange_mode_start(body: Dict[str, Any]) -> Dict[str, Any]:
    """启动 trade_executor，body: {\"mode\": \"off\"|\"paper\"|\"live_dry\"|\"live_real\"}"""
    mode = (body.get("mode") or os.getenv("TRADE_EXECUTOR_MODE", "off")).strip().lower()
    if mode not in ("off", "paper", "live_dry", "live_real"):
        return {"ok": False, "error": f"invalid_mode: {mode}"}
    if mode in ("live_dry", "live_real"):
        if not (os.getenv("BINANCE_API_KEY", "").strip()
                and os.getenv("BINANCE_API_SECRET", "").strip()):
            return {
                "ok": False,
                "error": "missing_credentials",
                "hint": "live 模式需要 BINANCE_API_KEY / BINANCE_API_SECRET；"
                        "请先 POST /api/v190/exchange/credentials 写入",
            }
    return await _start_trade_executor(mode)


@router.post("/api/v190/exchange/mode/stop")
async def v190_exchange_mode_stop() -> Dict[str, Any]:
    """停止 trade_executor（保留 mode 配置，不重置环境变量）。"""
    await _stop_trade_executor()
    return {"ok": True, "running": False}


@router.post("/api/v190/exchange/stop_all")
async def v190_exchange_stop_all() -> Dict[str, Any]:
    """一键熔断：取消 worker + 触发 kill_switch + 撤销 Binance 挂单。

    paper 模式仅取消 worker；live_dry/live_real 模式额外遍历 4 个核心永续对
    BTCUSDT/ETHUSDT/SOLUSDT/PAXGUSDT 调用 cancelOrder。
    """
    if _TRADE_EXECUTOR_STATE.get("stop_all_in_progress"):
        return {"ok": False, "error": "already_in_progress"}
    _TRADE_EXECUTOR_STATE["stop_all_in_progress"] = True
    try:
        cancelled_task = False
        task = _TRADE_EXECUTOR_STATE.get("task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            cancelled_task = True
        _TRADE_EXECUTOR_STATE["task"] = None

        ex = _TRADE_EXECUTOR_STATE.get("executor")
        kill_reason = None
        cancelled_orders = 0
        cancel_errors: List[str] = []

        if ex is not None and hasattr(ex, "risk_manager"):
            try:
                ex.risk_manager.kill_switch = True
                kill_reason = "user_stop_all"
            except Exception as exc:
                kill_reason = f"kill_switch_failed: {exc}"

        if ex is not None and getattr(ex, "adapter", None) and ex.adapter.name == "binance":
            try:
                client = getattr(ex.adapter, "client", None)
                if client and hasattr(client, "openOrders") and hasattr(client, "cancel_order"):
                    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "PAXGUSDT"):
                        try:
                            oo = client.openOrders(sym)
                            if isinstance(oo, list):
                                for o in oo:
                                    oid = o.get("orderId")
                                    if oid:
                                        client.cancel_order(sym, orderId=str(oid))
                                        cancelled_orders += 1
                        except Exception as exc:
                            cancel_errors.append(f"{sym}: {exc}")
            except Exception as exc:
                cancel_errors.append(f"adapter_dispatch: {exc}")

        _TRADE_EXECUTOR_STATE["executor"] = None
        os.environ["TRADE_EXECUTOR_MODE"] = "off"
        return {
            "ok": True,
            "cancelled_task": cancelled_task,
            "kill_switch": True,
            "kill_reason": kill_reason,
            "cancelled_orders": cancelled_orders,
            "cancel_errors": cancel_errors,
            "mode_after": "off",
        }
    finally:
        _TRADE_EXECUTOR_STATE["stop_all_in_progress"] = False


@router.post("/api/v190/exchange/credentials")
def v190_exchange_credentials(body: Dict[str, Any]) -> Dict[str, Any]:
    """更新 Binance API key/secret（仅写入进程内环境变量，不落盘）。

    body: {\"api_key\": \"...\", \"api_secret\": \"...\", \"use_testnet\": bool}
    """
    api_key = (body.get("api_key") or "").strip()
    api_secret = (body.get("api_secret") or "").strip()
    use_testnet = bool(body.get("use_testnet"))
    if api_key:
        os.environ["BINANCE_API_KEY"] = api_key
    if api_secret:
        os.environ["BINANCE_API_SECRET"] = api_secret
    os.environ["BINANCE_USE_TESTNET"] = "true" if use_testnet else "false"
    return {
        "ok": True,
        "has_api_key": bool(os.getenv("BINANCE_API_KEY", "").strip()),
        "has_api_secret": bool(os.getenv("BINANCE_API_SECRET", "").strip()),
        "use_testnet": use_testnet,
        "note": "已写入进程内环境变量；切换 mode 时重建 worker 即生效",
    }


@router.get("/api/v190/exchange/audit")
def v190_exchange_audit(
    limit: int = 50,
    status: Optional[str] = None,
    decision_id: Optional[int] = None,
    asset: Optional[str] = None,
    minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """trade_audit_log 查询接口。"""
    conn = db.get_connection()
    try:
        sql = "SELECT * FROM trade_audit_log WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if decision_id is not None:
            sql += " AND decision_id = ?"
            params.append(int(decision_id))
        if asset:
            sql += " AND asset = ?"
            params.append(asset.upper())
        if minutes:
            sql += " AND created_at >= datetime('now', ?)"
            params.append(f"-{int(minutes)} minutes")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        cur = conn.execute(sql, params)
        # v421：`db.get_connection()` 不设 row_factory，行是 tuple，
        # `dict(r)` 必抛 TypeError（实测 /api/v190/exchange/audit 恒 500）。
        # 用 cursor.description 组列名，与 row_factory 无关。
        cols = [d[0] for d in (cur.description or [])]
        items = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"count": len(items), "rows": items}
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════
# v191 每日归档查询 endpoints
# ════════════════════════════════════════════════════════════════════════
@router.get("/api/v190/exchange/daily_summary")
def v190_exchange_daily_summary(
    date: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 30,
) -> Dict[str, Any]:
    """每日反思总结归档。

    date: YYYY-MM-DD(可选;不传=最近 N 条)
    run_id: 过滤特定 run(可选)
    limit: 最大条数(默认 30)
    """
    try:
        from daily_archive import query_daily_summary
    except Exception as exc:
        return {"error": f"daily_archive import failed: {exc}"}
    conn = db.get_connection()
    try:
        rows = query_daily_summary(
            conn, summary_date=date, run_id=run_id, limit=limit,
        )
        # JSON 字段反序列化(给前端直接用)
        for r in rows:
            for k in ("asset_breakdown", "action_breakdown", "layer_breakdown"):
                try:
                    if isinstance(r.get(k), str) and r[k].startswith("{"):
                        r[k] = json.loads(r[k])
                except Exception:
                    pass
        return {"count": len(rows), "rows": rows}
    finally:
        conn.close()


@router.get("/api/v190/exchange/daily_resets")
def v190_exchange_daily_resets(
    limit: int = 30,
    trigger: Optional[str] = None,
) -> Dict[str, Any]:
    """每日数据重置时点归档。

    trigger: auto_daily / manual / startup / mode_change / kill_switch
    """
    try:
        from daily_archive import query_daily_resets
    except Exception as exc:
        return {"error": f"daily_archive import failed: {exc}"}
    conn = db.get_connection()
    try:
        rows = query_daily_resets(conn, limit=limit, trigger=trigger)
        return {"count": len(rows), "rows": rows}
    finally:
        conn.close()


@router.get("/api/v190/exchange/signal_archive")
def v190_exchange_signal_archive(
    decision_id: Optional[int] = None,
    asset: Optional[str] = None,
    audit_status: Optional[str] = None,
    date: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """单笔决策的全链路快照(从 raw_news → ai_decisions → trade_audit_log → 反思)。

    任一查询参数都可选,组合过滤。
    """
    try:
        from daily_archive import query_signal_chain
    except Exception as exc:
        return {"error": f"daily_archive import failed: {exc}"}
    conn = db.get_connection()
    try:
        rows = query_signal_chain(
            conn, decision_id=decision_id, asset=(asset.upper() if asset else None),
            audit_status=audit_status, decision_date=date, limit=limit,
        )
        return {"count": len(rows), "rows": rows}
    finally:
        conn.close()


# 注：原 if __name__ == "__main__": 块（带 uvicorn.run）已被移至文件末尾，
# 因为 v170-P0+ admin 端点定义在原 __main__ 之后；uvicorn.run 同步阻塞会
# 阻止后续装饰器执行，导致 admin 路由无法注册。
__main_block_moved_to_end = True

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
