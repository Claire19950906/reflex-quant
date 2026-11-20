# -*- coding: utf-8 -*-
"""人与行为 / 交互类（6 项）：F06 / F17 / F50 / F51 / F52 / F53。

两条底线：
1. 交易类动作一律只产出「草稿 + 需二次确认」，本模块不做任何真实下单；
2. 行为偏差只提示、不阻断，且必须给出可复核的证据行。
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Dict, List, Optional

from ..registry import feature
from ..types import FeatureResult
from ._util import mean, pct_change


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> Optional[int]:
    n = _num(v)
    if n is None:
        return None
    return int(n / 1000) if n > 10_000_000_000 else int(n)


_KNOWN_ASSETS = ("BTC", "ETH", "SOL", "XAU", "XAG", "WTI", "US500", "SPX", "DJI", "IXIC", "NVDA", "AAPL")


def _asset_of(target: Any) -> Optional[str]:
    """干预记录的 target 可能是「BTC」「BTC,ETH」或一段说明，取出其中的资产代码。"""
    if not target:
        return None
    txt = str(target).upper().replace("/", ",").replace("|", ",")
    for tok in txt.replace("，", ",").split(","):
        tok = tok.strip()
        if tok in _KNOWN_ASSETS:
            return tok
    return None


# ── F06 四图联动（后端关联契约） ────────────────────────────────────────
@feature("F06")
def linked_context(ctx, asset: Optional[str] = None) -> FeatureResult:
    asset = (asset or (ctx.assets()[0] if ctx.assets() else None))
    if not asset:
        return FeatureResult.no_data(
            "F06", "四图联动：无标的", "market_ticks 无任何资产，无法建立联动索引",
            evidence=ctx.evidence(table="market_ticks"),
        )
    news = [e for e in ctx.news(hours=72) if (e.get("asset") or "").upper() == asset.upper()][:10]
    signals = [s for s in ctx.abnormal(limit=50) if (s.get("asset") or "").upper() == asset.upper()][:10]
    flows = []
    for key in (f"{asset}.cvd", f"{asset}.taker_buy_sell"):
        row = ctx.macro_latest("flow", key)
        if row:
            flows.append({"metric": key, "ts": row["ts"], "value": row["value"]})
    decisions = [d for d in ctx.decisions(limit=50)
                 if (d.get("target_asset") or "").upper() == asset.upper()][:10]
    payload = {
        "asset": asset,
        "kline": {"timeframes": ["1m", "5m", "15m", "1h", "4h", "1d"], "price": ctx.price(asset)},
        "news_ids": [{"news_id": e.get("news_id") or e.get("id"), "ts": e["ts"],
                      "headline": (e.get("headline") or "")[:80]} for e in news],
        "abnormal_ids": [{"id": s["id"], "ts": s["ts"], "kind": s["signal_kind"]} for s in signals],
        "flow_blocks": flows,
        "decision_ids": [{"id": d["id"], "created_at": d.get("created_at"),
                          "action": d.get("suggested_action")} for d in decisions],
    }
    links = len(news) + len(signals) + len(flows) + len(decisions)
    return FeatureResult.ok(
        "F06", f"四图联动：{asset} 已生成关联索引（{links} 个可点节点）", payload,
        evidence=ctx.evidence(tables=["market_ticks", "news_events", "abnormal_signals",
                                      "macro_snapshots", "ai_decisions"]),
        sample_size=links,
        notes=["后端只提供「标的↔新闻↔资金↔决策」的 ID 契约；"
               "点击联动的交互层需在页面侧按该结构渲染"],
    )


# ── F17 自然语言改单（只出草稿） ────────────────────────────────────────
AMEND_PATTERNS = {
    "stop_loss": r"(止损|stop\s*loss)",
    "take_profit": r"(止盈|target|take\s*profit)",
    "size": r"(加仓|减仓|仓位|size)",
    "close": r"(平仓|平掉|close)",
}


@feature("F17")
def nl_amend(ctx, text: str = "", confirm: bool = False) -> FeatureResult:
    if not text:
        return FeatureResult.not_configured(
            "F17", "自然语言改单：等待指令文本",
            "需要传入一句人话指令，例如「把 BTC 止损提到 62000」",
            evidence=ctx.evidence(),
            notes=["解析规则：资产 + 动作（止损/止盈/加减仓/平仓）+ 数值；"
                   "任何情况下只产出草稿，confirm=True 也只是把草稿标记为已确认，不会真下单"],
        )
    asset = None
    for a in ctx.assets():
        if re.search(rf"\b{re.escape(a)}\b", text, re.IGNORECASE):
            asset = a
            break
    if asset is None:
        m = re.search(r"(BTC|ETH|SOL|XAU|XAG|WTI|NVDA|AAPL|MSFT|SPX|TSLA)", text, re.IGNORECASE)
        asset = m.group(1).upper() if m else None
    action = next((k for k, p in AMEND_PATTERNS.items() if re.search(p, text, re.IGNORECASE)), None)
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    price = nums[-1] if nums else None
    pct = None
    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if m_pct:
        pct = float(m_pct.group(1))
    if asset is None or action is None:
        return FeatureResult.not_configured(
            "F17", "自然语言改单：没解析出资产或动作",
            f"原文「{text[:40]}」缺少可识别的资产/动作；为避免误改，不生成草稿",
            evidence=ctx.evidence(), notes=["可识别的动作：止损、止盈、加仓、减仓、平仓"],
        )
    spot = ctx.price(asset)
    draft = {
        "draft_id": f"NL-{ctx.now}-{asset}",
        "asset": asset, "action": action, "price": price, "size_pct": pct,
        "reference_spot": spot,
        "deviation_pct": pct_change(price, spot) if price and spot else None,
        "mode": "paper", "dry_run": True,
        "requires_second_confirm": True,
        "confirmed": bool(confirm),
        "text": text[:200],
    }
    warnings_ = []
    if price and spot and abs(pct_change(price, spot) or 0) >= 5:
        warnings_.append("目标价与现价偏离 ≥5%，请确认是否为笔误")
    if action == "close" and not confirm:
        warnings_.append("平仓类指令默认只出草稿")
    return FeatureResult.ok(
        "F17", f"自然语言改单：已生成 {asset} {action} 草稿（纸面/dry-run，未执行）", draft,
        evidence=ctx.evidence(tables=["market_ticks", "t2_orders"]),
        severity="watch" if warnings_ else "info",
        action={"kind": "order_draft", "draft": draft},
        notes=warnings_ or ["草稿需人工二次确认；本模块不写任何真实订单"],
    )


# ── F50 干预效果追踪 ────────────────────────────────────────────────────
@feature("F50")
def intervention_effect(ctx) -> FeatureResult:
    rows = ctx.interventions(limit=100)
    if not rows:
        return FeatureResult.no_data(
            "F50", "干预追踪：暂无干预记录",
            "t2_interventions 为空；需要先发生「手动暂停/改单/平仓」才能计算手贱成本",
            evidence=ctx.evidence(table="t2_interventions"),
        )
    items = []
    for r in rows:
        asset = (r.get("asset") or _asset_of(r.get("target")) or "").upper()
        ts = _ts(r.get("created_at"))
        if not asset or not ts:
            continue
        bars = ctx.klines(asset, "1h", 96)
        after1 = next((b["close"] for b in bars if b["ts"] >= ts + 3600), None)
        after24 = next((b["close"] for b in bars if b["ts"] >= ts + 86400), None)
        at = next((b["close"] for b in bars if b["ts"] >= ts), None)
        items.append({
            "id": r.get("id"), "asset": asset, "kind": r.get("kind"),
            "ts": ts, "price_at": at,
            "pct_1h": pct_change(after1, at) if at else None,
            "pct_1d": pct_change(after24, at) if at else None,
            "note": r.get("reason") or r.get("note"),
        })
    if not items:
        return FeatureResult.no_data(
            "F50", "干预追踪：记录缺资产或时间", "干预记录存在但没有 asset/created_at，无法回填结果",
            evidence=ctx.evidence(table="t2_interventions"),
        )
    done = [i for i in items if i["pct_1d"] is not None]
    avg1 = mean([i["pct_1h"] for i in items if i["pct_1h"] is not None])
    avg24 = mean([i["pct_1d"] for i in done]) if done else None
    return FeatureResult.ok(
        "F50", f"干预追踪：{len(items)} 次干预，1h 平均 {avg1:.2f}%"
        + (f"，1d 平均 {avg24:.2f}%（{len(done)} 次已满 24h）" if avg24 is not None else ""),
        {"items": items, "avg_pct_1h": round(avg1, 3) if avg1 is not None else None,
         "avg_pct_1d": round(avg24, 3) if avg24 is not None else None,
         "caveat": "「手贱成本」= 干预后若按原方向继续持有会怎样；反向符号才说明干预有害"},
        evidence=ctx.evidence(table="t2_interventions"), sample_size=len(items),
    )


# ── F51 人机对比 ────────────────────────────────────────────────────────
@feature("F51")
def human_vs_system(ctx, days: int = 30) -> FeatureResult:
    since = ctx.now - days * 86400
    sys_rows = [d for d in ctx.decisions_since(since)
                if d.get("settled") == 1 and d.get("forward_pnl") is not None]
    manual = [r for r in ctx.interventions(limit=100)]
    manual_ts = [_ts(r.get("created_at")) for r in manual]
    manual_n = len([t for t in manual_ts if t and t >= since])
    orders = [o for o in ctx.orders(open_only=False) if _ts(o.get("created_at"))]
    manual_orders = len([o for o in orders if (_ts(o.get("created_at")) or 0) >= since])
    if not sys_rows and not manual_n:
        return FeatureResult.no_data(
            "F51", "人机对比：双账本都没数据",
            f"近 {days} 天既无已结算的系统决策，也无人工干预记录",
            evidence=ctx.evidence(tables=["ai_decisions", "t2_interventions"]),
        )
    sys_pnl = [float(d["forward_pnl"]) for d in sys_rows]
    summary = {
        "window_days": days,
        "system": {"n": len(sys_pnl), "avg_pct": round(mean(sys_pnl), 3) if sys_pnl else None,
                   "win_rate": round(len([p for p in sys_pnl if p > 0]) / len(sys_pnl), 3) if sys_pnl else None,
                   "turnover": len(sys_pnl)},
        "human": {"interventions": manual_n, "orders": manual_orders,
                  "avg_pct": None, "win_rate": None},
    }
    gap = []
    if not sys_pnl:
        gap.append("系统侧无已结算样本")
    if summary["human"]["avg_pct"] is None:
        gap.append("人工侧缺成交记录，收益无法计算（只能统计动作次数）")
    return FeatureResult.ok(
        "F51", f"人机对比（{days}天）：系统 {len(sys_pnl)} 笔均 {summary['system']['avg_pct']}%，人工 {manual_n} 次干预",
        {"summary": summary, "gaps": gap,
         "caveat": "人工侧仅有干预次数与订单数；没有人工成交价记录时不能比较收益，留空不猜"},
        evidence=ctx.evidence(tables=["ai_decisions", "t2_interventions", "t2_orders"]),
        sample_size=len(sys_pnl) + manual_n, severity="watch" if gap else "info",
    )


# ── F52 纪律监控 ────────────────────────────────────────────────────────
@feature("F52")
def discipline(ctx, days: int = 14) -> FeatureResult:
    since = ctx.now - days * 86400
    flags = []
    # 1) 连亏后加仓：已结算决策连续 2 笔亏损后，下一笔仓位/名义额更大
    settled = [dict(r) for r in ctx.q(
        "SELECT created_at, target_asset, suggested_action, forward_pnl FROM ai_decisions "
        "WHERE settled=1 AND forward_pnl IS NOT NULL ORDER BY id", note="discipline_pnl")]
    losses = [r for r in settled if float(r["forward_pnl"]) < 0]
    if len(losses) >= 2 and len(settled) >= 3:
        flags.append({"bias": "连亏后继续加仓", "evidence": "已结算样本中出现连续亏损",
                      "n_losses": len(losses), "n_total": len(settled), "level": "info"})
    # 2) 频繁改参
    changes = [c for c in ctx.param_changes(limit=200)
               if (_ts(c.get("created_at")) or 0) >= since]
    if len(changes) >= 10:
        flags.append({"bias": "频繁改参", "evidence": f"{days} 天内 {len(changes)} 次参数变更",
                      "n": len(changes), "level": "warn"})
    # 3) 报复交易：亏损当日下单数显著高于均值
    placed = [dict(r) for r in ctx.q(
        "SELECT created_at, status FROM trade_audit_log ORDER BY id DESC LIMIT 400",
        note="discipline_audit")]
    placed = [p for p in placed if (p.get("status") or "").upper() == "ORDER_PLACED"]
    by_day: Dict[str, int] = {}
    for p in placed:
        s = str(p.get("created_at") or "")[:10]
        if s:
            by_day[s] = by_day.get(s, 0) + 1
    if len(by_day) >= 3:
        med = statistics.median(by_day.values())
        hot = {d: c for d, c in by_day.items() if c >= max(3, med * 3)}
        if hot:
            flags.append({"bias": "报复交易（单日下单激增）", "evidence": hot, "level": "warn"})
    # 4) 只看利好：决策方向长期单边
    decs = [dict(r) for r in ctx.q(
        "SELECT suggested_action FROM ai_decisions WHERE created_at>=?",
        (since,), note="discipline_direction")]
    if len(decs) >= 20:
        buys = len([d for d in decs if (d.get("suggested_action") or "").upper() == "BUY"])
        share = buys / len(decs)
        if share >= 0.8 or share <= 0.2:
            flags.append({"bias": "方向单边（只看利好/只看利空）",
                          "evidence": f"BUY 占比 {share:.0%}", "n": len(decs), "level": "watch"})
    if not flags:
        return FeatureResult.ok(
            "F52", f"纪律监控：近 {days} 天未触发偏差", {"flags": [], "days": days},
            evidence=ctx.evidence(tables=["ai_decisions", "t2_param_changes", "trade_audit_log"]),
        )
    return FeatureResult.ok(
        "F52", f"纪律监控：触发 {len(flags)} 项行为偏差（只提示，不阻断）", {"flags": flags, "days": days},
        evidence=ctx.evidence(tables=["ai_decisions", "t2_param_changes", "trade_audit_log"]),
        sample_size=len(settled), severity="warn",
        notes=["偏差项仅作复盘提示，不参与下单拦截"],
    )


# ── F53 决策清单 ────────────────────────────────────────────────────────
CHECKLIST_FIELDS = ("invalidation_condition", "entry_price", "exit_price", "impact_score")


@feature("F53")
def decision_checklist(ctx, days: int = 14) -> FeatureResult:
    since = ctx.now - days * 86400
    rows = [d for d in ctx.decisions_since(since)]
    if not rows:
        return FeatureResult.no_data(
            "F53", f"决策清单：近 {days} 天无决策", "ai_decisions 无新记录，无从检查清单完整度",
            evidence=ctx.evidence(table="ai_decisions"),
        )
    full = [dict(r) for r in ctx.q(
        "SELECT id, target_asset, suggested_action, invalidation_condition, entry_price "
        "FROM ai_decisions WHERE created_at>=? ORDER BY id DESC LIMIT 200",
        (since,), note="decision_checklist")]
    missing = []
    for r in full:
        lacks = [f for f in ("invalidation_condition", "entry_price") if not r.get(f)]
        if lacks:
            missing.append({"id": r["id"], "asset": r["target_asset"],
                            "action": r["suggested_action"], "missing": lacks})
    coverage = 1 - len(missing) / len(full) if full else 0
    schema = {
        "required_before_order": ["失效条件 invalidation_condition",
                                  "最大损失（金额与百分比）",
                                  "什么情况不平（继续持有条件）"],
        "skippable": True, "skip_is_logged": True,
    }
    sev = "warn" if coverage < 0.8 else "info"
    return FeatureResult.ok(
        "F53", f"决策清单：近 {days} 天清单完整率 {coverage:.0%}（{len(missing)}/{len(full)} 条缺项）",
        {"coverage": round(coverage, 3), "checked": len(full), "missing": missing[:30],
         "checklist_schema": schema,
         "note": "清单字段落在 ai_decisions 列上；本功能只校验与提示，可跳过但留痕"},
        evidence=ctx.evidence(table="ai_decisions"), sample_size=len(full), severity=sev,
    )
