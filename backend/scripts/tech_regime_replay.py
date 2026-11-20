# -*- coding: utf-8 -*-
"""tech_regime_replay.py — 技术信号「环境分层」五年回放（只读，不写库）。

回答一个问题：把同样的技术信号放进不同市场环境（MA200 上下、ADX 强/弱、
趋势同向/逆势），方向命中率会不会从「无边际」变成「有边际」。

口径与 v450 回放一致：
  · 因果信号：tech_detector 现行版本（不含未来函数）
  · 未来收益：信号当根收盘 → 第 5 根收盘，百分比
  · 基线命中：同资产、同方向的无条件 5 日命中率（BUY 用 P(涨)，SELL 用 1-P(涨)）
  · 超额：信号自身 5 日收益减去同资产均值收益（不是命中率差）

用法：
    python3 scripts/tech_regime_replay.py            # 打印并写报告
    python3 scripts/tech_regime_replay.py --no-write
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sqlite3
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import tech_detector as td
import tech_signal_bridge as tsb

DB = BACKEND / "trident_event_bus.db"
OUT = BACKEND / "reports" / "tech_engine"


def load_assets(conn, min_bars=300):
    rows = conn.execute(
        "SELECT asset, COUNT(*) n FROM ohlcv_daily GROUP BY asset HAVING n >= ? ORDER BY asset",
        (min_bars,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def load_bars(conn, asset):
    rows = [dict(r) for r in conn.execute(
        "SELECT asset,ts,open,high,low,close,volume FROM ohlcv_daily "
        "WHERE asset=? ORDER BY ts", (asset,))]
    for b in rows:
        b["open"] = float(b["open"] if b["open"] is not None else b["close"])
        b["high"] = float(b["high"])
        b["low"] = float(b["low"])
        b["close"] = float(b["close"])
        b["volume"] = float(b["volume"] or 0)
    return rows


def sma200(closes):
    n = len(closes)
    out = [None] * n
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= 200:
            s -= closes[i - 200]
        if i >= 199:
            out[i] = s / 200.0
    return out


def baseline(rows):
    rets = [(rows[i + 5]["close"] - rows[i]["close"]) / rows[i]["close"] * 100
            for i in range(len(rows) - 5)]
    return statistics.mean(rets), sum(1 for x in rets if x > 0) / len(rets) * 100


def stats(rs, label, min_n=30):
    dirs = [r for r in rs if r["action"] in ("BUY", "SELL")]
    if not dirs:
        return {"label": label, "n": len(rs), "dir": 0}
    hit = sum(1 for r in dirs if r["hit"]) / len(dirs) * 100
    basehit = sum(r["basehit"] for r in dirs) / len(dirs)
    ex = statistics.mean(r["ex"] for r in dirs)
    return {"label": label, "n": len(rs), "dir": len(dirs),
            "hit": hit, "basehit": basehit, "delta": hit - basehit, "ex": ex}


def fmt(s):
    if not s.get("dir"):
        return f"| {s['label']} | {s['n']} | 0 | - | - | - | - |"
    mark = "**" if abs(s["delta"]) >= 5 and s["dir"] >= 30 else ""
    return (f"| {s['label']} | {s['n']} | {s['dir']} | "
            f"{mark}{s['hit']:.1f}{mark} | {s['basehit']:.1f} | "
            f"{s['delta']:+.1f} | {s['ex']:+.2f} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--min-bars", type=int, default=300)
    a = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    recs = []
    assets = load_assets(conn, a.min_bars)

    for asset, _n in assets:
        rows = load_bars(conn, asset)
        if len(rows) < 260:
            continue
        ma = sma200([r["close"] for r in rows])
        base_ret, base_up = baseline(rows)
        sigs = td.detect_signals(rows)
        for s in sigs:
            i = s["idx"]
            if i + 5 >= len(rows):
                continue
            fwd = (rows[i + 5]["close"] - rows[i]["close"]) / rows[i]["close"] * 100
            action, reason, strength = tsb.decide_direction(
                s["signals"], s.get("pct_1d"), None)
            keys = [h["k"] for h in s["signals"]]
            fams = {td._EVENT_FAMILY.get(k) for k in keys
                    if td._EVENT_FAMILY.get(k)}
            fams.discard(None)
            ctx = {h["k"] for h in s["signals"] if h.get("role") == "context"}
            trend_adx = ("bull" if "adx_strong_bull" in ctx else
                         "bear" if "adx_strong_bear" in ctx else "weak")
            ma_pos = ("above" if ma[i] is not None and rows[i]["close"] > ma[i]
                      else "below" if ma[i] is not None else "na")
            # 超额收益必须按方向算：SELL 的收益是 -fwd，基线是 -base_ret
            dir_ret = fwd if action == "BUY" else -fwd
            dir_base = base_ret if action == "BUY" else -base_ret
            recs.append({
                "asset": asset, "type": s["trigger_type"], "score": s["score"],
                "grade": s.get("grade"), "action": action, "fwd": fwd,
                "ex": dir_ret - dir_base, "basehit": base_up if action == "BUY" else 100 - base_up,
                "hit": (fwd if action == "BUY" else -fwd) > 0,
                "keys": keys, "family_n": len(fams),
                "ma": ma_pos, "adx": trend_adx,
            })

    # 环境标签
    def regime(r, mode):
        if mode == "ma":
            return r["ma"]
        if mode == "adx":
            return r["adx"]
        # 顺势/逆势：BUY 要价在 MA200 上且 ADX 多头；SELL 反之
        if r["action"] == "BUY":
            return "顺势" if (r["ma"] == "above" and r["adx"] == "bull") else "逆势"
        if r["action"] == "SELL":
            return "顺势" if (r["ma"] == "below" and r["adx"] == "bear") else "逆势"
        return "无方向"

    lines = []
    lines.append("# 技术信号环境分层回放（v451）")
    lines.append("")
    lines.append(f"生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}　资产 {len(assets)} 个　"
                 f"信号 {len(recs)} 条")
    lines.append("")
    lines.append("> 口径：因果信号、5 日未来收益、同资产基线；"
                 "Δ = 命中率 − 同资产同方向无条件命中率；**≥5pt 且样本≥30 才加粗**。")
    lines.append("> 环境定义：MA200=信号当根收盘相对 200 日均线；"
                 "ADX=信号当根是否带 `adx_strong_*`（ADX≥阈值）；"
                 "顺势=BUY 在 MA200 上且 ADX 多头 / SELL 反之。")
    lines.append("")

    lines.append("## 总览")
    lines.append("")
    lines.append("| 组 | 全部 | 有方向 | 命中率 | 基线 | Δ | 超额收益 |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(fmt(stats(recs, "全部")))
    for act in ("BUY", "SELL"):
        lines.append(fmt(stats([r for r in recs if r["action"] == act], act)))
    for g in ("A", "B", "C"):
        for act in ("BUY", "SELL"):
            rs = [r for r in recs if r["grade"] == g and r["action"] == act]
            if len(rs) >= 30:
                lines.append(fmt(stats(rs, f"{g}/{act}")))
    lines.append(fmt(stats([r for r in recs if r["family_n"] >= 3], "combo ≥3 族")))
    lines.append("")

    lines.append("## 按环境")
    lines.append("")
    lines.append("| 组 | 全部 | 有方向 | 命中率 | 基线 | Δ | 超额收益 |")
    lines.append("|---|---|---|---|---|---|---|")
    for act in ("BUY", "SELL"):
        for mode, vals in (("ma", ("above", "below")),
                           ("adx", ("bull", "bear", "weak")),
                           ("trend", ("顺势", "逆势"))):
            for v in vals:
                rs = [r for r in recs if r["action"] == act and regime(r, mode) == v]
                lines.append(fmt(stats(rs, f"{act} · {mode}={v}")))
    lines.append("")

    lines.append("## 关键因子 × 环境（桥接定向）")
    lines.append("")
    factors = ["rsi_oversold", "bb_lower_break", "kdj_overbought",
               "stoch_oversold", "div_bottom", "div_top", "support_test",
               "vol_bull_break", "vol_bear_break"]
    lines.append("| 因子 | 环境 | 有方向 | 命中率 | 基线 | Δ | 超额收益 |")
    lines.append("|---|---|---|---|---|---|---|")
    SIDE = {"rsi_oversold": "BUY", "bb_lower_break": "BUY", "stoch_oversold": "BUY",
            "vol_bull_break": "BUY", "vol_bear_break": "BUY", "div_bottom": "BUY",
            "support_test": "BUY", "kdj_overbought": "SELL", "div_top": "SELL"}
    for k in factors:
        side = SIDE.get(k)
        rs0 = [r for r in recs if k in r["keys"] and r["action"] == side]
        if not rs0:
            continue
        s = stats(rs0, f"{k} · 全部")
        lines.append(f"| {k} | 全部 | {s['dir']} | {s['hit']:.1f} | "
                     f"{s['basehit']:.1f} | {s['delta']:+.1f} | {s['ex']:+.2f} |")
        for mode, vals in (("ma", ("above", "below")),
                           ("adx", ("bull", "bear", "weak")),
                           ("trend", ("顺势", "逆势"))):
            for v in vals:
                rs = [r for r in rs0 if regime(r, mode) == v]
                if len(rs) < 30:
                    continue
                s = stats(rs, "")
                lines.append(f"| {k} | {mode}={v} | {s['dir']} | {s['hit']:.1f} | "
                             f"{s['basehit']:.1f} | {s['delta']:+.1f} | {s['ex']:+.2f} |")
    lines.append("")

    lines.append("## 反向因子单测（不用桥接，直接测方向）")
    lines.append("")
    lines.append("| 因子 | 做多命中 | 做空命中 | 样本 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for k in ("vol_bull_break", "vol_bear_break", "atr_expand", "div_bottom"):
        rs = [r for r in recs if k in r["keys"]]
        if not rs:
            continue
        n = len(rs)
        p_up = sum(1 for r in rs if r["fwd"] > 0) / n * 100
        base_up = sum(100 - r["basehit"] if r["action"] == "SELL" else r["basehit"]
                      for r in rs) / n
        lines.append(f"| {k} | {p_up:.1f}% | {100 - p_up:.1f}% | {n} | "
                     f"同资产基线涨 {base_up:.1f}%（直接翻方向需打败它） |")
    lines.append("")

    # 闸门模拟：只改「什么时候允许下单」，不改因子本身
    lines.append("## 闸门模拟（白名单 + MA200 / ADX 环境）")
    lines.append("")
    lines.append("| 规则 | 有方向 | 命中率 | 基线 | Δ | 超额收益 |")
    lines.append("|---|---|---|---|---|---|")
    def has(r, keys):
        return bool(set(r["keys"]) & keys)
    combos = [
        ("现白名单 rsi+bb", {"rsi_oversold", "bb_lower_break"}),
        ("+stoch", {"rsi_oversold", "bb_lower_break", "stoch_oversold"}),
        ("+vol_bear", {"rsi_oversold", "bb_lower_break", "vol_bear_break"}),
        ("四因子全开", {"rsi_oversold", "bb_lower_break", "stoch_oversold", "vol_bear_break"}),
    ]
    rules = []
    for name, keys in combos:
        rules.append((f"BUY · {name}（无闸门）",
                      lambda r, keys=keys: r["action"] == "BUY" and has(r, keys)))
        rules.append((f"BUY · {name} + MA200上",
                      lambda r, keys=keys: r["action"] == "BUY" and r["ma"] == "above" and has(r, keys)))
    rules += [
        ("BUY · 四因子 + 顺势(MA200上&ADX多)",
         lambda r: r["action"] == "BUY" and r["ma"] == "above" and r["adx"] == "bull"
         and has(r, {"rsi_oversold", "bb_lower_break", "stoch_oversold", "vol_bear_break"})),
        ("BUY · 四因子 + MA200上且ADX非多",
         lambda r: r["action"] == "BUY" and r["ma"] == "above" and r["adx"] != "bull"
         and has(r, {"rsi_oversold", "bb_lower_break", "stoch_oversold", "vol_bear_break"})),
        ("SELL · kdj_overbought（无闸门）",
         lambda r: r["action"] == "SELL" and "kdj_overbought" in r["keys"]),
        ("SELL · kdj_overbought + MA200下",
         lambda r: r["action"] == "SELL" and r["ma"] == "below" and "kdj_overbought" in r["keys"]),
        ("SELL · kdj_overbought + 顺势(MA200下&ADX空)",
         lambda r: r["action"] == "SELL" and r["ma"] == "below" and r["adx"] == "bear"
         and "kdj_overbought" in r["keys"]),
    ]
    for label, fn in rules:
        rs = [r for r in recs if fn(r)]
        st = stats(rs, label)
        if st.get("dir"):
            lines.append(f"| {label} | {st['dir']} | {st['hit']:.1f} | {st['basehit']:.1f} | "
                         f"{st['delta']:+.1f} | {st['ex']:+.2f} |")
        else:
            lines.append(f"| {label} | 0 | - | - | - | - |")
    lines.append("")

    report = "\n".join(lines) + "\n"
    print(report)
    if not a.no_write:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "v451_regime.md").write_text(report, encoding="utf-8")
        import gzip
        payload = json.dumps(
            {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
             "signals": len(recs),
             "records": [{k: r[k] for k in ("asset", "type", "action", "grade",
                                            "score", "fwd", "ex", "basehit",
                                            "ma", "adx", "keys")}
                         for r in recs]},
            ensure_ascii=False).encode("utf-8")
        with gzip.open(OUT / "v451_regime.json.gz", "wb") as f:
            f.write(payload)
        print(f"[ok] 写入 {OUT/'v451_regime.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
