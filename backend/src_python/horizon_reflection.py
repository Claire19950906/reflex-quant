#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""horizon_reflection — 把「方向错」与「节奏错」拆开（v342 第二件）。

问题
----
现有反思只看 5 天结算：一条 SELL 决策亏了，一律记「判断错」。
但两种错的处理方式完全不同：

    方向错 → 逻辑/证据错了，要改推理
    节奏错 → 方向对、入场时点错（追高、太早），要改执行，不是改判断

v342 已经把同一条决策在 15m/1h/4h/1d/5d 上分别结算（`decision_outcomes_horizon`），
本模块就是它的判定层：读多个周期的读数，给出**是哪一类错**。

口径（宁缺不猜）
----------------
- 只用 `status='settled'` 的行判好坏；`no_price` 不当亏损，`not_executable`
  单独记「周期选错」而不是「判断错」
- 长期窗口优先：5d 结算过就用 5d 判方向；5d 没结算才退到 1d，并标 partial
- 短期亏损 / 长期盈利 = 节奏错（不是方向错）；这条是本模块存在的理由
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

HORIZONS: tuple = ("15m", "1h", "4h", "1d", "5d")
SHORT_HORIZONS: tuple = ("15m", "1h", "4h")
LONG_HORIZONS: tuple = ("5d", "1d")          # 判「方向」的优先级：5d 先

# 判定阈值（经验值；样本长起来后应随成本分布校准）
SHORT_LOSS_PCT = -0.5     # 短期净收益 <= 此值 → 算「短期亏」
LONG_WIN_PCT = 0.5        # 长期净收益 >= 此值 → 算「方向对」
LONG_LOSS_PCT = -0.5      # 长期净收益 <= 此值 → 算「方向错」
COST_DRAG_WIN_PCT = 0.2   # 各周期原始涨幅都为正、但净收益 < 此值 → 成本侵蚀

VERDICT_DIRECTION = "direction_wrong"      # 长周期也亏：方向/逻辑错
VERDICT_TIMING = "timing_wrong"            # 短期亏 + 长期赚：节奏错
VERDICT_COST = "cost_drag"                 # 方向对但净收益被成本吃掉
VERDICT_HORIZON = "horizon_mismatch"       # 该周期对这类品种不可执行
VERDICT_NODATA = "no_data"                 # 取不到价，不下结论
VERDICT_OK = "ok"                          # 长周期判赢且短期不亏
VERDICT_PARTIAL = "partial"                # 长周期还没结算，只能看短期

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS decision_horizon_reflection (
    decision_id   INTEGER PRIMARY KEY,
    asset         TEXT,
    action        TEXT,
    verdict       TEXT NOT NULL,          -- direction_wrong / timing_wrong / cost_drag / ...
    codes_json    TEXT,                   -- ["H-DIR","H-TIME"] 这类码，便于聚合
    detail_json   TEXT,                   -- 各周期读数 + 触发依据（可回溯）
    horizons_used TEXT,                   -- 参与判定的周期，逗号分隔
    computed_at   INTEGER
)
"""

CODES = {
    VERDICT_DIRECTION: "H-DIR",    # 方向错
    VERDICT_TIMING: "H-TIME",      # 节奏错
    VERDICT_COST: "H-COST",        # 成本侵蚀
    VERDICT_HORIZON: "H-EXEC",     # 周期不可执行
    VERDICT_NODATA: "H-NODATA",    # 数据缺失
    VERDICT_OK: "H-OK",
    VERDICT_PARTIAL: "H-PARTIAL",
}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_SQL)


def _by_horizon(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        h = r["horizon"] if not isinstance(r, dict) else r.get("horizon")
        if not h:
            continue
        out[str(h)] = dict(r) if not isinstance(r, dict) else r
    return out


def classify(horizons: List[Dict[str, Any]]) -> Dict[str, Any]:
    """输入是多周期读数（read_horizons 的返回），输出「错在哪一类」。

    返回 {verdict, code, why, long_horizon, detail}
    """
    hz = _by_horizon(horizons)
    detail: Dict[str, Any] = {
        h: {"status": v.get("status"), "pnl_pct": v.get("pnl_pct"),
            "outcome_pct": v.get("outcome_pct"), "hit": v.get("hit")}
        for h, v in hz.items()
    }

    # 1) 周期不可执行：T+1 品种在 1d/5d 都还平不了仓 → 这是「周期选错」，
    #    不是「判断错」（判错会让反思去改本来正确的推理）
    if any((hz.get(h) or {}).get("status") == "not_executable"
           for h in ("1d", "5d")):
        return {"verdict": VERDICT_HORIZON, "code": CODES[VERDICT_HORIZON],
                "why": "长周期对该品种不可执行（T+1 / 结算窗口太短），不能据此改判断",
                "long_horizon": None, "detail": detail}

    # 2) 长期窗口：5d 优先，退 1d
    long_h: Optional[str] = None
    for h in LONG_HORIZONS:
        if (hz.get(h) or {}).get("status") == "settled":
            long_h = h
            break

    if long_h is None:
        settled_short = [h for h in SHORT_HORIZONS
                         if (hz.get(h) or {}).get("status") == "settled"]
        if not settled_short:
            return {"verdict": VERDICT_NODATA, "code": CODES[VERDICT_NODATA],
                    "why": "没有任何可用周期的价格读数（no_price），不下结论",
                    "long_horizon": None, "detail": detail}
        # 短期能看但长周期没有 → partial（宁可说"没结论"也不要下错结论）
        return {"verdict": VERDICT_PARTIAL, "code": CODES[VERDICT_PARTIAL],
                "why": f"长周期未结算，仅 {','.join(settled_short)} 可用",
                "long_horizon": None, "detail": detail}

    long_pnl = float((hz[long_h] or {}).get("pnl_pct") or 0.0)
    short_settled = {h: (hz.get(h) or {}) for h in SHORT_HORIZONS
                     if (hz.get(h) or {}).get("status") == "settled"}
    worst_short = min((float(v.get("pnl_pct") or 0.0) for v in short_settled.values()),
                      default=None)

    # 3) 方向错：长期也亏
    if long_pnl <= LONG_LOSS_PCT:
        return {"verdict": VERDICT_DIRECTION, "code": CODES[VERDICT_DIRECTION],
                "why": f"{long_h} 净收益 {long_pnl:+.2f}% ≤ {LONG_LOSS_PCT}%，长期判错方向",
                "long_horizon": long_h, "detail": detail}

    # 4) 节奏错：短期亏、长期赚（本模块的核心拆分）
    if worst_short is not None and worst_short <= SHORT_LOSS_PCT \
            and long_pnl >= LONG_WIN_PCT:
        return {"verdict": VERDICT_TIMING, "code": CODES[VERDICT_TIMING],
                "why": (f"短期最差 {worst_short:+.2f}% 但 {long_h} {long_pnl:+.2f}%，"
                        "方向对、入场时点错"),
                "long_horizon": long_h, "detail": detail}

    # 5) 成本侵蚀：各周期原始涨跌为正，净收益却接近 0
    raw_pos = [float((hz.get(h) or {}).get("outcome_pct") or 0.0)
               for h, v in hz.items() if v.get("status") == "settled"]
    if raw_pos and all(x > 0 for x in raw_pos) and long_pnl < COST_DRAG_WIN_PCT:
        return {"verdict": VERDICT_COST, "code": CODES[VERDICT_COST],
                "why": f"原始涨跌全为正但 {long_h} 净收益仅 {long_pnl:+.2f}%，成本吃掉利润",
                "long_horizon": long_h, "detail": detail}

    if long_pnl > 0:
        return {"verdict": VERDICT_OK, "code": CODES[VERDICT_OK],
                "why": f"{long_h} 净收益 {long_pnl:+.2f}%，未发现方向或节奏问题",
                "long_horizon": long_h, "detail": detail}

    # 长期为负但没到阈值（-0.5 ~ 0）：不硬判，留 partial
    return {"verdict": VERDICT_PARTIAL, "code": CODES[VERDICT_PARTIAL],
            "why": f"{long_h} 净收益 {long_pnl:+.2f}% 在无结论区间（{LONG_LOSS_PCT} ~ 0）",
            "long_horizon": long_h, "detail": detail}


def run_once(conn: sqlite3.Connection, limit: int = 500,
             commit: bool = True) -> Dict[str, Any]:
    """扫有读数的决策，写判定（幂等：同一 decision_id 覆盖写）。"""
    init_schema(conn)
    ids = conn.execute(
        "SELECT DISTINCT decision_id FROM decision_outcomes_horizon "
        "ORDER BY decision_id DESC LIMIT ?", (int(limit),)).fetchall()
    summary = {"decisions": 0, "by_verdict": {}}
    for row in ids:
        did = int(row[0])
        hz = conn.execute(
            "SELECT horizon, horizon_sec, status, outcome_pct, pnl_pct, hit "
            "FROM decision_outcomes_horizon WHERE decision_id=?", (did,)).fetchall()
        rows = [{"horizon": r[0], "horizon_sec": r[1], "status": r[2],
                 "outcome_pct": r[3], "pnl_pct": r[4], "hit": r[5]} for r in hz]
        res = classify(rows)
        head = conn.execute(
            "SELECT asset, final_action FROM trade_decisions WHERE id=?",
            (did,)).fetchone()
        asset = head[0] if head else None
        action = head[1] if head else None
        used = ",".join(sorted((r["horizon"] for r in rows),
                               key=lambda h: HORIZONS.index(h) if h in HORIZONS else 99))
        conn.execute(
            "INSERT OR REPLACE INTO decision_horizon_reflection "
            "(decision_id, asset, action, verdict, codes_json, detail_json, "
            " horizons_used, computed_at) VALUES (?,?,?,?,?,?,?,?)",
            (did, asset, action, res["verdict"], json.dumps([res["code"]]),
             json.dumps({"why": res["why"], "long_horizon": res["long_horizon"],
                         "horizons": res["detail"]}, ensure_ascii=False),
             used, int(time.time())))
        summary["decisions"] += 1
        summary["by_verdict"][res["verdict"]] = \
            summary["by_verdict"].get(res["verdict"], 0) + 1
    if commit:
        conn.commit()
    return summary


def read_stats(conn: sqlite3.Connection, days: int = 30,
               asset: Optional[str] = None) -> Dict[str, Any]:
    """按判定类型聚合：方向错 vs 节奏错 各占多少。"""
    cutoff = int(time.time()) - int(days) * 86400
    sql = ("SELECT verdict, COUNT(*) AS n FROM decision_horizon_reflection "
           "WHERE computed_at >= ?")
    params: list = [cutoff]
    if asset:
        sql += " AND UPPER(asset)=?"
        params.append(asset.strip().upper())
    sql += " GROUP BY verdict ORDER BY n DESC"
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return {"ok": True, "days": days, "asset": asset, "by_verdict": {},
                "note": "尚未跑过 horizon 判定"}
    total = sum(int(r[1] or 0) for r in rows)
    return {"ok": True, "days": days, "asset": asset,
            "total": total,
            "by_verdict": {r[0]: int(r[1]) for r in rows},
            "direction_vs_timing": {
                "direction_wrong": next((int(r[1]) for r in rows if r[0] == VERDICT_DIRECTION), 0),
                "timing_wrong": next((int(r[1]) for r in rows if r[0] == VERDICT_TIMING), 0),
            }}
