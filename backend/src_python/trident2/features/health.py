# -*- coding: utf-8 -*-
"""系统健康类：F02 / F03 / F35 / F45 / F46 / F47 / F63 / F64。"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from ..registry import feature
from ..types import FeatureResult
from ._util import mean, sharpe, stdev

MAX_TICK_AGE_SEC = 300
STALE_ORDER_SEC = 4 * 3600


def _check(name: str, status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


def _worst(checks: List[Dict[str, Any]]) -> str:
    order = {"fail": 3, "warn": 2, "no_data": 1, "pass": 0}
    worst = "pass"
    for c in checks:
        if order.get(c["status"], 0) > order[worst]:
            worst = c["status"]
    return worst


# ── F02 改动审计 ────────────────────────────────────────────────────────
@feature("F02")
def change_audit(ctx) -> FeatureResult:
    changes = ctx.param_changes(limit=30)
    if not changes:
        return FeatureResult.no_data(
            "F02", "改动审计：无改动记录",
            "t2_param_changes 为空，先通过 POST /api/v2/param-changes 记录一次参数/策略/开关变更",
            evidence=ctx.evidence(table="t2_param_changes"),
        )
    evaluated, pending = [], 0
    for ch in changes:
        age = ctx.now - int(ch["ts"])
        if age < 86400:
            pending += 1
            continue
        pre = [d for d in ctx.decisions_since(int(ch["ts"]) - 86400) if int(d["created_at"]) < int(ch["ts"])]
        post = [d for d in ctx.decisions_since(int(ch["ts"])) if int(d["created_at"]) <= int(ch["ts"]) + 86400]
        pre_n = [d["forward_pnl"] for d in pre if d.get("forward_pnl") is not None and d.get("settled")]
        post_n = [d["forward_pnl"] for d in post if d.get("forward_pnl") is not None and d.get("settled")]
        if len(pre_n) < 5 or len(post_n) < 5:
            evaluated.append({
                "id": ch["id"], "scope": ch["scope"], "key": ch["key"], "ts": ch["ts"],
                "status": "insufficient_sample", "pre_n": len(pre_n), "post_n": len(post_n),
            })
            continue
        evaluated.append({
            "id": ch["id"], "scope": ch["scope"], "key": ch["key"], "ts": ch["ts"],
            "status": "measured",
            "pre_mean_pnl": mean(pre_n), "post_mean_pnl": mean(post_n),
            "delta": mean(post_n) - mean(pre_n), "pre_n": len(pre_n), "post_n": len(post_n),
        })
    if not evaluated:
        # 有改动但都还在 24h 观察窗内：这是「观察中」，不是「没数据」
        return FeatureResult.ok(
            "F02", f"改动审计：{len(changes)} 条改动均在观察窗内（满 24h 后自动对比）",
            {"changes": [{"id": c["id"], "scope": c["scope"], "key": c["key"], "ts": c["ts"],
                          "status": "observation_window", "age_hours": round((ctx.now - int(c["ts"])) / 3600, 1)}
                         for c in changes],
             "pending": pending, "window_open": True,
             "note": "改动后前后 24h 的 settled 决策各满 5 条才会给出 delta，不提前下结论"},
            evidence=ctx.evidence(table="t2_param_changes", pending=pending),
            sample_size=len(changes), severity="info",
        )
    measured = [e for e in evaluated if e["status"] == "measured"]
    worst_delta = min((e["delta"] for e in measured), default=None)
    severity = "info"
    if worst_delta is not None and worst_delta < -0.5:
        severity = "warn"
    headline = f"改动审计：{len(measured)} 条已评估 / {pending} 条待观察"
    return FeatureResult.ok(
        "F02", headline,
        {"changes": evaluated, "pending": pending, "worst_delta_pp": worst_delta},
        evidence=ctx.evidence(table="t2_param_changes", window="pre24h/post24h", samples=len(evaluated)),
        sample_size=len(measured), severity=severity,
        action={"kind": "review_change", "ids": [e["id"] for e in measured]} if severity != "info" else None,
    )


# ── F03 盘前 60 秒体检 ──────────────────────────────────────────────────
@feature("F03")
def preflight(ctx) -> FeatureResult:
    checks: List[Dict[str, Any]] = []

    assets = ctx.assets()
    ages = []
    for a in assets[:40]:
        if ctx.has_table("market_ticks"):
            lvl = ctx.price_age_sec(a)
            if lvl is not None:
                ages.append(lvl)
    if ages:
        worst_age = max(ages)
        status = "fail" if worst_age > MAX_TICK_AGE_SEC * 3 else ("warn" if worst_age > MAX_TICK_AGE_SEC else "pass")
        checks.append(_check("行情新鲜度", status, f"最新行情距现在 {worst_age}s（阈值 {MAX_TICK_AGE_SEC}s）",
                             worst_age_sec=worst_age))
    else:
        checks.append(_check("行情新鲜度", "no_data", "market_ticks 无可用价格"))

    src = ctx.source_rows()
    if src:
        bad = [s for s in src if s.get("status") != "online" or (s.get("consecutive_failures") or 0) >= 3]
        share = len(bad) / len(src)
        status = "fail" if share >= 0.3 else ("warn" if bad else "pass")
        checks.append(_check("数据源存活", status,
                             f"{len(src)} 个源中 {len(bad)} 个异常（{share:.0%}）",
                             failing=[s["source_id"] for s in bad[:10]]))
    else:
        checks.append(_check("数据源存活", "no_data", "source_status 无记录"))

    trade_key = bool(os.environ.get("BINANCE_API_KEY", "").strip())
    manager_on = str(os.environ.get("TRADING_MANAGER_ENABLED", "")).lower() in ("1", "true", "yes", "on")
    if not trade_key:
        checks.append(_check("券商/交易所连接", "warn",
                             "未配置交易密钥，当前为纸面/只读模式（不会真实下单）"))
    else:
        checks.append(_check("券商/交易所连接", "pass" if manager_on else "warn",
                             "已配置密钥；" + ("交易进程已启用" if manager_on else "交易进程仍 fail-closed")))

    acc = ctx.account()
    equity = acc.get("equity")
    if not acc or not equity:
        checks.append(_check("保证金/权益", "no_data", "纸面账户权益为 0，未注资"))
    else:
        checks.append(_check("保证金/权益", "pass", f"纸面权益 {equity}", equity=equity))

    stale = [o for o in ctx.orders(open_only=True)
             if ctx.now - int(o["ts"]) > STALE_ORDER_SEC]
    if ctx.orders(open_only=True):
        checks.append(_check("挂单残留", "warn" if stale else "pass",
                             f"{len(stale)} 张挂单超过 {STALE_ORDER_SEC // 3600}h 未处理",
                             stale_ids=[o["id"] for o in stale[:10]]))
    else:
        checks.append(_check("挂单残留", "pass", "无未完成挂单"))

    latest_ts = max([int(t["ts"]) for t in [ctx.latest_tick(a) for a in assets[:20]] if t] or [0])
    if latest_ts:
        skew = ctx.now - latest_ts
        checks.append(_check("时钟偏差", "pass" if abs(skew) < 600 else "warn",
                             f"本机与最新行情时间戳相差 {skew}s（以行情为参照，非独立时钟源）",
                             skew_sec=skew))
    else:
        checks.append(_check("时钟偏差", "no_data", "无行情时间戳可作参照"))

    ready = not any(c["status"] == "fail" for c in checks)
    value = {"ready": ready, "checks": checks}
    severity = "warn" if any(c["status"] == "fail" for c in checks) else "info"
    headline = "盘前体检：通过" if ready else "盘前体检：存在阻断项"
    return FeatureResult.ok(
        "F03", headline, value,
        evidence=ctx.evidence(tables=["market_ticks", "source_status", "t2_account", "t2_orders"]),
        sample_size=len(checks), severity=severity, anchor_ts=latest_ts or None,
        action={"kind": "preflight_block"} if not ready else None,
    )


# ── F35 告警分级与冷却 ──────────────────────────────────────────────────
@feature("F35")
def alert_governance(ctx) -> FeatureResult:
    rows = ctx.q(
        "SELECT level, COUNT(*) n, COUNT(DISTINCT feature_id) f FROM t2_alerts "
        "WHERE created_at>=? GROUP BY level", (ctx.now - 86400,), note="t2_alerts",
    )
    counts = {r["level"]: r["n"] for r in rows}
    top = ctx.q(
        "SELECT feature_id, COUNT(*) n FROM t2_alerts WHERE created_at>=? "
        "GROUP BY feature_id ORDER BY n DESC LIMIT 5", (ctx.now - 86400,), note="t2_alerts_top",
    )
    cooldowns = {"P1": 600, "P2": 1800, "P3": 1800}
    value = {
        "last_24h": {"P1": counts.get("P1", 0), "P2": counts.get("P2", 0), "P3": counts.get("P3", 0)},
        "cooldown_sec": cooldowns,
        "noisiest": [{"feature_id": r["feature_id"], "count": r["n"]} for r in top],
    }
    p1 = counts.get("P1", 0)
    severity = "warn" if p1 > 10 else "info"
    return FeatureResult.ok(
        "F35", f"告警分级：24h 内 P1 {p1} / P2 {counts.get('P2', 0)} / P3 {counts.get('P3', 0)}",
        value, evidence=ctx.evidence(table="t2_alerts", window="24h"), sample_size=sum(counts.values()),
        severity=severity,
    )


# ── F45 策略健康度 ──────────────────────────────────────────────────────
@feature("F45")
def strategy_health(ctx) -> FeatureResult:
    # v404 隔离：绩效口径只认真实纸面成交（quick_sim_trades）。
    # 历史回放（t2_sim_trades）样本再大也不算绩效 —— 它可以反复重跑、可以调参重选，
    # 混进来就等于用一个能刷的样本池撑起"胜率"。
    live = [t for t in ctx.live_trades(limit=500) if t.get("pnl_pct") is not None]
    replay_n = ctx.replay_count()
    trades, source = live, "quick_sim_trades(纸面实跑)"
    if len(trades) < 20:
        return FeatureResult.no_data(
            "F45", f"策略健康度：真实成交仅 {len(trades)} 笔已结算",
            f"样本 < 20，滚动 Sharpe / 胜率都不构成结论（AGENTS.md：有效样本 <60 不构成结论）；"
            f"历史回放 {replay_n} 条已隔离，不进入绩效口径",
            evidence=ctx.evidence(table="quick_sim_trades", sample_size=len(trades), source=source),
        )
    pnls = [float(t["pnl_pct"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    payoff = (mean(wins) / abs(mean(losses))) if wins and losses and mean(losses) else None
    by_strategy: Dict[str, List[float]] = {}
    for t in trades:
        key = str(t.get("strategy_id") or t.get("source") or "unassigned")
        by_strategy.setdefault(key, []).append(float(t["pnl_pct"]))
    by_src: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        s = by_src.setdefault(str(t.get("source") or "quick_sim"), {"n": 0, "pnl_sum": 0.0, "wins": 0})
        s["n"] += 1
        s["pnl_sum"] += float(t["pnl_pct"])
        if float(t["pnl_pct"]) > 0:
            s["wins"] += 1
    per = []
    for sid, vals in by_strategy.items():
        per.append({"strategy_id": sid, "n": len(vals), "mean_pnl_pct": mean(vals),
                    "win_rate": len([v for v in vals if v > 0]) / len(vals)})
    gross = [float(t["pnl_pct"]) + (float(t.get("cost_bps") or 0) / 100.0) for t in trades]
    value = {
        "n_settled": len(trades),
        "data_class": "live",
        "source": source,
        "win_rate": len(wins) / len(trades),
        "avg_pnl_pct": mean(pnls),
        "avg_gross_pnl_pct": mean(gross),
        "gross_win_rate": len([g for g in gross if g > 0]) / len(gross),
        "cost_pct_per_trade": mean([float(t.get("cost_bps") or 0) / 100.0 for t in trades]),
        "payoff_ratio": payoff,
        "sharpe_annual": sharpe(pnls, 252),
        "stdev_pnl": stdev(pnls),
        "by_strategy": per,
        "by_source": {k: {"n": v["n"], "avg_pnl_pct": round(v["pnl_sum"] / v["n"], 3),
                          "win_rate": round(v["wins"] / v["n"], 3)} for k, v in by_src.items()},
        "caveat": "样本只含真实纸面成交（quick_sim_trades，已结算）；历史回放（t2_sim_trades）"
                  "已隔离，不参与胜率 / Sharpe / 累计盈亏。毛收益（未扣成本）与净收益分开看："
                  "毛≈0 而净显著为负，说明是成本吃掉而非方向错",
    }
    severity = "warn" if (value["win_rate"] < 0.4 and len(trades) >= 60) else "info"
    return FeatureResult.ok("F45", f"策略健康度：{len(trades)} 笔，胜率 {value['win_rate']:.0%}（{source}）", value,
                            evidence=ctx.evidence(table="quick_sim_trades",
                                                  source=source), sample_size=len(trades),
                            severity=severity)


# ── F46 执行链路监控 ────────────────────────────────────────────────────
@feature("F46")
def execution_chain(ctx) -> FeatureResult:
    rows = ctx.trade_audit(limit=500)
    if not rows:
        return FeatureResult.no_data(
            "F46", "执行链路：无下单审计记录",
            "trade_audit_log 为空；纸面模式下先通过 /api/v2/paper/orders 产生订单",
            evidence=ctx.evidence(table="trade_audit_log"),
        )
    total = len(rows)
    by_status: Dict[str, int] = {}
    by_mode: Dict[str, int] = {}
    for r in rows:
        by_status[r.get("status") or "unknown"] = by_status.get(r.get("status") or "unknown", 0) + 1
        by_mode[r.get("mode") or "unknown"] = by_mode.get(r.get("mode") or "unknown", 0) + 1
    filled = by_status.get("ORDER_PLACED", 0)
    skipped = by_status.get("ADAPTER_SKIPPED", 0)
    rejected = by_status.get("RISK_REJECTED", 0)
    pending_orders = ctx.orders(open_only=True)
    stale = [o for o in pending_orders if ctx.now - int(o["ts"]) > STALE_ORDER_SEC]
    fill_rate = filled / total if total else None
    value = {
        "n_orders": total,
        "by_status": by_status,
        "by_mode": by_mode,
        "fill_rate": fill_rate,
        "risk_reject_rate": rejected / total if total else None,
        "adapter_skip_rate": skipped / total if total else None,
        "pending_orders": len(pending_orders),
        "stale_orders": len(stale),
    }
    severity = "warn" if skipped / total > 0.5 else ("watch" if stale else "info")
    return FeatureResult.ok(
        "F46", f"执行链路：{total} 单，成交 {filled} / 风控拒 {rejected} / 适配器跳过 {skipped}", value,
        evidence=ctx.evidence(table="trade_audit_log", mode="paper/mock"), sample_size=total,
        severity=severity,
        action={"kind": "check_adapter"} if severity != "info" else None,
    )


# ── F47 数据源体检 ──────────────────────────────────────────────────────
@feature("F47")
def source_checkup(ctx) -> FeatureResult:
    assets = ctx.assets()
    cross = []
    for a in assets:
        srcs = ctx.tick_sources(a, lookback_sec=900)
        if len(srcs) < 2:
            continue
        prices = [s["price"] for s in srcs]
        mid = sorted(prices)[len(prices) // 2]
        if not mid:
            continue
        dev_bp = max(abs(p - mid) / mid * 10000 for p in prices)
        cross.append({"asset": a, "n_sources": len(srcs), "max_dev_bp": dev_bp,
                      "sources": [s["source"] for s in srcs]})
    stale_ind = [r for r in ctx.indicator_rows(lookback_hours=48)
                 if str(r.get("status") or "").lower() not in ("ok", "fresh", "healthy")]
    flags = ctx.quality_flags(lookback_hours=24)
    if not cross and not stale_ind and not flags:
        return FeatureResult.no_data(
            "F47", "数据源体检：无多源同刻价格可比对",
            "9 分钟内没有同一资产的两个及以上源价格，无法做互校",
            evidence=ctx.evidence(tables=["market_ticks", "indicator_freshness", "data_quality_flags"]),
        )
    bad = [c for c in cross if c["max_dev_bp"] > 20]
    value = {
        "crosschecked_assets": cross,
        "max_dev_bp": max((c["max_dev_bp"] for c in cross), default=None),
        "divergent_assets": [c["asset"] for c in bad],
        "stale_indicators": len(stale_ind),
        "quality_flags_24h": len(flags),
    }
    severity = "warn" if bad else ("watch" if stale_ind else "info")
    return FeatureResult.ok(
        "F47", f"数据源体检：{len(cross)} 个资产多源互校，{len(bad)} 个偏差 >20bp", value,
        evidence=ctx.evidence(tables=["market_ticks", "indicator_freshness", "data_quality_flags"]),
        sample_size=len(cross), severity=severity,
        action={"kind": "degrade_source", "assets": [c["asset"] for c in bad]} if bad else None,
    )


# ── F63 灾备降级 ────────────────────────────────────────────────────────
@feature("F63")
def failover(ctx) -> FeatureResult:
    src = ctx.source_rows()
    feeds = ctx.feed_rows(lookback_sec=900)
    if not src and not feeds:
        return FeatureResult.no_data(
            "F63", "灾备降级：无源健康数据",
            "source_status / feed_health 均为空，无法判断是否需要降级",
            evidence=ctx.evidence(tables=["source_status", "feed_health"]),
        )
    failing = [s for s in src if s.get("status") != "online" or (s.get("consecutive_failures") or 0) >= 3]
    err_feeds = [f for f in feeds if str(f.get("status") or "").lower() not in ("ok", "online", "success")]
    total_src = len(src) or 1
    fail_share = len(failing) / total_src
    if fail_share >= 0.5 or len(err_feeds) >= 20:
        mode = "read_only"
    elif failing or err_feeds:
        mode = "fallback"
    else:
        mode = "normal"
    value = {
        "recommended_mode": mode,
        "failing_sources": [s["source_id"] for s in failing[:15]],
        "fail_share": fail_share,
        "feed_errors_15m": len(err_feeds),
        "guard": "fail-closed：read_only 模式下新单不生成，只保留只读行情与复盘",
    }
    severity = "critical" if mode == "read_only" else ("warn" if mode == "fallback" else "info")
    return FeatureResult.ok(
        "F63", f"灾备降级：建议 {mode}（{len(failing)}/{len(src)} 源异常）", value,
        evidence=ctx.evidence(tables=["source_status", "feed_health"], window="15m"),
        sample_size=len(src) + len(feeds), severity=severity,
        action={"kind": "set_mode", "mode": mode} if mode != "normal" else None,
    )


# ── F64 密钥与权限 ──────────────────────────────────────────────────────
@feature("F64")
def key_permissions(ctx) -> FeatureResult:
    names = [
        "BINANCE_API_KEY", "BINANCE_API_SECRET", "OKX_API_KEY", "BYBIT_API_KEY",
        "OPENROUTER_API_KEY", "FRED_API_KEY", "COINGLASS_API_KEY", "FEISHU_WEBHOOK_URL",
    ]
    presence = {n: bool(os.environ.get(n, "").strip()) for n in names}
    trade_key = presence["BINANCE_API_KEY"]
    manager_on = str(os.environ.get("TRADING_MANAGER_ENABLED", "")).lower() in ("1", "true", "yes", "on")
    checks = []
    checks.append(_check("交易密钥与只读密钥分离", "warn" if trade_key else "pass",
                         "检测到交易密钥；请确认下单 key 未用于行情/数据拉取" if trade_key
                         else "未配置交易密钥，当前只读/纸面"))
    checks.append(_check("交易进程开关", "pass" if not manager_on else "warn",
                         "TRADING_MANAGER_ENABLED=" + ("on（可真实下单）" if manager_on else "off（fail-closed）")))
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    if os.path.exists(env_path):
        days = (time.time() - os.path.getmtime(env_path)) / 86400
        checks.append(_check("密钥轮换", "warn" if days > 180 else "pass",
                             f".env 最近修改距今 {days:.0f} 天（>180 天建议轮换）", age_days=days))
    else:
        checks.append(_check("密钥轮换", "no_data", ".env 文件不存在，无法判断轮换周期"))
    checks.append(_check("IP 白名单", "no_data", "需在交易所后台确认；本地无法读取白名单状态"))
    checks.append(_check("数据源密钥覆盖", "pass" if sum(1 for k, v in presence.items() if k not in ("BINANCE_API_KEY", "BINANCE_API_SECRET") and v) >= 2 else "warn",
                         f"{sum(1 for v in presence.values() if v)}/{len(presence)} 项已配置（不展示具体值）"))
    value = {"configured": presence, "checks": checks,
             "ready": not any(c["status"] == "fail" for c in checks)}
    severity = "warn" if trade_key and manager_on else "info"
    return FeatureResult.ok(
        "F64", f"密钥与权限：{sum(1 for v in presence.values() if v)} 项已配置，"
               f"交易开关 {'ON' if manager_on else 'OFF'}", value,
        evidence=ctx.evidence(source="os.environ + .env mtime（不落密钥值）"), sample_size=len(checks),
        severity=severity,
    )
