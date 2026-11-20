#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_fred_history.py — 把 FRED 日频序列回填到"能算分位"的长度（v461）

为什么
------
线上 `macro_snapshots` 里 FRED 日频序列只有 38 天（2026-10-03 建库起每天追加一条）。
而 v460/v461 的分量都用"历史分位"判高低：实际利率 83% 分位、信用利差 20 日变化……
拿 38 天算出来的 83% 其实是"最近 38 天里的 83%"，不是"过去三年偏高"。窗口太短会让
分量在几个月尺度上静默失真，而且从读数上看不出来。

怎么拉
------
优先走 FRED 官方 JSON API（`/fred/series/observations`，支持 `observation_start` +
`limit=20000`，和线上 `fred_feed` 共用同一个 key，走的是已经在用的通路）。
`fredgraph.csv` 在这台机器上被 TLS 掐断（UNEXPECTED_EOF / read timeout），只作兜底。

写库
----
写进 `macro_snapshots`，`source='fred_csv'`，payload 标 `backfill=True`；
`alt_fundamental._ms_series` 按天去重、同日留最后一条，和实时行不冲突。

用法
----
    python3 scripts/oneoff/backfill_fred_history.py --dry-run --years 3
    python3 scripts/oneoff/backfill_fred_history.py --apply  --years 3
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "src_python"))
DB = BACKEND / "trident_event_bus.db"

# 只回填"被基本面分量真正用到"的日频序列（月/季度序列分位口径不同，不放这里）
SERIES = {
    "DGS2": ("dgs2", "%"),
    "DGS5": ("dgs5", "%"),
    "DGS10": ("dgs10", "%"),
    "DGS30": ("dgs30", "%"),
    "T10Y2Y": ("t10y2y", "%"),
    "DFII10": ("tips10y", "%"),
    "T10YIE": ("t10yie", "%"),
    "BAMLH0A0HYM2": ("hy_oas", "%"),
    "BAA10Y": ("baa10y", "%"),
    "DTWEXBGS": ("dxy", ""),
    "VIXCLS": ("vix", ""),
    "SOFR": ("sofr", "%"),
    "DEXJPUS": ("usdjpy", ""),
    "DEXUSEU": ("usdeur", ""),
}


def _fetch_api(series_id: str, start: str, api_key: str, retries: int = 9):
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={api_key}&file_type=json"
           f"&observation_start={start}&limit=20000&sort_order=asc")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TridentAgentMVP/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            obs = data.get("observations") or []
            out = []
            for o in obs:
                v = o.get("value", ".")
                if v in ("", "."):
                    continue
                try:
                    out.append((o["date"], float(v)))
                except (KeyError, ValueError):
                    continue
            if not out:
                raise ValueError("观测为空")
            return out
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{series_id}: {type(last).__name__}: {last}")


def _ts_for(day: str) -> int:
    # 用当地中午对齐，避免 00:00 与"当天实时写入"的时刻排序错乱
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--years", type=float, default=3.0)
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    import config  # noqa: E402  (需要 sys.path 已插入)
    key = config.FRED_API_KEY
    if not key:
        print("未配置 FRED_API_KEY，无法回填")
        return 1

    start = (datetime.now() - timedelta(days=int(args.years * 365))).strftime("%Y-%m-%d")
    print(f"库: {DB}\n起始日: {start}（{args.years:g} 年）\n")

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    fetched = written = 0
    ok = fail = 0
    try:
        for sid, (mk, unit) in SERIES.items():
            try:
                pts = _fetch_api(sid, start, key)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print(f"  [失败] {sid:14s} {exc}")
                continue
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
                "WHERE metric_key=? AND value IS NOT NULL", (mk,))}
            new = [(d, v) for d, v in pts if d not in have]
            fetched += len(pts)
            print(f"  {sid:14s} → {mk:11s} 抓 {len(pts):5d} 天 | 库内 {len(have):3d} 天 | "
                  f"新增 {len(new):5d} 天 | 最新 {pts[-1][0]} = {pts[-1][1]:g}")
            if not args.apply or not new:
                continue
            payload = json.dumps({"series_id": sid, "backfill": True,
                                  "source": "fred_api"}, ensure_ascii=False)
            conn.executemany(
                "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(_ts_for(d), "fred", "fred_csv", mk, v, unit, "ok", payload) for d, v in new])
            conn.commit()
            written += len(new)
    finally:
        conn.close()
    print(f"\n[done] 成功 {ok} / 失败 {fail} 条序列，抓 {fetched:,} 条观测，写入 {written:,} 行"
          + ("" if args.apply else "（dry-run，未写库）"))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
