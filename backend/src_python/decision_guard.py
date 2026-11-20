"""共享的多证据交易闸门，不依赖 FastAPI。

v318 重构 —— 修复「长期无可执行信号」（线上 trident_event_bus.db 实测）
────────────────────────────────────────────────────────────────────────
1. final_score 的 p10~p90 全部等于 **+0.100**，80% 的行一模一样。
   根因：volatility 因子写成 clamp(1 - atr_pct/10)，而快照缺 ATR 时
   atr_pct=0 → 得分 **+1.0**（最大看多），又带 0.10 权重 → 每行白送
   +0.10 看多分。于是 final_score 测的是「ATR 取到没有」，不是「这条
   新闻有没有方向」。
2. 权重和 = 1.15（不是 1.0），叠加 impact_boost 后 final_score 可达 ±1.45，
   但下游 cross_asset_context 明确假设它在 [-1,1]。
3. 缺失因子按 0 计入分母（稀释）：中位 final_score 仅 +0.0929，p99=0.2819
   < BUY 门槛 0.4 → 结构性地永远够不到方向单。
4. assets.get(asset) or assets.get("BTC")：XAU/WTI 缺数据时静默拿 BTC 顶替
   （实测 512/1995 行 asset_key_absent）。
5. market_confirmation 文本 98.6% 是 unknown，回退成 change_24h_pct/5；
   而「取不到行情」时 change_24h_pct 恒为 0.0 → 「没有行情」被编码成
   「价格持平 = 中性确认」，与真实读数无法区分。

本次改造：
  ① 因子分两层 —— **方向因子**（有符号，定方向）与**质量因子**（无符号，
     只调 confidence）。volatility / cluster_heat / historical_confidence
     不再污染方向。
  ② **只在真实可得的因子上求方向**，并按可得权重归一化；coverage 单独
     记录并用于下调置信度 —— 方向不被缺失数据稀释，置信度反映证据厚度。
  ③ 缺失 ≠ 中性：取不到的因子一律 unavailable（不加分、不占权重）。
  ④ 不再用 BTC 快照顶替其它品种。
  ⑤ 影响评估（impact/heat/asset_impact）改成**幅度放大系数**，不再加法推
     方向 —— 影响度是无方向的量度，加法等于给高影响新闻白送看多分。
  ⑥ final_score 恒在 [-1,1]。
  ⑦ 锚点规则：AI 没有方向观点（HOLD/分析失败）时，市场上下文不得单方面
     造出方向；市场的独立读数另存 market_bias 供研究端使用。
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import evidence
from market import market_regime

# 买卖方向分数门槛（后台可调，5s TTL）：做多胜率偏低 → BUY 门槛默认高一档
_score_cache: Dict[str, Any] = {"ts": 0.0, "value": {"buy_min": 0.4, "sell_min": 0.3}}


def _direction_scores() -> Dict[str, float]:
    now = time.monotonic()
    if now - float(_score_cache.get("ts", 0.0)) < 5.0:
        return _score_cache["value"]
    value = {"buy_min": 0.4, "sell_min": 0.3}
    try:
        from paper_trading import get_settings as _get_pt_settings
        st = _get_pt_settings()
        value = {
            "buy_min": float(st.get("buy_min_score", 0.4)),
            "sell_min": float(st.get("sell_min_score", 0.3)),
        }
    except Exception:
        pass
    _score_cache.update({"ts": now, "value": value})
    return value


# ── v321.3 P1: 闸门分组校准 (需求 #1) ───────────────────────────────────────
# 闸门「整体拒对率 ≈ 基础概率」(81.0% vs 81.9%) 看起来毫无价值 —— 那是**整体**
# 数字把分组差异抹平了。实测按 (资产, 方向) 拆开 (321 条已结算决策):
#     BTC  BUY  -7.50pp (反向)   |  BTC  SELL +10.48pp (正)
#     ETH  SELL -5.91pp (反向)   |  WTI  BUY  +9.89pp (正)
#     XAU  BUY  +5.38pp (正)     |  XAU SELL  -4.48pp (反向)
# **同一个资产不同方向结论相反** —— 所以校准必须按 (资产, 方向)。
#
# 这里读 calibrate_gate.py 落库的 gate_calibration (applied=1 才生效):
#   · 正增量 (lift ≥ +1pp):  闸门**不该拦**这组信号 → 放宽门槛
#   · 负增量 (lift ≤ -3pp):  这组信号系统性反向 → 收紧门槛
#   · 无记录 / 样本不足:     维持默认门槛 (不猜)
# 门槛调整幅度**有界** (最多 ±50%), 且原始门槛始终保留在输出里可追溯。
_GATE_CALIB_CACHE: Dict[str, Any] = {"ts": 0.0, "value": {}}
GATE_CALIB_TTL_S = 60.0
GATE_CALIB_MAX_ADJUST = 0.5   # 门槛最多调整 ±50%


def _gate_calibration() -> Dict[tuple, dict]:
    """读闸门分组校准 (60s TTL 缓存)。读不到就返回空表 (不猜)。"""
    now = time.monotonic()
    if now - float(_GATE_CALIB_CACHE.get("ts", 0.0)) < GATE_CALIB_TTL_S:
        return _GATE_CALIB_CACHE["value"]
    out: Dict[tuple, dict] = {}
    try:
        import db as _db
        conn = _db.get_connection()
        conn.row_factory = __import__("sqlite3").Row
        try:
            rows = conn.execute(
                "SELECT asset, action, n_samples, hit_rate, base_rate, lift_pp, note "
                "FROM gate_calibration WHERE applied=1"
            ).fetchall()
            for r in rows:
                out[(str(r["asset"] or "").upper(), str(r["action"] or "").upper())] = {
                    "n_samples": _as_int(r["n_samples"], 0),
                    "hit_rate": float(r["hit_rate"] or 0.0),
                    "base_rate": float(r["base_rate"] or 0.0),
                    "lift_pp": float(r["lift_pp"] or 0.0),
                    "note": str(r["note"] or ""),
                }
        finally:
            conn.close()
    except Exception:
        out = {}
    _GATE_CALIB_CACHE.update({"ts": now, "value": out})
    return out


_CONF_GATE_CAP = 0.10      # 可信度 0 时最多把门槛抬高 0.10，不让一个 0.08 权重的因子翻盘


def _confidence_penalty(confidence) -> float:
    """v475：基本面可信度 → 开仓门槛的收紧量。

    为什么不是"直接乘分数"（v472 已经按权重折过一遍了）：
    权重打折回答"这个分数算多少"，门槛收紧回答"多确定才准开仓"，是两件事。

    只收紧不放宽：可信度 1.0 → 0；可信度 0.44 → +0.056；封顶 0.10。
    不可得（confidence=None）→ 0，保持 v321 的纯校准行为逐位不变。
    """
    if not isinstance(confidence, (int, float)):
        return 0.0
    conf = max(0.0, min(1.0, float(confidence)))
    return round(min(_CONF_GATE_CAP, _CONF_GATE_CAP * (1.0 - conf)), 4)


def _calibrated_thresholds(asset: str, action: str) -> tuple:
    """按分组校准调整门槛。返回 (buy_min, sell_min, calib_info)。

    calib_info 为 None 表示**没有**应用到校准 (默认门槛)。
    """
    scores = _direction_scores()
    buy_min = float(scores.get("buy_min", 0.4))
    sell_min = float(scores.get("sell_min", 0.3))
    cal = _gate_calibration()
    if not cal or not action:
        return buy_min, sell_min, None
    info = cal.get((str(asset or "").upper(), str(action).upper()))
    if not info:
        return buy_min, sell_min, None
    lift = float(info.get("lift_pp") or 0.0)
    if lift >= 1.0:
        # 正增量: 该方向的信号历史上比随机准 → 闸门放宽 (让它更容易成单)
        adj = -min(GATE_CALIB_MAX_ADJUST, lift / 100.0)
    elif lift <= -3.0:
        # 负增量: 系统性反向 → 闸门收紧 (让它更难成单)
        adj = min(GATE_CALIB_MAX_ADJUST, -lift / 100.0)
    else:
        adj = 0.0
    if action == "BUY":
        buy_min = max(0.05, min(0.95, buy_min * (1.0 + adj)))
    else:
        sell_min = max(0.05, min(0.95, sell_min * (1.0 + adj)))
    return buy_min, sell_min, dict(info, adjusted=adj)


def _clamp(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _clamp10(value: Any, default: float = 5.0) -> float:
    """影响评估分数 (0-10)：缺失/非法 → default（中性 5 = 不加不减）。"""
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return default


# ── 因子分层 ────────────────────────────────────────────────────────────────
# 方向因子：有符号，决定 final_score 的方向与强度。权重和 = 1.0。
DIRECTIONAL_WEIGHTS: Dict[str, float] = {
    # v405: news_sentiment 0.40 → 0.32，让出的 0.08 给了 fundamental。
    # 原因：此前六项全是价格/情绪/资金派生，公司级基本面权重为 0 ——
    # 「基本面」在本系统里只是 prompt 背景板，从不参与方向判定。
    # 0.08 是刻意的小权重：季报是慢变量，作用是把「估值极端 + 盈利修正」
    # 变成一条同向/反向的弱信号，而不是让 PE 分位压过当日新闻。
    "news_sentiment": 0.32,         # AI 自己的方向判断（锚点）
    # v342: 0.25 → 0.10。market_confirmation 是 LLM 三值枚举，实测 98.6% 是 unknown，
    # 实际长期失效；把 0.15 权重让给可计算的 structure 因子（见 structure_factor）。
    "market_confirmation": 0.10,    # 盘面是否确认这条新闻（LLM 枚举，多缺失）
    "structure": 0.15,              # v342: 盘面结构评分（价位/波动/周期共振，本地可算）
    "trend": 0.15,                  # 7 日结构
    "funding": 0.10,                # 资金费率（多头拥挤度）
    "price_fund_divergence": 0.10,  # 价格-资金背离
    "fundamental": 0.08,            # v405: 估值历史分位 + 盈利修正方向（见 fundamental_factor）
}
# 质量因子：无符号，只调 confidence，不参与方向。
QUALITY_FACTORS = ("volatility", "cluster_heat", "historical_confidence")
# market_confirmation 退回 24h 动量代理时权重减半：它和 trend 都是价格派生，
# 满权重等于把同一段行情算两遍。
# v342: 半权重跟随主权重走（mc 0.25→0.10 时 0.12→0.05），否则可得权重和会 >1，
# coverage 出现 1.02 这种「证据超过 100%」的读数。
_CONFIRMATION_PROXY_WEIGHT = 0.05


def _text(value: Any) -> str:
    """None → 空串；其余转小写去空白（行情/模型文本统一走这里）。"""
    if value is None:
        return ""
    return str(value).strip().lower()


def _has_field(payload: Dict[str, Any], key: str) -> bool:
    """行情字段是否真的取到了：'N/A' / None / 空串 / '-' 都算没取到。

    行情快照在 status=unavailable 时会填 0.0 / 'N/A' 占位；不做这个判断
    就会把「没有数据」当成「真实的 0」。
    """
    if not isinstance(payload, dict) or key not in payload:
        return False
    raw = payload.get(key)
    if raw is None:
        return False
    text = str(raw).strip()
    if not text:
        return False
    return text.upper() not in ("N/A", "NA", "NONE", "NULL", "-")


def _as_int(value, default: int = 0) -> int:
    """把数值字段读成 int，脏值一律回默认。

    事故（v323）：ai_decisions 的 cluster_size / child_count 列曾被写成
    'OIL|WTI|BUY' 这类聚合键（INSERT 参数错位）。下游 `int()` 抛 ValueError，
    整个闸门评估失败 → 那条分析直接作废（日志 50 次 GUARD-PRE failed）。
    单个脏字段不能炸掉一条分析，这里统一兜底；真实数字照常读。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            # 兼容 '12'、'12.0' 这类字符串
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _expected_move_pct(row: Dict[str, Any]) -> Optional[float]:
    """v342: 从 AI 的 impact_curve 取「与 expected_horizon 匹配」的预期波动（%）。

    取不到返回 None —— 不编造。映射：intraday→2h（退化 30m）/ 1-3d→1d / 1w+→1d。
    """
    curve = row.get("impact_curve")
    if isinstance(curve, str):
        try:
            curve = json.loads(curve)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(curve, dict):
        return None
    horizon = str(row.get("expected_horizon") or "").strip().lower()
    keys = ("2h", "30m") if horizon == "intraday" else ("1d", "2h")
    for k in keys:
        try:
            val = float(curve.get(k))
        except (TypeError, ValueError):
            continue
        return val
    return None


def _hold_reason(
    *,
    final_score: float,
    raw_action: str,
    action: str,
    scores: Dict[str, float],
    factors: Dict[str, Any],
    weights: Dict[str, float],
    avail: Dict[str, bool],
    coverage: float,
    validation: Dict[str, Any],
    risk_plan: Dict[str, Any],
    expected_move_pct: Optional[float],
    tech_info: Dict[str, Any],
) -> Dict[str, Any]:
    """v412：HOLD 到底为什么不参与——把"没下单"写成可复核的理由。

    为什么需要：复盘时最常见的空白不是"亏了为什么亏"，而是"那天明明看到了，
    为什么没做"。只输出 action=HOLD 的闸门等于把这个问题永久悬空。

    这里只**归纳已经算出来的东西**，不引入新判断：
      1. 分数离门槛还差多少（按最接近的那个方向算）；
      2. 证据层是否否决（raw_action 有方向但被 gate 打回）；
      3. 覆盖度是否不够（缺因子导致不敢重仓本身就是理由）；
      4. 技术状态是否给出了反方证据（只折减，不反向）；
      5. 赔率是否不合算（预期空间 < 5 根内常见逆风）——这是 v411/v412 才有的判据；
      6. 已有矛盾清单（来自 evidence）。
    """
    if action != "HOLD":
        return {"applicable": False}

    reasons: List[Dict[str, str]] = []
    buy_min = float(scores.get("buy_min") or 0.0)
    sell_min = float(scores.get("sell_min") or 0.0)

    # 1) 距门槛的缺口：只看更近的那个方向，避免"两个方向都差很远"这种废话
    need_buy = buy_min - final_score
    need_sell = final_score + sell_min
    if final_score >= 0:
        gap, target = need_buy, f"BUY 门槛 {buy_min:.2f}"
        nearest = "BUY"
    else:
        gap, target = need_sell, f"SELL 门槛 -{sell_min:.2f}"
        nearest = "SELL"
    if gap > 0:
        reasons.append({
            "code": "score_below_threshold",
            "detail": f"方向分 {final_score:+.2f} 距 {target} 还差 {gap:.2f}（更近的是 {nearest}）",
        })

    # 2) 证据层否决：有方向、但没过 gate
    if raw_action in ("BUY", "SELL"):
        verdict = str(validation.get("verdict") or "")
        reasons.append({
            "code": "evidence_gate",
            "detail": f"方向已达到 {raw_action} 门槛，但证据层判为「{verdict or '未放行'}」",
        })

    # 3) 覆盖度不足：缺的因子本身就是不敢做多的理由（不记 0 分）
    missing = [k for k, ok in avail.items() if not ok]
    if coverage < 0.5 or missing:
        reasons.append({
            "code": "low_coverage",
            "detail": f"可得因子覆盖 {coverage:.0%}"
                      + (f"，缺 {'/'.join(missing)}（缺失不等于中性）" if missing else ""),
        })

    # 4) 技术状态给出反方证据
    if tech_info.get("available") and tech_info.get("conflict"):
        reasons.append({
            "code": "tech_state_conflict",
            "detail": (f"技术状态读数为反向证据（偏置 {tech_info.get('bias')}，"
                       f"减分 ×{float(tech_info.get('damp') or 1.0):.2f}）：{tech_info.get('readout') or ''}"),
        })

    # 5) 赔率不合算：预期空间打不过正常逆风 —— 这条是"不做"最硬的理由
    if risk_plan.get("available"):
        adverse = risk_plan.get("expected_adverse_pct")
        level = risk_plan.get("level")
        if expected_move_pct is None:
            if level == "高":
                reasons.append({
                    "code": "odds_unknown",
                    "detail": (f"风险档位=高（{risk_plan.get('note')}），但这条新闻没有预期空间读数，"
                               "无法判断赔率 → 不猜"),
                })
        elif adverse is not None:
            space = abs(float(expected_move_pct))
            # v418：门槛从「≥ 逆风」升级为「≥ 逆风 ÷ 实测赔率」。
            # 1h 实测赔率 0.70 <1 → 光打平逆风还不够，要 1.43 倍才够本；
            # 1d 实测赔率 1.32 → 门槛比逆风松。4h 没有可靠赔率，退回保守线。
            need = risk_plan.get("require_space_pct")
            need = float(need) if need is not None else float(adverse)
            if space <= need:
                odds_txt = risk_plan.get("odds_typical")
                reasons.append({
                    "code": "odds_unfavorable",
                    "detail": (f"赔率不合算：预期空间 {space:.2f}% ≤ 该周期要求的最小空间 {need:.2f}%"
                               f"（5 根内常见逆风 {adverse}%"
                               + (f"，实测赔率 {odds_txt}" if odds_txt is not None else "，赔率未测")
                               + f"；档位 {level}）→ 赚的钱不够挨的波动"),
                })
            elif level == "高":
                reasons.append({
                    "code": "risk_high",
                    "detail": (f"风险档位=高（{risk_plan.get('note')}），"
                               f"即使要做也只能按 {risk_plan.get('size_factor')}× 仓位、"
                               f"止损 {risk_plan.get('stop_atr_multiple')} 倍 ATR"),
                })

    # 6) 矛盾清单（evidence 已经算过，这里只是搬到显眼处）
    for item in (validation.get("contradictions") or [])[:3]:
        if isinstance(item, dict):
            reasons.append({
                "code": f"contradiction:{item.get('type') or 'unknown'}",
                "detail": str(item.get("detail") or "")[:200],
            })

    # 头部一句话：先给最硬的理由（赔率 > 门槛 > 覆盖度）
    order = ("odds_unfavorable", "odds_unknown", "evidence_gate",
             "score_below_threshold", "low_coverage", "tech_state_conflict")
    first = next((r["detail"] for code in order for r in reasons if r["code"] == code), None)
    headline = "HOLD：" + (first if first else "没有单项硬否决，是多项弱证据叠加后仍不够门槛")

    # 什么条件下会变成可做（给复盘与监控用，不是预测）
    check_next: List[str] = []
    if gap > 0:
        check_next.append(f"{nearest} 方向分再走 {gap:.2f}（总分到 "
                          f"{buy_min if nearest == 'BUY' else -sell_min:.2f}）")
    if risk_plan.get("available") and risk_plan.get("level") == "高":
        check_next.append("风险档位从「高」降到「中」以下（波动回落 / ADX 收敛 / 价格回到价值区）")
    if tech_info.get("available") and tech_info.get("conflict"):
        check_next.append("技术状态不再给出反向证据")
    if missing:
        check_next.append(f"补齐 {'/'.join(missing)} 维度后再评估")

    return {
        "applicable": True,
        "headline": headline,
        "nearest_direction": nearest,
        "gap_to_threshold": round(gap, 4) if gap > 0 else 0.0,
        "reasons": reasons,
        "check_next": check_next,
    }


def evaluate_decision(row: Dict[str, Any],
                      asof_ts: float | None = None) -> Dict[str, Any]:
    try:
        context = json.loads(row.get("decision_context") or "{}")
    except (json.JSONDecodeError, TypeError):
        context = {}
    if not isinstance(context, dict):
        context = {}
    assets = context.get("assets") if isinstance(context.get("assets"), dict) else {}
    asset = str(row.get("target_asset") or "BTC").upper()
    # v318：不再用 BTC 快照顶替其它品种。行情缺失就当缺失 —— 拿 BTC 的涨跌
    # /资金费率去解释 XAU/WTI 的新闻，等于把别的品种的证据算成自己的。
    asset_context = assets.get(asset) if isinstance(assets.get(asset), dict) else {}
    if not isinstance(asset_context, dict):
        asset_context = {}
    stats = asset_context.get("stats_7d") if isinstance(asset_context.get("stats_7d"), dict) else {}
    # 行情快照是否真的取到了（status=unavailable 时所有数值字段都是 0 占位）。
    # v342-fix: partial（本地 ticks/ohlcv 兜底价）也算可用——否则本地有价格和日线，
    # confirmation 代理与 structure 仍被 market_ok=False 整体挡掉。
    market_ok = _text(asset_context.get("status")) in {"ok", "partial"}
    confirmation_text = _text(row.get("market_confirmation")) or "unknown"
    trend_text = _text(stats.get("trend"))
    cluster = max(1, _as_int(row.get("cluster_size"), 1))
    sample = row.get("history_sample") if isinstance(row.get("history_sample"), dict) else {}
    if sample:
        settled = _as_int(sample.get("total"), 0)
        wins = _as_int(sample.get("wins"), 0)
        scope = str(sample.get("scope") or "asset")
    else:
        settled = _as_int(row.get("history_total"), 0)
        wins = _as_int(row.get("history_wins"), 0)
        scope = str(row.get("history_scope") or "asset")
    scope_note = {
        "asset": "同品种",
        "lane": "同赛道自动补齐",
        "market": "全市场自动补齐",
    }.get(scope, "同品种")
    # ══ 方向因子：逐个判定「是否真的取到了」 ═════════════════════════════
    # 原则：**取不到 ≠ 中性**。缺失的因子既不加分也不占权重；
    #       真实读到 0（价格持平、资金费率 0）才是「中性」这一条有效证据。
    factors: Dict[str, Dict[str, Any]] = {}
    avail: Dict[str, bool] = {}
    eff_weight: Dict[str, float] = {}

    def _put(name: str, score: float, explanation: str, ok: bool, weight: float) -> None:
        factors[name] = {
            "score": round(float(score), 4),
            "explanation": explanation,
            "role": "directional",
            "available": bool(ok),
            "weight": round(float(weight), 4),
        }
        avail[name] = bool(ok)
        eff_weight[name] = float(weight) if ok else 0.0

    # 1) news_sentiment —— AI 的方向锚点。
    #    HOLD / 分析失败时 ai_worker 用 reconcile_score_action 把分数归零，
    #    0.0 的含义是「AI 没有方向观点」，不是「中性看多」。
    try:
        sentiment = _clamp(float(row.get("sentiment_score")))
    except (TypeError, ValueError):
        sentiment = 0.0
    has_anchor = abs(sentiment) > 1e-9
    # ── 方向锚点的符号对齐 ────────────────────────────────────────────────
    # 历史数据里存在「suggested_action=SELL 但 sentiment_score=+0.42」的错位
    # （LLM 主路径此前没有调用 reconcile_score_action）。闸门按分数符号算方向，
    # 错位会让 SELL 永远出不来、把 BUY 判成 SELL。这里以 action 为准对齐，
    # 并在 explanation 里标出来，不静默改数。
    action_hint = _text(row.get("suggested_action"))
    sign_fixed = False
    if action_hint in ("buy", "sell") and has_anchor:
        want_positive = action_hint == "buy"
        if (sentiment > 0) != want_positive:
            sentiment = _clamp(abs(sentiment) if want_positive else -abs(sentiment))
            sign_fixed = True
    if has_anchor:
        _sentiment_note = f"AI 新闻情绪分={sentiment:+.3f}"
        if sign_fixed:
            _sentiment_note += "（原分与 AI 方向符号相反，已按 suggested_action 对齐）"
    else:
        _sentiment_note = "AI 未给出方向观点（分数=0）→ 因子不可得"
    _put("news_sentiment", sentiment if has_anchor else 0.0,
         _sentiment_note, has_anchor, DIRECTIONAL_WEIGHTS["news_sentiment"])

    # 2) market_confirmation —— 盘面有没有确认这条新闻。
    #    文本 98.6% 是 unknown → 退回 24h 动量做弱代理；但代理必须建立在
    #    「真的取到行情」上，否则「没有行情」会被编码成「价格持平=中性确认」。
    if confirmation_text in {"confirmed", "strong", "yes", "positive"}:
        _put("market_confirmation", 1.0, f"盘面确认={confirmation_text}", True,
             DIRECTIONAL_WEIGHTS["market_confirmation"])
    elif confirmation_text in {"rejected", "opposite", "negative"}:
        _put("market_confirmation", -1.0, f"盘面反向={confirmation_text}", True,
             DIRECTIONAL_WEIGHTS["market_confirmation"])
    elif market_ok and _has_field(asset_context, "change_24h_str"):
        _put("market_confirmation", _clamp(asset_context.get("change_24h_pct", 0) / 5.0),
             f"无确认文本，退回 24h 动量代理（{asset_context.get('change_24h_str')}）",
             True, _CONFIRMATION_PROXY_WEIGHT)
    else:
        _put("market_confirmation", 0.0, "无确认文本且无可用行情 → 因子不可得", False,
             DIRECTIONAL_WEIGHTS["market_confirmation"])

    # 2b) structure —— v342: 把 prompt 里算过、但此前没落进闸门的「盘面结构」补上。
    #     structure_factor 用快照确定字段重算 [-2,+2] 的盘面偏向（价位位置 /
    #     ATR 分档 / 1d-4h-1h 周期共振），再按 AI 方向对齐成「结构是否支持这条新闻」。
    #     ⚠️ 只在结构真能算出来时参与；缺 price/区间/ATR 一律不可得，不用 0 冒充中性。
    _structure = {}
    try:
        import structure_factor as _sf
        _structure = _sf.compute(asset_context)
    except Exception as _e:  # 计算失败不得影响主判定
        _structure = {"available": False, "bias": 0.0, "detail": f"结构计算出错: {_e}"}
    _struct_ok = bool(_structure.get("available"))
    _struct_bias = _structure.get("bias") or 0.0
    if _struct_ok:
        # 盘面偏向 → 「对新闻方向的支持度」：同向=正，反向=负
        _anchor_dir = 0
        if has_anchor:
            _anchor_dir = 1 if sentiment > 0 else -1
        _struct_score = _clamp(_struct_bias / 2.0)
        if _anchor_dir:
            _struct_score = _clamp(_struct_score * _anchor_dir)
        _put("structure", _struct_score,
             f"结构评分 {_struct_bias:+.2f}/±2 → 支持度 {_struct_score:+.2f}（{_structure.get('detail', '')}）",
             True, DIRECTIONAL_WEIGHTS["structure"])
    else:
        _put("structure", 0.0,
             f"结构不可得：{_structure.get('detail', '缺快照字段')}",
             False, DIRECTIONAL_WEIGHTS["structure"])

    # 3) trend —— 7 日结构。只有真读到趋势标签才算数。
    if "strong bull" in trend_text:
        trend, trend_note = 1.0, "7日强多头结构"
    elif "bull" in trend_text:
        trend, trend_note = 0.6, "7日多头结构"
    elif "strong bear" in trend_text:
        trend, trend_note = -1.0, "7日强空头结构"
    elif "bear" in trend_text:
        trend, trend_note = -0.6, "7日空头结构"
    else:
        trend, trend_note = 0.0, "无有效趋势标签"
    _put("trend", trend, f"快照趋势={stats.get('trend', 'Unknown')}（{trend_note}）",
         abs(trend) > 0, DIRECTIONAL_WEIGHTS["trend"])

    # 4) funding —— 资金费率（多头拥挤度）。没取到费率就不算。
    funding_ok = market_ok and _has_field(asset_context, "funding_rate_str")
    funding_pct = 0.0
    if funding_ok:
        try:
            funding_pct = float(asset_context.get("funding_rate_pct") or 0.0)
        except (TypeError, ValueError):
            funding_pct = 0.0
        funding_pct = max(-1.0, min(1.0, funding_pct))
    _put("funding", _clamp(-funding_pct / 0.1) if funding_ok else 0.0,
         f"资金费率={funding_pct:.4f}%" if funding_ok else "未取到资金费率 → 因子不可得",
         funding_ok, DIRECTIONAL_WEIGHTS["funding"])

    # 5) price_fund_divergence —— 价格与资金的背离（领先于滞后的 trend）。
    divergence = market_regime.detect_price_fund_divergence(
        trend=str(stats.get("trend", "")),
        funding_rate_pct=funding_pct,
        basis_bps=asset_context.get("basis_bps"),
        oi_change_pct=asset_context.get("oi_change_pct"),
    )
    regime_score = (
        -divergence["strength"] if divergence["divergence"] == "bearish"
        else divergence["strength"] if divergence["divergence"] == "bullish"
        else 0.0
    )
    _put("price_fund_divergence", regime_score, divergence["verdict"],
         bool(divergence.get("divergence")), DIRECTIONAL_WEIGHTS["price_fund_divergence"])

    # ══ 质量因子：只调 confidence，不参与方向 ═════════════════════════════
    try:
        atr_pct = max(0.0, min(100.0, float(stats.get("atr_pct") or 0.0)))
    except (TypeError, ValueError):
        atr_pct = 0.0
    atr_ok = _has_field(stats, "atr_str") or atr_pct > 0
    factors["volatility"] = {
        "score": round(_clamp(1 - atr_pct / 10.0), 4) if atr_ok else 0.0,
        "explanation": (f"ATR={atr_pct:.3f}%（波动越大风险越高）" if atr_ok
                        else "未取到 ATR → 风险未知（旧逻辑当成零波动给满分看多）"),
        "role": "quality", "available": bool(atr_ok), "weight": 0.0,
    }
    factors["cluster_heat"] = {
        "score": round(_clamp((cluster - 1) / 4.0), 4),
        "explanation": f"新闻聚合数量={cluster}" + ("" if cluster > 1 else "（无同族新闻 → 无佐证）"),
        "role": "quality", "available": cluster > 1, "weight": 0.0,
    }
    factors["historical_confidence"] = {
        "score": round(_clamp(((wins / settled) - 0.5) * 2.0), 4) if settled else 0.0,
        "explanation": f"{scope_note}已结算 {settled} 条，胜 {wins} 条",
        "role": "quality", "available": settled > 0, "weight": 0.0,
    }

    # 风险折扣（ATR 越大越不可靠）：atr 2.5% → ×0.95，atr 10% → ×0.80
    risk_factor = 1.0 - min(0.20, atr_pct / 50.0) if atr_ok else 1.0
    # 佐证加成（同族新闻越多越可信）：上限 +8%
    corroboration_factor = 1.0 + min(0.08, max(0, cluster - 1) * 0.04)

    # 7) fundamental —— v405：公司级基本面（估值历史分位 + 盈利修正方向）。
    #    这是唯一一条不是「价格派生」也不算「情绪口径」的方向因子。读数来自
    #    fundamental_factor.build()（零网络读库：财报两表 + 自累积的 PE 快照）。
    #    BTC/XAU/WTI 这类非公司标的根本没有这条读数 —— 记不可得、让出 0.08 权重，
    #    不拿「市值/PE」硬套到数字货币上。
    _fund_info: Dict[str, Any] = {"available": False, "detail": "未接入基本面因子"}
    _fund_raw = row.get("fundamental")
    if isinstance(_fund_raw, str):
        try:
            _fund_raw = json.loads(_fund_raw)
        except (json.JSONDecodeError, TypeError):
            _fund_raw = None
    if isinstance(_fund_raw, dict) and _fund_raw.get("available"):
        _f_score = _clamp(float(_fund_raw.get("score") or 0.0))
        _f_comps = _fund_raw.get("components") or {}
        _f_flags = [str(x) for x in (_fund_raw.get("flags") or [])]
        _f_bits = []
        # v460: 非公司标的（黄金/原油/美元/外汇/加密）走 alt_fundamental，分量的口径
        # 是「实际利率/库存/COT/资金费率」，不是「估值分位/盈利修正」。不分开写，
        # 复盘会把黄金的 -0.46 当成"PE 太贵"。
        _is_alt = _fund_raw.get("kind") == "alt"
        # v475：这条打折从 alt 专有改成两侧通用 —— 公司票也有可信度了（v474），
        # 高分歧的 COIN 若仍拿满 0.08 权重，等于"标出来了但没管"。
        _f_w = DIRECTIONAL_WEIGHTS["fundamental"]
        _conf = _fund_raw.get("confidence")
        if isinstance(_conf, (int, float)):
            _f_w = _f_w * (0.6 + 0.4 * max(0.0, min(1.0, float(_conf))))
        if _is_alt:
            for _e in (_fund_raw.get("evidence") or [])[:4]:
                _f_bits.append(str(_e))
        # v472/v474：分歧度必须写进解释里 —— 同一个分数，"全体轻微偏多"和
        # "链上极多 / ETF 极空"含义完全不同；公司票的"估值说便宜、盈利在砍"同理。
        # 两个口径字段同名，所以这句放公共路径，不分公司/非公司。
        _dl = _fund_raw.get("dispersion_level")
        if _dl:
            _f_bits.append(
                f"分量分歧度{_dl}（{_fund_raw.get('dispersion')}，"
                f"多头权重 {_fund_raw.get('bull_weight')} / 空头权重 {_fund_raw.get('bear_weight')}，"
                f"可信度 {_fund_raw.get('confidence')}）")
        # v406: 有护栏时展示「折半后」的估值分，否则展示的分数和真正参与加权的不一致
        _f_val = _f_comps.get("valuation_used")
        if not isinstance(_f_val, (int, float)):
            _f_val = _f_comps.get("valuation_percentile")
        if isinstance(_f_val, (int, float)):
            _f_bits.append(f"估值分位分={_f_val:+.2f}")
        # v448-fix: 展示"折半后"的修正分；亏损股不看修正项（只有口径分，避免解释里出现
        # 一个根本没参与加权的 -0.50，让人误以为亏损股也被盈利修正拉低了）
        if _f_comps.get("path") != "loss_maker":
            _f_rev = _f_comps.get("earnings_revision_used")
            if not isinstance(_f_rev, (int, float)):
                _f_rev = _f_comps.get("earnings_revision")
            if isinstance(_f_rev, (int, float)):
                _f_bits.append(f"盈利修正分={_f_rev:+.2f}")
        # v448: 盈利位置 / 周期度 / 亏损口径 —— 不写出来，复盘只能看到"折半过"的分，
        # 却不知道是"周期见顶折半"还是"价值陷阱折半"，两者含义完全不同。
        _f_path = _f_comps.get("path")
        if _f_path == "loss_maker":
            _f_loss = _f_comps.get("loss_score")
            if isinstance(_f_loss, (int, float)):
                _f_bits.append(f"亏损股口径分={_f_loss:+.2f}")
            for _k, _label in (("rev_yoy", "营收增速"), ("gm_trend", "毛利率趋势"),
                               ("ocf_margin", "现金流率"), ("ocf_trend", "现金流趋势")):
                if isinstance(_f_comps.get(_k), (int, float)):
                    _f_bits.append(f"{_label}={_f_comps[_k]:+.2f}")
        _f_pos = _f_comps.get("earnings_position")
        if isinstance(_f_pos, (int, float)):
            _f_bits.append(f"盈利位置={_f_pos * 100:.0f}%")
        if isinstance(_f_comps.get("cyclicity"), (int, float)):
            _f_bits.append(f"周期度={_f_comps['cyclicity']}")
        # v454: 同业可比与宏观倾斜 —— 这两项都改变了分值，不写出来复盘会以为
        # "分数自己变了"。同业显示"跟谁比、便宜多少"，宏观显示"哪个板块、倾斜多少"。
        if isinstance(_f_comps.get("peer_rel_score"), (int, float)):
            _f_bits.append(f"同业可比={_f_comps['peer_rel_score']:+.2f}"
                           f"（{_f_comps.get('peer_sector') or '?'}）")
        if isinstance(_f_comps.get("macro_tilt"), (int, float)):
            # v459: 上限改为读常量 —— cap 从 0.15 降到 0.05 后，写死的"±0.15"会让复盘
            # 误判影响幅度（相差 3 倍）。
            try:
                import fundamental_factor as _ff_cap
                _cap_label = f"{float(_ff_cap._MACRO_TILT_CAP):.2f}"
            except Exception:
                _cap_label = "见常量"
            _f_bits.append(f"宏观倾斜={_f_comps['macro_tilt']:+.2f}"
                           f"（{_f_comps.get('macro_sector') or '?'}，上限±{_cap_label}）")
        # v488：生意质量（毛利率/FCF/杠杆）—— 只做解释，不参与打分。
        # 不写进解释的话，复盘只能看到 flags 里的 gm_down，不知道差多少。
        _dep_bits = []
        if isinstance(_f_comps.get("depth_gross_margin_ttm"), (int, float)):
            _dep_bits.append(f"毛利率{_f_comps['depth_gross_margin_ttm']:.1f}%")
        if isinstance(_f_comps.get("depth_gm_delta_pp"), (int, float)):
            _dep_bits.append(f"同比{_f_comps['depth_gm_delta_pp']:+.1f}pp")
        if isinstance(_f_comps.get("depth_fcf_margin_ttm"), (int, float)):
            _dep_bits.append(f"FCF率{_f_comps['depth_fcf_margin_ttm']:+.1f}%")
        if (isinstance(_f_comps.get("depth_leverage"), (int, float))
                and _f_comps["depth_leverage"] >= 0.85):
            _dep_bits.append(f"负债/资产{_f_comps['depth_leverage']:.2f}")
        if _dep_bits:
            _f_bits.append("生意质量 " + "/".join(_dep_bits[:3]))
        if _f_flags:
            _f_bits.append("护栏=" + "/".join(_f_flags))
        _fund_info = {
            "available": True,
            "score": round(_f_score, 4),
            "ticker": _fund_raw.get("ticker"),
            "kind": _fund_raw.get("kind"),
            "asset": _fund_raw.get("asset"),
            "reason": _fund_raw.get("reason"),
            "market": _fund_raw.get("market"),
            "flags": _f_flags,
            "components": _f_comps,
            "evidence": (_fund_raw.get("evidence") or [])[:3],
            "gaps": (_fund_raw.get("gaps") or [])[:4],
            "earnings_in_days": _fund_raw.get("earnings_in_days"),
            "detail": str(_fund_raw.get("detail") or "")[:120],
            # v475：分歧度/可信度跟着读数一起往下传 —— 门槛闸与影子账本都读它
            "dispersion": _fund_raw.get("dispersion"),
            "dispersion_level": _fund_raw.get("dispersion_level"),
            "confidence": _fund_raw.get("confidence"),
            "weight_effective": round(_f_w, 4),
        }
        # v460: alt 与公司侧共用一条 0.08 权重，但文案必须分开 —— 否则「估值分位/盈利
        # 修正」的说法会被套到黄金/原油/加密上，读的人会以为它们有财报。
        _fund_label = "非公司基本面" if _is_alt else "公司级基本面"
        _fund_name = _fund_raw.get("asset") if _is_alt else _fund_raw.get("ticker")
        # v472：高分歧按可信度打折（0.6~1.0 倍），不让"互相打架的分量"满权重进方向分。
        # 下限 0.6 是为了"分歧大 ≠ 没信息"：仍然保留一半左右的权重，而不是直接清零。
        _put("fundamental", _f_score,
             f"{_fund_label} {_fund_name}: " + "；".join(_f_bits or ["读数可得"]),
             True, _f_w)
    else:
        _fund_detail = "基本面不可得"
        _fund_reason = None
        _fund_kind = None
        if isinstance(_fund_raw, dict):
            _fund_detail = str(_fund_raw.get("detail") or "基本面无读数")[:80]
            _fund_reason = _fund_raw.get("reason")
            _fund_kind = _fund_raw.get("kind")
        # v406: 把「不适用(non_equity)」和「缺口(placeholder/no_data)」在输出里分开 ——
        # 都写"不可得"会让复盘误以为加密货币也该有财报，或以为 A股已覆盖。
        _fund_info = {"available": False, "detail": _fund_detail,
                      "kind": _fund_kind, "reason": _fund_reason}
        _put("fundamental", 0.0, _fund_detail, False, DIRECTIONAL_WEIGHTS["fundamental"])

    # ══ 方向分：只在真实可得的因子上加权，并按可得权重归一化 ════════════
    weights = dict(DIRECTIONAL_WEIGHTS)
    w_total = sum(weights.values())
    w_avail = sum(eff_weight[name] for name in weights)
    direction = 0.0
    if w_avail > 0:
        direction = _clamp(
            sum(factors[name]["score"] * eff_weight[name] for name in weights) / w_avail
        )
    coverage = (w_avail / w_total) if w_total > 0 else 0.0

    # 锚点规则：AI 没有方向观点（HOLD/分析失败）时，市场上下文不得单方面造出
    # 方向 —— 否则每条观望新闻都会因为「趋势偏空」被判成 SELL。市场的独立读数
    # 另存 market_bias 供研究端查看，不参与方向判定。
    market_names = [n for n in weights if n not in ("news_sentiment", "fundamental") and avail.get(n)]
    market_w = sum(eff_weight[n] for n in market_names)
    market_bias = None
    if market_w > 0:
        market_bias = round(_clamp(
            sum(factors[n]["score"] * eff_weight[n] for n in market_names) / market_w
        ), 4)
    base_score = direction if has_anchor else 0.0

    # 影响评估（AI 分析产出的 0-10 分；缺省 5 = 中性）改成**幅度放大系数**，
    # 不再是加法：影响度是无方向的量度，加法等于给每条高影响新闻白送看多分，
    # 还会把 final_score 冲出 [-1,1]。
    impact = _clamp10(row.get("impact_score"), 5.0)
    heat = _clamp10(row.get("heat_score"), 5.0)
    asset_impact = _clamp10(row.get("asset_impact"), 5.0)
    impact_scale = 1.0 + (impact - 5.0) * 0.03 + (heat - 5.0) * 0.01 + (asset_impact - 5.0) * 0.02
    final_score = round(_clamp(base_score * impact_scale), 4)

    # ── v342: 预期差（actual vs forecast）—— 只做「反方证据」，不给方向 ──────
    # 为什么不当方向因子：同一个超预期，对 CPI 是利空风险资产、对失业率是利多，
    # 符号取决于指标与政策反应函数；写死符号等于制造假精度。
    # 所以只在「预期差与新闻方向相反且幅度够大」时**减分 + 记矛盾**，
    # 权重表一个字不动（不重排 DIRECTIONAL_WEIGHTS）。
    _surprise_info: Dict[str, Any] = {"available": False, "damp": 1.0,
                                      "detail": "无预期差输入（未接事件）"}
    _pre_surprise_score = final_score
    try:
        import surprise_factor as _su
        _s = row.get("surprise")
        if isinstance(_s, str):
            try:
                _s = json.loads(_s)
            except (json.JSONDecodeError, TypeError):
                _s = None
        if isinstance(_s, dict) and _s.get("available"):
            _act_for_surprise = action_hint if action_hint in ("buy", "sell") else (
                "BUY" if final_score > 0 else "SELL" if final_score < 0 else "")
            _norm = abs(float(_s.get("norm") or 0.0))
            _conflict = _su.conflict_with_action(_s, _act_for_surprise)
            _damp = 1.0
            if _conflict and _norm >= 0.35:
                _damp = round(1.0 - min(0.25, 0.12 * _norm), 4)
                final_score = round(_clamp(final_score * _damp), 4)
            _surprise_info = {
                "available": True, "norm": _s.get("norm"),
                "polarity": _s.get("polarity"), "conflict": bool(_conflict),
                "damp": _damp, "detail": _s.get("detail", ""),
                "event": _s.get("event"),
            }
        elif isinstance(_s, dict):
            _surprise_info = {"available": False,
                              "detail": _s.get("detail", "预期差不可得"), "damp": 1.0}
    except Exception as _su_exc:
        _surprise_info = {"available": False, "damp": 1.0,
                          "detail": f"预期差模块出错: {type(_su_exc).__name__}"}

    # ── v343: 技术状态读数 —— 只当反方证据，不给方向 ──────────────────────
    # 数据来自快照的 technical_state（六维合成，见 technical_state.py）。
    # 沿用 surprise 的同一套哲学：不新增方向权重、不重排 DIRECTIONAL_WEIGHTS，
    # 只在「读数明确反向」时按小系数折减 + 记矛盾。
    # 只认 usable=True 的读数（覆盖 ≥50% 维度且 |bias| ≥0.45）：
    # 单维度推出来的偏置（例如黄金只有区间位置可算）不能当反方证据。
    _tech_info: Dict[str, Any] = {"available": False, "detail": "快照未带技术状态读数"}
    _tstate = (asset_context.get("technical_state")
               if isinstance(asset_context.get("technical_state"), dict) else {})
    if _tstate.get("available"):
        _t_bias = _tstate.get("bias")
        _t_usable = bool(_tstate.get("usable"))
        _act_for_tech = (action_hint.upper() if action_hint in ("buy", "sell")
                         else ("BUY" if final_score > 0 else "SELL" if final_score < 0 else ""))
        _t_conflict = False
        _t_damp = 1.0
        if (_t_usable and isinstance(_t_bias, (int, float)) and _act_for_tech
                and (_act_for_tech == "BUY" and _t_bias <= -0.45
                     or _act_for_tech == "SELL" and _t_bias >= 0.45)):
            _t_conflict = True
            _t_damp = round(1.0 - min(0.15, abs(float(_t_bias)) * 0.15), 4)
            final_score = round(_clamp(final_score * _t_damp), 4)
        _tech_info = {
            "available": True, "usable": _t_usable, "bias": _t_bias,
            "coverage": _tstate.get("coverage"), "conflict": _t_conflict,
            "damp": _t_damp,
            "readout": str(_tstate.get("readout") or "")[:160],
            "missing": _tstate.get("missing") or [],
        }

    # v412: 风险档位 → 执行建议（仓位/止损），**不进方向分**
    # 状态层已经证明没有方向优势（v408~v411），但"局面有多险"是可测的：
    # 波动扩张 + 趋势极端 + 价格离开价值区 + 杠杆在撤 同时出现时，5 根内逆风明显更大。
    # 所以这里只产出 size_factor / stop_atr_multiple / 常见逆风幅度，交给执行层用。
    _risk_plan: Dict[str, Any] = {"available": False, "detail": "快照未带风险档位"}
    _rk = _tstate.get("risk") if isinstance(_tstate.get("risk"), dict) else None
    if _rk:
        _risk_plan = {
            "available": True,
            "level": _rk.get("level"),
            "size_factor": _rk.get("size_factor"),
            "stop_atr_multiple": _rk.get("stop_atr_multiple"),
            "atr_pct": _rk.get("atr_pct"),
            "stop_pct_hint": (round(float(_rk["atr_pct"]) * float(_rk["stop_atr_multiple"]), 2)
                              if _rk.get("atr_pct") and _rk.get("stop_atr_multiple") else None),
            "expected_adverse_pct": _rk.get("expected_adverse_pct"),
            # v418：赔率侧
            "odds_typical": _rk.get("odds_typical"),
            "require_space_pct": _rk.get("require_space_pct"),
            # v420：成本地板
            "round_trip_cost_pct": _rk.get("round_trip_cost_pct"),
            "require_basis": _rk.get("require_basis"),
            "odds_sample": _rk.get("odds_sample"),
            # v419：持有期侧
            "min_hold_bars": _rk.get("min_hold_bars"),
            "holding_note": _rk.get("holding_note"),
            "odds_note": _rk.get("odds_note"),
            "drivers": list(_rk.get("drivers") or []),
            "note": _rk.get("note"),
            # v415：日内"等风险"参考（不改 size_factor，只提示同样的钱现在挨多少）
            "atr_vs_median": _rk.get("atr_vs_median"),
            "equal_risk_size": _rk.get("equal_risk_size"),
            "equal_risk_note": _rk.get("equal_risk_note"),
            "size_basis": _rk.get("size_basis"),
            "validated_timeframes": _rk.get("validated_timeframes"),
            # v417：止损距离 → 实际可下单的敞口（单笔风险预算 1% 本金）
            "stop_pct": _rk.get("stop_pct"),
            "max_risk_pct": _rk.get("max_risk_pct"),
            "position_pct": _rk.get("position_pct"),
            "position_pct_equal_risk": _rk.get("position_pct_equal_risk"),
        }

    # 方向不对称门槛：BUY 需要更高分（做多胜率偏低），SELL 门槛较低（做空更倾向）
    scores = _direction_scores()
    raw_action = (
        "BUY" if final_score >= scores["buy_min"]
        else "SELL" if final_score <= -scores["sell_min"]
        else "HOLD"
    )
    # ── v321.3 P1: 按 (资产, 方向) 的历史判别力校准门槛 ────────────────────
    # 闸门整体拒对率 ≈ 基础概率 (+0.03pp) 看似无用, 但那是**整体**数字:
    # 按分组拆开后 BTC BUY -7.50pp / BTC SELL +10.48pp / WTI BUY +9.89pp ……
    # **同一资产不同方向结论相反** → 门槛必须按分组调, 而不是一刀切。
    # 校准只对**已应用** (applied=1) 的分组生效; 没有记录就用默认门槛。
    # 原始门槛保留在 output 里, 随时可追溯 (不黑箱)。
    asset_key = str(row.get("asset") or row.get("target_asset") or "").upper()
    cal_action = raw_action if raw_action in ("BUY", "SELL") else ""
    buy_min_c, sell_min_c, calib_info = _calibrated_thresholds(asset_key, cal_action)
    if calib_info is not None:
        raw_action = (
            "BUY" if final_score >= buy_min_c
            else "SELL" if final_score <= -sell_min_c
            else "HOLD"
        )
    # ── v475：可信度门槛闸 ──────────────────────────────────────────────
    # "仓位打多少折"（v473）和"该不该开这一仓"是两件事：COIN 那种 +0.07 分、
    # 可信度 0.44 的票，按原来的门槛会开成一笔"微小多头"，而这更像"不知道"。
    # 只收紧不放宽，且封顶 0.10 —— 基本面只有 0.08 权重，不该靠它否决整条信号。
    _pen = _confidence_penalty(_fund_info.get("confidence"))
    buy_min_e = round(buy_min_c + _pen, 4)
    sell_min_e = round(sell_min_c + _pen, 4)
    _pre_gate_action = raw_action
    if _pen > 0:
        raw_action = (
            "BUY" if final_score >= buy_min_e
            else "SELL" if final_score <= -sell_min_e
            else "HOLD"
        )
    validation = evidence.evaluate(
        factors, final_score, raw_action, wins, settled, scope=scope,
        coverage=coverage, risk_factor=risk_factor,
        corroboration_factor=corroboration_factor,
    )
    # ── v181: 取消「只能看」分支（realtime_filter 二分类后此值不再产生）。
    # 若历史数据里有遗留 verdict="只能看"，这里降级为"放行（老数据兜底）"，
    # 不再用它强制 HOLD（避免旧新闻被永久封印）。新进新闻只会有"可交易"或"排除"。
    gate_verdict = str(row.get("gate_verdict") or "")
    if gate_verdict == "只能看":
        # 历史遗留：当作可交易，让 evidence + score 自行裁决
        gate_verdict = ""  # 让后续分支等价于"无门卫标签"
    # ── v342: confidence 三量拆分（纯增量：不改 unified_confidence 的原值）──
    # 一个 0-100 的数字同时承载方向/赔率/历史可靠，事后无法判断哪一环错了。
    # 这里另存三量，供复盘与前端分开看。
    try:
        import confidence_split as _cs
        _conf_split = _cs.compute(
            coverage=coverage, wins=wins, settled=settled,
            expected_move_pct=_expected_move_pct(row),
            atr_pct=atr_pct if atr_ok else None,
            contradiction_count=len(validation.get("contradictions") or []),
        )
    except Exception as _e:  # 拆分失败必须不影响主判定
        _conf_split = {"notes": [f"confidence_split 出错: {_e}"]}

    result = {
        "factors": factors,
        "weights": weights,
        "final_score": final_score,
        "raw_action": raw_action,
        "action": validation["gated_action"],
        "confidence": validation["confidence"],
        "evidence": validation["evidence"],
        "significance": validation["significance"],
        "contradictions": validation["contradictions"],
        "confidence_detail": validation["confidence_detail"],
        "verdict": validation["verdict"],
        # v318 新增：证据覆盖度 / 可得因子 / 市场独立读数 / 影响放大系数
        "coverage": round(coverage, 4),
        "available_factors": [name for name in weights if avail.get(name)],
        "market_bias": market_bias,
        "impact_scale": round(impact_scale, 4),
        # v342: 结构评分原始读数（可解释，不黑箱）+ confidence 三量
        "structure": _structure,
        "confidence_split": _conf_split,
        # v342: 预期差读数（含是否与新闻方向相反、减分系数）
        "surprise": _surprise_info,
        "pre_surprise_score": _pre_surprise_score,
        # v343: 技术状态读数（是否与决策方向相反、减分系数）——可追溯，不黑箱
        "technical_state": _tech_info,
        # v412: 风险档位（执行建议：仓位系数/止损 ATR 倍数/常见逆风）；不参与方向分
        "risk_plan": _risk_plan,
        # v405: 基本面读数（估值历史分位 + 盈利修正方向；含是否与新闻方向相反）
        "fundamental": _fund_info,
        # v321.3 P1: 门槛 + 分组校准信息 (可追溯, 不黑箱)
        "thresholds": {
            "buy_min": scores["buy_min"], "sell_min": scores["sell_min"],
            "buy_min_calibrated": round(buy_min_c, 4),
            "sell_min_calibrated": round(sell_min_c, 4),
            # v475：最终生效门槛 = 校准门槛 + 可信度收紧量（三个数都留，复盘能算清是哪一步）
            "buy_min_effective": buy_min_e,
            "sell_min_effective": sell_min_e,
            "confidence_penalty": _pen,
            "action_before_confidence_gate": _pre_gate_action,
        },
        "confidence_gate": ({"confidence": _fund_info.get("confidence"),
                             "penalty": _pen, "changed_action": raw_action != _pre_gate_action}
                            if _pen > 0 else None),
        "gate_calibration": calib_info,
    }
    if gate_verdict == "排除":
        result.update({
            "raw_action": "HOLD",
            "action": "HOLD",
            "verdict": f"门卫四步标签=排除：只分析不下单",
            "gate_blocked": True,
            "gate_verdict": gate_verdict,
        })
    # v342: 预期差与新闻方向相反 → 记矛盾（复盘可见），来源标 news_vs_surprise
    if _surprise_info.get("conflict"):
        _damp = float(_surprise_info.get("damp") or 1.0)
        result.setdefault("contradictions", []).append({
            "type": "news_vs_surprise",
            "detail": (f"预期差与新闻方向相反（normalized "
                       f"{float(_surprise_info.get('norm') or 0.0):+.2f}）"
                       f"，方向分按 ×{_damp:.2f} 折减"),
        })
    # v343: 技术状态读数与决策方向相反 → 记矛盾（复盘可见），来源标 news_vs_tech_state
    if _tech_info.get("conflict"):
        result.setdefault("contradictions", []).append({
            "type": "news_vs_tech_state",
            "detail": (f"技术状态偏置 {float(_tech_info.get('bias') or 0.0):+.2f}"
                       f"（覆盖 {float(_tech_info.get('coverage') or 0.0):.0%}）与决策方向相反，"
                       f"方向分按 ×{float(_tech_info.get('damp') or 1.0):.2f} 折减"),
        })
    # v405: 基本面读数与新闻方向相反 → 记矛盾（不折减，只是让复盘看得见）
    # 为什么不折减：基本面是季报慢变量，权重 0.08 已经把它压在弱信号的位置；
    # 再叠一层折减等于同一个反向证据被扣两次，且会让 HOLD 泛滥。
    if _fund_info.get("available") and has_anchor:
        _f_score = float(_fund_info.get("score") or 0.0)
        if abs(_f_score) >= 0.35 and (sentiment > 0) != (_f_score > 0):
            result.setdefault("contradictions", []).append({
                "type": "news_vs_fundamental",
                "detail": (f"新闻情绪 {sentiment:+.2f} 与基本面读数 {_f_score:+.2f}"
                           f"（{_fund_info.get('ticker')}）方向相反，仅记录不折减"),
            })
    # v412: HOLD 时的"不参与理由"——复盘要能用，不是只留一个 HOLD
    result["hold_reason"] = _hold_reason(
        final_score=final_score, raw_action=raw_action, action=result["action"],
        scores=scores, factors=factors, weights=weights, avail=avail, coverage=coverage,
        validation=validation, risk_plan=_risk_plan,
        expected_move_pct=_expected_move_pct(row), tech_info=_tech_info,
    )
    # v412: 风险档位与最终动作一并给出执行口径（高风险档即使方向成立也建议缩量）
    if _risk_plan.get("available") and _risk_plan.get("level") == "高":
        result["risk_plan"]["applies_to"] = validation["gated_action"]
        result["risk_plan"]["detail"] = (
            f"{validation['gated_action']} 且风险档位=高 → 建议按 {_risk_plan['size_factor']}× 仓位执行，"
            f"止损放 {_risk_plan['stop_atr_multiple']} 倍 ATR"
            + (f"（≈{_risk_plan['stop_pct_hint']}%）" if _risk_plan.get("stop_pct_hint") else "")
        )
    # ── v448: 开仓位置闸 / 同向确认闸 ────────────────────────────────
    # 默认**影子模式**：只把"当前价在 20 分钟窗口的什么位置、前 30 分钟是否同向"
    # 标注进结果，不改动作。原因不是保守，是 2030 笔历史信号实测：
    #   · "不追极值"在 1 小时档是**反向**的（被拦那档反而更好），不能直接生效；
    #   · "同向确认"在 1 小时档能筛掉更差的一档，但放行组仍是负期望。
    # 要真拦单必须显式设 ENTRY_GUARD_MODE=enforce；见 docs/v448。
    if os.getenv("ENTRY_GUARD_ENABLED", "1") == "1" and result.get("action") in ("BUY", "SELL"):
        try:
            import entry_guard as _eg
            result["entry_guard"] = _eg.annotate(row, result, asof_ts=asof_ts)
        except Exception as _eg_exc:
            result["entry_guard"] = {
                "available": False,
                "error": f"{type(_eg_exc).__name__}: {str(_eg_exc)[:80]}",
            }
    return result
