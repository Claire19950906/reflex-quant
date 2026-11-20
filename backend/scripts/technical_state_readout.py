#!/usr/bin/env python3
"""technical_state_readout.py —— 把技术指标跑成一句可读的市场状态（CLI）。

计算与取数全部在 `src_python/technical_state.py` 中（快照/闸门/worker 共用同一份），
这里只负责命令行参数与人读排版。

用法
----
    python3 scripts/technical_state_readout.py
    python3 scripts/technical_state_readout.py --asset ETH --timeframe 1h
    python3 scripts/technical_state_readout.py --asset XAU --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import technical_state as ts

DB_PATH = BACKEND / "trident_event_bus.db"


def render(state: Dict[str, Any]) -> str:
    if "error" in state:
        return f"[不计算] {state['error']}"
    m = state["_meta"]
    bd = state.get("bias_detail") or {}
    lines = [
        f"== {m['asset']} {m['timeframe']}（{m['bars']} 根）==",
        "",
        "读数：" + (state["readout"] or "（无可用维度）"),
        "",
        f"趋势    {state['trend']['stage']} | ADX {state['trend']['adx']}"
        f"（5 根变化 {state['trend']['adx_delta_5b']}）| +DI/-DI {state['trend']['plus_di']}/{state['trend']['minus_di']}",
        f"       结构 {state['trend']['structure']}（{state['trend']['trend_dir']}）"
        f"| 均线斜率 {state['trend']['ma_slope_pct_per_bar']}%/根"
        f"| Donchian 位置 {state['trend']['donchian_position']}"
        f"| RSI {state['trend']['rsi']}（极值驻留 {state['trend']['rsi_stretch_bars']} 根）",
        f"波动    {state['volatility']['state']} | 带宽 {state['volatility']['bb_width_pct']}%"
        f"（{state['volatility']['bb_width_percentile']} 分位）"
        f"| 已实现波动 {state['volatility']['realized_vol_pct']}%"
        f"（{state['volatility']['realized_vol_percentile']} 分位）"
        f"| ATR {state['volatility']['atr_pct']}%",
        f"价值区  {state['value_area']['readout'] or '样本不足'}"
        f" | POC {state['value_area']['poc']} / VAH {state['value_area']['vah']}"
        f" / VAL {state['value_area']['val']}",
        f"杠杆    {state['leverage']['label'] or '无 OI 数据'}"
        f"（价 {state['leverage']['price_change_pct']}%"
        f" / OI {state['leverage']['oi_change_pct']}%，窗口 {m.get('oi_window') or '—'}）"
        f" | 资金费率 {state['leverage']['funding_rate']}"
        f"（{state['leverage']['funding_percentile']} 分位，{state['leverage']['crowding']}）",
        f"主动性  {state['flow']['taker']['label'] or '无主动成交数据'}"
        f" | CVD 背离 {state['flow']['cvd_divergence'] or '未触发'}",
        f"持仓    {state['positioning']['label'] or '无持仓分歧数据'}"
        f" | 大户 {state['positioning']['top_long_short_ratio']}"
        f" vs 全体 {state['positioning']['retail_long_short_ratio']}",
    ]
    if bd.get("bias") is not None:
        lines.append(f"偏置    {bd['bias']:+.2f}（覆盖 {bd['coverage']:.0%}）"
                     f" 分量 {bd['components']}")
    if state["missing"]:
        lines += ["", "缺数据维度：" + "、".join(state["missing"]) + "（这些维度未参与读数，不做推测）"]
    risk = state.get("risk") or {}
    if risk.get("available"):
        lines.append(f"风控    {risk['note']}")
        if risk.get("drivers"):
            lines.append(f"        触发 {'、'.join(risk['drivers'])}")
        # v417/v418/v419：执行口径（开多大 / 要赚多少够本 / 要不要拿久）
        if risk.get("position_pct") is not None:
            lines.append(f"        敞口    止损 {risk.get('stop_pct')}%，按单笔亏 "
                         f"{risk.get('max_risk_pct')}% 本金反推 → 名义敞口约 {risk['position_pct']}%"
                         + (f"（等风险口径 {risk['position_pct_equal_risk']}%）"
                            if risk.get("position_pct_equal_risk") is not None else ""))
        if risk.get("require_space_pct") is not None:
            basis = {"cost": "成本地板", "adverse": "逆风线", "odds": "赔率线"}.get(
                risk.get("require_basis"), "")
            lines.append(f"        赔率    该周期实测 {risk.get('odds_typical') or '未测'}，"
                         f"预期空间需 ≥{risk['require_space_pct']}% 才够本"
                         + (f"（{basis}；来回成本 {risk.get('round_trip_cost_pct')}%）" if basis else ""))
        if risk.get("holding_note"):
            lines.append(f"        持有    {risk['holding_note']}")
    extra = state.get("_trend_detail") or {}
    if extra:
        lines.append(f"补充：Aroon {extra['aroon_up']}/{extra['aroon_down']}"
                     f" | Supertrend 方向 {extra['supertrend_dir']}"
                     f"（轨 {extra['supertrend_line']}）")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="技术指标 → 市场状态读数")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="4h", choices=sorted(ts._TF_SECONDS))
    ap.add_argument("--bars", type=int, default=240)
    ap.add_argument("--json", action="store_true", help="输出完整 JSON 而非人读格式")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        state = ts.build_state(conn, args.asset.upper(), args.timeframe, args.bars)
    finally:
        conn.close()
    if "error" not in state:
        state["bias_detail"] = ts.bias_of(state)
    print(json.dumps(state, ensure_ascii=False, indent=2) if args.json else render(state))
    return 0 if "error" not in state else 1


if __name__ == "__main__":
    raise SystemExit(main())
