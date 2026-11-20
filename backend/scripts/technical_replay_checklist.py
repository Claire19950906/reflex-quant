#!/usr/bin/env python3
"""technical_replay_checklist.py —— L3 人工回放验收（只读）。

L1（单测）证明"算得对"，L2（前向收益）证明"有没有统计意义"，但这两种都验不了
最要紧的一件事：**这句读数当时人看着对不对**。

这个脚本抽 N 个历史时点，用**严格因果**的方式（只用该时点之前的数据）重算当时
的状态读数，再附上之后 1/3/5 日的实际走势（涨跌幅 + 期间最高/最低），
生成一份可以逐条打勾的清单：

    ┌ 时点 / 当时读数 / 之后走势 / 机械判定 / 你的判断（留空给你填）

机械判定只是"偏置方向 vs 5 日涨跌方向"的符号比较，用来做粗筛；
真正的验收要你自己读那句读数，判断"当时这么描述，合理吗"。

用法
----
    python3 scripts/technical_replay_checklist.py                  # BTC 4h，30 个时点
    python3 scripts/technical_replay_checklist.py --asset ETH --timeframe 1h --points 20
    python3 scripts/technical_replay_checklist.py --no-write        # 只打印
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import technical_state as ts

DB_PATH = BACKEND / "trident_event_bus.db"
OUT_DIR = BACKEND / "reports" / "technical_replay"
CST = timezone(timedelta(hours=8))

# 回放需要的窗口：左边 240 根算指标，右边 30 根(5 日 4h)算事后走势
LOOKBACK_BARS = 240
HORIZONS_DAYS = (1, 3, 5)


def _fmt_ts(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, CST).strftime("%Y-%m-%d %H:%M")


def _bars_per_day(timeframe: str) -> int:
    return max(1, int(86400 // ts._TF_SECONDS.get(timeframe, 14400)))


def build_rows(conn: sqlite3.Connection, asset: str, timeframe: str,
               points: int) -> List[Dict[str, Any]]:
    candles = ts.load_candles(conn, asset, timeframe, 100_000)
    bpd = _bars_per_day(timeframe)
    need_future = max(HORIZONS_DAYS) * bpd
    if len(candles) < LOOKBACK_BARS + need_future + 5:
        return []
    start = LOOKBACK_BARS
    end = len(candles) - need_future - 1
    if end <= start:
        return []
    step = max(1, (end - start) // max(1, points - 1))
    idxs = list(range(start, end + 1, step))[:points]

    rows: List[Dict[str, Any]] = []
    for i in idxs:
        anchor_ts = candles[i][0]
        state = ts.build_state(conn, asset, timeframe, LOOKBACK_BARS, asof_ts=anchor_ts)
        row: Dict[str, Any] = {
            "ts": anchor_ts,
            "time": _fmt_ts(anchor_ts),
            "close": candles[i][4],
            "readout": state.get("readout") or "",
            "error": state.get("error"),
            "missing": state.get("missing") or [],
            "bias": None, "coverage": None, "usable": False,
            "fwd": {},
            "mechanical": "无法判定",
            "label_check": "无法判定",
            "stage": None, "direction": None, 
        }
        if "error" not in state:
            bd = ts.bias_of(state)
            row["bias"] = bd.get("bias")
            row["coverage"] = bd.get("coverage")
            row["usable"] = bool(bd.get("usable"))
            tr = state.get("trend") or {}
            row["stage"] = tr.get("stage")
            row["direction"] = tr.get("direction")
            for days in HORIZONS_DAYS:
                j = i + days * bpd
                if j >= len(candles):
                    continue
                ret = (candles[j][4] - candles[i][4]) / candles[i][4] * 100.0
                window = candles[i:j + 1]
                row["fwd"][days] = {
                    "ret": round(ret, 2),
                    "high": round((max(c[2] for c in window) - candles[i][4]) / candles[i][4] * 100.0, 2),
                    "low": round((min(c[3] for c in window) - candles[i][4]) / candles[i][4] * 100.0, 2),
                }
            five = row["fwd"].get(5)
            if row["usable"] and five:
                if row["bias"] < 0 and five["ret"] < 0:
                    row["mechanical"] = "命中（偏空→跌）"
                elif row["bias"] > 0 and five["ret"] > 0:
                    row["mechanical"] = "命中（偏多→涨）"
                elif abs(five["ret"]) < 0.5:
                    row["mechanical"] = "走平（无法区分）"
                else:
                    row["mechanical"] = "未命中"
            elif not row["usable"]:
                row["mechanical"] = "偏置不可用（覆盖不足）"

            # 文字标签核验：不依赖偏置，只看"这句话描述的行情有没有兑现"。
            # 规则很粗，但正好检验「读数可不可读」——标签都不兑现，读它就没意义。
            if five:
                rng = five["high"] - five["low"]
                stage = str(row.get("stage") or "")
                direc = str(row.get("direction") or "")
                if "震荡" in stage or "无趋势" in stage:
                    ok = abs(five["ret"]) <= 0.5 * rng if rng > 0 else False
                    row["label_check"] = ("命中（震荡兑现）" if ok
                                          else "未命中（走成了单边）")
                elif "多头主导" in direc:
                    row["label_check"] = "命中" if five["ret"] > 0 else "未命中"
                elif "空头主导" in direc:
                    row["label_check"] = "命中" if five["ret"] < 0 else "未命中"
                else:
                    row["label_check"] = "无法判定"
        rows.append(row)
    return rows


def render_markdown(rows: List[Dict[str, Any]], asset: str, timeframe: str) -> str:
    usable = [r for r in rows if r["usable"] and 5 in r["fwd"]]
    hits = sum(1 for r in usable if r["mechanical"].startswith("命中"))
    labeled = [r for r in rows if r["label_check"] in ("命中", "未命中")
               or r["label_check"].startswith("命中")]
    label_hits = sum(1 for r in labeled if r["label_check"].startswith("命中"))
    lines = [
        f"# 技术状态回放清单 · {asset} {timeframe}",
        "",
        f"生成时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M')}（UTC+8）",
        f"时点数：{len(rows)}　可机械判定（偏置）：{len(usable)}　其中命中：{hits}"
        f"（{(hits / len(usable) * 100) if usable else 0:.0f}%）",
        f"文字标签订正：{len(labeled)} 条可判，命中 {label_hits}"
        f"（{(label_hits / len(labeled) * 100) if labeled else 0:.0f}%）",
        "",
        "口径：每个时点的读数**只用该时点之前的数据**重算（`asof_ts` 严格截断），",
        "事后走势取该时点后的 1/3/5 日收盘涨跌；括号内是期间最高/最低相对基准价的偏移。",
        "",
        "> **偏置判定**=「合成偏置符号 vs 5 日涨跌符号」；**标签订正**=「读数里的趋势阶段/主导方",
        "> 有没有兑现」：震荡→5 日净涨跌不超过期间振幅一半；强趋势+多头主导→5 日收涨；空头主导→收跌。",
        "> 两者都是粗筛，真正的验收是你读那句读数，看它在当时是否站得住。",
        "> 请在每条的「你的判断」处填：对 / 错 / 半对。",
        "",
    ]
    for n, r in enumerate(rows, 1):
        lines.append(f"### {n:02d} · {r['time']}（收盘 {r['close']:,.2f}）")
        if r["error"]:
            lines += [f"读数：不可用（{r['error']}）", ""]
            continue
        bias_txt = (f"偏置 {r['bias']:+.2f}/覆盖 {r['coverage']:.0%}"
                    if r["bias"] is not None else "偏置 N/A")
        lines.append(f"**读数**：{r['readout']}")
        if r["missing"]:
            lines.append(f"**缺**：{'、'.join(r['missing'])}")
        fwd = " / ".join(
            f"{d}日 {r['fwd'][d]['ret']:+.2f}%（高 {r['fwd'][d]['high']:+.2f}% 低 {r['fwd'][d]['low']:+.2f}%）"
            for d in HORIZONS_DAYS if d in r["fwd"])
        lines.append(f"{bias_txt}　|　之后：{fwd}")
        lines.append(f"偏置判定：{r['mechanical']}　|　标签订正：{r['label_check']}"
                     f"　|　**你的判断**：＿＿")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="技术状态回放清单（L3 人工验收）")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="4h", choices=sorted(ts._TF_SECONDS))
    ap.add_argument("--points", type=int, default=30)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        rows = build_rows(conn, args.asset.upper(), args.timeframe, args.points)
    finally:
        conn.close()
    if not rows:
        print(f"{args.asset} {args.timeframe} 历史不够，无法回放（至少需要 "
              f"{LOOKBACK_BARS + max(HORIZONS_DAYS) * _bars_per_day(args.timeframe)} 根）",
              file=sys.stderr)
        return 1
    md = render_markdown(rows, args.asset.upper(), args.timeframe)
    print(md)
    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(CST).strftime("%Y-%m-%d")
        (OUT_DIR / "latest.md").write_text(md, encoding="utf-8")
        (OUT_DIR / f"{stamp}.md").write_text(md, encoding="utf-8")
        (OUT_DIR / "latest.json").write_text(json.dumps(
            {"asset": args.asset.upper(), "timeframe": args.timeframe, "rows": rows},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n清单已写入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
