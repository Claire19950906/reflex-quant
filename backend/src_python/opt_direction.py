# -*- coding: utf-8 -*-
"""opt_direction.py — v336 方向问题三件事

背景（实测 2026-11-11～11-13）:
    gate_evaluations 816 次评估, passed = 0, 拒因 no_direction 811 次。
    ai_decisions 11-12 的 546 条里 545 条 HOLD。
    也就是说 **no_direction 把所有问题糊成了一类**, 既分不清"模型真的判断无方向",
    也分不清"品种角色标错 / schema 坏了" —— 而这三种的修法完全不同。

本模块做三件只读的事:

1. **拆 no_direction** —— `classify_no_direction()` 把 HOLD 的理由文本分成
   model_hold_low_signal / chain_not_built / asset_role_mislabel / schema_empty / other。
   实测 545 条 HOLD 里绝大多数是前两类(模型保守), 另有一类是"BTC 被标成副品种"
   这种角色错标 —— 后者是 bug, 不是判断。

2. **方向确认闸回测** —— `prior_move_pct()` 用分钟线算"决策前 30 分钟有没有同向动",
   在 391 条影子样本上回测: 已同向动 vs 未同向动 的命中率差。
   这条规则的价值必须用样本证明, 不能凭感觉加。

3. **两段式预筛 / LLM 成本** —— `prefilter_worth_llm()` 用规则先滤掉
   "没有可交易品种 / 噪音 / 品种桶"的事件, 只把可能出方向的事件送贵模型。
   纯函数, 不调任何模型。

只读: 不写库, 不改交易行为。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"

CORE_ASSETS = ("BTC", "XAU", "WTI", "ETH", "SOL")
BUCKET_ASSETS = frozenset({
    "NONE", "", "UNKNOWN", "US_STOCK", "INDIVIDUAL_STOCK", "CRYPTO_MAJOR",
    "CN_STOCK", "ETF", "INDEX", "OTHER",
})

# 方向性词根 —— 只用于"这条事件有没有可能给出方向", 不用于判多空
DIRECTIONAL_CUES = (
    "上涨", "下跌", "涨", "跌", "突破", "跌破", "增持", "减持", "加息", "降息",
    "上调", "下调", "超预期", "不及预期", "流出", "流入", "减产", "增产",
    "制裁", "停火", "获批", "被拒", "裁员", "回购", "上调目标价", "下调目标价",
    "beat", "miss", "cut", "raise", "surge", "plunge", "outflow", "inflow",
    "upgrade", "dowgrade", "downgrade", "approve", "reject",
)

# 归因标签（输出给前端/统计用）
REASON_MODEL_HOLD = "model_hold_low_signal"
REASON_CHAIN = "chain_not_built"
REASON_ASSET_ROLE = "asset_role_mislabel"
REASON_SCHEMA = "schema_empty"
REASON_OTHER = "model_hold_other"


def classify_no_direction(reasoning: str, asset: str = "") -> str:
    """把一条 HOLD 的理由拆成可修的类别（顺序即优先级）。"""
    text = (reasoning or "").strip()
    if not text:
        return REASON_SCHEMA
    a = (asset or "").strip().upper()
    # "事件不直接命中本系统核心监控品种（BTC/XAU/WTI/ETH/SOL）" 但 asset 就是其中之一
    if "核心监控品种" in text and a in CORE_ASSETS:
        return REASON_ASSET_ROLE
    if "传导链" in text or "影响不明确" in text:
        return REASON_CHAIN
    if "不足以给出明确方向" in text or "观望" in text or "不构成交易信号" in text:
        return REASON_MODEL_HOLD
    if "按非交易信号处理" in text:
        return REASON_MODEL_HOLD
    return REASON_OTHER


def prefilter_worth_llm(row: Dict[str, Any]) -> Tuple[bool, str]:
    """两段式第一段（零成本规则筛）。返回 (是否值得送贵模型, 原因)。

    只在"明显不可能出方向"时拦；有方向线索的一律放行，避免把真信号筛掉。
    """
    asset = str(row.get("asset") or "").strip().upper()
    if asset in BUCKET_ASSETS:
        return False, "asset_bucket"
    if int(row.get("is_noise") or 0) == 1:
        return False, "noise"
    headline = str(row.get("headline") or row.get("title") or "").strip()
    if len(headline) < 8:
        return False, "headline_too_short"
    low = headline.lower()
    if not any(c in headline or c in low for c in DIRECTIONAL_CUES):
        return False, "no_directional_cue"
    return True, "ok"


# ---------------------------------------------------------------- 分钟线取价

def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _close_at(conn: sqlite3.Connection, asset: str, ts: float,
              tolerance_sec: int = 900) -> Optional[float]:
    """取 ts 之前（含）最近一根 1m 收盘价；超过容差视为无数据。"""
    row = conn.execute(
        "SELECT close, ts FROM ohlcv_intraday WHERE asset=? AND interval='1m' AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (asset, int(ts))).fetchone()
    if not row:
        return None
    if int(row["ts"]) < int(ts) - tolerance_sec:
        return None
    return float(row["close"])


def prior_move_pct(conn: sqlite3.Connection, asset: str, ts: float,
                   lookback_min: int = 30) -> Optional[float]:
    """决策前 lookback_min 分钟的涨跌幅（%）。无分钟线返回 None。"""
    now = _close_at(conn, asset, ts)
    before = _close_at(conn, asset, ts - lookback_min * 60)
    if not now or not before or before <= 0:
        return None
    return round((now - before) / before * 100, 3)


def backtest_confirm_gate(conn: sqlite3.Connection, lookback_min: int = 30) -> Dict[str, Any]:
    """在影子账本上回测"方向确认闸"：决策前已同向动 vs 未同向动 的命中率。"""
    cells = {"aligned": [0, 0], "opposed_or_flat": [0, 0], "no_data": [0, 0]}
    for r in conn.execute(
        "SELECT asset, direction, decided_ts, hit FROM shadow_ledger WHERE hit IS NOT NULL"
    ):
        d = str(r["direction"] or "").upper()
        if d not in ("BUY", "SELL"):
            continue
        mv = prior_move_pct(conn, r["asset"], r["decided_ts"], lookback_min)
        if mv is None:
            cells["no_data"][1] += 1
            continue
        same = (mv > 0) if d == "BUY" else (mv < 0)
        key = "aligned" if same else "opposed_or_flat"
        cells[key][1] += 1
        if int(r["hit"] or 0) == 1:
            cells[key][0] += 1

    def pct(c): return round(100.0 * c[0] / c[1], 1) if c[1] else None
    a, o = cells["aligned"], cells["opposed_or_flat"]
    lift = None
    if a[1] and o[1] and pct(a) is not None and pct(o) is not None:
        lift = round(pct(a) - pct(o), 1)
    return {
        "lookback_min": lookback_min,
        "aligned": {"hit": a[0], "n": a[1], "pct": pct(a)},
        "opposed_or_flat": {"hit": o[0], "n": o[1], "pct": pct(o)},
        "no_data": cells["no_data"][1],
        "lift_pp": lift,
    }


def no_direction_breakdown(conn: sqlite3.Connection, days: int = 14) -> Dict[str, Any]:
    """统计窗口内 HOLD 的细分原因 + 闸门拒绝分布。"""
    rows = conn.execute(
        "SELECT reasoning, target_asset FROM ai_decisions "
        "WHERE created_at >= datetime('now', ?) AND suggested_action='HOLD'",
        (f"-{days} day",)).fetchall()
    cnt = Counter(classify_no_direction(r["reasoning"], r["target_asset"]) for r in rows)
    gate = conn.execute(
        "SELECT reject_code, count(*) n FROM gate_evaluations "
        "WHERE evaluated_at >= strftime('%s','now', ?) GROUP BY reject_code",
        (f"-{days} day",)).fetchall()
    return {
        "days": days,
        "hold_total": len(rows),
        "reasons": dict(cnt.most_common()),
        "gate_reject_codes": {r["reject_code"]: r["n"] for r in gate},
    }


def llm_prefilter_savings(conn: sqlite3.Connection, days: int = 7) -> Dict[str, Any]:
    """第一段预筛能省掉多少贵模型调用。"""
    rows = conn.execute(
        "SELECT asset, is_noise, headline FROM news_events "
        "WHERE ts >= strftime('%s','now', ?)",
        (f"-{days} day",)).fetchall()
    keep, drop = 0, Counter()
    for r in rows:
        ok, why = prefilter_worth_llm(dict(r))
        if ok:
            keep += 1
        else:
            drop[why] += 1
    total = len(rows)
    return {
        "days": days, "total": total, "keep": keep, "drop": sum(drop.values()),
        "drop_reasons": dict(drop.most_common()),
        "saved_pct": round(100.0 * sum(drop.values()) / total, 1) if total else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = _ro()
    try:
        out = {
            "no_direction": no_direction_breakdown(conn, args.days),
            "confirm_gate": backtest_confirm_gate(conn),
            "prefilter": llm_prefilter_savings(conn, 7),
        }
    finally:
        conn.close()

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    nd = out["no_direction"]
    print(f"[1] no_direction 细分（近 {nd['days']} 天 HOLD {nd['hold_total']} 条）")
    for k, v in nd["reasons"].items():
        print(f"    {k:<24} {v:>5} 条  ({100.0*v/max(nd['hold_total'],1):.1f}%)")
    print(f"    闸门拒因: {nd['gate_reject_codes']}")
    print()
    cg = out["confirm_gate"]
    print(f"[2] 方向确认闸回测（决策前 {cg['lookback_min']} 分钟）")
    print(f"    已同向动     {cg['aligned']['pct']}%  (n={cg['aligned']['n']})")
    print(f"    未同向/反向  {cg['opposed_or_flat']['pct']}%  (n={cg['opposed_or_flat']['n']})")
    print(f"    无分钟线     {cg['no_data']} 条")
    print(f"    增量         {cg['lift_pp']} pp")
    print()
    pf = out["prefilter"]
    print(f"[3] 两段式预筛（近 {pf['days']} 天 {pf['total']} 条事件）")
    print(f"    可省贵模型调用 {pf['saved_pct']}%  保留 {pf['keep']} 条")
    for k, v in pf["drop_reasons"].items():
        print(f"      - {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
