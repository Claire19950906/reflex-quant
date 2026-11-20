#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sec_insider.py —— SEC Form 4 内部人交易（官方免费，免 key）

为什么加：
  Form 4 是美股唯一"必须披露、且有法律后果"的真实资金行为。内部人集中买入
  很少是噪音；而卖方信号要打折看（期权行权、税务代扣都会产生卖单）。
  所以这里分两条口径分别统计，不合并成一个"净买入"糊过去。

口径：
  - 只统计 nonDerivativeTransaction 里的 P（公开市场买入）与 S（公开市场卖出）；
  - 单位是**股数**，不是金额（价格字段常缺失，硬算金额会掺假）；
  - 窗口 30 天，按 filing-date 归日。

限制（写清楚，免得误用）：
  只能看到向 SEC 提交的部分（10b5-1 计划内的交易也在里面），且只覆盖关注标的；
  解析失败的 filing 不猜、不计入，覆盖率会在日志里打出来。

已知坑：sec.gov 对本机首次连接常 TLS reset，必须 `--tlsv1.2` 且带真实 UA + 联系方式。

用法：
  python3 sec_insider.py                     # 关注标的全部
  python3 sec_insider.py --symbols NVDA
  python3 sec_insider.py --dry-run
"""
import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import external_data

SOURCE = "sec_form4"
# SEC 要求 UA 里带机构名 + 真实邮箱，否则直接拒连（实测带 Browser UA 反而 000）
UA = "ClaireResearch admin@claire-research.com"
WATCH = ["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "MU", "META", "AMZN", "GOOGL"]
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE = "/Users/claire/.trident/cache/sec_tickers.json"
WINDOW_DAYS = 30
MAX_FILINGS = 8          # 每个标的最近 8 份，够看趋势，也不至于打爆


def curl(url: str, tries: int = 4) -> Optional[str]:
    cmd = ["curl", "-sL", "-m", "30", "--noproxy", "*", "--compressed", "--tlsv1.2",
           "-H", f"User-Agent: {UA}", "-H", "Accept: application/xml, text/xml, */*", url]
    for i in range(tries):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if out.stdout.strip():
                return out.stdout
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def ticker_to_cik() -> Dict[str, str]:
    import os
    raw = None
    if os.path.exists(CACHE):
        raw = open(CACHE, encoding="utf-8").read()
    if not raw:
        raw = curl(TICKERS_URL)
        if raw:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            open(CACHE, "w", encoding="utf-8").write(raw)
    if not raw:
        return {}
    data = json.loads(raw)
    out = {}
    for item in data.values():
        out[str(item["ticker"]).upper()] = str(item["cik_str"]).zfill(10)
    return out


def recent_filings(cik: str) -> List[Tuple[str, str, str]]:
    """返回 [(filing_date, accession, cik)]，按日期倒序。

    主通道用 data.sec.gov 的 submissions JSON：一次请求能拿到全部近期申报，
    比 atom 逐页翻稳得多（atom 在本机经常 000）。atom 作为兜底。
    """
    cutoff = (datetime.now() - timedelta(days=WINDOW_DAYS)).date()
    out: List[Tuple[str, str, str]] = []

    raw = curl(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if raw:
        try:
            recent = json.loads(raw)["filings"]["recent"]
            for form, fdate, acc in zip(recent["form"], recent["filingDate"],
                                        recent["accessionNumber"]):
                if form not in ("4", "4/A"):
                    continue
                try:
                    fd = datetime.strptime(fdate, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if fd < cutoff:
                    continue
                out.append((fdate, acc, cik))
                if len(out) >= MAX_FILINGS:
                    break
        except (KeyError, TypeError, ValueError):
            out = []

    if out:
        return out

    # 兜底：atom 列表
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={cik}&type=4&owner=include&count={MAX_FILINGS}&output=atom")
    xml = curl(url)
    if not xml:
        return []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        d = re.search(r"<filing-date>([\d-]+)</filing-date>", e)
        acc = re.search(r"<accession-number>([\d-]+)</accession-number>", e)
        if not d or not acc:
            continue
        try:
            fd = datetime.strptime(d.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if fd < cutoff:
            continue
        out.append((d.group(1), acc.group(1), cik))
    return out


def primary_doc(acc: str, cik: str) -> Optional[str]:
    idx = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/index.json"
    raw = curl(idx)
    if not raw:
        return None
    try:
        items = json.loads(raw).get("directory", {}).get("item", [])
    except json.loads:
        return None
    for it in items:
        name = it.get("name", "")
        if name.endswith(".xml") and not name.startswith("R"):
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{name}"
    return None


def parse_txn(xml: str) -> Tuple[float, float]:
    """返回 (买入股数, 卖出股数)"""
    buys = sells = 0.0
    for block in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S):
        code = re.search(r"<transactionCode>([A-Z])</transactionCode>", block)
        shares = re.search(r"<transactionShares>\s*<value>([\d.]+)</value>", block)
        if not code or not shares:
            continue
        try:
            n = float(shares.group(1))
        except ValueError:
            continue
        if code.group(1) == "P":
            buys += n
        elif code.group(1) == "S":
            sells += n
    return buys, sells


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or WATCH
    ciks = ticker_to_cik()
    if not ciks:
        print("[FATAL] 拿不到 ticker→CIK 映射，本次不写库")
        return 1

    for sym in syms:
        cik = ciks.get(sym)
        if not cik:
            print(f"  {sym} 找不到 CIK，跳过")
            continue
        files = recent_filings(cik)
        if not files:
            print(f"  {sym} 近 {WINDOW_DAYS} 天无记录（网络不可达或确实没提交，一律不写 0）")
            continue
        buys = sells = 0.0
        ok = 0
        for fdate, acc, c in files:
            doc = primary_doc(acc, c)
            if not doc:
                continue
            xml = curl(doc)
            if not xml:
                continue
            b, s = parse_txn(xml)
            buys += b
            sells += s
            ok += 1
        if ok == 0:
            print(f"  {sym} {len(files)} 份 filing 全部解析失败，不写库")
            continue
        print(f"  {sym:<6} {len(files)} 份 filing（成功解析 {ok}）| 买入 {buys:,.0f} 股 / 卖出 {sells:,.0f} 股")
        if args.dry_run:
            continue
        ts = int(time.mktime(datetime.now().replace(hour=0, minute=0, second=0).timetuple())) * 1000
        external_data.ingest_external(
            asset=sym, indicator="insider_buy_shares_30d",
            records=[{"ts": ts, "value": buys}], source=SOURCE,
            meta={"window": f"{WINDOW_DAYS}d", "filings": ok, "unit": "shares"})
        external_data.ingest_external(
            asset=sym, indicator="insider_sell_shares_30d",
            records=[{"ts": ts, "value": sells}], source=SOURCE,
            meta={"window": f"{WINDOW_DAYS}d", "filings": ok, "unit": "shares"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
