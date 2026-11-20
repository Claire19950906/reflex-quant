# -*- coding: utf-8 -*-
"""tech_signal_bridge.py — 把 tech_signals 接进决策链路（零 LLM 成本）。

背景
----
`tech_signals` 每周产出约 90 条 K 线形态信号（背离 / 反转 / RSI 极值 / 支撑测试），
下游只被"归因复盘"消费，**从未进入 ai_decisions**。结果决策层一周只剩 3 个真信号，
且全部来自新闻 —— 技术面引擎实际在空转。

本模块按形态的教科书含义直接出决策，不调 LLM：
    div_bottom / reversal_bottom / support_test / rsi_oversold  → BUY
    div_top    / reversal_top    / resistance  / rsi_overbought → SELL

为什么先落一条合成 raw_news
--------------------------
`ai_decisions.news_id` 是 `INTEGER NOT NULL`，且前端按 news 展示决策。
所以每条技术信号配一条 source='tech_signal' 的合成新闻，决策挂在其上 ——
这样结算 worker、前端、归因页全部复用，不需要改任何下游。

幂等
----
用 raw_news.content_fingerprint = f"tech:{asset}:{ts}:{trigger_type}" 去重，
重复跑安全。

用法
----
    python3 tech_signal_bridge.py --dry-run        # 只打印，不写库
    python3 tech_signal_bridge.py --days 7         # 实写最近 7 天
    python3 tech_signal_bridge.py --days 30 --min-score 0.78
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(HERE), "trident_event_bus.db")
sys.path.insert(0, HERE)
import technical_indicators as ti
import strength_weights as sw

#: 只桥接核心监控品种（与 ai_worker 的核心清单一致）
CORE_ASSETS = ("BTC", "ETH", "XAU", "WTI", "SOL")

#: 形态 → 方向。顺序即优先级，先命中的先返回。
_BULL = ("div_bottom", "reversal_bottom", "support_test", "rsi_oversold", "pct_up")
_BEAR = ("div_top", "reversal_top", "resistance", "rsi_overbought", "pct_down")

# 台账复核后判定为「噪声 / 反向」的形态：不再直出 BUY/SELL 决策，
# 只作为背景证据保留在信号里。原因见每条注释；结论来自
# backend/reports/indicator_ledger/latest.md（5 日前向收益口径）。
#   div_top / macd_top_div : 看跌命中率 42.7% / 43.5%，实测反向；
#   div_bottom / macd_bot_div: 看涨超额为负，噪声；
#   ema_golden / ema_death : 金/死叉单出均无方向边际，噪声/反向。
# 新口径的 MACD 交叉命名（macd_bull_cross / macd_bear_cross）在新台账验证前
# 同样不直出方向。
_HOLD_ONLY = frozenset({
    "div_top", "div_bottom",
    "macd_top_div", "macd_bot_div", "macd_bull_cross", "macd_bear_cross",
    "ema_golden", "ema_death",
    # 回测台账判定为噪声/反向的因子：只作背景证据，不直出方向。
    # 依据 reports/indicator_ledger/factor_effectiveness.md（5 日前向口径）。
    "cci_overbought",      # 各族超买回落均为噪声
    "aroon_bear",          # equity/precious/index 反向；不直出看跌
    # kdj_overbought 在 v450 台账（基线修正后）转为有效，已移出观察池
    # 吞没形态：看涨仅 precious 组有效但样本小，看跌 equity/precious/index 反向；整体背景证据。
    "engulfing_bull", "engulfing_bear",
    # RSI 极值驻留是情绪状态证据，不单独定向。
    "rsi_stretch",
    # 均线交叉：死叉各族反向，金叉仅 equity/index 有边际，先整体作背景证据。
    "ma_golden", "ma_death",
    # MACD 零轴穿越：复盘为反向/噪声，只作背景证据。
    "macd_above_zero", "macd_below_zero",
    # 随机指标超买噪声；斜率/价值区/K线形态作背景证据，不直出方向。
    "stoch_overbought", "ma_slope", "value_zone",
    "pattern_hammer", "pattern_shooting_star", "pattern_doji",
    # 本轮新增：钝化/量能/上轨突破/空头排列/MACD死叉 为噪声或反向，背景证据。
    "rsi_blunt_oversold", "rsi_blunt_overbought",
    "bb_upper_break", "ma_bear_aligned", "macd_death", "volume_zspike",
    # 波动率突扩：中性背景；放量下破/云图空头反向，只作背景证据。
    "atr_expansion", "vol_bear_break", "ichimoku_bear",
    # 放量上破/云图多头：仅作顺势佐证，不单独定向（超额近 0，仅 crypto/equity 偏多）。
    "vol_bull_break", "ichimoku_bull",
})

_LABEL = {
    "div_bottom": "底部背离（价格新低但 RSI 未创新低）",
    "div_top": "顶部背离（价格新高但 RSI 未创新高）",
    "reversal_bottom": "底部反转形态",
    "reversal_top": "顶部反转形态",
    "support_test": "回踩支撑位",
    "resistance": "触及压力位",
    "rsi_oversold": "RSI 超卖",
    "rsi_overbought": "RSI 超买",
    "ema_golden": "均线金叉",
    "ema_death": "均线死叉",
    "macd_bull_cross": "MACD 柱由负转正（多头交叉）",
    "macd_bear_cross": "MACD 柱由正转负（空头交叉）",
    "adx_strong_bull": "ADX 强多头趋势",
    "adx_strong_bear": "ADX 强空头趋势",
    "mfi_oversold": "MFI 资金流量超卖",
    "mfi_overbought": "MFI 资金流量超买",
    "cci_oversold": "CCI 通道超卖",
    "cci_overbought": "CCI 通道超买",
    "swing_bull": "上升摆动结构（HH/HL）",
    "swing_bear": "下降摆动结构（LH/LL）",
    "aroon_bull": "Aroon 上行趋势",
    "aroon_bear": "Aroon 下行趋势",
    "engulfing_bull": "看涨吞没形态",
    "engulfing_bear": "看跌吞没形态",
    "kdj_oversold": "KDJ 超卖",
    "kdj_overbought": "KDJ 超买",
    "rsi_stretch": "RSI 极值驻留",
    "ma_golden": "均线金叉",
    "ma_death": "均线死叉",
    "macd_above_zero": "MACD 主线转正",
    "macd_below_zero": "MACD 主线转负",
    "stoch_oversold": "随机指标超卖",
    "stoch_overbought": "随机指标超买",
    "ma_slope": "均线斜率",
    "value_zone": "筹码价值区位置",
    "pattern_hammer": "锤子线",
    "pattern_shooting_star": "流星线",
    "pattern_doji": "十字星",
    "supertrend_bull": "SuperTrend 多头轨",
    "supertrend_bear": "SuperTrend 空头轨",
    "market_state": "市场状态",
    "rsi_blunt_oversold": "RSI 超卖钝化",
    "rsi_blunt_overbought": "RSI 超买钝化",
    "ma_bull_aligned": "均线多头排列",
    "ma_bear_aligned": "均线空头排列",
    "bb_upper_break": "布林上轨突破",
    "bb_lower_break": "布林下轨跌破",
    "macd_golden": "MACD 金叉",
    "macd_death": "MACD 死叉",
    "volume_zspike": "量能 Z 放量",
    "atr_expansion": "波动率突扩",
    "vol_bull_break": "放量上破",
    "vol_bear_break": "放量下破",
    "ichimoku_bull": "一目云图多头",
    "ichimoku_bear": "一目云图空头",
}

# 趋势确认类证据：只用于校验/加权其他反转信号，不单独直出方向。
# 强趋势里逆势抄底/摸顶风险大，所以它们本身不构成 BUY/SELL，只改变强度。
# 复盘台账 signal_review.md：bear 侧（adx_strong_bear/swing_bear/aroon_bear/
# supertrend_bear）5 日前向命中均 <50%、超额为负，在本仓样本里不支持看跌顺势，
# 因此这些证据不参与"顺势加分"，只保留逆势风险减分；bull 侧保持顺势证据。
_TREND_BULL = {"adx_strong_bull", "supertrend_bull", "swing_bull", "aroon_bull"}
_TREND_BEAR = {"adx_strong_bear", "supertrend_bear", "swing_bear", "aroon_bear"}

# 转折/形态类事件：逐点回测后不单独定向，只作为顺势共振的轻量佐证。
# ma_golden/engulfing_bull 在 equity/index 偏多（命中略高、超额近 0）→ 仅给 BUY 加分；
# ma_death/engulfing_bear/macd 零轴穿越在各族噪声/反向 → 只作背景，不加分。
_TURN_BULL = {"ma_golden", "engulfing_bull", "bb_lower_break", "macd_golden",
              "ichimoku_bull", "vol_bull_break"}
_TURN_BEAR = {"ma_death", "engulfing_bear", "ma_bear_aligned", "macd_death",
              "vol_bear_break", "ichimoku_bear"}

# 多周期共振参与判断的周期（kline_bars 内可用）。1d 走日线表。
# 15m/10m/5m/3m/1m 高频周期只在 kline_bars 里有零散覆盖；缺数据的周期在
# multi_tf_resonance 里按"不足 12 根直接跳过"处理，不造数、不影响其余周期判共振。
_MTF_TIMEFRAMES = ("1d", "4h", "1h")
_MTF_LOOKBACK = 60


def _mtf_bars(conn: sqlite3.Connection, asset: str, tf: str) -> List[list]:
    """取某资产某周期最近若干根升序 K 线 [ts,o,h,l,c,v]；日线表优先，否则 kline_bars。"""
    if tf == "1d":
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM ohlcv_daily "
            "WHERE asset=? ORDER BY ts DESC LIMIT ?", (asset, _MTF_LOOKBACK)).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM kline_bars "
            "WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
            (asset, tf, _MTF_LOOKBACK)).fetchall()
    bars: List[list] = []
    for r in reversed(rows):
        ts, o, h, l, c, v = r[0], r[1], r[2], r[3], r[4], r[5]
        if c is None or c <= 0 or h is None or h < l:
            continue
        bars.append([int(ts), float(o or c), float(h), float(l), float(c), float(v or 0.0)])
    return bars


def multi_tf_resonance(conn: sqlite3.Connection, asset: str) -> Dict[str, Any]:
    """多周期共振：1d/4h/1h 各算 SuperTrend 方向，统计多/空周期数。

    返回 {direction: bull|bear|neutral|no_data, bull_n, bear_n, tf: {tf: ±1}}。
    缺数据的周期跳过，不造数；只有一个周期有数据时不构成共振，标 neutral。
    """
    per: Dict[str, int] = {}
    for tf in _MTF_TIMEFRAMES:
        bars = _mtf_bars(conn, asset, tf)
        if len(bars) < 12:
            continue
        st = ti.supertrend(bars, 10, 3.0)
        d = st["direction"][-1] if st["direction"] else None
        if d is None:
            continue
        per[tf] = int(d)
    bull_n = sum(1 for v in per.values() if v == 1)
    bear_n = sum(1 for v in per.values() if v == -1)
    if not per:
        return {"direction": "no_data", "bull_n": 0, "bear_n": 0, "tf": {}}
    if len(per) < 2:
        return {"direction": "neutral", "bull_n": bull_n, "bear_n": bear_n, "tf": per}
    if bull_n > bear_n:
        direction = "bull"
    elif bear_n > bull_n:
        direction = "bear"
    else:
        direction = "neutral"
    return {"direction": direction, "bull_n": bull_n, "bear_n": bear_n, "tf": per}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _fingerprint(asset: str, ts: int, trigger: str) -> str:
    raw = f"tech:{asset}:{ts}:{trigger}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _parse_items(signal_json: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(signal_json or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("k")]


def decide_direction(items: List[Dict[str, Any]], pct_1d: Optional[float],
                     resonance: Optional[Dict[str, Any]] = None,
                     allow: Optional[Dict[str, Any]] = None) -> Tuple[str, str, float]:
    """返回 (action, reason, strength)。

    strength 0-1，喂给 evidence_confidence（×100 后必须过闸门 15）。
    ADX 强趋势只作趋势确认：与候选方向同向加分、逆势减分，不单独出方向。

    allow（可选）：白名单配置。传进来时，其中的 `blacklist`（台账判定为**反向**
    的因子，如 vol_bear_break / vol_bull_break）与 `_HOLD_ONLY` 一并降级为背景证据，
    不再直出方向 —— 反向因子照原方向出信号等于系统性做错方向。
    """
    keys = [str(it.get("k") or "") for it in items]
    weights = {str(it.get("k") or ""): float(it.get("w") or 0.0) for it in items}

    # 台账复核为噪声/反向的形态：只留作证据，不直出方向，避免把负超额信号
    # 直接喂成 BUY/SELL 决策。若同一批 items 里另有有效形态，仍走下面分支。
    hold_only = _HOLD_ONLY
    blacklist = set((allow or {}).get("blacklist") or ())
    if blacklist:
        hold_only = _HOLD_ONLY | blacklist
    actionable = [k for k in keys if k not in hold_only]
    if not actionable:
        return "HOLD", "信号形态在台账中为噪声/反向，仅作背景证据，不出方向", 0.0

    trend_bull = any(k in _TREND_BULL for k in keys)
    trend_bear = any(k in _TREND_BEAR for k in keys)
    turn_bull = any(k in _TURN_BULL for k in keys)
    turn_bear = any(k in _TURN_BEAR for k in keys)

    def _adjust(action: str, base_strength: float, reason: str,
                resonance: Optional[Dict[str, Any]] = None) -> Tuple[str, str, float]:
        w = sw.weights()
        # 趋势同向加分，逆势减分（趋势里逆势信号更不可靠）
        if trend_bull and action == "BUY":
            base_strength = sw.clamp(base_strength + w["trend_same"])
            reason += "；强多头趋势共振"
        elif trend_bear and action == "SELL":
            base_strength = sw.clamp(base_strength + w["trend_same"])
            reason += "；强空头趋势共振"
        elif trend_bull and action == "SELL":
            base_strength = sw.clamp(base_strength + w["trend_against"])
            reason += "；但处强多头趋势，逆势看空"
        elif trend_bear and action == "BUY":
            base_strength = sw.clamp(base_strength + w["trend_against"])
            reason += "；但处强空头趋势，逆势抄底"
        # 转折/形态轻量佐证：同向小幅加分（不单独出方向），逆势不加重
        if turn_bull and action == "BUY":
            base_strength = sw.clamp(base_strength + 0.03)
            reason += "；转折形态偏多佐证"
        elif turn_bear and action == "SELL":
            base_strength = sw.clamp(base_strength + 0.03)
            reason += "；转折形态偏空佐证"
        # 多周期共振：同向加分、逆势减分；分歧/数据不足不动。
        if resonance and resonance.get("direction") in ("bull", "bear"):
            res_dir = resonance["direction"]
            res_label = f"多周期共振{('多头' if res_dir == 'bull' else '空头')}" \
                        f"({resonance['bull_n']}多/{resonance['bear_n']}空)"
            if (res_dir == "bull" and action == "BUY") or (res_dir == "bear" and action == "SELL"):
                base_strength = sw.clamp(base_strength + w["resonance_same"])
                reason += f"；{res_label}同向"
            else:
                base_strength = sw.clamp(base_strength + w["resonance_against"])
                reason += f"；{res_label}逆势"
        return action, reason, base_strength

    # 形态优先：背离/反转权重最高
    for k in ("div_bottom", "div_top", "reversal_bottom", "reversal_top"):
        if k in actionable:
            w = max(weights.values()) if weights else 1.0
            action = "BUY" if k in _BULL else "SELL"
            return _adjust(action, min(1.0, 0.55 + 0.2 * w),
                            f"{_LABEL.get(k, k)}（权重 {w:.1f}）", resonance)

    # RSI/MFI/CCI/KDJ/Stoch/布林下轨 极值（均值回归）
    for k in ("rsi_oversold", "mfi_oversold", "cci_oversold", "kdj_oversold",
              "stoch_oversold", "bb_lower_break"):
        if k in actionable:
            return _adjust("BUY", 0.45, f"{_LABEL.get(k, k)}（均值回归）", resonance)
    for k in ("rsi_overbought", "mfi_overbought", "kdj_overbought"):
        if k in actionable:
            return _adjust("SELL", 0.45, f"{_LABEL.get(k, k)}（均值回归）", resonance)
    # aroon_bull 上升新鲜度作为顺势加分证据（不单独直出方向）；aroon_bear 已进 HOLD_ONLY。

    # 支撑/压力（优先级高于纯涨跌幅：回踩支撑的当日下跌不按顺势做空）
    for k in ("support_test", "resistance"):
        if k in actionable:
            action = "BUY" if k == "support_test" else "SELL"
            return _adjust(action, 0.40, _LABEL.get(k, k), resonance)

    # 纯涨跌幅：顺势（仅显式 pct_1d 描述项；pct_3d/放量/波动率等不单独定向）
    if "pct_1d" in actionable and pct_1d is not None and abs(float(pct_1d)) >= 1.0:
        action = "BUY" if float(pct_1d) > 0 else "SELL"
        return _adjust(action, 0.35, f"单日涨跌幅 {float(pct_1d):+.2f}%（顺势）", resonance)

    # 其余未登记的描述项（放量、波动率、3日涨跌幅等）：不足以单独定向，不出方向。
    return "HOLD", "形态不足以确定方向", 0.0


from tech_vol_regime import asset_class, vol_bucket


def load_actionable() -> Dict[str, Any]:
    """台账白名单：只有通过基线检验的因子才允许出方向。

    路径 src_python/runtime/tech_actionable.json；文件缺失时退化为「全部放行」，
    避免调度任务因配置丢失直接停摆。改因子不用改代码。

    结构（v453 起带周期分档）：
        bull / bear         —— 扁平列表（旧版，兼容用；实为 5 日口径）
        by_horizon.{1,3,5,10}.{bull,bear} —— 按交易日前向收益分档放行
        blacklist           —— 台账判定「反向」的因子，一律降级为背景证据
        regime              —— MA200 / ADX 环境闸门
    """
    p = Path(__file__).resolve().parent / "runtime" / "tech_actionable.json"
    try:
        data = {"bull": [], "bear": [], **json.loads(p.read_text(encoding="utf-8"))}
        data.setdefault("allow_all", False)   # 有配置文件就必须走白名单
        return data
    except Exception:
        # 配置缺失时放行，避免调度任务直接停摆（会在日志里体现为空配置）
        return {"bull": [], "bear": [], "allow_all": True}


#: 台账覆盖的周期（交易日）。同一个因子在不同周期结论可以完全相反
#: —— bb_upper_break 3 日超额 +1.2 判「有效」，5 日只剩噪声；
#: 所以放行口径必须跟着周期走，而不是一套 5 日通吃。
HORIZONS: tuple[int, ...] = (1, 3, 5, 10)


def allowed_keys(allow: Dict[str, Any], horizon: int) -> set[str]:
    """某周期放行的因子集合。配置没有分档时退回扁平 bull/bear（兼容旧配置）。"""
    by = (allow or {}).get("by_horizon") or {}
    slot = by.get(str(int(horizon)))
    if isinstance(slot, dict):
        return set(slot.get("bull") or []) | set(slot.get("bear") or [])
    return set((allow or {}).get("bull") or []) | set((allow or {}).get("bear") or [])


def _is_actionable(items: List[Dict[str, Any]], allow: Dict[str, Any],
                   horizon: int = 5) -> bool:
    if allow.get("allow_all"):
        return True
    keys = {str(x.get("k")) for x in items if x.get("k")}
    blacklist = set((allow or {}).get("blacklist") or ())
    if keys and not (keys - blacklist):
        return False                     # 整批证据都在黑名单里 → 不出方向
    return bool(keys & allowed_keys(allow, horizon))


#: 波动档位缓存：(asset, ts) -> low/mid/high/None
_VOL_CACHE: Dict[Tuple[str, int], Any] = {}


def _vol_cached(conn: sqlite3.Connection, asset: str, ts: int):
    key = (asset, int(ts))
    if key not in _VOL_CACHE:
        try:
            _VOL_CACHE[key] = vol_bucket(conn, asset, int(ts))
        except Exception:
            _VOL_CACHE[key] = None
    return _VOL_CACHE[key]


def strata_horizon(items: List[Dict[str, Any]], allow: Dict[str, Any],
                   cls: Optional[str], vol: Optional[str]) -> Optional[int]:
    """分层格子给出的周期覆盖（v456）。

    为什么需要"覆盖"而不是"并集"：kdj_overbought 在全局 5 日口径里是被放行的，
    但高波动状态下它的兑现期是 10 日 —— 用并集永远先命中 5 日，分层结论白算。
    所以命中格子时直接接管周期。

    只在 `strata.enabled` 为真、且品种类别/波动档位都已知时生效；
    档位算不出来（历史不足）就退回全局口径，不猜。
    """
    strata = (allow or {}).get("strata") or {}
    if not strata.get("enabled") or not cls or not vol:
        return None
    keys = {str(x.get("k")) for x in items if x.get("k")}
    for cell in strata.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        if str(cell.get("factor")) not in keys:
            continue
        dim = cell.get("dim")
        key = str(cell.get("key") or "")
        if dim == "vol" and key == vol:
            pass
        elif dim == "class" and key == cls:
            pass
        elif dim == "both" and key == f"{cls}/{vol}":
            pass
        else:
            continue
        h = cell.get("horizon")
        if h in HORIZONS:
            return int(h)
    return None


def resolve_horizon(items: List[Dict[str, Any]], allow: Dict[str, Any],
                    horizons: tuple[int, ...] = HORIZONS,
                    cls: Optional[str] = None, vol: Optional[str] = None) -> Optional[int]:
    """按周期从短到长找第一个放行的周期；都不放行返回 None。

    先短后长：能 3 日兑现的信号不该拖到 10 日结算，否则样本会因为等不满 K 线而白丢。
    分层格子优先（见 strata_horizon）：它给的是"这个状态下的兑现期"，比全局口径更具体。
    """
    override = strata_horizon(items, allow, cls, vol)
    if override is not None:
        return override
    for h in horizons:
        if _is_actionable(items, allow, h):
            return int(h)
    return None


#: MA200 状态缓存：(asset, ts) -> "above" / "below" / None（样本不足）
_MA200_CACHE: Dict[Tuple[str, int], Any] = {}


def ma200_state(conn: sqlite3.Connection, asset: str, ts: int):
    """信号当根收盘相对 200 日均线。只用 ts 及之前的 bar，不含未来。"""
    key = (asset, int(ts))
    if key in _MA200_CACHE:
        return _MA200_CACHE[key]
    rows = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 200",
        (asset, int(ts)),
    ).fetchall()
    val = None
    if len(rows) >= 200:
        closes = [float(x[0]) for x in rows]
        ma = sum(closes) / 200.0
        val = "above" if closes[0] > ma else "below"
    _MA200_CACHE[key] = val
    return val


def regime_ok(conn: sqlite3.Connection, asset: str, ts: int, action: str,
              items: List[Dict[str, Any]], allow: Dict[str, Any]) -> bool:
    """环境闸门：MA200 定多空站位，ADX 只用于 SELL 侧的顺势确认。

    依据 reports/tech_engine/v451_regime.md（五年 22,476 条因果信号）：
      · BUY 白名单：无闸门 +3.2pt / 超额 +0.62%；加 MA200 上 +5.3pt / +1.08%
      · SELL kdj：无闸门 −0.5pt / −0.38%；加 MA200 下 + ADX 空 +5.6pt / +1.48%
    数据不足 200 根时一律不放行（宁可不出信号，也不猜）。
    """
    cfg = (allow or {}).get("regime") or {}
    if action == "BUY" and cfg.get("buy_require_ma200_above"):
        return ma200_state(conn, asset, ts) == "above"
    if action == "SELL" and cfg.get("sell_require_ma200_below_adx_bear"):
        if ma200_state(conn, asset, ts) != "below":
            return False
        keys = {str(x.get("k")) for x in items}
        return "adx_strong_bear" in keys
    return True


def build_candidates(conn: sqlite3.Connection, days: int, min_score: float,
                     allow: Optional[Dict[str, Any]] = None,
                     allow_all: bool = False) -> List[Dict[str, Any]]:
    if allow is None:
        allow = {"allow_all": True} if allow_all else load_actionable()
    elif allow_all:
        allow = {"allow_all": True}
    cutoff = 0
    try:
        cutoff = int(conn.execute("SELECT strftime('%s','now')").fetchone()[0]) - days * 86400
    except Exception:
        cutoff = 0
    rows = conn.execute(
        "SELECT id, asset, ts, close, trigger_type, score, pct_1d, support_level, signal_json"
        " FROM tech_signals WHERE ts >= ? AND score >= ? AND uses_future = 0"
        "   AND ts >= (SELECT COALESCE(MAX(strftime('%s', started_at)), 0) FROM data_epoch)"
        "   AND asset IN ('BTC','ETH','XAU','WTI','SOL')"
        " ORDER BY ts ASC",
        (cutoff, float(min_score)),
    ).fetchall()

    out: List[Dict[str, Any]] = []
    #: 同品种同方向 3 天内只出一次 —— 一个顶部背离往往连续好几天都在，
    #: 不去重的话 BTC/SOL/ETH 会每天各刷一条 SELL，把前端刷成噪音。
    _DEDUP_WIN_S = 3 * 86400
    last_dir: Dict[str, Tuple[str, int]] = {}
    for r in rows:
        fp = _fingerprint(r["asset"], int(r["ts"]), str(r["trigger_type"]))
        if conn.execute(
            "SELECT 1 FROM raw_news WHERE content_fingerprint = ? LIMIT 1", (fp,)
        ).fetchone():
            continue  # 已桥接过
        items = _parse_items(r["signal_json"])
        resonance = multi_tf_resonance(conn, r["asset"])
        cls = asset_class(r["asset"])
        vb = _vol_cached(conn, r["asset"], int(r["ts"]))
        horizon = resolve_horizon(items, allow, cls=cls, vol=vb)
        if horizon is None:
            continue
        action, why, strength = decide_direction(items, r["pct_1d"], resonance, allow)
        if action == "HOLD":
            continue
        if not regime_ok(conn, r["asset"], int(r["ts"]), action, items, allow):
            continue
        prev = last_dir.get(r["asset"])
        if prev and prev[0] == action and abs(int(r["ts"]) - prev[1]) < _DEDUP_WIN_S:
            continue
        last_dir[r["asset"]] = (action, int(r["ts"]))
        out.append({
            "tech_id": r["id"],
            "asset": r["asset"],
            "ts": int(r["ts"]),
            "close": float(r["close"] or 0.0),
            "trigger": r["trigger_type"],
            "score": float(r["score"] or 0.0),
            "action": action,
            "horizon": horizon,
            "asset_class": cls,
            "vol_bucket": vb,
            "reason": why,
            "strength": strength,
            "resonance": resonance,
            "fingerprint": fp,
            "items": items,
        })
    return out


def _news_content(c: Dict[str, Any]) -> str:
    return (
        f"[技术信号] {c['asset']} {c['trigger']}：{c['reason']}。"
        f"收盘 {c['close']}，信号强度 {c['score']:.2f}。"
        f"（来源 tech_signals#{c['tech_id']}，规则直出，未经 LLM）"
    )


def write_one(conn: sqlite3.Connection, c: Dict[str, Any]) -> int:
    import datetime as _dt
    ts_txt = _dt.datetime.fromtimestamp(c["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    content = _news_content(c)

    cur = conn.execute(
        "INSERT INTO raw_news (source, content, timestamp, ts, status, is_noise, relevance_score,"
        " published_at, content_fingerprint, first_seen_ts, gate_verdict, gate_strength,"
        " gate_is_material, gate_main_asset, gate_timing, gate_kind, gate_unavailable)"
        " VALUES ('tech_signal', ?, ?, ?, 'DONE', 0, 0.9, ?, ?, ?, '技术信号直出', ?, 1, ?, 'intraday', 'technical', 0)",
        (
            content, str(c["ts"]), c["ts"], ts_txt, c["fingerprint"], c["ts"],
            int(round(c["score"] * 10)), c["asset"],
        ),
    )
    news_id = int(cur.lastrowid)

    score = float(c["strength"]) if c["action"] == "BUY" else -float(c["strength"])
    reasoning = (
        f"[技术信号直出] {c['asset']} {c['reason']}；"
        f"触发类型 {c['trigger']}，强度 {c['score']:.2f}；"
        f"规则映射（未调用 LLM）：底部形态→BUY / 顶部形态→SELL。"
    )
    cur = conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning,"
        " created_at, market_category, target_asset, reasoning_path,"
        " prediction_type, event_phase, market_confirmation, expected_horizon,"
        " event_strength, direct_catalyst, decision_context,"
        " heat_score, impact_score, asset_impact,"
        " evidence_confidence, evidence_action, verification,"
        " analysis_degraded, analysis_degraded_reason, ts, entry_price, entry_time)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'continuation', 'early', 'unknown', '1-3d',"
        " 'medium', 0, ?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, ?)",
        (
            news_id, score, c["action"], reasoning, ts_txt,
            "CRYPTO" if c["asset"] in ("BTC", "ETH", "SOL") else ("GOLD" if c["asset"] == "XAU" else "OIL"),
            c["asset"], reasoning,
            json.dumps({"source": "tech_signal", "tech_id": c["tech_id"],
                        "trigger": c["trigger"], "close": c["close"]}, ensure_ascii=False),
            round(c["score"] * 10, 1), round(c["score"] * 10, 1), round(c["score"] * 10, 1),
            round(c["strength"] * 100, 1), c["action"],
            "skip | 技术信号规则直出，无 peer 评审",
            c["ts"], c["close"], ts_txt,
        ),
    )
    # v489：技术直出带价写入 = 已落地，exec_state 记空串（否则会被当成"没落地"）。
    try:
        conn.execute("UPDATE ai_decisions SET exec_state='' WHERE id=?",
                     (cur.lastrowid,))
    except Exception as _exc_es:
        # v490: 原先静默 pass —— exec_state 回填失败不留痕，会被当成"已落地"。
        # 落丢写账本，便于事后统计技术直出链的回填缺口。
        try:
            import silent_ledger
            silent_ledger.record(
                "tech_signal.exec_state",
                f"decision={cur.lastrowid} {type(_exc_es).__name__}: {str(_exc_es)[:100]}",
            )
        except Exception as _exc_ledger:
            # v491：连丢写账本都失败时至少要能看见 —— 全静默会让"技术直出没记上"
            # 彻底查不出来。
            print(f"[SILENT-LEDGER:tech] record failed: "
                  f"{type(_exc_ledger).__name__}: {_exc_ledger}", flush=True)
    # v483: 技术直出链此前既不落指纹也不进审计分母 —— 390 条决策里指纹只覆盖 81 条、
    # 审计只 50 条，缺口主要来自这条路径。指纹补上；审计交给 hook（它会按
    # [技术信号直出] 前缀记一行 skipped，让"没审"和"漏审"能区分开）。
    try:
        from code_fingerprint import stamp as _cfp_stamp
        _cfp_stamp(conn, cur.lastrowid)
    except Exception as _exc_fp:
        print(f"[FINGERPRINT:tech] skipped: {type(_exc_fp).__name__}: {_exc_fp}", flush=True)
    try:
        import audit_gate_hook as _agh
        _agh.gate(conn, cur.lastrowid, tag="tech")
    except Exception as _exc_ag:
        print(f"[AUDIT-GATE:tech] skipped: {type(_exc_ag).__name__}: {_exc_ag}", flush=True)
    return news_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-score", type=float, default=0.58,
                    help="分数下限；0.58=B 级起，0.78=A 级起（新口径，满值约 0.99）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-all", action="store_true",
                    help="跳过台账白名单（研究/对照用，不建议线上）")
    args = ap.parse_args()

    conn = _connect()
    try:
        cands = build_candidates(conn, args.days, args.min_score, allow_all=args.allow_all)
        print(f"[BRIDGE] 近 {args.days} 天候选 {len(cands)} 条（score>={args.min_score}，"
              f"品种={'/'.join(CORE_ASSETS)}）")
        for c in cands:
            print(f"  {c['asset']:4s} {c['trigger']:14s} → {c['action']:4s} "
                  f"| {c['reason']} | ts={c['ts']}")
        if args.dry_run:
            print("[BRIDGE] dry-run，未写库")
            return 0
        n = 0
        for c in cands:
            try:
                write_one(conn, c)
                n += 1
            except Exception as exc:
                print(f"  [BRIDGE] 写入失败 {c['asset']} {c['trigger']}: "
                      f"{type(exc).__name__}: {exc}", flush=True)
        conn.commit()
        print(f"[BRIDGE] 已写入 {n} 条决策")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
