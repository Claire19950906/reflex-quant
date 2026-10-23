"""
test_tech_detector.py — v303: 因果性(无未来函数)与指标正确性验证
=============================================================
关键测试: **truncation invariance**(截断不变性) ——
若检测器对未来函数免疫, 那么把序列在 t 截断后重跑,
所有 uses_future=False 的信号内容必须与全量跑出来的一致。
这是验证"无未来函数"最强的黑盒测试。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tech_detector as td  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def load(asset: str, limit: int = 400) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT asset,ts,open,high,low,close,volume FROM ohlcv_daily "
        "WHERE asset=? ORDER BY ts DESC LIMIT ?", (asset, limit)
    ).fetchall()
    con.close()
    return [dict(r) for r in reversed(rows)]


def test_indicator_sanity(rows: list[dict]):
    print("\n[1] 指标数值合理性")
    check("MU 数据量 >= 200", len(rows) >= 200, f"got {len(rows)}")
    if len(rows) < 120:
        return
    ind = td.indicators(rows)
    rsis = [x for x in ind["rsi"] if x is not None]
    check("RSI 有值", len(rsis) > 0)
    check("RSI 落在 [0,100]", all(0 <= x <= 100 for x in rsis),
          f"min={min(rsis) if rsis else 'NA'} max={max(rsis) if rsis else 'NA'}")
    emas = [x for x in ind["ema_fast"] if x is not None]
    prices = [r["close"] for r in rows]
    check("EMA fast 在价格量级内",
          all(min(prices) * 0.5 <= x <= max(prices) * 1.5 for x in emas),
          f"min={min(emas) if emas else 'NA'}")
    vrs = [x for x in ind["vol_ratio"] if x is not None]
    check("量比有值且 >0", len(vrs) > 0 and all(x > 0 for x in vrs))
    check("量比最大值 > 1.2", (max(vrs) if vrs else 0) > 1.2,
          f"max={max(vrs) if vrs else 'NA'}")
    atrs = [x for x in ind["atr"] if x is not None]
    check("ATR > 0", len(atrs) > 0 and all(x > 0 for x in atrs))
    check("检测到 pivot", len(ind["pivots"]) > 0, f"n={len(ind['pivots'])}")


def test_no_lookahead(rows: list[dict]):
    """截断不变性 — 无未来函数的决定性测试。"""
    print("\n[2] 无未来函数 (truncation invariance)")
    td.ALLOW_FUTURE = False
    full = td.detect_signals(rows)
    causal = [s for s in full if not s["uses_future"]]
    check("全量检出信号", len(causal) > 0, f"n={len(causal)}")
    bad = 0
    for cut in (0.7, 0.8, 0.9):
        sub = rows[: int(len(rows) * cut)]
        got_by_ts = {s["ts"]: s for s in td.detect_signals(sub)}
        for s in causal:
            g = got_by_ts.get(s["ts"])
            if g is None:
                continue      # pivot 右窗口不足未产出 = 预期延迟
            if abs(g["score"] - s["score"]) > 1e-6:
                bad += 1
            if {h["k"] for h in g["signals"]} != {h["k"] for h in s["signals"]}:
                bad += 1
    check("截断后既有信号完全一致 (score+signals)", bad == 0,
          f"{bad} 处不一致 → 存在未来函数")
    sub = rows[: int(len(rows) * 0.8)]
    got = td.detect_signals(sub)
    full_ts = {s["ts"] for s in full}
    phantom = [s for s in got if s["ts"] not in full_ts]
    check("截断不产生幻影信号", len(phantom) == 0, f"phantom={len(phantom)}")
    td.ALLOW_FUTURE = True


def test_reversal_flagged(rows: list[dict]):
    print("\n[3] 转折点 / 未来函数标记")
    td.ALLOW_FUTURE = True
    rev = [s for s in td.detect_signals(rows) if s["trigger_type"] == "reversal"]
    check("转折点已检出", len(rev) > 0, f"n={len(rev)}")
    check("转折点全部 uses_future=True", all(s["uses_future"] for s in rev))
    td.ALLOW_FUTURE = False
    rev2 = [s for s in td.detect_signals(rows) if s["trigger_type"] == "reversal"]
    check("关闭开关后转折点为 0", len(rev2) == 0, f"n={len(rev2)}")
    td.ALLOW_FUTURE = True


def test_combo_gate(rows: list[dict]):
    print("\n[4] 组合门槛 (单指标不触发; 转折点豁免)")
    sig = td.detect_signals(rows)
    # 转折点是设计上的单一强信号(用户要求"尽量提前找到"), 豁免组合门槛
    causal = [s for s in sig if s["trigger_type"] != "reversal"]
    single = [s for s in causal if sum(1 for h in s["signals"] if h["w"] >= 0.8) < 2]
    check("因果信号无单指标触发", len(single) == 0, f"{len(single)} 条")
    rev = [s for s in sig if s["trigger_type"] == "reversal"]
    check("转折点均带 reversal 核心信号",
          all(any(h["k"].startswith("reversal_") for h in s["signals"]) for s in rev))
    ks: dict = {}
    for s in sig:
        for h in s["signals"]:
            ks[h["k"]] = ks.get(h["k"], 0) + 1
    print("   信号种类分布:", json.dumps(ks, ensure_ascii=False))


def test_liquidation_hook(rows: list[dict]):
    print("\n[5] 爆单热力接入")
    # 造在确定已有因果信号的 bar 上, 验证强平能叠加进打分并改变 trigger_type
    base = [s for s in td.detect_signals(rows)
            if s["trigger_type"] != "reversal"]
    check("存在基线因果信号", len(base) > 0, f"n={len(base)}")
    if not base:
        return
    tgt = max(base, key=lambda s: s["score"])
    fake = {tgt["ts"]: {"long_liq_usd": 5_000_000, "short_liq_usd": 1_000_000,
                        "total_usd": 6_000_000}}
    after = {s["ts"]: s for s in td.detect_signals(rows, liq_by_ts=fake)}
    got = after.get(tgt["ts"])
    check("目标 bar 仍被检出", got is not None)
    if got:
        check("liquidation 已计入 signals",
              any(h["k"] == "liquidation" for h in got["signals"]),
              str([h["k"] for h in got["signals"]]))
        check("分数被抬升", got["score"] >= tgt["score"],
              f"{got['score']} vs {tgt['score']}")
        # 合并后主类型可能为 reversal, 但 merged_types 必须记录原类型
        if got["trigger_type"] == "liquidation":
            check("trigger_type 为 liquidation", True)
        else:
            check("liquidation 出现在被合并的信号里",
                  "liquidation" in [h["k"] for h in got["signals"]]
                  or "liquidation" in (got.get("merged_types") or []))
    # 无数据时必须干净退化
    check("无强平数据时无 liquidation 信号",
          not any(h["k"] == "liquidation"
                  for s in td.detect_signals(rows, liq_by_ts=None)
                  for h in s["signals"]))
    # 远离任何 bar 的时间戳不应被匹配
    far = {rows[-1]["ts"] + 999 * 86400: {"total_usd": 9e9}}
    check("超出 12h 窗口的强平不匹配",
          not any(h["k"] == "liquidation"
                  for s in td.detect_signals(rows, liq_by_ts=far)
                  for h in s["signals"]))


def test_merge_same_ts(rows: list[dict]):
    print("\n[6] 同 ts 合并 (转折点不得覆盖因果信号)")
    sig = td.detect_signals(rows)
    ts_list = [s["ts"] for s in sig]
    check("输出 ts 无重复", len(ts_list) == len(set(ts_list)),
          f"{len(ts_list) - len(set(ts_list))} 条重复")
    # 原始两类信号数之和 应 <= 合并后条数 + ...; 至少不能出现丢失
    td.ALLOW_FUTURE = False
    causal_only = {s["ts"] for s in td.detect_signals(rows)}
    td.ALLOW_FUTURE = True
    merged_ts = {s["ts"] for s in sig}
    lost = causal_only - merged_ts
    check("因果信号在合并后全部保留", not lost, f"丢失 {len(lost)} 条")


def main() -> int:
    print("=" * 62)
    print("tech_detector 因果性与指标验证")
    print("=" * 62)
    rows = load("MU")
    if len(rows) < 120:
        print("数据不足, 跳过")
        return 1
    test_indicator_sanity(rows)
    test_no_lookahead(rows)
    test_reversal_flagged(rows)
    test_combo_gate(rows)
    test_liquidation_hook(rows)
    test_merge_same_ts(rows)
    print("\n" + "=" * 62)
    print(f"结果: PASS={PASS} FAIL={FAIL}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())