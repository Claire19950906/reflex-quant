#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_quality_guard.py —— 派生指标的数值合理性守卫

为什么需要：
  `freshness_audit` 只管"这套序列还活着吗"，不管"这个数字像不像真的"。
  所以 Coinbase 溢价能一路算成 **−91.97%**、白银溢价能算出 **+114,690%**，
  两条都顺利进了库、进了规则层，才被人肉发现。
  这类错误靠人盯不住，得有守卫。

做法（不猜、不修数，只标可疑）：
  - 给每个派生指标一条**物理/业务区间**（不是我想要的理想值，是"超过就不像真的"）；
  - 检查最近 N 个点，越界的写进 `data_quality_flags`；
  - 只标记，**不改值、不删行** —— 数据的事交给人看，守卫只负责把脏的挑出来。

用法：
  python3 data_quality_guard.py --dry-run
  python3 data_quality_guard.py
"""
import argparse
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

import db

# 指标 → (下限, 上限, 说明)。上限下限取"越过就一定有问题"的宽区间，不做精细筛选。
SANITY: Dict[str, Tuple[float, float, str]] = {
    "cb_premium_pct": (-5, 5, "现货跨所溢价超过 ±5% 不可能是真实报价"),
    "sge_silver_premium_pct": (-40, 60, "白银内外价差 ±40/60% 之外是单位或换算错了"),
    "sge_gold_premium_pct": (-10, 30, "黄金内外价差区间应远窄于白银"),
    "xau_xag_ratio": (30, 160, "金银比历史区间约 30~160，超出必是标的或单位错"),
    "xau_hg_ratio": (3, 40, "金铜比区间约 3~40"),
    "xau_wti_ratio": (15, 110, "金油比区间约 15~110"),
    "xau_spx_ratio": (0.2, 3.0, "金/标普比价区间"),
    "xau_atm_iv": (0, 200, "隐含波动率 0~200%"),
    "xag_atm_iv": (0, 300, "隐含波动率 0~300%"),
    "crypto_atm_iv": (0, 400, "加密 IV 0~400%"),
    "crypto_25d_skew": (-40, 40, "25Δ 偏度合理区间"),
    "xau_25d_skew": (-40, 40, "25Δ 偏度合理区间"),
    "xag_25d_skew": (-40, 40, "25Δ 偏度合理区间"),
    "us_skew_index": (100, 180, "CBOE SKEW 历史区间约 100~180"),
    "stable_supply_total": (1e10, 1e13, "稳定币总供应 100 亿~10 万亿"),
    "stable_supply_30d_pct": (-30, 60, "30 日供应变化 ±30/60% 之外不正常"),
    "fomc_days_to_next": (0, 200, "距下次议息不应超过 200 天"),
    "fomc_meetings_90d": (0, 10, "90 天内最多十场"),
}

LOOKBACK = 200            # 每个指标回看多少个点
TABLE = "data_quality_flags"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, source TEXT, asset TEXT, indicator TEXT, value REAL,
            rule TEXT, note TEXT, created_at INTEGER
        )""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_ind_ts ON {TABLE}(indicator, ts)")


# 死线检测：连续 N 个点数值完全不变，多半是源挂了还回一个固定值
#   （FRED 那次就是全 000，能一路写进库）。
FLATLINE_N = 12


def scan_flatlines(conn: sqlite3.Connection, dry_run: bool) -> int:
    """跨日死线：按自然日取当日最后一个值，若最近 N 个**不同日期**值完全相同 → 可疑。

    只看"最近 N 个点"会误报：VIX 这类一天内被轮询几十次、值不变很正常。
    真正要抓的是"连着十几个交易日都一模一样"，那基本是上游回了个固定值。
    """
    now = int(time.time())
    hits = 0
    inds = [r[0] for r in conn.execute(
        "SELECT DISTINCT indicator FROM external_indicators WHERE value IS NOT NULL")]
    for ind in inds:
        rows = conn.execute(
            """SELECT ts, source, asset, value FROM external_indicators
               WHERE indicator=? AND value IS NOT NULL ORDER BY ts DESC LIMIT 400""",
            (ind,)).fetchall()
        per_day: Dict[int, Tuple[int, str, str, float]] = {}
        for ts, src, asset, val in rows:
            # ts 有秒有毫秒，先归一到秒
            t_sec = int(ts / 1000) if ts > 10_000_000_000 else int(ts)
            day = t_sec // 86400
            if day not in per_day:                 # 降序读，首见即当日最新
                per_day[day] = (t_sec, src, asset, val)
        if len(per_day) < 12:
            continue
        days = sorted(per_day.keys(), reverse=True)[:12]
        vals = {round(per_day[d][3], 6) for d in days}
        if len(vals) != 1:
            continue
        hits += 1
        v = next(iter(vals))
        ts, src, asset = per_day[days[0]][0], per_day[days[0]][1], per_day[days[0]][2]
        print(f"  ⚠ {ind:<24} 最近 {len(days)} 个交易日全为 {v} —— 疑似源回固定值")
        if dry_run:
            continue
        exists = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE indicator=? AND ts=? AND rule='flatline' LIMIT 1",
            (ind, ts)).fetchone()
        if not exists:
            conn.execute(
                f"""INSERT INTO {TABLE}(ts, source, asset, indicator, value, rule, note, created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (ts, src, asset, ind, v, "flatline",
                 f"连续 {len(days)} 个交易日同值，疑似上游失效", now))
    return hits


def scan(conn: sqlite3.Connection, dry_run: bool) -> int:
    ensure_table(conn)
    now = int(time.time())
    total = 0
    for ind, (lo, hi, note) in SANITY.items():
        rows = conn.execute(
            """SELECT ts, source, asset, value FROM external_indicators
               WHERE indicator=? AND value IS NOT NULL
               ORDER BY ts DESC LIMIT ?""", (ind, LOOKBACK)).fetchall()
        if not rows:
            continue
        bad = [r for r in rows if not (lo <= r[3] <= hi)]
        total += len(bad)
        if not bad:
            continue
        worst = max(bad, key=lambda r: abs(r[3] - (lo if r[3] < lo else hi)))
        print(f"  ⚠ {ind:<24} {len(bad)}/{len(rows)} 点越界 "
              f"[{lo}, {hi}] | 最远 {worst[3]:.4g} @ "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(worst[0] if worst[0] < 1e11 else worst[0] / 1000))}")
        if dry_run:
            continue
        for ts, src, asset, val in bad:
            rule = "below_min" if val < lo else "above_max"
            # 同一条(指标+时间+规则)不重复记
            exists = conn.execute(
                f"SELECT 1 FROM {TABLE} WHERE indicator=? AND ts=? AND rule=? LIMIT 1",
                (ind, ts, rule)).fetchone()
            if exists:
                continue
            conn.execute(
                f"""INSERT INTO {TABLE}(ts, source, asset, indicator, value, rule, note, created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (ts, src, asset, ind, val, rule,
                 f"区间[{lo},{hi}] 越界：{note}", now))
    if not dry_run:
        conn.commit()
    return total


def scan_cross_source(conn: sqlite3.Connection, dry_run: bool) -> int:
    """跨源一致性：同一件事两家给的数差太远，必有一家口径不对。

    刚接的 DefiLlama / CoinPaprika 和原有 CoinGecko 是并行跑的两套，
    没人盯着就会出现"看板写 58.8%、另一块写 56.9%"这种自相矛盾。
    这里只做一件事：差距超阈值就叫一声，不改数、不选边。
    """
    now = int(time.time())
    hits = 0

    def _latest(ind: str, src: str) -> Optional[float]:
        r = conn.execute(
            """SELECT value FROM external_indicators
               WHERE indicator=? AND source=? ORDER BY ts DESC LIMIT 1""",
            (ind, src)).fetchone()
        return float(r[0]) if r else None

    checks = [
        # (说明, A源指标, A源名, B源指标, B源名, 允许差, 单位)
        ("BTC 占比", "cg_btc_dominance", "coingecko",
         "cp_btc_dominance", "coinpaprika", 4.0, "pp"),
        ("加密总市值", "cg_total_mcap_usd", "coingecko",
         "cp_total_mcap_usd", "coinpaprika", 8.0, "pct"),
        ("稳定币总量", "stable_supply_total", "derived_crypto",
         "stable_supply_total", "defillama", 15.0, "pct"),
    ]
    for name, ind_a, src_a, ind_b, src_b, tol, unit in checks:
        a, b = _latest(ind_a, src_a), _latest(ind_b, src_b)
        if a is None or b is None:
            continue
        gap = abs(a - b)
        rel = gap / max(abs(a), abs(b), 1e-9) * 100
        bad = gap if unit == "pp" else rel
        if bad <= tol:
            continue
        hits += 1
        print(f"  ⚠ {name:<10} {src_a}={a:,.4g} vs {src_b}={b:,.4g} "
              f"差 {bad:.2f}{unit}（阈值 {tol}{unit}）")
        if dry_run:
            continue
        exists = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE indicator=? AND rule='cross_source_gap'"
            " AND ts=? LIMIT 1", (ind_a, now // 3600)).fetchone()
        if not exists:
            conn.execute(
                f"""INSERT INTO {TABLE}(ts, source, asset, indicator, value, rule, note, created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (now, src_a, "-", ind_a, a, "cross_source_gap",
                 f"{src_a}={a:.6g} vs {src_b}={b:.6g}，差 {bad:.2f}{unit}，超阈值 {tol}{unit}",
                 now))
    return hits


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    n = scan(conn, args.dry_run)
    flat = scan_flatlines(conn, args.dry_run)
    cross = scan_cross_source(conn, args.dry_run)
    checked = len(SANITY)
    print(f"[{time.strftime('%H:%M:%S')}] 数值守卫：检查 {checked} 个指标，"
          f"越界 {n} 点、死线 {flat} 条、跨源 {cross} 处{'' if args.dry_run else '（已记录，未修改数据）'}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
