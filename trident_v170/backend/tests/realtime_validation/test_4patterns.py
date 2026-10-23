#!/usr/bin/env python3
"""v221.10 — 4 类行情精准识别验证。

用户截图（10-03 BTC 10m）里 4 个红箭头分别对应：
  ① 顶部（约 20:30）：大阳线 + 交易量激增
  ② 中部（约 02:00）：大阴线（连续大跌）
  ③ 底部（约 02:30）：大幅涨跌转折点
  ④ 右侧底部（约 05:00）：大阳线恢复

我们要验证：当前 scan_anomalies / scan_abnormals / 4 个直接检测器
（big_candle / vol_break / vol_z / reversal / event_arrow / swing）
能否在「事件当下」精准命中这 4 类行情的每一个实例，而不是事后才看出来。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))

import technical_indicators as ti  # noqa: E402
import abnormal_detector as ad     # noqa: E402

DATA_FILE = "/tmp/btc_10m_3d.json"
d = json.load(open(DATA_FILE))
bars = d["bars"]
print(f"=== BTC 10m 数据 ===")
print(f"Bars: {len(bars)}")
print(f"Time range: {datetime.fromtimestamp(bars[0]['time'], tz=timezone.utc)} → {datetime.fromtimestamp(bars[-1]['time'], tz=timezone.utc)}")
print(f"Price range: {min(b['low'] for b in bars):.1f} → {max(b['high'] for b in bars):.1f}")
print()

candles = [
    [int(b["time"]) * 1000, b["open"], b["high"], b["low"], b["close"], b.get("volume", 0)]
    for b in bars
]
closes = [b["close"] for b in bars]
volumes = [b.get("volume", 0) for b in bars]

print("=== Step 1: 逐根 bar 多指标快照（事件当下） ===")
pct_th, vol_th, swing_pct_th, swing_range_th, cum4_th = ad._interval_thresholds(600)
big_range_th = ad.BIG_CANDLE_RANGE_THRESHOLD_BY_INTERVAL[600]
big_body_th = ad.BIG_CANDLE_BODY_THRESHOLD_BY_INTERVAL[600]
vol_z_th = ad.VOL_Z_THRESHOLD_BY_INTERVAL[600]
rev_pct_th = ad.REVERSAL_PCT_THRESHOLD_BY_INTERVAL[600]
ea_body_th = ad.EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL[600]
ea_vol_th = ad.EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL[600]

print(f"  10m thresholds: pct={pct_th}%  vol={vol_th}x  swing_pct={swing_pct_th}%  swing_range={swing_range_th}%")
print(f"                  big_range={big_range_th}%  big_body={big_body_th}%  vol_z={vol_z_th}  rev={rev_pct_th}%")
print(f"                  ea_body={ea_body_th}%  ea_vol={ea_vol_th}x")
print()

TS, OPEN, HIGH, LOW, CLOSE, VOL = 0, 1, 2, 3, 4, 5

results = []
for i in range(30, len(candles)):
    cur = candles[i]
    ts_sec = int(cur[TS] // 1000)
    o, h, lo, c, v = cur[OPEN], cur[HIGH], cur[LOW], cur[CLOSE], cur[VOL]

    body_pct = (c - o) / o * 100.0 if o else 0
    range_pct = (h - lo) / o * 100.0 if o else 0
    pct_abs = abs(body_pct)
    direction = "bullish" if c > o else ("bearish" if c < o else "neutral")

    vol_ratio = None
    if i >= ad.VOL_LOOKBACK:
        prev_vols = [candles[j][VOL] for j in range(i - ad.VOL_LOOKBACK, i) if candles[j][VOL] > 0]
        if prev_vols:
            vol_ratio = v / (sum(prev_vols) / len(prev_vols))

    sub_candles = candles[: i + 1]
    sub_closes = closes[: i + 1]
    bc = ad._big_candle_score(cur, big_range_th, big_body_th)
    vol_z = ti.vol_zscore(sub_candles)
    ea = ad._event_arrow_score(candles, i, ea_body_th, ea_vol_th)
    rv = ad._reversal_score(candles, i, ad.REVERSAL_LOOKBACK, ad.REVERSAL_DECISION_WINDOW, rev_pct_th) if i >= ad.REVERSAL_LOOKBACK + 1 else None
    sw = ad._swing_score(candles, i, ad.SWING_LOOKBACK, swing_pct_th, swing_range_th) if i >= ad.SWING_LOOKBACK else None
    eng = ti.engulfing(sub_candles)
    macd_x = ti.macd_cross(sub_closes)

    flag_volume_spike = (vol_ratio is not None and vol_ratio >= vol_th) or (vol_z is not None and abs(vol_z) >= vol_z_th)
    flag_big_bullish = direction == "bullish" and pct_abs >= big_body_th
    flag_big_bearish = direction == "bearish" and pct_abs >= big_body_th
    flag_reversal = rv is not None

    results.append({
        "i": i, "ts": ts_sec,
        "dt": datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%m-%d %H:%M"),
        "O": o, "H": h, "L": lo, "C": c, "V": v,
        "body_pct": round(body_pct, 3),
        "range_pct": round(range_pct, 3),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "vol_z": round(vol_z, 2) if vol_z else None,
        "big_candle": bc["kind"] if bc else None,
        "event_arrow": ea["direction"] if ea else None,
        "reversal": rv["direction"] if rv else None,
        "swing": sw["direction"] if sw else None,
        "engulfing": eng,
        "macd_cross": macd_x,
        "flag_volume_spike": flag_volume_spike,
        "flag_big_bullish": flag_big_bullish,
        "flag_big_bearish": flag_big_bearish,
        "flag_reversal": flag_reversal,
    })

print("=== Step 2: 4 类行情命中清单（事件当下） ===")
CAT = {
    "📊 交易量激增": lambda r: r["flag_volume_spike"],
    "📈 大阳线":     lambda r: r["flag_big_bullish"],
    "📉 大阴线":     lambda r: r["flag_big_bearish"],
    "🔄 转折点":     lambda r: r["flag_reversal"],
}
for label, pred in CAT.items():
    hits = [r for r in results if pred(r)]
    print(f"\n【{label}】 命中 {len(hits)} 次")
    for r in hits[:25]:
        dir_arrow = "↑" if r["body_pct"] > 0 else ("↓" if r["body_pct"] < 0 else "·")
        extras = []
        if r["vol_ratio"]:
            extras.append(f"VR={r['vol_ratio']}x")
        if r["vol_z"]:
            extras.append(f"Z={r['vol_z']}")
        if r["event_arrow"]:
            extras.append(f"event_arrow={r['event_arrow']}")
        if r["reversal"]:
            extras.append(f"reversal={r['reversal']}")
        if r["swing"]:
            extras.append(f"swing={r['swing']}")
        if r["big_candle"]:
            extras.append(f"big={r['big_candle']}")
        if r["engulfing"]:
            extras.append(f"engulfing={r['engulfing']}")
        if r["macd_cross"]:
            extras.append(f"macd_{r['macd_cross']}")
        print(f"  {r['dt']}  {dir_arrow} body={r['body_pct']:+.2f}%  range={r['range_pct']:.2f}%  "
              f"O={r['O']:.0f} C={r['C']:.0f}  {'  '.join(extras)}")
    if len(hits) > 25:
        print(f"  ... (其余 {len(hits)-25} 条略)")


print("\n\n=== Step 3: 对比 API 返回的 anomalies ===")
scan_out = ad.scan_anomalies(candles, limit=12, scan_window=len(candles), interval_sec=600)
print(f"scan_anomalies 输出 {len(scan_out)} 条：")
for a in scan_out:
    dt = datetime.fromtimestamp(a["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
    print(f"  {dt}  type={a['type']:20s}  dir={a['direction']:10s}  indicator={a['indicator']}")


print("\n\n=== Step 4: 4 类行情覆盖矩阵（独立 vs scan_anomalies） ===")
ts_lookup = {r["ts"]: r for r in results}
scan_ts_by_ind = {}
for a in scan_out:
    scan_ts_by_ind.setdefault(a["indicator"], set()).add(a["ts"])


def coverage(category_name, pred):
    hit_ts = {r["ts"] for r in results if pred(r)}
    scan_ts = set()
    for ind, tss in scan_ts_by_ind.items():
        scan_ts |= tss
    overlap = hit_ts & scan_ts
    print(f"\n【{category_name}】")
    print(f"  独立触发 bar 数:    {len(hit_ts)}")
    print(f"  scan_anomalies 输出 bar 数: {len(scan_ts)}")
    print(f"  重叠（API 命中数）: {len(overlap)}")
    missed = hit_ts - scan_ts
    if missed:
        print(f"  ⚠️  API 漏掉 {len(missed)} 个：")
        for ts in sorted(missed):
            r = ts_lookup[ts]
            extras = []
            if r["vol_ratio"]:
                extras.append(f"VR={r['vol_ratio']}x")
            if r["big_candle"]:
                extras.append(f"big={r['big_candle']}")
            extras.append(f"body={r['body_pct']:+.2f}%")
            print(f"    {r['dt']}  {'  '.join(extras)}")


coverage("📊 交易量激增", lambda r: r["flag_volume_spike"])
coverage("📈 大阳线",     lambda r: r["flag_big_bullish"])
coverage("📉 大阴线",     lambda r: r["flag_big_bearish"])
coverage("🔄 转折点",     lambda r: r["flag_reversal"])


print("""

=== Step 5: 多因子组合候选（精准识别建议） ===

组合优先级（v221.10 提案）:

1️⃣ 「事件箭头 event_arrow」 ⊃ 大阳/大阴 + 量激增
   - 条件: |body| ≥ ea_body_th (10m 0.5%) AND vol_ratio ≥ ea_vol_th (10m 2.0×)
   - 命中 = 同时满足「价格剧烈 + 量能放大」复合信号
   - 当前已实现 ✓（importance=90，最高优先级）

2️⃣ 「转折点 reversal」 需提前到 N+1 bar（实时化）
   - 当前: 局部极值 (6 根) + 之后 3 根反向 ≥ 阈值% → 等 3 根才确认
   - 改: 局部极值 + 紧接着 1 根 close 突破极值反向 ≥ 单根阈值% → bar 1 即确认
   - 优点: 用户截图箭头 ③ 02:30 转折点即可在 bar-1 close 时被标

3️⃣ 「大阳/大阴 big_candle」 ⊃ event_arrow 子集
   - 当前: body_strong (|body|≥0.5%) OR wick_strong (range≥0.7% AND body≥0.3%)
   - 问题: 当前 import=65，被 event_arrow(90) 压过 → 同根 big_candle 被 cofire 静默
   - 改: 大阳/大阴是「价格形态」，event_arrow 是「价格+量」—— 是 2 个独立维度
   - 建议: cofire_dedup 拆开 big_candle 与 event_arrow（不互吃）

4️⃣ 「量激增 vol_z」当前 import=55，被 vol_break/event_arrow 同根压制
   - 改: 解耦 —— vol_z 是「纯量能」独立信号，不被父级吞掉
   - 输出: "量能激增 Z=2.3" 标签，与 vol_break 共存显示

结论：当前 4 类行情**单指标已实现**，但**多因子组合 + 实时确认**有 3 处可优化：
  A. reversal 实时化（移除 3 bar 决策窗口）
  B. cofire_dedup 不再压制 big_candle（保留 big_candle 作为形态独立信号）
  C. vol_z 不被 vol_break 同根覆盖（保留量能维度标签）
""")
