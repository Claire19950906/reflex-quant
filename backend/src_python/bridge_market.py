#!/usr/bin/env python3
"""bridge_market.py — 桥服务的行情/异动排行这一簇（2026-11-18）。

搬出原因：`market_movers` 是 243 行的端点，加上快速路径、新闻窗口读取、120s 缓存
和预热点（31 品种 × 异动+新闻）预热线程，一整套逻辑混在 8,000 行的 bridge_server 里。
这套东西的特征是**自成一体的缓存 + 自己的后台线程**，所以整块搬走最干净：
缓存（_BMOVERS_CACHE）与预热线程归这个模块所有，bridge_server 只留一个薄端点。

讲清楚三条约束（都在代码注释里）：
1. 冷启动不许在请求线程里现算完整版（4~11s，前端 3 个周期轮询会把线程池占满，
   连 /api/health 都排队 14s）；请求只回快速路径，完整版由缓存/预热线程补。
2. 缓存过期走 stale-while-revalidate：先给旧值，后台单飞线程刷新。
3. 预热前先看 free 内存（8GB 机器剩余 <150MB 时整轮跳过，否则会推 swap）。

bridge_server 用 `from bridge_market import *` 接回；端点函数名仍叫 market_movers。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Annotated, Any

from fastapi import Query

from bridge_builders import _strip_hash_prefix, db_cursor

__all__ = [
    "TF_HOURS",
    "TF_BARS_BACK",
    "_load_news_for_window",
    "market_movers",
    "start_movers_prewarm",
]


TF_HOURS = {
    "3m": 0.05, "5m": 0.083, "10m": 0.167, "1h": 1, "4h": 4, "30d": 720, "24h": 24,
}


TF_BARS_BACK = {
    "3m": 1, "5m": 1, "10m": 1, "1h": 1, "4h": 1, "24h": 1, "30d": 30,
}


def _load_news_for_window(conn: Any, since: int) -> list[dict[str, Any]]:
    """读取 since 之后的真实新闻, 统一成前端契约字段:
    id / source / headline / body / assets / action / score / ts"""
    def _cols(table: str) -> set[str]:
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            return set()

    try:
        if {"headline", "ts"} <= _cols("news_events"):
            return [dict(r) for r in conn.execute(
                "SELECT id, source, headline, body, action, score, assets, ts "
                "FROM news_events WHERE ts >= ? ORDER BY ts DESC LIMIT 2000",
                (since,),
            ).fetchall()]

        # 金融版: 真实新闻在 raw_news, 标题列叫 content
        rn = _cols("raw_news")
        if {"content", "ts"} <= rn:
            assets_expr = "gate_assets" if "gate_assets" in rn else "''"
            score_expr = "COALESCE(relevance_score, 0)" if "relevance_score" in rn else "0"
            rows = conn.execute(
                f"SELECT id, source, content, {assets_expr} AS assets, "
                f"{score_expr} AS score, ts FROM raw_news "
                "WHERE ts > 0 AND ts >= ? ORDER BY ts DESC LIMIT 2000",
                (since,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                txt = _strip_hash_prefix(r["content"])
                if not txt:
                    continue
                out.append({
                    "id": r["id"],
                    "source": r["source"] or "news",
                    "headline": txt,
                    "body": txt,
                    "assets": r["assets"] or "",
                    # raw_news 没有买卖方向字段, 留空由调用方归一为 HOLD
                    "action": None,
                    "score": r["score"] or 0,
                    "ts": r["ts"],
                })
            return out
        print("[bridge] market_movers: 未找到结构匹配的新闻表 (news_events / raw_news 均不匹配)", flush=True)
    except Exception as exc:  # pragma: no cover - 新闻只是附加信息, 绝不影响异动点
        print(f"[bridge] market_movers 新闻读取跳过: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    return []


_BMOVERS_CACHE: dict[tuple, tuple[float, dict]] = {}


_BMOVERS_TTL_S = 120.0


def _movers_fast_payload(tf: str, limit: int) -> dict:
    """只读 ohlcv_daily 的轻量排行（价 + 涨跌幅），给冷启动用。

    与完整版的差别：不带 events / abnormals / drivers。前端拿到它就能把
    「资产涨跌排行」列出来并自动选第一个品种，K 线立刻能画；异动/新闻
    等富信息由后台完整版补齐。
    """
    bars_back = TF_BARS_BACK.get(tf, 1)
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT asset, close, ts, source FROM ohlcv_daily "
            "WHERE (asset, ts) IN (SELECT asset, MAX(ts) FROM ohlcv_daily GROUP BY asset) "
            "ORDER BY asset"
        ).fetchall()
        base_rows = conn.execute(
            "SELECT asset, close FROM ("
            "  SELECT asset, close, ROW_NUMBER() OVER (PARTITION BY asset ORDER BY ts DESC) AS rn"
            "  FROM ohlcv_daily WHERE close IS NOT NULL) WHERE rn = ?",
            (bars_back + 1,),
        ).fetchall()
    base = {b["asset"]: b["close"] for b in base_rows if b["close"]}
    items = []
    for r in rows:
        price = r["close"]
        prev = base.get(r["asset"])
        chg = None
        if price and prev:
            try:
                chg = round((float(price) - float(prev)) / float(prev) * 100.0, 2)
            except Exception:
                chg = None
        items.append({
            "asset": r["asset"], "price": price, "change24h": chg,
            "source": r["source"] or "ohlcv_daily", "source_count": 1,
            "events": [], "abnormals": [], "last_ts": r["ts"],
        })
    items.sort(key=lambda x: abs(x["change24h"] or 0) if x["change24h"] is not None else -1, reverse=True)
    items = items[:limit]
    return {
        "ok": True, "partial": True, "timeframe": tf, "bars_back": bars_back,
        "hours": TF_HOURS.get(tf, 24), "items": items, "n": len(items), "count": len(items),
        "abnormal_engine": "pending (完整版后台计算中)",
        "data_source": "ohlcv_daily (fast path)",
        "note": "异动/新闻等细节稍后自动补齐，不影响先看 K 线",
    }


def market_movers(
    timeframe: str = "24h",
    limit: Annotated[int, Query(ge=1, le=1000)] = 20,
    full: bool = False,
):
    """行情涨跌排行 (前端 MoversPanel 主数据源).

    v303 Q1 cont 实现 (之前 8000 死 + bridge 未实现 → catch-all 返空 items →
    前端 setSelected 拿不到第一个品种 → selected 一直 null → K线 effect 直接
    `if (!selected) return` 不执行 → 整页 K线空白)。

    数据源 ohlcv_daily (日线):
      - price     = 最新一根 close
      - change24h = (最新 close - 前一天 close) / 前一天 close * 100
      - events    = 同窗口内 news_events 命中数
      - abnormals = 同窗口内 critical_price_moves 异动点

    注意: ohlcv_daily 是日线表, 不能用 "ts >= now - 24h" 定位基准 (日线粒度下
    24h 窗口内常常一根 bar 都没有 → 基准退化成全历史首根 → chg 算出 +684% 这种
    离谱值)。所以基准改用 "倒数第 2 根 bar"(LIMIT 1 OFFSET 1), 与粒度无关。
    """
    # timeframe → 回看根数 (ohlcv_daily 是日线, 1 根 = 1 天)
    tf = timeframe or "24h"
    _ck = (tf, int(limit))
    _hit = _BMOVERS_CACHE.get(_ck)
    if _hit and (time.time() - _hit[0]) < _BMOVERS_TTL_S:
        return _hit[1]
    # v345: 缓存过期但旧值还在 → 立刻返回旧值、后台异步刷新（stale-while-revalidate）。
    #   原来过期就得现算 4~11s，页面这段时间只能显示"还没选品种"；
    #   改为先给旧数据，用户永远不用等，最多差一两分钟。
    if _hit:
        def _refresh(_tf: str, _lim: int) -> None:
            try:
                market_movers(timeframe=_tf, limit=_lim)
            except Exception as _exc:  # pragma: no cover
                print(f"[movers] 后台刷新失败 {_tf}: {_exc}", flush=True)
        threading.Thread(target=_refresh, args=(tf, int(limit)), daemon=True).start()
        return _hit[1]
    # v345.1 冷启动兜底：进程刚起 / 缓存还没热时，完整版要算 31 个品种 ×
    #   异动 + 新闻配对，实测 4~11s 甚至超时 —— 前端拿不到列表就没有 selected，
    #   归因页永远显示"还没选品种"（K 线一根不画）。
    #   这里先给一份只用 ohlcv_daily 的最小列表（两条 SQL，几十毫秒），
    #   够前端选品种 + 画 K 线；完整版（含异动/新闻）丢后台算完落缓存。
    if not full:
        # v345.2: 只回轻量列表，**绝不在请求线程里现算**完整版。
        #   之前每次请求都同步算 31 品种 × 异动 + 新闻（4~11s），前端 3 个周期
        #   同时轮询 → 线程池被占满 → 连 /api/health 都要排队 14s，整机像挂了。
        #   完整版交给预热线程（或后台单飞线程）算完落缓存，请求永远毫秒级返回。
        try:
            fast = _movers_fast_payload(tf, int(limit))
            if fast and fast.get("items"):
                return fast
        except Exception as _exc:
            print(f"[movers] 快速路径失败，回落到完算: {_exc}", flush=True)
    bars_back = TF_BARS_BACK.get(tf, 1)
    now = int(time.time())
    since = now - TF_HOURS.get(tf, 24) * 3600
    # v303 R11: abnormals/events 的回看窗口不能跟着 tf 缩到 24h。
    # 数据侧 critical_price_moves / news_events 的最新 ts 本就滞后于 now
    # (实测 MU 异动最新 10-15, 新闻最新 10-20, 而 now=10-20),
    # 一旦按 24h 卡窗口就查出 0 条 → 异动 marker / 新闻时间线全空。
    # 这里给一个下限: 至少 30 天, 保证滞后数据也能落进结果。
    since = min(since, now - 30 * 86400)

    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT asset, close, ts, source FROM ohlcv_daily "
            "WHERE (asset, ts) IN ("
            "  SELECT asset, MAX(ts) FROM ohlcv_daily GROUP BY asset"
            ") ORDER BY asset"
        ).fetchall()

    # 每个品种取倒数第 (bars_back+1) 根作基准
    # 原来对每个品种单独跑一次 `ORDER BY ts DESC LIMIT 1 OFFSET n`（N+1 查询，
    # 品种一多就 6 秒+，而前端走的就是 bridge）。改成窗口函数一次取完。
    base_map: dict[str, float] = {}
    src_map: dict[str, str] = {}
    for r in rows:
        src_map[r["asset"]] = r["source"] or "ohlcv_daily"
    with db_cursor() as conn:
        base_rows = conn.execute(
            "SELECT asset, close FROM ("
            "  SELECT asset, close, ROW_NUMBER() OVER ("
            "    PARTITION BY asset ORDER BY ts DESC) AS rn"
            "  FROM ohlcv_daily WHERE close IS NOT NULL"
            ") WHERE rn = ?",
            (bars_back + 1,),
        ).fetchall()
    for b in base_rows:
        if b["close"]:
            base_map[b["asset"]] = b["close"]

    # 异动点 + 新闻: 一次查全, 再按 asset 分桶
    # v303 R12: abnormals 改用 tech_signals(技术指标组合检测), 不再是旧的
    # critical_price_moves(纯涨跌幅统计)。旧表 ts 取窗口起点, 标记时间错位。
    abnormals: dict[str, list[dict[str, Any]]] = {}
    event_counts: dict[str, int] = {}
    tech_available = True
    with db_cursor() as conn:
        has_tech = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='tech_signals'").fetchone() is not None
        if has_tech:
            ts_rows = conn.execute(
                "SELECT asset, ts, close, trigger_type, score, uses_future, "
                "pct_1d, pct_3d, vol_ratio, support_level, signal_json, "
                "merged_types, bars_available, data_max_ts "
                "FROM tech_signals WHERE ts >= ? ORDER BY ts DESC",
                (since,),
            ).fetchall()
            for r in ts_rows:
                try:
                    sigs = json.loads(r["signal_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    sigs = []
                try:
                    merged = json.loads(r["merged_types"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    merged = []
                abnormals.setdefault(r["asset"], []).append({
                    "ts": r["ts"],
                    "pct_change": r["pct_1d"],
                    "vol_ratio": r["vol_ratio"],
                    "trigger_type": r["trigger_type"],
                    "score": r["score"],
                    "uses_future": int(r["uses_future"] or 0),
                    "close": r["close"],
                    "support_level": r["support_level"],
                    "signals": sigs,
                    "merged_types": merged,
                    "bars_available": r["bars_available"],
                    "data_max_ts": r["data_max_ts"],
                    # legacy 字段保留兼容 (旧前端读 kind/pct)
                    "kind": r["trigger_type"],
                    "pct": r["pct_1d"],
                })
        else:
            # 表尚未建立 → 明确标注不可用, 绝不回退到旧的涨跌幅统计冒充技术指标
            tech_available = False
        nv = _load_news_for_window(conn, since)

    aliases = {
        "BTC": ["BTC", "BITCOIN", "比特币"], "ETH": ["ETH", "ETHEREUM", "以太坊"],
        "SOL": ["SOL", "SOLANA"], "BNB": ["BNB", "BINANCE", "币安"],
        "ADA": ["ADA", "CARDANO", "艾达"], "DOGE": ["DOGE", "DOGECOIN", "狗狗币"],
        "XRP": ["XRP", "RIPPLE", "瑞波"], "MU": ["MU", "MICRON", "美光"],
        "NVDA": ["NVDA", "NVIDIA", "英伟达"], "TSLA": ["TSLA", "TESLA", "特斯拉"],
        "AAPL": ["AAPL", "APPLE", "苹果"], "SKHYNIX": ["SKHYNIX", "SK HYNIX", "海力士"],
        "WTI": ["WTI", "原油", "OIL"], "XAU": ["XAU", "GOLD", "黄金"],
    }
    # v303 R11: 按资产收集真实新闻事件 (前端 MoverEvent 契约:
    #   {ts, action, score, content, source})。
    # 之前只统计了个数并返回 [{"n": count}] 占位符 —— 前端 L3050
    # `e.content.length` 直接抛 TypeError, 整页白屏 (Round 10 之后暴露)。
    events_by_asset: dict[str, list[dict[str, Any]]] = {}
    for r in nv:
        hay = f"{r['assets'] or ''} {r['headline'] or ''}".upper()
        for asset, als in aliases.items():
            if not any(a in hay for a in als):
                continue
            action = (r["action"] or "").upper()
            if action not in ("BUY", "SELL", "HOLD"):
                action = "HOLD"
            # content 优先用 body(正文), 空则退 headline —— 前端直接渲染它
            content = (r["body"] or "").strip() or (r["headline"] or "").strip()
            if not content:
                continue
            events_by_asset.setdefault(asset, []).append({
                "id": r["id"],
                "ts": r["ts"],
                "action": action,
                "score": round(abs(r["score"] or 0), 4),
                "content": content,
                "headline": r["headline"] or "",
                "source": r["source"] or "news",
            })
    for asset, evs in events_by_asset.items():
        evs.sort(key=lambda x: x["ts"], reverse=True)
        events_by_asset[asset] = evs[:60]
    for asset in events_by_asset:
        event_counts[asset] = len(events_by_asset[asset])

    # v303 R12: 给每个异动点配新闻归因 (用户需求: "异常识别点上方要显示是什么新闻事件影响")
    # 窗口取 [signal_ts - 3d, signal_ts + 3d], 按时间接近度 + 新闻重要性排序,
    # 只挂**真实存在**的新闻; 没有匹配就留空数组, 前端不显示任何归因,
    # 绝不用"无新闻影响"之类的占位文案冒充。
    NEWS_LOOKBACK = 3 * 86400
    NEWS_LOOKAHEAD = 3 * 86400
    for asset, sigs in abnormals.items():
        evs = events_by_asset.get(asset, [])
        for sig in sigs:
            st = sig["ts"]
            near = [e for e in evs
                    if st - NEWS_LOOKBACK <= e["ts"] <= st + NEWS_LOOKAHEAD]
            near.sort(key=lambda e: (abs(e["ts"] - st) - e["score"] * 86400))
            sig["news"] = [{
                "id": e["id"], "ts": e["ts"], "headline": e["headline"],
                "content": e["content"], "action": e["action"],
                "score": e["score"], "source": e["source"],
                "lag_sec": int(e["ts"] - st),
            } for e in near[:3]]

    items: list[dict[str, Any]] = []
    for r in rows:
        asset = r["asset"]
        last = r["close"]
        if last is None:
            continue
        base = base_map.get(asset)
        chg = None
        if base not in (None, 0):
            chg = round((last - base) / base * 100, 2)
        items.append({
            "asset": asset,
            "price": round(last, 6),
            "change24h": chg,
            "source": src_map.get(asset, "ohlcv_daily"),
            "source_count": 1,
            # 前端 MoverEvent[] — 完整字段, 不再是计数占位符
            "events": events_by_asset.get(asset, []),
            "abnormals": abnormals.get(asset, []),
            "last_ts": r["ts"],
        })

    # 涨跌幅绝对值降序 (异动榜语义); 没 chg 的排最后
    items.sort(key=lambda x: abs(x["change24h"] or 0) if x["change24h"] is not None else -1,
               reverse=True)
    items = items[:limit]
    _payload = {
        "ok": True,
        "timeframe": tf,
        "bars_back": bars_back,
        "hours": TF_HOURS.get(tf, 24),
        "items": items,
        "n": len(items),
        "count": len(items),
        # v303 R12: 异动检测已换成技术指标组合 (tech_signals)
        "abnormal_engine": "tech_signals (v303 R12)" if tech_available else "unavailable",
        "data_source": "ohlcv_daily + tech_signals + news_events (v303 R12)",
    }
    _BMOVERS_CACHE[_ck] = (time.time(), _payload)
    return _payload


_PREWARM_TIMEFRAMES = ("10m", "1h", "4h", "24h", "30d")


def _free_mb() -> float:
    """本机 free 内存(MB)。8GB 机器上 free 掉到 20MB 时，再跑一轮 31 品种全量
    计算就会把机器推进 swap，接口从 1s 变成 40s —— 所以预热必须先看内存。"""
    try:
        import subprocess
        out = subprocess.check_output(["vm_stat"], timeout=3).decode()
        for line in out.splitlines():
            if "Pages free" in line:
                return int(line.split(":")[1].strip().rstrip(".")) * 4096 / 1024 / 1024
    except Exception:
        return 999.0
    return 999.0


def _prewarm_movers_loop() -> None:
    import time as _t
    _t.sleep(6)  # 等启动期挂路由/建连接的风头过去
    while True:
        if _free_mb() < 150:
            _t.sleep(120)          # 内存告急：整轮跳过，只留请求时按需算
            continue
        for tf in _PREWARM_TIMEFRAMES:
            try:
                t0 = _t.time()
                market_movers(timeframe=tf, limit=20, full=True)
                cost = _t.time() - t0
                if cost > 3:
                    print(f"[movers-prewarm] {tf} 预计算 {cost:.1f}s", flush=True)
            except Exception as exc:  # pragma: no cover
                print(f"[movers-prewarm] {tf} 失败: {exc}", flush=True)
            _t.sleep(2)
        _t.sleep(60)


def start_movers_prewarm() -> threading.Thread:
    """启动 movers 预热后台线程并返回它（bridge_server 启动时调用一次）。

    v345.2: 原来看 TRIDENT_BRIDGE_AUX_JOBS，可这个变量这个环境从来没设，
    等于预热线程从没跑过 → 每次请求都现算 → 页面越点越卡。默认启动。
    """
    th = threading.Thread(target=_prewarm_movers_loop, daemon=True,
                          name="bridge-movers-prewarm")
    if os.getenv("TRIDENT_BRIDGE_AUX_JOBS", "1") != "0":
        th.start()
    return th
