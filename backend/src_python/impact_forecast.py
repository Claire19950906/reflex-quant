#!/usr/bin/env python3
"""
第12条：信息影响力幅度（从「从未输出」到「可测」）

现状：ai_decisions.impact_magnitude 8319 行全为 0 —— 系统从没输出过幅度，
      所以「评估一条信息的市场影响力」无从测起。

本脚本定义并落地一个可测的幅度输出：
  预测值 = 该资产 × 该动作(BUY/SELL) 历史「未来3日绝对超额移动」的 P25/P50/P75
  约束   = 只用事件发生之前的样本（防前视），样本 <20 条则不给预测（判不可预测）
  测法   = 区间覆盖率：实际幅度落在 [P25,P75] 的比例。覆盖率 ≈50% 说明区间合理，
           远高于 50% 说明区间太宽（没用），远低于 50% 说明区间太窄（胡说）。

产出：runtime/eval/eval.db 的 impact_forecast 表（独立库，不碰生产库写锁）。

用法：python3 impact_forecast.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"
EVAL_DB = BACKEND / "runtime" / "eval" / "eval.db"
MIN_SAMPLES = 20
WINDOW_DAYS = 3


def daily(conn) -> dict:
    rows = conn.execute(
        "SELECT asset, date(ts,'unixepoch') d, ts, close FROM ohlcv_daily "
        "WHERE close IS NOT NULL AND close>0 ORDER BY ts").fetchall()
    out = {}
    for asset, d, ts, close in rows:
        out.setdefault((asset or '').strip().upper(), {})[d] = float(close)
    return {a: dict(sorted(m.items())) for a, m in out.items()}


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def main() -> int:
    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2
    EVAL_DB.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(str(EVAL_DB), timeout=30)
    dst.execute("""CREATE TABLE IF NOT EXISTS impact_forecast(
        news_id INTEGER PRIMARY KEY, asset TEXT, event_ts INTEGER, action TEXT,
        pred_p50 REAL, pred_lo REAL, pred_hi REAL, actual_pct REAL,
        covered INTEGER, samples INTEGER, built_at TEXT)""")
    dst.execute("DELETE FROM impact_forecast")

    book = daily(src)
    events = src.execute(
        "SELECT id, ts, asset, action FROM news_events "
        "WHERE action IN ('BUY','SELL') AND ts IS NOT NULL ORDER BY ts").fetchall()

    # 先算每条事件的实际幅度（事件→次日收盘进场，之后 3 日）
    actuals = []
    for nid, ts, asset, action in events:
        a = (asset or '').strip().upper()
        s = book.get(a)
        if not s:
            continue
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        days = list(s)
        entry = next((d for d in days if d > day), None)
        if not entry:
            continue
        i = days.index(entry)
        if i + WINDOW_DAYS >= len(days):
            continue
        e = s[entry]
        act = abs(s[days[i + WINDOW_DAYS]] / e - 1) * 100 if e else None
        if act is None:
            continue
        actuals.append((nid, int(ts), a, action, act))

    # 逐条用「之前」的样本给预测（防前视）
    out = []
    hist: dict = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for nid, ts, a, action, act in actuals:
        key = (a, action)
        prior = [x[2] for x in hist.get(key, []) if x[1] < ts]
        if len(prior) < MIN_SAMPLES:
            out.append((nid, a, ts, action, None, None, None, act, None, len(prior), now))
        else:
            p50, lo, hi = pct(prior, 0.5), pct(prior, 0.25), pct(prior, 0.75)
            covered = 1 if lo <= act <= hi else 0
            out.append((nid, a, ts, action, p50, lo, hi, act, covered, len(prior), now))
        hist.setdefault(key, []).append((nid, ts, act))

    dst.executemany("INSERT OR REPLACE INTO impact_forecast VALUES(?,?,?,?,?,?,?,?,?,?,?)", out)
    dst.commit()

    total = len(out)
    pred_n = sum(1 for r in out if r[8] is not None)
    cov = sum(1 for r in out if r[8] == 1)
    print(f"事件总数 {total}，其中给出幅度预测 {pred_n} 条（样本≥{MIN_SAMPLES}）")
    if pred_n:
        print(f"区间覆盖率 = {cov / pred_n * 100:.1f}%  (n={pred_n})")
        print("读数：≈50% 表示区间宽度合理；>>50% 区间太宽没用；<<50% 区间太窄不可信")
        med = [r[4] for r in out if r[4] is not None]
        print(f"预测中位幅度 = {pct(med, 0.5):.2f}%")
    else:
        print("还没有任何事件的历史样本够 20 条，暂时无法给预测")
    dst.close()
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
