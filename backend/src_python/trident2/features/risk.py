# -*- coding: utf-8 -*-
"""风控与执行类（纸面 / dry-run）：F04 / F05 / F07 / F08 / F09 / F10 / F25 / F27 / F38。

铁律：本模块不产生真实委托。所有「执行」都写到 t2_orders（mode=paper/dry_run）
和 t2_interventions，真实下单需要 TRADING_MANAGER_ENABLED + 交易所密钥，
且不在本层直接触发。
"""
from __future__ import annotations

import json as _json
import statistics
import time
from typing import Any, Dict, List, Optional

from .. import store
from ..registry import feature
from ..types import FeatureResult
from ._util import correlation, mean, pct_change, quantile


def json_load(raw: Any) -> Dict[str, Any]:
    try:
        return _json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}

SLIPPAGE_WARN_BP = 10.0
VA_R_Z = 1.645  # 95% 单尾
CONCENTRATION_WARN = 0.5


def _position_value(pos: Dict[str, Any], price: Optional[float]) -> Optional[float]:
    if price is None:
        return None
    return abs(float(pos["qty"]) * float(price))


# ── F04 持仓 thesis ─────────────────────────────────────────────────────
@feature("F04")
def position_thesis(ctx) -> FeatureResult:
    positions = ctx.positions("open")
    if not positions:
        return FeatureResult.no_data(
            "F04", "持仓 thesis：当前无持仓",
            "t2_positions 无 open 记录，先通过纸面下单建立持仓",
            evidence=ctx.evidence(table="t2_positions"),
        )
    rows = []
    missing = []
    for p in positions:
        th = p.get("thesis_json") if isinstance(p.get("thesis_json"), dict) else {}
        reason = th.get("reason") or th.get("entry_reason")
        invalidation = th.get("invalidation") or p.get("stop_price")
        time_stop = th.get("time_stop") or p.get("time_stop_min")
        complete = bool(reason and invalidation and time_stop)
        summary = f"{p['asset']} {p['side']} {p['qty']}@{p['entry_price']}｜" \
                  f"理由：{reason or '缺失'}｜失效：{invalidation or '缺失'}｜时限：{time_stop or '缺失'}min"
        rows.append({"position_id": p["id"], "asset": p["asset"], "complete": complete, "summary": summary})
        if not complete:
            missing.append(p["id"])
    return FeatureResult.ok(
        "F04", f"持仓 thesis：{len(positions) - len(missing)}/{len(positions)} 条完整",
        {"positions": rows, "missing": missing, "coverage": (len(positions) - len(missing)) / len(positions)},
        evidence=ctx.evidence(table="t2_positions"), sample_size=len(positions),
        severity="warn" if missing else "info",
        action={"kind": "require_thesis", "position_ids": missing} if missing else None,
    )


# ── F05 一键止血剧本 ────────────────────────────────────────────────────
@feature("F05")
def kill_switch(ctx, playbook: str = "flatten_all", confirm: bool = False,
                reason: str = "") -> FeatureResult:
    valid = {"flatten_all", "cancel_all", "stop_new_strategies", "stop_new_orders"}
    if playbook not in valid:
        return FeatureResult.not_configured(
            "F05", "一键止血：未知剧本", f"playbook={playbook} 不在 {sorted(valid)}", 
            evidence=ctx.evidence(playbook=playbook),
        )
    positions = ctx.positions("open")
    orders = ctx.orders(open_only=True)
    if not positions and not orders:
        return FeatureResult.no_data(
            "F05", "一键止血：无持仓、无挂单", "没有可止血的对象",
            evidence=ctx.evidence(tables=["t2_positions", "t2_orders"]),
        )

    plan_positions = []
    for p in positions:
        px = ctx.price(p["asset"])  # 取不到就留空，不拿入场价顶替
        plan_positions.append({"position_id": p["id"], "asset": p["asset"], "side": p["side"],
                               "qty": p["qty"], "mark_price": px})
    plan_orders = [{"order_id": o["id"], "asset": o["asset"], "side": o["side"],
                    "order_type": o["order_type"], "status": o["status"]} for o in orders]

    executed = False
    results: List[Dict[str, Any]] = []
    if confirm:
        if playbook in ("flatten_all", "stop_new_strategies"):
            for p in plan_positions:
                if p["mark_price"] is None:
                    continue
                closed = store.close_position(ctx.conn, p["position_id"], float(p["mark_price"]),
                                              note=f"kill_switch:{playbook}")
                results.append({"position_id": p["position_id"], "closed": True,
                                "pnl": closed.get("realized_pnl")})
        if playbook in ("cancel_all", "flatten_all", "stop_new_orders"):
            for o in plan_orders:
                store.update_order(ctx.conn, o["order_id"], status="cancelled", note=f"kill_switch:{playbook}")
                results.append({"order_id": o["order_id"], "cancelled": True})
        store.insert_intervention(ctx.conn, kind=f"kill_switch:{playbook}",
                                  target="all", note=reason or "一键止血",
                                  before_json={"positions": plan_positions, "orders": plan_orders},
                                  after_json={"results": results})
        store.set_state(ctx.conn, "trading_halted", {
            "playbook": playbook, "ts": ctx.now, "until": ctx.now + 3600,
        })
        executed = True

    value = {
        "playbook": playbook,
        "dry_run": not confirm,
        "requires_confirm": not confirm,
        "positions_to_close": plan_positions,
        "orders_to_cancel": plan_orders,
        "executed": executed,
        "execution_mode": "paper",
        "results": results,
    }
    severity = "warn" if confirm else "watch"
    headline = ("一键止血：已执行 " if confirm else "一键止血：演练（待二次确认）") + playbook
    return FeatureResult.ok(
        "F05", headline, value,
        evidence=ctx.evidence(tables=["t2_positions", "t2_orders", "t2_interventions"]),
        sample_size=len(plan_positions) + len(plan_orders), severity=severity,
        action=None if confirm else {"kind": "kill_switch", "playbook": playbook, "confirm_required": True},
    )


# ── F07 组合级风险与压力测试 ────────────────────────────────────────────
@feature("F07")
def portfolio_risk(ctx) -> FeatureResult:
    positions = ctx.positions("open")
    acc = ctx.account()
    equity = acc.get("equity") or 0
    if len(positions) < 2 or equity <= 0:
        return FeatureResult.no_data(
            "F07", f"组合风险：持仓 {len(positions)} 笔 / 权益 {equity}",
            "组合级 VaR 至少需要 2 笔持仓 + 非零权益；当前样本不足",
            evidence=ctx.evidence(tables=["t2_positions", "t2_account"]), sample_size=len(positions),
        )
    marks, rets, weights = {}, {}, {}
    for p in positions:
        px = ctx.price(p["asset"])
        if px is None:
            continue
        marks[p["asset"]] = px
        weights[p["asset"]] = weights.get(p["asset"], 0) + abs(float(p["qty"]) * px)
        r = ctx.returns_pct(ctx.daily_closes(p["asset"], 90))
        if len(r) >= 20:
            rets[p["asset"]] = r
    if len(rets) < 2:
        return FeatureResult.no_data(
            "F07", "组合风险：历史收益样本不足",
            f"仅 {len(rets)} 个持仓资产有 >=20 日收益序列，无法估计协方差",
            evidence=ctx.evidence(table="kline_bars/ohlcv_daily"), sample_size=len(rets),
        )
    total = sum(weights.values()) or 1
    w = {a: v / total for a, v in weights.items()}
    cov_assets = sorted(rets.keys())
    n = min(len(rets[a]) for a in cov_assets)
    corr = {}
    for i, a in enumerate(cov_assets):
        for b in cov_assets[i + 1:]:
            c = correlation(rets[a][-n:], rets[b][-n:])
            if c is not None:
                corr[f"{a}-{b}"] = c
    port_ret = []
    for i in range(n):
        port_ret.append(sum(w[a] * rets[a][-n + i] for a in cov_assets if a in w))
    # port_ret 是百分比单位（%），VaR 用权益金额时必须先除 100，否则会把 2% 的日波动算成 200%
    sigma = statistics.pstdev(port_ret) if len(port_ret) > 2 else None
    var95 = (VA_R_Z * sigma / 100.0 * equity) if sigma else None
    hhi = sum(v ** 2 for v in w.values())
    # 情景：BTC −10%，用各资产对 BTC 日收益的 beta 传导
    stress = {}
    if "BTC" in rets:
        for a in cov_assets:
            r = correlation(rets[a][-n:], rets["BTC"][-n:])
            beta = None
            if r is not None:
                sd_a = statistics.pstdev(rets[a][-n:])
                sd_b = statistics.pstdev(rets["BTC"][-n:])
                beta = r * sd_a / sd_b if sd_b else None
            if beta is not None:
                stress[a] = {"beta_to_btc": beta, "pnl_pct_if_btc_minus10": beta * -10.0}
    severity = "info"
    if (var95 is not None and var95 / equity > 0.05) or hhi > CONCENTRATION_WARN:
        severity = "warn"
    value = {
        "equity": equity,
        "notional": total,
        "weights": w,
        "hhi_concentration": hhi,
        "daily_sigma_pct": sigma * 100 if sigma else None,
        "var95_1d_usdt": var95,
        "var95_pct_equity": (var95 / equity * 100) if var95 else None,
        "correlations": corr,
        "stress_btc_minus10": stress,
    }
    var_pct = (var95 / equity * 100) if var95 else None
    return FeatureResult.ok(
        "F07", f"组合风险：集中度 HHI {hhi:.2f}，"
               + (f"1日 95% VaR {var_pct:.2f}% 权益" if var_pct is not None else "VaR 样本不足"), value,
        evidence=ctx.evidence(tables=["t2_positions", "t2_account", "kline_bars"], window="90d"),
        sample_size=n, severity=severity,
    )


# ── F08 每笔自动挂保护单 ────────────────────────────────────────────────
@feature("F08")
def protective_orders(ctx, auto: bool = False) -> FeatureResult:
    positions = ctx.positions("open")
    if not positions:
        return FeatureResult.no_data(
            "F08", "保护单：当前无持仓", "没有需要挂保护单的仓位",
            evidence=ctx.evidence(table="t2_positions"),
        )
    rows, missing = [], []
    for p in positions:
        has_stop = p.get("stop_price") is not None
        has_target = p.get("target_price") is not None
        has_time = p.get("time_stop_min") is not None
        rows.append({"position_id": p["id"], "asset": p["asset"],
                     "stop": has_stop, "target": has_target, "time_stop": has_time})
        if not (has_stop and has_target and has_time):
            missing.append(p["id"])
    created = 0
    if auto and missing:
        for p in positions:
            if p["id"] not in missing:
                continue
            px = ctx.price(p["asset"]) or float(p["entry_price"])
            side = p["side"].lower()
            stop = p.get("stop_price") or (px * 0.985 if side in ("long", "buy") else px * 1.015)
            target = p.get("target_price") or (px * 1.03 if side in ("long", "buy") else px * 0.97)
            store.insert_order(ctx.conn, asset=p["asset"], side="sell" if side in ("long", "buy") else "buy",
                               order_type="OCO", qty=p["qty"], stop_price=stop, target_price=target,
                               oco_group=f"POS{p['id']}", status="working", mode="paper",
                               parent_position_id=p["id"], note="auto protective from F08")
            created += 1
    coverage = (len(positions) - len(missing)) / len(positions)
    value = {"positions": rows, "missing_protection": missing, "coverage": coverage,
             "orders_created": created, "mode": "paper"}
    return FeatureResult.ok(
        "F08", f"保护单覆盖 {coverage:.0%}（{len(positions) - len(missing)}/{len(positions)}）", value,
        evidence=ctx.evidence(tables=["t2_positions", "t2_orders"]), sample_size=len(positions),
        severity="warn" if missing else "info",
        action={"kind": "create_protective_orders", "position_ids": missing} if missing else None,
    )


def _implied_probabilities(ctx, limit: int = 4, window: int = 3 * 86400) -> List[Dict[str, Any]]:
    """Polymarket 事件概率（macro_snapshots.category='prediction'）。

    F09 本身是「事件驱动」的，附带市场对 Fed 路径/衰退/加密目标价的隐含概率，
    让降仓预案有前瞻坐标；只做展示，不参与阈值判断。
    """
    rows = ctx.q(
        "SELECT metric_key, value, payload, ts FROM macro_snapshots "
        "WHERE category='prediction' AND value IS NOT NULL AND ts >= ? "
        "ORDER BY ts DESC LIMIT 200", (ctx.now - window,), note="prediction_snapshots",
    )
    seen, out = set(), []
    for r in rows:
        key = r["metric_key"]
        if key in seen:
            continue
        seen.add(key)
        payload = json_load(r["payload"]) or {}
        out.append({"metric": key.replace("prob:", ""), "prob": round(float(r["value"]), 4),
                    "question": payload.get("question") or payload.get("label"),
                    "ts": r["ts"]})
    return out[:limit]


# ── F09 事件驱动降仓 ────────────────────────────────────────────────────
@feature("F09")
def event_de_risk(ctx, confirm: bool = False, min_impact: float = 8.0) -> FeatureResult:
    decisions = [d for d in ctx.decisions_since(ctx.now - 2 * 3600)
                 if d.get("impact_score") is not None and float(d["impact_score"]) >= min_impact
                 and str(d.get("suggested_action") or "").upper() not in ("HOLD", "")]
    if not decisions:
        return FeatureResult.no_data(
            "F09", f"事件驱动降仓：2h 内无 impact≥{min_impact} 的非 HOLD 事件",
            "没有达到阈值的高冲击事件，不需要降仓预案",
            evidence={**ctx.evidence(table="ai_decisions", window="2h"),
                      "implied_probabilities": _implied_probabilities(ctx)},
        )
    positions = ctx.positions("open")
    if not positions:
        return FeatureResult.no_data(
            "F09", f"事件驱动降仓：有 {len(decisions)} 个高冲击事件，但无持仓",
            "没有可降的仓位；预案为空",
            evidence={**ctx.evidence(tables=["ai_decisions", "t2_positions"]),
                      "implied_probabilities": _implied_probabilities(ctx)},
            sample_size=len(decisions),
        )
    plan = []
    for p in positions:
        hits = [d for d in decisions if str(d.get("target_asset") or "").upper() == p["asset"].upper()]
        if not hits:
            continue
        impact = max(float(d["impact_score"]) for d in hits)
        reduce_ratio = 1.0 if impact >= 9 else 0.5
        plan.append({"position_id": p["id"], "asset": p["asset"], "impact": impact,
                     "reduce_ratio": reduce_ratio,
                     "trigger_decision_ids": [d["id"] for d in hits]})
    executed = []
    if confirm:
        for item in plan:
            if item["reduce_ratio"] >= 1.0:
                px = ctx.price(item["asset"])
                if px:
                    store.close_position(ctx.conn, item["position_id"], px, note="F09 event de-risk")
                    executed.append({"position_id": item["position_id"], "action": "closed"})
                else:
                    executed.append({"position_id": item["position_id"], "action": "no_mark_price"})
            else:
                executed.append({"position_id": item["position_id"],
                                 "action": "manual_partial_reduce", "ratio": item["reduce_ratio"]})
        store.insert_intervention(ctx.conn, kind="event_de_risk", target=",".join(i["asset"] for i in plan),
                                  note=f"impact>={min_impact}", before_json={"plan": plan},
                                  after_json={"executed": executed})
    value = {"high_impact_events": len(decisions), "plan": plan, "executed": executed,
             "dry_run": not confirm, "mode": "paper"}
    return FeatureResult.ok(
        "F09", f"事件驱动降仓：{len(decisions)} 个高冲击事件 → {len(plan)} 笔仓位处置", value,
        evidence=ctx.evidence(tables=["ai_decisions", "t2_positions"]), sample_size=len(decisions),
        severity="warn" if plan else "info",
        action={"kind": "de_risk", "confirm_required": True} if plan and not confirm else None,
    )


# ── F10 执行质量监控 ────────────────────────────────────────────────────
@feature("F10")
def execution_quality(ctx) -> FeatureResult:
    fills = [o for o in ctx.orders() if o.get("filled_price") and o.get("price")]
    if len(fills) < 3:
        return FeatureResult.no_data(
            "F10", f"执行质量：成交样本 {len(fills)} 笔",
            "滑点统计至少需要 3 笔有成交价与委托价的订单",
            evidence=ctx.evidence(table="t2_orders"), sample_size=len(fills),
        )
    slips = []
    for o in fills:
        ref = float(o["price"])
        fill = float(o["filled_price"])
        if ref <= 0:
            continue
        sign = 1.0 if o["side"].lower() in ("buy", "long") else -1.0
        slips.append((fill - ref) / ref * 10000 * sign)
    by_asset: Dict[str, List[float]] = {}
    for o, s in zip(fills, slips):
        by_asset.setdefault(o["asset"], []).append(s)
    bad = [a for a, vals in by_asset.items() if mean(vals) is not None and mean(vals) > SLIPPAGE_WARN_BP]
    value = {"n_fills": len(slips), "median_slippage_bp": statistics.median(slips),
             "mean_slippage_bp": mean(slips), "p90_slippage_bp": quantile(slips, 0.9),
             "by_asset": {a: mean(v) for a, v in by_asset.items()}, "degraded_assets": bad}
    return FeatureResult.ok(
        "F10", f"执行质量：中位滑点 {statistics.median(slips):.1f}bp，{len(bad)} 个品种恶化", value,
        evidence=ctx.evidence(table="t2_orders", fields="price/filled_price"),
        sample_size=len(slips), severity="warn" if bad else "info",
        action={"kind": "throttle", "assets": bad} if bad else None,
    )


# ── F25 流动性骤降 ──────────────────────────────────────────────────────
@feature("F25")
def liquidity_drop(ctx) -> FeatureResult:
    assets = ctx.assets()
    rows = []
    for a in assets:
        hist = ctx.depth_history(a, limit=24)
        if len(hist) < 3:
            continue
        cur = hist[0]
        spreads = [h["spread_bp"] for h in hist[1:] if h["spread_bp"] is not None]
        depths = [(h["bid_vol"] or 0) + (h["ask_vol"] or 0) for h in hist[1:]]
        if not spreads:
            continue
        med_spread = statistics.median(spreads)
        med_depth = statistics.median(depths) if depths else None
        cur_spread = cur["spread_bp"]
        cur_depth = (cur["bid_vol"] or 0) + (cur["ask_vol"] or 0)
        spread_ratio = (cur_spread / med_spread) if med_spread else None
        depth_ratio = (cur_depth / med_depth) if med_depth else None
        flag = None
        if spread_ratio and spread_ratio >= 3 and depth_ratio is not None and depth_ratio <= 0.5:
            flag = "critical"
        elif (spread_ratio and spread_ratio >= 2) or (depth_ratio is not None and depth_ratio <= 0.5):
            flag = "warn"
        rows.append({"asset": a, "spread_bp": cur_spread, "median_spread_bp": med_spread,
                     "spread_x": spread_ratio, "depth": cur_depth, "median_depth": med_depth,
                     "depth_x": depth_ratio, "flag": flag})
    if not rows:
        return FeatureResult.no_data(
            "F25", "流动性骤降：无盘口快照",
            "depth_snapshots 中每个资产至少需要 3 条历史才能比较；当前不足",
            evidence=ctx.evidence(table="depth_snapshots"),
        )
    worst = [r for r in rows if r["flag"]]
    severity = "critical" if any(r["flag"] == "critical" for r in rows) else ("warn" if worst else "info")
    return FeatureResult.ok(
        "F25", f"流动性：{len(worst)}/{len(rows)} 个资产点差或深度恶化", {"assets": rows},
        evidence=ctx.evidence(table="depth_snapshots", window="最近 24 条"), sample_size=len(rows),
        severity=severity,
        action={"kind": "halt_entries", "assets": [r["asset"] for r in rows if r["flag"] == "critical"]} if severity == "critical" else None,
    )


# ── F27 杠杆三连 ────────────────────────────────────────────────────────
@feature("F27")
def leverage_triple(ctx) -> FeatureResult:
    assets = ("BTC", "ETH", "SOL", "XAU")
    rows = []
    for a in assets:
        funding_rows = ctx.macro_history("structure", f"{a}.funding", 30)
        funding_vals = [r["value"] for r in funding_rows if r["value"] is not None]
        oi_rows = ctx.macro_history("structure", f"{a}.oi", 30)
        oi_vals = [r["value"] for r in oi_rows if r["value"] is not None]
        liq_rows = ctx.macro_history("liquidation", f"{a}.liq", 30)
        liq_vals = [r["value"] for r in liq_rows if r["value"] is not None]
        triggers = []
        detail = {}
        if funding_vals:
            latest = funding_vals[-1]
            detail["funding"] = latest
            if abs(latest) >= 0.0003:
                triggers.append("funding_extreme")
        if len(oi_vals) >= 8:
            latest_oi = oi_vals[-1]
            base = statistics.median(oi_vals[:-1])
            detail["oi_jump_x"] = (latest_oi / base) if base else None
            if base and latest_oi / base >= 1.1:
                triggers.append("oi_surge")
        if len(liq_vals) >= 8:
            latest_liq = liq_vals[-1]
            base = statistics.median(liq_vals[:-1])
            detail["liq_x"] = (latest_liq / base) if base else None
            if base and latest_liq / base >= 2:
                triggers.append("liq_wave")
        if len(triggers) >= 2:
            rows.append({"asset": a, "n_triggers": len(triggers), "triggers": triggers, **detail})
    if not rows:
        return FeatureResult.no_data(
            "F27", "杠杆三连：无资产同时触发 ≥2 项",
            "funding / OI / 强平至少需要 8 条历史才能判断突变；当前无组合触发",
            evidence=ctx.evidence(tables=["macro_snapshots"]),
        )
    triple = [r for r in rows if r["n_triggers"] >= 3]
    severity = "critical" if triple else "warn"
    return FeatureResult.ok(
        "F27", f"杠杆三连：{len(rows)} 个资产命中 ≥2 项，其中 {len(triple)} 个三项齐发",
        {"assets": rows, "triple_assets": [r["asset"] for r in triple]},
        evidence=ctx.evidence(tables=["macro_snapshots"], keys="funding/oi/liq"), sample_size=len(rows),
        severity=severity,
        action={"kind": "reduce_leverage", "assets": [r["asset"] for r in triple]} if triple else None,
    )


# ── F38 清算热力图 ──────────────────────────────────────────────────────
@feature("F38")
def liquidation_heatmap(ctx) -> FeatureResult:
    clusters = []
    for a in ("BTC", "ETH", "SOL", "XAU"):
        rows = ctx.q(
            "SELECT ts, value, payload FROM macro_snapshots "
            "WHERE category='liquidation' AND metric_key=? ORDER BY ts DESC LIMIT 1",
            (f"{a}.liq",), note=f"liq:{a}",
        )
        if not rows:
            continue
        payload = {}
        try:
            import json
            payload = json.loads(rows[0]["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        price = ctx.price(a)
        clusters.append({
            "asset": a, "ts": rows[0]["ts"], "total_liq": rows[0]["value"],
            "long_liq": payload.get("long_liq"), "short_liq": payload.get("short_liq"),
            "max_single_sz": payload.get("max_single_sz"), "price": price,
            "note": payload.get("note"),
        })
    if not clusters:
        return FeatureResult.no_data(
            "F38", "清算热力图：无强平数据",
            "macro_snapshots.category=liquidation 无记录",
            evidence=ctx.evidence(table="macro_snapshots"),
        )
    max_sizes = [c["max_single_sz"] for c in clusters if c["max_single_sz"]]
    large = []
    if max_sizes:
        p90 = quantile(max_sizes, 0.9)
        large = [c["asset"] for c in clusters if c["max_single_sz"] and p90 and c["max_single_sz"] >= p90]
    value = {"clusters": clusters, "large_single_liq_assets": large,
             "caveat": "数据源只给批次强平单数与单笔最大规模，不含价位分布；插针预警需价位粒度数据"}
    severity = "warn" if large else "info"
    return FeatureResult.ok(
        "F38", f"清算热力：{len(clusters)} 个品种有强平簇，{len(large)} 个出现异常大单", value,
        evidence=ctx.evidence(table="macro_snapshots", metric="*.liq"), sample_size=len(clusters),
        severity=severity,
        action={"kind": "watch_liquidation", "assets": large} if large else None,
    )
