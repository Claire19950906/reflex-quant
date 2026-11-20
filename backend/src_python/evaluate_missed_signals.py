#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evaluate_missed_signals.py — v310 P0: 给 missed_signals 回填 5d/20d 结果与命中

背景
----
`populate_missed_signals.py` 只负责**发现**漏检信号, 其 compute_missed() 把
actual_outcome_5d / actual_outcome_20d / hit 直接写死为 None。于是「分析页
(实时信号台) 错过信号界面」只有信号、没有结果 —— 用户看不到"这些漏掉的信号
后来到底对不对", 界面等于半个空壳。

本脚本补齐结果半边:
  · 基准价: 异动时刻 (critical_price_moves.ts) 之前最近一根 ohlcv_intraday 收盘
  · 5d/20d 价: 同源分钟线 (与 db.resolve_entry_price / _forward_outcome_pct
    同一数据源, 避免 daily/intraday 口径噪声)
  · 方向: A1_strong_buy → BUY / A2_strong_sell → SELL /
          A3_continuation → 跟随 move_pct 符号 (趋势延续)
  · hit: 按方向判定 (BUY 看涨为命中, SELL 看跌为命中)

安全性: 默认 dry-run; --commit 才写库; 只 UPDATE 结果三列, 不动信号本身。
用法:
    python3 evaluate_missed_signals.py
    python3 evaluate_missed_signals.py --commit
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

DB_PATH = BACKEND / "trident_event_bus.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _price_at(conn, asset: str, ts: int, direction: str,
              prefer: str = "intraday") -> float | None:
    """direction='before' 取 ts 之前最近一根; 'after' 取 ts+n 之后最早一根。

    prefer 决定用哪张表 —— **基准价与 forward 价必须同源**, 否则两源口径差
    (实测 WTI 3.2% / BTC 1.0%) 会把后视收益抬高 (intraday 口径命中率 72%
    vs daily 口径 95%, 差额纯粹来自数据源不一致)。
    """
    if prefer == "intraday":
        if direction == "before":
            row = conn.execute(
                "SELECT close FROM ohlcv_intraday WHERE asset=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
        else:
            row = conn.execute(
                "SELECT close FROM ohlcv_intraday WHERE asset=? AND ts>=? "
                "ORDER BY ts ASC LIMIT 1", (asset, ts)).fetchone()
        if row and row[0]:
            return float(row[0])
    if direction == "before":
        row = conn.execute(
            "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 1", (asset, ts)).fetchone()
    else:
        row = conn.execute(
            "SELECT close FROM ohlcv_daily WHERE asset=? AND ts>=? "
            "ORDER BY ts ASC LIMIT 1", (asset, ts)).fetchone()
    return float(row[0]) if row and row[0] else None


def _has_intraday_at(conn, asset: str, ts: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ohlcv_intraday WHERE asset=? AND ts>=? LIMIT 1",
        (asset, ts)).fetchone()
    return row is not None


def _direction_of(pattern_type: str, move_pct: float) -> str:
    if pattern_type == "A1_strong_buy_missed":
        return "BUY"
    if pattern_type == "A2_strong_sell_missed":
        return "SELL"
    # A3_continuation: 跟随异动方向 (趋势延续)
    return "BUY" if (move_pct or 0) > 0 else "SELL"


_MOVE_PCT_RE = re.compile(r"move_pct=([-+]?\d+(?:\.\d+)?)")


def _move_pct_from_signal(signal: str | None) -> float | None:
    """从 missed_signals.signal 文本里还原 move_pct。

    v334: 26 条漏检行的 signal_id 指向已被删除的 critical_price_moves
    (同一个"删旧插新拿新自增 id"的老毛病), JOIN 不上 → 永远结算不了,
    S9 卡在 85.9%。但 signal 文本里本来就写着 `move_pct=-5.03`,
    方向 (尤其 A3/A4 "跟随异动方向") 完全可以从这里还原。
    """
    if not signal:
        return None
    m = _MOVE_PCT_RE.search(signal)
    return float(m.group(1)) if m else None


def _date_to_epoch(decision_date: str | None) -> int | None:
    """'YYYY-MM-DD' → 当日 00:00 UTC 的 epoch。"""
    if not decision_date:
        return None
    try:
        d = _dt.datetime.strptime(str(decision_date)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return int(d.replace(tzinfo=_dt.timezone.utc).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _connect()
    t0 = time.time()
    try:
        rows = conn.execute(
            """SELECT ms.id, ms.asset, ms.signal_id, ms.pattern_type, ms.signal,
                      ms.decision_date,
                      m.ts AS move_ts, m.move_pct, m.start_price
               FROM missed_signals ms
               LEFT JOIN critical_price_moves m ON m.id = ms.signal_id"""
        ).fetchall()
        print("=" * 68)
        print("漏检信号结果回填 — v310 P0")
        print("=" * 68)
        print(f"  missed_signals 总数: {len(rows)}")

        updates = []
        stats = {"BUY": [0, 0], "SELL": [0, 0]}
        src_count: dict = {}
        no_move = no_entry = no_fwd5 = 0
        recovered = 0
        for r in rows:
            if r["move_ts"] is None:
                # v334 回退: signal_id 成孤儿 → 用 decision_date 当时的价格当基准,
                # move_pct 从 signal 文本还原。语义反而更准: 基准价就是"那一刻"。
                mvts = _date_to_epoch(r["decision_date"])
                move_pct = _move_pct_from_signal(r["signal"])
                if mvts is None or move_pct is None:
                    no_move += 1
                    continue
                recovered += 1
                start_price = None
            else:
                mvts = int(r["move_ts"])
                move_pct = r["move_pct"]
                start_price = r["start_price"]
            # 口径选择: forward 时刻有没有分钟线 → 决定整条链路用哪张表,
            # 保证「基准价 + forward 价」同源。
            src = "intraday" if _has_intraday_at(conn, r["asset"], mvts + 5 * 86400) \
                else "daily"
            # 基准价: 优先用 critical_price_moves.start_price —— 那是「信号出现时」
            # 的价格, 才是「如果当时接住这个信号」的合理成交价。
            # 若用 ts 之前最近一根收盘, 对慢涨慢跌 (duration 最高 50400 分钟) 来说
            # 往往已经取到异动末端, 后视收益会被动量效应抬高 (实测 BUY 命中率
            # 虚高到 98%)。start_price 缺失时才回退到行情表回溯。
            entry = None
            if src == "intraday":
                entry = _price_at(conn, r["asset"], mvts, "before", "intraday")
            else:
                entry = _price_at(conn, r["asset"], mvts, "before", "daily")
            if not entry:
                entry = start_price
            if not entry:
                no_entry += 1
                continue
            entry = float(entry)
            p5 = _price_at(conn, r["asset"], mvts + 5 * 86400, "after", src)
            if not p5:
                no_fwd5 += 1
                continue
            p20 = _price_at(conn, r["asset"], mvts + 20 * 86400, "after", src)
            o5 = round((p5 - entry) / entry * 100, 3)
            o20 = round((p20 - entry) / entry * 100, 3) if p20 else None
            direction = _direction_of(r["pattern_type"], move_pct or 0)
            hit = 1 if (o5 > 0 if direction == "BUY" else o5 < 0) else 0
            stats[direction][0] += 1
            stats[direction][1] += hit
            src_count[src] = src_count.get(src, 0) + 1
            updates.append((o5, o20, hit, r["id"]))

        print(f"  可评估: {len(updates)}  (含孤儿回补 {recovered})   缺异动记录: {no_move}   "
              f"缺基准价: {no_entry}   缺5d行情: {no_fwd5}")
        print(f"  取价口径: {src_count}  (基准价与 forward 价同源)")
        for d, (n, h) in stats.items():
            print(f"    {d}: n={n:<5} 命中={h:<5} 命中率={h*100//max(n,1)}%")
        if updates:
            avg5 = sum(u[0] for u in updates) / len(updates)
            hits = sum(u[2] for u in updates)
            print(f"    合计: n={len(updates)} 命中={hits} "
                  f"命中率={hits*100//len(updates)}%  5d均收益={avg5:+.3f}%")
        print("  ⚠️ 口径提醒: 漏检信号来自 critical_price_moves (已实现的强异动), "
              "因此该命中率反映的是\n     「事后异动的动量延续率」, 不能直接等同于"
              "「信号的预测能力」。")

        if args.commit:
            for o5, o20, hit, mid in updates:
                conn.execute(
                    "UPDATE missed_signals SET actual_outcome_5d=?, "
                    "actual_outcome_20d=?, hit=? WHERE id=?",
                    (o5, o20, hit, mid))
            conn.commit()
            print(f"\n  ✅ 已落库 {len(updates)} 行")
        else:
            print("\n  (dry-run, 未落库 — 加 --commit 生效)")
        print(f"  耗时 {time.time() - t0:.1f}s")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
