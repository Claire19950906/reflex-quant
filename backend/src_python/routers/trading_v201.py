# -*- coding: utf-8 -*-
"""交易信号 / v201 事件影响路由（结构切片，2026-11-17）。

从 api_server.py 搬来 5 条端点：
  /api/trading/signals（v398 优先代理 8100 拿完整形状；仅当 action/settled
        非默认才退回本地实现，因为 8100 返回里没这两个字段）
  /api/trading/latest（每品种最新一条，按 id DESC LIMIT 1）
  /api/v201/impact/by-asset（事件→K 线影响，v319 契约：有符号百分数）
  /api/v201/event/opportunities
  /api/v201/trade-points（下次交易点日历，含 T+0/T+1 与滑点）
DB_PATH 与 _fetch_signals_from_bridge 仍在 api_server，用 _api() 回引。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Annotated, Any, Dict, List, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


def _api():
    """延迟回引 api_server（DB 路径与 8100 代理助手仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/trading/signals")
async def get_trading_signals(
    asset: str = "",
    action: str = "",
    limit: int = 200,                # v221.3: 默认 20 → 200（归因页 30 天窗口下保证密度）
    settled: int = -1,               # -1=全部, 0=未结算, 1=已结算
    from_ts: int = 0,                # v221.3: 时间窗下界（按 news_time）
    to_ts: int = 0,                  # v221.3: 时间窗上界（0=now）
    hide_neutral: int = -1,          # -1=未指定（跟随 8100 默认）；1=过滤中性；0=保留全部
):
    """
    交易系统对接接口 — 返回精简版信号数据。

    Query params:
      asset         — 品种过滤，如 BTC / XAU / WTI / ETH（留空=全部）
      action        — 方向过滤，BUY / SELL / HOLD（留空=全部）
      limit         — 返回条数，默认 200，最大 500
      settled       — 结算状态，0=未结算 / 1=已结算 / -1=全部（默认）
      from_ts       — news_time 下界（秒，0=不限）
      to_ts         — news_time 上界（秒，0=now）
      hide_neutral  — 1=过滤 HOLD+score=0（默认）/ 0=保留全部

    返回字段:
      signal_id, news_time, asset, action, score, reasoning, reasoning_path,
      market_category, event_strength, direct_catalyst, prediction_type,
      market_confirmation, entry_price, exit_price, is_correct, settled, created_at

    v221.3 调整（用户 2026-10-05 截图反馈"绿点看不见"）：
      ① 默认 limit 20→200（归因页 K 线 30 天窗口下，20 条不够覆盖密度）
      ② 新增 from_ts/to_ts 时间窗过滤（前端无需自筛）
      ③ 新增 hide_neutral=1 默认过滤 HOLD+score=0 信号（无方向噪声）
    """
    # v398: 统一走 8100（形状与前端一致）。只有 8000 独有的历史过滤参数
    #   （action / settled）不为默认值时，才退回下面的本地实现 —— 那两个参数
    #   8100 的返回里没有对应字段，在代理层没法正确过滤。
    if not action and settled < 0:
        _pb = await asyncio.to_thread(
            _api()._fetch_signals_from_bridge, asset, from_ts, to_ts,
            hide_neutral, min(max(limit, 1), 500))
        if _pb is not None:
            return _pb

    limit = min(max(limit, 1), 500)
    if to_ts <= 0:
        to_ts = int(time.time())

    where_clauses = []
    params: List[Any] = []

    if asset:
        where_clauses.append("UPPER(ad.target_asset) = ?")
        params.append(asset.upper())
    if action:
        where_clauses.append("UPPER(ad.suggested_action) = ?")
        params.append(action.upper())
    if settled >= 0:
        where_clauses.append("qs.settled = ?")
        params.append(settled)
    # v283+ 修复：raw_news 有两列时间戳——
    #   timestamp TEXT（ISO 字符串 '2026-10-09 07:51:38'，SQLite 比较时只解析 2026 整年）
    #   ts         INTEGER（epoch 秒）
    #   旧代码用 rn.timestamp 比较时所有行都解析成 2026，导致 from_ts<2026 时全 0、>202600 时全 0
    #   改用 rn.ts（INTEGER 秒）才能用前端传来的 from_ts/to_ts（也是秒）正确过滤
    if from_ts > 0:
        where_clauses.append("rn.ts >= ?")
        params.append(int(from_ts))
    if to_ts > 0:
        where_clauses.append("rn.ts <= ?")
        params.append(int(to_ts))
    if hide_neutral:
        # 过滤无方向信号：HOLD 或 score=0/空 → 不展示绿点
        where_clauses.append(
            "(UPPER(ad.suggested_action) IN ('BUY', 'SELL') "
            " AND (ad.sentiment_score IS NOT NULL AND ad.sentiment_score != 0))"
        )

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    async with aiosqlite.connect(_api().DB_PATH, timeout=20) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                ad.id,
                rn.timestamp AS news_time,  -- ISO 字符串保留在响应（前端按 ISO 解析）
                UPPER(ad.target_asset) AS asset,
                UPPER(ad.suggested_action) AS action,
                ad.sentiment_score AS score,
                ad.reasoning,
                ad.reasoning_path,
                ad.market_category,
                ad.event_strength,
                ad.direct_catalyst,
                ad.prediction_type,
                ad.market_confirmation,
                qs.entry_price,
                qs.exit_price,
                qs.verdict AS is_correct,
                qs.settled,
                ad.created_at
            FROM ai_decisions ad
            INNER JOIN raw_news rn ON rn.id = ad.news_id
            LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
            WHERE {where_sql}
            ORDER BY ad.id DESC
            LIMIT ?
            """,
            params + [limit],
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]


@router.get("/api/v201/impact/by-asset")
async def get_event_kline_impact_by_asset(
    asset: str = "",
    from_ts: int = 0,        # 0 = 不限下界
    to_ts: int = 0,          # 0 = 不限上界 (默认 = now)
    severity_min: str = "low",  # low / medium / high
    min_abs_pct: float = 0.0,   # 最小幅度门槛 (百分数, 0 = 不过滤)
    order: str = "time",        # time (最新优先) | magnitude (幅度最大优先)
    limit: int = 100,
):
    """v201: 事件→K线影响 (event_kline_impact) 按品种 + 时间窗查询。

    用途：归因页 K 线标注源 (movers-panel)。
    数据：worker (`engine/event_kline_impact_worker.py`) 已计算好每条事件的：
      - event_title (raw_news.content 前 80 字)
      - impact_pct / impact_severity (high/medium/low)
      - direction (up/down/neutral)

    v319 契约 (与 bridge 8100 完全一致):
      · **单位**: impact_pct 输出成**有符号百分数** (0.0033 小数 → +0.33%),
        幅度另存 impact_abs_pct (恒 ≥ 0), 原始小数保留在 impact_pct_raw。
        worker 的 impact_pct 是小数且恒 ≥ 0 (方向另存 direction 列),
        不转换的话前端 marker-noise 的 0.3% 门槛会把 marker 全过滤掉。
      · **方向**: move_direction (up/down/flat) 按 direction 列给出 (真实涨跌, 涨绿跌红)。
      · **排序**: order=time → event_time DESC (最新优先);
        order=magnitude → 按幅度 DESC —— 归因页要的是"这段时间里最大的几次冲击",
        只按时间排序 + LIMIT 会把窗口另一端的事件挤掉 (30d 窗口只剩最近 1~2 天有 marker)。
      · severity 门槛下推到 SQL (先 LIMIT 再过滤会把更早的高严重度行吃掉)。
    """
    asset_up = (asset or "").strip().upper()
    if not asset_up:
        raise HTTPException(status_code=400, detail="asset is required (e.g., BTC/XAU/WTI)")
    if not to_ts:
        to_ts = int(time.time())
    if not from_ts:
        # 默认给 7 天前 — 覆盖 K 线 ~200 桶 (10m 周期)
        from_ts = to_ts - 7 * 86400

    sev_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    min_rank = sev_rank.get(str(severity_min).lower(), 1)

    limit = max(1, min(int(limit or 100), 1000))
    try:
        min_abs = abs(float(min_abs_pct or 0.0))
    except (TypeError, ValueError):
        min_abs = 0.0

    order_sql = (
        "ORDER BY ABS(COALESCE(impact_pct,0)) DESC, event_time DESC"
        if str(order or "time").strip().lower() == "magnitude"
        else "ORDER BY event_time DESC"
    )
    sev_case = (
        "CASE LOWER(COALESCE(impact_severity,'low'))"
        " WHEN 'critical' THEN 3 WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END"
    )

    sql = (
        "SELECT event_source, event_id, event_title, event_time, "
        "       asset, pre_event_close, max_high, min_low, "
        "       impact_pct, impact_severity, direction, "
        "       importance, impact_window_start, impact_window_end "
        "FROM event_kline_impact "
        "WHERE UPPER(asset) = ? AND event_time BETWEEN ? AND ? "
    )
    params: list = [asset_up, int(from_ts), int(to_ts)]
    if min_rank > 0:
        sql += f"AND {sev_case} >= ? "
        params.append(min_rank)
    if min_abs > 0:
        # impact_pct 是小数 → ×100 换算成百分数再比
        sql += "AND ABS(COALESCE(impact_pct,0))*100 >= ? "
        params.append(min_abs)
    sql += order_sql + " LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(_api().DB_PATH, timeout=20) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()

    out = []
    for r in rows:
        d = dict(r)
        # severity 过滤 (SQL 已下推, 这里只是兜底)
        if sev_rank.get(str(d.get("impact_severity") or "low").lower(), 0) < min_rank:
            continue
        # 把时间戳统一成秒级 int
        for k in ("event_time", "impact_window_start", "impact_window_end"):
            try:
                d[k] = int(d[k] or 0)
            except (TypeError, ValueError):
                d[k] = 0
        # ── v319 单位统一 ─────────────────────────────────────────────────────
        # event_kline_impact.impact_pct 是**小数** ((max_high-pre)/pre, 0.0033 = 0.33%)
        # 且恒 ≥ 0 (方向另存 direction 列); 前端 marker-noise 的幅度门槛按**百分数**
        # 比较 (1.0 = 1%) → 这里统一转成有符号百分数, 幅度另存 impact_abs_pct,
        # 否则前端会把全部 marker 判成"幅度不足"直接过滤掉。
        try:
            frac = float(d.get("impact_pct") or 0.0)
        except (TypeError, ValueError):
            frac = 0.0
        mag = abs(frac) * 100.0
        dirn = str(d.get("direction") or "neutral").lower()
        if dirn == "up":
            signed = mag
        elif dirn == "down":
            signed = -mag
        else:
            signed = 0.0
            dirn = "flat"
        d["impact_pct_raw"] = frac
        d["impact_pct"] = signed
        d["impact_abs_pct"] = mag
        d["move_direction"] = dirn          # 真实涨跌方向 (涨绿跌红)
        d["event_backed"] = True            # 来自 news_events → 有真实新闻事件链
        d["severity"] = str(d.get("impact_severity") or "low").lower()
        out.append(d)

    return {
        "ok": True,
        "asset": asset_up,
        "from_ts": int(from_ts),
        "to_ts": int(to_ts),
        "count": len(out),
        "n": len(out),            # 与 bridge 8100 同名 (前端按 n/items 读)
        "items": out,
        "source": "event_kline_impact",
        "honest_note": (
            "event_kline_impact: 事件→K线影响 (worker 已算好 event_title/severity/direction); "
            "impact_pct 已统一成有符号百分数"
        ),
    }


@router.get("/api/v201/event/opportunities")
def get_event_opportunities(
    asset: str = "",
    from_ts: int | None = None,
    to_ts: int | None = None,
    horizon_minutes: Annotated[int, Query(ge=5, le=10080)] = 240,
    lookback_days: Annotated[int, Query(ge=1, le=1095)] = 180,
    entry_mode: str = "auto",        # auto | confirm | immediate
    min_abs_edge_pct: float = 0.0,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
):
    """**时点口径**的事件机会卡 (v319+, 与 bridge 8100 完全一致)。

    每张卡只用该事件**时点之前**的数据:
      · 期望方向/幅度 ← 同类历史事件的条件统计 (event_time 之前);
      · 波动率 σ     ← 事件之前的日线;
      · 成本门槛     ← cost_model (fee + slippage, 往返);
      · 交收时点     ← T+0 / T+1 规则。

    证据不足 / 幅度淹没在噪声 / 扣完成本没优势 → 诚实报 NO_TRADE, 不硬给方向。

    ⚠️ 同步 def (不是 async): 机会卡计算走**同步 sqlite** (event_opportunity
    模块用 conn.execute), FastAPI 会把 def 端点丢线程池跑, 不阻塞事件循环。

    ⚠️ 参数校验与 bridge 8100 **同一套** (Annotated + Query): 越界直接 422,
    不做静默 clamp —— 否则同一个请求打两个端口会得到不同结果。
    """
    try:
        from event_opportunity import list_opportunities
    except Exception as exc:  # 模块缺失 → 诚实报错, 不静默返回空
        raise HTTPException(status_code=500, detail=f"event_opportunity 模块不可用: {exc}")
    now = int(to_ts) if to_ts else int(time.time())
    frm = int(from_ts) if from_ts else (now - 7 * 86400)
    conn = None
    try:
        conn = sqlite3.connect(_api().DB_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        return list_opportunities(
            conn,
            asset=asset or "",
            from_ts=frm,
            to_ts=now,
            limit=limit,
            horizon_minutes=horizon_minutes,
            lookback_days=lookback_days,
            entry_mode=entry_mode,
            min_abs_edge_pct=min_abs_edge_pct,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # 机会卡算不出来 → 诚实报错, 不假装"没有机会"
        return {
            "ok": False,
            "n": 0,
            "ready": 0,
            "items": [],
            "error": str(exc),
            "honest_note": "机会卡计算失败 (不是'没有机会', 是算不出来)",
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@router.get("/api/v201/trade-points")
def get_trade_points(
    asset: str = "",
    as_of_ts: int | None = None,
    horizon_hours: Annotated[int, Query(ge=1, le=720)] = 72,
    action_window_minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
    horizon_minutes: Annotated[int, Query(ge=5, le=10080)] = 240,
    lookback_days: Annotated[int, Query(ge=1, le=1095)] = 180,
    entry_mode: str = "auto",        # auto | confirm | immediate
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """**下次交易点日历**（前瞻）—— 未来可执行 / 待准备的交易时点。

    每个点包含:
      · 入场窗口: entry_not_before_ts ~ entry_deadline_ts
      · 方向与期望冲击 (edge_pct) / 扣完成本后的净优势 (edge_pct_net)
      · 交收口径: T+0 / T+1 + 最早可平仓时点 (earliest_exit_ts)
      · 成本: 每边手续费与滑点 bps、往返成本
      · 风控: 止损 / 目标 / 仓位上限; 以及"为什么不做"的诚实理由

    口径: 所有统计只用 as_of_ts(=现在) **之前**的数据 —— 未来的事件也只用
    今天能看到的历史来评估，不含任何未来函数。

    ⚠️ 同步 def（不是 async）: 走同步 sqlite，FastAPI 丢线程池，不阻塞事件循环。
    """
    try:
        from event_opportunity import list_trade_points
    except Exception as exc:  # 模块缺失 → 诚实报错, 不静默返回空
        raise HTTPException(status_code=500, detail=f"event_opportunity 模块不可用: {exc}")
    conn = None
    try:
        conn = sqlite3.connect(_api().DB_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        return list_trade_points(
            conn,
            as_of_ts=as_of_ts,
            horizon_hours=horizon_hours,
            action_window_minutes=action_window_minutes,
            asset=asset or "",
            limit=limit,
            horizon_minutes=horizon_minutes,
            lookback_days=lookback_days,
            entry_mode=entry_mode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # 算不出来 → 诚实报错, 不假装"没有交易点"
        return {
            "ok": False,
            "n": 0,
            "live": 0,
            "ready": 0,
            "points": [],
            "error": str(exc),
            "honest_note": "交易点日历计算失败 (不是'没有交易点', 是算不出来)",
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@router.get("/api/trading/latest")
async def get_latest_signals():
    """
    交易系统对接接口 — 每个品种的最新一条信号。

    返回: 按品种分组的最近信号，含 score/action/reasoning。
    覆盖品种: BTC, ETH, XAU, WTI, SOL
    """
    assets = ("BTC", "ETH", "XAU", "WTI", "SOL")
    result: Dict[str, Any] = {}
    async with aiosqlite.connect(_api().DB_PATH, timeout=20) as db:
        db.row_factory = aiosqlite.Row
        for a in assets:
            cursor = await db.execute(
                """
                SELECT
                    ad.id,
                    rn.timestamp AS news_time,
                    UPPER(ad.target_asset) AS asset,
                    UPPER(ad.suggested_action) AS action,
                    ad.sentiment_score AS score,
                    ad.reasoning,
                    ad.reasoning_path,
                    ad.market_category,
                    ad.event_strength,
                    ad.direct_catalyst,
                    ad.prediction_type,
                    ad.market_confirmation,
                    qs.entry_price,
                    ad.created_at
                FROM ai_decisions ad
                INNER JOIN raw_news rn ON rn.id = ad.news_id
                LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                WHERE UPPER(ad.target_asset) = ?
                ORDER BY ad.id DESC
                LIMIT 1
                """,
                (a,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            result[a] = dict(row) if row else None
    return result
