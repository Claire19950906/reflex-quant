import json
import sys
from datetime import datetime, timezone
d = json.load(open("/tmp/btc_v221_10e.json"))
print(f"Bars: {len(d['bars'])}, Anomalies: {len(d['anomalies'])}")
v221_10_inds = {"volume_spike", "big_bullish", "big_bearish", "reversal_p"}
print()
print("=== 全部 4 类 marker ===")
n = 0
for a in d["anomalies"]:
    if a.get("indicator") in v221_10_inds:
        n += 1
        dt = datetime.fromtimestamp(a["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        print(f"  {dt}  {a['type']:<35}  dir={a['direction']:<10}  ind={a['indicator']}")
print(f"\n  共 {n} 条 4 类 marker")
print()
print("=== 单 indicator marker ===")
n1 = 0
for a in d["anomalies"]:
    if a.get("indicator") not in v221_10_inds:
        n1 += 1
        dt = datetime.fromtimestamp(a["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        print(f"  {dt}  {a['type']:<20}  ind={a['indicator']}")
print(f"\n  共 {n1} 条单 indicator marker")
