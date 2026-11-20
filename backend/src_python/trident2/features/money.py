# -*- coding: utf-8 -*-
"""资金管理类（6 项）：F18 / F54 / F55 / F56 / F57 / F58。

全部只做「算账 + 出方案」，不执行任何真实资金动作；
权益来源是 t2_account（纸面账本），未配置初始权益时只给单位名义下的测算。
"""
from __future__ import annotations

import datetime as _dt
import statistics
from typing import Any, Dict, List, Optional

from .. import store
from ..registry import feature
from ..types import FeatureResult
from ._util import mean, pct_change, realized_vol

DEFAULT_FEE_BPS = 8.0


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> Optional[int]:
    n = _num(v)
    if n is not None:
        return int(n / 1000) if n > 10_000_000_000 else int(n)
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(_dt.datetime.strptime(s[:19], fmt).timestamp())
        except ValueError:
            continue
    return None


def _equity(ctx) -> Dict[str, Any]:
    acc = ctx.account() or {}
    return {
        "mode": acc.get("mode"), "cash": _num(acc.get("cash")) or 0.0,
        "equity": _num(acc.get("equity")) or 0.0,
        "realized_pnl": _num(acc.get("realized_pnl")) or 0.0,
        "fee_bps": _num(acc.get("fee_bps")) or DEFAULT_FEE_BPS,
        "updated_at": acc.get("updated_at"),
    }


# ── F18 成本账本 ────────────────────────────────────────────────────────
@feature("F18")
def cost_ledger(ctx, days: int = 30) -> FeatureResult:
    rows = ctx.cost_entries(since_ts=ctx.now - days * 86400)
    audit = [a for a in ctx.trade_audit(300) if (a.get("status") or "").upper() == "ORDER_PLACED"]
    fee_bps = _equity(ctx)["fee_bps"]
    fee_cost = 0.0
    for a in audit:
        notional = _num(a.get("notional_usdt")) or 0.0
        if notional:
            fee_cost += notional * fee_bps / 10000.0 * 2
    by_cat: Dict[str, float] = {}
    daily: Dict[str, float] = {}
    by_cat_tokens: Dict[str, float] = {}
    total_tokens = 0.0
    for r in rows:
        # v453 P2-E16：账本里现在有一类 currency='TOKEN' 的 LLM 用量行。
        # **不能**把 token 当 USDT 相加（否则 500 万 token 会显示成 500 万"美元"）。
        cat = r.get("category") or "other"
        amount = _num(r.get("amount")) or 0.0
        if str(r.get("currency") or "").upper() == "TOKEN":
            by_cat_tokens[cat] = by_cat_tokens.get(cat, 0.0) + amount
            total_tokens += amount
            continue
        by_cat[cat] = by_cat.get(cat, 0.0) + amount
        daily[str(r.get("day") or "")] = daily.get(str(r.get("day") or ""), 0.0) + amount
    total = sum(by_cat.values()) + fee_cost
    gaps = []
    if not by_cat_tokens:
        gaps.append("LLM token 用量未接入（cost_ledger 无 TOKEN 行）")
    else:
        gaps.append("LLM token 已记数量，但单价未配置（不臆造费用）")
    gaps.append("数据订阅账单无接口，无法核对")
    if not rows and not audit:
        return FeatureResult.no_data(
            "F18", "成本账本：无任何成本记录",
            "t2_cost_ledger 为空且 trade_audit_log 无 ORDER_PLACED",
            evidence=ctx.evidence(tables=["t2_cost_ledger", "trade_audit_log"]),
        )
    _msg = (f"成本账本：近 {days} 天合计 {total:.2f} USDT"
            f"（手续费 {fee_cost:.2f} + 记账 {sum(by_cat.values()):.2f}）")
    if total_tokens:
        _msg += f"；LLM token {int(total_tokens):,}（单价未配置）"
    return FeatureResult.ok(
        "F18", _msg,
        {"window_days": days, "fee_bps": fee_bps, "fee_cost_est": round(fee_cost, 4),
         "ledger_total": round(sum(by_cat.values()), 4), "by_category": by_cat,
         "llm_tokens_total": int(total_tokens),
         "llm_tokens_by_category": {k: int(v) for k, v in by_cat_tokens.items()},
         "daily": sorted(({"day": k, "amount": round(v, 4)} for k, v in daily.items()),
                         key=lambda x: x["day"], reverse=True)[:14],
         "total": round(total, 4), "gaps": gaps},
        evidence=ctx.evidence(tables=["t2_cost_ledger", "trade_audit_log"]),
        sample_size=len(rows) + len(audit), severity="watch" if gaps else "info",
    )


# ── F54 自动仓位 sizing ─────────────────────────────────────────────────
@feature("F54")
def auto_sizing(ctx, target_vol: float = 0.15, risk_pct: float = 0.01,
                notional_base: float = 1000.0) -> FeatureResult:
    eq = _equity(ctx)
    rows = []
    for a in ctx.assets():
        bars = ctx.klines(a, "1d", 90)
        closes = [b["close"] for b in bars]
        if len(closes) < 30:
            continue
        vol = realized_vol(closes, 20)
        hit = None
        pnls = [float(d["forward_pnl"]) for d in ctx.decisions(limit=200)
                if (d.get("target_asset") or "").upper() == a.upper()
                and d.get("settled") == 1 and d.get("forward_pnl") is not None]
        if pnls:
            hit = len([p for p in pnls if p > 0]) / len(pnls)
        if vol is None or vol <= 0:
            continue
        vol_scale = target_vol / vol
        risk_units = (notional_base * risk_pct) / (vol * 2)  # 以 2σ 日波动为风险预算
        kelly = None
        if hit is not None and len(pnls) >= 20:
            payoff = (mean([p for p in pnls if p > 0]) or 0) / abs(mean([p for p in pnls if p < 0]) or 1)
            kelly = max(0.0, min(0.25, hit - (1 - hit) / payoff)) if payoff else None
        rows.append({"asset": a, "daily_vol_pct": round(vol * 100, 2),
                     "vol_target_scale": round(vol_scale, 3),
                     "risk_budget_notional": round(risk_units, 2),
                     "kelly_fraction": round(kelly, 3) if kelly is not None else None,
                     "sample_n": len(pnls)})
    if not rows:
        return FeatureResult.no_data(
            "F54", "仓位 sizing：无波动率序列", "各资产日线不足 30 根，算不出已实现波动",
            evidence=ctx.evidence(table="kline_bars"),
        )
    rows.sort(key=lambda r: r["daily_vol_pct"])
    equity_note = (f"账户权益 {eq['equity']:.2f} USDT（{eq['mode']}）" if eq["equity"] > 0
                   else "账户未配置初始权益，以下按每 1000 USDT 名义基准测算")
    return FeatureResult.ok(
        "F54", f"仓位 sizing：{len(rows)} 个资产已给出波动目标手数（{equity_note}）",
        {"rows": rows, "target_vol": target_vol, "risk_pct_per_trade": risk_pct,
         "notional_base": notional_base, "equity": eq,
         "method": ["波动率目标：target_vol/已实现波动", "风险预算：单笔风险=名义×risk_pct，除以 2σ",
                    "凯利分数：仅样本≥20 时给出，上限 25%"],
         "caveat": "凯利对胜率估计极敏感，样本不足时不给；建议取 vol_target 与 kelly 的较小值"},
        evidence=ctx.evidence(tables=["kline_bars", "ai_decisions", "t2_account"]),
        sample_size=len(rows),
    )


# ── F55 组合再平衡 ──────────────────────────────────────────────────────
@feature("F55")
def rebalance(ctx, drift_threshold_pct: float = 5.0, confirm: bool = False) -> FeatureResult:
    targets = store.get_state(ctx.conn, "target_weights")
    if isinstance(targets, str):
        try:
            import json
            targets = json.loads(targets)
        except Exception:
            targets = None
    base = ctx.account()
    equity = _num(base.get("equity")) or 0.0
    cash = _num(base.get("cash")) or 0.0
    pos = ctx.positions(status="open")
    value = {}
    for p in pos:
        px = ctx.price(p["asset"]) or _num(p.get("avg_price")) or 0.0
        val = (_num(p.get("qty")) or 0.0) * px
        if val:
            value[p["asset"]] = value.get(p["asset"], 0.0) + val
    total = sum(value.values()) + cash
    if not targets:
        equal = round(1.0 / max(len(value) + 1, 1), 4)
        return FeatureResult.ok(
            "F55", "组合再平衡：未配置目标权重，先给等权建议",
            {"configured": False, "proposed_equal_weight": equal,
             "current": {"positions": value, "cash": cash, "total": round(total, 2)},
             "how_to_set": "store.set_state(conn,'target_weights',{...})",
             "mode": "plan_only"},
            evidence=ctx.evidence(tables=["t2_account", "t2_positions"]),
            notes=["t2_state 无 target_weights；确认后按建议写入 target_weights",
                   "保护项：只输出计划，不自动降仓；确认后才生成 order 草稿（纸面）"],
        )
    plan = []
    for asset, wt in targets.items():
        tgt_val = total * float(wt)
        cur_val = value.get(asset, 0.0)
        drift = (cur_val - tgt_val) / total * 100 if total else None
        action = None
        if drift is not None and abs(drift) >= drift_threshold_pct:
            action = "trim" if drift > 0 else "add"
        if action == "trim":
            action = "trim_only_reduce"
        plan.append({"asset": asset, "target_w": float(wt), "current_value": round(cur_val, 2),
                     "target_value": round(tgt_val, 2), "drift_pct": round(drift, 2) if drift else None,
                     "action": action})
    triggered = [p for p in plan if p["action"]]
    if equity <= 0 and total <= 0:
        return FeatureResult.no_data(
            "F55", "组合再平衡：账户无权益", "t2_account 权益为 0 且无持仓，无法计算实际权重",
            evidence=ctx.evidence(tables=["t2_account", "t2_positions"]),
            value={"plan": plan},
        )
    return FeatureResult.ok(
        "F55", f"组合再平衡：{len(triggered)}/{len(plan)} 个资产偏离超 {drift_threshold_pct}%",
        {"plan": plan, "triggered": triggered, "total_value": round(total, 2),
         "mode": "plan_only", "confirmed": bool(confirm),
         "note": "保护项：只给计划与只减不加的方向，不落单"},
        evidence=ctx.evidence(tables=["t2_account", "t2_positions", "t2_state"]),
        sample_size=len(plan), severity="watch" if triggered else "info",
    )


# ── F56 现金也是仓位 ────────────────────────────────────────────────────
@feature("F56")
def cash_as_position(ctx, days: int = 30) -> FeatureResult:
    eq = _equity(ctx)
    pos_value = 0.0
    for p in ctx.positions(status="open"):
        px = ctx.price(p["asset"]) or _num(p.get("avg_price")) or 0.0
        pos_value += (_num(p.get("qty")) or 0.0) * px
    total = pos_value + eq["cash"]
    if total <= 0:
        return FeatureResult.no_data(
            "F56", "现金仓位：账户未初始化",
            "t2_account 现金与权益均为 0；需要先设定纸面初始资金，才能算现金占比与拖累",
            evidence=ctx.evidence(table="t2_account"),
        )
    cash_pct = eq["cash"] / total
    gaps = ["保证金利息 / 稳定币理财 / 货基收益率未接入账户接口，现金收益留空",
            "现金拖累需要基准收益率才能算，当前只给占比"]
    return FeatureResult.ok(
        "F56", f"现金仓位：现金占总资产 {cash_pct:.1%}（{eq['cash']:.2f}/{total:.2f} USDT）",
        {"cash": round(eq["cash"], 2), "positions_value": round(pos_value, 2),
         "total": round(total, 2), "cash_pct": round(cash_pct, 4),
         "cash_yield_pct": None, "drag_pct": None, "gaps": gaps,
         "note": "现金超过 50% 时会把策略收益稀释；portfolio 层面的收益应含现金收益"},
        evidence=ctx.evidence(tables=["t2_account", "t2_positions"]),
        severity="watch" if cash_pct > 0.5 else "info",
    )


# ── F57 成本批次与税 ────────────────────────────────────────────────────
@feature("F57")
def cost_lots(ctx, method: str = "fifo") -> FeatureResult:
    fills = [a for a in ctx.trade_audit(300)
             if (a.get("status") or "").upper() == "ORDER_PLACED"
             and (_num(a.get("filled_price")) or 0) > 0 and (_num(a.get("quantity")) or 0) > 0]
    if not fills:
        return FeatureResult.no_data(
            "F57", "成本批次：无成交明细",
            "trade_audit_log 中 ORDER_PLACED 且带 filled_price/quantity 的记录为 0",
            evidence=ctx.evidence(table="trade_audit_log"),
        )
    by_asset: Dict[str, List[Dict[str, Any]]] = {}
    for a in fills:
        ts = _ts(a.get("created_at")) or 0
        by_asset.setdefault((a.get("asset") or "?").upper(), []).append(
            {"ts": ts, "side": (a.get("side") or "").upper(),
             "qty": _num(a.get("quantity")), "px": _num(a.get("filled_price")),
             "fee_bps": None})
    out = []
    unrealized_unavailable = True
    for asset, lots in by_asset.items():
        lots.sort(key=lambda x: x["ts"])
        fifo_pnl, lifo_pnl = 0.0, 0.0
        inv_open: List[Dict[str, float]] = []
        realized_qty = 0.0
        for t in lots:
            if t["side"] == "BUY":
                inv_open.append({"qty": t["qty"], "px": t["px"]})
            elif inv_open:
                remain = t["qty"]
                # FIFO：先买先出
                tmp = [dict(i) for i in inv_open]
                while remain > 1e-12 and tmp:
                    first = tmp[0]
                    take = min(first["qty"], remain)
                    fifo_pnl += take * (t["px"] - first["px"])
                    first["qty"] -= take
                    remain -= take
                    if first["qty"] <= 1e-12:
                        tmp.pop(0)
                # LIFO：后买先出
                tmp2 = [dict(i) for i in inv_open]
                remain = t["qty"]
                while remain > 1e-12 and tmp2:
                    last = tmp2[-1]
                    take = min(last["qty"], remain)
                    lifo_pnl += take * (t["px"] - last["px"])
                    last["qty"] -= take
                    remain -= take
                    if last["qty"] <= 1e-12:
                        tmp2.pop()
                realized_qty += t["qty"]
                # 同步更新真实持仓队列（FIFO 视角）
                remain = t["qty"]
                while remain > 1e-12 and inv_open:
                    take = min(inv_open[0]["qty"], remain)
                    inv_open[0]["qty"] -= take
                    remain -= take
                    if inv_open[0]["qty"] <= 1e-12:
                        inv_open.pop(0)
        open_qty = sum(i["qty"] for i in inv_open)
        avg_cost = (sum(i["qty"] * i["px"] for i in inv_open) / open_qty) if open_qty else None
        out.append({"asset": asset, "fills": len(lots), "realized_qty": round(realized_qty, 8),
                    "fifo_realized_pnl": round(fifo_pnl, 4), "lifo_realized_pnl": round(lifo_pnl, 4),
                    "lot_method_delta": round(fifo_pnl - lifo_pnl, 4),
                    "open_qty": round(open_qty, 8),
                    "avg_cost_fifo": round(avg_cost, 4) if avg_cost else None})
    gaps = ["税负预估需要辖区税率与持仓期，未配置税率，暂不给金额",
            "未实现盈亏需要当前价与批次成本，只有 3 笔成交时参考意义有限"]
    return FeatureResult.ok(
        "F57", f"成本批次：{len(out)} 个资产已按 FIFO/LIFO 拆解（方法={method}）",
        {"assets": out, "method": method, "gaps": gaps,
         "note": "批次差异 = FIFO 已实现盈亏 − LIFO 已实现盈亏，用于决定先卖哪一批"},
        evidence=ctx.evidence(table="trade_audit_log"), sample_size=len(fills),
    )


# ── F58 子账户隔离 ──────────────────────────────────────────────────────
@feature("F58")
def sub_accounts(ctx, strategy_count: int = 3) -> FeatureResult:
    subs = ctx.subaccounts()
    eq = _equity(ctx)
    if not subs:
        per = round(1.0 / max(strategy_count, 1), 4)
        return FeatureResult.ok(
            "F58", "子账户隔离：尚未分账，先给按策略等分建议",
            {"configured": False,
             "suggested_split": {f"strategy_{i + 1}": per for i in range(strategy_count)},
             "equity": eq, "table": "t2_subaccounts(account_id,name,weight,capital,mode)"},
            evidence=ctx.evidence(tables=["t2_subaccounts", "t2_account"]),
            notes=["t2_subaccounts 为空；一个策略爆仓会拖垮全部资金，建议按策略分账"],
        )
    rows = []
    total_cap = 0.0
    for s in subs:
        cap = _num(s.get("capital")) or 0.0
        total_cap += cap
        rows.append({"account_id": s.get("account_id") or s.get("id"), "name": s.get("name"),
                     "weight": _num(s.get("weight")), "capital": cap,
                     "mode": s.get("mode"), "status": s.get("status")})
    max_share = max((r["capital"] for r in rows), default=0) / total_cap if total_cap else None
    return FeatureResult.ok(
        "F58", f"子账户：{len(rows)} 个账户，最大单账户占比 {max_share:.1%}"
        if max_share is not None else f"子账户：{len(rows)} 个账户（未填资金）",
        {"accounts": rows, "total_capital": round(total_cap, 2), "max_share": max_share,
         "note": "单账户占比 >40% 时隔离效果有限"},
        evidence=ctx.evidence(table="t2_subaccounts"), sample_size=len(rows),
        severity="watch" if max_share and max_share > 0.4 else "info",
    )
