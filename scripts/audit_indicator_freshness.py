#!/usr/bin/env python3
"""指标新鲜度体检：哪些指标组停更、停更是"正常"还是"真断"。

为什么做：
    external_indicators 有 176 个 (source, indicator) 组，随时有一批超 30 分钟没更新。
    但"没更新"有三种完全不同的性质，混在一起看只会误判：
      1) 真断粮 —— 唯一源死了，且没有新鲜的同名替代 → 要修
      2) 遗留死路径 —— 老写入器停了，但同名指标另有新鲜源（读端取 max(ts) 不受影响）→ 可清
      3) 休市正常 —— 股票/ETF 类，周末或隔夜不开盘 → 不是问题

用法：
    python3 scripts/audit_indicator_freshness.py
    python3 scripts/audit_indicator_freshness.py --stale-hours 24
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/Users/claire/Desktop/金融1.0/backend/src_python")
import db

# 已知的低频源：样本太少（<3 行）时算不出节奏，用固定容差兜底，
# 否则"周报只有一行"会被 6 小时阈值判成断粮（fed_h41 实测就误报过）。
KNOWN_CADENCE_H = {
    "fed_h41": 10 * 24,      # H.4.1 美联储持仓周报，每周四发布
    "fred": 2 * 24,
    "treasury_gov": 4 * 24,  # 美债曲线按美东日终发布：周末+节假日连在一起可到 4 天
    "fred_csv": 2 * 24,
    "cboe": 2 * 24,
    "alias": 4 * 24,         # 别名行镜像的是日终官方数据，周末+Lag 会到 3-4 天
}

# 交易所有休市节奏的源：周末/隔夜停更是正常的
MARKET_SOURCES = {
    "yfinance", "sina_etf", "stooq", "tencent", "akshare", "eastmoney",
    "cn_etf", "nasdaq", "cboe",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-hours", type=float, default=0.5,
                    help="超过多少小时算停更（默认 0.5h，与读取端 30 分钟门禁对齐）")
    args = ap.parse_args()

    conn = sqlite3.connect("file:" + db.get_db_path() + "?mode=ro", uri=True, timeout=30)
    now = int(time.time())
    cutoff = now - int(args.stale_hours * 3600)
    # macro_snapshots 是小时级写入（实测约 60 分钟一批），不能用 30 分钟门禁去卡它
    macro_cutoff = now - max(3 * 3600, int(args.stale_hours * 3600))

    rows = conn.execute(
        "SELECT source, indicator, MAX(ts) FROM external_indicators "
        "GROUP BY source, indicator").fetchall()
    # macro_snapshots 是前端宏观区块真正读的表。若某指标在那边有新鲜的等价口径
    # （metric_key 大小写不敏感匹配），那 external_indicators 里的旧行同样是死路径。
    macro_fresh = {}
    try:
        for mk, mx in conn.execute(
                "SELECT metric_key, MAX(ts) FROM macro_snapshots GROUP BY metric_key"):
            macro_fresh[str(mk).lower()] = int(mx or 0)
    except sqlite3.Error:
        pass
    fresh_by_indicator = defaultdict(int)
    for _s, ind, mx in rows:
        fresh_by_indicator[ind] = max(fresh_by_indicator[ind], mx)

    # 每个 (source, indicator) 的自有节奏：取最近 50 个点的平均间隔，容差 = max(6h, 3×间隔)。
    # 日频/周频指标（国债收益率、fed_h41 周报）用 30 分钟阈值判"停更"必然误报。
    pace = {}
    for src, ind in conn.execute(
            "SELECT source, indicator FROM external_indicators "
            "GROUP BY source, indicator").fetchall():
        ts = [r[0] for r in conn.execute(
            "SELECT ts FROM external_indicators WHERE source=? AND indicator=? "
            "ORDER BY ts DESC LIMIT 50", (src, ind)).fetchall()]
        if len(ts) >= 3:
            gaps = [ts[i] - ts[i + 1] for i in range(len(ts) - 1)]
            gaps = [g for g in gaps if g > 0]
            if gaps:
                gaps.sort()
                pace[(src, ind)] = gaps[len(gaps) // 2]
        else:
            pace[(src, ind)] = 0

    real, legacy, market = [], [], []
    for src, ind, mx in rows:
        typ = pace.get((src, ind), 0)
        tol = max(6 * 3600, 3 * typ)
        if typ == 0:
            tol = max(tol, KNOWN_CADENCE_H.get(src, 24) * 3600)   # 常量是小时，tol 是秒
        elif src in KNOWN_CADENCE_H:
            # 已知低频源按"发布节奏"给下限：日终/周报遇到周末会天然超过 3×中位间隔
            tol = max(tol, KNOWN_CADENCE_H[src] * 3600)
        if now - mx <= tol:
            continue
        age_h = (now - mx) / 3600.0
        twin = macro_fresh.get(str(ind).lower(), 0)
        # 同名指标另有源在写时，按"日终官方口径"给 4 天宽限：周五的数据到周一本来就有 72h+
        if now - fresh_by_indicator[ind] <= max(tol, 4 * 24 * 3600):
            legacy.append((src, ind, age_h, fresh_by_indicator[ind]))
        elif twin >= macro_cutoff:
            legacy.append((src, ind, age_h, twin))
        elif src in MARKET_SOURCES:
            market.append((src, ind, age_h))
        else:
            real.append((src, ind, age_h))

    print(f"新鲜度体检 · {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
          f"停更阈值 {args.stale_hours}h\n")
    print(f"总组数 {len(rows)} · 停更 {len(real) + len(legacy) + len(market)}")

    print(f"\n【真断粮】唯一源停更且无替代 — {len(real)} 组  ← 要修")
    for src, ind, age in sorted(real, key=lambda x: -x[2])[:12]:
        print(f"   {src:<22}{ind:<26}滞后 {age:7.1f}h")

    print(f"\n【遗留死路径】同名指标在 external_indicators 或 macro_snapshots 有新鲜口径 — {len(legacy)} 组")
    for src, ind, age, fresh in sorted(legacy, key=lambda x: -x[2])[:8]:
        print(f"   {src:<18}{ind:<22}旧 {age:7.1f}h · 新鲜源 "
              f"{datetime.fromtimestamp(fresh).strftime('%m-%d %H:%M')}")

    print(f"\n【休市正常】股票/ETF 类隔夜或周末停更 — {len(market)} 组")
    for src, ind, age in sorted(market, key=lambda x: -x[2])[:5]:
        print(f"   {src:<18}{ind:<22}{age:7.1f}h")

    # 读端是否会因多源而读到旧值：读端一律 ORDER BY ts DESC LIMIT 1，跨源取最新
    print("\n结论：只要保持\"读端不按 source 过滤\"，多源并存不会读到旧值；"
          "需要处理的只有【真断粮】那批。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
