"""
P1-B 市场基本面检测 (Market Regime Detection)
==============================================

本模块由两条互补的能力线合并而成 (2026-10 双向同步; 此前同名文件互相覆盖):

① COT / funding regime 检测 (P1-B / P1-D)
基于 CFTCCOT 数据 (cftc_cot_data) 判定 7 类市场 regime:
  1. leverage_buildup       — 机构开杠杆 (OI 单周↑5% + 多头持仓↑)
  2. deleveraging           — 套利离场/去杠杆 (OI 单周↓5% + 多头持仓↓)
  3. large_spec_long        — 机构大资金做多 (净多持仓 > 历史 80% 分位)
  4. large_spec_short       — 机构大资金做空 (净空持仓 < 历史 20% 分位)
  5. commercial_hedging     — 产业套保激增 (commercial_net 周变化 > 0.05)
  6. risk_off_rotation      — 集体撤资到避险 (crypto OI↓ & 贵金属 OI↑)
  7. spec_extreme           — 极端投机 (净持仓处于历史 95% 分位以上)

P1-D 新增 2 类 funding rate 衍生 regime (基于 Binance 永续合约资金费率):
  8. funding_extreme_long   — 资金费率极正 (current rate ≥ 0.03%/8h ≈ 33% APR+)
  9. funding_extreme_short  — 资金费率极负 (current rate ≤ -0.03%/8h)

输入: cftc_cot_data 表 (13 个资产 × 156 周) + Binance 永续合约 funding rate
输出: dict 含 regimes 列表 + 每 regime 涉及资产 + score + 历史百分位
主入口: detect_regimes()

② 价格-资金背离 (纯函数, 零网络依赖)
  detect_price_fund_divergence() 把「价格方向 vs 资金方向」摆在一起做背离判定，
  供 decision_guard 在方向门槛上做「松绑/翻转」。

  背景: decision_guard 里的 trend 因子是「过去窗口期涨跌 + 连续同向 K 线数」的
  滞后确认, 趋势反转初期仍输出旧方向 (Strong Bull/Bull), 把 AI 抓到的反向信号
  抵消成 HOLD。资金面 (永续资金费率 / 基差 / 持仓) 更灵敏, 能在价格还没掉头时
  先给出领先信号。

  边界: 只做静态方向背离 (当前费率方向 vs 当前价格趋势), 不依赖历史费率序列;
  basis_bps / oi_change_pct 作为可选增强, 缺失时自动跳过对应信号。
  数据由上层 (market_snapshot / macro_context) 拉取后传入。
"""
from __future__ import annotations
import sqlite3
from typing import Any, Dict, List, Optional

# Regime 判定阈值
LEVERAGE_OI_THRESHOLD = 0.05   # OI 周变化 ±5% 触发杠杆/去杠杆
COMM_HEDGE_THRESHOLD = 0.05    # 产业套保单周变化 > 5% OI 触发
FUNDING_EXTREME_LONG = 0.0003  # funding rate > 0.03%/8h 触发 long_pay 极端 (≈33% APR)
FUNDING_EXTREME_SHORT = -0.0003  # funding rate < -0.03%/8h 触发 short_pay 极端
SPEC_PERCENTILE_LONG = 0.80    # 大资金做多阈值 (历史 80% 分位)
SPEC_PERCENTILE_SHORT = 0.20   # 大资金做空阈值 (历史 20% 分位)
SPEC_EXTREME_PCTL = 0.95       # 极端投机阈值 (95% 分位)
RISK_OFF_CUTOFF = "2024-01-01" # 历史百分位计算窗口
MIN_REPORTS = 20               # 至少 20 周历史才能判定 percentile


def _safe_pct(numerator, denom) -> float:
    if numerator is None or denom is None or denom == 0:
        return 0.0
    return float(numerator) / float(denom)


def _percentile_rank(value: float, history: list) -> float:
    """返回 value 在 history 中的百分位 (0-1, 越高越极端)."""
    if not history or value is None:
        return 0.5
    below = sum(1 for h in history if h is not None and h < value)
    return below / len(history)


def _fetch_cot_rows(conn: sqlite3.Connection, asset: str | None = None,
                    since_date: str = "2024-01-01") -> list:
    """拉取 COT 数据, 按 asset+report_date 倒序."""
    if asset:
        rows = conn.execute(
            "SELECT asset, report_date, open_interest, change_oi, "
            "noncomm_net, comm_net, spec_index, pct_oi_noncomm_long, pct_oi_noncomm_short "
            "FROM cftc_cot_data WHERE asset=? AND report_date >= ? ORDER BY report_date DESC",
            (asset, since_date),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT asset, report_date, open_interest, change_oi, "
            "noncomm_net, comm_net, spec_index, pct_oi_noncomm_long, pct_oi_noncomm_short "
            "FROM cftc_cot_data WHERE report_date >= ? ORDER BY asset, report_date DESC",
            (since_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def _asset_history_spec_pct(asset_rows: list) -> list:
    """提取某资产历史 noncomm_net / open_interest 时间序列 (ASC)."""
    return [_safe_pct(r["noncomm_net"], r["open_interest"])
            for r in reversed(asset_rows) if r["open_interest"]]


def _detect_leverage_buildup(asset_rows: list) -> dict | None:
    """regime 1+2: leverage_buildup / deleveraging (最近 1 周 OI 变化)."""
    if not asset_rows:
        return None
    latest = asset_rows[0]
    if latest["open_interest"] is None or latest["change_oi"] is None:
        return None
    oi_chg_pct = _safe_pct(latest["change_oi"], latest["open_interest"])
    if abs(oi_chg_pct) < LEVERAGE_OI_THRESHOLD:
        return None
    prev = asset_rows[1] if len(asset_rows) > 1 else None
    spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
    spec_prev = _safe_pct(prev["noncomm_net"], prev["open_interest"]) if prev else 0
    spec_delta = spec_now - spec_prev
    regime = "leverage_buildup" if oi_chg_pct > 0 else "deleveraging"
    return {
        "asset": latest["asset"],
        "regime": regime,
        "report_date": latest["report_date"],
        "oi_chg_pct": round(oi_chg_pct * 100, 2),
        "spec_net_pct": round(spec_now * 100, 2),
        "spec_delta_pct": round(spec_delta * 100, 2),
        "intensity": "strong" if abs(oi_chg_pct) > 0.10 else "moderate",
        "score": min(1.0, abs(oi_chg_pct) / 0.20),
    }


def _detect_large_spec(asset_rows: list) -> dict | None:
    """regime 3+4: large_spec_long / large_spec_short (基于历史百分位)."""
    if len(asset_rows) < MIN_REPORTS:
        return None
    history = _asset_history_spec_pct(asset_rows)
    latest = asset_rows[0]
    spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
    pctl = _percentile_rank(spec_now, history)
    if pctl >= SPEC_PERCENTILE_LONG:
        regime = "large_spec_long"
    elif pctl <= SPEC_PERCENTILE_SHORT:
        regime = "large_spec_short"
    else:
        return None
    return {
        "asset": latest["asset"],
        "regime": regime,
        "report_date": latest["report_date"],
        "spec_net_pct": round(spec_now * 100, 2),
        "percentile": round(pctl * 100, 1),
        "intensity": "extreme" if pctl >= SPEC_EXTREME_PCTL or pctl <= (1 - SPEC_EXTREME_PCTL) else "moderate",
        "score": abs(pctl - 0.5) * 2,
    }


def _detect_commercial_hedging(asset_rows: list) -> dict | None:
    """regime 5: commercial_hedging (产业套保单周变化 > 5% OI)."""
    if len(asset_rows) < 2:
        return None
    latest = asset_rows[0]
    prev = asset_rows[1]
    if latest["open_interest"] is None or prev["open_interest"] is None:
        return None
    comm_now = _safe_pct(latest["comm_net"], latest["open_interest"])
    comm_prev = _safe_pct(prev["comm_net"], prev["open_interest"])
    comm_delta = comm_now - comm_prev
    if abs(comm_delta) < COMM_HEDGE_THRESHOLD:
        return None
    direction = "increasing" if comm_delta > 0 else "decreasing"
    return {
        "asset": latest["asset"],
        "regime": "commercial_hedging",
        "report_date": latest["report_date"],
        "comm_delta_pct": round(comm_delta * 100, 2),
        "comm_now_pct": round(comm_now * 100, 2),
        "direction": direction,
        "intensity": "strong" if abs(comm_delta) > 0.10 else "moderate",
        "score": min(1.0, abs(comm_delta) / 0.20),
    }


def _detect_risk_off_rotation(all_rows_by_asset: dict) -> dict | None:
    """regime 6: risk_off_rotation (crypto OI↓ + 贵金属 OI↑)."""
    crypto_assets = {"BTC", "ETH"}
    safe_assets = {"GOLD", "SILVER"}
    crypto_latest = [all_rows_by_asset[a][0] for a in crypto_assets
                     if a in all_rows_by_asset and all_rows_by_asset[a]]
    safe_latest = [all_rows_by_asset[a][0] for a in safe_assets
                   if a in all_rows_by_asset and all_rows_by_asset[a]]
    if not crypto_latest or not safe_latest:
        return None
    crypto_oi_chg = sum(_safe_pct(r["change_oi"], r["open_interest"])
                        for r in crypto_latest) / len(crypto_latest)
    safe_oi_chg = sum(_safe_pct(r["change_oi"], r["open_interest"])
                      for r in safe_latest) / len(safe_latest)
    if crypto_oi_chg > -LEVERAGE_OI_THRESHOLD or safe_oi_chg < 0.03:
        return None
    return {
        "regime": "risk_off_rotation",
        "report_date": max(r["report_date"] for r in crypto_latest + safe_latest),
        "crypto_oi_chg_pct": round(crypto_oi_chg * 100, 2),
        "safe_oi_chg_pct": round(safe_oi_chg * 100, 2),
        "assets_crypto": [r["asset"] for r in crypto_latest],
        "assets_safe": [r["asset"] for r in safe_latest],
        "intensity": "strong" if crypto_oi_chg < -0.10 and safe_oi_chg > 0.10 else "moderate",
        "score": min(1.0, (-crypto_oi_chg + safe_oi_chg) / 0.30),
    }


def _detect_spec_extreme(asset_rows: list) -> list:
    """regime 7: spec_extreme (净持仓 > 95% 分位) — 可能反转信号."""
    if len(asset_rows) < MIN_REPORTS:
        return []
    history = _asset_history_spec_pct(asset_rows)
    latest = asset_rows[0]
    spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
    pctl = _percentile_rank(spec_now, history)
    if pctl < SPEC_EXTREME_PCTL and pctl > (1 - SPEC_EXTREME_PCTL):
        return []
    side = "long" if spec_now > 0 else "short"
    return [{
        "asset": latest["asset"],
        "regime": "spec_extreme",
        "report_date": latest["report_date"],
        "side": side,
        "spec_net_pct": round(spec_now * 100, 2),
        "percentile": round(pctl * 100, 1),
        "score": 1.0,
        "intensity": "extreme",
    }]


def _detect_funding_extremes() -> list:
    """regime 8/9: funding_extreme_long/short (基于 Binance 永续合约资金费率).

    拉取 BTCUSDT/ETHUSDT/SOLUSDT/BNBUSDT 的当前资金费率;
    若 ≥ FUNDING_EXTREME_LONG (≈33% APR) ⇒ funding_extreme_long
    若 ≤ FUNDING_EXTREME_SHORT               ⇒ funding_extreme_short
    """
    try:
        from funding_rate import get_funding_metrics
    except ImportError:
        return []
    out: list = []
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"):
        try:
            m = get_funding_metrics(sym)
        except Exception:
            continue
        if not m or not m.get("ok"):
            continue
        cur = m.get("current_rate", 0.0)
        ann = m.get("annualized_current_pct", 0.0)
        ts = m.get("ts")
        if cur >= FUNDING_EXTREME_LONG:
            out.append({
                "asset": sym,
                "regime": "funding_extreme_long",
                "report_date": ts,
                "side": "long_pay",
                "current_rate": round(cur, 6),
                "annualized_pct": round(ann, 2),
                "score": min(1.0, abs(cur) / 0.001),
                "intensity": "extreme",
                "data_source": "binance_fapi_fundingRate",
            })
        elif cur <= FUNDING_EXTREME_SHORT:
            out.append({
                "asset": sym,
                "regime": "funding_extreme_short",
                "report_date": ts,
                "side": "short_pay",
                "current_rate": round(cur, 6),
                "annualized_pct": round(ann, 2),
                "score": min(1.0, abs(cur) / 0.001),
                "intensity": "extreme",
                "data_source": "binance_fapi_fundingRate",
            })
    return out


def detect_regimes(conn: sqlite3.Connection, since_date: str = RISK_OFF_CUTOFF) -> dict:
    """主入口: 检测所有 9 类 regime (7 COT + 2 funding)."""
    all_rows = _fetch_cot_rows(conn, asset=None, since_date=since_date)
    if not all_rows:
        return {"ok": False, "error": "no_cot_data", "regimes": [], "by_asset": {}}

    by_asset: dict = {}
    for r in all_rows:
        by_asset.setdefault(r["asset"], []).append(r)

    regimes: list = []
    for asset, rows in by_asset.items():
        lv = _detect_leverage_buildup(rows)
        if lv:
            regimes.append(lv)
        ls = _detect_large_spec(rows)
        if ls:
            regimes.append(ls)
        ch = _detect_commercial_hedging(rows)
        if ch:
            regimes.append(ch)
        regimes.extend(_detect_spec_extreme(rows))

    ro = _detect_risk_off_rotation(by_asset)
    if ro:
        regimes.append(ro)

    regimes.extend(_detect_funding_extremes())

    regimes.sort(key=lambda r: r.get("score", 0), reverse=True)

    asset_summary: dict = {}
    for asset, rows in by_asset.items():
        if not rows:
            continue
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        spec_now = _safe_pct(latest["noncomm_net"], latest["open_interest"])
        spec_prev = _safe_pct(prev["noncomm_net"], prev["open_interest"]) if prev else 0
        oi_chg = _safe_pct(latest["change_oi"], latest["open_interest"])
        asset_summary[asset] = {
            "report_date": latest["report_date"],
            "open_interest": latest["open_interest"],
            "oi_chg_pct": round(oi_chg * 100, 2),
            "spec_net_pct": round(spec_now * 100, 2),
            "spec_delta_pct": round((spec_now - spec_prev) * 100, 2),
        }

    return {
        "ok": True,
        "n_regimes": len(regimes),
        "regimes": regimes,
        "by_asset": asset_summary,
        "since_date": since_date,
        "n_assets": len(by_asset),
    }


if __name__ == "__main__":
    import json
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "trident_event_bus.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = detect_regimes(conn)
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ─── 价格-资金背离 (纯函数) ──────────────────────────
def _clamp(v: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(v)))
    except (TypeError, ValueError):
        return 0.0


def detect_price_fund_divergence(
    *,
    trend: str = "",
    funding_rate_pct: float = 0.0,
    basis_bps: Optional[float] = None,
    oi_change_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """判定价格 vs 资金的方向背离。

    参数:
      trend            价格趋势标签 (_compute_period_stats / stats_7d.trend)
      funding_rate_pct 永续资金费率 (%, 正=多头付溢价, 负=空头主导)
      basis_bps        基差 (bp, 正=升水, 负=贴水), 可选
      oi_change_pct    持仓变化 (%, 正=增仓, 负=减仓), 可选

    返回:
      divergence "bearish"|"bullish"|None   bearish=顶背离(价格涨但资金空),
                                            bullish=底背离(价格跌但资金多)
      strength   0~1 背离强度 (费率幅度归一化 + 多信号加成)
      signals    命中的背离信号描述
      regime     机器标签
      verdict    人读说明
    """
    t = (trend or "").lower()
    price_up = "bull" in t
    price_down = "bear" in t

    try:
        fr = float(funding_rate_pct)
    except (TypeError, ValueError):
        fr = 0.0

    divergence: Optional[str] = None
    signals: List[str] = []

    # ① 费率背离 (核心信号)
    if price_up and fr < 0:
        divergence = "bearish"
        signals.append(f"价格偏强({trend})但资金费率转负 {fr * 100:.4f}%")
    elif price_down and fr > 0:
        divergence = "bullish"
        signals.append(f"价格偏弱({trend})但资金费率转正 +{fr * 100:.4f}%")

    # ② 基差背离 (可选增强)
    if basis_bps is not None:
        try:
            bps = float(basis_bps)
        except (TypeError, ValueError):
            bps = None
        if bps is not None:
            if price_up and bps < 0:
                divergence = "bearish"
                signals.append(f"基差转负 {bps:.1f}bp (期货贴水)")
            elif price_down and bps > 0:
                divergence = "bullish"
                signals.append(f"基差转正 +{bps:.1f}bp (期货升水)")

    # ③ 持仓背离 (可选增强)
    if oi_change_pct is not None:
        try:
            oi = float(oi_change_pct)
        except (TypeError, ValueError):
            oi = None
        if oi is not None:
            if price_up and oi < 0:
                divergence = "bearish"
                signals.append(f"持仓下降 {oi:.1f}%")
            elif price_down and oi > 0:
                divergence = "bullish"
                signals.append(f"持仓上升 +{oi:.1f}%")

    if divergence is None:
        return {
            "divergence": None,
            "strength": 0.0,
            "signals": [],
            "regime": "neutral",
            "verdict": "价格与资金方向一致，无背离",
        }

    fr_strength = _clamp(abs(fr) / 0.1)      # 0.1% 费率 → 满强度
    extra = 0.15 * max(0, len(signals) - 1)  # 每多一条信号 +0.15
    strength = _clamp(fr_strength + extra)

    return {
        "divergence": divergence,
        "strength": round(strength, 3),
        "signals": signals,
        "regime": f"price_fund_{divergence}",
        "verdict": "；".join(signals),
    }

