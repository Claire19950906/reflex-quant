#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_opportunity.py — **时点口径 (point-in-time)** 的事件机会卡 (v319+)

背景
----
归因页已经能看到"事件 → K 线"的真实冲击 (event_kline_impact), 但那是**事后**
解释。要能事前回答"这个事件现在值不值得做", 必须在**事件发生的那个时点**,
只用当时已知的信息算出: 受影响标的 / 方向 / 依据 / 时点 / 风控。

为什么必须显式做"时点口径"
--------------------------
最省事的写法是"拿这个事件之后的实际涨跌当期望" —— 那是**未来函数**:
用事后胜率去做决策, 回测 90% 胜率、上线即穿仓。本模块的口径是:

  1. 期望幅度/方向来自**历史同类事件**的条件统计, 且只取 `event_time` **之前**的行;
  2. 波动率 σ 只用 `event_time` **之前**的日线;
  3. 成本门槛来自 cost_model (fee + slippage, 往返);
  4. 每个候选机会都用**它自己那个时点**的信息计算 (不是"现在"的信息)。

所以本模块的输出天然可回测: 用 t 时刻的事件生成机会, 就只用 ≤ t 的数据。

输出 (机会卡)
--------------
    {
      "asset": "XAU",
      "direction": "LONG" | "SHORT" | "NO_TRADE",
      "status": "READY" | "NO_TRADE",
      "edge_pct": 0.62,          # 条件期望幅度 (点时口径, 有符号)
      "edge_pct_net": 0.46,      # 扣往返成本后
      "edge_measure": "max_excursion_4h",   # ← edge 的度量口径 (见下)
      "confidence": 0.55,        # 0..1
      "rationale": "…",          # 依据 (中文, 可直接给用户看)
      "evidence": {...},         # 样本量/胜率/σ/信噪比 …
      "timing": {...},           # 入场模式/窗口/持有期/最早可平仓时点
      "risk": {...},             # 止损/目标/仓位上限/风险预算
      "cost": {...},             # cost_model.settlement_policy
      "affected_assets": [...],  # 事件影响的标的 (规则抽取)
      "reason": "…",             # status=NO_TRADE 时的原因
    }

口径边界 (别误读 edge_pct)
--------------------------
edge_pct 来自 event_kline_impact.impact_pct = 事件后 4h 窗口内的**最大偏移**
(max(上冲, 下砸)), 因此它是**可实现收益的上界**, 不是"持有到期能赚多少"。
真实前向收益无法在 4h 尺度上重建 (历史事件的分钟线覆盖率实测为 0, 日线只能
给到 24h 跨度), 所以保留最大偏移口径并显式标注 —— 卡片里每张都带
edge_measure / edge_measure_note, 前端与复盘都按"冲击量级"读, 不按"预期收益"读。

零外部依赖 (stdlib + event_struct + cost_model), 可离线单测。
"""
from __future__ import annotations

import math
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import cost_model
import event_struct

# ── 门槛 (全部可被调用方覆盖) ───────────────────────────────────────────────
MIN_SAMPLES = 5              # 历史样本少于这个数 → 不给方向 (诚实: 证据不足)
DEFAULT_LOOKBACK_DAYS = 180  # 条件统计的回看窗口
DEFAULT_HORIZON_MINUTES = 240  # 默认持有期 (4h)
DEFAULT_RISK_BUDGET_PCT = 0.5    # 单笔最大亏损 (占权益 %)
DEFAULT_MAX_POSITION_PCT = 10.0  # 单标的敞口上限 (占权益 %)
MIN_STOP_PCT = 0.3           # 止损下限 (太小会被噪声打掉)
# |edge| / σ(持有期) 低于此值 → 期望幅度淹没在噪声里 → 不做。
# ⚠️ 实测 (生产库 300 张卡): snr 中位数 0.175 / p90 0.233 / **最大 0.358**,
#    也就是说当前数据下**没有一张卡够得着 1.0** —— 真正卡住的是成本门槛
#    (edge 中位数 0.08% < 往返成本 0.16~0.32%)。
#    这个阈值**不是**随手写的: 它表示"事件冲击必须比一次典型的持有期波动还大",
#    低于这个量级的事件不足以驱动进场决策 (否则止损会被噪声打掉)。
#    别为了让 ready 变多就调松它 —— 那是把"诚实报不做"改成"硬给方向"。
MIN_SIGNAL_TO_NOISE = 1.0
CONFIRM_WINDOW_MINUTES = 30  # "确认后再进" 的等待窗口
IMMEDIATE_WINDOW_MINUTES = 5

# 定时公布的宏观数据 → 默认"等确认再进" (第一波常是假突破)
CONFIRM_FIRST_EVENT_TYPES = frozenset({
    "加息", "降息", "CPI", "非农", "原油库存", "macro_speech",
})

# 规则方向 bias (中文) → 交易方向
_BIAS_TO_DIRECTION = {"利多": "LONG", "利空": "SHORT", "中性": "NEUTRAL"}

# ── edge 的度量口径 (必须对外声明, 否则会被误读成"预期到手收益") ─────────────
# edge_pct 来自 event_kline_impact.impact_pct, 而该列是**事件后 4h 窗口内的
# 最大偏移** (max(上冲, 下砸), 见 engine/event_kline_impact_worker.py:
# impact_pct = max(up_move, dn_move)), 不是"持有到期时的收盘收益"。
#
# 差别很大: 事后极值是**可实现收益的上界** —— 你不可能在事件落地的瞬间
# 就知道哪一边会走出更大的偏移并恰好在极值成交。所以 edge_pct 只能读作
# "事件冲击的量级", **不能**读作"这一单预期能赚 edge_pct"。
#
# 为什么不用真实前向收益替代: 实测历史事件在事件后 4h 内有分钟线的覆盖
# 率为 0 (ohlcv_intraday 只覆盖最近约一个月), 日线又只能给到 24h 跨度 ——
# 4h 尺度上根本没有可用的前向价格序列, 拿日线硬凑会引入 horizon 错配。
# 因此保留最大偏移口径 (样本量足够、跨历史一致), 但显式标注它是上界。
EDGE_MEASURE = "max_excursion_4h"
EDGE_MEASURE_NOTE = (
    "edge_pct 是事件后 4h 内最大偏移的条件均值 = 可实现收益的**上界**; "
    "实际到手要打折 (无法在极值成交), 不能直接当成预期收益"
)


def _signed_pct(impact_pct: Any, direction: Any) -> float:
    """event_kline_impact 行 → **有符号百分数**。

    impact_pct 是小数 (0.0138 = 1.38%) 且恒 ≥ 0; 方向另存 direction 列。
    """
    try:
        frac = float(impact_pct or 0.0)
    except (TypeError, ValueError):
        return 0.0
    mag = abs(frac) * 100.0
    d = str(direction or "neutral").strip().lower()
    if d == "up":
        return mag
    if d == "down":
        return -mag
    return 0.0


def _classify(title: str) -> Tuple[str, List[str], Optional[float], Optional[str]]:
    """标题 → (事件类型, 受影响标的, 量级数值, 量级单位)。规则抽取, 不猜。"""
    try:
        atoms = event_struct.extract_events(title or "")
    except Exception:
        atoms = []
    if not atoms:
        return "", [], None, None
    a = atoms[0]
    return (
        str(a.get("event_type") or ""),
        list(a.get("target_assets") or []),
        a.get("magnitude_value"),
        a.get("magnitude_unit"),
    )


def _sigma_daily_pct(conn: sqlite3.Connection, asset: str, as_of_ts: int,
                     window: int = 20) -> float:
    """**事件时点之前**的日线对数收益标准差 (%)。取不到 → 0.0。"""
    try:
        rows = conn.execute(
            "SELECT close FROM ohlcv_daily WHERE UPPER(asset)=? AND ts<? AND close>0 "
            "ORDER BY ts DESC LIMIT ?",
            (asset.upper(), int(as_of_ts), int(window)),
        ).fetchall()
    except sqlite3.Error:
        return 0.0
    closes = []
    for r in rows:
        try:
            closes.append(float(r[0]))
        except (TypeError, ValueError):
            continue
    if len(closes) < 3:
        return 0.0
    closes.reverse()  # 时间正序
    rets = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 0.0)) * 100.0



def _event_stats(conn: sqlite3.Connection, asset: str, as_of_ts: int,
                 lookback_days: int, event_type: str = "") -> Dict[str, Any]:
    """同标的 / 同类事件的**历史响应**统计 (严格只用 as_of_ts 之前的行)。

    返回:
        {"samples", "mean_move_pct", "mean_abs_pct", "hit_rate", "up", "down", "scope"}
      scope: "same_type" (同类事件样本足够) / "asset" (退回标的整体) / "none"
    """
    frm = int(as_of_ts) - int(lookback_days) * 86400
    try:
        rows = conn.execute(
            "SELECT event_title, impact_pct, direction FROM event_kline_impact "
            "WHERE UPPER(asset)=? AND event_time<? AND event_time>=? "
            "ORDER BY event_time DESC LIMIT 2000",
            (asset.upper(), int(as_of_ts), frm),
        ).fetchall()
    except sqlite3.Error:
        rows = []

    same_type: List[float] = []
    all_moves: List[float] = []
    for r in rows:
        title, impact_pct, direction = r[0], r[1], r[2]
        mv = _signed_pct(impact_pct, direction)
        if mv == 0.0:
            continue
        all_moves.append(mv)
        if event_type:
            t_type, _assets, _v, _u = _classify(title or "")
            if t_type == event_type:
                same_type.append(mv)

    def _agg(moves: List[float]) -> Dict[str, Any]:
        if not moves:
            return {"samples": 0, "mean_move_pct": 0.0, "mean_abs_pct": 0.0,
                    "hit_rate": 0.0, "up": 0, "down": 0}
        mean = sum(moves) / len(moves)
        up = sum(1 for m in moves if m > 0)
        down = sum(1 for m in moves if m < 0)
        same_sign = up if mean > 0 else down
        return {
            "samples": len(moves),
            "mean_move_pct": round(mean, 4),
            "mean_abs_pct": round(sum(abs(m) for m in moves) / len(moves), 4),
            "hit_rate": round(same_sign / len(moves), 4),
            "up": up,
            "down": down,
        }

    scope = "none"
    moves = all_moves
    if event_type and len(same_type) >= MIN_SAMPLES:
        moves = same_type
        scope = "same_type"
    elif all_moves:
        scope = "asset"
    stats = _agg(moves)
    stats["scope"] = scope
    stats["same_type_samples"] = len(same_type)
    return stats


def _entry_plan(event_type: str, entry_mode: str, event_time: int) -> Dict[str, Any]:
    """入场时点方案。

    entry_mode:
      "auto"     → 定时公布的宏观数据默认"确认后进", 其余"立即进";
      "confirm"  → 等第一根 5min 级别的确认 (最坏等 CONFIRM_WINDOW_MINUTES);
      "immediate"→ 事件落地即市价进 (窗口 IMMEDIATE_WINDOW_MINUTES)。
    """
    mode = (entry_mode or "auto").strip().lower()
    if mode == "auto":
        mode = "confirm" if event_type in CONFIRM_FIRST_EVENT_TYPES else "immediate"
    window = CONFIRM_WINDOW_MINUTES if mode == "confirm" else IMMEDIATE_WINDOW_MINUTES
    return {
        "entry_mode": mode,
        "entry_window_minutes": window,
        "entry_not_before_ts": int(event_time),
        "entry_deadline_ts": int(event_time) + window * 60,
        "entry_note": ("定时公布类数据: 等第一根确认 K 线再进, 避免吃到第一波假突破"
                       if mode == "confirm" else "事件落地即进, 窗口内未确认则放弃"),
    }



def build_opportunity(conn: sqlite3.Connection, *, asset: str, event_time: int,
                      title: str = "", severity: str = "", event_id: str = "",
                      horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
                      lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                      risk_budget_pct: float = DEFAULT_RISK_BUDGET_PCT,
                      max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
                      entry_mode: str = "auto",
                      min_samples: int = MIN_SAMPLES,
                      as_of_ts: Optional[int] = None) -> Dict[str, Any]:
    """给**单个事件**算一张机会卡 (严格时点口径)。

    参数:
      conn          sqlite 连接 (只读)
      asset         标的 (XAU / BTC / …)
      event_time    事件发生时间 (epoch 秒)
      title         事件标题 (已知信息)
      severity      事件重要度 (high/medium/low)
      horizon_minutes 计划持有期 (分钟)
      risk_budget_pct  单笔最大亏损预算 (占权益 %)
      max_position_pct 单标的敞口上限 (占权益 %)
      entry_mode    auto / confirm / immediate
      min_samples   出方向所需的最小历史样本数
      as_of_ts      **统计截止时点** (epoch 秒)。缺省 = event_time。
                    v318: 日历里的**未来**事件必须显式传"现在"作为截止点 ——
                    否则统计会把事件之后才发生的历史事件也算进来 (未来函数)。
                    event_time 仍用于计算入场窗口与最早可平仓时点。
    """
    asset_u = (asset or "").strip().upper()
    event_time = int(event_time)
    as_of = int(as_of_ts) if as_of_ts else event_time
    event_type, affected, magnitude, magnitude_unit = _classify(title)

    stats = _event_stats(conn, asset_u, as_of, lookback_days, event_type)
    sigma_daily = _sigma_daily_pct(conn, asset_u, as_of)
    horizon_days = max(float(horizon_minutes) / 1440.0, 1.0 / 24.0)
    sigma_horizon = sigma_daily * math.sqrt(horizon_days)

    policy = cost_model.settlement_policy(asset_u)
    cost_pct = float(policy["round_trip_pct"])
    earliest_exit = cost_model.earliest_exit_ts(asset_u, event_time)

    # 方向: 经验条件统计优先, 样本不足才用规则先验
    direction = "NO_TRADE"
    direction_source = "none"
    edge = 0.0
    if stats["samples"] >= min_samples and abs(stats["mean_move_pct"]) > 0:
        edge = float(stats["mean_move_pct"])
        direction = "LONG" if edge > 0 else "SHORT"
        direction_source = f"empirical_{stats['scope']}"
    else:
        bias = event_struct.direction_for(event_type, asset_u) if event_type else None
        mapped = _BIAS_TO_DIRECTION.get(str(bias or "").strip())
        if mapped and mapped != "NEUTRAL":
            direction = mapped
            direction_source = "rule_prior"
            edge = sigma_horizon if sigma_horizon > 0 else 0.0
            edge = edge if direction == "LONG" else -edge
        else:
            direction = "NO_TRADE"
            direction_source = "none"

    abs_edge = abs(edge)
    edge_net = abs_edge - cost_pct
    snr = (abs_edge / sigma_horizon) if sigma_horizon > 0 else 0.0

    # ── 风控 ────────────────────────────────────────────────────────────
    stop = max(sigma_horizon, MIN_STOP_PCT)
    if abs_edge > 0:
        stop = min(stop, abs_edge * 0.8)
    stop = round(max(stop, MIN_STOP_PCT), 4)
    target = round(max(abs_edge, stop * 1.5), 4)
    size_by_risk = (float(risk_budget_pct) / stop * 100.0) if stop > 0 else 0.0
    position_pct = round(min(float(max_position_pct), size_by_risk), 4)
    max_loss_pct = round(position_pct * stop / 100.0, 4)

    confidence = 0.0
    if direction != "NO_TRADE":
        sample_factor = min(1.0, stats["samples"] / 20.0)
        consistency = float(stats["hit_rate"]) if direction_source.startswith("empirical") else 0.4
        noise_factor = min(1.0, snr / 2.0)
        confidence = round(min(1.0, 0.4 * sample_factor + 0.35 * consistency + 0.25 * noise_factor), 3)

    # ── 门槛: 证据不足 / 淹没在噪声 / 扣完成本没优势 → NO_TRADE ─────────
    # 门槛顺序有讲究:
    #   · 规则先验的 edge 本来就定义为 1σ (按构造) → 不再拿信噪比判它,
    #     只看成本门槛 (它的低置信度已经体现在 confidence 上);
    #   · σ 未知 (没有事前日线) → 不做信噪比判定, 但在 evidence 里标明
    #     sigma_available=False, 不假装知道波动率。
    reason = ""
    if direction == "NO_TRADE":
        reason = (f"证据不足: 历史同类样本 {stats['samples']} 条 < {min_samples} 条, "
                  f"且规则先验也没给出方向")
    elif direction_source == "rule_prior":
        if edge_net <= 0:
            reason = (f"扣完成本没有优势: edge {abs_edge:.2f}% ≤ 往返成本 {cost_pct:.2f}%")
    elif sigma_horizon > 0 and snr < MIN_SIGNAL_TO_NOISE - 1e-9:
        reason = (f"信噪比不足: |edge|={abs_edge:.2f}% vs σ(持有期)={sigma_horizon:.2f}% "
                  f"({snr:.2f} < {MIN_SIGNAL_TO_NOISE})")
    elif edge_net <= 0:
        reason = (f"扣完成本没有优势: edge {abs_edge:.2f}% ≤ 往返成本 {cost_pct:.2f}%")
    status = "READY" if not reason else "NO_TRADE"
    if status == "NO_TRADE":
        direction = "NO_TRADE"

    timing = _entry_plan(event_type, entry_mode, event_time)
    timing.update({
        "hold_horizon_minutes": int(horizon_minutes),
        "t_plus": policy["t_plus"],
        "earliest_exit_ts": int(earliest_exit),
        "settlement_note": policy["settlement_note"],
    })

    rationale = _rationale(
        title=title, event_type=event_type, asset=asset_u, stats=stats,
        direction=direction, direction_source=direction_source, edge=edge,
        edge_net=edge_net, cost_pct=cost_pct, sigma_horizon=sigma_horizon,
        confidence=confidence, severity=severity,
    )

    return {
        "asset": asset_u,
        "event_id": str(event_id or ""),
        "event_time": event_time,
        "event_title": title or "",
        "event_type": event_type,
        "severity": (severity or "").lower(),
        "affected_assets": affected or [asset_u],
        "magnitude_value": magnitude,
        "magnitude_unit": magnitude_unit,
        "direction": direction,
        "status": status,
        "direction_source": direction_source,
        "edge_pct": round(edge, 4),
        "edge_pct_net": round(edge_net, 4),
        "edge_measure": EDGE_MEASURE,
        "edge_measure_note": EDGE_MEASURE_NOTE,
        "confidence": confidence,
        "rationale": rationale,
        "reason": reason,
        "evidence": {
            "samples": stats["samples"],
            "same_type_samples": stats.get("same_type_samples", 0),
            "scope": stats.get("scope", "none"),
            "hit_rate": stats.get("hit_rate", 0.0),
            "mean_move_pct": stats.get("mean_move_pct", 0.0),
            "mean_abs_pct": stats.get("mean_abs_pct", 0.0),
            "up": stats.get("up", 0),
            "down": stats.get("down", 0),
            "sigma_daily_pct": round(sigma_daily, 4),
            "sigma_horizon_pct": round(sigma_horizon, 4),
            "sigma_available": bool(sigma_horizon > 0),
            "signal_to_noise": round(snr, 3),
            "lookback_days": int(lookback_days),
            "point_in_time": True,
        },
        "timing": timing,
        "risk": {
            "stop_loss_pct": stop,
            "target_pct": target,
            "position_pct_cap": position_pct,
            "risk_budget_pct": float(risk_budget_pct),
            "max_loss_pct": max_loss_pct,
            "note": (f"单笔最多亏 {max_loss_pct:.2f}% 权益 (预算 {risk_budget_pct:.2f}%), "
                     f"仓位上限 {position_pct:.2f}% 权益"),
        },
        "cost": policy,
    }


def _rationale(*, title: str, event_type: str, asset: str, stats: Dict[str, Any],
               direction: str, direction_source: str, edge: float, edge_net: float,
               cost_pct: float, sigma_horizon: float, confidence: float,
               severity: str) -> str:
    """人话依据 (中文) —— 前端直接展示。"""
    if direction == "NO_TRADE":
        return f"事件『{title or '未知'}』历史证据不足或优势被成本吃掉, 不做。"
    if direction_source.endswith("same_type"):
        src = "同类历史事件的条件统计"
    elif direction_source.endswith("asset"):
        src = "该标的全部历史事件的统计"
    else:
        src = "事件类型规则先验 (历史样本不足)"
    arrow = "看涨" if direction == "LONG" else "看跌"
    return (
        f"{title or '事件'}"
        + (f" [{event_type}]" if event_type else "")
        + (f" ({severity})" if severity else "")
        + f": {src}给出{arrow}方向 —— 期望冲击 {edge:+.2f}% (4h 最大偏移口径, 上界), "
        f"扣往返成本 {cost_pct:.2f}% 后仍剩 {edge_net:+.2f}%; "
        f"样本 {stats.get('samples', 0)} 条, 同向胜率 {float(stats.get('hit_rate', 0)) * 100:.0f}%, "
        f"持有期噪声 σ≈{sigma_horizon:.2f}%, 置信度 {confidence:.2f}"
    )


def list_opportunities(conn: sqlite3.Connection, *, asset: str = "",
                        from_ts: int = 0, to_ts: Optional[int] = None,
                        limit: int = 20, horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
                        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                        entry_mode: str = "auto",
                        min_abs_edge_pct: float = 0.0) -> Dict[str, Any]:
    """把最近的事件逐个转成机会卡 (每个都用**它自己那个时点**的信息)。

    数据源: event_kline_impact (事件标题/时间/重要度) —— 只用事件**当时已知**的字段,
    不用它自己的事后涨跌 (那是未来信息)。
    """
    now = int(to_ts) if to_ts else int(time.time())
    sql = ("SELECT id, event_id, event_title, event_time, asset, impact_severity "
           "FROM event_kline_impact WHERE event_time>=? AND event_time<=?")
    params: List[Any] = [int(from_ts), now]
    if asset:
        sql += " AND UPPER(asset)=?"
        params.append(asset.strip().upper())
    sql += " ORDER BY event_time DESC LIMIT ?"
    params.append(int(limit) * 4)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        rows = []

    items: List[Dict[str, Any]] = []
    for r in rows:
        op = build_opportunity(
            conn,
            asset=r[4], event_time=int(r[3] or 0), title=r[2] or "",
            severity=r[5] or "", event_id=r[1] or str(r[0]),
            horizon_minutes=horizon_minutes, lookback_days=lookback_days,
            entry_mode=entry_mode,
        )
        if min_abs_edge_pct and abs(op["edge_pct"]) < float(min_abs_edge_pct):
            continue
        items.append(op)
        if len(items) >= int(limit):
            break

    ready = [o for o in items if o["status"] == "READY"]
    return {
        "ok": True,
        "n": len(items),
        "ready": len(ready),
        "items": items,
        "source": "event_kline_impact",
        "point_in_time": True,
        "params": {
            "asset": asset.upper() if asset else "",
            "from_ts": int(from_ts),
            "to_ts": now,
            "limit": int(limit),
            "horizon_minutes": int(horizon_minutes),
            "lookback_days": int(lookback_days),
            "entry_mode": entry_mode,
        },
        "honest_note": ("每个机会卡只用该事件时点之前的数据 (同类事件条件统计 + 事前波动率), "
                        "不含任何事后涨跌; 证据不足或扣完成本没优势 → NO_TRADE; "
                        "edge_pct 是 4h 最大偏移口径 = 可实现收益的上界, 不是预期到手收益"),
    }


# ── v318：下次交易点日历 (前瞻) ─────────────────────────────────────────────

def _point_from_card(card: Dict[str, Any], *, kind: str, event_time: int,
                     title: str, impact: str = "") -> Dict[str, Any]:
    """机会卡 → 日历条目（扁平化：前端直接排期用）。"""
    timing = card.get("timing") or {}
    cost = card.get("cost") or {}
    return {
        "kind": kind,
        "asset": card.get("asset", ""),
        "event_time": int(event_time),
        "event_title": title or "",
        "event_type": card.get("event_type", ""),
        "impact": (impact or "").lower(),
        # 交易点：最早可进场 ~ 进场窗口截止
        "entry_not_before_ts": int(timing.get("entry_not_before_ts") or event_time),
        "entry_deadline_ts": int(timing.get("entry_deadline_ts") or event_time),
        "direction": card.get("direction", "NO_TRADE"),
        "status": card.get("status", "NO_TRADE"),
        "direction_source": card.get("direction_source", "none"),
        "edge_pct": card.get("edge_pct", 0.0),
        "edge_pct_net": card.get("edge_pct_net", 0.0),
        "edge_measure": card.get("edge_measure", EDGE_MEASURE),
        "confidence": card.get("confidence", 0.0),
        # T+0 / T+1 与滑点 —— 日历的核心口径
        "t_plus": cost.get("t_plus", ""),
        "earliest_exit_ts": int(timing.get("earliest_exit_ts") or 0),
        "cost": cost,
        "timing": timing,
        "risk": card.get("risk") or {},
        "reason": card.get("reason", ""),
        "rationale": card.get("rationale", ""),
    }


def list_trade_points(conn: sqlite3.Connection, *,
                      as_of_ts: Optional[int] = None,
                      horizon_hours: int = 72,
                      action_window_minutes: int = 60,
                      asset: str = "",
                      limit: int = 50,
                      horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
                      lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                      entry_mode: str = "auto",
                      min_samples: int = MIN_SAMPLES) -> Dict[str, Any]:
    """**下次交易点日历**（前瞻）: 未来可执行 / 待准备的交易时点。

    两类来源:
      · upcoming  —— 日历里**已排期但还没发生**的事件 (macro_calendar):
        提前准备（方向 / 成本门槛 / 最早可平仓时点都在事件落地前就定好）;
      · actionable —— 刚发生、**入场窗口还开着**的事件 (event_kline_impact):
        现在就能进（窗口内未确认则放弃）;
      · expired    —— 入场窗口已过的同类事件（只留档，不再算可执行点）。

    每个点都带: 方向 / 期望冲击 / 扣完成本后的净优势 / T+0-T+1 交收 /
    滑点与手续费 / 止损目标 / 为什么不做。

    口径: 所有统计一律只用 `as_of_ts`(=现在) **之前**的数据 —— 未来的事件
    也必须用"今天能看到的历史"来评估，不允许把事件之后的行情算进来。
    """
    now = int(as_of_ts) if as_of_ts else int(time.time())
    horizon_end = now + int(horizon_hours) * 3600
    action_start = now - int(action_window_minutes) * 60
    asset_u = (asset or "").strip().upper()
    points: List[Dict[str, Any]] = []
    sources = {"macro_calendar": 0, "event_kline_impact": 0, "skipped_no_asset": 0}

    # ── A. 已排期但还没发生的事件 ──
    try:
        rows = conn.execute(
            """SELECT event_id, title, impact, country, event_time FROM macro_calendar
               WHERE event_time > ? AND event_time <= ?
               ORDER BY event_time ASC LIMIT ?""",
            (now, horizon_end, max(1, int(limit) * 4)),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        ets = int(r["event_time"] or 0)
        title = str(r["title"] or "")
        _etype, affected, _m, _u = _classify(title)
        targets = [str(a).strip().upper() for a in (affected or [])]
        if asset_u:
            targets = [asset_u] if asset_u in targets else []
        if not targets:
            # 规则抽不出受影响标的 → 诚实跳过，不瞎猜一个品种
            sources["skipped_no_asset"] += 1
            continue
        for tgt in targets[:3]:
            card = build_opportunity(
                conn, asset=tgt, event_time=ets, title=title,
                severity=str(r["impact"] or ""), event_id=str(r["event_id"] or ""),
                horizon_minutes=horizon_minutes, lookback_days=lookback_days,
                entry_mode=entry_mode, min_samples=min_samples, as_of_ts=now,
            )
            points.append(_point_from_card(
                card, kind="upcoming", event_time=ets, title=title,
                impact=str(r["impact"] or ""),
            ))
        sources["macro_calendar"] += 1

    # ── B. 刚发生、入场窗口还开着的事件 ──
    try:
        rows = conn.execute(
            """SELECT id, event_id, event_title, event_time, asset, impact_severity
               FROM event_kline_impact
               WHERE event_time > ? AND event_time <= ?
               ORDER BY event_time DESC LIMIT ?""",
            (action_start, now, max(1, int(limit) * 4)),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        ets = int(r["event_time"] or 0)
        tgt = str(r["asset"] or "").strip().upper()
        if not ets or not tgt:
            continue
        if asset_u and tgt != asset_u:
            continue
        card = build_opportunity(
            conn, asset=tgt, event_time=ets, title=str(r["event_title"] or ""),
            severity=str(r["impact_severity"] or ""),
            event_id=str(r["event_id"] or r["id"]),
            horizon_minutes=horizon_minutes, lookback_days=lookback_days,
            entry_mode=entry_mode, min_samples=min_samples, as_of_ts=now,
        )
        point = _point_from_card(
            card, kind="actionable", event_time=ets,
            title=str(r["event_title"] or ""), impact=str(r["impact_severity"] or ""),
        )
        if point["entry_deadline_ts"] < now:
            point["kind"] = "expired"
        points.append(point)
        sources["event_kline_impact"] += 1

    points.sort(key=lambda p: (p["entry_not_before_ts"], p["asset"]))
    live = [p for p in points if p["kind"] != "expired"]
    ready = [p for p in live if p["status"] == "READY"]
    return {
        "ok": True,
        "as_of_ts": now,
        "n": len(points),
        "live": len(live),
        "ready": len(ready),
        "points": points[: int(limit)],
        "sources": sources,
        "params": {
            "as_of_ts": now,
            "horizon_hours": int(horizon_hours),
            "action_window_minutes": int(action_window_minutes),
            "asset": asset_u,
            "limit": int(limit),
            "horizon_minutes": int(horizon_minutes),
            "lookback_days": int(lookback_days),
            "entry_mode": entry_mode,
        },
        "honest_note": (
            "每个交易点只用 as_of_ts(=现在)之前的数据评估 (同类历史事件条件统计 + 事前波动率), "
            "未来的事件也只用今天能看到的历史; 交收口径 T+0/T+1 与滑点/手续费来自 cost_model; "
            "证据不足或扣完成本没优势 → NO_TRADE, 不硬给方向"
        ),
    }

