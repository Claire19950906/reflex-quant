# -*- coding: utf-8 -*-
"""聪明钱（Hyperliquid 钱包追踪）路由（结构切片，2026-11-17）。

从 api_server.py 搬来 /api/smart-money/fills 与 /state 两条端点 +
_HL_* 常量与 _hl_info 助手（含超时 + 5s 内存缓存）。
设计取舍（原注释保留）：
  · 不上 WebSocket，前端 30s 轮询 + 后端 5s 缓存吸收洪峰；
  · 不持久化，watchlist 在前端 localStorage，后端无状态；
  · 地址只允许 0x + 40 位 hex；
  · Info API 公开免鉴权，但做超时与 5xx 友好返回。
"""
from __future__ import annotations

import concurrent.futures as _cf
import re as _re
import time
from typing import Any, Dict, Optional

import logging

import requests
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


# v499: 进程级共享池（见 /batch 注释）
_SHARED_POOL = _cf.ThreadPoolExecutor(max_workers=20, thread_name_prefix="sm-inner")
# 外层池单独一个：_one 内部还要 submit 到 _SHARED_POOL 并等结果，
# 若内外共用一个池，外层任务占满 worker 后内层子任务会排队等不到线程。
_OUTER_POOL = _cf.ThreadPoolExecutor(max_workers=6, thread_name_prefix="sm-outer")

_HL_ADDR_RE = _re.compile(r"^0[xX][0-9a-fA-F]{40}$")


_HL_INFO_URL = "https://api.hyperliquid.xyz/info"


_HL_INFO_TIMEOUT_S = 5


_HL_CACHE: Dict[str, tuple[float, Any]] = {}  # {cache_key: (expires_at, value)}


_HL_CACHE_TTL_S = 30.0


def _hl_info(payload: Dict[str, Any], cache_key: str) -> Dict[str, Any]:
    """POST 到 Hyperliquid Info，5s 内存缓存（同 key 同参数共用）。

    v498：本机到 api.hyperliquid.xyz 的 TLS 会间歇性被重置（requests 直接抛
    SSLError，同一时刻系统 curl 却能通），面板一加地址就"加载失败"。改成
    **curl 优先（8s 上限）→ requests 兜底**，与 lsr_feed 同一套做法；
    之前的 requests 优先会先白等两轮 6s 超时，单地址能拖到 17s。
    """
    now = time.time()
    hit = _HL_CACHE.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]

    last_err: Optional[str] = None
    try:
        import json as _json
        import subprocess as _sp
        p = _sp.run(
            ["/usr/bin/curl", "-s", "--compressed", "-m", "6", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", _json.dumps(payload), _HL_INFO_URL],
            capture_output=True, text=True, timeout=10,
        )
        out = (p.stdout or "").strip()
        if out:
            data = _json.loads(out)
            _HL_CACHE[cache_key] = (now + _HL_CACHE_TTL_S, data)
            return data
        last_err = f"curl empty (exit {p.returncode})"
    except Exception as e:
        last_err = f"curl: {repr(e)[:120]}"

    # curl 再试一次（8s 上限）：本机 TLS 握手经常第一次被重置、第二次就通
    try:
        import json as _json2
        import subprocess as _sp2
        p2 = _sp2.run(
            ["/usr/bin/curl", "-s", "--compressed", "-m", "6", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", _json2.dumps(payload), _HL_INFO_URL],
            capture_output=True, text=True, timeout=10,
        )
        out2 = (p2.stdout or "").strip()
        if out2:
            data2 = _json2.loads(out2)
            _HL_CACHE[cache_key] = (now + _HL_CACHE_TTL_S, data2)
            return data2
    except Exception:
        pass

    try:
        r = requests.post(
            _HL_INFO_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=_HL_INFO_TIMEOUT_S,
        )
        if r.status_code == 200:
            # v500: 200 但返回 HTML/非 JSON（网关拦截页）→ ValueError 原先会漏成 500。
            # 对调用方语义应是"上游不可用"，统一归 502。
            data = r.json()
            _HL_CACHE[cache_key] = (now + _HL_CACHE_TTL_S, data)
            return data
        last_err = f"http {r.status_code}: {r.text[:120]}"
    except ValueError as e:
        last_err = f"non-json response: {repr(e)[:80]}"
    except requests.Timeout:
        last_err = "timeout"
    except requests.RequestException as e:
        last_err = repr(e)[:160]

    # v498: 三条路都失败时，回退到该地址上一次成功结果（哪怕已过期）。
    #   宁可显示"最多 30 分钟前"的真实成交，也不要整块变"加载失败"。
    if hit is not None:
        return hit[1]

    if last_err == "timeout":
        raise HTTPException(status_code=504, detail="hyperliquid info timeout")
    raise HTTPException(status_code=502, detail=f"hyperliquid unreachable: {last_err}")


@router.get("/api/smart-money/fills", tags=["smart-money"])
def _smart_money_fills(address: str):
    """单个钱包最近成交(最多 2000 笔,Hyperliquid userFills 不带时间过滤,
    由前端按 time 过滤到「近 N 小时」)."""
    addr = (address or "").strip()
    if not _HL_ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail="address must be 0x + 40 hex chars")
    addr_lc = addr.lower()
    # v498: 原来拉 userFills（最多 2000 笔、单地址 ~560KB），HFT 钱包一次要 5–17s，
    #   面板 3 个地址并发就会打满 12s 超时。改为 userFillsByTime + 12h 窗口，
    #   只取面板真正会显示的时间段，payload 与耗时都降一个量级。
    start_ms = int(time.time() * 1000) - 12 * 3600 * 1000
    raw = _hl_info(
        {"type": "userFillsByTime", "user": addr_lc, "startTime": start_ms},
        f"fills12h:{addr_lc}",
    )
    if not isinstance(raw, list):
        raise HTTPException(status_code=502, detail=f"hyperliquid unexpected shape: {type(raw).__name__}")
    # 标注方向人类可读:side="B/A",dir="Open Long/Close Long/..."  → 标准化
    norm = []
    for f in raw:
        try:
            norm.append({
                "tid": f.get("tid"),
                "time": f.get("time"),  # ms epoch
                "coin": f.get("coin"),
                "px": f.get("px"),
                "sz": f.get("sz"),
                "side": f.get("side"),          # "B"=buy, "A"=sell
                "dir": f.get("dir"),            # "Open Long" / "Close Short" / ...
                "closed_pnl": f.get("closedPnl"),
                "fee": f.get("fee"),
                "fee_token": f.get("feeToken"),
                "oid": f.get("oid"),
                "hash": f.get("hash"),
                "crossed": f.get("crossed", False),
            })
        except Exception:
            logger.exception("smart_money: skip malformed fill for %s", addr_lc[:10])
    return {
        "ok": True,
        "address": addr_lc,
        "ts": int(time.time() * 1000),
        "count": len(norm),
        "fills": norm,
    }


@router.get("/api/smart-money/state", tags=["smart-money"])
def _smart_money_state(address: str):
    """单个钱包当前持仓 + 账户净值(clearinghouseState).
    持仓数组为空的账户照样返回(就是「0 持仓」,正常状态)."""
    addr = (address or "").strip()
    if not _HL_ADDR_RE.match(addr):
        raise HTTPException(status_code=400, detail="address must be 0x + 40 hex chars")
    addr_lc = addr.lower()
    raw = _hl_info({"type": "clearinghouseState", "user": addr_lc}, f"state:{addr_lc}")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail=f"hyperliquid unexpected shape: {type(raw).__name__}")
    margin = raw.get("marginSummary") or {}
    positions = []
    for ap in (raw.get("assetPositions") or []):
        pos = (ap or {}).get("position") or {}
        lev = (ap or {}).get("leverage") or {}
        positions.append({
            "coin": pos.get("coin"),
            "side": "long" if float(pos.get("szi", "0") or 0) > 0 else "short" if float(pos.get("szi", "0") or 0) < 0 else "flat",
            "size": pos.get("szi"),
            "entry_px": pos.get("entryPx"),
            "position_value": pos.get("positionValue"),
            "unrealized_pnl": pos.get("unrealizedPnl"),
            "leverage_value": lev.get("value"),
            "leverage_type": lev.get("type"),
            "liquidation_px": pos.get("liquidationPx"),
        })
    return {
        "ok": True,
        "address": addr_lc,
        "ts": int(time.time() * 1000),
        "account_value": margin.get("accountValue"),
        "total_ntl_pos": margin.get("totalNtlPos"),
        "total_raw_usd": margin.get("totalRawUsd"),
        "total_margin_used": margin.get("totalMarginUsed"),
        "withdrawable": raw.get("withdrawable"),
        "positions": positions,
    }

@router.get("/api/smart-money/batch", tags=["smart-money"])
def _smart_money_batch(addresses: str):
    """v498: 一次返回多个钱包的 fills + state。

    背景：浏览器同源连接只有 6 条，6 个地址 × 2 个端点 = 12 个请求会互相排队，
    面板一直停在"拉取中"；而 curl 直连却很快。改成前端只发 1 个请求、后端用
    线程池并发拉上游，既绕开浏览器连接上限，也把总耗时压到单地址水平。
    """
    import concurrent.futures as _cf

    # v499: 单地址内部也并发 —— fills 与 state 各一次上游调用，串行时 6 地址
    #   实测 13–21s，超过 Next 代理 30s 上限时会直接 500（面板永远"拉取中"）。
    #   内外两层池分开：外层按地址、内层按端点，避免同一线程池互相等待死锁。
    # v499: 原来每次请求都新建 pool + _inner 两个线程池（实测进程里已累积到 354 个
    #   ThreadPoolExecutor），每个连接还要重载一次 CA 证书，单核 CPU 被顶上 20%、
    #   同步接口在这期间排队。改成进程级共享池，请求之间复用线程。
    _inner = _SHARED_POOL

    def _safe(kind: str, addr: str) -> Dict[str, Any]:
        try:
            return _smart_money_fills(addr) if kind == "fills" else _smart_money_state(addr)
        except HTTPException as e:
            return {"ok": False, "error": e.detail}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:120]}

    def _one(addr: str) -> Dict[str, Any]:
        fa = _inner.submit(_safe, "fills", addr)
        sa = _inner.submit(_safe, "state", addr)
        return {"address": addr.strip().lower(), "fills": fa.result(), "state": sa.result()}

    items = [a for a in (addresses or "").split(",") if a.strip()]
    items = items[:12]
    bad = [a for a in items if not _HL_ADDR_RE.match(a.strip())]
    if bad:
        raise HTTPException(status_code=400, detail=f"bad address: {bad[0][:20]}")
    results = list(_OUTER_POOL.map(_one, items))
    return {"ok": True, "ts": int(time.time() * 1000), "items": results}

