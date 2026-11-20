#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fed_h41.py —— 美联储资产负债表（H.4.1 周报）

为什么值得加：
  美联储总资产是美元流动性最粗的那根水管。它扩张，风险资产和黄金通常顺风；
  它收缩，估值端承压。美元、黄金、加密三个品种共用这一个上游变量，
  FRED 的 WALCL 现在在本机拿不到（fred.stlouisfed.org 直连 000），
  所以直接从美联储官网抓 H.4.1 —— 同一手来源，且不依赖 key。

口径：
  H.4.1 表里的数字单位是 **百万美元**，这里原样入库，meta 里写明单位，
  不换算、不四舍五入到"万亿"去糊弄。

用法：
  python3 fed_h41.py
  python3 fed_h41.py --dry-run
"""
import argparse
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import external_data

URL = "https://www.federalreserve.gov/releases/h41/current/h41.htm"
SOURCE = "fed_h41"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
           "Accept-Language": "en-US,en;q=0.9"}

# 表内标签 -> 我们的指标名
FIELDS = {
    "fed_total_assets": "Total assets",
    "fed_securities_held": "Securities held outright",
    "fed_treasury_holdings": "U.S. Treasury securities",
}


def fetch_html(tries: int = 4) -> Optional[str]:
    import requests
    s = requests.Session()
    s.trust_env = False          # 办公代理会把这页拦掉，必须直连
    for i in range(tries):
        try:
            r = s.get(URL, headers=HEADERS, timeout=25)
            if r.status_code == 200 and len(r.text) > 10000:
                return r.text
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def parse(html: str) -> Dict[str, Any]:
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&#xa0;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text)

    # 报表日期："Averages of daily figures Wednesday Nov 11, 2026"
    m = re.search(r"(?:Wednesday|Thursday|Friday|Monday|Tuesday|Saturday|Sunday)\s+"
                  r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})", text)
    as_of = None
    if m:
        as_of = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")

    out: Dict[str, Any] = {"as_of": as_of, "values": {}}
    for name, label in FIELDS.items():
        # 标签后面跟的是"当周值 变化 去年同期"三个数，取第一个
        # 标签后面可能带脚注标记（"Securities held outright 1"），先吃掉再取值
        mm = re.search(re.escape(label) + r"(?:\s*(?:\([0-9]\)|[0-9]))?\s+([\d,]{6,})", text)
        if mm:
            out["values"][name] = float(mm.group(1).replace(",", ""))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    html = fetch_html()
    if not html:
        print("[FATAL] H.4.1 抓不到（网站不可达），本次不写库")
        return 1

    data = parse(html)
    if not data["values"]:
        print("[FATAL] 页面拿到了但一个字段都没解析出来，格式可能变了 —— 不写库，等人来看")
        return 1

    as_of = data["as_of"] or datetime.now()
    ts = int(time.mktime(as_of.timetuple()))        # 周报按发布日打点
    for name, value in data["values"].items():
        print(f"  {name:<22} {value:>15,.0f} 百万美元  @ {as_of:%Y-%m-%d}")
        if args.dry_run:
            continue
        external_data.ingest_external(
            asset="*", indicator=name, records=[{"ts": ts, "value": value}],
            source=SOURCE,
            meta={"unit": "million USD", "report": "H.4.1 weekly",
                  "as_of": as_of.strftime("%Y-%m-%d"), "url": URL})
    if not args.dry_run:
        print("  已写入 external_indicators（source=fed_h41）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
