#!/usr/bin/env python3
"""v221.10 — 4 类行情精准识别验证（改造后版本）

对比改造前后 scan_anomalies 输出，验证：
  ✅ 交易量激增 (volume_spike) — 多因子加权，量 z + 量比加成
  ✅ 大阳线 (big_bullish) — body + range + 量能加权
  ✅ 大阴线 (big_bearish) — body + range + 量能加权
  ✅ 转折点 (reversal_p) — 实时化（当前 bar 突破反向即可）

实测数据：BTC 10m，100 根 bar，2026-10-04 14:00 → 10-05 06:30
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))

import abnormal_detector as ad  # noqa: E402

d = json.load(open("/tmp/btc_10m_3d.json"))
bars = d["bars"]
print("=== BTC 10m 数据 ===")
print(f"Bars: {len(bars)}")
print(f"Time range: {datetime.fromtimestamp(bars[0]['time'], tz=timezone.utc)} → {datetime.fromtimestamp(bars[-1]['time'], tz=timezone.utc)}")
print(f"Price range: {min(b['low'] for b in bars):.1f} → {max(b['high'] for b in bars):.1f}")
print()

candles = [
    [int(b["time"]) * 1000, b["open"], b["high"], b["low"], b["close"], b.get("volume", 0)]
    for b in bars
]

print("=== v221.10 4 类行情 marker 独立识别 ===")
markers_4p = ad.detect_4patterns(candles, interval_sec=600)
print(f"输出 {len(markers_4p)} 条 marker：")
print()
print(f"{'时间':<18} {'类型':<35} {'方向':<10} {'指标':<14} {'score':<6}")
print("-" * 90)
for m in markers_4p:
    dt = datetime.fromtimestamp(m["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
    print(f"{dt:<18} {m['type']:<35} {m['direction']:<10} {m['indicator']:<14} {m.get('score', 0):<6}")

# ── 按类统计 ──
print()
print("=== 按类统计 ===")
from collections import Counter
ctr = Counter(m["indicator"] for m in markers_4p)
for ind in ["volume_spike", "big_bullish", "big_bearish", "reversal_p"]:
    print(f"  {ind:<14}: {ctr.get(ind, 0)} 次")

print()
print("=== 对比：scan_anomalies 综合输出（含 4 类行情追加）===")
scan_out = ad.scan_anomalies(candles, limit=20, scan_window=len(candles), interval_sec=600)
print(f"scan_anomalies 输出 {len(scan_out)} 条：")
print()
for a in scan_out:
    dt = datetime.fromtimestamp(a["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
    print(f"  {dt}  {a['type']:<30}  dir={a['direction']:<10}  ind={a['indicator']}")

print()
print("=== 用户截图 4 个箭头对照验证 ===")
print("""
用户 2026-10-05 截图（10-03 BTC 10m）里 4 个红箭头：
  ① 顶部（约 20:30）：大阳线 + 交易量激增
  ② 中部（约 02:00）：大阴线
  ③ 底部（约 02:30）：大幅涨跌转折点
  ④ 右侧底部（约 05:00）：大阳线恢复

本次测试数据（10-04 → 10-05）覆盖了类似形态：
""")
arrows_check = [
    ("① 顶部 20:30 大阳+量激增", "10-04 20:30/20:40/22:00/22:10 5 连阳 + 量 4-6x"),
    ("② 中部 02:00 大阴", "10-05 03:50/04:10/04:20/04:30 4 连阴 + 量 2-3x"),
    ("③ 底部 02:30 转折", "10-05 04:30 85541 → 后续反弹"),
    ("④ 右侧 05:00 大阳", "10-05 05:50/06:00/06:10 反弹"),
]
for label, expect in arrows_check:
    print(f"  {label}  期望：{expect}")
