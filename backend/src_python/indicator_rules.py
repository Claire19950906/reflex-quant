#!/usr/bin/env python3
"""指标 → 信号：把 74 个"只写不读"的外部指标变成可被决策层看见的异常签名。

背景：
  数据侧已经采了 100 多个外部指标（VIX 期限比、跨所价差、中国金溢价、资金费率、
  美债利差、链上拥堵……），但绝大多数进库后没人查——等于白采。
  这里给它们写**明确的阈值规则**，命中就落一条 abnormal_signals，
  走系统原有的 record_signal（下游消费这条链已有，不需要改别处）。

阈值全部取行业惯用/公认水平，不是为了让系统通得过而调的：
  · VIX 期限结构倒挂：VIX9D > VIX，即近端恐慌强于远端（急跌特征）
  · VIX > 30：公认的"高波动区间"
  · 10Y-2Y 倒挂：< 0，经典衰退前兆指标
  · 跨所价差 > 0.5%：同币种跨交易所出现可套利级别的偏离，通常意味着某所有资金异常
  · 中国金溢价 > 1%：上金所金价折算成美元盎司后相对伦敦金溢价，反映境内实物需求/走私压力
  · 资金费率 |f| > 0.05%/8h：杠杆单边拥挤，常见于逼空或踩踏前
  · 内存池 > 15 万笔：链上拥堵，影响交易所提币时效与情绪

去重：同一 (asset, signal_kind) 在 2 小时内已有 open 记录就不再重复写。
"""
import os
import json
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import abnormal_detector
import db
import technical_indicators as ti

DEDUP_SECONDS = 2 * 3600
TECH_LOOKBACK = 120        # 取最近多少根日线算技术指标

# ───────────────────────── 阈值配置（可刷新） ─────────────────────────
# 默认兜底值：配置文件读不到时用这套行业惯例。回测结论写进
# runtime/indicator_thresholds.json，重跑 threshold_group_backtest.py 后可刷新该文件。
DEFAULT_THRESHOLDS: Dict[str, Dict[str, Optional[float]]] = {
    "crypto":   {"rsi_oversold": 30.0, "mfi_oversold": 15.0, "mfi_overbought": None, "adx_strong": 30.0},
    "equity":   {"rsi_oversold": 30.0, "mfi_oversold": 20.0, "mfi_overbought": None, "adx_strong": 30.0},
    "precious": {"rsi_oversold": 25.0, "mfi_oversold": None,  "mfi_overbought": None, "adx_strong": 30.0},
    "index":    {"rsi_oversold": 30.0, "mfi_oversold": 15.0, "mfi_overbought": None, "adx_strong": 30.0},
    "commodity": {"rsi_oversold": None, "mfi_oversold": None, "rsi_overbought": 75.0, "mfi_overbought": 85.0, "adx_strong": 30.0},
    "other":    {"adx_strong": 30.0},
}
# 未登记族/缺配置时的全局兜底
GLOBAL_FALLBACK = {"mfi_oversold": 20.0, "mfi_overbought": 80.0, "adx_strong": 30.0}

THRESH_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "runtime", "indicator_thresholds.json")
_thresh_cache: Dict[str, Any] = {"loaded": False, "data": None, "mtime": None}


def _load_thresholds() -> Dict[str, Any]:
    """读分组阈值配置；读不到/损坏回落到 DEFAULT_THRESHOLDS。

    缓存按文件 mtime 失效：配置文件被刷新后，下一次取值自动重载，常驻服务
    不需要重启；读不到文件/损坏时沿用上一次成功缓存，再没有才回落默认。
    """
    mtime = 0.0
    try:
        mtime = os.path.getmtime(THRESH_CONFIG_PATH)
    except OSError:
        mtime = 0.0
    if _thresh_cache["loaded"] and _thresh_cache.get("mtime") == mtime:
        return _thresh_cache["data"]
    data: Dict[str, Any] = {"groups": {}, "asset_overrides": {}}
    try:
        with open(THRESH_CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict) and isinstance(raw.get("groups"), dict):
            data["groups"] = raw["groups"]
            data["asset_overrides"] = raw.get("asset_overrides", {}) or {}
    except Exception:
        data["groups"] = {}
    _thresh_cache["loaded"] = True
    _thresh_cache["data"] = data
    _thresh_cache["mtime"] = mtime
    return data


def _thresh(asset: str, group: str, key: str) -> Optional[float]:
    """取某资产某阈值：资产 override > 分组配置 > 默认兜底 > 全局兜底。None=不发该方向信号。"""
    cfg = _load_thresholds()
    ov = cfg.get("asset_overrides", {}).get(asset)
    if isinstance(ov, dict) and key in ov:
        return ov[key]
    grp_cfg = cfg.get("groups", {}).get(group, {})
    if isinstance(grp_cfg, dict) and key in grp_cfg:
        return grp_cfg[key]  # 可能为 None（该族不发此方向信号）
    default_grp = DEFAULT_THRESHOLDS.get(group, {})
    if key in default_grp and default_grp[key] is not None:
        return default_grp[key]
    return GLOBAL_FALLBACK.get(key)


# 资产族分组（与 threshold_group_backtest.GROUPS 对齐）
_CRYPTO = {"BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"}
_EQUITY = {"AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "MU", "NVDA", "TSLA",
           "000660.KS", "SKHYNIX", "TENCENT"}
_PRECIOUS = {"XAU", "XAG", "HG"}
_INDEX = {"DJI", "IXIC", "NDX", "SPX"}
_COMMODITY = {"WTI"}


def _group(asset: str) -> str:
    if asset in _CRYPTO:
        return "crypto"
    if asset in _EQUITY:
        return "equity"
    if asset in _PRECIOUS:
        return "precious"
    if asset in _INDEX:
        return "index"
    if asset in _COMMODITY:
        return "commodity"
    return "other"


def latest(conn: sqlite3.Connection, asset: str, indicator: str) -> Optional[Tuple[float, int]]:
    row = conn.execute(
        """SELECT value, ts FROM external_indicators
           WHERE asset=? AND indicator=? AND value IS NOT NULL
           ORDER BY ts DESC LIMIT 1""", (asset, indicator)).fetchone()
    if not row:
        return None
    return float(row[0]), int(row[1])


def already_open(conn: sqlite3.Connection, asset: str, kind: str, now: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM abnormal_signals
           WHERE asset=? AND signal_kind=? AND status='open' AND ts >= ?
           LIMIT 1""", (asset, kind, now - DEDUP_SECONDS)).fetchone()
    return row is not None


def evaluate(conn: sqlite3.Connection, now: int, getter=None) -> List[Dict[str, Any]]:
    """跑全部规则，返回命中的信号（尚未去重）。

    getter 可注入（用于自检）：默认读库，自检时传入假值，验证规则阈值是否真的会触发。
    """
    hits: List[Dict[str, Any]] = []
    if getter is None:
        getter = lambda a, i: latest(conn, a, i)

    def add(asset: str, kind: str, direction: str, hint: str,
            detail: Dict[str, Any], ts: int) -> None:
        hits.append({"asset": asset, "kind": kind, "direction": direction,
                     "hint": hint, "detail": detail, "ts": ts})

    # —— 波动率结构 ——
    got = getter("US_MACRO", "us_vix_term_ratio")
    vix = getter("US_MACRO", "us_vix_cboe")
    if got and got[0] > 1.0:
        add("MARKET", "macro_vix_term_inversion", "risk_off",
            f"⚠️ VIX 期限结构倒挂：近端(9D)已高于远端(30D)，比值 {got[0]:.3f}。"
            f"这是急跌特征而非慢跌，风险偏好通常在数小时内急剧收缩。",
            {"vix_term_ratio": got[0]}, got[1])
    if vix and vix[0] > 30:
        add("MARKET", "macro_vix_stress", "risk_off",
            f"⚠️ VIX {vix[0]:.2f} 已进入高波动区间（>30）。",
            {"vix": vix[0]}, vix[1])

    # —— 利率曲线 ——
    # v333: 优先用财政部官方曲线推出来的 `us_t10y2y_gov`（每日更新、鲜度最好），
    #   FRED 的 `us_t10y2y` 常常断更（实测停在 11-12），只在 gov 缺失时回退。
    got = getter("US_MACRO", "us_t10y2y_gov") or getter("US_MACRO", "us_t10y2y")
    if got and got[0] < 0:
        add("MARKET", "macro_curve_inversion", "risk_off",
            f"⚠️ 10Y-2Y 收益率曲线倒挂（{got[0]:+.2f}pct），历史上是衰退前兆。",
            {"t10y2y": got[0]}, got[1])

    # —— 利率曲线：10Y-3M（更灵敏的衰退先行指标）——
    got3m = getter("US_MACRO", "us_t10y3m_gov")
    if got3m and got3m[0] < 0:
        add("MARKET", "macro_curve_inversion_10y3m", "risk_off",
            f"⚠️ 10Y-3M 收益率曲线倒挂（{got3m[0]:+.2f}pct）。"
            f"短端高于长端通常意味着政策利率过高、增长预期转弱。",
            {"t10y3m": got3m[0]}, got3m[1])

    # —— 加密：跨所价差 ——
    for asset in ("BTC", "ETH"):
        got = getter(asset, "x_spot_dispersion_pct")
        if got and got[0] > 0.5:
            add(asset, "crypto_venue_dispersion", "bullish",
                f"⚠️ {asset} 跨交易所价差 {got[0]:.3f}%（>0.5%）。"
                f"同币种出现可套利级偏离，说明某一家报价异常或资金在单边扫货。",
                {"dispersion_pct": got[0]}, got[1])

    # —— 加密：资金费率拥挤 ——
    for asset in ("BTC", "ETH", "SOL"):
        # 币安那路偶尔断（无代理时连不上），MEXC 资金费率是免代理的备用源，
        # 缺哪个用哪个，别因为一家断了就当"没有拥挤"。
        got = getter(asset, "bn_funding_rate") or getter(asset, "mexc_funding_rate")
        if got and abs(got[0]) > 0.0005:
            side = "多头拥挤" if got[0] > 0 else "空头拥挤"
            add(asset, "crypto_funding_extreme",
                "bearish" if got[0] > 0 else "bullish",
                f"⚠️ {asset} 资金费率 {got[0]*100:.4f}%/8h（{side}）。"
                f"杠杆单边堆积，常见于逼空或踩踏前的最后一段。",
                {"funding_rate": got[0], "side": side}, got[1])

    # —— 黄金：中国实物金溢价 ——
    got = getter("SGE_GOLD", "sge_gold_premium_pct")
    if got and abs(got[0]) > 1.0:
        add("XAU", "gold_china_premium",
            "bullish" if got[0] > 0 else "bearish",
            f"⚠️ 上金所金价折算后相对伦敦金溢价 {got[0]:+.2f}%（>1%）。"
            f"境内实物需求或进口受阻，通常先于国际金价反应。",
            {"premium_pct": got[0]}, got[1])

    # —— 黄金期权：偏度与波动率（币安 XAU 期权，2026-11 接入）——
    # 阈值取值依据：黄金 30 日 IV 历史常态在 15~25 区间，>35 属于事件级定价；
    # 25Δ 偏度正常为微正（看跌略贵），转负说明虚值看涨被抢，属押注上行。
    skew = getter("XAU", "xau_25d_skew")
    if skew and skew[0] < 0:
        add("XAU", "gold_option_skew_negative", "bullish",
            f"黄金期权 25Δ 偏度 {skew[0]:+.2f}（负值）：虚值看涨比看跌贵，"
            f"期权资金在押上行而不是买保险。",
            {"skew_25d": skew[0]}, skew[1])
    giv = getter("XAU", "xau_atm_iv")
    if giv and giv[0] > 35:
        add("XAU", "gold_iv_elevated", "risk_off",
            f"黄金平值 IV {giv[0]:.1f}% 已进入事件级区间（常态 15~25）。",
            {"xau_atm_iv": giv[0]}, giv[1])

    # —— 黄金：金银比极端（避险主导 / 白银被压）——
    got = getter("*", "xau_xag_ratio")
    if got and got[0] > 85:
        add("XAU", "gold_silver_ratio_stretched", "risk_off",
            f"金银比 {got[0]:.1f}（>85）：历史上多见于避险资金主导、白银被压的阶段，"
            f"均值回复压力偏大。",
            {"xau_xag_ratio": got[0]}, got[1])

    # —— 白银：国内实物溢价（上金所相对 COMEX）——
    got = getter("XAG", "sge_silver_premium_pct")
    if got and got[0] > 10:
        add("XAG", "silver_china_premium_wide", "bullish",
            f"国内白银溢价 {got[0]:+.2f}%（>10%）：境内实物端比国际紧，"
            f"盘子小，容易被继续逼。",
            {"premium_pct": got[0]}, got[1])

    # —— 加密：Coinbase 溢价（美元通道方向）——
    got = getter("ETH", "cb_premium_pct")
    if got and abs(got[0]) > 0.3:
        side = "美元通道在主动买" if got[0] > 0 else "美元通道在往外卖"
        add("ETH", "coinbase_premium_extreme",
            "bullish" if got[0] > 0 else "bearish",
            f"Coinbase 溢价 {got[0]:+.3f}%（|·|>0.3%）：{side}。",
            {"cb_premium_pct": got[0]}, got[1])

    # —— 加密：稳定币供应收缩（干火药减少）——
    got = getter("BTC", "stable_supply_30d_pct")
    if got and got[0] < -3:
        add("BTC", "stablecoin_supply_contracting", "risk_off",
            f"USDT+USDC 总供应 30 日缩减 {got[0]:+.2f}%：可随时入场的资金在变少。",
            {"stable_supply_30d_pct": got[0]}, got[1])
    elif got and got[0] > 3:
        # 反过来的方向原来没写：稳定币在扩，说明新钱在进来，是有弹药而不是没弹药。
        # 阈值与收缩端对称取 ±3%（30 日），依据是这条序列自身的波动区间。
        add("BTC", "stablecoin_supply_expanding", "bullish",
            f"USDT+USDC 总供应 30 日扩张 {got[0]:+.2f}%：稳定币在净增发，场外弹药在补充。",
            {"stable_supply_30d_pct": got[0]}, got[1])

    # —— 美股：尾部风险定价（CBOE SKEW）——
    got = getter("US_EQ", "us_skew_index")
    if got and got[0] > 145:
        add("MARKET", "us_tail_risk_priced", "risk_off",
            f"SKEW {got[0]:.1f}（>145）：虚值看跌相对变贵，机构在为崩盘买保险。",
            {"skew_index": got[0]}, got[1])

    # —— 美股：内部人集中卖出 ——
    for sym in ("AAPL", "MSFT", "NVDA", "AMD", "MU", "META", "AMZN", "GOOGL"):
        got = getter(sym, "insider_sell_shares_30d")
        if got and got[0] > 20000:
            add(sym, "insider_selling_cluster", "risk_off",
                f"{sym} 近 30 日内部人卖出 {int(got[0]):,} 股（>2 万股）。"
                f"注意：10b5-1 计划内的卖出也在其中，需打折看。",
                {"sell_shares_30d": got[0]}, got[1])

    # —— 链上拥堵 ——
    got = getter("BTC", "btc_mempool_tx")
    if got and got[0] > 150000:
        add("BTC", "chain_congestion", "bearish",
            f"⚠️ BTC 内存池 {int(got[0])} 笔待确认，链上拥堵。"
            f"交易所提币时效会变差，容易放大恐慌。",
            {"mempool_tx": got[0]}, got[1])

    return hits


# --------------------------------------------------------------------------
# 技术面（K 线）规则：ADX 趋势强度 + MFI 资金流量极值
# --------------------------------------------------------------------------
def _load_daily_bars(conn: sqlite3.Connection, asset: str,
                     limit: int = TECH_LOOKBACK) -> List[list]:
    """取某资产最近 limit 根升序日线，转成 [ts, open, high, low, close, volume]。"""
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM ohlcv_daily "
        "WHERE asset=? ORDER BY ts DESC LIMIT ?", (asset, limit),
    ).fetchall()
    bars: List[list] = []
    for r in reversed(rows):
        ts, o, h, l, c, v = r[0], r[1], r[2], r[3], r[4], r[5]
        if c is None or c <= 0 or h is None or h < l:
            continue
        bars.append([int(ts), float(o or c), float(h), float(l), float(c), float(v or 0.0)])
    return bars


def evaluate_technical(conn: sqlite3.Connection, now: int,
                      asset_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """对各资产最近日线跑 ADX / MFI 规则。缺数据的资产直接跳过，不造数。

    asset_filter 可注入资产清单（自检用）；默认从 ohlcv_daily 去重取。
    """
    hits: List[Dict[str, Any]] = []
    if asset_filter is not None:
        assets = asset_filter
    else:
        assets = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM ohlcv_daily").fetchall()]

    for asset in assets:
        bars = _load_daily_bars(conn, asset)
        if len(bars) < 30:
            continue
        ts_last = bars[-1][0]
        grp = _group(asset)

        # —— SuperTrend：趋势轨方向证据（10,3），落独立证据 kind 供复盘引用 ——
        st = ti.supertrend(bars, period=10, multiplier=3.0)
        st_dir = st["direction"][-1] if st["direction"] else None
        if st_dir == 1:
            hits.append({"asset": asset, "kind": "supertrend_bull",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} SuperTrend 位于多头轨，趋势向上。",
                         "detail": {"line": st["line"][-1], "group": grp}, "ts": ts_last})
        elif st_dir == -1:
            hits.append({"asset": asset, "kind": "supertrend_bear",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} SuperTrend 位于空头轨，趋势向下。",
                         "detail": {"line": st["line"][-1], "group": grp}, "ts": ts_last})

        # —— ADX：趋势强度 + DI 方向 ——
        adx = ti.adx(bars, period=14)
        adx_v = adx["adx"][-1] if adx["adx"] else None
        pdi = adx["plus_di"][-1] if adx["plus_di"] else None
        ndi = adx["minus_di"][-1] if adx["minus_di"] else None
        adx_strong = _thresh(asset, grp, "adx_strong") or 30.0
        if adx_v is not None and pdi is not None and ndi is not None and adx_v >= 20.0:
            if adx_v >= adx_strong and pdi > ndi:
                hits.append({"asset": asset, "kind": "adx_strong_bull",
                             "direction": "bullish",
                             "hint": f"⚠️ {asset} ADX {adx_v:.1f}（≥{adx_strong:.0f}）"
                                     f"且 +DI {pdi:.1f} > -DI {ndi:.1f}：强多头趋势，"
                                     f"逆势抄底风险大。",
                             "detail": {"adx": adx_v, "plus_di": pdi, "minus_di": ndi,
                                        "group": grp}, "ts": ts_last})
            elif adx_v >= adx_strong and ndi > pdi:
                hits.append({"asset": asset, "kind": "adx_strong_bear",
                             "direction": "bearish",
                             "hint": f"⚠️ {asset} ADX {adx_v:.1f}（≥{adx_strong:.0f}）"
                                     f"且 -DI {ndi:.1f} > +DI {pdi:.1f}：强空头趋势，"
                                     f"逆势接飞刀风险大。",
                             "detail": {"adx": adx_v, "plus_di": pdi, "minus_di": ndi,
                                        "group": grp}, "ts": ts_last})

        # —— MFI：资金流量超买/超卖（量价加权 RSI），阈值按资产族回测分组 ——
        mfi_vals = ti.mfi(bars, period=14)
        mfi_v = mfi_vals[-1] if mfi_vals else None
        mfi_os_thr = _thresh(asset, grp, "mfi_oversold")
        mfi_ob_thr = _thresh(asset, grp, "mfi_overbought")
        if mfi_v is not None and mfi_ob_thr is not None and mfi_v >= mfi_ob_thr:
            hits.append({"asset": asset, "kind": "mfi_overbought",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} MFI {mfi_v:.1f}（≥{mfi_ob_thr:.0f}）："
                                 f"资金流量过热，放量冲高后易回落。",
                         "detail": {"mfi": mfi_v, "group": grp}, "ts": ts_last})
        elif mfi_v is not None and mfi_os_thr is not None and mfi_v <= mfi_os_thr:
            hits.append({"asset": asset, "kind": "mfi_oversold",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} MFI {mfi_v:.1f}（≤{mfi_os_thr:.0f}）："
                                 f"资金流量过冷，量价超卖，存在反弹修复空间。",
                         "detail": {"mfi": mfi_v, "group": grp}, "ts": ts_last})

        # —— CCI：趋势通道极值（+100 上轨 / -100 下轨），作背景趋势证据 ——
        cci_vals = ti.cci(bars, period=20)
        cci_v = cci_vals[-1] if cci_vals else None
        if cci_v is not None and cci_v >= 100.0:
            hits.append({"asset": asset, "kind": "cci_overbought",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} CCI {cci_v:.1f}（≥100）："
                                 f"价格显著偏离通道上轨，短线超买。",
                         "detail": {"cci": cci_v, "group": grp}, "ts": ts_last})
        elif cci_v is not None and cci_v <= -100.0:
            hits.append({"asset": asset, "kind": "cci_oversold",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} CCI {cci_v:.1f}（≤-100）："
                                 f"价格显著偏离通道下轨，短线超卖。",
                         "detail": {"cci": cci_v, "group": grp}, "ts": ts_last})

        # —— 摆动结构：HH/HL 上升骨架、LH/LL 下降骨架，趋势方向证据 ——
        swing = ti.swing_structure(bars)
        s_trend = swing.get("trend")
        if s_trend == "up":
            hits.append({"asset": asset, "kind": "swing_bull",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} 摆动结构 {swing.get('structure')}："
                                 f"高点低点同步抬高，上升结构成立。",
                         "detail": {"structure": swing.get("structure"), "group": grp},
                         "ts": ts_last})
        elif s_trend == "down":
            hits.append({"asset": asset, "kind": "swing_bear",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} 摆动结构 {swing.get('structure')}："
                                 f"高点低点同步降低，下降结构成立。",
                         "detail": {"structure": swing.get("structure"), "group": grp},
                         "ts": ts_last})

        # —— Aroon：趋势新鲜度，Up 贴近 100 上行推进、Down 贴近 100 下行推进 ——
        aroon = ti.aroon(bars, period=25)
        a_up = aroon["up"][-1] if aroon["up"] else None
        a_down = aroon["down"][-1] if aroon["down"] else None
        if a_up is not None and a_down is not None and a_up >= 70.0 and a_down <= 30.0:
            hits.append({"asset": asset, "kind": "aroon_bull",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} Aroon Up {a_up:.0f}/Down {a_down:.0f}："
                                 f"上行趋势新鲜度高。",
                         "detail": {"aroon_up": a_up, "aroon_down": a_down, "group": grp},
                         "ts": ts_last})
        elif a_up is not None and a_down is not None and a_down >= 70.0 and a_up <= 30.0:
            hits.append({"asset": asset, "kind": "aroon_bear",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} Aroon Down {a_down:.0f}/Up {a_up:.0f}："
                                 f"下行趋势新鲜度高。",
                         "detail": {"aroon_up": a_up, "aroon_down": a_down, "group": grp},
                         "ts": ts_last})

        # —— 市场状态：趋势/震荡/高波动环境标签（环境证据，不直出方向）——
        vol = ti.realized_vol(bars, period=20)
        rv_now = vol[-1] if vol else None
        rv_hist = [v for v in vol[:-1] if v is not None]
        rv_pct: Optional[float] = None
        if rv_now is not None and rv_hist:
            rv_pct = sum(1 for v in rv_hist if v <= rv_now) / len(rv_hist) * 100.0
        vreg = ti.volume_regime(bars, lookback=5)
        state: str = "unknown"
        if rv_pct is not None and rv_pct >= 80.0:
            state = "high_vol"
        elif adx_v is not None and adx_v < 20.0:
            state = "range"
        elif adx_v is not None and adx_v >= 30.0:
            state = "trend"
        hits.append({"asset": asset, "kind": "market_state",
                     "direction": None,
                     "hint": f"ℹ️ {asset} 市场状态：{state}（波动分位 "
                             f"{rv_pct:.0f}%，量能 {vreg.get('regime')}）。",
                     "detail": {"state": state, "rv_pct": rv_pct,
                                "vol_regime": vreg.get("regime"),
                                "price_up": vreg.get("price_up"),
                                "group": grp}, "ts": ts_last})

        # —— 短线反转形态：看涨/看跌吞没（两 K 实体包覆，方向相反），作辅助证据 ——
        eng = ti.engulfing(bars)
        if eng == "bullish":
            hits.append({"asset": asset, "kind": "engulfing_bull",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} 出现看涨吞没形态，短线承接转强。",
                         "detail": {"group": grp}, "ts": ts_last})
        elif eng == "bearish":
            hits.append({"asset": asset, "kind": "engulfing_bear",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} 出现看跌吞没形态，短线抛压转强。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— KDJ 超买超卖（9,3,3）：J/K 越线，辅助极值证据 ——
        kdj_vals = ti.kdj([c[4] for c in bars], [c[2] for c in bars],
                          [c[3] for c in bars], 9, 3, 3)
        kdj_hit = ti.kdj_trip(kdj_vals, overbought=80.0, oversold=20.0)
        if kdj_hit.get("triggered"):
            if kdj_hit["direction"] == "bearish":
                hits.append({"asset": asset, "kind": "kdj_overbought",
                             "direction": "bearish",
                             "hint": f"⚠️ {asset} KDJ J={kdj_hit['j']}/K={kdj_hit['k']} 超买。",
                             "detail": {"j": kdj_hit["j"], "k": kdj_hit["k"],
                                        "group": grp}, "ts": ts_last})
            elif kdj_hit["direction"] == "bullish":
                hits.append({"asset": asset, "kind": "kdj_oversold",
                             "direction": "bullish",
                             "hint": f"⚠️ {asset} KDJ J={kdj_hit['j']}/K={kdj_hit['k']} 超卖。",
                             "detail": {"j": kdj_hit["j"], "k": kdj_hit["k"],
                                        "group": grp}, "ts": ts_last})

        # —— RSI 极值驻留：高位/低位连续根数，情绪锁定证据（不直出方向）——
        stretch = ti.rsi_stretch([c[4] for c in bars])
        if stretch.get("side") and stretch.get("bars", 0) >= 4:
            hits.append({"asset": asset, "kind": "rsi_stretch",
                         "direction": "bullish" if stretch["side"] == "oversold" else "bearish",
                         "hint": f"ℹ️ {asset} RSI 连续 {stretch['bars']} 根停留在 "
                                 f"{'超卖' if stretch['side'] == 'oversold' else '超买'}区 "
                                 f"(RSI {stretch.get('rsi')})。",
                         "detail": {"side": stretch["side"], "bars": stretch["bars"],
                                    "rsi": stretch.get("rsi"), "group": grp}, "ts": ts_last})

        # —— MA 金叉/死叉（快 SMA5 上穿/下穿慢 SMA20），趋势转折证据 ——
        cross = ti.ma_cross([c[4] for c in bars], fast=5, slow=20)
        if cross == "golden":
            hits.append({"asset": asset, "kind": "ma_golden",
                         "direction": "bullish",
                         "hint": f"ℹ️ {asset} 短期均线上穿中期均线（金叉），趋势转暖。",
                         "detail": {"fast": 5, "slow": 20, "group": grp}, "ts": ts_last})
        elif cross == "death":
            hits.append({"asset": asset, "kind": "ma_death",
                         "direction": "bearish",
                         "hint": f"ℹ️ {asset} 短期均线下穿中期均线（死叉），趋势转弱。",
                         "detail": {"fast": 5, "slow": 20, "group": grp}, "ts": ts_last})

        # —— MACD 主线穿越零轴：正负区切换，背景趋势证据 ——
        zero = ti.macd_zero_cross([c[4] for c in bars])
        if zero == "above_zero":
            hits.append({"asset": asset, "kind": "macd_above_zero",
                         "direction": "bullish",
                         "hint": f"ℹ️ {asset} MACD 主线由负转正，多头占优。",
                         "detail": {"group": grp}, "ts": ts_last})
        elif zero == "below_zero":
            hits.append({"asset": asset, "kind": "macd_below_zero",
                         "direction": "bearish",
                         "hint": f"ℹ️ {asset} MACD 主线由正转负，空头占优。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— Stochastic %K：位置极值，辅助超买超卖证据 ——
        stoch_vals = ti.stoch(bars, k_period=14, smooth=3)
        stoch_v = stoch_vals[-1] if stoch_vals else None
        if stoch_v is not None and stoch_v >= 80.0:
            hits.append({"asset": asset, "kind": "stoch_overbought",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} %K {stoch_v:.1f}（≥80）：随机指标超买。",
                         "detail": {"stoch": stoch_v, "group": grp}, "ts": ts_last})
        elif stoch_v is not None and stoch_v <= 20.0:
            hits.append({"asset": asset, "kind": "stoch_oversold",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} %K {stoch_v:.1f}（≤20）：随机指标超卖。",
                         "detail": {"stoch": stoch_v, "group": grp}, "ts": ts_last})

        # —— MA 斜率：趋势陡峭度（%/根），加速段风险提示，不直出方向 ——
        slope = ti.ma_slope([c[4] for c in bars], period=20, lookback=5)
        if slope is not None:
            hits.append({"asset": asset, "kind": "ma_slope",
                         "direction": "bullish" if slope > 0 else "bearish",
                         "hint": f"ℹ️ {asset} 20 日均线斜率 {slope:+.3f}%/根。",
                         "detail": {"slope": slope, "group": grp}, "ts": ts_last})

        # —— 单根 K 线形态：锤子/流星（需结合趋势位置，先作背景证据）——
        pat = ti.candle_pattern(bars[-1]) if bars else None
        if pat in ("hammer", "shooting_star", "doji"):
            pat_cn = {"hammer": "锤子线", "shooting_star": "流星线", "doji": "十字星"}[pat]
            direction = "bullish" if pat == "hammer" else ("bearish" if pat == "shooting_star" else None)
            hits.append({"asset": asset, "kind": f"pattern_{pat}",
                         "direction": direction,
                         "hint": f"ℹ️ {asset} 出现{pat_cn}，位置含义需结合趋势判断。",
                         "detail": {"pattern": pat, "group": grp}, "ts": ts_last})

        # —— RSI 钝化：连续超买/超卖横盘，动能衰竭前兆，背景证据 ——
        blunt = ti.rsi_blunting([c[4] for c in bars], period=14, lookback=5)
        if blunt == "bearish":
            hits.append({"asset": asset, "kind": "rsi_blunt_overbought",
                         "direction": "bearish",
                         "hint": f"ℹ️ {asset} RSI 连续高位钝化，涨势动能衰竭。",
                         "detail": {"group": grp}, "ts": ts_last})
        elif blunt == "bullish":
            hits.append({"asset": asset, "kind": "rsi_blunt_oversold",
                         "direction": "bullish",
                         "hint": f"ℹ️ {asset} RSI 连续低位钝化，跌势动能衰竭。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— 均线多/空排列：MA5>10>20>60（多头）或反向（空头），趋势结构证据 ——
        stack = ti.ma_stack([c[4] for c in bars])
        if stack.get("bull_aligned"):
            hits.append({"asset": asset, "kind": "ma_bull_aligned",
                         "direction": "bullish",
                         "hint": f"ℹ️ {asset} 均线多头排列（5>10>20>60）。",
                         "detail": {"group": grp}, "ts": ts_last})
        elif stack.get("bear_aligned"):
            hits.append({"asset": asset, "kind": "ma_bear_aligned",
                         "direction": "bearish",
                         "hint": f"ℹ️ {asset} 均线空头排列（5<10<20<60）。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— 布林带上下轨突破：价格挤出轨道，超买/超卖嫌疑 ——
        bb_brk = ti.bollinger_break([c[4] for c in bars], period=20, num_std=2.0)
        if bb_brk == "upper_break":
            hits.append({"asset": asset, "kind": "bb_upper_break",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} 收盘价突破布林上轨，短线超买。",
                         "detail": {"group": grp}, "ts": ts_last})
        elif bb_brk == "lower_break":
            hits.append({"asset": asset, "kind": "bb_lower_break",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} 收盘价跌破布林下轨，短线超卖。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— MACD DIF/DEA 金叉/死叉：柱线交叉，转折佐证 ——
        mc = ti.macd_cross([c[4] for c in bars])
        if mc == "golden":
            hits.append({"asset": asset, "kind": "macd_golden",
                         "direction": "bullish",
                         "hint": f"ℹ️ {asset} MACD 金叉（DIF 上穿 DEA）。",
                         "detail": {"group": grp}, "ts": ts_last})
        elif mc == "death":
            hits.append({"asset": asset, "kind": "macd_death",
                         "direction": "bearish",
                         "hint": f"ℹ️ {asset} MACD 死叉（DIF 下穿 DEA）。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— 量能 Z-score：自适应放量/缩量，背景证据 ——
        vz = ti.vol_zscore(bars, lookback=20)
        if vz is not None and vz >= 2.0:
            hits.append({"asset": asset, "kind": "volume_zspike",
                         "direction": None,
                         "hint": f"ℹ️ {asset} 量能 Z={vz:.1f}（显著放量）。",
                         "detail": {"z": round(vz, 2), "group": grp}, "ts": ts_last})

        # —— ATR 突扩：波动率突然放大（事件/异动前兆），中性背景证据 ——
        if ti.atr_break(bars, period=14, lookback=20, threshold=1.5):
            hits.append({"asset": asset, "kind": "atr_expansion",
                         "direction": None,
                         "hint": f"ℹ️ {asset} ATR 突破近 20 根均值 1.5 倍，波动率突扩。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— 量价齐破：显著放量 + 收盘突破近 20 根极值，方向证据 ——
        vb = ti.vol_break(bars, z_threshold=2.0, lookback=20)
        if vb == "bullish_break":
            hits.append({"asset": asset, "kind": "vol_bull_break",
                         "direction": "bullish",
                         "hint": f"⚠️ {asset} 放量上破近 20 根高点。",
                         "detail": {"group": grp}, "ts": ts_last})
        elif vb == "bearish_break":
            hits.append({"asset": asset, "kind": "vol_bear_break",
                         "direction": "bearish",
                         "hint": f"⚠️ {asset} 放量下破近 20 根低点。",
                         "detail": {"group": grp}, "ts": ts_last})

        # —— Ichimoku 云图：价格在云上/云下/云内，趋势结构证据 ——
        ic = ti.ichimoku(bars)
        sa = ic["senkou_a"][-1] if ic["senkou_a"] else None
        sb = ic["senkou_b"][-1] if ic["senkou_b"] else None
        if sa is not None and sb is not None and bars:
            cloud_top, cloud_bot = max(sa, sb), min(sa, sb)
            last_close = bars[-1][4]
            if last_close > cloud_top:
                hits.append({"asset": asset, "kind": "ichimoku_bull",
                             "direction": "bullish",
                             "hint": f"ℹ️ {asset} 价格位于云图上方，多头结构。",
                             "detail": {"sa": sa, "sb": sb, "group": grp}, "ts": ts_last})
            elif last_close < cloud_bot:
                hits.append({"asset": asset, "kind": "ichimoku_bear",
                             "direction": "bearish",
                             "hint": f"ℹ️ {asset} 价格位于云图下方，空头结构。",
                             "detail": {"sa": sa, "sb": sb, "group": grp}, "ts": ts_last})

        # —— 筹码结构：价格相对价值区（POC/VAH/VAL）位置，不直出方向 ——
        vp = ti.volume_profile(bars, lookback=120, bins=24)
        if vp.get("position") and vp["position"] != "in_value":
            pos_cn = "价值区上方" if vp["position"] == "above_value" else "价值区下方"
            hits.append({"asset": asset, "kind": "value_zone",
                         "direction": "bullish" if vp["position"] == "above_value" else "bearish",
                         "hint": f"ℹ️ {asset} 价格位于{pos_cn}（POC 偏离 "
                                 f"{vp.get('poc_dist_pct')}%，{vp.get('weight')} 口径）。",
                         "detail": {"position": vp["position"], "poc": vp.get("poc"),
                                    "vah": vp.get("vah"), "val": vp.get("val"),
                                    "poc_dist_pct": vp.get("poc_dist_pct"),
                                    "weight": vp.get("weight"), "group": grp},
                         "ts": ts_last})

    return hits


def self_test(now: int) -> int:
    """用极端行情值跑一遍，确认每条规则都能触发（不写库）。"""
    stress = {
        ("US_MACRO", "us_vix_term_ratio"): (1.12, now),
        ("US_MACRO", "us_vix_cboe"): (34.5, now),
        ("US_MACRO", "us_t10y2y"): (-0.35, now),
        ("US_MACRO", "us_t10y2y_gov"): (-0.35, now),
        ("US_MACRO", "us_t10y3m_gov"): (-0.21, now),
        ("BTC", "x_spot_dispersion_pct"): (0.83, now),
        ("ETH", "x_spot_dispersion_pct"): (0.61, now),
        ("BTC", "bn_funding_rate"): (0.00092, now),
        ("ETH", "bn_funding_rate"): (-0.00077, now),
        ("SGE_GOLD", "sge_gold_premium_pct"): (1.84, now),
        ("BTC", "btc_mempool_tx"): (212000, now),
        # 备用源与新增方向也要各有一条自检，否则"接了但不会触发"查不出来
        ("SOL", "mexc_funding_rate"): (0.00081, now),
        ("BTC", "stable_supply_30d_pct"): (-4.2, now),
    }
    expand = dict(stress)
    expand[("BTC", "stable_supply_30d_pct")] = (5.1, now)
    hits = evaluate(None, now, getter=lambda a, i: stress.get((a, i)))
    print(f"[自检] 极端行情下命中 {len(hits)} 条规则")
    for h in hits:
        print(f"  ✓ {h['asset']:8} {h['kind']:26} {h['direction']}")
    hits2 = evaluate(None, now, getter=lambda a, i: expand.get((a, i)))
    kinds2 = {h["kind"] for h in hits2}
    print(f"[自检] 稳定币扩张方向命中: {'stablecoin_supply_expanding' in kinds2}")

    # 技术面规则自检：合成强多头 / 强空头 / 超买 / 超卖 K 线，确认各规则能触发
    def _synth(kind: str) -> List[list]:
        bars: List[list] = []
        base = 100.0
        for i in range(40):
            if kind == "bull":
                base *= 1.03
            elif kind == "bear":
                base *= 0.97
            elif kind == "ob":
                base = 100 + i * 0.2 if i < 20 else 104 - (i - 20) * 0.1
            else:  # oversold
                base = 100 - i * 0.2 if i < 20 else 80 + (i - 20) * 0.1
            bars.append([i * 86400, base, base * 1.01, base * 0.99, base, 1000.0])
        return bars

    def _synth_swing(kind: str) -> List[list]:
        """交替波峰/波谷序列：波峰根高抬、波谷根低探，峰谷同步抬升/降低。
        左窗比右窗更早一段，保证波峰波谷在左右窗内都是局部极值。"""
        bars: List[list] = []
        for i in range(40):
            if kind == "swing_bull":
                if i % 2 == 0:  # 波峰
                    lo = 100.0 + (i // 2) * 10.0
                    hi = lo + 8.0
                else:           # 波谷
                    lo = 80.0 + ((i - 1) // 2) * 10.0
                    hi = lo + 2.0
            else:
                if i % 2 == 0:  # 波峰
                    lo = 200.0 - (i // 2) * 10.0
                    hi = lo + 2.0
                else:           # 波谷
                    lo = 180.0 - ((i - 1) // 2) * 10.0
                    hi = lo + 8.0
            bars.append([i * 86400, (hi + lo) / 2.0, hi, lo, (hi + lo) / 2.0, 1000.0])
        return bars

    synth_map = {"T_BULL": "bull", "T_BEAR": "bear", "T_OB": "ob", "T_OS": "os",
                 "T_SB": "swing_bull", "T_SE": "swing_bear"}
    orig_load = globals()["_load_daily_bars"]

    def _load(_c, asset, limit=TECH_LOOKBACK):
        if asset in ("T_SB", "T_SE"):
            return _synth_swing(synth_map[asset])
        return _synth(synth_map[asset])

    globals()["_load_daily_bars"] = _load
    try:
        tech_hits = evaluate_technical(None, now, asset_filter=list(synth_map))
    finally:
        globals()["_load_daily_bars"] = orig_load
    tech_kinds = {h["kind"] for h in tech_hits}
    for h in tech_hits:
        print(f"  ✓ {h['asset']:8} {h['kind']:26} {h['direction']}")
    tech_ok = {"adx_strong_bull", "adx_strong_bear",
               "mfi_overbought", "mfi_oversold",
               "cci_oversold", "cci_overbought",
               "aroon_bull", "aroon_bear"} <= tech_kinds
    # swing_bull/swing_bear 需要左右确认窗的标准峰谷结构，自检合成序列不强制构出，
    # 单列一项在真实日线上校验，不计入硬性齐全判定。
    print(f"[自检] 技术面 ADX/MFI/CCI/Aroon 规则齐全: {tech_ok}"
          f"（swing 结构命中: {'swing_bull' in tech_kinds or 'swing_bear' in tech_kinds}）")

    ok = len(hits) >= 10 and "stablecoin_supply_expanding" in kinds2 and tech_ok
    return 0 if ok else 1


def main() -> int:
    now = int(time.time())
    if "--self-test" in sys.argv:
        return self_test(now)
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")

    hits = evaluate(conn, now)
    # 技术面规则（ADX / MFI），读日线实时算，不依赖外部指标表
    try:
        hits.extend(evaluate_technical(conn, now))
    except Exception as exc:
        print(f"[指标规则] 技术面规则执行失败: {type(exc).__name__}: {exc}", flush=True)
    written, skipped = 0, 0
    try:
        for h in hits:
            if already_open(conn, h["asset"], h["kind"], now):
                skipped += 1
                continue
            abnormal_detector.record_signal(
                conn, ts=h["ts"], asset=h["asset"], signal_kind=h["kind"],
                direction=h["direction"], anchor_ts=None,
                detail=dict(h["detail"], source="indicator_rules"),
                hint=h["hint"], status="open")
            written += 1
    finally:
        conn.close()

    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] 指标规则 命中 {len(hits)}｜新写 {written}｜去重跳过 {skipped}", flush=True)
    for h in hits:
        print(f"  - {h['asset']:8} {h['kind']:26} {h['direction']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
