# -*- coding: utf-8 -*-
"""研究复盘类（10 项）：F01 / F11 / F12 / F13 / F15 / F16 / F19 / F33 / F48 / F49。

这些功能的价值在于「可验证」：能算就给出数字与样本量，样本不足就说不足，
绝不把三两笔样本包装成结论。
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

from .. import store
from ..registry import feature
from ..types import FeatureResult
from ._util import correlation, mean, pct_change, realized_vol, sharpe, stdev

MIN_TRAITS = 5


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _phi_inv(p: float) -> float:
    """标准正态分位（Acklam 近似，够用）。"""
    if p <= 0 or p >= 1:
        return float("nan")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _hour_bars(ctx, asset: str, start: int, end: int) -> List[Dict[str, Any]]:
    """按秒级窗口取 1h K 线（底表 ts 是毫秒，这里统一成秒）。"""
    rows = ctx.q(
        "SELECT ts, close, high, low, volume FROM kline_bars "
        "WHERE symbol=? AND timeframe='1h' AND ts>=? AND ts<=? ORDER BY ts",
        (asset, start * 1000, end * 1000), note=f"hour_bars:{asset}",
    )
    out = []
    for r in rows:
        ts = int(r["ts"])
        if ts > 10_000_000_000:
            ts //= 1000
        out.append({"ts": ts, "close": r["close"], "high": r["high"],
                    "low": r["low"], "volume": r["volume"]})
    return out


# ── F01 影响回填 ────────────────────────────────────────────────────────
@feature("F01")
def impact_backfill(ctx, capture: bool = True, window_hours: int = 72,
                    limit: int = 200) -> FeatureResult:
    events = [e for e in ctx.news(hours=window_hours)
              if e.get("asset") and str(e["asset"]).upper() in ctx.kline_symbols()][:limit]
    if not events:
        return FeatureResult.no_data(
            "F01", "影响回填：窗口内无带标的的新闻",
            f"最近 {window_hours}h 的 news_events 里没有 asset 字段非空的记录",
            evidence=ctx.evidence(tables=["news_events"]),
        )
    captured = 0
    for e in events:
        news_id = str(e.get("news_id") or e.get("id"))
        asset = (e["asset"] or "").upper()
        if not asset:
            continue
        if ctx.q("SELECT 1 FROM t2_event_backfill WHERE news_id=? AND asset=? LIMIT 1",
                 (news_id, asset), note="t2_event_backfill_exists"):
            continue
        bars = _hour_bars(ctx, asset, int(e["ts"]), int(e["ts"]) + 26 * 3600)
        if not bars:
            continue
        entry = bars[0]["close"]
        p1 = pct_change(_at(bars, int(e["ts"]) + 3600), entry)
        p4 = pct_change(_at(bars, int(e["ts"]) + 4 * 3600), entry)
        p24 = pct_change(_at(bars, int(e["ts"]) + 24 * 3600), entry)
        settled = 1 if p24 is not None else 0
        if capture:
            ctx.conn.execute(
                "INSERT OR IGNORE INTO t2_event_backfill "
                "(news_id, asset, event_ts, action, entry_price, pct_1h, pct_4h, pct_24h, settled, filled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (news_id, asset, int(e["ts"]), e.get("action"), entry, p1, p4, p24,
                 settled, ctx.now if settled else None),
            )
            captured += 1
    if capture:
        ctx.conn.commit()
    rows = [dict(r) for r in ctx.q(
        "SELECT * FROM t2_event_backfill ORDER BY event_ts DESC LIMIT 500",
        note="t2_event_backfill_all")]
    done = [r for r in rows if r["settled"] == 1]
    by_action: Dict[str, List[float]] = {}
    for r in done:
        if r.get("pct_4h") is not None and r.get("action"):
            by_action.setdefault(r["action"], []).append(r["pct_4h"])
    summary = {a: {"n": len(v), "avg_pct_4h": round(mean(v), 3)} for a, v in by_action.items()}
    pending = len([r for r in rows if r["settled"] == 0])
    if not rows:
        return FeatureResult.no_data(
            "F01", "影响回填：暂无样本",
            "窗口内新闻缺少对应 1h K 线，无法计算 1h/4h/24h 涨跌",
            evidence=ctx.evidence(tables=["news_events", "kline_bars", "t2_event_backfill"]),
        )
    return FeatureResult.ok(
        "F01", f"影响回填：已回填 {len(done)} 条（待回填 {pending} 条，本轮新增 {captured} 条）",
        {"total": len(rows), "settled": len(done), "pending": pending,
         "captured_now": captured, "by_action_4h": summary,
         "note": "回填口径：以事件后第一根 1h 收盘为基准，比较 +1h/+4h/+24h"},
        evidence=ctx.evidence(tables=["news_events", "kline_bars", "t2_event_backfill"]),
        sample_size=len(rows),
    )


def _at(bars: List[Dict[str, Any]], target: int) -> Optional[float]:
    """取 target 秒之后最近一根 K 线的收盘；超过末根 90 分钟视为缺失。"""
    for b in bars:
        if b["ts"] >= target:
            if b["ts"] - target <= 5400:
                return b["close"]
            return None
    return None


# ── F11 相似历史检索 ────────────────────────────────────────────────────
SIM_SYMBOLS = ("BTC", "ETH", "SPX", "NVDA", "WTI", "AAPL")


@feature("F11")
def similar_history(ctx, top_n: int = 5, horizon: int = 5) -> FeatureResult:
    best = None
    for sym in SIM_SYMBOLS:
        bars = ctx.klines(sym, "1d", 400)
        if len(bars) >= 150:
            best = (sym, bars)
            break
    if not best:
        return FeatureResult.no_data(
            "F11", "相似历史：日线不足", "没有资产累计 ≥150 根日线，无法做相似日检索",
            evidence=ctx.evidence(table="kline_bars"),
        )
    sym, bars = best
    closes = [b["close"] for b in bars]
    vols = [b["volume"] or 0.0 for b in bars]
    rets = [pct_change(closes[i], closes[i - 1]) for i in range(1, len(closes))]
    rows = []
    for i in range(25, len(bars)):
        r5 = pct_change(closes[i], closes[i - 5])
        r20 = pct_change(closes[i], closes[i - 20])
        v20 = stdev([r for r in rets[i - 21:i - 1] if r is not None]) if i > 21 else None
        base_vol = statistics.median([v for v in vols[i - 20:i] if v]) if i >= 20 else None
        vol_x = (vols[i] / base_vol) if base_vol else None
        if None in (r5, r20, v20, vol_x):
            continue
        rows.append({"i": i, "ts": bars[i]["ts"], "vec": [r5, r20, v20, vol_x]})
    if len(rows) < 60:
        return FeatureResult.no_data(
            "F11", "相似历史：可用日不足", f"{sym} 可构造状态向量的交易日仅 {len(rows)} 天",
            evidence=ctx.evidence(table="kline_bars", symbol=sym),
        )
    dims = len(rows[0]["vec"])
    scales = []
    for d in range(dims):
        col = [r["vec"][d] for r in rows]
        scales.append(stdev(col) or 1.0)
    today = rows[-1]
    cands = [r for r in rows[:-horizon - 1] if abs(today["ts"] - r["ts"]) > 30 * 86400]
    scored = []
    for r in cands:
        dist = math.sqrt(sum(((today["vec"][d] - r["vec"][d]) / scales[d]) ** 2 for d in range(dims)))
        fwd = pct_change(closes[r["i"] + horizon], closes[r["i"]])
        scored.append({"ts": r["ts"], "dist": dist, "vec": [round(v, 4) for v in r["vec"]],
                       "fwd_5d_pct": fwd})
    scored.sort(key=lambda r: r["dist"])
    top = scored[:top_n]
    fwd_vals = [t["fwd_5d_pct"] for t in top if t["fwd_5d_pct"] is not None]
    avg_fwd = round(mean(fwd_vals), 2) if fwd_vals else None
    return FeatureResult.ok(
        "F11", f"相似历史：{sym} 今日最像 {len(top)} 个历史日，其后 {horizon} 日平均 {avg_fwd}%"
            if avg_fwd is not None else f"相似历史：{sym} 命中 {len(top)} 个历史日（部分无后续行情）",
        {"symbol": sym, "today_vec": [round(v, 4) for v in today["vec"]],
         "neighbors": top, "avg_forward_pct": avg_fwd,
         "dimensions": ["5日涨跌", "20日涨跌", "20日波动率", "当日量比"],
         "caveat": f"仅 {len(top)} 个邻居的统计，非预测"},
        evidence=ctx.evidence(table="kline_bars", symbol=sym, days=len(rows)),
        sample_size=len(scored),
    )


# ── F12 PnL 归因分解 ────────────────────────────────────────────────────
@feature("F12")
def pnl_attribution(ctx) -> FeatureResult:
    # v404 隔离：归因只做真实纸面成交，不拿历史回放充样本
    live = [t for t in ctx.live_trades(limit=500)
            if t.get("entry_price") and t.get("exit_price")]
    replay_n = ctx.replay_count()
    trades, source = live, "quick_sim_trades(纸面实跑)"
    if len(trades) < MIN_TRAITS:
        return FeatureResult.no_data(
            "F12", f"PnL 归因：真实成交仅 {len(trades)} 笔",
            f"样本<{MIN_TRAITS}，拆解没有统计意义；历史回放 {replay_n} 条已隔离，不进入归因",
            evidence=ctx.evidence(table="quick_sim_trades"),
        )
    fee_bps = _num(ctx.account().get("fee_bps")) or 8.0
    rows = []
    for t in trades:
        entry_ts = int(t.get("entry_ts") or 0)
        exit_ts = int(t.get("exit_ts") or 0)
        if entry_ts and entry_ts > 10_000_000_000:
            entry_ts //= 1000
        if exit_ts and exit_ts > 10_000_000_000:
            exit_ts //= 1000
        direction = 1.0 if (t.get("action") or "").upper() == "BUY" else -1.0
        btc = _hour_bars(ctx, "BTC", entry_ts, exit_ts + 3600) if entry_ts else []
        btc_ret = pct_change(btc[-1]["close"], btc[0]["close"]) if len(btc) >= 2 else None
        pnl = _num(t.get("pnl_pct")) or 0.0
        # 回放样本的 pnl 已扣 cost_bps；纸面实跑按账户 fee_bps 双边计
        cost = (_num(t.get("cost_bps")) or fee_bps * 2) / 100.0
        beta = (direction * btc_ret) if btc_ret is not None else None
        alpha = (pnl - beta + cost) if beta is not None else None
        rows.append({"asset": t.get("asset"), "action": t.get("action"), "pnl_pct": pnl,
                     "beta_pct": beta, "cost_pct": round(-cost, 3), "alpha_pct": alpha})
    total = sum(r["pnl_pct"] for r in rows)
    betas = [r["beta_pct"] for r in rows if r["beta_pct"] is not None]
    alphas = [r["alpha_pct"] for r in rows if r["alpha_pct"] is not None]
    costs = sum(r["cost_pct"] for r in rows)
    by_asset: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        a = by_asset.setdefault(r["asset"] or "?", {"n": 0, "pnl": 0.0, "alpha": 0.0})
        a["n"] += 1
        a["pnl"] += r["pnl_pct"]
        a["alpha"] += r["alpha_pct"] or 0.0
    headline = (f"PnL 归因：{len(rows)} 笔合计 {total:.2f}%"
                f"（beta {sum(betas):.2f}% / alpha {sum(alphas):.2f}% / 成本 {costs:.2f}%）")
    return FeatureResult.ok(
        "F12", headline,
        {"n": len(rows), "data_class": "live", "source": source,
         "total_pnl_pct": round(total, 3),
         "beta_pct": round(sum(betas), 3) if betas else None,
         "alpha_pct": round(sum(alphas), 3) if alphas else None,
         "cost_pct": round(costs, 3), "by_asset": by_asset, "trades": rows,
         "caveat": f"样本 {len(rows)} 笔（{source}），低于 20 笔时只能当参考；beta 用 BTC 同期 1h 收盘近似"},
        evidence=ctx.evidence(tables=["quick_sim_trades", "kline_bars"],
                              source=source), sample_size=len(rows),
    )


# ── F13 跨资产传导链（日线） ────────────────────────────────────────────
@feature("F13")
def cross_asset_leads(ctx, max_lag: int = 3) -> FeatureResult:
    pairs = [("BTC", "ETH"), ("BTC", "SOL"), ("ETH", "SOL"),
             ("SPX", "NVDA"), ("SPX", "AAPL"), ("WTI", "XAU")]
    rows = []
    for a, b in pairs:
        ca, cb = ctx.daily_closes(a, 200), ctx.daily_closes(b, 200)
        n = min(len(ca), len(cb))
        if n < 60:
            continue
        ra = ctx.returns_pct(ca[-n:])
        rb = ctx.returns_pct(cb[-n:])
        best = None
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                r = correlation(ra[:len(ra) - lag or None], rb[lag:])
            else:
                r = correlation(ra[-lag:], rb[:len(rb) + lag])
            if r is None:
                continue
            if best is None or abs(r) > abs(best[1]):
                best = (lag, r)
        if not best:
            continue
        lag, r = best
        lead = a if lag > 0 else (b if lag < 0 else None)
        rows.append({"pair": f"{a}/{b}", "best_lag_days": lag, "corr": round(r, 3),
                     "leader": lead, "n": n})
    if not rows:
        return FeatureResult.no_data(
            "F13", "跨资产传导：日线配对不足", "各配对至少需要 60 个共同日线样本",
            evidence=ctx.evidence(table="kline_bars"),
        )
    rows.sort(key=lambda r: abs(r["corr"]), reverse=True)
    strong = [r for r in rows if abs(r["corr"]) >= 0.3 and r["leader"]]
    return FeatureResult.ok(
        "F13", f"传导链：{len(strong)} 对存在领先关系（最强 {rows[0]['pair']} r={rows[0]['corr']}）",
        {"links": rows, "strong": strong,
         "caveat": "日线 lead-lag 只能说明先后顺序与相关，不构成因果"},
        evidence=ctx.evidence(table="kline_bars"), sample_size=len(rows),
        severity="watch" if strong else "info",
    )


# ── F33 高频 lead-lag ───────────────────────────────────────────────────
@feature("F33")
def hf_leadlag(ctx, max_lag: int = 4) -> FeatureResult:
    pairs = [("BTC", "ETH"), ("BTC", "SOL"), ("ETH", "SOL"), ("BTC", "XAU"), ("XAU", "WTI")]
    rows = []
    for a, b in pairs:
        ba = ctx.klines(a, "1h", 400)
        bb = ctx.klines(b, "1h", 400)
        ma = {x["ts"]: x["close"] for x in ba}
        mb = {x["ts"]: x["close"] for x in bb}
        common = sorted(set(ma) & set(mb))
        if len(common) < 100:
            continue
        ca = [ma[t] for t in common]
        cb = [mb[t] for t in common]
        ra, rb = ctx.returns_pct(ca), ctx.returns_pct(cb)
        best = None
        for lag in range(0, max_lag + 1):
            if lag == 0:
                r = correlation(ra, rb)
            else:
                r = correlation(ra[:len(ra) - lag], rb[lag:])
            if r is None:
                continue
            if best is None or abs(r) > abs(best[1]):
                best = (lag, r)
        if best:
            rows.append({"pair": f"{a}→{b}", "lag_hours": best[0], "corr": round(best[1], 3),
                         "leader": a if best[0] > 0 else "同步", "n": len(common)})
    if not rows:
        return FeatureResult.no_data(
            "F33", "高频 lead-lag：对齐样本不足",
            "1h K 线共同时间点不足 100 个",
            evidence=ctx.evidence(table="kline_bars"),
        )
    rows.sort(key=lambda r: abs(r["corr"]), reverse=True)
    leads = [r for r in rows if r["lag_hours"] > 0 and abs(r["corr"]) >= 0.2]
    return FeatureResult.ok(
        "F33", f"高频 lead-lag：{len(leads)}/{len(rows)} 对存在小时级领先",
        {"links": rows,
         "note": f"按 1h 收益率做 0–{max_lag} 小时位移相关，lag>0 表示前者领先"},
        evidence=ctx.evidence(table="kline_bars", timeframe="1h"), sample_size=len(rows),
    )


# ── F15 影子账本 ────────────────────────────────────────────────────────
@feature("F15")
def shadow_book(ctx) -> FeatureResult:
    rows = ctx.shadow_rows(300)
    if not rows:
        return FeatureResult.no_data(
            "F15", "影子账本：无记录", "shadow_ledger 为空；影子策略尚未跑起来",
            evidence=ctx.evidence(table="shadow_ledger"),
        )
    settled = [r for r in rows if r.get("hit") is not None]
    hits = [r for r in settled if r.get("hit")]
    skipped = [r for r in rows if (r.get("gate") or "").lower() in ("skip", "reject", "blocked")]
    acted = [r for r in rows if (r.get("action_after") or "").upper() not in ("", "HOLD", "NONE")]
    moves = [_num(r.get("move_pct")) for r in settled if _num(r.get("move_pct")) is not None]
    hit_rate = len(hits) / len(settled) if settled else None
    mean_move = mean(moves) if moves else None
    gate_scores = [_num(r.get("gate_score")) for r in rows if _num(r.get("gate_score")) is not None]
    by_day: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        ts = int(r.get("decided_ts") or 0)
        if ts > 10_000_000_000:
            ts //= 1000
        day = str(ts // 86400)
        d = by_day.setdefault(day, {"n": 0, "hit": 0, "settled": 0, "move_sum": 0.0})
        d["n"] += 1
        if r.get("hit") is not None:
            d["settled"] += 1
            if r.get("hit"):
                d["hit"] += 1
        d["move_sum"] += _num(r.get("move_pct")) or 0.0
    recon = [{"day_index": k, "n": v["n"], "settled": v["settled"],
              "hit_rate": round(v["hit"] / v["settled"], 3) if v["settled"] else None,
              "avg_move": round(v["move_sum"] / v["n"], 3) if v["n"] else None}
             for k, v in sorted(by_day.items(), reverse=True)][:10]
    if not settled:
        return FeatureResult.ok(
            "F15", f"影子账本：{len(rows)} 条待结算（{len(skipped)} 条被闸门拦下）",
            {"n": len(rows), "skipped": len(skipped), "recon": recon},
            evidence=ctx.evidence(table="shadow_ledger"), sample_size=len(rows),
        )
    return FeatureResult.ok(
        "F15", f"影子账本：{len(settled)} 条已结算，命中率 {hit_rate:.1%}，平均波动 {mean_move:.2f}%",
        {"n": len(rows), "settled": len(settled), "hit_rate": round(hit_rate, 4) if hit_rate else None,
         "avg_move_pct": round(mean_move, 3) if mean_move is not None else None,
         "skipped_by_gate": len(skipped), "acted": len(acted),
         "avg_gate_score": round(mean(gate_scores), 3) if gate_scores else None,
         "recon": recon,
         "caveat": "影子账本只记录被闸门拦下/被改判的信号，不构成完整收益曲线"},
        evidence=ctx.evidence(table="shadow_ledger"), sample_size=len(settled), severity="info",
    )


# ── F16 回测-实盘一致性 ─────────────────────────────────────────────────
@feature("F16")
def backtest_live_drift(ctx) -> FeatureResult:
    trades = [t for t in ctx.sim_trades(200) if t.get("entry_price") and t.get("entry_ts")]
    audit = [a for a in ctx.trade_audit(200) if (a.get("status") or "").upper() == "ORDER_PLACED"]
    items = {}
    # 1) 成交价漂移：纸面假设价 vs 当时市场 1h 收盘
    slips = []
    for t in trades:
        ts = int(t["entry_ts"])
        if ts > 10_000_000_000:
            ts //= 1000
        bars = _hour_bars(ctx, t["asset"], ts - 3600, ts + 3600)
        if not bars:
            continue
        mk = bars[-1]["close"]
        if not mk:
            continue
        direction = 1.0 if (t.get("action") or "").upper() == "BUY" else -1.0
        slips.append(direction * (_num(t["entry_price"]) - mk) / mk * 100)
    items["entry_price_drift_bp"] = (round(statistics.median(slips) * 100, 2) if slips else None)
    items["entry_price_drift_n"] = len(slips)
    # 2) 信号延迟：trade_audit_log.created_at（文本）与决策时间差
    lat = []
    for a in audit:
        do = ctx.decision_by_id(a.get("decision_id"))
        if not do:
            continue
        created = do.get("created_at")
        ts = _parse_ts(created)
        t2 = _parse_ts(a.get("created_at"))
        if ts and t2:
            lat.append((t2 - ts) / 60.0)
    items["signal_latency_min_median"] = round(statistics.median(lat), 2) if lat else None
    items["signal_latency_n"] = len(lat)
    # 3) 成本漂移
    acct = ctx.account()
    items["fee_bps_model"] = _num(acct.get("fee_bps"))
    items["fee_bps_realized"] = None
    items["fee_bps_note"] = "未接入真实成交手续费账单，实际值留空"
    drift_flags = []
    if items["entry_price_drift_bp"] is not None and abs(items["entry_price_drift_bp"]) > 10:
        drift_flags.append("成交价漂移>10bp")
    if items["signal_latency_min_median"] is not None and items["signal_latency_min_median"] > 5:
        drift_flags.append("信号延迟>5min")
    if not slips and not lat:
        return FeatureResult.no_data(
            "F16", "回测-实盘一致性：无可比对样本",
            "quick_sim_trades 无带入场时间/价格的记录，trade_audit_log 无 ORDER_PLACED 记录",
            evidence=ctx.evidence(tables=["quick_sim_trades", "trade_audit_log"]),
        )
    return FeatureResult.ok(
        "F16", f"回测-实盘：{len(drift_flags)} 项漂移超阈值"
        + (f"（{'、'.join(drift_flags)}）" if drift_flags else ""),
        dict(items, drift_flags=drift_flags, audit_n=len(audit)),
        evidence=ctx.evidence(tables=["quick_sim_trades", "trade_audit_log", "kline_bars"]),
        sample_size=len(slips) + len(lat), severity="warn" if drift_flags else "info",
    )


def _parse_ts(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if float(v) > 10_000_000_000 else float(v)
    s = str(v).strip()
    if s.isdigit():
        return float(s) / 1000 if float(s) > 10_000_000_000 else float(s)
    try:
        import datetime as _dt
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return _dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt).timestamp()
            except ValueError:
                continue
    except Exception:
        return None
    return None


# ── F19 过拟合守卫 ──────────────────────────────────────────────────────
@feature("F19")
def overfit_guard(ctx, min_n: int = 30) -> FeatureResult:
    rows = [dict(r) for r in ctx.q(
        "SELECT strategy_id, forward_pnl, evidence_confidence FROM ai_decisions "
        "WHERE settled=1 AND forward_pnl IS NOT NULL", note="decisions_settled")]
    groups: Dict[str, List[float]] = {}
    for r in rows:
        groups.setdefault(str(r["strategy_id"] or "unassigned"), []).append(float(r["forward_pnl"]))
    if not rows:
        return FeatureResult.no_data(
            "F19", "过拟合守卫：无已结算决策", "ai_decisions 中 settled=1 且 forward_pnl 非空为 0 条",
            evidence=ctx.evidence(table="ai_decisions"),
        )
    trials = max(len(groups), 2)
    sr0 = None
    out = []
    for sid, vals in sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        n = len(vals)
        m = mean(vals) or 0.0
        sd = stdev(vals) or 0.0
        sr = (m / sd) if sd else None
        tstat = (m / (sd / math.sqrt(n))) if sd and n > 1 else None
        p = None
        if tstat is not None:
            p = 2 * (1 - _phi(abs(tstat)))
        dsr = None
        if sr is not None and n > 2:
            skew = _skew(vals)
            kurt = _kurt(vals)
            if sr0 is None:
                g = 0.5772156649
                sr0 = ((1 - g) * _phi_inv(1 - 1 / trials) + g * _phi_inv(1 - 1 / (trials * math.e))) \
                      / math.sqrt(max(n - 1, 1))
            denom = math.sqrt(max(1 - skew * sr + ((kurt - 1) / 4) * sr * sr, 1e-9))
            dsr = _phi((sr - sr0) * math.sqrt(max(n - 1, 1)) / denom)
        out.append({"strategy_id": sid, "n": n, "mean_pnl_pct": round(m, 3),
                    "sharpe_per_trade": round(sr, 3) if sr else None,
                    "t_stat": round(tstat, 2) if tstat else None,
                    "p_value_approx": round(p, 4) if p is not None else None,
                    "deflated_sharpe": round(dsr, 4) if dsr is not None else None,
                    "usable": n >= min_n, "reason": "" if n >= min_n else f"样本 {n} < {min_n}，不允许当结论"})
    usable = [o for o in out if o["usable"]]
    best = max(usable, key=lambda o: o["mean_pnl_pct"]) if usable else None
    return FeatureResult.ok(
        "F19", f"过拟合守卫：{len(out)} 个策略，{len(usable)} 个达到 {min_n} 笔可用门槛"
        + (f"（最佳 {best['strategy_id']} 均值 {best['mean_pnl_pct']}%）" if best else ""),
        {"min_n": min_n, "trials_baseline": trials, "groups": out,
         "caveat": "deflated Sharpe 用 Bailey-López de Prado 简化式，trials 取现有策略数；样本不足者不参与评价"},
        evidence=ctx.evidence(table="ai_decisions"), sample_size=len(rows),
        severity="warn" if not usable else "info",
    )


def _skew(vals: List[float]) -> float:
    n = len(vals)
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / n) or 1.0
    return (sum(((v - m) / sd) ** 3 for v in vals) / n) if n > 2 else 0.0


def _kurt(vals: List[float]) -> float:
    n = len(vals)
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / n) or 1.0
    return (sum(((v - m) / sd) ** 4 for v in vals) / n) if n > 3 else 3.0


# ── F48 影子单 / 假设单 ─────────────────────────────────────────────────
@feature("F48")
def hypothetical_order(ctx, offset_bp: float = 50.0, hours: int = 24) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        px = ctx.price(a)
        bars = ctx.klines(a, "5m", hours * 12)
        if not px or not bars:
            continue
        hypo = px * (1 - offset_bp / 10000.0)
        touched = [b for b in bars if b["low"] is not None and b["low"] <= hypo]
        fill_price = touched[0]["close"] if touched else None
        rows.append({
            "asset": a, "hypo_price": round(hypo, 4), "spot": px,
            "would_fill": bool(touched),
            "minutes_to_fill": round((touched[0]["ts"] - bars[0]["ts"]) / 60, 1) if touched else None,
            "fill_vs_hypo_bp": round((fill_price - hypo) / hypo * 10000, 2) if fill_price else None,
            "bars_checked": len(bars),
        })
    if not rows:
        return FeatureResult.no_data(
            "F48", "影子单：无可用 5m K 线", "资产缺少 5m 行情，无法回放挂单是否会被成交",
            evidence=ctx.evidence(table="kline_bars", timeframe="5m"),
        )
    filled = [r for r in rows if r["would_fill"]]
    adverse = [r for r in filled if (r["fill_vs_hypo_bp"] or 0) < -1]
    return FeatureResult.ok(
        "F48", f"影子单：-{offset_bp:g}bp 限价单在 {hours}h 内 {len(filled)}/{len(rows)} 个会成交",
        {"offset_bp": offset_bp, "window_hours": hours, "assets": rows,
         "adverse_fill": adverse,
         "caveat": "用 5m K 线 low 判断触价，未计队列位置与部分成交"},
        evidence=ctx.evidence(table="kline_bars", timeframe="5m"), sample_size=len(rows),
        severity="watch" if adverse else "info",
    )


# ── F49 信源记分卡 ──────────────────────────────────────────────────────
@feature("F49")
def source_scorecard(ctx) -> FeatureResult:
    rows = ctx.source_stats()
    if not rows:
        return FeatureResult.no_data(
            "F49", "信源记分卡：无统计", "source_stats 为空，缺少信源命中率记录",
            evidence=ctx.evidence(table="source_stats"),
        )
    cards = []
    for r in rows:
        n = int(r.get("sample_n") or 0)
        hit = _num(r.get("hit_rate"))
        mult = _num(r.get("source_mult"))
        avg_pnl = _num(r.get("avg_forward_pnl"))
        score = (hit * mult) if (hit is not None and mult is not None) else hit
        cards.append({
            "source": r.get("news_source"), "asset": r.get("asset"), "n": n,
            "hit_rate": hit, "source_mult": mult, "avg_forward_pnl": avg_pnl,
            "score": round(score, 4) if score is not None else None,
            "tier": ("core" if score is not None and score >= 1.0 and n >= 20
                     else ("watch" if score is not None and score >= 0.8 else "weak")),
            "usable": n >= 20,
        })
    cards.sort(key=lambda c: (c["score"] is None, -(c["score"] or 0)))
    weak = [c for c in cards if c["tier"] == "weak" and c["usable"]]
    return FeatureResult.ok(
        "F49", f"信源记分卡：{len(cards)} 条（{len(weak)} 条可用但偏弱）",
        {"cards": cards[:20], "weak": weak, "total_n": sum(c["n"] for c in cards),
         "missing": ["谁先报（时延）未记录，暂无法评分时效；影响判断准确度用 source_mult 间接反映"]},
        evidence=ctx.evidence(table="source_stats"), sample_size=len(cards),
        severity="watch" if weak else "info",
    )
