# -*- coding: utf-8 -*-
"""v226+ Q7 闭环：周期分析（大/小/极短周期 + ZigZag + Hurst + 反转点）。

设计约束（与项目其它分析器一致）：
  - 纯函数，无外部依赖；输入 List[float]，输出 dict。
  - 样本不足时返回 None/默认值，禁止造数。
  - Hurst 用经典 R/S 算法；ZigZag 用 5% 反转阈值。
  - Phase 用 Wyckoff 四阶段（accumulation/markup/distribution/markdown）。
  - Reversal 用 RSI 极值 + ZigZag 反转点 + Hurst 三票投票。

典型用法：
    from cycle_analyst import (
        detect_phase, detect_cycle_length, predict_reversal,
        zigzag_pivots, hurst_exponent, multi_timeframe_signal,
    )
    phase = detect_phase(btc_30d_closes)              # 'markup' / 'accumulation' / ...
    cycles = detect_cycle_length(btc_180d_closes)      # days
    reversal = predict_reversal(btc_60d_closes)        # {'prob': 0.7, 'days_to': 3}
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple


# 最小样本数门槛（低于此返回 None / 默认值）
_MIN_SAMPLES = 10


# ────────────────────────────────────────────────────────────────────
# 1) ZigZag 枢轴点
# ────────────────────────────────────────────────────────────────────

def zigzag_pivots(prices: Sequence[float], threshold_pct: float = 5.0,
                  ) -> List[Dict[str, Any]]:
    """ZigZag 枢轴检测：相对前一个枢轴的反转幅度 ≥ threshold_pct 视为新枢轴。

    Parameters
    ----------
    prices : 升序时间序列的收盘价
    threshold_pct : 反转阈值百分比（默认 5%）

    Returns
    -------
    List[dict] : 每项 {"idx", "price", "type": "high"|"low"}
        按时间顺序；首尾通常是 high 或 low 之一。
    """
    if not prices or len(prices) < 2:
        return []
    threshold = threshold_pct / 100.0
    pivots: List[Dict[str, Any]] = []

    # 寻找第一个枢轴：找到目前为止的最高/最低
    # 简单起见：用第一个点作起点，按后续方向确定类型
    last_idx = 0
    last_price = float(prices[0])
    direction = 0  # 0=未定 1=上行（之前在爬升） -1=下行

    for i in range(1, len(prices)):
        p = float(prices[i])
        if last_price <= 0:
            break
        if direction == 0:
            # 还没确定：方向由第一段走势决定
            if p > last_price:
                direction = 1
                last_price = p
                last_idx = i
            elif p < last_price:
                direction = -1
                last_price = p
                last_idx = i
            # else: 不动（p == last_price）
            continue
        if direction == 1:
            # 上行：跟踪更高的高点
            if p > last_price:
                last_price = p
                last_idx = i
                continue
            # 反转下行：检查幅度
            drop = (last_price - p) / last_price
            if drop >= threshold:
                # 记录上一个 high 为枢轴，转下行
                pivots.append({"idx": last_idx, "price": last_price, "type": "high"})
                # 重置成下行模式
                direction = -1
                last_price = p
                last_idx = i
        else:  # direction == -1
            # 下行：跟踪更低的低点
            if p < last_price:
                last_price = p
                last_idx = i
                continue
            # 反转上行：检查幅度
            rise = (p - last_price) / last_price
            if rise >= threshold:
                pivots.append({"idx": last_idx, "price": last_price, "type": "low"})
                direction = 1
                last_price = p
                last_idx = i

    # 最后一个未结的方向补一个枢轴
    if pivots:
        last_type = pivots[-1]["type"]
        if last_type == "high":
            # 当前还在下行（没结），补一个 low
            pivots.append({"idx": last_idx, "price": last_price, "type": "low"})
        elif last_type == "low":
            pivots.append({"idx": last_idx, "price": last_price, "type": "high"})
    else:
        # 没产生任何枢轴（数据不足或完全单向）→ 至少标一个
        pivots.append({"idx": last_idx, "price": last_price,
                       "type": "high" if direction == 1 else "low"})

    return pivots

def _rsi14(closes: Sequence[float]) -> Optional[float]:
    """RSI(14) — Wilder 平滑。需要 ≥ 15 个收盘价。样本不足返回 None。"""
    if len(closes) < 15:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains[:14]) / 14.0
    avg_l = sum(losses[:14]) / 14.0
    for i in range(14, len(gains)):
        avg_g = (avg_g * 13.0 + gains[i]) / 14.0
        avg_l = (avg_l * 13.0 + losses[i]) / 14.0
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))
# ────────────────────────────────────────────────────────────────────
# 2) Hurst 指数（重标极差法 R/S）
# ────────────────────────────────────────────────────────────────────

def hurst_exponent(prices: Sequence[float]) -> Optional[float]:
    """Hurst 指数（R/S 重标极差法）。

    解释：
      H > 0.5  → 持续趋势（持久性）
      H = 0.5  → 随机游走
      H < 0.5  → 均值回复（反持久）

    样本不足返回 None。
    """
    n = len(prices)
    if n < 20:
        return None
    returns: List[float] = []
    for i in range(1, n):
        if prices[i - 1] <= 0 or prices[i] <= 0:
            continue
        returns.append(math.log(prices[i] / prices[i - 1]))
    if len(returns) < 10:
        return None
    sizes = [10, 20, 40, 80, 160]
    sizes = [s for s in sizes if s <= len(returns) // 2]
    if len(sizes) < 2:
        return None
    log_sizes: List[float] = []
    log_rs: List[float] = []
    for size in sizes:
        n_chunks = len(returns) // size
        if n_chunks < 1:
            continue
        rs_values: List[float] = []
        for j in range(n_chunks):
            chunk = returns[j * size:(j + 1) * size]
            if not chunk:
                continue
            mean = sum(chunk) / len(chunk)
            deviations = [x - mean for x in chunk]
            cum_dev: List[float] = []
            s = 0.0
            for d in deviations:
                s += d
                cum_dev.append(s)
            r = max(cum_dev) - min(cum_dev)
            std = math.sqrt(sum(d * d for d in deviations) / len(deviations))
            if std > 1e-10:
                rs_values.append(r / std)
            elif r > 1e-10:
                # 完全确定性趋势：returns 完全相同，std=0 但 r>0 → 视作强趋势
                # 用 R 本身作 proxy（避免除零）
                rs_values.append(r)
        if rs_values:
            avg_rs = sum(rs_values) / len(rs_values)
            if avg_rs > 1e-10:
                log_sizes.append(math.log(size))
                log_rs.append(math.log(avg_rs))
    if len(log_sizes) < 2:
        return None
    n_pts = len(log_sizes)
    mean_x = sum(log_sizes) / n_pts
    mean_y = sum(log_rs) / n_pts
    num = sum((log_sizes[i] - mean_x) * (log_rs[i] - mean_y) for i in range(n_pts))
    den = sum((log_sizes[i] - mean_x) ** 2 for i in range(n_pts))
    if den == 0:
        return None
    return num / den


def detect_phase(prices: Sequence[float], window: int = 20) -> Dict[str, Any]:
    """检测当前周期阶段：accumulation / markup / distribution / markdown。"""
    if len(prices) < _MIN_SAMPLES:
        return {"phase": "unknown", "rsi": None, "hurst": None,
                "trend_pct": 0.0, "confidence": 0.0}
    sub = list(prices[-window:]) if len(prices) >= window else list(prices)
    first = float(sub[0])
    last = float(sub[-1])
    trend_pct = ((last - first) / first * 100.0) if first > 0 else 0.0
    rsi = _rsi14(sub)
    h = hurst_exponent(sub)

    if rsi is not None:
        if rsi >= 75 and trend_pct >= 5:
            phase = "distribution"
            conf = min(1.0, (rsi - 75) / 25.0 + abs(trend_pct) / 30.0)
        elif rsi <= 25 and trend_pct <= -5:
            phase = "accumulation"
            conf = min(1.0, (25 - rsi) / 25.0 + abs(trend_pct) / 30.0)
        elif trend_pct >= 5:
            phase = "markup"
            conf = min(1.0, abs(trend_pct) / 20.0)
        elif trend_pct <= -5:
            phase = "markdown"
            conf = min(1.0, abs(trend_pct) / 20.0)
        else:
            phase = "ranging"
            conf = 1.0 - abs(trend_pct) / 5.0
    else:
        if trend_pct >= 5:
            phase = "markup"
            conf = 0.4
        elif trend_pct <= -5:
            phase = "markdown"
            conf = 0.4
        else:
            phase = "ranging"
            conf = 0.4

    return {
        "phase": phase,
        "rsi": rsi,
        "hurst": h,
        "trend_pct": round(trend_pct, 2),
        "confidence": round(max(0.0, min(1.0, conf)), 2),
    }


def detect_cycle_length(prices: Sequence[float], min_pivots: int = 3) -> Optional[int]:
    """检测主周期长度（天）：用 ZigZag 枢轴 → 平均峰-谷距离。"""
    pivots = zigzag_pivots(prices, threshold_pct=8.0)
    real_pivots = [p for p in pivots if p["type"] in ("high", "low")]
    if len(real_pivots) < min_pivots:
        return None
    diffs = []
    for i in range(1, len(real_pivots)):
        diffs.append(real_pivots[i]["idx"] - real_pivots[i - 1]["idx"])
    if not diffs:
        return None
    diffs.sort()
    mid = len(diffs) // 2
    if len(diffs) % 2 == 0 and len(diffs) > 1:
        return (diffs[mid - 1] + diffs[mid]) // 2
    return diffs[mid]


def predict_reversal(prices: Sequence[float],
                     reversal_threshold_pct: float = 3.0) -> Dict[str, Any]:
    """预测近期反转点概率 + 预计天数。

    三票投票：
      ① RSI 极值（> 70 顶背 / < 30 底背） → 1 票
      ② ZigZag 末段是否出现反向枢轴 → 1 票
      ③ Hurst > 0.7（强趋势）→ 反转概率反而更高（趋势易反转）→ 1 票
    """
    if len(prices) < _MIN_SAMPLES:
        return {"prob": 0.0, "days_to": None, "direction": None,
                "votes": 0, "rsi_vote": False, "pivot_vote": False,
                "hurst_vote": False}
    sub = list(prices[-30:]) if len(prices) >= 30 else list(prices)
    rsi = _rsi14(sub)
    # Hurst 用全部数据（last 30 样本不足）→ 反映主趋势持久性
    h = hurst_exponent(prices)
    pivots = zigzag_pivots(sub, threshold_pct=reversal_threshold_pct)

    rsi_vote = False
    direction: Optional[str] = None
    if rsi is not None:
        if rsi >= 75:
            rsi_vote = True
            direction = "down"
        elif rsi <= 25:
            rsi_vote = True
            direction = "up"

    pivot_vote = False
    real_pivots = [p for p in pivots if p["type"] in ("high", "low")]
    if len(real_pivots) >= 2:
        last_two = real_pivots[-2:]
        if last_two[0]["type"] != last_two[1]["type"]:
            pivot_vote = True
            if direction is None:
                direction = "down" if last_two[-1]["type"] == "low" else "up"

    hurst_vote = (h is not None and h >= 0.7)

    votes = sum([rsi_vote, pivot_vote, hurst_vote])
    prob = votes / 3.0

    days_to = None
    if real_pivots:
        last_idx = real_pivots[-1]["idx"]
        days_to = max(0, len(sub) - 1 - last_idx)

    return {
        "prob": round(prob, 3),
        "days_to": days_to,
        "direction": direction,
        "votes": votes,
        "rsi_vote": rsi_vote,
        "pivot_vote": pivot_vote,
        "hurst_vote": hurst_vote,
    }


def multi_timeframe_signal(prices_by_tf: Dict[str, Sequence[float]],
                           ) -> Dict[str, Any]:
    """多时间帧信号聚合（大/小/极短三档）。"""
    phases: Dict[str, str] = {}
    confidences: Dict[str, float] = {}
    for tf, prices in prices_by_tf.items():
        if not prices or len(prices) < _MIN_SAMPLES:
            phases[tf] = "unknown"
            confidences[tf] = 0.0
            continue
        result = detect_phase(prices)
        phases[tf] = result["phase"]
        confidences[tf] = result["confidence"]

    real = [p for p in phases.values() if p != "unknown" and p != "ranging"]
    if len(real) >= 2 and len(set(real)) == 1:
        alignment = "aligned"
        score = 1.0
    elif len(real) >= 2 and len(set(real)) == len(real):
        alignment = "diverging"
        score = 0.0
    else:
        alignment = "mixed"
        score = 0.5

    weighted: Dict[str, float] = {}
    for tf, p in phases.items():
        if p in ("unknown", "ranging"):
            continue
        weighted[p] = weighted.get(p, 0.0) + confidences[tf]
    dominant = max(weighted, key=weighted.get) if weighted else "ranging"

    hints = {
        "aligned": "三周期同步 → 趋势稳健，警惕极端末段",
        "diverging": "周期错位 → 行情震荡，等待共振",
        "mixed": "部分周期共振 → 关注分歧收敛的方向",
    }

    return {
        "phase_by_tf": phases,
        "alignment": alignment,
        "dominant_phase": dominant,
        "alignment_score": round(score, 2),
        "hint": hints[alignment],
    }


__all__ = [
    "zigzag_pivots",
    "hurst_exponent",
    "detect_phase",
    "detect_cycle_length",
    "predict_reversal",
    "multi_timeframe_signal",
    "extract_cycle_narrative",
]


# v302+ · cycle_analyst LLM 语义抽取
# 把硬指标 (phase / rsi / hurst / pivot votes) + 近期新闻摘要
# 喂给 LLM, 让它生成一段人类可读的「周期叙事」。
# 不依赖 causal_graph / causal_chains, 单独可用。
def extract_cycle_narrative(
    asset: str,
    metrics: Dict[str, Any],
    recent_news: Optional[Sequence[Dict[str, Any]]] = None,
    max_tokens: int = 320,
) -> Dict[str, Any]:
    """v302+ · 用 LLM 把硬指标 + 近期新闻翻译成一段叙事。

    Args:
        asset: 资产 symbol, e.g. 'BTC'
        metrics: predict_reversal() 或 multi_timeframe_signal() 的返回
        recent_news: [{title, source, ts}], 最多 5 条

    Returns:
        {ok, narrative, sources_used, elapsed_sec, error?}

    fallback: 当 LLM 不可用 (无 _llm 或返回 None) 时, 用模板拼接,
              仍给前端一个可用的字符串, 不让 UI 卡住。
    """
    import time as _t
    t0 = _t.monotonic()
    sources_used: List[str] = []

    # 尝试 LLM
    try:
        from causal_graph import _llm  # type: ignore
        prompt = _build_narrative_prompt(asset, metrics, recent_news or [])
        sys_prompt = (
            "你是一个量化研究助手, 负责把硬指标 + 新闻事实合成一段 1-2 句的周期叙事。"
            "只输出叙事本身, 不要解释, 不要编号, 不要 Markdown。"
            "如果证据冲突, 描述分歧; 不要发明数字。"
        )
        llm_out = _llm(prompt, sys_prompt, max_tokens=max_tokens)
        if llm_out and llm_out.strip():
            sources_used.append("llm")
            return {
                "ok": True,
                "narrative": llm_out.strip()[:600],
                "sources_used": sources_used,
                "elapsed_sec": round(_t.monotonic() - t0, 2),
            }
    except Exception as exc:
        llm_error = f"{type(exc).__name__}: {str(exc)[:80]}"

    # fallback: 模板拼接
    fb = _template_narrative(asset, metrics, recent_news or [])
    sources_used.append("template")
    out = {
        "ok": True,
        "narrative": fb,
        "sources_used": sources_used,
        "elapsed_sec": round(_t.monotonic() - t0, 2),
    }
    if llm_error:
        out["llm_error"] = llm_error
    return out


def _build_narrative_prompt(asset: str, metrics: Dict[str, Any], recent_news: Sequence[Dict[str, Any]]) -> str:
    """构造 LLM prompt。"""
    lines: List[str] = []
    lines.append(f"资产: {asset}")
    if "phase_by_tf" in metrics:
        lines.append(f"多周期相位: {metrics.get('phase_by_tf', {})}")
        lines.append(f"对齐度: {metrics.get('alignment', '')} ({metrics.get('alignment_score', '')})")
        lines.append(f"主相位: {metrics.get('dominant_phase', '')}")
    else:
        lines.append(f"相位: {metrics.get('phase', '')}")
        lines.append(f"反转概率: {metrics.get('prob', 0):.0%}")
        lines.append(f"方向: {metrics.get('direction', '')}")
        lines.append(f"距反转: {metrics.get('days_to', '')} 天")
    if recent_news:
        lines.append("近期新闻:")
        for n in recent_news[:5]:
            t = n.get("title", "").replace("\n", " ")[:120]
            lines.append(f"  - {t}")
    return "\n".join(lines)


def _template_narrative(asset: str, metrics: Dict[str, Any], recent_news: Sequence[Dict[str, Any]]) -> str:
    """当 LLM 不可用时的模板回退, 仍能给前端一个可读字符串。"""
    parts: List[str] = []
    phase = metrics.get("phase") or metrics.get("dominant_phase") or "未知"
    if phase == "markdown":
        parts.append(f"{asset} 当前处于下跌相位")
    elif phase == "markup":
        parts.append(f"{asset} 当前处于上涨相位")
    elif phase == "ranging":
        parts.append(f"{asset} 当前处于盘整相位")
    else:
        parts.append(f"{asset} 当前相位: {phase}")

    prob = metrics.get("prob")
    if isinstance(prob, (int, float)) and prob > 0:
        d = metrics.get("direction") or "?"
        parts.append(f"近反转概率 {prob:.0%} (方向 {d})")

    if recent_news:
        parts.append(f"近期 {len(recent_news)} 条相关新闻可作背景")
    return "; ".join(parts) + "。"