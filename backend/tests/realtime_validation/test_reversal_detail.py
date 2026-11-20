#!/usr/bin/env python3
"""调试：每根 bar 的 reversal_p 得分（即使未触发也列出 ≥15 的）"""
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src_python"))

import abnormal_detector as ad

d = json.load(open("/tmp/btc_10m_3d.json"))
bars = d["bars"]
candles = [[int(b["time"])*1000, b["open"], b["high"], b["low"], b["close"], b.get("volume",0)] for b in bars]

TS, OPEN, HIGH, LOW, CLOSE, VOL = 0, 1, 2, 3, 4, 5

print("=== 每根 bar 的 reversal_p 得分（≥15 即列）===")
for i in range(30, len(candles)):
    cur = candles[i]
    o, h, lo, c, v = cur[OPEN], cur[HIGH], cur[LOW], cur[CLOSE], cur[VOL]
    if None in (o, h, lo, c, v) or o <= 0:
        continue
    if i < ad._PATTERN_REVERSAL_LOOKBACK:
        continue
    window = candles[i - ad._PATTERN_REVERSAL_LOOKBACK:i]
    prev_high = max(b[HIGH] for b in window if b[HIGH] is not None)
    prev_low = min(b[LOW] for b in window if b[LOW] is not None)
    if prev_high <= prev_low:
        continue
    wr = (prev_high - prev_low) / prev_low * 100
    s = 0
    kind = ""
    if c > prev_high:
        s = int((c - prev_low) / prev_low * 100 * 30)
        kind = "breakout_up"
    elif c < prev_low:
        s = int((prev_high - c) / c * 100 * 30)
        kind = "breakout_down"
    if s >= 15:
        dt = datetime.fromtimestamp(int(cur[TS] / 1000), tz=timezone.utc).strftime("%m-%d %H:%M")
        dir_arrow = chr(8593) if c > o else chr(8595)
        print(f"  {dt}  {dir_arrow} c={c:.0f}  prev_H={prev_high:.0f} prev_L={prev_low:.0f}  wr={wr:.2f}%  score={s}  kind={kind}")
