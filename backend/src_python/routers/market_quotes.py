# -*- coding: utf-8 -*-
"""行情快照 / 涨跌排行 / 门卫反思路由（结构切片，2026-11-17）。

从 api_server.py 搬来 4 条端点：
  /api/market/prices（v398: 先问 8100，失败回退外网多源）
  /api/market/price-at（P2-13 按时刻反查最近 K 线）
  /api/market/movers（v341 120s 缓存 + v398-fix 参数原样透传）
  /api/market/gate-reflection（门卫正向/反向复盘）

助手与缓存在 api_server.py 原位保留：`_cached_market_prices` 有 4 处内部调用、
`_cached_quick_sim` 1 处、`_MOVERS_CACHE` 被 lifespan 两个预热线程直接写、
`_movers_sync` 与 `_detect_5m_abnormals` 另有调用点。这里用模块级 `_api()`
延迟回引，调用时 api_server 已完成初始化，不会循环 import。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter()


def _api():
    """延迟回引 api_server（模块导入期不可用，函数调用期一定已就绪）。"""
    import api_server
    return api_server


@router.get("/api/market/prices")
async def get_market_prices():
    """行情快照（顶栏 + 分析上下文用）。

    v398: 先问 8100（1s，读本地 K 线），失败才回退外网多源（13.5s，行为同以前）。
    回退路径保留，保证 8100 挂了页面还有价可看。
    """
    _pb = await asyncio.to_thread(_api()._fetch_prices_from_bridge)
    if _pb:
        return _pb
    return await asyncio.to_thread(_api()._cached_market_prices)


@router.post("/api/market/price-at")
async def post_market_price_at(body: Dict[str, Any]):
    asset = (body.get("asset") or "").upper().strip()
    ts_iso = (body.get("ts") or "").strip()
    timeframe = (body.get("timeframe") or "1m").strip()
    if not asset or not ts_iso:
        return {"ok": False, "error": "asset / ts required", "code": 400}
    return await asyncio.to_thread(_api()._price_at_sync, asset, ts_iso, timeframe)


@router.get("/api/market/movers")
async def get_market_movers(timeframe: str = "24h", limit: int = 20, full: bool = False):
    """行情·事件归因页：全品种涨跌排行 + 各品种关联新闻事件。

    参数与 8100 完全一致（timeframe 默认 24h、limit 默认 20、full 默认 false）——
    8000 这份只是 8100 的代理，任何"悄悄改默认值"都会让两端返回不同内容。

    v170: 支持 timeframe=10m（10 分钟 K 线聚合）→ 异动检测自动切到 10m。

    事件标注只做「时间就近 + 多源并列」，不做因果断言（无硬归因）。

    v341: 实测单次要 37 秒、返回 778KB。前端 30 秒轮询一次，
      等于每一轮都在等一个 37 秒的请求，bridge 转发 25 秒超时就先放弃了，
      页面因此长期是空的。这里加一层结果缓存（同参数 120 秒内复用），
      首轮仍然慢，但之后的轮询全部命中缓存。数据本身是 24h 排行，120 秒不会失真。

    v398-fix: 不再把 timeframe 硬映射成 3m（见 _api()._fetch_movers_from_bridge 注释）。
    """
    tf = (timeframe or "24h").strip().lower()
    if tf not in _api()._MOVERS_TF_OK:
        tf = "24h"
    _ck = _api()._movers_cache_key(tf, limit, full)
    hit = _api()._MOVERS_CACHE.get(_ck)
    now = time.monotonic()
    if hit and now - hit[0] < _api()._MOVERS_CACHE_TTL_S:
        return hit[1]
    # v376: api 不再自己重算 —— _api()._movers_sync 单次要 30s、瞬时把 api RSS 顶到 480MB，
    #   在 2GB 机器上直接触发 jetsam。8100 已经有同一份缓存，直接取过来即可。
    try:
        _mbody = _api()._fetch_movers_from_bridge(tf, limit, full)
        if _mbody:
            _api()._MOVERS_CACHE[_ck] = (now, _mbody)
            return _mbody
    except Exception as _mexc:
        print(f"[MOVERS] 取 8100 失败, 回退本地重算: {type(_mexc).__name__}", flush=True)
    data = await asyncio.to_thread(_api()._movers_sync, tf)
    _api()._MOVERS_CACHE[_ck] = (now, data)
    return data


@router.get("/api/market/gate-reflection")
async def get_gate_reflection(asset: str = "BTC", days: int = 3):
    """门卫反思（第一层 AI 语义筛选复盘）：正向（判断兑现）+ 反向（异动漏检），绑定选中品种。"""
    asset = (asset or "BTC").upper()
    return await asyncio.to_thread(_api()._gate_reflection_sync, asset, days)


# ─── 行情历史 / 归因 / 订单簿深度（2026-11-17 从 api_server 追加）──────────

@router.get("/api/market/history/{asset}")
async def get_market_history(
    asset: str, interval: str = "1h", limit: int = 72, days_back: int = 0,
):
    """行情·事件归因页走势数据：返回某品种 close 价时间序列（Area 折线用）。

    统一真实源：加密→币安永续、XAU→OKX 永续、WTI→库内 kline_bars(4h)。
    拿不到真实 K 线时返回 points=[] + missing 说明，前端据实提示，绝不 mock 造线。

    v221.7: 加 days_back 参数支持 30 天跨周回溯。
      days_back=0   → 默认行为：单页拉取（≤24h），limit 截断
      days_back=7/14/30 → 分页拉取（30d × 144/d for 10m ≈ 4320 bars）
    """
    return await asyncio.to_thread(_api()._history_sync, asset, interval, limit, days_back)


@router.get("/api/market/attribute")
async def get_market_attribute(asset: str, ts: int):
    """按需异动 AI 归因：直接回答「该异动时间点发生了什么」。

    复用 3m 异动归因链路（attribute_5m_abnormal）：命中 _5M_ATTRIB_CACHE 直接返回；
    否则取该时间锚前窗口内的公开新闻现场归因（后台同步调用，前端点按触发）。
    """
    return await asyncio.to_thread(_api()._attribute_sync, asset, ts)


@router.get("/api/market/depth/{asset}")
async def get_market_depth(asset: str, limit: int = 100):
    """实时订单簿深度（Binance 现货 / OKX 永续）：归因 K 线叠加用。
    返回 {status, asset, bids, asks, ts_ms, source}。
    bids 升序、asks 升序；limit 5-100；缓存 10s。
    """
    return await asyncio.to_thread(_api()._depth_sync, asset, limit)
