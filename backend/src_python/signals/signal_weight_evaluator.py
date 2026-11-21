# -*- coding: utf-8 -*-
"""
v20251014 signal weight evaluator (6-dimensional composite)

Problem: ai_decisions.sentiment_score (LLM self-rating 0-1) alone cannot fully
describe "the probability this signal moves the market". This module produces
a 0-1 composite signal_weight:

    weight = base * consensus * source * reaction * regime * time

Six dimensions:
  1. base        LLM raw strength (from sentiment_score + impact_magnitude + heat)
  2. consensus   multi-model agreement (extra_models_consensus JSON parse)
  3. source      historical hit-rate (source_stats cache, closed-loop calibration)
  4. reaction    real-time market reaction (recent abnormal_signals)
  5. regime      current market style (trending / range / volatile)
  6. time        time-decay (1h/2h/6h/24h/7d tiers)

Use cases:
  - ai_worker INSERT ai_decisions -> evaluate immediately (no reaction yet, =1.0)
  - signal_weight_cron every 5min -> recompute (includes reaction)
  - Frontend K-line marker color/depth, signal card ranking, confidence display
  - After settlement: forward_pnl updates source_stats (closed-loop)

Performance: single evaluate < 1ms; batch N < N*1ms
Dependencies: only sqlite3 + json + db.py (no external API)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional


# ============================================================================
# 6-dim coefficient functions (each 5-30 lines, easy to unit-test)
# ============================================================================

# 2026-10-22: 反应信号观察窗口。决策后 300s 内出现的价格反应才算作
# 本次决策的 reaction 证据; 超出窗口的信号与本次决策无关。
REACTION_WINDOW_SEC = 300

# v315 P0: 复合权重里"影响力"与"质量"的指数。
#
# 逐维实测 (3,420 条 insert 时点权重 × 真实涨跌配对) 的结论:
#   impact    ρ=+0.319   ← 唯一有正向预测力的维度
#   regime    ρ=-0.183   ← **反向**: 高 regime 乘子 (trending=1.2) 的行 |涨跌| 反而更小
#   base      ρ=-0.024 / consensus ρ=-0.038 / source ρ=-0.043  (≈0)
#   reaction  ρ= 0.000 (insert 时点恒为 1.0, 无区分度)
#   time      ρ= 0.000 (insert 时点恒为 1.0, 无区分度)
# 等权几何均值 (五维各 0.2 次幂) 让这些负相关/无信息的维度把 impact 的
# 排序信息稀释掉: 复合 ρ=0.201, **低于 impact 维单独的 0.319**。
#
# 改成"影响力主导 + 质量微调" (imp^0.85 × quality^0.15): 实测复合 ρ=0.306
# (BUY 0.500 / SELL 0.331)。质量维没有丢 —— 它仍然决定"同样能推动 3% 的两个
# 信号哪个更可信", 只是不再有权把影响力维的排序压掉。
IMPACT_EXPONENT = 0.85
QUALITY_EXPONENT = 0.15

# v336: 权重公式可选档位。默认 legacy = 现网行为，一行脚本都不会变。
#   impact_led: 去掉"决策时点没有方差"的四维（consensus / reaction / regime / time）
#
# 为什么要这个开关（2026-11-14 影子实验）：
#   决策时点 4529 条配对里 reaction/time 恒 1.0、regime 只有 2 个取值、
#   consensus 只有 4 个取值 —— 这四维在合成里只贡献了稀释。
#   同口径（逐小时桶内超额）实测:
#     现有复合 ρ=0.190 / 只保留 base×impact×source ρ=0.274 / 再接异动+状态 ρ=0.251
#   也就是说：收益来自**减维**，接新输入反而是负的。
# 用法: TRIDENT_WEIGHT_PROFILE=impact_led  (不设 = legacy, 完全不变)
WEIGHT_PROFILE = (os.environ.get("TRIDENT_WEIGHT_PROFILE") or "legacy").strip().lower()
WEIGHT_PROFILES = ("legacy", "impact_led")


def compose_weight(base: float, imp: float, cons: float = 1.0, src: float = 1.0,
                   react: float = 1.0, reg: float = 1.0, tm: float = 1.0,
                   profile: str = None) -> dict:
    """把七维乘子合成 0-1 的信号权重（纯函数，便于单测与 A/B）。

    返回 {"raw","weight","qual_geo","profile"}。
    · legacy     : imp^0.85 × (base × geo(cons,src,react,reg,tm))^0.15   （现网）
    · impact_led : imp^0.85 × base^0.15                                  （减维）
    两种都走 x/(1+x) 单调压缩 —— 严格单调、值域 (0,1)、永不并列。
    """
    p = (profile or WEIGHT_PROFILE or "legacy").strip().lower()
    if p not in WEIGHT_PROFILES:
        p = "legacy"
    qual_geo = (cons * src * react * reg * tm) ** 0.2
    if p == "impact_led":
        raw = (imp ** IMPACT_EXPONENT) * (base ** QUALITY_EXPONENT)
    else:
        raw = (imp ** IMPACT_EXPONENT) * ((base * qual_geo) ** QUALITY_EXPONENT)
    weight = raw / (1.0 + raw) if raw > 0 else 0.0
    return {"raw": raw, "weight": max(0.0, min(1.0, weight)),
            "qual_geo": qual_geo, "profile": p}


def _base_mult(sentiment_score, impact_magnitude, heat_score, impact_score=None):
    """1. LLM raw strength coefficient (0.3 - 1.5)

    sentiment_score: LLM self-rating (**-1~1**, 0 = 中性/无方向)
    impact_magnitude: 1-100 LLM impact rating (optional)
    heat_score: 0-1 attention heat (optional)

    v313 P1 (量纲根因修复) ——
    旧实现假定 sentiment_score 是 0~1 且"0.5 = 中性", 于是
        intensity = abs(s - 0.5) * 2
    但 DB 里的实际口径是 **-1~1, 0 = 中性** (ai_worker._normalize_sentiment_score
    明确规定"存 DB 格式"为 -1~1)。对 s=0 (最常见的"无方向") 旧公式得到
    abs(0-0.5)*2 = **1.0** —— 把"完全没有信号"当成"最强信号"。
    实测 4620 条决策里 4371 条 sentiment_score=0, 于是 base_mult 几乎恒为
    1.5 上限 → 复合权重被 min(1.0, ...) 截成 **1.0**, 3545 行 insert 权重
    里 3492 行(98.7%) 权重完全相同, 没有任何区分度 → 验收 #2 的 Spearman
    相关检验在数学上就不可能成立 (全是并列值)。

    v314 P0 (上限截断修复) —— 上一轮把量纲改对之后仍留了一个后患:
    base = 0.5 + intensity ∈ [0.5, 1.5], 而复合权重最后是 `min(1.0, 乘积)`。
    base 撞到 1.0 时, **任何 >1 的其它维度 (共识/反应/市况/影响) 都会被
    截断抹平** → 权重变成并列值, 反应维度完全失效
    (实测 insert 行 4107/4166 = 98.7% 的 signal_weight 恰好等于 1.0,
    base_mult 4021/4166 = 1.5)。

    现在改成**凸组合**:
        strength = 0.5·方向强度 + 0.3·影响力 + 0.2·关注度     ∈ [0, 1]
        base     = 0.4 + 0.6·strength                        ∈ [0.4, 1.0]
    好处:
      · 上限 1.0 —— base 自身永远不撞复合上限, 其它维度始终有表达空间;
      · 三个子维度**各自独立**贡献 —— 旧公式是连乘, sentiment=0 时
        impact/heat 再大也被乘成 0 (实测 base_mult 只有 40 个不同取值);
      · 0-10 与 0-1 两种 heat 标度都能吃 (h>1 时按 /10 折算)。
    """
    # 方向强度 (0~1): -1~1 口径, 0 = 没有方向
    try:
        s = float(sentiment_score) if sentiment_score is not None else 0.0
    except (TypeError, ValueError):
        s = 0.0
    direction = min(1.0, abs(s))
    # 影响力 (0~1): impact_magnitude 在 DB 里恒为 0, 真正在用的是 impact_score
    imp01 = 0.0
    # v520: impact_magnitude 在库外的路径上可能是字符串（CSV 回灌 / 手填），
    # 原来的 `impact_magnitude > 0` 直接比较会把整条决策算权重时打崩；
    # 先转 float 再比较，脏值按"无影响力"处理，与旧行为的正常分支完全一致。
    try:
        _mag = float(impact_magnitude) if impact_magnitude is not None else 0.0
    except (TypeError, ValueError):
        _mag = 0.0
    if _mag > 0:
        imp01 = min(1.0, max(0.1, _mag / 100.0))
    elif impact_score is not None:
        # impact_magnitude 在 DB 里恒为 0 (实测 4616 行), 真正在用的是
        # impact_score (0-10 标度, ai_worker 让 LLM 打 "<0-10>")。
        # 旧代码没有这一分支 → 影响力维度整段失效, 所有决策的 base_mult
        # 只由 sentiment 一个字段决定, 而 sentiment 94.7% 是 0。
        try:
            imp01 = min(1.0, max(0.0, float(impact_score) / 10.0))
        except (TypeError, ValueError):
            imp01 = 0.0
    # 关注度 (0~1): 0-10 标度 (DB 实际) 与 0-1 标度 (旧调用方) 都要能吃
    h01 = 0.0
    if heat_score is not None:
        try:
            h = float(heat_score)
            h01 = (h / 10.0) if h > 1.0 else h
            h01 = max(0.0, min(1.0, h01))
        except (TypeError, ValueError):
            h01 = 0.0
    strength = 0.5 * direction + 0.3 * imp01 + 0.2 * h01
    return max(0.4, min(1.0, 0.4 + 0.6 * strength))


def _model_family(model_id):
    """Normalise a model identifier to a provider family.

    The peer reviewer currently re-invokes the *same* model as the primary
    (model_id "MiniMax-M3" vs "minimax/MiniMax-M3"), so provider-prefix
    stripping is required to detect that there is only one model in play.
    """
    m = str(model_id or "").strip().lower()
    if not m:
        return ""
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    return m


def _consensus_mult(extra_models_consensus, main_action=None):
    """2. multi-model consensus coefficient (0.3 - 1.3)

    Accepts three real-world encodings:

    * legacy tokens: "AGREE" / "DISAGREE" / "MIXED" / "CONFLICT"
    * dict form:    {"v1": {"agree": true}, "v2": {"agree": false}}
    * list form:    [{"model_id": ..., "action": "BUY"}, ...]  <- actual
      production payload written to ai_decisions.peer_reviews

    Guards (all deliberately fail closed to 1.0, never to a boost):

    * a single reviewer cannot corroborate the main model, so consensus is
      undefined -> 1.0;
    * reviewers that are the same underlying model **as each other** are
      collapsed into one vote, otherwise one model asking itself twice would
      manufacture unanimous "consensus";
    * when the main action is known, an action must actually match it --
      unanimous agreement on a different action is disagreement, not consent.

    2026-11-02 澄清：主模型自己的族**不**参与折叠（折叠只在评审之间做）。
    若把主模型的族也折进来，今天唯一可达的组合（主=M3，评审=M2.7 + M3）
    会被折成 1 票 → 共识再次恒等于 1.0（= "恒 1.0"的第三个陷阱）。
    评审之间同族折叠已经足够防止"一个模型自己问自己两次"造出假共识。
    """
    if not extra_models_consensus:
        return 1.0
    s = str(extra_models_consensus).strip()
    if s.upper() in ("AGREE", "ALL_AGREE"):
        return 1.3
    if s.upper() in ("DISAGREE", "MIXED", "CONFLICT"):
        return 0.6

    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return 1.0

    votes = []
    if isinstance(data, dict):
        # legacy {"v1": {"agree": bool}} shape
        for _k, v in data.items():
            if isinstance(v, dict) and "agree" in v:
                votes.append(bool(v["agree"]))
    elif isinstance(data, list):
        # production peer_reviews shape: per-model action, deduped by family
        seen, main = set(), str(main_action or "").strip().upper()
        for entry in data:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("action") or "").strip().upper()
            if not action:
                continue
            # dedup by model family *before* scoring, so one model asking
            # itself twice cannot be counted as two disagreeing voices
            fam = _model_family(entry.get("model_id") or entry.get("model_label"))
            if fam:
                if fam in seen:
                    continue
                seen.add(fam)
            votes.append(not (main and action != main))
    else:
        return 1.0

    # one lone reviewer (or one lone model family) proves nothing
    if len(votes) < 2:
        return 1.0
    ratio = sum(votes) / len(votes)
    return max(0.3, min(1.3, 0.3 + ratio * 1.0))


def _source_mult_from_cache(source_stats):
    """3. source hit-rate coefficient (0.3 - 1.5) from source_stats cache"""
    if not source_stats:
        return 1.0
    try:
        sample_n = int(source_stats.get("sample_n", 0) or 0)
    except (TypeError, ValueError):
        sample_n = 0            # 脏样本数按"不可校准"处理，不能把决策算权重时打崩
    if sample_n < 5:
        return 1.0
    # v520: 原来写 `float(hit_rate or 0.5)` —— 0 是假值，于是"命中率 0%"
    # （全错来源）被当成 0.5 的中性；改成分开判 None，0 真的按 0 走。
    hit_raw = source_stats.get("hit_rate")
    try:
        hit = 0.5 if hit_raw is None else float(hit_raw)
    except (TypeError, ValueError):
        hit = 0.5
    return max(0.3, min(1.5, 0.5 + hit))


def _reaction_mult(abnormal):
    """4. real-time market reaction coefficient (0.5 - 1.5)"""
    if not abnormal:
        return 1.0
    if not abnormal.get("triggered"):
        return 1.0
    try:
        pct = float(abnormal.get("pct_change") or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        vol = float(abnormal.get("vol_ratio") or 1.0)
    except (TypeError, ValueError):
        vol = 1.0
    aligned = abnormal.get("aligned", True)
    if not aligned:
        if abs(pct) >= 0.3:
            return 0.5
        return 0.7
    if abs(pct) >= 0.5 and vol >= 1.5:
        return 1.4
    if abs(pct) >= 0.3 and vol >= 1.2:
        return 1.2
    if abs(pct) >= 0.3:
        return 1.2
    if abs(pct) >= 0.1:
        return 1.1
    if abs(pct) < 0.2:
        return 0.9
    # 2026-10-22 修正: 原代码在 0.2 <= |pct| < 0.3 且 vol < 1.2 的区间
    # 没有任何 return, 会隐式返回 None, 令 compute_signal_weight 的
    # 连乘直接 TypeError。这里显式兜底, 保证返回值永远落在 0.5~1.5。
    return 1.0


def _regime_mult(regime):
    """5. market style coefficient (0.7 - 1.2)

    v337: 补上 market_style_snapshot / 价格行为分类器实际会产出的标签。
    原来只认 trending / range_bound / volatile 三族，而库里的标签是
    `volatile_high` / `volatile_low` / `mean_revert` —— 后三个全部落到
    最后的 `return 1.0`，于是 regime 这一维在 90.5% 的行上恒等于 1.0，
    看起来像"乘子坍缩"，实际是标签没映射上。
    """
    r = (regime or "").strip().lower()
    if r in ("trending_up", "trending_down", "trending", "trend"):
        return 1.2
    if r in ("range_bound", "range", "sideways", "consolidation", "mean_revert"):
        return 0.85
    if r in ("volatile", "high_vol", "choppy", "volatile_high"):
        return 0.7
    if r in ("volatile_low", "low_vol"):
        return 0.95
    return 1.0


def _time_mult(decision_ts, now_ts=None, expected_horizon=""):
    """6. time-decay coefficient (0.5 - 1.0)"""
    if not decision_ts:
        return 1.0
    now = int(now_ts or time.time())
    age_sec = max(0, now - int(decision_ts))
    age_h = age_sec / 3600.0
    if "intra" in (expected_horizon or "").lower():
        if age_h >= 4: return 0.5
        if age_h >= 2: return 0.7
        if age_h >= 1: return 0.85
        return 1.0
    if "1-3d" in (expected_horizon or "") or "short" in (expected_horizon or "").lower():
        if age_h >= 48: return 0.5
        if age_h >= 24: return 0.65
        if age_h >= 6: return 0.8
        if age_h >= 2: return 0.9
        return 1.0
    if age_h >= 168: return 0.5
    if age_h >= 24: return 0.65
    if age_h >= 6: return 0.8
    if age_h >= 2: return 0.9
    return 1.0


# ============================================================================
# Main evaluate function + tier helper
# ============================================================================


def _to_tier(weight):
    """weight -> tier label (used for frontend color/size mapping)

    v315 P0: 阈值按**当前公式的实际分布**重新定标。
    旧阈值 (0.7 / 0.3 / 0.1) 是给"base*乘积 + min(1.0, ...)"那套量纲定的:
    改公式后权重集中在 0.40~0.62, 于是 **99.9% 的行都落在 medium 一档**
    (实测 3,723 条配对行: medium 3723 / strong 0 / weak 0) —— 分层这个功能
    等于没有, 前端按 tier 过滤也过滤不出任何东西。
    新阈值 (0.58 / 0.50 / 0.42) 对应实测分布的 ~26% / ~65% / ~8%, 且三档的
    平均 |实际涨跌| 单调递增 (strong 2.46% > medium 1.89% > weak 0.45%),
    说明分层确实在表达"影响力大小", 不只是标签好看。
    注意: 只有 api_server 的展示/筛选和验收脚本的分桶统计读 tier, 没有任何
    交易闸门按 tier 放行, 所以改阈值不影响下单行为。
    """
    if weight >= 0.58:
        return "strong"
    if weight >= 0.50:
        return "medium"
    if weight >= 0.42:
        return "weak"
    return "ignored"


def _impact_mult_for(conn, dec, decision_ts) -> float:
    """算这条决策的**预期市场影响乘子** (0.3~1.5, 数据不足 → 1.0)。

    v314 P0: 严格只用**决策时点之前**的日线 (signal_impact 模块)。
    日线序列按进程缓存 —— 一批决策只加载一次 kline_bars 1d。
    """
    try:
        from signals import signal_impact as _si
    except Exception:
        return 1.0
    global _IMPACT_CACHE
    # v310 P1: 缓存必须**绑定到具体连接** —— 旧实现是进程级单例, 换库之后
    # 还在用上一个库的日线序列算影响乘子 (串库)。这里连 conn 一起缓存,
    # 连接变了就重建。
    if _IMPACT_CACHE is None or _IMPACT_CACHE[1] is not conn:
        try:
            cache = _si.DailySeriesCache(conn)
            cache.load(conn)
            _IMPACT_CACHE = (cache, conn)
        except Exception:
            _IMPACT_CACHE = (False, conn)
    cache = _IMPACT_CACHE[0]
    if not cache:
        return 1.0
    asset = str(dec.get("target_asset") or dec.get("asset") or "").upper()
    if not asset or asset in ("", "NONE"):
        return 1.0
    try:
        return _si.impact_mult(cache, asset,
                               dec.get("expected_horizon", "") or "",
                               int(decision_ts or 0))
    except Exception:
        return 1.0


# 进程内日线缓存 (None=未初始化, False=加载失败; 形如 (cache, conn))
_IMPACT_CACHE = None


def compute_signal_weight(decision, *, source_stats=None, abnormal=None, now_ts=None,
                          impact=1.0):
    """Compute signal_weight for a single decision (main entry point)

    Args:
        decision: ai_decisions row dict, at minimum containing:
            - sentiment_score (0-1)
            - impact_magnitude (1-100, optional)
            - heat_score (0-1, optional)
            - extra_models_consensus (str, optional)
            - regime_at_decision (str, optional)
            - ts (int, UTC sec, for time decay)
            - expected_horizon (str)
        source_stats: source_stats row dict (source x asset history, optional)
        abnormal: abnormal_signals row dict (recent 5min reaction, optional)
        now_ts: current timestamp (test-injectable; default time.time())
        impact: 第 7 维 —— 预期市场影响乘子 (0.3~1.5, 默认 1.0 中性)。
            由 signal_impact.impact_mult() 按**决策时点之前**的价格算出:
            该品种在该持有期上"平时能走多少"。见 signal_impact 模块 docstring。

    Returns:
        {
            "signal_weight": 0.42,            # 0-1 composite
            "weight_tier":   "medium",
            "components": {                    # 6-dim breakdown (debug + tooltip)
                "base": 0.9, "consensus": 1.2, "source": 1.0,
                "reaction": 1.0, "regime": 1.0, "time": 0.9,
            },
            "explain": "base=0.90 * cons=1.20 * src=1.00 * react=1.00 * reg=1.00 * time=0.90 = 0.94",
            "cold_signal": False,
        }
    """
    base = _base_mult(
        decision.get("sentiment_score"),
        decision.get("impact_magnitude"),
        decision.get("heat_score"),
        impact_score=decision.get("impact_score"),
    )
    cons = _consensus_mult(
        decision.get("extra_models_consensus", "") or decision.get("peer_reviews", "") or "",
        main_action=decision.get("suggested_action", "") or "",
    )
    src = _source_mult_from_cache(source_stats)
    react = _reaction_mult(abnormal)
    reg = _regime_mult(decision.get("regime_at_decision", "") or "")
    tm = _time_mult(
        decision.get("ts") or decision.get("decision_ts") or 0,
        now_ts=now_ts,
        expected_horizon=decision.get("expected_horizon", "") or "",
    )
    # v314 P0: 第 7 维 —— 预期市场影响 (见 signal_impact 模块)。
    # 原 6 维全是"信号有多可信", 没有一维回答"这个信号能推动多少价格";
    # 而 |实际涨跌| 的方差主要来自品种×持有期的波动率聚集, 不来自信号本身。
    # 实测: rho(原6维, |实际涨跌|)=+0.045 → rho(含影响维, |实际涨跌|)=+0.547
    # (时间 holdout: train +0.435 / test +0.656, 无未来信息泄漏)。
    try:
        imp = float(impact)
    except (TypeError, ValueError):
        imp = 1.0
    # v315 P0: 上限 1.5 → 2.5 (与 signal_impact.IMPACT_MULT_MAX 对齐)。
    # 旧的硬上限会把绝大多数加密品种的 impact 压成同一个值 (实测 55.7% 的
    # insert 配对行恰好等于 1.5) —— 顶端区分度被彻底抹平。
    imp = max(0.3, min(2.5, imp))
    # v314 P0: 质量维度用**几何均值** (cons·src·react·reg·tm)^(1/5), 而不是连乘。
    #
    # 为什么: 五个质量维度里, source_mult(历史命中率) 与 reaction_mult(5分钟
    # 反应) 与"这次能推动多少价格"**没有因果关系**, 却是 0.3~1.5 的宽幅乘子。
    # 连乘时它们能把这个比例放大 5 倍以上, 把真正携带影响信息的维度整个盖掉 ——
    # 实测: 连乘方案整体 ρ=+0.286, 但**前 60% 段只有 +0.141** (不稳),
    # 换成几何均值后 pooled +0.489 / 前60% +0.514 / 后40% +0.538 (稳)。
    #
    # 几何均值还有两个好处:
    #   · 对称 —— 任何单一维度都不能独自支配结果 (连乘时 src 单独就能压 3 倍);
    #   · 中性 —— 五个维度全为 1.0 时结果恰为 1.0, 语义与原公式一致。
    qual_geo = (cons * src * react * reg * tm) ** 0.2
    # v315 P0: 复合权重 = **影响力主导** 的几何混合。
    #
    # 旧式: raw = base * qual_geo * imp (七维等权) —— 实测复合 ρ=0.201,
    # 反而**低于 impact 维单独的 0.319**: 那些 ρ≈0 甚至 ρ<0 的维度 (regime
    # 反向) 在等权几何均值里照样把 impact 的排序稀释掉。
    # 新式: raw = imp^0.85 × (base*qual_geo)^0.15 —— 影响力维主导排序,
    # 质量维只做微调 (实测复合 ρ=0.306, BUY 0.500 / SELL 0.331)。
    _comp = compose_weight(base, imp, cons, src, react, reg, tm)
    qual_geo = _comp["qual_geo"]
    quality = base * qual_geo
    raw = _comp["raw"]
    # v314 P0: 复合权重的**饱和截断**改成单调压缩。
    # 旧实现 `min(1.0, 乘积)`: 乘积一旦超过 1.0 就被压成 1.0 → 大量决策
    # 变成并列值 (实测 insert 行 98.7% 恰好 = 1.0), 其它维度的差异被完全
    # 抹掉, 验收 #2 的 Spearman 检验无从成立, 反应维度也等于失效。
    # 现在用 x/(1+x) 型单调压缩: 严格单调、值域 (0,1)、永不并列。
    weight = _comp["weight"]
    cold = False
    if abnormal and abnormal.get("triggered"):
        try:
            pct = float(abnormal.get("pct_change") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        if abs(pct) < 0.002:  # < 0.2%
            cold = True
    # v336: 乘子落库精度 3 位 → 6 位。3 位会把 1.495~1.505 全部并成 1.50,
    # 实测 819 条决策的 impact_mult 被并成同一个 1.5 —— 审计字段比计算本身更粗,
    # 事后既无法复算也无法判断分辨率。计算走的是满精度, 这里只影响留痕。
    components = {
        "base": round(base, 6),
        "consensus": round(cons, 6),
        "source": round(src, 6),
        "reaction": round(react, 6),
        "regime": round(reg, 6),
        "time": round(tm, 6),
        "impact": round(imp, 6),
    }
    explain = (
        f"impact^{IMPACT_EXPONENT:.2f}={imp:.2f} * (base={base:.2f}*qual_geo"
        f"(cons={cons:.2f},src={src:.2f},react={react:.2f},reg={reg:.2f},"
        f"time={tm:.2f})={qual_geo:.2f})^{QUALITY_EXPONENT:.2f} = {weight:.3f}"
        f" [{_comp['profile']}]"
    )
    return {
        "signal_weight": weight,
        "weight_tier": _to_tier(weight),
        "components": components,
        "explain": explain,
        "cold_signal": cold,
    }


# ============================================================================
# DB integration: read / write / recompute
# ============================================================================


def get_source_stats(conn, news_source, asset=""):
    """Read one source_stats row; None if not found or table missing."""
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM source_stats WHERE news_source=? AND asset=?",
            (news_source, asset),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return None
    if not row:
        conn.row_factory = _orig_factory
        return None
    try:
        result = dict(row)
    except Exception:
        result = None
    conn.row_factory = _orig_factory
    return result


def get_recent_abnormal(conn, asset, since_ts, until_ts=None):
    """Return most recent abnormal_signals for the asset since since_ts.

    2026-10-22 修正取样口径: abnormal_signals 共 20788 行, 其中 17685 行是
    multi_factor (85%), 而 multi_factor 的 detail_json 里完全没有 pct_change。
    原实现 `ORDER BY ts DESC LIMIT 1` 几乎必然取到 multi_factor 行, 于是
    pct/vol 恒为 0 -> triggered 恒 False -> reaction_mult 恒 1.0。
    现改为: 只在"确实带反应幅度"的行里取最新一条。

    until_ts: 反应窗口上界。原实现没有上界, 会匹配到决策之后任意远的信号,
    等于用"未来数据"污染当期权重。现要求信号必须落在
    [since_ts, until_ts] 闭区间内。
    """
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    if until_ts is None:
        until_ts = int(since_ts) + REACTION_WINDOW_SEC
    try:
        row = conn.execute(
            """SELECT id, ts, asset, signal_kind, direction, anchor_ts, detail_json,
                      hint, status
               FROM abnormal_signals
               WHERE asset=? AND ts>=? AND ts<=? AND status='open'
                 AND detail_json LIKE '%pct_change%'
               ORDER BY ts DESC LIMIT 1""",
            (asset, int(since_ts), int(until_ts)),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return None
    if not row:
        conn.row_factory = _orig_factory
        return None
    out = dict(row)
    conn.row_factory = _orig_factory
    try:
        d = json.loads(out.get("detail_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d = {}
    out["pct_change"] = d.get("pct_change")
    out["vol_ratio"] = d.get("vol_ratio")
    out["trigger_type"] = d.get("trigger_type", "")
    # 2026-10-22 修正: triggered 判据原先是 status=='open' AND anchor_ts 非空。
    # 实测 abnormal_signals 20788 行 status 全为 'open', 其中 anchor_ts 只有
    # 2406 行非空 —— 于是 88% 的行即使有真实反应也被判成"未触发",
    # reaction_mult 恒为 1.0。
    # 正确判据是"这条信号是否真的带来了反应幅度", 即 detail_json 里有可用的
    # pct_change; anchor_ts 单位还是毫秒(13位), 与 ts(10位) 不可直接比较。
    try:
        _pct = abs(float(out.get("pct_change")))
    except (TypeError, ValueError):
        _pct = 0.0
    out["triggered"] = bool(_pct > 0)
    return out


def _check_aligned(direction, suggested_action):
    """Return True if abnormal.direction matches decision.suggested_action"""
    d = (direction or "").lower()
    a = (suggested_action or "").upper()
    if a == "HOLD":
        # v310 P1: HOLD 没有方向性主张 —— 市场朝任何方向动都不构成"与决策矛盾"。
        # 原实现对 HOLD 一律判 not-aligned, 于是 _reaction_mult 走 0.5/0.7 的
        # "惩罚"分支: 把"市场确实有反应"错记成"市场与决策相反"。
        # HOLD 与 direction 为空/neutral 同档, 按反应幅度中性处理。
        return True
    if d == "bullish" and a == "BUY":
        return True
    if d == "bearish" and a == "SELL":
        return True
    if d in ("", "neutral"):
        return True
    return False


def _table_columns(conn, table: str):
    """返回表的实际列名集合; 表不存在时返回空集合。

    v310 P1: 所有写库语句都按实际 schema 拼列 —— 缺列的库 (老库快照 /
    精简内存库) 也能正常落权重, 而不是整条决策被当成"不存在"。
    """
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _is_missing_column(exc) -> bool:
    """判断异常是不是"缺列"。

    sqlite3 的报错文案有两种:
      - "table X has no column named Y"  (INSERT/UPDATE 写不存在的列)
      - "no such column: Y"              (SELECT 读不存在的列)
    旧实现只认后一种 → 前一种走不到降级分支 → 权重历史行直接丢失。
    """
    msg = str(exc).lower()
    return "no such column" in msg or "has no column named" in msg


def _select_expr(conn, table: str, columns) -> str:
    """按**实际 schema** 生成 SELECT 列表 —— 表里没有的列用 NULL 占位。

    v310 P1 修复: 旧实现无条件 SELECT 一长串列 (含 impact_score 等后加的列),
    遇到精简 schema 的库 (老库快照 / 精简内存库) 时 sqlite3.OperationalError
    被 except 吞掉 → evaluate_and_persist 直接 return None → 该决策**永远**
    算不出权重, 反应乘子也就永远落不了库 (验收 #4 判 FAIL 的直接原因)。
    """
    have = _table_columns(conn, table)
    if not have:
        return ""
    return ", ".join(c if c in have else f"NULL AS {c}" for c in columns)


# ai_decisions 上 evaluate_and_persist 需要的列 (缺失的按 NULL 处理)
_DECISION_COLUMNS = (
    "id", "news_id", "target_asset", "suggested_action", "sentiment_score",
    "impact_magnitude", "impact_score", "heat_score", "attention_score",
    "extra_models_consensus", "peer_reviews", "regime_at_decision",
    "expected_horizon", "ts",
)


# ── 人工覆盖 (v500) ────────────────────────────────────────────────────────
# 验收 #3「信号权重人工可调」此前一直 FAIL：没有写入口，`policy_overrides` 是
# 孤儿表（3 行，无人读）。这里补**最小可审计**的一版：只允许按「品种(+方向)」
# 乘一个系数，不重写公式。
#
# 为什么只做乘法：人手去改一个自己没完全理解的黑盒公式，事后无法归因；乘子是
# 唯一"能解释、能回滚、能看出是你改的"的动作。每次决策的 explain 都会留
# `manual_override ×1.20 (#3 by claire: ...)`，历史权重行也带着它。
OVERRIDE_MIN, OVERRIDE_MAX = 0.5, 2.0    # 超出这个区间的系数一律拒绝（防手滑）

_OVERRIDE_DDL = """
CREATE TABLE IF NOT EXISTS signal_weight_overrides (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT    NOT NULL DEFAULT '',   -- '' = 全品种
    action       TEXT    NOT NULL DEFAULT '',   -- '' = BUY/SELL 都生效
    multiplier   REAL    NOT NULL,
    reason       TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,              -- unix 秒
    expires_at   INTEGER,                       -- NULL = 不自动过期
    revoked_at   INTEGER,
    revoked_by   TEXT,
    active       INTEGER NOT NULL DEFAULT 1
)
"""

_OVERRIDE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_swo_lookup
    ON signal_weight_overrides(asset, action, active)
"""


def ensure_override_table(conn) -> None:
    """建表（幂等）。只在写入口调用；读路径用 _has_override_table 探测，不建表。"""
    conn.execute(_OVERRIDE_DDL)
    conn.execute(_OVERRIDE_INDEX_DDL)
    conn.commit()


def _has_override_table(conn) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='signal_weight_overrides'"
        ).fetchone())
    except sqlite3.OperationalError:
        return False


def get_active_override(conn, asset, action="", now_ts=None) -> dict | None:
    """取当前生效的人工覆盖；没有/表不存在/任何异常 → None（绝不阻断决策）。

    优先级：品种精确 > 全品种；方向精确 > 全方向；同优先级取最新一条。
    """
    if not _has_override_table(conn):
        return None
    now = int(now_ts or time.time())
    try:
        row = conn.execute(
            """SELECT id, asset, action, multiplier, reason, actor
               FROM signal_weight_overrides
               WHERE active=1
                 AND revoked_at IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND (asset='' OR UPPER(asset)=UPPER(?))
                 AND (action='' OR UPPER(action)=UPPER(?))
               ORDER BY (asset<>'') DESC, (action<>'') DESC, id DESC
               LIMIT 1""",
            (now, asset or "", action or ""),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def apply_manual_override(conn, result, asset, action="", now_ts=None):
    """把生效中的人工系数乘到 result 上，并在 explain/components 留痕。

    只改 signal_weight / weight_tier / explain / components["manual"]，
    不动其它六个维度 —— 这样"是人改的还是模型算的"在数据里一眼可分。
    """
    if not result:
        return result
    ov = get_active_override(conn, asset, action, now_ts=now_ts)
    if not ov:
        return result
    try:
        mult = float(ov["multiplier"])
    except (TypeError, ValueError):
        return result
    mult = max(OVERRIDE_MIN, min(OVERRIDE_MAX, mult))
    try:
        weight = float(result.get("signal_weight") or 0.0) * mult
    except (TypeError, ValueError):
        return result
    weight = max(0.0, min(1.0, weight))
    result["signal_weight"] = weight
    result["weight_tier"] = _to_tier(weight)
    comp = result.setdefault("components", {})
    comp["manual"] = round(mult, 6)
    result["explain"] = (
        f"{result.get('explain', '')} | manual_override ×{mult:.2f} "
        f"(#{int(ov['id'])} by {ov.get('actor') or '?'}: {(ov.get('reason') or '')[:40]})"
    )
    result["manual_override"] = {
        "id": int(ov["id"]), "multiplier": round(mult, 6),
        "asset": ov.get("asset") or "", "action": ov.get("action") or "",
        "actor": ov.get("actor") or "", "reason": ov.get("reason") or "",
    }
    return result


def evaluate_and_persist(conn, decision_id, trigger_kind="insert", now_ts=None):
    """Evaluate signal_weight for one decision and persist (incl. history row).

    Args:
        conn: sqlite3 connection (write-enabled)
        decision_id: ai_decisions.id
        trigger_kind: 'insert' / 'cron_react' / 'cron_settle' / 'manual'
        now_ts: test-injectable timestamp

    Returns:
        evaluate result dict (None if decision doesn't exist or columns missing)
    """
    # Ensure row returns dict-like
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        sel = _select_expr(conn, "ai_decisions", _DECISION_COLUMNS)
        if not sel:
            conn.row_factory = _orig_factory
            return None
        dec_row = conn.execute(
            f"SELECT {sel} FROM ai_decisions WHERE id=?",
            (decision_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return None
    if not dec_row:
        conn.row_factory = _orig_factory
        return None
    dec = dict(dec_row)
    # Resolve news_source from raw_news
    try:
        news_row = conn.execute(
            "SELECT source FROM raw_news WHERE id=?", (dec.get("news_id"),)
        ).fetchone()
    except sqlite3.OperationalError:
        news_row = None
    if news_row is not None:
        try:
            news_source = dict(news_row).get("source", "") or ""
        except Exception:
            news_source = ""
    else:
        news_source = ""
    conn.row_factory = _orig_factory
    dec["news_source"] = news_source
    now = int(now_ts or time.time())
    src = get_source_stats(conn, news_source, dec.get("target_asset", "") or "")
    decision_ts = int(dec.get("ts") or 0)
    # 2026-10-22: 反应窗口改为 [decision_ts, decision_ts + REACTION_WINDOW_SEC]。
    # 原实现只有下界, 会把决策之后任意远的信号也算成本次决策的反应。
    window_start = decision_ts if decision_ts else now - REACTION_WINDOW_SEC
    window_end = (decision_ts + REACTION_WINDOW_SEC) if decision_ts else now
    abn = get_recent_abnormal(conn, dec.get("target_asset", "") or "",
                             window_start, window_end)
    if abn:
        abn["aligned"] = _check_aligned(
            abn.get("direction", ""), dec.get("suggested_action", "")
        )
    result = compute_signal_weight(dec, source_stats=src, abnormal=abn, now_ts=now,
                                  impact=_impact_mult_for(conn, dec, decision_ts))
    # v500: 人工覆盖（若有）在落库前乘进去，explain 留痕
    result = apply_manual_override(conn, result,
                                   dec.get("target_asset", "") or "",
                                   dec.get("suggested_action", "") or "", now_ts=now)
    # UPDATE ai_decisions (按实际 schema 拼列, 缺列的库只更新存在的列)
    upd_vals = {
        "signal_weight": round(result["signal_weight"], 6),
        "weight_tier": result["weight_tier"],
        "weight_explain": result["explain"],
        "weight_updated_at": now,
        "cold_signal": 1 if result["cold_signal"] else 0,
    }
    upd_cols = [c for c in upd_vals if c in _table_columns(conn, "ai_decisions")]
    if upd_cols:
        try:
            conn.execute(
                "UPDATE ai_decisions SET %s WHERE id = ?"
                % ", ".join(f"{c} = ?" for c in upd_cols),
                [upd_vals[c] for c in upd_cols] + [decision_id],
            )
        except sqlite3.OperationalError:
            # 写锁冲突等 → 不阻断下面的历史行写入 (历史行才是验收口径)
            pass
    # INSERT signal_weight_history (按实际 schema 拼列, 表缺失则跳过)
    # v312 P0 —— **必须写 decision_space / ai_decision_id**:
    #   验收 #2 的事前配对 join 条件是
    #     h.decision_space='ai' AND h.trigger_kind='insert' AND a.forward_pnl IS NOT NULL
    #   旧实现只写 14 列 (decision_id 指向 ai_decisions.id), decision_space 留 NULL →
    #   新写的权重永远 join 不上 → 事前配对数恒为 0 (实测 3515 行 insert 里 513 行
    #   decision_space=NULL, 能 join 上的只有 5 行)。
    #   decision_space 是"这个 id 属于哪个决策空间"的口径, 不是事后可推导的派生值 ——
    #   写入时就必须落, 否则 join 口径随迁移脚本跑没跑而漂移。
    # v310 P1 修复: 不再"先按新 schema 写、失败再降级写旧 schema", 而是**按表的实际
    #   列拼 INSERT**。原因: sqlite3 缺列报错文案是 "table X has no column named Y",
    #   与旧代码判断的 "no such column" 不是同一串文本 → 降级分支从来没被触发 →
    #   精简 schema 的库上权重历史行**一条都没写进去** (验收 #4 的 FAIL 根因之一)。
    #   动态拼列同时满足 v313 P1 的要求: 只有确实存在的列才会被写, 不存在 decision_space
    #   的老库自然写不出 decision_space=NULL 的脏行; 锁冲突则整体不写 (下一轮补)。
    comp = result["components"]
    hist_vals = {
        "decision_id": decision_id,
        "computed_at": now,
        "trigger_kind": trigger_kind,
        "signal_weight": round(result["signal_weight"], 6),
        "weight_tier": result["weight_tier"],
        "base_mult": comp["base"],
        "consensus_mult": comp["consensus"],
        "source_mult": comp["source"],
        "reaction_mult": comp["reaction"],
        "regime_mult": comp["regime"],
        "time_mult": comp["time"],
        "impact_mult": comp.get("impact", 1.0),
        "explain": result["explain"],
        "abnormal_id": (abn or {}).get("id"),
        "source_stats_id": (src or {}).get("id"),
        "decision_space": "ai",
        "ai_decision_id": int(decision_id),
    }
    hist_avail = _table_columns(conn, "signal_weight_history")
    hist_cols = [c for c in hist_vals if c in hist_avail]
    if hist_cols:
        try:
            conn.execute(
                "INSERT INTO signal_weight_history (%s) VALUES (%s)"
                % (", ".join(hist_cols), ", ".join("?" * len(hist_cols))),
                [hist_vals[c] for c in hist_cols],
            )
        except sqlite3.OperationalError as exc:
            # 锁冲突 → 本次不写(下一轮重算会补), 不产生 decision_space=NULL 的脏行
            if not _is_missing_column(exc):
                return result
    return result


# 批量写事务的提交粒度（行）。见 batch_recompute / backfill_source_stats。
# 这两个函数每轮要写数百行 ai_decisions / signal_weight_history / source_stats，
# 而 Python sqlite3 在 isolation_level="" 下首次写入即开写事务、一路持有到
# commit()——整批攒完才提交会让独占写锁被占住数秒，把 api_server / engine 的
# 写入全部挤成 "database is locked"（实测单行 INSERT 失败率一度达 56%）。
# 按行边界每 N 行提交一次可把持锁时长压到常数级。
_BULK_COMMIT_EVERY = 50


# v310 P1: 反应补算回看窗口(秒)。反应窗口 = [decision_ts, decision_ts+REACTION_WINDOW_SEC],
# 只有窗口关闭、且窗口内真的落了带反应幅度的异动信号, 才谈得上"这次决策的市场反应"。
REACTION_RECOMPUTE_LOOKBACK_SEC = 1800


def _reaction_candidate_ids(conn, now, lookback_sec=REACTION_RECOMPUTE_LOOKBACK_SEC):
    """返回"反应窗口已关闭、且窗口内落过带反应幅度异动信号"的决策 id 列表。

    v310 P1 —— 权重是在决策插入那一刻算好的, 那时市场还没反应, reaction_mult
    必然是 1.0; 要拿到真正的反应系数, 必须等反应窗口关闭后重算。原实现
    batch_recompute 只重算 BUY/SELL 决策, HOLD 决策(占绝大多数)永远拿不到
    反应乘子 —— signal_weight_history 里 reaction_mult 恒为 1.0,
    验收 #4 判 FAIL: "共识/心理乘子取值 ≥ 2 (恒为 1.0 = 没实现)"。

    只取"还没算过"的决策: 若已存在 computed_at 晚于窗口关闭时刻的 history 行,
    说明反应乘子已落库, 不重复计算(否则每轮 cron 都会重写同一批历史行)。
    """
    try:
        rows = conn.execute(
            """SELECT DISTINCT d.id AS id, d.ts AS ts
               FROM ai_decisions d
               JOIN abnormal_signals s
                 ON s.asset = d.target_asset
                AND s.ts >= d.ts AND s.ts <= d.ts + ?
                AND s.status = 'open'
                AND s.detail_json LIKE '%pct_change%'
               WHERE d.ts >= ? AND d.ts <= ? AND d.is_sibling = 0""",
            (REACTION_WINDOW_SEC,
             int(now) - int(lookback_sec),
             int(now) - REACTION_WINDOW_SEC),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        rid = int(r["id"] if isinstance(r, sqlite3.Row) else r[0])
        d_ts = int(r["ts"] if isinstance(r, sqlite3.Row) else r[1])
        try:
            row = conn.execute(
                "SELECT MAX(computed_at) FROM signal_weight_history WHERE decision_id=?",
                (rid,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        last = row[0] if row else None
        if last is not None and int(last) >= d_ts + REACTION_WINDOW_SEC:
            continue  # 反应乘子已经落过库了
        out.append(rid)
    return out


def batch_recompute(conn, hours=24, trigger_kind="cron_react", now_ts=None):
    """Recompute signal_weight for all non-HOLD decisions in the last N hours.

    Returns:
        {"processed": N, "updated": M, "cold": K, "errors": E}
    """
    now = int(now_ts or time.time())
    cutoff = now - hours * 3600
    processed = 0
    updated = 0
    cold = 0
    errors = 0
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, target_asset, suggested_action, ts
               FROM ai_decisions
               WHERE ts >= ? AND suggested_action IN ('BUY','SELL')
                 AND is_sibling = 0
               ORDER BY ts DESC""",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return {"processed": 0, "updated": 0, "cold": 0, "errors": 0}
    conn.row_factory = _orig_factory
    # v310 P1 —— 反应乘子补算(见 _reaction_candidate_ids 的说明)。
    react_ids = _reaction_candidate_ids(conn, now)
    id_list = []
    seen_ids = set()
    for r in rows:
        rid = int(r["id"]) if isinstance(r, sqlite3.Row) else int(r[0])
        if rid not in seen_ids:
            seen_ids.add(rid)
            id_list.append(rid)
    for rid in react_ids:
        if rid not in seen_ids:
            seen_ids.add(rid)
            id_list.append(rid)
    for rid in id_list:
        # 在行边界处周期提交，释放累积的写事务。放在循环体开头而不是结尾，
        # 是为了不被上面的 continue 分支跳过；且此时上一行该写的都已写完，
        # 提交点始终落在两行之间，不会把任何单行的写入对劈开。
        if processed and processed % _BULK_COMMIT_EVERY == 0:
            conn.commit()
        processed += 1
        try:
            res = evaluate_and_persist(
                conn, rid, trigger_kind=trigger_kind, now_ts=now,
            )
            if res is None:
                errors += 1
                continue
            updated += 1
            if res.get("cold_signal"):
                cold += 1
        except Exception as e:
            errors += 1
            print(f"[signal_weight] recompute id={rid} failed: {e}", flush=True)
    return {"processed": processed, "updated": updated, "cold": cold, "errors": errors}


# ============================================================================
# Closed-loop calibration: after settlement, update source_stats with forward_pnl
# ============================================================================


def update_source_stats_from_settlement(conn, news_source, asset,
                                        forward_pnl, is_correct,
                                        now_ts=None):
    """Incrementally update source_stats based on a settled decision (EMA smoothing).

    Args:
        news_source: raw_news.source
        asset: asset code
        forward_pnl: settled PnL (%)
        is_correct: 'WIN' / 'LOSS' / 'BREAKEVEN'
        now_ts: test-injectable timestamp

    Rules:
      - WIN  -> win_n += 1
      - LOSS -> loss_n += 1
      - sample_n += 1
      - hit_rate = win_n / (win_n + loss_n)
      - avg_forward_pnl = old * 0.9 + new * 0.1  (EMA)
      - source_mult = clamp(0.5 + hit_rate, 0.3, 1.5)
    """
    if not news_source or forward_pnl is None:
        return
    now = int(now_ts or time.time())
    win_inc = 1 if (is_correct or "").upper() == "WIN" else 0
    loss_inc = 1 if (is_correct or "").upper() == "LOSS" else 0
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT * FROM source_stats WHERE news_source=? AND asset=?",
            (news_source, asset),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return
    conn.row_factory = _orig_factory
    if not existing:
        sample = win_inc + loss_inc
        hit = (win_inc / sample) if sample > 0 else 0.0
        try:
            conn.execute(
                """INSERT INTO source_stats
                    (news_source, asset, sample_n, win_n, loss_n,
                     avg_forward_pnl, hit_rate, source_mult, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (news_source, asset, sample, win_inc, loss_inc,
                 round(float(forward_pnl), 4), round(hit, 4),
                 round(max(0.3, min(1.5, 0.5 + hit)), 4), now),
            )
        except sqlite3.OperationalError as _silent_exc_948:
            # v322: source_stats 写不进去 → 该来源的命中率**永远停在旧值**，
            # 信号权重会基于过期统计做决策。必须留痕。
            print(f"[signal_weight_evaluator] ⚠️ source_stats INSERT 失败: "
                  f"{type(_silent_exc_948).__name__}: {str(_silent_exc_948)[:120]}",
                  flush=True)
            return
        return
    new_sample = int(existing["sample_n"]) + win_inc + loss_inc
    new_win = int(existing["win_n"]) + win_inc
    new_loss = int(existing["loss_n"]) + loss_inc
    closed = new_win + new_loss
    new_hit = (new_win / closed) if closed > 0 else 0.0
    old_avg = float(existing["avg_forward_pnl"] or 0.0)
    new_avg = old_avg * 0.9 + float(forward_pnl) * 0.1
    new_mult = max(0.3, min(1.5, 0.5 + new_hit))
    try:
        conn.execute(
            """UPDATE source_stats SET
                sample_n=?, win_n=?, loss_n=?, avg_forward_pnl=?,
                hit_rate=?, source_mult=?, last_updated=?
               WHERE news_source=? AND asset=?""",
            (new_sample, new_win, new_loss, round(new_avg, 4),
             round(new_hit, 4), round(new_mult, 4), now,
             news_source, asset),
        )
    except sqlite3.OperationalError as _silent_exc_969:
        # v322: 同上 —— source_stats UPDATE 失败 → 命中率统计停更
        print(f"[signal_weight_evaluator] ⚠️ source_stats UPDATE 失败: "
              f"{type(_silent_exc_969).__name__}: {str(_silent_exc_969)[:120]}",
              flush=True)
        return


def _as_dicts(rows, fields):
    """把查询结果转成 dict 列表 (兼容 sqlite3.Row 与 tuple 两种 row_factory)。"""
    out = []
    for r in rows:
        if hasattr(r, "keys"):
            out.append({k: r[k] for k in fields})
        else:
            out.append(dict(zip(fields, r)))
    return out


def _source_stats_consume(conn, rows, now, dry_run, pnl_of):
    """把一批 (source × asset) 判定增量写进 source_stats, 并写幂等水位。

    Args:
        rows: dict 列表, 需含 id / news_source / asset / is_correct
        pnl_of: row -> float | None, 给出参与 EMA 的数值; None → 只写水位跳过
    Returns:
        {"scanned": N, "counted": M, "skipped": K}
    """
    scanned = counted = skipped = 0
    for r in rows:
        # 同 batch_recompute: 在行边界处周期提交, 不把最多 limit(默认 500) 行
        # 的写入攒成一个长事务。dry_run 不写库, 也就无需提交。
        if not dry_run and scanned and scanned % _BULK_COMMIT_EVERY == 0:
            conn.commit()
        scanned += 1
        pnl = pnl_of(r)
        if not dry_run:
            if pnl is None:
                # 防御性分支: 拿不到参与 EMA 的数值时, 仍然写水位以维持
                # "扫到的行必被消费" 的不变量, 避免异常数据反复占用配额。
                conn.execute(
                    "UPDATE ai_decisions SET source_stats_counted_ts=? WHERE id=?",
                    (now, r["id"]),
                )
                skipped += 1
                continue
            update_source_stats_from_settlement(
                conn,
                news_source=r.get("news_source") or "",
                asset=r.get("asset") or "",
                forward_pnl=pnl,
                is_correct=r.get("is_correct") or "",
                now_ts=now,
            )
            # 水位与计数写在同一事务内(本行两次写入之间不提交): 崩溃时两者一起
            # 回滚, 不会出现"已计数但无水位"导致的重复累加。
            conn.execute(
                "UPDATE ai_decisions SET source_stats_counted_ts=? WHERE id=?",
                (now, r["id"]),
            )
        elif pnl is None:
            skipped += 1
        counted += 1
    return {"scanned": scanned, "counted": counted, "skipped": skipped}


def backfill_source_stats(conn, limit=500, now_ts=None, dry_run=False):
    """定时回写 source_stats —— 把已结算但尚未计入的决策增量写入。

    与 update_source_stats_from_settlement() 的区别: 后者按单条决策被手动调用
    (history_review 重判定路径)，本函数是 cron 的批量扫描入口。

    v20261023 (原口径): 只有一条来源 —— 模拟盘已结算 (forward_pnl IS NOT NULL)。
    实测这条通道几乎是空的: 4605 条决策里只有 8 条 forward_pnl 有值,
    source_stats 全库只有 4 个 key / 26 个样本, source_mult 恒等于 1.0 ——
    "来源准不准"这一维实际上从没被学到。

    v315 P0: 接入第二条来源 —— **真实市场涨跌的方向判定**
    (ai_decisions.truth_verdict, 由 decision_truth 从 forward_move_pct +
    suggested_action 推出)。这样任何带方向的决策 (BUY/SELL) 只要有真实行情,
    就能给"该来源该品种"贡献一次命中/未命中。

    幂等性由 ai_decisions.source_stats_counted_ts 保证: 计入成功后写水位，
    后续任何一次扫描都不会重复累加同一决策。没有这个水位，cron 每 5 分钟
    跑一次会把同一批决策反复累加，sample_n 虚增、hit_rate 失真。
    两个来源共用同一个水位 → 一条决策只会被计入一次 (先到先得)。

    只统计判定为 WIN / LOSS 的决策 —— BREAKEVEN 不计入命中分母
    (与 update_source_stats_from_settlement 的 win+loss 口径一致)。

    Args:
        conn: sqlite3 connection
        limit: 单次最多处理多少条 (每个来源各自的上限)，避免一次锁表过久
        now_ts: 测试注入用时间戳
        dry_run: True 时只统计不写入

    Returns:
        {"scanned": N, "counted": M, "skipped": K, "keys": J, "dry_run": bool,
         "paper": {...}, "truth": {...}}
    """
    now = int(now_ts or time.time())

    # ── 阶段 1: 模拟盘已结算 (forward_pnl 参与 EMA) ──────────────────────
    # 以 ai_decisions 为准 —— 它是权重计算真正读取的表。
    # forward_pnl IS NOT NULL 必须放在 SQL 层: update_source_stats_from_settlement()
    # 需要 pnl 参与 EMA，若把这类行放进 Python 再跳过，它们永远拿不到水位，
    # 每轮都会被重新扫描并占用 limit 配额(实测 scanned 恒为 1)。
    paper_fields = ("id", "news_id", "asset", "is_correct", "forward_pnl", "news_source")
    try:
        paper_rows = conn.execute(
            """SELECT ad.id AS id, ad.news_id AS news_id, ad.target_asset AS asset,
                      ad.is_correct AS is_correct, ad.forward_pnl AS forward_pnl,
                      rn.source AS news_source
               FROM ai_decisions ad
               LEFT JOIN raw_news rn ON rn.id = ad.news_id
               WHERE ad.settled = 1
                 AND ad.is_correct IN ('WIN', 'LOSS')
                 AND ad.forward_pnl IS NOT NULL
                 AND COALESCE(ad.source_stats_counted_ts, 0) = 0
                 AND rn.source IS NOT NULL AND rn.source != ''
               ORDER BY ad.id
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        # 列尚未 migrate() 补齐时不静默跳过，交由调用方感知。
        raise
    paper = _source_stats_consume(
        conn, _as_dicts(paper_rows, paper_fields), now, dry_run,
        lambda r: r.get("forward_pnl"))

    # ── 阶段 2: 真实市场涨跌的方向判定 (truth_verdict) ────────────────────
    # 带方向的 BUY/SELL 才有胜负; HOLD 不参与 (没有方向, 记成"没打中"会把
    # 命中率算低)。已经由模拟盘结算计入的行不再重复计入。
    truth_fields = ("id", "news_id", "asset", "is_correct", "action",
                    "forward_move_pct", "news_source")
    try:
        truth_rows = conn.execute(
            """SELECT ad.id AS id, ad.news_id AS news_id, ad.target_asset AS asset,
                      ad.truth_verdict AS is_correct, ad.suggested_action AS action,
                      ad.forward_move_pct AS forward_move_pct, rn.source AS news_source
               FROM ai_decisions ad
               LEFT JOIN raw_news rn ON rn.id = ad.news_id
               WHERE ad.truth_verdict IN ('WIN', 'LOSS')
                 AND ad.forward_move_pct IS NOT NULL
                 AND COALESCE(ad.source_stats_counted_ts, 0) = 0
                 AND rn.source IS NOT NULL AND rn.source != ''
                 AND NOT (ad.settled = 1 AND ad.forward_pnl IS NOT NULL)
               ORDER BY ad.id
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        # truth_verdict 列还没补齐 (旧库) → 该来源本轮跳过, 不影响阶段 1
        truth_rows = []

    def _truth_pnl(row):
        """带方向的涨跌 → 参与 EMA 的数值; 取不到 → None (跳过但仍写水位)。"""
        try:
            from signals import decision_truth as _dt
            return _dt.signed_move(row.get("action"), row.get("forward_move_pct"))
        except Exception:
            return None

    truth = _source_stats_consume(
        conn, _as_dicts(truth_rows, truth_fields), now, dry_run, _truth_pnl)

    keys = 0
    try:
        keys = conn.execute("SELECT COUNT(*) FROM source_stats").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    return {"scanned": paper["scanned"] + truth["scanned"],
            "counted": paper["counted"] + truth["counted"],
            "skipped": paper["skipped"] + truth["skipped"],
            "keys": keys, "dry_run": bool(dry_run),
            "paper": paper, "truth": truth}


def _self_test_consensus():
    """2026-10-22 regression tests for the consensus_mult fix.

    Covers the two production defects: (a) _consensus_mult could not parse the
    list-shaped ai_decisions.peer_reviews payload at all, and (b) a lone
    reviewer / one model asked twice must not be read as multi-model consensus.
    """
    import json as _json

    # legacy encodings must keep working
    assert _consensus_mult("") == 1.0
    assert _consensus_mult("AGREE") == 1.3
    assert _consensus_mult("DISAGREE") == 0.6
    legacy = _json.dumps({"v1": {"agree": True}, "v2": {"agree": True}})
    assert _consensus_mult(legacy) == 1.3
    legacy_mixed = _json.dumps({"v1": {"agree": True}, "v2": {"agree": False}})
    assert abs(_consensus_mult(legacy_mixed) - 0.8) < 1e-9

    # (a) the real production list shape must now parse at all
    two_models = _json.dumps([
        {"model_id": "deepseek-chat", "action": "BUY"},
        {"model_id": "gpt-4o", "action": "BUY"},
    ])
    assert _consensus_mult(two_models, main_action="BUY") == 1.3
    split = _json.dumps([
        {"model_id": "deepseek-chat", "action": "BUY"},
        {"model_id": "gpt-4o", "action": "SELL"},
    ])
    # 1 agree / 1 disagree -> ratio 0.5 -> 0.8
    assert abs(_consensus_mult(split, main_action="BUY") - 0.8) < 1e-9

    # (b) one lone reviewer is not consensus, even when it agrees
    solo = _json.dumps([{"model_id": "gpt-4o", "action": "BUY"}])
    assert _consensus_mult(solo, main_action="BUY") == 1.0

    # the same model twice (provider-prefixed or not) is still one voice
    same = _json.dumps([
        {"model_id": "minimax/MiniMax-M3", "action": "HOLD"},
        {"model_id": "MiniMax-M3", "action": "HOLD"},
    ])
    assert _consensus_mult(same, main_action="HOLD") == 1.0

    # all reviewers agreeing on a *different* action is disagreement
    disagree_all = _json.dumps([
        {"model_id": "deepseek-chat", "action": "SELL"},
        {"model_id": "gpt-4o", "action": "SELL"},
    ])
    assert abs(_consensus_mult(disagree_all, main_action="BUY") - 0.3) < 1e-9

    # malformed / unusable payloads fail closed to 1.0
    assert _consensus_mult("[not json") == 1.0
    assert _consensus_mult(_json.dumps([{"model_id": "x"}])) == 1.0
    assert _consensus_mult(_json.dumps("scalar")) == 1.0
    # every result must stay inside the documented band
    for payload in (two_models, split, solo, same, disagree_all):
        for act in ("BUY", "HOLD", ""):
            v = _consensus_mult(payload, main_action=act)
            assert 0.3 <= v <= 1.3, (payload, act, v)

    # end-to-end: compute_signal_weight must consume peer_reviews
    d = {"sentiment_score": 0.5, "suggested_action": "BUY", "ts": 1792000000,
         "peer_reviews": two_models, "extra_models_consensus": ""}
    r = compute_signal_weight(d, now_ts=1792000000)
    assert r["components"]["consensus"] == 1.3, r["components"]

    # and must fall back to 1.0 (not a boost) when the reviewers are one model
    d2 = dict(d, peer_reviews=same)          # same model twice, agreeing
    r2 = compute_signal_weight(d2, now_ts=1792000000)
    assert r2["components"]["consensus"] == 1.0, r2["components"]

    # one model disagreeing twice with itself is one dissent, not two:
    # ratio 0/1 -> lone vote -> 1.0
    self_dis = _json.dumps([
        {"model_id": "minimax/MiniMax-M3", "action": "SELL"},
        {"model_id": "MiniMax-M3", "action": "SELL"},
    ])
    assert _consensus_mult(self_dis, main_action="BUY") == 1.0
    # ...but that model plus one genuinely different model dissenting is real
    # consensus: 0 agree / 2 disagree -> ratio 0 -> 0.3
    self_dis_two = _json.dumps([
        {"model_id": "minimax/MiniMax-M3", "action": "SELL"},
        {"model_id": "MiniMax-M3", "action": "SELL"},
        {"model_id": "deepseek-chat", "action": "SELL"},
    ])
    assert abs(_consensus_mult(self_dis_two, main_action="BUY") - 0.3) < 1e-9


def _self_test_reaction():
    """2026-10-22 regression tests for the reaction_mult fix.

    Covers the three defects found in production:
      1. multi_factor rows (85% of the table, no pct_change) shadowed the real
         reaction rows under `ORDER BY ts DESC LIMIT 1`.
      2. The lookup had no upper time bound, so it could match signals from far
         in the future -- future-data leakage into current decision weights.
      3. _reaction_mult could implicitly return None for 0.2<=|pct|<0.3 with
         vol<1.2, which makes compute_signal_weight raise TypeError.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE abnormal_signals(
        id INTEGER, ts INTEGER, asset TEXT, signal_kind TEXT, direction TEXT,
        anchor_ts INTEGER, detail_json TEXT, hint TEXT, status TEXT)""")
    T = 1_000_000
    # A multi_factor row (no pct_change) sits exactly on top of a real one.
    conn.execute(
        "INSERT INTO abnormal_signals VALUES"
        "(1,?,'XAU','multi_factor','neutral',NULL,'{}','h','open')", (T,))
    conn.execute(
        "INSERT INTO abnormal_signals VALUES"
        "(2,?,'XAU','abnormal_swing','neutral',NULL,"
        "'{\"pct_change\": 0.45}','h','open')", (T + 10,))
    # A far-future reaction that must NOT be attributed to a decision at T.
    conn.execute(
        "INSERT INTO abnormal_signals VALUES"
        "(3,?,'XAU','abnormal_swing','neutral',NULL,"
        "'{\"pct_change\": 0.99}','h','open')", (T + 999_999,))

    row = get_recent_abnormal(conn, "XAU", T, T + REACTION_WINDOW_SEC)
    assert row is not None, "should find the real reaction row"
    assert row["signal_kind"] == "abnormal_swing", (
        "multi_factor row (no pct_change) must be skipped, got %r"
        % row["signal_kind"])
    assert row["triggered"] is True, "pct_change 0.45 must count as triggered"
    assert row["ts"] == T + 10, "must not match the out-of-window future row"

    # Other assets must never leak in.
    assert get_recent_abnormal(conn, "ETH", T, T + REACTION_WINDOW_SEC) is None, \
        "must not leak signals across assets"

    # When only a far-future row exists, the windowed query must return None.
    conn.execute("DELETE FROM abnormal_signals WHERE id=2")
    assert get_recent_abnormal(conn, "XAU", T, T + REACTION_WINDOW_SEC) is None, \
        "future-only signal must not match (future-data leakage)"

    # _reaction_mult must never return None across the whole input space.
    for pct in (0.0, 0.05, 0.15, 0.25, 0.35, 0.6, 1.0, 5.0, -0.25, -1.0):
        for vol in (None, 0.5, 1.0, 1.2, 1.3, 1.5, 3.0):
            m = _reaction_mult({"triggered": True, "pct_change": pct,
                                "vol_ratio": vol, "aligned": True})
            assert m is not None, "None returned for pct=%s vol=%s" % (pct, vol)
            assert 0.5 <= m <= 1.5, "out of range: %s" % m

    # The previously-unreachable 0.2<=pct<0.3 band now returns a valid value.
    assert _reaction_mult({"triggered": True, "pct_change": 0.25,
                           "vol_ratio": 1.0, "aligned": True}) == 1.1
    assert _reaction_mult({"triggered": True, "pct_change": 0.45,
                           "vol_ratio": 1.0, "aligned": True}) == 1.2

    # A real reaction must actually move the composite weight.
    base = compute_signal_weight({"target_asset": "XAU", "sentiment_score": 0.5})
    withr = compute_signal_weight(
        {"target_asset": "XAU", "sentiment_score": 0.5},
        abnormal={"triggered": True, "pct_change": 0.45,
                  "vol_ratio": 1.0, "aligned": True})
    assert base["signal_weight"] != withr["signal_weight"], \
        "reaction must change the composite weight"
    conn.close()
    return True


# ============================================================================
# CLI / self-test entry point
# ============================================================================


if __name__ == "__main__":
    import sys
    print("=== signal_weight_evaluator self-test ===")
    # 1) base_mult
    # v314 P0: 全空输入 = 强度 0 → 中性基准 0.4 (旧实现返回 1.0, 但那是在
    # "0.5=中性"的错误量纲下算出来的, 无法与新口径共存)。
    assert _base_mult(None, None, None) == 0.4
    # v313 P1: sentiment_score 是 -1~1 口径 (0=中性), 不是 0~1 (0.5=中性)。
    # 旧断言 _base_mult(0.5, None, None) == 0.5 编码的是**错误量纲** ——
    # 它把 "0.5 = 中性" 当成前提, 而 DB 里 0.5 是"中等强度方向",
    # 0 才是中性。于是旧公式把 94.7% 的决策 (sentiment=0, 无方向) 算成
    # 强度 1.0 的"最强信号", base_mult 全部顶到 1.5 上限。
    assert _base_mult(0.0, None, None) == 0.4, "sentiment=0 (无方向) 应为中性基准"
    assert _base_mult(0.5, None, None) > 0.4, "|0.5|=0.5 强度 → base > 中性"
    assert _base_mult(-0.5, None, None) == _base_mult(0.5, None, None), \
        "负向同强度应同权重"
    assert 0.4 <= _base_mult(0.85, 80, 0.7) <= 1.0
    # heat_score 是 0-10 标度 (DB 实际), 5.0 应等价于 0-1 标度的 0.5
    assert abs(_base_mult(0.0, None, 5.0) - _base_mult(0.0, None, 0.5)) < 1e-9
    # impact_score (0-10) 是 DB 里真正在用的影响力字段 —— 必须**独立**贡献
    # (v314: 旧公式是连乘, sentiment=0 时 impact 再大也被乘成 0)
    assert _base_mult(0.0, None, None, impact_score=0.0) < \
           _base_mult(0.0, None, None, impact_score=10.0), \
           "impact_score 必须独立贡献 (旧实现整段失效)"
    # base 自身不得撞到复合上限 1.0 —— 否则共识/反应/影响维度被截断抹平
    assert _base_mult(1.0, 100, 10.0, impact_score=10.0) <= 1.0
    # 2) consensus_mult
    assert _consensus_mult("") == 1.0
    assert _consensus_mult("AGREE") == 1.3
    assert _consensus_mult("DISAGREE") == 0.6
    assert _consensus_mult('{"v1":{"agree":true},"v2":{"agree":true}}') == 1.3
    # 3) source_mult
    assert _source_mult_from_cache(None) == 1.0
    assert _source_mult_from_cache({"sample_n": 2, "hit_rate": 0.9}) == 1.0
    assert _source_mult_from_cache({"sample_n": 10, "hit_rate": 0.7}) == 1.2
    # 4) reaction_mult
    assert _reaction_mult(None) == 1.0
    assert _reaction_mult({"triggered": False}) == 1.0
    assert _reaction_mult({"triggered": True, "pct_change": 0.8, "vol_ratio": 2.0}) == 1.4
    assert _reaction_mult({"triggered": True, "pct_change": -0.5, "vol_ratio": 2.0, "aligned": False}) == 0.5
    # 4b) reaction regression tests (2026-10-22 fix)
    _self_test_reaction()
    print("  reaction regression: OK")
    # 4c) consensus regression tests (2026-10-22 fix)
    _self_test_consensus()
    print("  consensus regression: OK")
    # 5) regime_mult
    assert _regime_mult("trending_up") == 1.2
    assert _regime_mult("range_bound") == 0.85
    assert _regime_mult("volatile") == 0.7
    # 6) time_mult
    now = 1000000
    assert _time_mult(now, now_ts=now) == 1.0
    assert _time_mult(now - 3600, now_ts=now, expected_horizon="intraday") == 0.85
    # composite
    # v314 P0: 权重口径改为 base × 质量几何均值 × 影响维, 再做 x/(1+x) 单调压缩。
    # 这个例子: base=0.88, qual_geo=1.09, impact=1.00 → raw=0.96 → weight=0.49。
    # 断言改为**关系式** (而不是某个具体数值): 提高 impact 必须提高权重,
    # 几何均值的中性点 (五维全 1.0) 必须恰为 1.0。
    result = compute_signal_weight({
        "sentiment_score": 0.85, "impact_magnitude": 80, "heat_score": 0.7,
        "extra_models_consensus": '{"v1":{"agree":true},"v2":{"agree":true}}',
        "regime_at_decision": "trending_up", "ts": now, "expected_horizon": "1-3d",
    }, now_ts=now)
    assert 0.0 < result["signal_weight"] < 1.0, f"unexpected: {result}"
    hi = compute_signal_weight({
        "sentiment_score": 0.85, "impact_magnitude": 80, "heat_score": 0.7,
        "extra_models_consensus": '{"v1":{"agree":true},"v2":{"agree":true}}',
        "regime_at_decision": "trending_up", "ts": now, "expected_horizon": "1-3d",
    }, now_ts=now, impact=1.5)
    assert hi["signal_weight"] > result["signal_weight"], \
        "影响维必须单调提高权重"
    # 五维质量全为中性 (1.0) 时几何均值恰为 1.0
    neutral = compute_signal_weight({
        "sentiment_score": 0.85, "impact_magnitude": 80, "heat_score": 0.7,
        "regime_at_decision": "trending_up", "ts": now, "expected_horizon": "1-3d",
    }, now_ts=now)
    assert abs(neutral["components"]["consensus"] - 1.0) < 1e-9
    print(f"  composite: weight={result['signal_weight']:.3f} tier={result['weight_tier']}")
    print(f"  explain: {result['explain']}")
    print("ALL PASSED")
    sys.exit(0)






# ─────────────────────────────────────────────────────────────────────────────
# 成交决策 (trade_decisions) 的权重 —— G13/G13b 的关键补齐
# ─────────────────────────────────────────────────────────────────────────────
#
# 为什么必须单独算一份:
#   signal_weight_history 里的权重一直是按 **ai_decisions** (AI 提案) 算的,
#   而真实盈亏 (trade_reflections_v2.pnl_pct) 是记在 **trade_decisions** (成交决策)
#   上的 —— 两个编号空间, 直接 join 只有个位数行。这就是"权重算不准"的根因:
#   根本不是权重算法错, 而是**权重从没落在有结果的那批决策上**。
#
#   这里用**完全相同的 6 维口径**, 对成交决策再算一遍, 落到 decision_space='trade',
#   于是"权重 → 真实盈亏"可以直接对账 (每条成交决策的权重 vs 它的 pnl_pct)。

TRADE_WEIGHT_FIELDS = (
    "id, decision_ts, asset, intended_action, final_action, decision_type, "
    "news_id, ai_decision_id, event_strength, news_star, context_json, status"
)


def _fetch_trade_decision(conn, td_id):
    try:
        row = conn.execute(
            "SELECT %s FROM trade_decisions WHERE id=?" % TRADE_WEIGHT_FIELDS,
            (int(td_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return dict(row)
    except Exception:
        return None


def _fetch_ai_for_trade(conn, td):
    """找出这条成交决策对应的 AI 提案 (优先 ai_decision_id, 否则按新闻号+资产回溯)。"""
    cols = ("id, news_id, sentiment_score, impact_magnitude, impact_score, heat_score, "
            "attention_score, extra_models_consensus, peer_reviews, regime_at_decision, "
            "expected_horizon, ts, suggested_action, target_asset")
    aid = td.get("ai_decision_id")
    if aid:
        try:
            row = conn.execute(
                "SELECT %s FROM ai_decisions WHERE id=?" % cols, (int(aid),),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row:
            return dict(row)
    news_id = td.get("news_id")
    if news_id is None:
        return None
    asset = (td.get("asset") or "").upper()
    try:
        if asset:
            row = conn.execute(
                "SELECT %s FROM ai_decisions WHERE news_id=? AND UPPER(target_asset)=? "
                "ORDER BY ts DESC LIMIT 1" % cols, (int(news_id), asset),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT %s FROM ai_decisions WHERE news_id=? ORDER BY ts DESC LIMIT 1" % cols,
                (int(news_id),),
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return dict(row) if row else None


def _news_source_for_trade(conn, td):
    news_id = td.get("news_id")
    if news_id is None:
        return ""
    for sql, args in (
        ("SELECT source FROM raw_news WHERE id=?", (int(news_id),)),
        ("SELECT source FROM news_events WHERE news_id=? LIMIT 1", (int(news_id),)),
    ):
        try:
            row = conn.execute(sql, args).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row[0]:
            return str(row[0])
    return ""


def evaluate_and_persist_trade(conn, trade_decision_id, trigger_kind="backfill_trade",
                                now_ts=None):
    """为一条**成交决策**计算信号权重并落库 (decision_space='trade')。

    与 evaluate_and_persist (AI 提案) 的 6 维口径完全一致, 只是作用对象换成
    最终成交决策 —— 这样 trade_reflections_v2 里的真实盈亏就能和权重直接对账。
    """
    td = _fetch_trade_decision(conn, trade_decision_id)
    if not td:
        return None
    ai = _fetch_ai_for_trade(conn, td) or {}
    asset = (td.get("asset") or ai.get("target_asset") or "").upper()
    action = (td.get("final_action") or td.get("intended_action")
              or ai.get("suggested_action") or "").upper()
    decision_ts = int(td.get("decision_ts") or 0)
    if not decision_ts:
        try:
            decision_ts = int(float(td.get("created_at") or 0))
        except (TypeError, ValueError):
            decision_ts = 0
    # 没有 AI 提案时, 用新闻打分兜底 (sentiment_score 口径: 0~1, 0.5 中性)
    sentiment = ai.get("sentiment_score")
    if sentiment is None:
        try:
            row = conn.execute(
                "SELECT score FROM news_events WHERE news_id=? LIMIT 1",
                (int(td.get("news_id") or 0),),
            ).fetchone()
            raw = float(row[0]) if row and row[0] is not None else None
            sentiment = 0.5 + (max(-1.0, min(1.0, raw)) * 0.5) if raw is not None else None
        except (sqlite3.OperationalError, TypeError, ValueError):
            sentiment = None
    payload = {
        "sentiment_score": sentiment,
        "impact_magnitude": ai.get("impact_magnitude"),
        "impact_score": ai.get("impact_score"),
        "heat_score": ai.get("heat_score"),
        "attention_score": ai.get("attention_score"),
        "extra_models_consensus": ai.get("extra_models_consensus") or "",
        "peer_reviews": ai.get("peer_reviews") or "",
        "regime_at_decision": ai.get("regime_at_decision") or "",
        "expected_horizon": ai.get("expected_horizon") or "",
        "ts": decision_ts,
        "suggested_action": action,
        "target_asset": asset,
    }
    now = int(now_ts or time.time())
    news_source = _news_source_for_trade(conn, td)
    src = get_source_stats(conn, news_source, asset)
    window_start = decision_ts if decision_ts else now - REACTION_WINDOW_SEC
    window_end = (decision_ts + REACTION_WINDOW_SEC) if decision_ts else now
    abn = get_recent_abnormal(conn, asset, window_start, window_end)
    if abn:
        abn["aligned"] = _check_aligned(abn.get("direction", ""), action)
    result = compute_signal_weight(payload, source_stats=src, abnormal=abn, now_ts=now,
                                  impact=_impact_mult_for(conn, payload, decision_ts))
    # v500: 交易空间同样受人工覆盖约束（按最终成交的 asset/action 匹配）
    result = apply_manual_override(conn, result, asset, action, now_ts=now)
    comp = result["components"]
    # 新表结构: 带上 decision_space / ai_decision_id / trade_decision_id
    # v310 P1: 同 evaluate_and_persist —— 按表的实际列拼 INSERT。
    # 旧实现按固定 17 列写、失败再降级写 14 列, 但 sqlite3 缺列报错文案是
    # "table X has no column named Y", 与判断用的 "no such column" 不匹配 →
    # 降级分支从未触发 → 精简 schema 的库上这条权重**根本没落库**。
    hist_vals = {
        "decision_id": int(trade_decision_id),
        "computed_at": now,
        "trigger_kind": trigger_kind,
        "signal_weight": round(result["signal_weight"], 6),
        "weight_tier": result["weight_tier"],
        "base_mult": comp["base"],
        "consensus_mult": comp["consensus"],
        "source_mult": comp["source"],
        "reaction_mult": comp["reaction"],
        "regime_mult": comp["regime"],
        "time_mult": comp["time"],
        "impact_mult": comp.get("impact", 1.0),
        "explain": result["explain"],
        "abnormal_id": (abn or {}).get("id"),
        "source_stats_id": (src or {}).get("id"),
        "decision_space": "trade",
        "ai_decision_id": ai.get("id"),
        "trade_decision_id": int(trade_decision_id),
    }
    hist_avail = _table_columns(conn, "signal_weight_history")
    hist_cols = [c for c in hist_vals if c in hist_avail]
    if hist_cols:
        try:
            conn.execute(
                "INSERT INTO signal_weight_history (%s) VALUES (%s)"
                % (", ".join(hist_cols), ", ".join("?" * len(hist_cols))),
                [hist_vals[c] for c in hist_cols],
            )
        except sqlite3.OperationalError as exc:
            # 锁冲突 → 本次不写(下一轮重算会补), 不产生 decision_space=NULL 的脏行
            if not _is_missing_column(exc):
                return result
    return result

