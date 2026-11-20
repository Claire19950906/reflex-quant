# -*- coding: utf-8 -*-
"""宏观面路由（结构切片，2026-11-17）。

从 api_server.py 整簇搬来 3 条：/api/macro/context（盘面结构/舆情/加息预期/资金流向）、
/api/macro/history（macro_snapshots 近 N 小时序列）、/api/macro/calendar（FF + 金十双源日历）。

连带搬走的助手与缓存（AST 核对：段外零调用点）：
  _MACRO_CTX_CACHE / _MACRO_CTX_TTL_S / _MACRO_HISTORY_DEFAULT_KEYS /
  _MACRO_HISTORY_CACHE / _MACRO_HISTORY_TTL_S / _macro_history_sync / _macro_calendar_sync

context 那条的 5 分钟缓存 + 每步 10s 硬超时是 v361 P3 的成果（原来单次 40s+，前端 30s
轮询会把页面拖死），逐字保留，没有为了"看起来干净"去简化超时。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter

from feeds import bls_feed
import config
import db
import macro_context

# BLS 面板短缓存：页面 30s 轮询不直接打 BLS API（v2 免费仅 500 次/日）；
# 缓存 10 分钟 → 页面高频轮询只读缓存，实际 API 调用 ~144 次/日（安全）。
# 只服务 macro/context，随簇搬来。
_BLS_SNAPSHOT_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_BLS_SNAPSHOT_TTL_S = 600.0

router = APIRouter()


_MACRO_CTX_CACHE: Dict[str, Any] = {"ts": 0.0, "value": None}
_MACRO_CTX_TTL_S = 300.0


@router.get("/api/macro/context")
async def get_macro_context(refresh: int = 0):
    """盘面结构 / 舆情 / 加息预期 / 资金流向。refresh=1 才打外部源。

    v361 P3: 原实现实测单次 40s+（BLS/FRED 走代理串行超时），前端 30s 轮询会把它
    反复打进线程池，页面其他卡片一起被拖慢。现在 5 分钟缓存 + 每步 10s 硬超时，
    超时就用上一次成功的快照，宁可稍旧也不许拖死页面。
    """
    if refresh:
        pack = await asyncio.to_thread(macro_context.build_context)
        _MACRO_CTX_CACHE.update({"ts": time.monotonic(), "value": pack})
        return pack
    _cached = _MACRO_CTX_CACHE.get("value")
    if _cached is not None and time.monotonic() - float(_MACRO_CTX_CACHE.get("ts", 0.0)) < _MACRO_CTX_TTL_S:
        return _cached
    try:
        latest = await asyncio.wait_for(asyncio.to_thread(macro_context.latest_snapshot), timeout=10.0)
    except Exception:
        latest = None
    if not latest:
        return _cached or {"status": "empty", "items": [], "note": "尚无沉淀，带 refresh=1 拉取"}
    # ── 附加 BLS 官方一手数据（CPI/非农/失业率，页面日历面板展示用）──
    # BLS 不落 macro_snapshots（避免与发布检测 watcher 抢配额），页面访问时
    # 拉最新值（带 10min 缓存：页面 30s 轮询只读缓存，实际 API 调用 ~144 次/日，
    # 远低于 v2 免费 500 次/日）。key 未配置/拉取失败 → 不附加（前端面板显示 N/A）。
    items = list(latest)
    try:
        now = time.monotonic()
        cached = _BLS_SNAPSHOT_CACHE.get("value")
        if cached is None or now - float(_BLS_SNAPSHOT_CACHE.get("expires", 0.0)) >= _BLS_SNAPSHOT_TTL_S:
            try:
                bls_rows = await asyncio.wait_for(asyncio.to_thread(bls_feed.fetch_snapshot), timeout=8.0)
            except Exception:
                bls_rows = []
            _BLS_SNAPSHOT_CACHE.update({"expires": time.monotonic(), "value": bls_rows})
        else:
            bls_rows = cached
        for row in bls_rows:
            if row.get("status") == "ok" and row.get("value") is not None:
                items.append(row)
    except Exception:
        pass
    pack = {"status": "ok", "items": items}
    _MACRO_CTX_CACHE.update({"ts": time.monotonic(), "value": pack})
    return pack


# -- 资金流动页 · 历史时间序列（资金费率/CVD/BTC ETF 净流入动态趋势图） ----------

_MACRO_HISTORY_DEFAULT_KEYS = (
    "BTC.funding", "ETH.funding", "SOL.funding", "XAU.funding",
    "btc_etf_net", "BTC.cvd", "ETH.cvd", "SOL.cvd",
)
_MACRO_HISTORY_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "value": None}
_MACRO_HISTORY_TTL_S = 120.0  # 秒：前端 30s 轮询只读缓存，实际查库 ~720 次/日


def _macro_history_sync(hours: int, keys: List[str]) -> Dict[str, Any]:
    """从 macro_snapshots 取近 N 小时的指定 metric_key 时间序列（升序）。"""
    window_hours = max(1, min(int(hours or 48), 24 * 30))
    since = int(time.time()) - window_hours * 3600
    wanted = [k for k in keys if k]
    if not wanted:
        return {"status": "empty", "series": {}}
    conn = db.get_connection()
    try:
        qmarks = ",".join("?" for _ in wanted)
        rows = conn.execute(
            f"SELECT ts, metric_key, value FROM macro_snapshots "
            f"WHERE metric_key IN ({qmarks}) AND ts >= ? AND status='ok' AND value IS NOT NULL "
            f"ORDER BY ts ASC, id ASC",
            (*wanted, since),
        ).fetchall()
    except Exception as exc:
        return {"status": "error", "series": {}, "note": f"{type(exc).__name__}"}
    finally:
        conn.close()
    series: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        key = str(row[1])
        series.setdefault(key, []).append({"ts": int(row[0]), "value": float(row[2])})
    # 同一 ts 保留最后一条，防重复落库抖动
    for key, pts in series.items():
        dedup: Dict[int, Dict[str, Any]] = {}
        for p in pts:
            dedup[p["ts"]] = p
        series[key] = list(dedup.values())
    return {"status": "ok", "series": series}


@router.get("/api/macro/history")
async def get_macro_history(hours: int = 48, keys: str = ""):
    """资金流动页动态趋势图数据源：返回指定 metric_key 的历史时间序列。

    keys 留空则返回默认资金流核心指标（资金费率/BTC ETF 净流入/CVD）。
    """
    read_keys = [k.strip() for k in (keys or "").split(",") if k.strip()]
    cache_key = f"{int(hours)}:{','.join(read_keys)}"
    now = time.monotonic()
    cached = _MACRO_HISTORY_CACHE.get("value")
    if (
        cached
        and _MACRO_HISTORY_CACHE.get("key") == cache_key
        and now - float(_MACRO_HISTORY_CACHE.get("ts", 0.0)) < _MACRO_HISTORY_TTL_S
    ):
        return cached
    if not read_keys:
        read_keys = list(_MACRO_HISTORY_DEFAULT_KEYS)
    payload = await asyncio.to_thread(_macro_history_sync, hours, read_keys)
    _MACRO_HISTORY_CACHE.update({"ts": now, "key": cache_key, "value": payload})
    return payload


@router.get("/api/macro/calendar")
async def get_macro_calendar(
    hours: int = 168,         # 默认 7 天窗口（FF 主要 week view）
    min_impact: str = "low",  # low / med / high
    min_star: int = 0,        # v226: 0=不限(走min_impact); 1-5=只显示≥此星
    sources: str = "ff,jin10",  # 逗号分隔
    limit: int = 200,
):
    """v170: 多源宏观日历查询接口（ForexFactory + 金十双源合并）。

    - 默认从 macro_calendar 表查未来 hours 小时内的事件；
    - min_impact: low（全显示）/ med（中+高）/ high（只高影响）；
    - min_star (v226): 1-5 颗星阈值（与 min_impact 取交集，max(star) 字段）；
      0 = 不按 star 过滤（仅 min_impact）；
    - sources: 逗号分隔的 source 列表；
    - 按 event_time 升序返回，最多 limit 条。

    返回: {events: [...], fetched_at, sources_used, counts_by_source}
    """
    valid_impact = {"low": 0, "med": 1, "high": 2}
    min_rank = valid_impact.get(str(min_impact).lower(), 0)
    min_star = max(0, min(5, int(min_star or 0)))
    src_list = [s.strip() for s in (sources or "").split(",") if s.strip()]
    if not src_list:
        src_list = ["ff", "jin10"]
    payload = await asyncio.to_thread(
        _macro_calendar_sync, hours, min_rank, src_list, int(limit),
        str(min_impact).lower(), min_star)
    return payload


def _macro_calendar_sync(hours: int, min_rank: int, sources: List[str], limit: int,
                         min_impact_str: str = "low", min_star: int = 0) -> Dict[str, Any]:
    """[sync] /api/macro/calendar 的查询实现。

    多源合并策略：
      - 同一时刻只返回去重后的最新版本（按 (country, title, event_time//3600) 聚合，
        优先保留 impact 更高的源）；
      - FF → jin10 优先级：FF 有 high 时显示 FF；FF 缺失或 med 时叠加 jin10 数据。
      - v226: min_star 阈值（与 min_impact 取交集，star 字段 ≥ min_star 才显示）。
    """
    now_ts = int(time.time())
    win_hi = now_ts + int(hours) * 3600
    win_lo = now_ts - 3600  # 容忍 1h 内已开始未结束的
    conn = db.get_connection()
    out: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    try:
        placeholders = ",".join("?" * len(sources))
        # 不在 SQL 层做 limit —— 1 周窗口内事件可能 200+，且 Python 层还要做
        # impact 过滤，limit 在 SQL 层会把"靠后的高影响事件"漏掉（如本周高
        # 影响事件全集中在 +12h~+90h，但 limit*3=15 只够装低影响）。
        rows = conn.execute(
            f"""
            SELECT source, event_id, country, title, impact, star, event_time,
                   forecast, previous, actual, fetched_at
              FROM macro_calendar
             WHERE event_time BETWEEN ? AND ?
               AND source IN ({placeholders})
             ORDER BY event_time ASC
            """,
            (win_lo, win_hi, *sources),
        ).fetchall()
    except Exception as exc:
        rows = []
        print(f"[MACRO-CAL] query failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    impact_rank = {"high": 2, "med": 1, "low": 0}
    bucket: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    for r in rows:
        source, eid, country, title, impact, star, ets, forecast, previous, actual, fetched_at = r
        # v226: min_star 过滤（仅当 >0 时生效，与 impact 过滤取交集）
        cur_star = int(star or 0)
        if min_star > 0 and cur_star < min_star:
            continue
        if impact_rank.get(str(impact).lower(), 0) < min_rank:
            continue
        # 1 小时精度聚合键：FF/jin10 同事件合并
        key = (str(country).upper(), str(title).lower().strip(), int(int(ets) // 3600))
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = {
                "source": source,
                "event_id": eid,
                "country": str(country).upper(),
                "title": title,
                "impact": impact,
                "star": int(star or 0),  # v215: 1-5 颗星
                "event_time": int(ets),
                "forecast": forecast or "",
                "previous": previous or "",
                "actual": actual or "",
                "sources": [source],
            }
        else:
            # 同 key 已存在 → 选 impact 更高的；都 push the title 否则都保留 sources
            if source not in existing["sources"]:
                existing["sources"].append(source)
            if impact_rank.get(str(impact).lower(), 0) > impact_rank.get(
                    str(existing["impact"]).lower(), 0):
                existing["impact"] = impact
                existing["source"] = source  # 高 impact 源作为展示源
                existing["country"] = str(country).upper()
            # v215: star 字段取 max (FF: 4/3/2, jin10: 1-5)
            try:
                cur_star = int(star or 0)
            except Exception:
                cur_star = 0
            if cur_star > int(existing.get("star") or 0):
                existing["star"] = cur_star
            # 数值字段优先非空
            for f in ("forecast", "previous", "actual"):
                if not existing.get(f) and (locals().get(f) or ""):
                    existing[f] = locals().get(f)
        counts[source] = counts.get(source, 0) + 1

    out = sorted(bucket.values(), key=lambda e: e["event_time"])[:int(limit)]
    return {
        "events": out,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources_used": sources,
        "counts_by_source": counts,
        "window_hours": int(hours),
        "min_impact": str(min_impact_str or "low").lower(),
        "min_star": int(min_star or 0),  # v226
    }


# ─── v226: Fed 利率预期 + 跨资产异常（2026-11-17 从 api_server 追加）───────────

_FED_CACHE: Dict[str, Any] = {}


@router.get("/api/fed/expectation")
async def get_fed_expectation():
    """v226: 当前 Fed 利率预期快照。

    数据源优先级：FRED DGS2+DFF → yfinance ^FVX 5Y 代理 → 硬编码兜底。
    推导原理（第一性原理）：
      · 2Y UST yield = 24 个月短期利率几何平均预期（折现回来）
      · 短期利率 = EFFR (FRED DFF)
      · 推导: spread = 2Y − EFFR = 市场对未来 24 月累计 Fed 行为定价
      · 标准 Fed 操作 25bp/次 → 累计次数 = spread_bp / 25

    Returns
    -------
    {
      "ok": bool,
      "ts": int,
      "data_source": "fred" | "yfinance_5y_proxy" | "fallback_hardcode",
      "current": {"dgs2_pct": float, "effr_pct": float},
      "expectation": {
        "spread_bp", "expected_24m_change_bp", "expected_step_count",
        "direction": "hawkish" | "dovish" | "neutral", "narrative": str,
        "first_principles_chain": [str, ...]
      },
      "history_24h": {"dgs2_change_bp": float, "samples": int},
      "anomaly": {"is_anomaly": bool, "type": str, "severity": str, "message": str}
    }
    """
    # v359: FRED/新浪都慢时这一路会拖到 20s（6s×重试），页面跟着转圈。
    #   加 60s 缓存 + 8s 硬超时：超时直接如实返回 timeout，不编数据。
    now_t = time.time()
    if _FED_CACHE.get("payload") and now_t - _FED_CACHE.get("ts", 0) < 60:
        return _FED_CACHE["payload"]
    try:
        import fed_rate_expectation as _fed
        payload = await asyncio.wait_for(asyncio.to_thread(_fed.fetch_expectation), timeout=8.0)
        _FED_CACHE["ts"] = now_t
        _FED_CACHE["payload"] = payload
        return payload
    except asyncio.TimeoutError:
        return {"ok": False, "timeout": True,
                "error": "上游取数超过 8s（FRED/yfinance 慢），本轮无数据"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# v496：跨资产异动快照单次约 12s（external_indicators 全表扫描 × 品种），
# 而 /discover 面板 60s 轮询一次 —— 原来每个请求还**算两遍**（detect 一次、
# 输出 snapshot 再算一次），实测 25s，前端看起来就是"空的/卡住"。
# 这里：① 快照只算一次 ② 加 3 分钟缓存（24h 异动本身不会秒级变化）
# ③ 25s 硬超时，超时明确回 timeout，不再让请求无限挂着。
_CA_ANOMALY_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "payload": None}
_CA_ANOMALY_TTL_S = 180.0
_CA_ANOMALY_TIMEOUT_S = 30.0


@router.get("/api/cross-asset/anomaly")
async def get_cross_asset_anomaly(min_classes: int = 4, min_pct: float = 0.5):
    """v226: 检测"≥ N 个不同行业同步同向"异动。

    用户原话："超过四类不同行业涨跌一致信号（k线形态差不多）"。

    Parameters
    ----------
    min_classes : int
        最少不同行业数（默认 4）。
    min_pct : float
        同向最小幅度阈值（%，默认 0.5）。

    Returns
    -------
    {
      "ok": bool,
      "ts": int,
      "cached": bool,
      "is_anomaly": bool,
      "direction": "up" | "down" | "none",
      "classes_aligned": int,
      "classes_total": int,
      "symbols_aligned": [str, ...],
      "classes_aligned_names": [str, ...],
      "shape_consistent": bool,
      "severity": "high" | "med" | "low",
      "message": str,
      "snapshot": {完整 cross_asset_snapshot}
    }
    """
    _mc = max(2, int(min_classes))
    _mp = float(min_pct)
    key = f"{_mc}:{_mp}"
    now = time.time()
    cached = _CA_ANOMALY_CACHE.get("payload")
    if (cached is not None
            and _CA_ANOMALY_CACHE.get("key") == key
            and now - float(_CA_ANOMALY_CACHE.get("ts") or 0.0) < _CA_ANOMALY_TTL_S):
        out = dict(cached)
        out["ts"] = int(now)
        out["cached"] = True
        out["cache_age_s"] = round(now - float(_CA_ANOMALY_CACHE["ts"]), 1)
        return out

    try:
        def _run():
            import cross_asset_context as _cac
            snap = _cac.cross_asset_snapshot(asset="*", ts_iso=None)
            anomaly = _cac.detect_anomaly(snap, min_classes=_mc, min_pct=_mp)
            anomaly["ok"] = True
            anomaly["snapshot"] = snap
            anomaly["snapshot_ts"] = snap.get("anchor_ts")
            return anomaly

        payload = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=_CA_ANOMALY_TIMEOUT_S)
        payload["ts"] = int(time.time())
        payload["cached"] = False
        _CA_ANOMALY_CACHE.update({"ts": time.time(), "key": key, "payload": payload})
        return payload
    except asyncio.TimeoutError:
        return {
            "ok": False, "timeout": True, "cached": False,
            "error": f"跨资产快照计算超过 {int(_CA_ANOMALY_TIMEOUT_S)}s，本轮无结果；下次轮询命中缓存或重算",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
