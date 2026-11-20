#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v409.7 决策级归因：不等成交，用「决策之后的真实价格走向」验证经验规则。

为什么要有
----------
`insight_verifier` 验的是 `quick_sim_trades`。而全库累计只有 7 笔成交，任何洞察样本 ≤7，
`verified` 在数学上不可达（见 CHANGELOG v409.6）。于是换口径：

  **门卫拦下了什么、放行了什么，后续价格替我们结账。**

做法（可复现、纯只读数据 + 最后一步写 verification_*）
-----------------------------------------------------
对一条「买/卖门槛」类洞察（`score.*` 或文本里带方向的经验）：
  1. 圈定样本：同资产（洞察 asset 为空则全资产）、决策时刻 ∈ [洞察创建, now-24h]；
  2. 每条决策算 24h 后的方向调整收益 `ret`（BUY 取 +涨，SELL 取 +跌）；
  3. 按门卫结果分两组：
     · 放行组 allowed = `suggested_action == 方向`
     · 拦截组 blocked = `ungated_action == 方向` 且 `suggested_action == 'HOLD'`
  4. 规则若成立，"拦截组"的平均收益应显著低于"放行组"：
        score = 0.5 + 0.5 * tanh( (allowed_mean - blocked_mean) / 0.005 )
  5. 结论：score ≥0.7 且两组各 ≥5 条 → verified；≤0.3 → failed；否则 insufficient_sample。

注意：本模块**不**把 HOLD/HOLD 的决策算进任何一组 —— 那不是门卫的功劳，是模型自己没方向。
"""
from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

HORIZON_HOURS = int(os.environ.get("INSIGHT_ATTR_HORIZON_H", "24") or 24)
MIN_GROUP_N = 5          # 两组各自的样本下限
STALE_TOLERANCE_HOURS = 3  # 取不到正好 24h 那根时允许的容差
SIGNAL_SCALE = 0.005     # 0.5% 的组间收益差 = 一个"满格"信号

# v453: 门卫方向由**参数键**决定，不是由 insight_direction 决定。
# insight_direction 是"调整方向"（提高/降低门槛），拿它当 BUY/SELL 会把
# "降低买入门槛"喂给卖出信号测试，产出假 verified。空串 = 门槛存在但两侧共用
# （min_event_strength 这类），可分资产、不可分方向，暂不测。
GATE_SIDE = {
    "score.buy_min_score": "BUY",
    "score.sell_min_score": "SELL",
    "filter.buy_min_score": "BUY",
    "filter.sell_min_score": "SELL",
    "filter.news_quality.partial_skipped": "",   # 纯过滤统计，无方向可测
    "min_event_strength": "",
    "signal_threshold": "",
}


def gate_side_for(param_key: str) -> str:
    """返回该参数键的门卫方向（BUY / SELL / 空=不可测）。"""
    return GATE_SIDE.get((param_key or "").strip(), "")


def normalize_direction(value: Any) -> str:
    """`insight_hypothesis` 把方向存成 1 / -1 / 0，旧的文本洞察可能是 BUY/SELL。

    注意：这是**参数调整方向**（提高=1 / 降低=-1），不是交易方向。把它直接当
    BUY/SELL 用会拿"降低买入门槛"去测卖出信号 —— 所以下面有 gate_side_for()。
    """
    if value is None:
        return ""
    s = str(value).strip().upper()
    if s in ("1", "1.0", "+1", "BUY", "LONG", "做多"):
        return "BUY"
    if s in ("-1", "-1.0", "SELL", "SHORT", "做空"):
        return "SELL"
    return ""


def _price_at(conn: sqlite3.Connection, symbol: str, ts_ms: int,
              tolerance_hours: int = 1) -> Optional[float]:
    """取 ts_ms 当根或之前最近一根 1h 收盘价（容差内）。"""
    tol_ms = tolerance_hours * 3600 * 1000
    row = conn.execute(
        "SELECT close FROM kline_bars WHERE symbol=? AND timeframe='1h'"
        " AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
        (symbol, ts_ms, ts_ms - tol_ms),
    ).fetchone()
    if row and row[0]:
        return float(row[0])
    return None


def _price_after(conn: sqlite3.Connection, symbol: str, ts_ms: int,
                 hours: int = HORIZON_HOURS) -> Optional[float]:
    """取 ts_ms + hours 之后最近一根 1h 收盘价（容差内）。"""
    target = ts_ms + hours * 3600 * 1000
    tol_ms = STALE_TOLERANCE_HOURS * 3600 * 1000
    row = conn.execute(
        "SELECT close FROM kline_bars WHERE symbol=? AND timeframe='1h'"
        " AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT 1",
        (symbol, target, target + tol_ms),
    ).fetchone()
    if row and row[0]:
        return float(row[0])
    return None


def _ts_ms(created_at: str) -> Optional[int]:
    """把 ai_decisions.created_at（带 +08:00 的 ISO）转 epoch 毫秒。"""
    if not created_at:
        return None
    s = created_at.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return None


def collect_decision_rows(conn: sqlite3.Connection, asset: str,
                          since_iso: str, until_iso: str) -> List[Dict[str, Any]]:
    """圈定候选决策（只取有方向的：放行或有向被拦）。"""
    where_asset = ""
    params: List[Any] = []
    if asset:
        where_asset = " AND d.target_asset = ?"
        params.append(asset)
    rows = conn.execute(
        "SELECT d.id AS id, d.created_at AS created_at, d.target_asset AS asset,"
        "       d.suggested_action AS action, d.ungated_action AS ungated"
        " FROM ai_decisions d"
        " WHERE d.created_at >= ? AND d.created_at <= ?"
        "   AND d.analysis_degraded = 0" + where_asset +
        " ORDER BY d.id",
        (since_iso, until_iso, *params),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r) if not isinstance(r, tuple) else {
            "id": r[0], "created_at": r[1], "asset": r[2], "action": r[3], "ungated": r[4]}
        act = (d.get("action") or "").upper()
        ung = (d.get("ungated") or "").upper()
        if act in ("BUY", "SELL") or ung in ("BUY", "SELL"):
            out.append(d)
    return out


def direction_adjusted_return(conn: sqlite3.Connection, symbol: str, ts_ms: int,
                              direction: str, hours: int = HORIZON_HOURS) -> Optional[float]:
    """方向调整后的 24h 收益率（BUY 取涨、SELL 取跌）。"""
    p0 = _price_at(conn, symbol, ts_ms)
    p1 = _price_after(conn, symbol, ts_ms, hours)
    if not p0 or not p1 or p0 <= 0:
        return None
    move = (p1 - p0) / p0
    return move if direction == "BUY" else -move


def score_split(allowed: List[float], blocked: List[float]) -> Dict[str, Any]:
    """纯函数：两组方向调整收益 → 证据分 + 结论。

    样本不足两组各 ≥MIN_GROUP_N 时，仍然计算并返回 score（**只作留档、可供观察样本积累**），
    但 verdict 固定 insufficient_sample —— 不拿小样本冒充结论。
    """
    n_a, n_b = len(allowed), len(blocked)
    if n_a == 0 or n_b == 0:
        return {"verdict": "insufficient_sample", "score": None,
                "n_allowed": n_a, "n_blocked": n_b,
                "reason": f"两组样本不足（放行 {n_a} / 拦截 {n_b}，各需 ≥{MIN_GROUP_N}）"}
    a = sum(allowed) / n_a
    b = sum(blocked) / n_b
    diff = a - b
    score = round(0.5 + 0.5 * math.tanh(diff / SIGNAL_SCALE), 3)
    enough = n_a >= MIN_GROUP_N and n_b >= MIN_GROUP_N
    if not enough:
        verdict = "insufficient_sample"
    elif score >= 0.7:
        verdict = "verified"
    elif score <= 0.3:
        verdict = "failed"
    else:
        verdict = "partial"
    return {"verdict": verdict, "score": score, "n_allowed": n_a, "n_blocked": n_b,
            "allowed_mean_pct": round(a * 100, 3), "blocked_mean_pct": round(b * 100, 3),
            "enough": enough,
            "reason": f"放行均值 {a*100:+.2f}% vs 拦截均值 {b*100:+.2f}%"
                      + ("" if enough else f"（样本不足，各需 ≥{MIN_GROUP_N}）")}


def attribute_insight(conn: sqlite3.Connection, insight: Dict[str, Any],
                      days_back: int = 7, now_iso: Optional[str] = None) -> Dict[str, Any]:
    """对单条洞察做决策级归因（不写库）。"""
    key = (insight.get("insight_param_key") or "").strip()
    # v453 防呆：只有门槛类（score.* / filter.*）能被门卫归因验证。
    # 仓位/止盈/叙事类（sizing.*）测不出"门卫放行质量"，硬套会产出假 verified。
    if key and not key.startswith(("score.", "filter.")):
        return {"verdict": "not_applicable", "score": None,
                "reason": f"{key} 是仓位/叙事类，不走门卫归因"}
    side = gate_side_for(key) if key else ""
    if key and key in GATE_SIDE and not side:
        return {"verdict": "not_applicable", "score": None,
                "reason": f"{key} 是两侧共用门槛，无方向可分"}
    # 有门卫方向就按键走；没有键才退回旧的 "调整方向" 映射（兼容老调用）
    direction = side or normalize_direction(insight.get("insight_direction"))
    if direction not in ("BUY", "SELL"):
        # 没有方向的（例如"降低仓位"）不走本口径
        return {"verdict": "not_applicable", "score": None, "reason": "洞察没有方向"}
    asset = (insight.get("asset") or "").upper()
    since = insight.get("created_at") or ""
    # 决策必须已经"结完账"：只取 24h 之前发生的
    now = now_iso or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = datetime.now().timestamp() - HORIZON_HOURS * 3600
    until_iso = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    floor_ts = datetime.now().timestamp() - days_back * 86400
    if since:
        try:
            floor_ts = max(floor_ts, datetime.fromisoformat(since).timestamp())
        except Exception:
            pass
    since_iso = datetime.fromtimestamp(floor_ts).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    rows = collect_decision_rows(conn, asset, since_iso, until_iso)
    allowed: List[float] = []
    blocked: List[float] = []
    used = 0
    for d in rows:
        ts = _ts_ms(d.get("created_at") or "")
        if not ts:
            continue
        symbol = (d.get("asset") or "").upper()
        if not symbol:
            continue
        act = (d.get("action") or "").upper()
        ung = (d.get("ungated") or "").upper()
        ret = direction_adjusted_return(conn, symbol, ts, direction)
        if ret is None:
            continue
        used += 1
        if act == direction:
            allowed.append(ret)
        elif ung == direction and act == "HOLD":
            blocked.append(ret)
    res = score_split(allowed, blocked)
    res.update({"used": used, "asset": asset or "*", "direction": direction,
                "since": since_iso, "until": until_iso})
    # v453: 把「建议方向 vs 证据方向」的对账放进模块自身。
    # 同一份证据，建议"提高门槛"还是"降低门槛"，结论正好相反 ——
    # 对账留在调用方（脚本）会让别的调用方读到相反结论。这里一次算清。
    advice = 0
    try:
        advice = int(float(insight.get("insight_direction") or 0))
    except (TypeError, ValueError):
        advice = 0
    if advice in (1, -1) and res.get("enough") and res.get("score") is not None:
        score = float(res["score"])
        if score >= 0.7:              # 拦截组更差 = 门卫拦对了
            res["verdict"] = "verified" if advice > 0 else "failed"
        elif score <= 0.3:            # 拦截组更好 = 门卫拦错了
            res["verdict"] = "verified" if advice < 0 else "failed"
        else:
            res["verdict"] = "partial"
        res["verdict_basis"] = "evidence+direction"
    return res
