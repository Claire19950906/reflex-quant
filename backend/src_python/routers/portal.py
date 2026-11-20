# -*- coding: utf-8 -*-
"""投资人 portal / master 代理路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v300.1 那一簇 6 条：security/watermark、security/entry、
signal/{symbol}、backtest、batch、master/health，连带 _proxy_to_master 助手。

注意：/api/security/watermark 在本文件里与 v300 反爬块内那条**同路径重复**
（那条是缩进在 if 分支里注册的）。这里只是把 v300.1 这份搬走，重复问题本身
留给 P3 统一处理，不在切片里顺手删。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import config

logger = logging.getLogger(__name__)

router = APIRouter()


# v300.1 — 投资人 portal 端点 (轻量,不需要 master)
# =============================================================
# 给投资人 portal 用的两个端点:NDA 同意后调 watermark 拿专属 ID,
# 调 entry 记录入场。这两个**不需要 master**,所以即使 Mac 没开也能用。

@router.get("/api/security/watermark")
async def _investor_watermark(request: Request, investor_id: str = ""):
    """生成投资人专属水印 token。"""
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "unknown"))
    if not investor_id:
        return {"watermark": "", "investor_id": ""}
    # 用 watermark_v2 复用已有逻辑(如果 v300 块 import 了)
    try:
        from watermark_v2 import make_watermark_token
        token = make_watermark_token(
            investor_id=investor_id,
            ip=client_ip,
            ts=int(time.time()),
        )
        return {"watermark": token, "investor_id": investor_id, "ip": client_ip}
    except ImportError:
        # fallback:简单 hash
        import hashlib
        h = hashlib.sha256(f"{investor_id}:{client_ip}:{time.time()//3600}".encode()).hexdigest()
        return {"watermark": h, "investor_id": investor_id, "ip": client_ip}


@router.post("/api/security/entry")
async def _investor_entry(payload: dict, request: Request):
    """记录投资人入场(用于审计)。"""
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "unknown"))
    investor_id = (payload or {}).get("investor_id", "anon")
    ua = (payload or {}).get("ua", "")
    logger.info("[v300.1] investor entry: id=%s ip=%s ua=%s",
                investor_id, client_ip, ua[:80])
    # 尝试写审计(如果有 demo_audit)
    try:
        from demo_audit import record
        record(
            api_key_id=f"investor:{investor_id}",
            endpoint="/api/security/entry",
            ip=client_ip,
            ua=ua,
            decision="entry",
            extra={"investor_id": investor_id},
        )
    except Exception:
        pass
    return {"ok": True, "ts": int(time.time())}


# =============================================================
# v300.1 — Master 代理:把投资人请求转给 Mac 上的算法
# =============================================================
# 这一段是 Fly.io 镜像里的"代理层"代码
# 它接收投资人请求 → 加上 token → 转给 Mac 上的 master API → 拿到结果
# 然后过 demo 模式的脱敏 + 限流 + 水印 + 审计

_MASTER_URL = os.getenv("TRIDENT_MASTER_URL", "").rstrip("/")
_MASTER_TOKEN = os.getenv("TRIDENT_MASTER_TOKEN", "")
_MASTER_TIMEOUT = float(os.getenv("TRIDENT_MASTER_TIMEOUT", "10.0"))


async def _proxy_to_master(path: str, method: str = "GET", params=None, body=None):
    """把请求转发给 Mac 上的 master API."""
    if not _MASTER_URL:
        raise HTTPException(
            status_code=503,
            detail="TRIDENT_MASTER_URL not configured (Mac 上的 master API 没起来? "
                   "或者 cloudflared URL 没设到 Fly secrets)"
        )
    import httpx
    try:
        async with httpx.AsyncClient(timeout=_MASTER_TIMEOUT) as client:
            r = await client.request(
                method=method,
                url=f"{_MASTER_URL}{path}",
                params=params or {},
                json=body,
                headers={"Authorization": f"Bearer {_MASTER_TOKEN}"},
            )
        return JSONResponse(
            status_code=r.status_code,
            content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="master API timeout (Mac 关闭了? cloudflared 挂了?)")
    except httpx.ConnectError as e:
        raise HTTPException(status_code=502, detail=f"master API unreachable: {e}")
    except Exception as e:
        logger.exception("master proxy failed")
        raise HTTPException(status_code=500, detail=f"master proxy error: {e}")


@router.get("/api/signal/{symbol}")
async def _investor_signal(symbol: str, request: Request,
                          timeframe: str = "1h", lookback: int = 7):
    """投资人门户入口 — 转发给 master,然后过 demo 脱敏."""
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "unknown"))
    ua = request.headers.get("user-agent", "")
    logger.info("/api/signal/%s from %s ua=%s", symbol, client_ip, ua[:60])

    if _MASTER_URL:
        # 代理模式:Fly.io 转发给 Mac
        resp = await _proxy_to_master(
            f"/signal/{symbol.upper()}",
            method="GET",
            params={"timeframe": timeframe, "lookback": lookback},
        )
        return resp
    else:
        # 本地模式:如果 master_api 直接跑在本地,直接调用
        try:
            from master_api import trident_compute_signal
            start = time.time()
            result = trident_compute_signal(
                symbol=symbol.upper(),
                params={"timeframe": timeframe, "lookback": lookback},
            )
            return {"ok": True, "symbol": symbol.upper(),
                    "ts": int(time.time()),
                    "latency_ms": round((time.time() - start) * 1000, 1),
                    **result}
        except NotImplementedError as e:
            raise HTTPException(status_code=501, detail=str(e))
        except Exception as e:
            logger.exception("local master failed")
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.post("/api/backtest")
async def _investor_backtest(payload: dict, request: Request):
    """回测入口 — 转发给 master."""
    if _MASTER_URL:
        return await _proxy_to_master("/backtest", method="POST", body=payload)
    return {"ok": False, "error": "backtest not implemented locally"}


@router.post("/api/batch")
async def _investor_batch(payload: dict, request: Request):
    """批量信号 — 转发给 master."""
    if _MASTER_URL:
        return await _proxy_to_master("/batch", method="POST", body=payload)
    return {"ok": False, "error": "batch not implemented locally"}


@router.get("/api/master/health")
async def _master_health():
    """健康检查 — Fly.io 镜像本身."""
    return {"ok": True, "service": "trident-proxy", "ts": int(time.time())}
