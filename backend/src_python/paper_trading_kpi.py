"""v182.1 模拟实盘买卖点技术指标 KPI 纯函数模块。

对外只暴露 compute_buy_sell_kpi(conn, days, asset), 返回 dict。

设计要点：
- LEFT JOIN quick_sim_trades + ai_decisions + raw_news 一次拉全字段
- 按 (asset × action × outcome win/loss/breakeven) 累计
- 给前端"分张/跌制定策略"面板用：
  * BUY/SELL 命中后即时涨/跌 的胜率差（建议等涨势确认再追 / 建议做空反弹）
  * 入场距新闻发布时间的分布（<60s / 60-300s / 300-900s / 900s+, 对应赢家/输家）
  * 按事件强度 (weak/medium/strong/extreme) 分层胜率
  * 推荐入场分位（按历史 score 20-80 分位）
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _outcome(pnl_pct):
    if pnl_pct is None:
        return "breakeven"
    if pnl_pct > 0.05:
        return "win"
    if pnl_pct < -0.05:
        return "loss"
    return "breakeven"


def _stats(rs):
    n = len(rs)
    if n == 0:
        return {"count": 0}
    wins = [r for r in rs if _outcome(_safe_float(r["pnl_pct"])) == "win"]
    losses = [r for r in rs if _outcome(_safe_float(r["pnl_pct"])) == "loss"]
    pnls = [_safe_float(r["pnl_pct"]) for r in rs if _safe_float(r["pnl_pct"]) is not None]
    mfes = [_safe_float(r["mfe_pct"]) for r in rs if _safe_float(r["mfe_pct"]) is not None]
    maes = [_safe_float(r["mae_pct"]) for r in rs if _safe_float(r["mae_pct"]) is not None]
    holds = [_safe_float(r["horizon_minutes"]) for r in rs if _safe_float(r["horizon_minutes"]) is not None]
    scores = [_safe_float(r["score"]) for r in rs if _safe_float(r["score"]) is not None]
    return {
        "count": n, "win_count": len(wins), "loss_count": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "median_pnl_pct": round(sorted(pnls)[len(pnls) // 2], 4) if pnls else 0.0,
        "best_pnl_pct": round(max(pnls), 4) if pnls else 0.0,
        "worst_pnl_pct": round(min(pnls), 4) if pnls else 0.0,
        "avg_mfe_pct": round(sum(mfes) / len(mfes), 4) if mfes else None,
        "avg_mae_pct": round(sum(maes) / len(maes), 4) if maes else None,
        "avg_hold_minutes": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
    }


def _trigger_pattern(rs):
    """入场距新闻发布时间的分布：<60s / 60-300s / 300-900s / 900s+"""
    buckets = {"<60s": 0, "60-300s": 0, "300-900s": 0, "900s+": 0}
    entry_secs = []
    for r in rs:
        et = _safe_float(r["entry_ts"])
        nt = _safe_float(r["news_ts"])
        if et is None or nt is None:
            continue
        dt = et - nt
        if dt < 60:
            buckets["<60s"] += 1
        elif dt < 300:
            buckets["60-300s"] += 1
        elif dt < 900:
            buckets["300-900s"] += 1
        else:
            buckets["900s+"] += 1
        entry_secs.append(int(dt))
    return {
        "buckets": buckets,
        "median_delay_sec": int(sorted(entry_secs)[len(entry_secs) // 2]) if entry_secs else None,
        "best_delay_sec": int(sorted(entry_secs)[len(entry_secs) // 4]) if len(entry_secs) > 4 else None,
    }


def _buy_advice(w_up, n_up, w_down, n_down):
    """根据 BUY 后涨/跌 的胜率给出推荐。"""
    parts = []
    if n_up >= 5 and n_down >= 5:
        r_up = w_up / n_up
        r_down = w_down / n_down
        if r_up > r_down + 0.08:
            parts.append(
                f"历史 BUY 命中后若即时上涨 >=0.3%, 胜率 {r_up:.0%} ({w_up}/{n_up}), "
                f"明显高于跌后买入 {r_down:.0%} ({w_down}/{n_down}) -> "
                f"建议等涨势确认再追(盘中突破入场价 0.3% 才追)"
            )
        elif r_down > r_up + 0.08:
            parts.append(
                f"历史 BUY 命中后若即时下跌 >=0.3%, 胜率反而更高 {r_down:.0%} ({w_down}/{n_down}) -> "
                f"建议逢低接刀, 等价格回踩入场价 -0.3% 再加仓"
            )
        else:
            parts.append(
                f"BUY 涨/跌后胜率差异不显著(涨 {r_up:.0%} vs 跌 {r_down:.0%}) -> 当前入场时机中性"
            )
    elif n_up + n_down >= 5:
        parts.append(
            f"BUY 样本不足(涨后 {n_up} / 跌后 {n_down}), 建议积累更多样本后再做策略调整"
        )
    return " | ".join(parts) if parts else "样本太少(<5), 暂无买入时机建议"


def _sell_advice(w_up, n_up, w_down, n_down):
    """根据 SELL 后涨/跌 的胜率给出推荐。"""
    parts = []
    if n_up >= 5 and n_down >= 5:
        r_up = w_up / n_up
        r_down = w_down / n_down
        if r_down > r_up + 0.08:
            parts.append(
                f"历史 SELL 命中后若价格继续跌 >=0.3%, 胜率 {r_down:.0%} ({w_down}/{n_down}), "
                f"明显高于涨后再卖 {r_up:.0%} ({w_up}/{n_up}) -> "
                f"建议顺势追空(盘中跌破入场价 0.3% 再追)"
            )
        elif r_up > r_down + 0.08:
            parts.append(
                f"历史 SELL 后若价格已先涨 >=0.3%, 胜率反而更高 {r_up:.0%} ({w_up}/{n_up}) -> "
                f"建议做空反弹, 等价格反弹至入场价 +0.3% 再开仓"
            )
        else:
            parts.append(
                f"SELL 涨/跌后胜率差异不显著(涨 {r_up:.0%} vs 跌 {r_down:.0%}) -> 当前入场时机中性"
            )
    elif n_up + n_down >= 5:
        parts.append(
            f"SELL 样本不足(涨后 {n_up} / 跌后 {n_down}), 建议积累更多样本后再做策略调整"
        )
    return " | ".join(parts) if parts else "样本太少(<5), 暂无卖出时机建议"


def compute_buy_sell_kpi(conn, days=60, asset=None):
    """主入口：拉全 trades × decisions × news, 分类累计 BUY/SELL 后涨/跌的胜率。

    Args:
        conn: 已 open 的 sqlite3 连接 (row_factory=sqlite3.Row)
        days: 统计窗口 (默认 60 天, 最大 365)
        asset: 过滤单个资产, None=全部
    """
    base_sql = """SELECT qs.asset, qs.action, qs.score,
                         qs.entry_ts, qs.exit_ts, qs.horizon_minutes,
                         qs.pnl_pct, qs.pnl_usdt,
                         qs.max_price, qs.min_price,
                         qs.notional_usdt, qs.entry_price,
                         qs.mfe_pct, qs.mae_pct,            -- P1: 结算字段改读 quick_sim_trades
                         ad.event_strength, ad.direct_catalyst,
                         ad.prediction_type, ad.event_phase,
                         ad.sentiment_score, ad.attention_score,
                         ad.heat_score, ad.impact_score,
                         rn.published_at, rn.first_seen_ts, rn.ts AS news_ts,
                         rn.gate_strength, rn.gate_is_material
                    FROM quick_sim_trades qs
               LEFT JOIN ai_decisions ad ON qs.decision_id = ad.id
               LEFT JOIN raw_news      rn ON qs.news_id      = rn.id
                   WHERE qs.settled = 1
                     AND qs.exit_ts IS NOT NULL
                     AND qs.exit_ts >= strftime('%s', 'now', ?)"""
    days_param = f"-{int(days)} days"
    params = [days_param]
    if asset:
        base_sql += " AND qs.asset = ?"
        params.append(asset)
    base_sql += " ORDER BY qs.exit_ts DESC LIMIT 5000"
    rows = conn.execute(base_sql, params).fetchall()

    overall_stats = _stats(rows)

    per_asset_action = []
    per_bucket = {}
    for r in rows:
        k = f"{r['asset']}_{r['action']}"
        per_bucket.setdefault(k, []).append(r)
    for k, rs in sorted(per_bucket.items(), key=lambda x: -len(x[1])):
        s = _stats(rs)
        s["asset"] = rs[0]["asset"]
        s["action"] = rs[0]["action"]
        scores = sorted([_safe_float(r["score"]) for r in rs if _safe_float(r["score"]) is not None])
        if scores:
            n = len(scores)
            s["score_p20"] = round(scores[max(0, n // 5)], 4)
            s["score_p80"] = round(scores[min(n - 1, n * 4 // 5)], 4)
        per_asset_action.append(s)

    win_rows = [r for r in rows if _outcome(_safe_float(r["pnl_pct"])) == "win"]
    loss_rows = [r for r in rows if _outcome(_safe_float(r["pnl_pct"])) == "loss"]
    win_pattern = _trigger_pattern(win_rows)
    loss_pattern = _trigger_pattern(loss_rows)

    strength_buckets = {"weak": [], "medium": [], "strong": [], "extreme": []}
    for r in rows:
        es = (r["event_strength"] or "medium").lower()
        strength_buckets.setdefault(es, []).append(r)
    by_event_strength = []
    for k, rs in strength_buckets.items():
        if not rs:
            continue
        s = _stats(rs)
        s["event_strength"] = k
        by_event_strength.append(s)

    buy_rows = [r for r in rows if r["action"] == "BUY"]
    sell_rows = [r for r in rows if r["action"] == "SELL"]
    b_wu = b_wd = b_nu = b_nd = 0
    for r in buy_rows:
        ep = _safe_float(r["entry_price"])
        mx = _safe_float(r["max_price"])
        pnl = _safe_float(r["pnl_pct"])
        if ep is None or mx is None:
            continue
        win = pnl is not None and pnl > 0.05
        if mx >= ep * 1.003:
            b_nu += 1
            if win:
                b_wu += 1
        else:
            b_nd += 1
            if win:
                b_wd += 1
    s_wu = s_wd = s_nu = s_nd = 0
    for r in sell_rows:
        ep = _safe_float(r["entry_price"])
        mn = _safe_float(r["min_price"])
        pnl = _safe_float(r["pnl_pct"])
        if ep is None or mn is None:
            continue
        win = pnl is not None and pnl > 0.05
        if mn <= ep * 0.997:
            s_nd += 1
            if win:
                s_wd += 1
        else:
            s_nu += 1
            if win:
                s_wu += 1

    buy_pattern = {
        "win_rate_after_up": round(b_wu / b_nu, 4) if b_nu else 0.0,
        "win_rate_after_down": round(b_wd / b_nd, 4) if b_nd else 0.0,
        "total_after_up": b_nu,
        "total_after_down": b_nd,
        "advice": _buy_advice(b_wu, b_nu, b_wd, b_nd),
    }
    sell_pattern = {
        "win_rate_after_up": round(s_wu / s_nu, 4) if s_nu else 0.0,
        "win_rate_after_down": round(s_wd / s_nd, 4) if s_nd else 0.0,
        "total_after_up": s_nu,
        "total_after_down": s_nd,
        "advice": _sell_advice(s_wu, s_nu, s_wd, s_nd),
    }

    return {
        "days": days,
        "asset_filter": asset,
        "total_trades": overall_stats["count"],
        "overall": overall_stats,
        "per_asset_action": per_asset_action,
        "by_event_strength": by_event_strength,
        "buy_pattern": buy_pattern,
        "sell_pattern": sell_pattern,
        "win_trigger_pattern": win_pattern,
        "loss_trigger_pattern": loss_pattern,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }