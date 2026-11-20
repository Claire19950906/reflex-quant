#!/usr/bin/env python3
"""行情质量体检：冻结报价 / 空洞 / 停更。

为什么需要（v343 实测）：
    1 分钟线里有大量"与前一根完全相同"的收盘价 —— 那不是行情没动，是**报价没更新**。
    XAU 24.1%（最长连续 30 根）、WTI 12.8%（最长 57 根）、NVDA 19.0%
    （**最长连续 959 根 ≈ 16 小时**）。BTC 只有 3.1%（最长 6 根），说明这是
    特定数据源的问题，不是市场特性。

    后果：MFE/MAE 会系统性偏小 —— 窗口全冻住时 MFE=MAE=0，
    看起来"这段完全没波动"，于是止盈止损被标定得过窄。

用法：
    python3 scripts/audit_price_quality.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

sys.path.insert(0, "backend/src_python")
import db

FROZEN_BAD_RATIO = 0.15     # 同价占比超过这个值视为数据可疑
FROZEN_BAD_RUN = 120        # 连续同价超过 2 小时视为冻结


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30)
    assets = [r[0] for r in conn.execute(
        "SELECT DISTINCT asset FROM ohlcv_intraday").fetchall()]

    out = {}
    now = int(time.time())
    for a in assets:
        rows = conn.execute(
            "SELECT ts, close FROM ohlcv_intraday WHERE asset=? ORDER BY ts", (a,)
        ).fetchall()
        n = len(rows)
        if n < 10:
            continue
        same = 0
        run = best = 1
        for i in range(1, n):
            if rows[i][1] == rows[i - 1][1]:
                same += 1
                run += 1
                best = max(best, run)
            else:
                run = 1
        ratio = same / n
        stale_min = int((now - rows[-1][0]) / 60)
        out[a] = {
            "bars": n,
            "frozen_ratio": round(ratio, 4),
            "max_frozen_run": best,
            "last_bar_age_min": stale_min,
            "verdict": ("frozen" if (ratio > FROZEN_BAD_RATIO or best > FROZEN_BAD_RUN)
                        else "ok"),
        }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print(f"{'资产':<10}{'根数':>7}{'同价占比':>10}{'最长冻结':>9}{'最后更新':>10}  判定")
    for a, v in sorted(out.items(), key=lambda kv: -kv[1]["frozen_ratio"]):
        mark = "❌ 冻结" if v["verdict"] == "frozen" else "✅"
        print(f"{a:<10}{v['bars']:>7}{v['frozen_ratio']:>9.1%}"
              f"{v['max_frozen_run']:>8}根{v['last_bar_age_min']:>8}分  {mark}")
    bad = [a for a, v in out.items() if v["verdict"] == "frozen"]
    print(f"\n可疑资产 {len(bad)} 个：{', '.join(bad) if bad else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
