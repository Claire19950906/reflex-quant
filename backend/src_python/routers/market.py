# -*- coding: utf-8 -*-
"""市场状态 / 跨市场行情路由（结构切片，2026-11-17）。

从 api_server.py 搬来 9 条端点：v188/markets/status（列表 + 单品种）、
v189/market 的 es / nq / us10y / us5y / yield-curve-spread / funding-rate / all，
连带 _load_market_cache_from_disk（模块导入时立即执行一次）与 _market_all_warmer
两个助手 —— 二者在段外零调用点，随段搬走。

注意：段内保留了 `_load_market_cache_from_disk()` 这个**模块级调用**，
它的执行时机仍是 api_server import 本模块时，行为与原来一致。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import db

router = APIRouter()


@router.get("/api/v188/markets/status")
async def v188_markets_status():
    """v188+: 全球市场实时开闭市状态(WTI/XAU/BTC/美股 4 类)。零依赖、纯静态时区表。"""
    try:
        from market import market_hours as _mh
        return {"ok": True, "ts": int(time.time()), "markets": _mh.current_status()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# v189-3: ES/NQ/US10Y + 永续资金费率 (数据源扩充)
@router.get("/api/v189/market/es")
async def v189_market_es():
    """E-mini S&P 500 期货 — 美股开盘前先行指标。"""
    try:
        from feeds import es_feed
        return {"ok": True, "data": es_feed.fetch_es()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/market/nq")
async def v189_market_nq():
    """E-mini Nasdaq 100 期货 — BTC 风险情绪先行指标。"""
    try:
        from feeds import nq_feed
        return {"ok": True, "data": nq_feed.fetch_nq()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/market/us10y")
async def v189_market_us10y():
    """美国 10 年期国债收益率 (^TNX)。"""
    try:
        from feeds import us10y_feed
        return {"ok": True, "data": us10y_feed.fetch_us10y()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/market/us5y")
async def v189_market_us5y():
    """美国 5 年期国债收益率 (^FVX)。"""
    try:
        from feeds import us10y_feed
        return {"ok": True, "data": us10y_feed.fetch_us5y()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/market/yield-curve-spread")
async def v189_market_yield_curve_spread():
    """10Y-5Y 利差 (bp)。负值 = 倒挂 (衰退信号)。"""
    try:
        from feeds import us10y_feed
        return {"ok": True, "data": us10y_feed.fetch_yield_curve_spread()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/market/funding-rate")
async def v189_market_funding_rate(instrument: Optional[str] = None):
    """永续合约资金费率 (OKX 公共 API)。

    不传 instrument: 返回 BTC/ETH/SOL/XAU 4 个主流永续
    传 instrument=BTC-USDT-SWAP: 单个
    """
    try:
        from feeds import funding_rate_feed
        if instrument:
            return {"ok": True, "data": funding_rate_feed._fetch_one(instrument)}
        return funding_rate_feed.fetch_funding_rates()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


FEED_TIMEOUT_S = 20
_MARKET_ALL_CACHE: Dict[str, Any] = {}

def _load_market_cache_from_disk() -> None:
    """v366: 冷启动读盘快照（<=3h 内有效），把首屏等待从 16s 降到 0。"""
    try:
        import json as _json, os as _os, time as _t
        _fp = _os.path.expanduser("~/.trident/cache/market_all.json")
        with open(_fp, "r", encoding="utf-8") as _f:
            _blob = _json.load(_f)
        _age = _t.time() - float(_blob.get("ts") or 0)
        if 0 < _age <= 3 * 3600 and isinstance(_blob.get("data"), dict):
            _data = _blob["data"]
            _data["snapshot_age_s"] = int(_age)   # 让前端知道这是盘上快照
            _MARKET_ALL_CACHE["all"] = {"ts": _blob["ts"], "data": _data}
            print(f"[market/all] 已载入磁盘快照 age={int(_age)}s", flush=True)
    except Exception:
        pass

_load_market_cache_from_disk()
_MARKET_ALL_TTL = 60.0


async def _compute_market_all():
    """真正去拉所有源（慢，5Y 可能 10~40s）。只给预热线程和冷启动用。"""
    import asyncio as _asyncio
    _now = time.time()
    out: Dict[str, Any] = {"ok": True, "ts": int(_now), "sources": {}}
    try:
        from feeds import es_feed
        from feeds import nq_feed
        from feeds import us10y_feed
        from feeds import funding_rate_feed
        from feeds import dollar_index_feed

        def _safe(fn, *args):
            # v189-3-fix: 单个 feed 失败/返回 None 不阻塞其他, 返回 {'ok': False, 'error': ...}
            try:
                r = fn(*args)
                if r is None:
                    return {"ok": False, "error": "feed returned None"}
                # yield_curve_spread 等返回 raw dict (没 ok 字段) — 视为成功
                if "ok" not in r and not isinstance(r, list):
                    r = {"ok": True, **r} if isinstance(r, dict) else {"ok": True, "data": r}
                return r
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # v189-3-fix: 并发执行所有 feed (executor 跑阻塞 IO), 总耗时≈最慢那个而不是总和.
        # 注: yfinance 对短时间高并发易触发 rate limit (会返回 'possibly delisted'),
        # 所以并发数限制在 3 个一组, 避免被 yfinance 屏蔽.
        loop = _asyncio.get_event_loop()

        async def _run_one(fn):
            # v350: 单源硬超时。任何一个源（尤其 yfinance）挂住时，
            # 只把这一项标成缺失，整个接口照常返回，不再陪它一起卡 40s+。
            try:
                return await _asyncio.wait_for(loop.run_in_executor(None, _safe, fn), timeout=FEED_TIMEOUT_S)
            except _asyncio.TimeoutError:
                return {"ok": False, "error": f"timeout>{FEED_TIMEOUT_S}s"}

        # 第一组: 期货 (ES/NQ) — 内部走 yfinance 但 ticker 不同, cache miss
        grp1 = await _asyncio.gather(
            _run_one(es_feed.fetch_es),
            _run_one(nq_feed.fetch_nq),
            _run_one(dollar_index_feed.fetch_dxy),
        )
        # 第二组: 国债 + 利差
        grp2 = await _asyncio.gather(
            _run_one(us10y_feed.fetch_us10y),
            _run_one(us10y_feed.fetch_us5y),
            _run_one(us10y_feed.fetch_yield_curve_spread),
        )
        # 第三组: funding rate (OKX 公开 API, 跟 yfinance 无关)
        grp3 = await _asyncio.gather(
            _run_one(funding_rate_feed.fetch_funding_rates),
        )
        results = list(grp1) + list(grp2) + list(grp3)
        out["sources"]["es"] = results[0]
        out["sources"]["nq"] = results[1]
        out["sources"]["dxy"] = results[2]
        out["sources"]["us10y"] = results[3]
        out["sources"]["us5y"] = results[4]
        out["sources"]["yield_curve_spread"] = results[5]
        out["sources"]["funding_rate"] = results[6]
        # v189-3-fix: ok 改为仅当所有 source 都 ok 才 True.
        # ok=True 只表示"本次有返回值"；每个源的可用性看各自的 ok / error
        out["ok"] = True
        out["partial"] = not all((r or {}).get("ok", False) for r in results)
        # v355: 本轮失败的源，沿用上一轮的真实值（打上 stale 标记），
        # 避免"一次全部失败 → 空结果被缓存 → 页面永久空白"。
        _prev = (_MARKET_ALL_CACHE.get("all") or {}).get("data") or {}
        for _k, _v in (_prev.get("sources") or {}).items():
            _new = out["sources"].get(_k)
            _empty = (not isinstance(_new, dict)) or (
                _new.get("ok") is False and all(
                    _new.get(_f) is None for _f in
                    ("price", "yield_pct", "spread_bp", "items")
                )
            )
            if _empty and isinstance(_v, dict):
                _carry = dict(_v)
                _carry["stale"] = True          # 前端可据此标"沿用上次值"
                out["sources"][_k] = _carry
        _MARKET_ALL_CACHE["all"] = {"ts": _now, "data": out}
        # v366: 把快照落盘 —— 重启后第一枪直接读盘，不必冷启动重算 16s
        try:
            import json as _json, os as _os
            _d = _os.path.expanduser("~/.trident/cache")
            _os.makedirs(_d, exist_ok=True)
            with open(_os.path.join(_d, "market_all.json"), "w", encoding="utf-8") as _f:
                _json.dump({"ts": _now, "data": out}, _f, ensure_ascii=False)
        except Exception:
            pass
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/market/all")
async def v189_market_all():
    """前端入口：永远秒回。

    v352: 以前缓存过期就当场重算，前端 usePoll 8s 就放弃 → 页面在"有数/全 —"之间闪。
    改成 stale-while-revalidate：有旧数据就直接给旧数据，刷新交给后台预热线程。
    """
    _cached = _MARKET_ALL_CACHE.get("all")
    if _cached:
        return _cached["data"]
    return await _compute_market_all()


def _market_all_warmer():
    """v351: 后台预热 /market/all。

    冷启动时 5Y 要爬财政部 CSV（10~50s），前端 usePoll 8s 就放弃 → 页面永远空。
    这里每 30s 跑一次，让缓存始终是热的，浏览器请求基本都走缓存（实测 0.01s）。
    """
    import asyncio as _a
    time.sleep(20)           # v365: 90s → 20s（冷启动窗口太长，前端开局全 "—"）
    interval = 60.0
    while True:
        _t0 = time.monotonic()
        try:
            _a.run(_compute_market_all())
        except Exception:
            pass
        _elapsed = time.monotonic() - _t0
        # v367: 自适应退避。单轮重算 >8s 说明这台机器已经没有余量（GIL/IO 被占满）,
        # 继续每 60s 重算只会让请求线程一起排队（实测 /api/health/models 被拖到 25s）:
        # 健康时保持 60s（首屏仍走热缓存）; 吃力时按倍数后退, 上限 15 分钟。
        if _elapsed > 8.0:
            interval = min(interval * 2.0, 900.0)
            print(f"[market/all] 本轮 {_elapsed:.1f}s > 8s → 预热退避到 {int(interval)}s", flush=True)
        else:
            interval = 60.0
        time.sleep(interval)


try:
    import threading as _threading
    _threading.Thread(target=_market_all_warmer, daemon=True, name="market-all-warmer").start()
    print("[market/all] 后台预热线程已启动", flush=True)
except Exception as _e:
    print(f"[market/all] 预热线程启动失败: {_e}", flush=True)


@router.get("/api/v188/markets/status/{asset}")
async def v188_market_status_for_asset(asset: str):
    """v188+: 单个品种状态(路径: btc/xau/wti/us_equity)。"""
    try:
        from market import market_hours as _mh
        st = _mh.status_for(asset)
        if st is None:
            return {"ok": False, "error": f"unknown asset: {asset}"}
        return {"ok": True, "ts": int(time.time()), "market": st}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# v189-3: 回测归档 admin 端点
