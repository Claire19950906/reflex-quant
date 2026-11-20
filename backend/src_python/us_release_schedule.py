#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
us_release_schedule.py —— 美联储 FOMC 会议前瞻日程（官方页，免 key）

为什么做：
  `macro_calendar_ff.py` 依赖的 ForexFactory 本周 feed 只覆盖**到当天为止**，
  `_nextweek.xml` / `_thisweekextended.xml` 两个变体已被源站下线（稳定 404），
  所以系统原先对"下周有没有议息"是瞎的 —— 而这恰恰是黄金、美元最在意的事件。

  替代源试过：BLS 官方日程页、Investing(forexprostools) 日历、TradingEconomics、
  TradingView 日历接口 —— **全部对脚本访问返回 403**。只有美联储自己的 FOMC
  日程页可稳定取到（需浏览器 UA）。

  所以这里只补 FOMC 这一块真实拿得到的前瞻；CPI/NFP 的下周日期拿不到，
  不编、不留空壳，表现为缺失。

写什么：
  `fomc_days_to_next`      距下一次议息决议日还有几天（0=今天，整数，asset='US_MACRO'）
  `fomc_meetings_90d`      未来 90 天内决议日个数
  meta 里带下一次的日期与全年日程。

口径：
  - 决议日取会议**第二天**（声明与发布会当天），不是第一天；
  - 日期按美国东部时间理解，这里只做"天数"用途，不做盘中时点精确换算。
"""
import argparse
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import db
import external_data

SOURCE = "fed_fomc_calendar"
URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
ASSET = "US_MACRO"

MONTHS = {m: i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"), 1)}


def fetch_html(timeout: int = 20) -> str:
    """必须走 curl：requests 的 trust_env 会命中办公代理，Fed 站会直接断连。"""
    last = ""
    for attempt in range(3):
        if attempt:
            time.sleep(2 * attempt)
        out = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "--noproxy", "*", "-A", UA,
             "-H", "Accept: text/html,application/xhtml+xml", URL],
            capture_output=True, text=True)
        if out.returncode == 0 and out.stdout:
            return out.stdout
        last = f"rc={out.returncode} {out.stderr[:60]}"
    # Fed 站的 TLS 握手会偶发失败（rc=35），重试 3 次仍不行才算真失败
    raise RuntimeError(f"curl 失败 {last}")


def parse_meetings(html: str) -> List[Tuple[int, datetime]]:
    """从各年份区块里抠出"月 起-止"，返回 [(year, 决议日)]。

    v335 修过两个坑：
      1. 只认 **区间形式**（如 `January 27-28`）。单日期形式的 `February 18`
         是"会议纪要发布日"（原文写作 `(Released February 18, 2026)`），不是会议，
         早先版本把它当会议，导致 2026 年凭空多出 2 月一场。
      2. 用例时间窗按"下一个年份标题"切，不按字符数切，否则下半年会议被截断。
    """
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean)

    # 年份区块：从 "YYYY FOMC Meetings" 到下一个 "YYYY FOMC Meetings"
    heads = [(m.start(), int(m.group(1)))
             for m in re.finditer(r"(20\d{2}) FOMC Meetings", clean) if 20 <= int(m.group(1)[2:]) <= 30]
    results: List[Tuple[int, datetime]] = []
    for i, (pos, year) in enumerate(heads):
        nxt = heads[i + 1][0] if i + 1 < len(heads) else len(clean)
        block = clean[pos:nxt]
        for mon, d1, d2 in re.findall(
                r"(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+(\d{1,2})\s*[-–]\s*(\d{1,2})", block):
            try:
                results.append((year, datetime(year, MONTHS[mon], int(d2))))
            except ValueError:
                continue

    # 去重 + 3 天内视为同一场（防止同一场被解析成两个日期），保留靠后的=决议日
    results.sort(key=lambda x: x[1])
    uniq: List[Tuple[int, datetime]] = []
    for y, d in results:
        if uniq and (d.date() - uniq[-1][1].date()).days <= 3:
            uniq[-1] = (y, d) if d > uniq[-1][1] else uniq[-1]
            continue
        uniq.append((y, d))
    return uniq


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    html = fetch_html()
    meetings = parse_meetings(html)
    if not meetings:
        print("  解析到 0 场会议 —— 页面结构可能变了，本次不写库（不拿假数据凑）")
        return 1

    now = datetime.now()
    today = now.date()
    upcoming = [d for _, d in meetings if d.date() >= today]
    print(f"  解析到 {len(meetings)} 场会议（{meetings[0][1]:%Y-%m-%d} ~ {meetings[-1][1]:%Y-%m-%d}）")
    if not upcoming:
        print("  没有未来会议 —— 不写库")
        return 1

    nxt = upcoming[0]
    days_to_next = (nxt.date() - today).days
    within_90 = sum(1 for d in upcoming if (d.date() - today).days <= 90)
    print(f"  下一次决议日 {nxt:%Y-%m-%d}（距今 {days_to_next} 天），未来 90 天 {within_90} 场")

    if args.dry_run:
        return 0

    ts_ms = int(time.time() * 1000)
    cal = [{"date": d.strftime("%Y-%m-%d")} for d in upcoming[:8]]
    external_data.ingest_external(
        asset=ASSET, indicator="fomc_days_to_next",
        records=[{"ts": ts_ms, "value": float(days_to_next)}],
        source=SOURCE,
        meta={"next_meeting": nxt.strftime("%Y-%m-%d"), "calendar": cal,
              "note": "决议日=会议第二天（声明+发布会当天）"})
    external_data.ingest_external(
        asset=ASSET, indicator="fomc_meetings_90d",
        records=[{"ts": ts_ms, "value": float(within_90)}],
        source=SOURCE, meta={"window": "90d", "calendar": cal})
    print("  已写入 fomc_days_to_next / fomc_meetings_90d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
