# -*- coding: utf-8 -*-
"""复盘（replay）路由（结构切片，2026-11-17）。

从 api_server.py 搬来 8 条端点：
  /api/replay/signals（复盘明细，默认只看已结算）、/stats（汇总，含时段窗口）
  /settings（GET/POST，合并口径设置）、/reset、/positions
  /api/replay/kline/live（最后一根 bar 的实时价量）、/api/replay/signal/{id}/kline
统计与行情助手仍在 api_server（内部 _replay_collect_context / _replay_build_fast 还要用），
router 用 _api() 回引。api_server 里保留两个同名薄壳，供内部调用方继续 await。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
from feeds import quote_sources

TZ_SHANGHAI = config.TZ_SHANGHAI


class PaperTradingBody(BaseModel):
    is_running: Optional[bool] = None
    tracks: Optional[List[str]] = None
    gate_enabled: Optional[bool] = None
    news_enabled: Optional[bool] = None
    news_daily_target: Optional[int] = None
    news_sources: Optional[Dict[str, bool]] = None
    news_trade_max_age_seconds: Optional[int] = None
    tp_enabled: Optional[bool] = None
    tp1_mult: Optional[float] = None
    tp2_mult: Optional[float] = None
    tp1_ratio: Optional[float] = None
    tp2_ratio: Optional[float] = None
    buy_min_score: Optional[float] = None
    sell_min_score: Optional[float] = None
    sl_mult: Optional[float] = None
    market_structure_enabled: Optional[bool] = None


router = APIRouter()


def _api():
    """延迟回引 api_server（复盘统计、行情与设置助手仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/replay/signals")
async def get_replay_signals(
    asset: str = "",
    action: str = "",
    settled: int = 1,  # 0=未结算 1=已结算 -1=全部（默认只看已结算，便于复盘）
    limit: int = 100,
):
    """
    复盘看板列表接口：返回所有 EXECUTED 已结算的信号，含完整复盘字段。

    关键字段：entry_price, exit_price, max_price, min_price, max_price_time,
    min_price_time, mfe_pct, mae_pct, forward_pnl, event_strength,
    extra_models_consensus, reasoning, reasoning_path, is_correct。

    Query params:
      asset   — 品种过滤，如 BTC / XAU / WTI / ETH
      action  — 方向 BUY/SELL/HOLD
      settled — 0=未结算 1=已结算 -1=全部
      limit   — 返回条数，默认 100，最大 500
    """
    limit = min(max(limit, 1), 500)

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
    # 模拟盘只看有 entry_price 的已结算信号
    where_clauses.append("qs.entry_price IS NOT NULL AND qs.entry_price > 0")

    where_sql = " AND ".join(where_clauses)

    async with aiosqlite.connect(config.DB_PATH, timeout=20) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                ad.id,
                ad.news_id,
                rn.timestamp AS news_time,
                rn.source,
                rn.content AS news_text,
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
                ad.event_phase,
                ad.expected_horizon,
                ad.invalidation_condition,
                ad.decision_context,
                qs.entry_price,
                qs.exit_price,
                ad.partial_exit_price,
                ad.partial_exit_ratio,
                ad.partial_pnl_pct,
                ad.heat_score,
                ad.impact_score,
                ad.asset_impact,
                qs.max_price,
                qs.min_price,
                qs.max_price_time,
                qs.min_price_time,
                datetime(qs.entry_ts, 'unixepoch', '+8 hours') AS entry_time,
                qs.verdict AS is_correct,
                qs.settled,
                qs.notional_usdt AS notional_usdt,   -- v386: 该笔仓位自己的名义金额（账户口径以此为准）
                qs.mfe_pct,
                qs.mae_pct,
                qs.pnl_pct AS forward_pnl,
                qs.mfe_time_mins,
                ad.extra_models_consensus,
                ad.doubao_action,
                ad.doubao_reasoning,
                ad.peer_reviews,
                ad.no_trade_reason,
                ad.trade_gate_reason,
                ad.evidence_confidence,
                ad.evidence_action,
                ad.cluster_size,
                ad.timeframe_match,
                ad.created_at,
                ad.strategy_id,
                ad.strategy_version_id,
                s.name AS strategy_name,
                sv.version AS strategy_version,
                sv.params AS strategy_params
            FROM ai_decisions ad
            INNER JOIN raw_news rn ON rn.id = ad.news_id
            LEFT JOIN strategies s ON s.id = ad.strategy_id
            LEFT JOIN strategy_versions sv ON sv.id = ad.strategy_version_id
            LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
            WHERE {where_sql}
            ORDER BY ad.id DESC
            LIMIT ?
            """,
            params + [limit],
        )
        rows = await cursor.fetchall()
        await cursor.close()

    # JSON-serialize the consensus field if present (it's stored as TEXT JSON)
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        for json_field in ("extra_models_consensus", "strategy_params", "peer_reviews"):
            value = d.get(json_field)
            if value and isinstance(value, str):
                try:
                    d[json_field] = json.loads(value)
                except Exception:
                    pass
        # 遗留清洗：去掉早期"由观望转看多/看空"前缀（现在 AI 观望就观望，不再转方向）
        for k in ("reasoning", "doubao_reasoning"):
            if d.get(k):
                d[k] = _api()._strip_hold_override(d[k])
        # Ensure news_time / entry_time are ISO strings
        for k in ("news_time", "entry_time", "created_at"):
            v = d.get(k)
            if v is None:
                continue
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif not isinstance(v, str):
                d[k] = str(v)
            elif k == "news_time" and "T" not in v:
                # "YYYY-MM-DD HH:MM:SS" → ISO 兼容（前端 Date.parse 可靠解析）
                d[k] = v.replace(" ", "T", 1)
        out.append(d)
    return out


@router.get("/api/replay/stats")
async def get_replay_stats(
    strategy_id: Optional[int] = None,
    version_id: Optional[int] = None,
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """当前策略模拟交易统计，胜率只使用有明确胜负的已结算交易。

    v232.5 P0+: 同时输出「盈亏比」(avg_win/abs(avg_loss)) 与「期望值」
    (winrate*avg_win + (1-winrate)*avg_loss) — 用户反馈 2026-10-06:
    「69% 胜率但盈亏比 0.30 → 实际负期望」 — 这两个字段让胜率假象无处遁形。
    v180 P0+: 支持 hours / from_ts / to_ts 窗口过滤(用 entry_time 做 SQL 过滤),
    让 A 卡片跟全局 PeriodSelector 同步 —— 切到"近 7 天"则只统计 7 天内开仓的
    真实持仓,与其他 6 张卡片同源同窗。三个参数都为空时退化为"全期"兼容。
    """
    from quick_sim import _resolve_window
    # v302+ P0+:走 _api()._resolve_window_with_log 输出 DEBUG（env WINDOW_DEBUG=1 开启）
    lo, hi, lo_iso, hi_iso = _api()._resolve_window_with_log(
        hours, from_ts, to_ts, caller="get_replay_stats")
    strategy_filter = "entry_price IS NOT NULL AND entry_price > 0"
    filter_params: tuple[Any, ...] = ()
    if strategy_id is not None:
        strategy_filter += " AND strategy_id = ?"
        filter_params += (strategy_id,)
    if version_id is not None:
        strategy_filter += " AND strategy_version_id = ?"
        filter_params += (version_id,)
    # v302+ P0+【修复】ai_decisions.entry_time 是 Beijing 朴素 ISO(quick_sim.fromtimestamp
    # 在 CST 时区下输出 'YYYY-MM-DDTHH:MM:SS'),不能与 UTC lo/hi 串直接字典序比。
    # 直接用 ai_decisions.ts INTEGER(UTC 秒)与 lo/hi 比，无歧义。
    # v302+ P0+【双保险】即使 entry_time 时戳格式未来再被改坏，按 Beijing 日期 substr
    # 仍能精确切到"今日 / 昨日 / 选期"。lo/hi 是 UTC 秒，entry_time 是 UTC ISO 字符串，
    # 直接 substr(1,10) 取的就是 UTC 日期；前端 'today' 给的是 Beijing 00:00 ~ 次日 00:00
    # (= UTC 16:00 - 16:00)，北京日边界天然对齐 UTC 日期，无需 +8h 校正。
    # 只在 lo/hi 都有时启用（避免空窗口/全期模式多算一次）。
    if lo is not None and hi is not None:
        strategy_filter += " AND ts >= ?"
        filter_params += (lo,)
        strategy_filter += " AND ts <= ?"
        filter_params += (hi,)
    elif lo is not None:
        strategy_filter += " AND ts >= ?"
        filter_params += (lo,)
    elif hi is not None:
        strategy_filter += " AND ts <= ?"
        filter_params += (hi,)
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN settled = 0 THEN 1 ELSE 0 END) AS tracking,
                SUM(CASE WHEN settled = 1 THEN 1 ELSE 0 END) AS settled,
                -- 成败判定按 forward_pnl 符号（真实持仓结算权威值；is_correct 历史多存 HOLD 不可靠）
                SUM(CASE WHEN settled = 1 AND forward_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN settled = 1 AND forward_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN settled = 1 AND (forward_pnl IS NULL OR forward_pnl = 0) THEN 1 ELSE 0 END) AS holds,
                -- 方向性指标只统计有明确胜负的 W/L 单（forward_pnl ≠ 0）
                AVG(CASE WHEN settled = 1 AND forward_pnl IS NOT NULL AND forward_pnl <> 0 THEN forward_pnl END) AS avg_pnl,
                -- v232.5 P0+: 平均盈利 + 平均亏损（期望值与盈亏比需要分桶）
                AVG(CASE WHEN settled = 1 AND forward_pnl > 0 THEN forward_pnl END) AS avg_win,
                AVG(CASE WHEN settled = 1 AND forward_pnl < 0 THEN forward_pnl END) AS avg_loss,
                -- 波动指标保留全部已结算（MFE/MAE 是市场波动度量，HOLD 单同样有效）
                AVG(CASE WHEN settled = 1 THEN mfe_pct END) AS avg_mfe,
                AVG(CASE WHEN settled = 1 THEN mae_pct END) AS avg_mae,
                MAX(CASE WHEN settled = 1 AND forward_pnl IS NOT NULL AND forward_pnl <> 0 THEN forward_pnl END) AS best_trade,
                MIN(CASE WHEN settled = 1 AND forward_pnl IS NOT NULL AND forward_pnl <> 0 THEN forward_pnl END) AS worst_trade,
                -- v342: 降级决策单独计数 —— "没分析"不能被读成"判断为观望"
                SUM(CASE WHEN COALESCE(analysis_degraded,0) = 1 THEN 1 ELSE 0 END) AS degraded,
                SUM(CASE WHEN COALESCE(analysis_degraded,0) = 0 THEN 1 ELSE 0 END) AS analyzed
            FROM ai_decisions
            WHERE {strategy_filter}
            """,
            filter_params,
        )
        overall = dict(await cur.fetchone())
        await cur.close()

        # v453: 降级率给两套口径 —— raw（analysis_degraded 原始占比，含"本不该分析"的越界）
        # 与 effective（真失败的占比）。混着看会把"越界 54%"读成"模型 70% 都在失败"。
        try:
            from analysis.contract import summarize_states
            cur = await db.execute(
                f"""
                SELECT COALESCE(analysis_degraded, 0),
                       COALESCE(analysis_degraded_reason, ''),
                       COALESCE(target_asset, ''),
                       COALESCE(hard_data_status, '')
                FROM ai_decisions
                WHERE {strategy_filter}
                """,
                filter_params,
            )
            _state_rows = await cur.fetchall()
            await cur.close()
            _sum = summarize_states([tuple(r) for r in _state_rows])
            overall["analysis_states"] = {
                "complete": _sum["complete"],
                "compressed": _sum["compressed"],
                "out_of_scope": _sum["out_of_scope"],
                "failed": _sum["failed"],
            }
            overall["raw_degraded_rate"] = _sum["raw_degraded_rate"]
            overall["effective_analysis_rate"] = _sum["effective_analysis_rate"]
            overall["failure_rate_in_scope"] = _sum["failure_rate_in_scope"]
        except Exception as _sum_exc:
            overall["analysis_states"] = {}
            print(f"[REPLAY] 终态口径计算失败: {type(_sum_exc).__name__}: {_sum_exc}")

        cur = await db.execute(
            f"""
            SELECT
                UPPER(target_asset) AS asset,
                COUNT(*) AS total,
                SUM(CASE WHEN forward_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN forward_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                AVG(forward_pnl) AS avg_pnl
            FROM ai_decisions
            WHERE settled = 1 AND {strategy_filter}
            GROUP BY UPPER(target_asset)
            ORDER BY total DESC
            """,
            filter_params,
        )
        by_asset = [dict(r) for r in await cur.fetchall()]
        await cur.close()

        cur = await db.execute(
            f"""
            SELECT
                UPPER(suggested_action) AS action,
                COUNT(*) AS total,
                SUM(CASE WHEN forward_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                AVG(forward_pnl) AS avg_pnl
            FROM ai_decisions
            WHERE settled = 1 AND {strategy_filter}
            GROUP BY UPPER(suggested_action)
            """,
            filter_params,
        )
        by_action = [dict(r) for r in await cur.fetchall()]
        await cur.close()

    decided = (overall.get("wins") or 0) + (overall.get("losses") or 0)
    for key in ("total", "tracking", "settled", "wins", "losses", "holds"):
        overall[key] = int(overall.get(key) or 0)
    overall["winrate"] = round((overall.get("wins") or 0) / decided, 4) if decided else 0.0
    # v300+ P0+：同步输出 winrate_pct（0-100 标度）— 与 quick_sim._profitability.winrate_pct
    # 口径一致，让反思页 A/B 卡片用同一种单位（已是 pct 数字）显示胜率，
    # 避免后人加 C 卡时被"A 用 winrate 要 ×100 · B 用 winrate_pct 不 ×"两个惯例绊倒。
    # 旧的 winrate 字段保留以兼容 replay-stats.tsx、history/page.tsx 等其他消费方。
    overall["winrate_pct"] = round(overall["winrate"] * 100, 2) if decided else None
    # v342: 明确区分"分析过"与"降级未分析"，前端可据此显示"未分析 N 条"
    overall["degraded"] = int(overall.get("degraded") or 0)
    overall["analyzed"] = int(overall.get("analyzed") or 0)
    overall["degraded_pct"] = (
        round(overall["degraded"] / (overall["degraded"] + overall["analyzed"]), 4)
        if (overall["degraded"] + overall["analyzed"]) else None
    )
    # v232.5 P0+: 盈亏比 + 期望值 — 让 69% 胜率盈亏比 0.30 的假象显形
    _api()._avg_win = overall.get("avg_win")
    _api()._avg_loss = overall.get("avg_loss")
    try:
        aw = float(_api()._avg_win) if _api()._avg_win is not None else 0.0
    except Exception:
        aw = 0.0
    try:
        al = float(_api()._avg_loss) if _api()._avg_loss is not None else 0.0
    except Exception:
        al = 0.0
    # 盈亏比 = avg_win / abs(avg_loss); 双方都没有时 None
    # 注: JSON 不允许 Infinity, 从未亏损用字符串 "Infinity" 标识,前端会解析为 ∞
    if aw > 0 and al < 0:
        overall["profit_loss_ratio"] = round(aw / abs(al), 4)
    elif aw > 0:
        overall["profit_loss_ratio"] = "Infinity"  # 从未亏损,前端按 ∞ 显示
    else:
        overall["profit_loss_ratio"] = None
    # 期望值 = winrate*avg_win + (1-winrate)*avg_loss (用决策间平均的 forward_pnl 视角)
    if decided > 0 and (aw > 0 or al < 0):
        wr = float(overall["winrate"] or 0.0)
        overall["expected_value_per_trade"] = round(wr * aw + (1 - wr) * al, 6)
        # 期望值归一化为「> 1 视为正向期望」「< 0 视为负向」
        if overall["expected_value_per_trade"] > 0:
            overall["expectancy_label"] = "正向"
        elif overall["expected_value_per_trade"] < 0:
            overall["expectancy_label"] = "负向"
        else:
            overall["expectancy_label"] = "中性"
    else:
        overall["expected_value_per_trade"] = None
        overall["expectancy_label"] = "数据不足"
    return {
        "overall": overall,
        "strategy_id": strategy_id,
        "strategy_version_id": version_id,
        "by_asset": by_asset,
        "by_action": by_action,
        "is_paper_trading": True,
    }


@router.get("/api/replay/settings")
async def get_replay_settings():
    return await asyncio.to_thread(_api()._combined_replay_settings)


@router.post("/api/replay/settings")
async def update_replay_settings(body: PaperTradingBody):
    try:
        return await asyncio.to_thread(_api()._apply_replay_settings, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/replay/reset")
async def reset_replay(force: bool = False):
    """一键重置模拟盘（自动备份 → 清空交易痕迹），供复盘页“重置”按钮调用。

    v482: `force=true` 越过 15 分钟重置冷却（默认冷却见 `REPLAY_RESET_COOLDOWN_S`）。
    """
    return await asyncio.to_thread(_api()._reset_replay_sync, force)


@router.get("/api/replay/positions")
async def get_replay_positions(limit: int = 200):
    _api()._replay_want_limit(limit)
    _api()._replay_ensure_refresher()
    snapshot = _api()._replay_get_snapshot(limit)
    if snapshot is not None:
        return snapshot
    # 快照还没就绪 → 立刻返回「无网络快版」：DB 持仓 + 缓存行情，浮盈亏拿不到就留空。
    # 前端 10s 后再轮询就有完整快照，不会卡在「加载中…」。
    return await _api()._replay_build_fast(limit)


@router.get("/api/replay/kline/live")
async def replay_kline_live(asset: str, interval: str = "1h"):
    """复盘 K 线轻量实时：最新价 + 最新 bar 成交量（加密走 Binance，2s 缓存）。

    供前端原地更新最后一根 bar（价格 2s 级跳动，成交量同步更新）。
    """
    if interval not in ("1m", "15m", "1h", "4h", "1d"):
        interval = "1h"
    # 同步网络请求必须放 to_thread，否则 Binance 慢时阻塞事件循环（所有请求断开假死）
    bn = await asyncio.to_thread(_api()._fetch_binance_klines, asset, interval)
    if bn is not None:
        last = bn[1]
        return {"asset": str(asset or "").upper(), "price": last["close"], "volume": last["volume"], "ts": int(time.time())}
    price = await asyncio.to_thread(_api()._live_price_sync, asset)
    return {"asset": str(asset or "").upper(), "price": price, "volume": None, "ts": int(time.time())}


@router.get("/api/replay/signal/{signal_id}/kline")
async def get_replay_signal_kline(signal_id: int, interval: str = "1h"):
    """根据信号 ID 生成复盘 K 线：优先真实行情（Yahoo Finance），失败降级模拟 K 线。

    真实 K 线覆盖入场前后窗口（1m→±6h、15m/1h→±24h、4h→±72h、1d→±60 天）；
    进/出场位置由前端按 entry_price / exit_price 用竖线标注。
    """
    if interval not in ("1m", "15m", "1h", "4h", "1d"):
        interval = "1h"
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                ad.id, qs.entry_price, qs.exit_price, qs.max_price,
                qs.min_price, qs.max_price_time, qs.min_price_time,
                ad.target_asset,
                datetime(qs.entry_ts, 'unixepoch', '+8 hours') AS entry_time
            FROM ai_decisions ad
            LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
            WHERE ad.id = ?
            """,
            (signal_id,),
        )
        row = await cur.fetchone()
        await cur.close()

    if row is None:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")

    d = dict(row)
    if not d.get("entry_price"):
        raise HTTPException(status_code=400, detail="signal has no entry_price")

    # Convert entry_time to epoch seconds
    entry_time = d.get("entry_time")
    if isinstance(entry_time, str):
        try:
            entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
        except Exception:
            entry_time = datetime.now(TZ_SHANGHAI)
    elif entry_time is None:
        entry_time = datetime.now(TZ_SHANGHAI)
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=TZ_SHANGHAI)
    entry_ts = int(entry_time.timestamp())

    # 真实 K 线优先（按 market 分发：binance_fapi/yahoo/okx_swap/domestic_spot）；失败降级模拟 K 线
    real_klines = await asyncio.to_thread(_api()._fetch_real_klines, str(d.get("target_asset") or ""), entry_ts, interval)
    if real_klines is not None:
        klines = real_klines
        data_source = "real"
        # market_type 反映真实数据来源：加密=binance_perp、股票=yahoo（前端的 Yahoo Finance 角标用）
        target_asset = str(d.get("target_asset") or "")
        r_meta = quote_sources.route(target_asset)
        if r_meta is not None and r_meta["market"] == "yahoo":
            market_type = "yahoo"
        else:
            # 加密币（binance_fapi/okx_swap）/ 原油（domestic_spot）统一标 binance_perp（与历史口径兼容）
            market_type = "binance_perp"
    else:
        klines = _api()._build_simulated_klines(
            entry_time_ts=entry_ts,
            entry_price=float(d["entry_price"]),
            max_price=d.get("max_price"),
            min_price=d.get("min_price"),
            exit_price=d.get("exit_price"),
            asset=str(d.get("target_asset") or ""),
        )
        data_source = "simulated"
        market_type = "simulated"
    action = str(d.get("suggested_action") or "HOLD").upper()
    try:
        params = json.loads(d.get("strategy_params") or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    trailing = float(params.get("trailing_callback_rate") or 0.0)
    entry_price = float(d["entry_price"])
    max_price = float(d["max_price"]) if d.get("max_price") else entry_price
    min_price = float(d["min_price"]) if d.get("min_price") else entry_price
    if action == "BUY":
        stop_loss = max_price * (1 - trailing / 100) if trailing > 0 else entry_price * (1 - 0.8 / 100)
        take_profit = max_price
    elif action == "SELL":
        stop_loss = min_price * (1 + trailing / 100) if trailing > 0 else entry_price * (1 + 0.8 / 100)
        take_profit = min_price
    else:
        stop_loss = None
        take_profit = None
    markers = [{
        "time": entry_ts,
        "position": "belowBar" if action == "BUY" else "aboveBar",
        "color": "#2ebd85" if action == "BUY" else "#f6465d",
        "shape": "arrowUp" if action == "BUY" else "arrowDown",
        "text": f"{action} {entry_price}",
        "kind": "entry",
    }]
    if d.get("exit_price"):
        markers.append({
            "time": int(d["max_price_time"] or d["min_price_time"] or entry_ts),
            "position": "aboveBar" if action == "BUY" else "belowBar",
            "color": "#9aa3af",
            "shape": "circle",
            "text": f"EXIT {float(d['exit_price']):.4f}",
            "kind": "exit",
        })
    return {
        "signal_id": signal_id,
        "asset": d.get("target_asset"),
        "action": action,
        "entry_price": entry_price,
        "entry_time": entry_ts,
        "exit_price": d.get("exit_price"),
        "stop_loss": round(stop_loss, 6) if stop_loss else None,
        "take_profit": round(take_profit, 6) if take_profit else None,
        "trailing_callback_rate": trailing,
        "invalidation_condition": d.get("invalidation_condition") or "",
        "settled": int(d.get("settled") or 0),
        "is_paper_trading": True,
        "data_source": data_source,
        "market_type": market_type,   # binance_perp=币安永续真实行情 / simulated=模拟反推
        "markers": markers,
        "klines": klines,
    }

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
