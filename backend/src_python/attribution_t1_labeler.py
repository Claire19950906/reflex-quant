#!/usr/bin/env python3
"""
归因 T+1 验证标签

问题：event_move_attribution 记录了「某次异动归因到某条新闻」，但从未标注
      这条归因事后对不对，所以「归因能力」只能算覆盖率，算不了准确率。

做法：对每条归因，用日线算出「异动之后」的走势（进场一律推到异动次日收盘，铁律2），
     落三张可验证标签：
       1 异动是否延续（continued）：3 日后是否还朝同方向走
       2 新闻方向是否正确（news_dir_correct）：新闻标的 BUY/SELL 与 3 日后实际方向是否一致
       3 归因是否可分离（separable）：同期是否有其他事件也能解释这次异动（时间窗内多条新闻）

产出表 event_attribution_t1，供尺子直接读，不改任何生产逻辑。

用法：
  python3 attribution_t1_labeler.py            # 打标签
  python3 attribution_t1_labeler.py --show      # 看汇总
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"


def daily_closes(conn) -> dict:
    rows = conn.execute(
        "SELECT asset, date(ts,'unixepoch') d, ts, close FROM ohlcv_daily "
        "WHERE close IS NOT NULL AND close>0 ORDER BY ts").fetchall()
    out: dict = {}
    for asset, d, ts, close in rows:
        out.setdefault(asset.strip().upper(), {})[d] = float(close)
    for a in out:
        out[a] = dict(sorted(out[a].items()))
    return out


def label(conn) -> int:
    book = daily_closes(conn)
    rows = conn.execute(
        "SELECT a.id, a.asset, a.move_ts, a.move_pct, a.move_kind, "
        "       COALESCE(ne.action, a.ev_action) AS news_action, a.news_event_id "
        "FROM event_move_attribution a "
        "LEFT JOIN news_events ne ON ne.id = a.news_event_id").fetchall()
    conn.execute("""CREATE TABLE IF NOT EXISTS event_attribution_t1(
        attribution_id INTEGER PRIMARY KEY, asset TEXT, move_date TEXT, move_kind TEXT,
        move_pct REAL, move_dir TEXT, entry_date TEXT, fwd1 REAL, fwd3 REAL,
        continued INTEGER, news_action TEXT, news_dir_correct INTEGER,
        separable INTEGER, labelled_at TEXT)""")
    conn.execute("DELETE FROM event_attribution_t1")   # 口径变了就整体重算，避免新旧混算
    out = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for aid, asset, mts, mpct, kind, ev_action, news_id in rows:
        a = (asset or "").strip().upper()
        series = book.get(a)
        if not series or not mts:
            continue
        move_date = datetime.fromtimestamp(int(mts), tz=timezone.utc).strftime("%Y-%m-%d")
        days = list(series)
        # 进场：异动日之后的第一个收盘（铁律2）
        entry = next((d for d in days if d > move_date), None)
        if not entry:
            continue
        i = days.index(entry)
        if i + 1 >= len(days):
            continue
        e = series[entry]
        fwd1 = (series[days[i + 1]] / e - 1) if e else None
        fwd3 = (series[days[i + 3]] / e - 1) if i + 3 < len(days) and e else None
        move_dir = "UP" if (mpct or 0) > 0 else "DOWN"
        continued = None
        if fwd3 is not None:
            continued = 1 if ((fwd3 > 0) == (move_dir == "UP")) else 0
        news_dir_correct = None
        if ev_action in ("BUY", "SELL") and fwd3 is not None:
            news_dir_correct = 1 if ((ev_action == "BUY") == (fwd3 > 0)) else 0
        # 可分离：只看 ±15 分钟窗口。窗口越宽可分离率越低（实测 15min 54% / 1h 28% / 24h 3.5%），
        # 所以「分离不出来」是粒度问题，不是能力问题。
        win = conn.execute(
            "SELECT count(*) FROM news_events WHERE ts BETWEEN ? AND ? AND is_noise=0 "
            "AND (asset = ? OR assets LIKE ?)",
            (int(mts) - 900, int(mts) + 900, asset, f"%{asset}%")).fetchone()[0]
        separable = 0 if win > 1 else 1
        out.append((aid, asset, move_date, kind, mpct, move_dir, entry,
                    fwd1, fwd3, continued, ev_action, news_dir_correct, separable, now))
    conn.executemany("INSERT OR REPLACE INTO event_attribution_t1 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    conn.commit()
    return len(out)


def show(conn) -> None:
    total = conn.execute("SELECT count(*) FROM event_attribution_t1").fetchone()[0]
    if not total:
        print("还没有标签，先跑打标")
        return
    print(f"归因 T+1 标签：{total} 条")
    for name, col in (("异动延续率", "continued"),
                      ("新闻方向正确率", "news_dir_correct"),
                      ("归因可分离率", "separable")):
        n, hit = conn.execute(
            f"SELECT count(*), sum({col}) FROM event_attribution_t1 WHERE {col} IS NOT NULL").fetchone()
        n = n or 0
        hit = hit or 0
        acc = f"{hit / n * 100:.1f}%" if n else "—"
        judge = "样本不足" if n < 200 else "已达标" if hit / n >= 0.55 else "未达线"
        print(f"  {name:<12} {acc:>7}  (n={n})  {judge}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(DB), timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        if args.show:
            show(conn)
        else:
            n = label(conn)
            print(f"已写入 {n} 条归因标签 → event_attribution_t1")
            show(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
