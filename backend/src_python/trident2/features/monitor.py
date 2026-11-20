# -*- coding: utf-8 -*-
"""市场监控类（上）：F14 / F20–F24 / F26 / F28。

原则：每个指标都必须由库内真实序列算出；数据不足就 no_data 并写清缺什么。
阈值一律用「相对自身历史」或「相对基线」，不用拍脑袋的绝对百分比。
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from ..registry import feature
from ..types import FeatureResult
from ._util import atr, mean, pct_change, realized_vol, zscore

Z_WARN = 2.0
VOL_SPIKE_X = 2.0
NEWS_SPIKE_X = 3.0


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ── F14 拥挤度与流向反转 ────────────────────────────────────────────────
@feature("F14")
def crowding(ctx) -> FeatureResult:
    rows = []
    for a in ("BTC", "ETH", "SOL"):
        funding = _num((ctx.external("bn_funding_rate", a) or {}).get("value"))
        oi_hist = [_num(r["value"]) for r in ctx.external_history("bn_open_interest_usd", a, 48)]
        oi_hist = [v for v in oi_hist if v is not None]
        ls = _num((ctx.external("bn_long_short_ratio", a) or {}).get("value"))
        cvd = _num((ctx.macro_latest("flow", f"{a}.cvd") or {}).get("value"))
        cheap = _num((ctx.latest_tick(a) or {}).get("change24h"))
        flags = []
        if funding is not None and abs(funding) >= 0.0005:
            flags.append("funding_extreme")
        if len(oi_hist) >= 12:
            base = statistics.median(oi_hist[: max(len(oi_hist) // 2, 1)])
            now = oi_hist[-1]
            if base and now / base >= 1.1:
                flags.append("oi_surge")
        if ls is not None and (ls >= 2.5 or ls <= 0.4):
            flags.append("ls_crowded")
        if cvd is not None and cheap is not None and ((cvd > 0 > cheap) or (cvd < 0 < cheap)):
            flags.append("flow_reversal")
        if flags:
            rows.append({"asset": a, "flags": flags, "funding": funding, "ls_ratio": ls,
                         "oi_now": oi_hist[-1] if oi_hist else None, "cvd_pct": cvd,
                         "change24h": cheap})
    if not rows:
        if not any(ctx.external("bn_funding_rate", a) for a in ("BTC", "ETH", "SOL")):
            return FeatureResult.no_data(
                "F14", "拥挤度：无衍生品持仓数据",
                "external_indicators 缺 bn_funding_rate / open_interest；无法判断拥挤",
                evidence=ctx.evidence(tables=["external_indicators", "macro_snapshots"]),
            )
        return FeatureResult.ok(
            "F14", "拥挤度：无资产触发拥挤标志", {"crowded": [], "checked": ["BTC", "ETH", "SOL"]},
            evidence=ctx.evidence(tables=["external_indicators", "macro_snapshots"]), sample_size=3,
        )
    severity = "warn" if any(len(r["flags"]) >= 2 for r in rows) else "info"
    return FeatureResult.ok(
        "F14", f"拥挤度：{len(rows)} 个资产出现拥挤/反转信号",
        {"crowded": rows}, evidence=ctx.evidence(tables=["external_indicators", "macro_snapshots"]),
        sample_size=len(rows), severity=severity,
        action={"kind": "watch_crowding", "assets": [r["asset"] for r in rows]} if severity != "info" else None,
    )


# ── F20 相对自身异动 ────────────────────────────────────────────────────
@feature("F20")
def relative_move(ctx) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        closes = ctx.daily_closes(a, 60)
        if len(closes) < 21:
            continue
        rets = ctx.returns_pct(closes)
        latest = rets[-1]
        z = zscore(latest, rets[:-1][-20:])
        if z is None:
            continue
        rows.append({"asset": a, "last_ret_pct": latest, "z_20d": z, "abs_z": abs(z)})
    if not rows:
        return FeatureResult.no_data(
            "F20", "相对异动：日线样本不足",
            "每个资产至少需要 21 根日线才能算 20 日 z-score",
            evidence=ctx.evidence(table="kline_bars/ohlcv_daily"),
        )
    rows.sort(key=lambda r: r["abs_z"], reverse=True)
    hits = [r for r in rows if r["abs_z"] >= Z_WARN]
    top = rows[:5]
    return FeatureResult.ok(
        "F20", f"相对异动：{len(hits)} 个资产 |z|≥{Z_WARN}，最异常 {top[0]['asset']} z={top[0]['z_20d']:.2f}",
        {"flagged": hits, "top5": top, "coverage": len(rows)},
        evidence=ctx.evidence(table="kline_bars/ohlcv_daily", window="20d"), sample_size=len(rows),
        severity="warn" if hits else "info",
    )


# ── F21 成交突增 ────────────────────────────────────────────────────────
@feature("F21")
def volume_spike(ctx) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        bars = ctx.klines(a, "1d", 25)
        if len(bars) < 21:
            continue
        vols = [b["volume"] for b in bars if b["volume"]]
        if len(vols) < 20:
            continue
        latest = bars[-1]
        base = statistics.median(vols[:-1][-20:])
        ratio = (latest["volume"] / base) if base else None
        prev_close = bars[-2]["close"]
        direction = "up" if latest["close"] > prev_close else ("down" if latest["close"] < prev_close else "flat")
        if ratio is None:
            continue
        rows.append({"asset": a, "volume_ratio": ratio, "direction": direction,
                     "change_pct": pct_change(latest["close"], prev_close)})
    if not rows:
        return FeatureResult.no_data(
            "F21", "成交突增：日线成交量样本不足", "需要 20 日均量与最新一根日线",
            evidence=ctx.evidence(table="kline_bars"),
        )
    hits = sorted([r for r in rows if r["volume_ratio"] >= VOL_SPIKE_X],
                  key=lambda r: r["volume_ratio"], reverse=True)
    return FeatureResult.ok(
        "F21", f"成交突增：{len(hits)} 个资产量比 ≥{VOL_SPIKE_X}",
        {"flagged": hits, "max_ratio": max(r["volume_ratio"] for r in rows)},
        evidence=ctx.evidence(table="kline_bars", window="20d"), sample_size=len(rows),
        severity="warn" if hits else "info",
    )


# ── F22 波动率突变 ──────────────────────────────────────────────────────
@feature("F22")
def volatility_regime(ctx) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        bars = ctx.klines(a, "1d", 70)
        closes = [b["close"] for b in bars]
        if len(bars) < 40:
            continue
        atr_now = atr(bars, 14)
        atr_prev = atr(bars[:-10], 14)
        vol20 = realized_vol(closes, 20)
        vol60 = realized_vol(closes, 60) if len(closes) >= 61 else None
        if atr_now is None or atr_prev is None or vol20 is None:
            continue
        atr_x = (atr_now / atr_prev) if atr_prev else None
        vol_x = (vol20 / vol60) if vol60 else None
        flags = []
        if atr_x and atr_x >= 1.3:
            flags.append("atr_up")
        if vol_x and vol_x >= 1.3:
            flags.append("rv_up")
        rows.append({"asset": a, "atr_x": atr_x, "rv20": vol20, "rv60": vol60,
                     "rv_x": vol_x, "flags": flags})
    if not rows:
        return FeatureResult.no_data(
            "F22", "波动率突变：K 线样本不足", "需要 40+ 根日线算 ATR 与已实现波动",
            evidence=ctx.evidence(table="kline_bars"),
        )
    iv = {}
    for ind in ("crypto_atm_iv", "dv_dvol"):
        row = ctx.external(ind)
        if row:
            iv[ind] = row["value"]
    hits = [r for r in rows if r["flags"]]
    value = {"flagged": hits, "assets": rows, "iv_snapshot": iv,
             "iv_note": "IV 为市场级快照，未与单资产一一对应"}
    return FeatureResult.ok(
        "F22", f"波动率：{len(hits)} 个资产波动放大", value,
        evidence=ctx.evidence(tables=["kline_bars", "external_indicators"]), sample_size=len(rows),
        severity="warn" if hits else "info",
    )


# ── F23 关键位突破 ──────────────────────────────────────────────────────
@feature("F23")
def level_breakout(ctx) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        bars = ctx.klines(a, "1d", 40)
        if len(bars) < 25:
            continue
        close = bars[-1]["close"]
        prev_high = max(b["high"] for b in bars[-21:-1] if b["high"])
        prev_low = min(b["low"] for b in bars[-21:-1] if b["low"])
        closes20 = [b["close"] for b in bars[-20:]]
        ma20 = mean(closes20)
        sd20 = statistics.pstdev(closes20) if len(closes20) > 1 else None
        boll_up = ma20 + 2 * sd20 if sd20 else None
        boll_low = ma20 - 2 * sd20 if sd20 else None
        events = []
        if close > prev_high:
            events.append("break_20d_high")
        if close < prev_low:
            events.append("break_20d_low")
        if boll_up and close > boll_up:
            events.append("above_boll_upper")
        if boll_low and close < boll_low:
            events.append("below_boll_lower")
        if close > 0:
            grid = 10 ** max(len(str(int(close))) - 3, 0)
            nearest = round(close / grid) * grid
            dist = abs(close - nearest) / close * 100
            if dist <= 0.3:
                events.append(f"near_round_{int(nearest)}")
        if events:
            rows.append({"asset": a, "close": close, "events": events,
                         "prev_high": prev_high, "prev_low": prev_low,
                         "boll_upper": boll_up, "boll_lower": boll_low})
    if not rows:
        return FeatureResult.no_data(
            "F23", "关键位：无突破事件", "20 日高低点 / 布林带均未触发；也可能是日线样本不足",
            evidence=ctx.evidence(table="kline_bars", window="20d"),
        )
    breaks = [r for r in rows if any(e.startswith("break") for e in r["events"])]
    return FeatureResult.ok(
        "F23", f"关键位：{len(breaks)} 个资产突破 20 日区间", {"events": rows},
        evidence=ctx.evidence(table="kline_bars", window="20d"), sample_size=len(rows),
        severity="warn" if breaks else "info",
    )


# ── F24 量价背离 ────────────────────────────────────────────────────────
@feature("F24")
def price_volume_divergence(ctx) -> FeatureResult:
    rows = []
    for a in ctx.assets():
        bars = ctx.klines(a, "1d", 25)
        if len(bars) < 15:
            continue
        vols = [b["volume"] for b in bars if b["volume"]]
        if len(vols) < 12:
            continue
        avg_vol = mean(vols[-10:])
        last = bars[-1]
        prev = bars[-2]
        chg = pct_change(last["close"], prev["close"])
        if avg_vol is None or chg is None:
            continue
        vol_x = last["volume"] / avg_vol if last["volume"] and avg_vol else None
        high10 = max(b["close"] for b in bars[-11:-1])
        low10 = min(b["close"] for b in bars[-11:-1])
        signal = None
        if vol_x and vol_x >= 1.5 and abs(chg) <= 0.3:
            signal = "放量滞涨"
        elif vol_x and vol_x <= 0.7 and last["close"] >= high10:
            signal = "缩量新高"
        elif vol_x and vol_x <= 0.7 and last["close"] <= low10:
            signal = "缩量新低"
        if signal:
            rows.append({"asset": a, "signal": signal, "volume_ratio": vol_x,
                         "change_pct": chg, "close": last["close"]})
    if not rows:
        return FeatureResult.ok(
            "F24", "量价背离：无背离信号", {"signals": [], "checked": len(ctx.assets())},
            evidence=ctx.evidence(table="kline_bars", window="10d"),
        )
    return FeatureResult.ok(
        "F24", f"量价背离：{len(rows)} 个资产趋势衰竭信号", {"signals": rows},
        evidence=ctx.evidence(table="kline_bars", window="10d"), sample_size=len(rows),
        severity="watch",
    )


# ── F26 大额净流入流出 ──────────────────────────────────────────────────
@feature("F26")
def net_flow(ctx) -> FeatureResult:
    rows = []
    for a in ("BTC", "ETH", "SOL", "XAU", "WTI"):
        series = [r for r in ctx.macro_series(f"{a}.cvd", 200) if _num(r["value"]) is not None]
        if len(series) < 24:
            continue
        vals = [float(r["value"]) for r in series]
        rows.append({
            "asset": a, "cvd_now_pct": vals[-1], "cvd_1h_avg": mean(vals[-4:]),
            "cvd_4h_avg": mean(vals[-16:]), "cvd_1d_avg": mean(vals[-96:]) if len(vals) >= 96 else None,
        })
    if not rows:
        return FeatureResult.no_data(
            "F26", "净流入：无 CVD 序列", "macro_snapshots 缺 */cvd（主动买卖净额）数据",
            evidence=ctx.evidence(table="macro_snapshots"),
        )
    big = [r for r in rows if r["cvd_now_pct"] is not None and abs(r["cvd_now_pct"]) >= 2]
    return FeatureResult.ok(
        "F26", f"资金流向：{len(big)} 个品种 1h 净流入超 2%", {"assets": rows, "large": big},
        evidence=ctx.evidence(table="macro_snapshots", metric="*.cvd"), sample_size=len(rows),
        severity="warn" if big else "info",
    )


# ── F28 稳定币与基差 ────────────────────────────────────────────────────
@feature("F28")
def stablecoin_basis(ctx) -> FeatureResult:
    supply = ctx.external("stable_supply_total")
    chg30 = ctx.external("stable_supply_30d_pct")
    usdt = ctx.external("sc_usdt_mcap")
    usdc = ctx.external("sc_usdc_mcap")
    basis = {}
    for a in ("BTC", "ETH", "SOL"):
        row = ctx.external("bn_basis_pct", a)
        if row:
            v = _num(row["value"])
            basis[a] = (v * 100 if v is not None and abs(v) < 1 else v)
    etf = ctx.macro_latest("flow", "btc_etf_net")
    etf_ok = etf is not None and etf.get("value") is not None
    if not supply and not basis:
        return FeatureResult.no_data(
            "F28", "稳定币与基差：无链上/基差数据",
            "external_indicators 缺 stable_supply_total 与 bn_basis_pct",
            evidence=ctx.evidence(table="external_indicators"),
        )
    flags = []
    chg_val = _num(chg30["value"]) if chg30 else None
    if chg_val is not None and abs(chg_val) >= 3:
        flags.append("stable_supply_shift")
    for a, bp in basis.items():
        if bp is not None and abs(bp) >= 0.5:
            flags.append(f"basis_{a}")
    value = {
        "stable_supply_total": _num(supply["value"]) if supply else None,
        "stable_supply_30d_pct": chg_val,
        "usdt_mcap": _num(usdt["value"]) if usdt else None,
        "usdc_mcap": _num(usdc["value"]) if usdc else None,
        "perp_basis_pct": basis,
        "btc_etf_net": etf.get("value") if etf_ok else None,
        "flags": flags,
        "caveat": "BTC ETF 净流入数据源未配置（SOSOVALUE_ETF_URL 空），该项留空",
    }
    return FeatureResult.ok(
        "F28", f"稳定币与基差：{len(flags)} 个信号", value,
        evidence=ctx.evidence(tables=["external_indicators", "macro_snapshots"]),
        sample_size=len(basis), severity="warn" if flags else "info",
    )
