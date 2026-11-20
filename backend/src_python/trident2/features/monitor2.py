# -*- coding: utf-8 -*-
"""市场监控类（下）：F29–F32 / F34 / F36 / F37 / F39–F44。"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from .. import store
from ..registry import feature
from ..types import FeatureResult
from ._util import correlation, mean, pct_change

NEWS_SPIKE_X = 3.0


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ── F29 新闻密度突增 ────────────────────────────────────────────────────
@feature("F29")
def news_density(ctx) -> FeatureResult:
    events = ctx.news(hours=24 * 7)
    if not events:
        return FeatureResult.no_data(
            "F29", "新闻密度：7 天内无新闻事件", "news_events 为空",
            evidence=ctx.evidence(table="news_events"),
        )
    now_h = ctx.now // 3600
    per_asset_hour: Dict[str, Dict[int, int]] = {}
    for e in events:
        asset = (e.get("asset") or "").upper()
        if not asset:
            continue
        h = int(e["ts"]) // 3600
        per_asset_hour.setdefault(asset, {})
        per_asset_hour[asset][h] = per_asset_hour[asset].get(h, 0) + 1
    rows = []
    for asset, buckets in per_asset_hour.items():
        last_hour = buckets.get(now_h, 0) + buckets.get(now_h - 1, 0)
        baseline = [v for h, v in buckets.items() if h < now_h - 1]
        if len(baseline) < 20:
            continue
        base = statistics.median(baseline)
        ratio = (last_hour / base) if base else None
        if ratio and ratio >= NEWS_SPIKE_X:
            rows.append({"asset": asset, "last_2h": last_hour, "median_hourly": base, "x": ratio})
    rows.sort(key=lambda r: r["x"], reverse=True)
    if not rows:
        return FeatureResult.ok(
            "F29", "新闻密度：无突增", {"flagged": [], "assets_checked": len(per_asset_hour)},
            evidence=ctx.evidence(table="news_events", window="7d"), sample_size=len(events),
        )
    return FeatureResult.ok(
        "F29", f"新闻密度：{len(rows)} 个标的 2h 条数 ≥{NEWS_SPIKE_X}x 基线", {"flagged": rows[:10]},
        evidence=ctx.evidence(table="news_events", window="7d"), sample_size=len(events),
        severity="warn",
    )


# ── F30 信源分歧 ────────────────────────────────────────────────────────
@feature("F30")
def source_divergence(ctx) -> FeatureResult:
    events = ctx.news(hours=6)
    if not events:
        return FeatureResult.no_data(
            "F30", "信源分歧：6h 内无新闻", "news_events 为空，无法比较信源方向",
            evidence=ctx.evidence(table="news_events"),
        )
    by_asset: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        asset = (e.get("asset") or "").upper()
        action = (e.get("action") or "").upper()
        if asset and action in ("BUY", "SELL"):
            by_asset.setdefault(asset, []).append(e)
    rows = []
    for asset, items in by_asset.items():
        buy = [i for i in items if i["action"] == "BUY"]
        sell = [i for i in items if i["action"] == "SELL"]
        if len(items) < 6:
            continue
        buy_share = len(buy) / len(items)
        if buy_share >= 0.25 and (1 - buy_share) >= 0.25:
            rows.append({
                "asset": asset, "n": len(items), "buy": len(buy), "sell": len(sell),
                "sources": sorted({i["source"] for i in items if i.get("source")})[:8],
                "sample_buy": [i["headline"][:60] for i in buy[:2]],
                "sample_sell": [i["headline"][:60] for i in sell[:2]],
            })
    if not rows:
        return FeatureResult.ok(
            "F30", "信源分歧：未检测到相反方向", {"divergent": [], "assets_checked": len(by_asset)},
            evidence=ctx.evidence(table="news_events", window="6h"), sample_size=len(events),
        )
    return FeatureResult.ok(
        "F30", f"信源分歧：{len(rows)} 个标的多空说法对立", {"divergent": rows},
        evidence=ctx.evidence(table="news_events", window="6h"), sample_size=len(events),
        severity="watch",
    )


# ── F31 宏观倒计时与预期差 ──────────────────────────────────────────────
@feature("F31")
def macro_countdown(ctx) -> FeatureResult:
    events = ctx.calendar_events(days=7) + ctx.econ_events(days=7)
    upcoming = [e for e in events if e.get("event_time") or e.get("ts")]
    fomc_days = _num((ctx.external("fomc_days_to_next") or {}).get("value"))
    if not upcoming and fomc_days is None:
        return FeatureResult.no_data(
            "F31", "宏观倒计时：7 天内无日历事件", "macro_calendar / econ_events 无未来事件",
            evidence=ctx.evidence(tables=["macro_calendar", "econ_events"]),
        )
    rows = []
    for e in upcoming:
        t = e.get("event_time") or e.get("ts")
        rows.append({
            "title": e.get("title"), "country": e.get("country"),
            "impact": e.get("impact"), "hours_left": (int(t) - ctx.now) / 3600,
            "forecast": e.get("forecast"), "previous": e.get("previous"), "actual": e.get("actual"),
        })
    rows.sort(key=lambda r: r["hours_left"])
    surprises = []
    for e in events:
        actual, forecast = _num(e.get("actual")), _num(e.get("forecast"))
        if actual is not None and forecast is not None:
            surprises.append({"title": e.get("title"), "actual": actual, "forecast": forecast,
                              "surprise": actual - forecast})
    nearest = rows[0] if rows else None
    headline = (f"宏观倒计时：最近事件「{nearest['title']}」还有 {nearest['hours_left']:.1f}h"
                if nearest else f"宏观倒计时：FOMC 还有 {fomc_days:.0f} 天")
    return FeatureResult.ok(
        "F31", headline,
        {"upcoming": rows[:12], "surprises_48h": surprises, "fomc_days_to_next": fomc_days},
        evidence=ctx.evidence(tables=["macro_calendar", "econ_events", "external_indicators"]),
        sample_size=len(rows), severity="watch" if nearest and nearest["hours_left"] <= 12 else "info",
    )


# ── F32 相关性突变 ──────────────────────────────────────────────────────
@feature("F32")
def correlation_shift(ctx) -> FeatureResult:
    series = {}
    for a in ("BTC", "ETH", "SOL", "XAU", "WTI", "NVDA", "SPX"):
        closes = ctx.daily_closes(a, 60)
        if len(closes) >= 45:
            series[a] = ctx.returns_pct(closes)[-40:]
    if len(series) < 2:
        return FeatureResult.no_data(
            "F32", "相关性突变：日线序列不足",
            "至少需要 2 个资产各 45 根日线才能比较前后 20 日相关性",
            evidence=ctx.evidence(table="kline_bars"),
        )
    rows = []
    keys = sorted(series)
    pairs = 0
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            n = min(len(series[a]), len(series[b]))
            if n < 40:
                continue
            pairs += 1
            old = correlation(series[a][-40:-20], series[b][-40:-20])
            new = correlation(series[a][-20:], series[b][-20:])
            if old is None or new is None:
                continue
            delta = new - old
            if abs(delta) >= 0.35 or (old > 0.3 and new < -0.1) or (old < -0.3 and new > 0.1):
                rows.append({"pair": f"{a}-{b}", "corr_prev20": old, "corr_last20": new, "delta": delta})
    if not rows:
        return FeatureResult.ok(
            "F32", "相关性：未出现结构突变", {"pairs_checked": pairs},
            evidence=ctx.evidence(table="kline_bars", window="40d"), sample_size=pairs,
        )
    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return FeatureResult.ok(
        "F32", f"相关性突变：{len(rows)} 对资产关系改写", {"pairs": rows},
        evidence=ctx.evidence(table="kline_bars", window="40d"), sample_size=len(rows),
        severity="warn",
    )


# ── F34 市场宽度 ────────────────────────────────────────────────────────
@feature("F34")
def market_breadth(ctx) -> FeatureResult:
    adv = _num((ctx.macro_latest("breadth", "crypto.advancers") or {}).get("value"))
    dec = _num((ctx.macro_latest("breadth", "crypto.decliners") or {}).get("value"))
    fng = _num((ctx.macro_latest("sentiment", "crypto_fng") or {}).get("value"))
    changes = []
    for a in ctx.assets():
        t = ctx.latest_tick(a)
        if t and t.get("change24h") is not None:
            changes.append(float(t["change24h"]))
    up = len([c for c in changes if c > 0])
    down = len([c for c in changes if c < 0])
    if adv is None and not changes:
        return FeatureResult.no_data(
            "F34", "市场宽度：无涨跌家数数据", "macro breadth 与 tick.change24h 均不可用",
            evidence=ctx.evidence(tables=["macro_snapshots", "market_ticks"]),
        )
    ratio = (adv / dec) if adv is not None and dec else None
    regime = "risk_on" if ratio and ratio > 1.5 else ("risk_off" if ratio and ratio < 0.67 else "neutral")
    headline = (f"市场宽度：{regime}（涨跌比 {ratio:.2f}）" if ratio
                else f"市场宽度：本地 {up} 涨 / {down} 跌")
    return FeatureResult.ok(
        "F34", headline,
        {"advancers": adv, "decliners": dec, "adv_dec_ratio": ratio,
         "local_up": up, "local_down": down, "fear_greed": fng, "regime": regime},
        evidence=ctx.evidence(tables=["macro_snapshots", "market_ticks"]),
        sample_size=len(changes), severity="watch" if regime == "risk_off" else "info",
    )


# ── F36 异动四件套 ──────────────────────────────────────────────────────
@feature("F36")
def anomaly_quartet(ctx) -> FeatureResult:
    signals = ctx.abnormal(limit=10)
    if not signals:
        return FeatureResult.no_data(
            "F36", "异动四件套：无异动记录", "abnormal_signals 为空",
            evidence=ctx.evidence(table="abnormal_signals"),
        )
    news_recent = ctx.news(hours=48)
    rows = []
    for s in signals:
        asset = s["asset"]
        anchor = int(s["anchor_ts"] or s["ts"])
        news_cnt = len([e for e in news_recent
                        if (e.get("asset") or "").upper() == asset.upper()
                        and abs(int(e["ts"]) - anchor) <= 7200])
        depth = ctx.depth(asset)
        bars = ctx.klines(asset, "1d", 25)
        vol_ratio = None
        if len(bars) >= 21 and bars[-1]["volume"]:
            base = statistics.median([b["volume"] for b in bars[-21:-1] if b["volume"]])
            vol_ratio = bars[-1]["volume"] / base if base else None
        rows.append({
            "id": s["id"], "asset": asset, "kind": s["signal_kind"], "direction": s["direction"],
            "trigger": s.get("hint") or s["signal_kind"], "anchor_ts": anchor,
            "news_2h": news_cnt, "volume_ratio": vol_ratio,
            "liquidity_spread_bp": depth.get("spread_bp") if depth else None,
            "liquidity_imbalance": depth.get("imbalance") if depth else None,
        })
    incomplete = [r for r in rows if r["liquidity_spread_bp"] is None or r["news_2h"] == 0]
    return FeatureResult.ok(
        "F36", f"异动四件套：{len(rows)} 条异动已补齐上下文（{len(incomplete)} 条缺项）",
        {"items": rows, "incomplete_ids": [r["id"] for r in incomplete]},
        evidence=ctx.evidence(tables=["abnormal_signals", "news_events", "depth_snapshots", "kline_bars"]),
        sample_size=len(rows), severity="watch" if incomplete else "info",
    )


# ── F37 异动快照与回填 ──────────────────────────────────────────────────
@feature("F37")
def anomaly_snapshot(ctx, capture: bool = True, limit: int = 5) -> FeatureResult:
    captured = 0
    if capture:
        for s in ctx.abnormal(limit=int(limit)):
            anchor = int(s["anchor_ts"] or s["ts"])
            exists = ctx.q(
                "SELECT 1 FROM t2_anomaly_snapshots WHERE asset=? AND kind=? AND anchor_ts=? LIMIT 1",
                (s["asset"], s["signal_kind"], anchor), note="t2_anomaly_snapshot_exists",
            )
            if exists:
                continue
            bar = ctx.klines(s["asset"], "1d", 1)
            ctx.conn.execute(
                "INSERT INTO t2_anomaly_snapshots (ts, asset, kind, anchor_ts, close, baseline_json, evidence_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (ctx.now, s["asset"], s["signal_kind"], anchor,
                 bar[0]["close"] if bar else None,
                 store._dump({"hint": s.get("hint")}),
                 store._dump(s.get("detail_json"))),
            )
            captured += 1
        ctx.conn.commit()
    pending = [dict(r) for r in ctx.q(
        "SELECT * FROM t2_anomaly_snapshots WHERE settled=0 ORDER BY id LIMIT 200",
        note="t2_anomaly_pending",
    )]
    settled_now = 0
    outcomes = []
    for snap in pending:
        age = ctx.now - int(snap["anchor_ts"])
        if snap.get("close") is None:
            continue
        entry = float(snap["close"])
        bars = [b for b in ctx.klines(snap["asset"], "1h", 48) if b["ts"] > int(snap["anchor_ts"])]
        p1 = p4 = p24 = None
        if bars:
            p1 = pct_change(bars[0]["close"], entry)
            p4 = pct_change(bars[min(3, len(bars) - 1)]["close"], entry)
            p24 = pct_change(bars[-1]["close"], entry) if age >= 86400 else None
        else:
            # 只有日线的资产（个股异动）退化成「次日收盘」口径，1h/4h 留空，不拿别的周期顶替
            dbars = [b for b in ctx.klines(snap["asset"], "1d", 30) if b["ts"] > int(snap["anchor_ts"])]
            if not dbars:
                continue
            p24 = pct_change(dbars[0]["close"], entry)
        ctx.conn.execute(
            "UPDATE t2_anomaly_snapshots SET pct_1h=?, pct_4h=?, pct_1d=?, settled=?, settled_at=? WHERE id=?",
            (p1, p4, p24,
             1 if (p24 is not None or p1 is not None) else 0,
             ctx.now if (p24 is not None or p1 is not None) else None, snap["id"]),
        )
        settled_now += 1
        outcomes.append({"id": snap["id"], "asset": snap["asset"], "kind": snap["kind"],
                         "pct_1h": p1, "pct_4h": p4, "pct_1d": p24})
    ctx.conn.commit()
    allrows = [dict(r) for r in ctx.q(
        "SELECT * FROM t2_anomaly_snapshots ORDER BY id DESC LIMIT 200", note="t2_anomaly_all")]
    total_rows = int(ctx.q("SELECT COUNT(*) c FROM t2_anomaly_snapshots", note="t2_anomaly_count")[0]["c"])
    settled_rows = int(ctx.q("SELECT COUNT(*) c FROM t2_anomaly_snapshots WHERE settled=1",
                            note="t2_anomaly_settled")[0]["c"])
    done = [r for r in allrows if r["settled"] == 1]
    if not allrows:
        return FeatureResult.no_data(
            "F37", "异动快照：样本库为空", "abnormal_signals 无记录，且未捕获任何快照",
            evidence=ctx.evidence(table="t2_anomaly_snapshots"),
        )
    hit_rate = None
    directional = [r for r in done if r.get("pct_1h") is not None]
    if directional:
        hit_rate = len([r for r in directional if abs(r["pct_1h"]) >= 0.5]) / len(directional)
    return FeatureResult.ok(
        "F37", f"异动快照：累计 {total_rows} 条（已回填 {settled_rows} 条），本轮新增 {captured} 条",
        {"captured": captured, "settled_now": settled_now, "total": total_rows,
         "settled": settled_rows, "recent_outcomes": outcomes[-10:],
         "unsettleable_assets": [a for a in {r["asset"] for r in allrows}
                                 if not ctx.klines(a, "1h", 2)][:10],
         "note": "回填需要该资产的小时线；只有日线或根本没有 K 线的资产（如个股异动）会一直留在待回填",
         "pct_1h_abs_ge_0.5_rate": hit_rate},
        evidence=ctx.evidence(tables=["abnormal_signals", "t2_anomaly_snapshots", "kline_bars"]),
        sample_size=len(allrows),
    )


# ── F39 盘口失衡与大单墙 ────────────────────────────────────────────────
@feature("F39")
def orderbook_imbalance(ctx) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        d = ctx.depth(a)
        if not d:
            continue
        rows.append({
            "asset": a, "ts": d["ts"], "spread_bp": d["spread_bp"],
            "imbalance": d.get("imbalance"), "bid_vol": d.get("bid_vol"), "ask_vol": d.get("ask_vol"),
            "wall_bid": d.get("wall_bid"), "wall_ask": d.get("wall_ask"),
            "wall_bid_px": d.get("wall_bid_px"), "wall_ask_px": d.get("wall_ask_px"),
        })
    if not rows:
        return FeatureResult.no_data(
            "F39", "盘口失衡：无盘口快照", "depth_snapshots 无记录；盘口采集未运行",
            evidence=ctx.evidence(table="depth_snapshots"),
        )
    flagged = []
    for r in rows:
        im = _num(r["imbalance"])
        if im is None:
            continue
        if abs(im) >= 0.35:
            flagged.append({"asset": r["asset"], "imbalance": im,
                            "side": "bid_heavy" if im > 0 else "ask_heavy",
                            "wall_bid": r["wall_bid"], "wall_ask": r["wall_ask"]})
    return FeatureResult.ok(
        "F39", f"盘口：{len(flagged)}/{len(rows)} 个资产失衡显著", {"assets": rows, "flagged": flagged},
        evidence=ctx.evidence(table="depth_snapshots"), sample_size=len(rows),
        severity="warn" if flagged else "info",
    )


# ── F40 价差与套利 ──────────────────────────────────────────────────────
@feature("F40")
def spread_arbitrage(ctx) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        srcs = ctx.tick_sources(a, lookback_sec=900)
        if len(srcs) < 2:
            continue
        prices = [s["price"] for s in srcs]
        lo, hi = min(prices), max(prices)
        spread_bp = (hi - lo) / lo * 10000 if lo else None
        rows.append({"asset": a, "n_sources": len(srcs), "spread_bp": spread_bp,
                     "low_source": [s["source"] for s in srcs if s["price"] == lo][0],
                     "high_source": [s["source"] for s in srcs if s["price"] == hi][0]})
    disp = _num((ctx.external("x_spot_dispersion_pct") or {}).get("value"))
    basis = {}
    for a in ("BTC", "ETH", "SOL"):
        r = ctx.external("bn_basis_pct", a)
        if r:
            basis[a] = _num(r["value"])
    if not rows and disp is None and not basis:
        return FeatureResult.no_data(
            "F40", "价差与套利：无多源报价", "同资产 15 分钟内不足 2 个源，且无基差指标",
            evidence=ctx.evidence(tables=["market_ticks", "external_indicators"]),
        )
    opps = [r for r in rows if r["spread_bp"] and r["spread_bp"] >= 10]
    return FeatureResult.ok(
        "F40", f"价差：{len(opps)} 个资产跨源价差 ≥10bp（未计费用）",
        {"cross_source": rows, "opportunities": opps, "spot_dispersion_pct": disp,
         "perp_basis_pct": basis,
         "caveat": "未扣手续费/资金费/滑点，不是可直接执行的套利结论"},
        evidence=ctx.evidence(tables=["market_ticks", "external_indicators"]), sample_size=len(rows),
        severity="watch" if opps else "info",
    )


# ── F41 解锁与事件日历 ──────────────────────────────────────────────────
@feature("F41")
def unlock_calendar(ctx) -> FeatureResult:
    events = ctx.calendar_events(days=14)
    fomc_days = _num((ctx.external("fomc_days_to_next") or {}).get("value"))
    earnings = _num((ctx.external("us_earnings_count_7d") or {}).get("value"))
    if not events and fomc_days is None and earnings is None:
        return FeatureResult.no_data(
            "F41", "事件日历：无可用数据", "macro_calendar 无未来事件，且 FOMC/财报计数缺失",
            evidence=ctx.evidence(tables=["macro_calendar", "external_indicators"]),
        )
    rows = []
    for e in events:
        rows.append({"title": e.get("title"), "impact": e.get("impact"),
                     "hours_left": (int(e["event_time"]) - ctx.now) / 3600,
                     "forecast": e.get("forecast"), "previous": e.get("previous")})
    rows.sort(key=lambda r: r["hours_left"])
    high = [r for r in rows if str(r["impact"]).lower() == "high"]
    return FeatureResult.ok(
        "F41", f"事件日历：14 天内 {len(rows)} 个事件，其中高影响 {len(high)} 个",
        {"next_14d": rows[:20], "high_impact": high, "fomc_days_to_next": fomc_days,
         "us_earnings_7d": earnings, "missing": ["代币解锁日历（未接入解锁/供给数据）"]},
        evidence=ctx.evidence(tables=["macro_calendar", "external_indicators"]), sample_size=len(rows),
        severity="watch" if high else "info",
    )


# ── F42 新上架与新叙事 ──────────────────────────────────────────────────
@feature("F42")
def new_listing_narrative(ctx) -> FeatureResult:
    young = []
    since = ctx.now - 14 * 86400
    rows = ctx.q("SELECT symbol, MIN(ts) AS first_ts FROM kline_bars GROUP BY symbol",
                 note="kline_first_ts")
    for r in rows:
        first = int(r["first_ts"])
        if first > 10_000_000_000:
            first //= 1000
        if first >= since:
            young.append({"asset": r["symbol"], "first_bar_ts": first,
                          "days_listed": round((ctx.now - first) / 86400, 1)})
    topics = []
    td_rows = [dict(r) for r in ctx.q(
        "SELECT topic_key, display_name, mentions, heat, day FROM crowd_topic_daily "
        "ORDER BY day DESC, mentions DESC LIMIT 300", note="crowd_topic_daily")]
    by_topic: Dict[str, List[int]] = {}
    names: Dict[str, str] = {}
    for r in td_rows:
        by_topic.setdefault(r["topic_key"], []).append(int(r["mentions"] or 0))
        names.setdefault(r["topic_key"], r["display_name"] or r["topic_key"])
    for key, vals in by_topic.items():
        if len(vals) < 3:
            continue
        base = statistics.median(vals[1:])
        latest = vals[0]
        if base and latest / base >= 2 and latest >= 3:
            topics.append({"topic": (names[key] or key)[:80], "mentions": latest,
                           "baseline": base, "x": latest / base})
    topics.sort(key=lambda t: t["x"], reverse=True)
    if not young and not topics:
        return FeatureResult.ok(
            "F42", "新上架与新叙事：无新增", {"new_assets": [], "rising_topics": []},
            evidence=ctx.evidence(tables=["kline_bars", "crowd_topic_daily"]),
        )
    return FeatureResult.ok(
        "F42", f"新上架/叙事：{len(young)} 个新标的，{len(topics)} 个话题升温",
        {"new_assets": young, "rising_topics": topics[:10]},
        evidence=ctx.evidence(tables=["kline_bars", "crowd_topic_daily"]),
        sample_size=len(young) + len(topics), severity="watch" if topics[:3] else "info",
    )


# ── F43 期权持仓 ────────────────────────────────────────────────────────
@feature("F43")
def options_positioning(ctx) -> FeatureResult:
    rows = []
    for a in ("BTC", "ETH"):
        max_pain = _num((ctx.macro_latest("options", f"{a}.opt_max_pain") or {}).get("value"))
        skew = _num((ctx.macro_latest("options", f"{a}.opt_skew") or {}).get("value"))
        cp = ctx.macro_latest("options", f"{a}.opt_call_put_oi")
        wall = ctx.macro_latest("options", f"{a}.opt_wall")
        spot = ctx.price(a)
        if max_pain is None and skew is None:
            continue
        read = ("中性")
        if skew is not None and skew < -3:
            read = "put 偏贵（防守情绪）"
        elif skew is not None and skew > 3:
            read = "call 偏贵（追涨情绪）"
        rows.append({
            "asset": a, "spot": spot, "max_pain": max_pain,
            "max_pain_dist_pct": pct_change(max_pain, spot) if spot and max_pain else None,
            "skew": skew, "call_put_oi": cp.get("value") if cp else None,
            "wall": wall.get("value") if wall else None, "read": read,
        })
    if not rows:
        return FeatureResult.no_data(
            "F43", "期权持仓：无期权链数据", "macro_snapshots.category=options 无 BTC/ETH 记录",
            evidence=ctx.evidence(table="macro_snapshots"),
        )
    extreme = [r["asset"] for r in rows if r["skew"] is not None and abs(r["skew"]) > 5]
    return FeatureResult.ok(
        "F43", f"期权持仓：{len(rows)} 个标的定位已读取，{len(extreme)} 个 skew 极端",
        {"assets": rows, "extreme_skew": extreme},
        evidence=ctx.evidence(table="macro_snapshots", category="options"), sample_size=len(rows),
        severity="watch" if extreme else "info",
    )


# ── F44 链上与巨鲸 ──────────────────────────────────────────────────────
@feature("F44")
def onchain_whales(ctx) -> FeatureResult:
    hashrate = _num((ctx.external("cm_hashrate") or ctx.external("btc.hashrate") or {}).get("value"))
    mempool = _num((ctx.external("btc_mempool_mb") or {}).get("value"))
    tx24 = _num((ctx.external("btc_tx_24h") or {}).get("value"))
    usdt = _num((ctx.external("sc_usdt_mcap") or {}).get("value"))
    usdc = _num((ctx.external("sc_usdc_mcap") or {}).get("value"))
    stable_chg = _num((ctx.external("stable_supply_30d_pct") or {}).get("value"))
    if all(v is None for v in (hashrate, mempool, tx24, usdt, usdc)):
        return FeatureResult.no_data(
            "F44", "链上与巨鲸：无链上指标", "external_indicators 缺 hashrate/mempool/稳定币供应",
            evidence=ctx.evidence(table="external_indicators"),
        )
    flags = []
    if stable_chg is not None and abs(stable_chg) >= 3:
        flags.append("stable_supply_shift")
    return FeatureResult.ok(
        "F44", f"链上：哈希率/内存池/稳定币 {len(flags)} 个信号",
        {"btc_hashrate": hashrate, "btc_mempool_mb": mempool, "btc_tx_24h": tx24,
         "usdt_mcap": usdt, "usdc_mcap": usdc, "stable_supply_30d_pct": stable_chg,
         "flags": flags,
         "missing": ["交易所余额", "巨鲸转账", "矿工流出（未接入地址级链上数据）"]},
        evidence=ctx.evidence(table="external_indicators"), sample_size=4,
        severity="watch" if flags else "info",
    )
