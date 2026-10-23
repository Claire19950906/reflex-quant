"""共享的多证据交易闸门，不依赖 FastAPI。"""

import json
import time
from typing import Any, Dict

import evidence
import market_regime

# 买卖方向分数门槛（后台可调，5s TTL）：做多胜率偏低 → BUY 门槛默认高一档
_score_cache: Dict[str, Any] = {"ts": 0.0, "value": {"buy_min": 0.4, "sell_min": 0.3}}


def _direction_scores() -> Dict[str, float]:
    now = time.monotonic()
    if now - float(_score_cache.get("ts", 0.0)) < 5.0:
        return _score_cache["value"]
    value = {"buy_min": 0.4, "sell_min": 0.3}
    try:
        from paper_trading import get_settings as _get_pt_settings
        st = _get_pt_settings()
        value = {
            "buy_min": float(st.get("buy_min_score", 0.4)),
            "sell_min": float(st.get("sell_min_score", 0.3)),
        }
    except Exception:
        pass
    _score_cache.update({"ts": now, "value": value})
    return value


def _clamp(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _clamp10(value: Any, default: float = 5.0) -> float:
    """影响评估分数 (0-10)：缺失/非法 → default（中性 5 = 不加不减）。"""
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return default


def evaluate_decision(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        context = json.loads(row.get("decision_context") or "{}")
    except (json.JSONDecodeError, TypeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    assets = context.get("assets") if isinstance(context.get("assets"), dict) else {}
    asset = str(row.get("target_asset") or "BTC").upper()
    asset_context = assets.get(asset) or assets.get("BTC") or {}
    if not isinstance(asset_context, dict):
        asset_context = {}
    stats = asset_context.get("stats_7d") if isinstance(asset_context.get("stats_7d"), dict) else {}
    confirmation_text = str(row.get("market_confirmation") or "unknown").lower()
    confirmation = 1.0 if confirmation_text in {"confirmed", "strong", "yes", "positive"} else (-1.0 if confirmation_text in {"rejected", "opposite", "negative"} else _clamp(asset_context.get("change_24h_pct", 0) / 5))
    trend_text = str(stats.get("trend", "unknown")).lower()
    trend = 1.0 if "strong bull" in trend_text else 0.6 if "bull" in trend_text else -1.0 if "strong bear" in trend_text else -0.6 if "bear" in trend_text else 0.0
    atr_pct = _clamp(stats.get("atr_pct", 0), 0, 100)
    funding_pct = _clamp(asset_context.get("funding_rate_pct", 0), -1, 1)
    cluster = max(1, int(row.get("cluster_size") or 1))
    sample = row.get("history_sample") if isinstance(row.get("history_sample"), dict) else {}
    if sample:
        settled = int(sample.get("total") or 0)
        wins = int(sample.get("wins") or 0)
        scope = str(sample.get("scope") or "asset")
    else:
        settled = int(row.get("history_total") or 0)
        wins = int(row.get("history_wins") or 0)
        scope = str(row.get("history_scope") or "asset")
    scope_note = {
        "asset": "同品种",
        "lane": "同赛道自动补齐",
        "market": "全市场自动补齐",
    }.get(scope, "同品种")
    # ── 价格-资金背离（领先于 lagged trend 的方向信号）──
    divergence = market_regime.detect_price_fund_divergence(
        trend=str(stats.get("trend", "")),
        funding_rate_pct=funding_pct,
        basis_bps=asset_context.get("basis_bps"),
        oi_change_pct=asset_context.get("oi_change_pct"),
    )
    regime_score = (
        -divergence["strength"] if divergence["divergence"] == "bearish"
        else divergence["strength"] if divergence["divergence"] == "bullish"
        else 0.0
    )
    factors = {
        "news_sentiment": {"score": _clamp(row.get("sentiment_score")), "explanation": "AI 新闻情绪分"},
        "market_confirmation": {"score": confirmation, "explanation": f"市场确认={confirmation_text}"},
        "trend": {"score": trend, "explanation": f"快照趋势={stats.get('trend', 'Unknown')}"},
        "volatility": {"score": _clamp(1 - atr_pct / 10), "explanation": f"ATR={atr_pct:.3f}%"},
        "funding": {"score": _clamp(-funding_pct / 0.1), "explanation": f"资金费率={funding_pct:.4f}%"},
        "cluster_heat": {"score": _clamp((cluster - 1) / 4), "explanation": f"新闻聚合数量={cluster}"},
        "historical_confidence": {"score": _clamp(((wins / settled) - 0.5) * 2) if settled else 0.0, "explanation": f"{scope_note}已结算 {settled} 条，胜 {wins} 条"},
        "price_fund_divergence": {"score": regime_score, "explanation": divergence["verdict"]},
    }
    weights = {"news_sentiment": 0.30, "market_confirmation": 0.20, "trend": 0.15, "volatility": 0.10, "funding": 0.10, "cluster_heat": 0.10, "historical_confidence": 0.05, "price_fund_divergence": 0.15}
    final_score = sum(factors[name]["score"] * weight for name, weight in weights.items())
    # 影响评估加权（AI 分析产出的 0-10 分；缺省 5 = 中性，不加不减）：
    #   市场影响力 impact_score 权重最高，其次目标资产影响 asset_impact，舆情热度 heat_score 最轻
    impact = _clamp10(row.get("impact_score"), 5.0)
    heat = _clamp10(row.get("heat_score"), 5.0)
    asset_impact = _clamp10(row.get("asset_impact"), 5.0)
    impact_boost = (impact - 5.0) * 0.03 + (heat - 5.0) * 0.01 + (asset_impact - 5.0) * 0.02
    # price_fund_divergence 已纳入 weights（0.15）：价格涨但资金空 → 负分下拉（压 BUY / 扶 SELL）；反之亦然
    final_score = round(final_score + impact_boost, 4)
    # 方向不对称门槛：BUY 需要更高分（做多胜率偏低），SELL 门槛较低（做空更倾向）
    scores = _direction_scores()
    raw_action = (
        "BUY" if final_score >= scores["buy_min"]
        else "SELL" if final_score <= -scores["sell_min"]
        else "HOLD"
    )
    validation = evidence.evaluate(factors, final_score, raw_action, wins, settled, scope=scope)
    # ── v181: 取消「只能看」分支（realtime_filter 二分类后此值不再产生）。
    # 若历史数据里有遗留 verdict="只能看"，这里降级为"放行（老数据兜底）"，
    # 不再用它强制 HOLD（避免旧新闻被永久封印）。新进新闻只会有"可交易"或"排除"。
    gate_verdict = str(row.get("gate_verdict") or "")
    if gate_verdict == "只能看":
        # 历史遗留：当作可交易，让 evidence + score 自行裁决
        gate_verdict = ""  # 让后续分支等价于"无门卫标签"
    if gate_verdict == "排除":
        return {
            "factors": factors,
            "weights": weights,
            "final_score": round(final_score, 4),
            "raw_action": "HOLD",
            "action": "HOLD",
            "confidence": validation["confidence"],
            "evidence": validation["evidence"],
            "significance": validation["significance"],
            "contradictions": validation["contradictions"],
            "confidence_detail": validation["confidence_detail"],
            "verdict": f"门卫四步标签=排除：只分析不下单",
            "gate_blocked": True,
            "gate_verdict": gate_verdict,
        }
    return {
        "factors": factors,
        "weights": weights,
        "final_score": round(final_score, 4),
        "raw_action": raw_action,
        "action": validation["gated_action"],
        "confidence": validation["confidence"],
        "evidence": validation["evidence"],
        "significance": validation["significance"],
        "contradictions": validation["contradictions"],
        "confidence_detail": validation["confidence_detail"],
        "verdict": validation["verdict"],
    }
