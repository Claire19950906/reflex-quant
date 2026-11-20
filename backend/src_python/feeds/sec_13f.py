#!/usr/bin/env python3
"""sec_13f.py — 知名投资人 13F 季度持仓（SEC EDGAR 公开数据）。

为什么单独做：
  聪明钱面板是 Hyperliquid 钱包的链上实时成交；巴菲特、阿克曼、Burry 这些人
  不上链、没有 0x 地址，唯一权威的公开口径是 SEC 的 13F-HR 季度持仓表。
  这里不做任何推算：只抓 SEC 原始 XML，按 CUSIP 聚合，季环比也只用相邻两期
  真实申报值对比。

用法：
  python3 -m feeds.sec_13f            # 全量刷新（默认 12 家）
  python3 -m feeds.sec_13f --show     # 只打印，不写库
"""
from __future__ import annotations

import json
import os
import re as _re
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BACKEND_DIR, "trident_event_bus.db")
UA = "Trident Research claire@example.com"

# 只放“公开、长期可查、13F 连续申报”的人/机构
MANAGERS: List[Tuple[str, str, str]] = [
    ("Berkshire Hathaway（巴菲特）", "0001067983", "Warren Buffett"),

    ("Carl Icahn（伊坎）", "0000921669", "Carl Icahn"),
    ("Viking Global（哈维·桑德勒）", "0001103804", "Viking Global"),
    ("Point72（科恩）", "0001603466", "Steve Cohen"),
    ("Bridgewater（达利欧）", "0001350694", "Ray Dalio"),
    ("Baupost Group（卡拉曼）", "0001061768", "Seth Klarman"),
    ("Third Point（洛布）", "0001040273", "Daniel Loeb"),
    ("Appaloosa（特珀）", "0001656456", "David Tepper"),
    ("Duquesne Family Office（德鲁肯米勒）", "0001536411", "Stanley Druckenmiller"),
    ("Himalaya Capital（李录）", "0001709323", "Li Lu"),
    ("Gates Foundation Trust（盖茨）", "0001166559", "Bill Gates"),
    ("Lindsell Train（尼克·特雷恩）", "0001112520", "Nick Train"),
]

_CREATE = """
CREATE TABLE IF NOT EXISTS sec_13f_holdings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    manager       TEXT NOT NULL,
    person        TEXT,
    cik           TEXT NOT NULL,
    report_date   TEXT NOT NULL,   -- 13F periodOfReport, YYYY-MM-DD
    filed_date    TEXT NOT NULL,
    accession     TEXT NOT NULL,
    issuer        TEXT,
    cusip         TEXT,
    value_usd     REAL,            -- 13F XML 的 value 字段已是美元（不是纸质表的千美元）
    shares        REAL,
    put_call      TEXT,
    created_at    INTEGER NOT NULL,
    UNIQUE(cik, report_date, cusip, put_call)
);
"""


def _curl(url: str, timeout: int = 12) -> Optional[str]:
    time.sleep(3.0)  # SEC 对突发请求敏感，限速（重试会被直接 TLS 重置）
    for _ in range(2):
        p = subprocess.run(
            ["/usr/bin/curl", "-4", "-sS", "--http1.1", "--compressed", "-m", str(timeout),
             "-A", UA, url],
            capture_output=True, text=True, timeout=timeout + 3,
        )
        out = (p.stdout or "").strip()
        if out:
            return out
        time.sleep(1.0)
    print(f"[13F] curl miss: {url[:90]}", flush=True)
    return None


def _json(url: str) -> Optional[Any]:
    raw = _curl(url)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _recent_13f(cik: str, limit: int = 3) -> List[Dict[str, str]]:
    d = _json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if not d:
        return []
    r = (d.get("filings") or {}).get("recent") or {}
    out = []
    for i, form in enumerate(r.get("form") or []):
        if form != "13F-HR":          # 13F-NT 是"无持仓通知"，没有信息表
            continue
        out.append({
            "accession": r["accessionNumber"][i],
            "filed": r["filingDate"][i],
        })
        if len(out) >= limit:
            break
    return out


def _iso(period: str) -> str:
    """SEC 的 periodOfReport 是 MM-DD-YYYY；统一成 YYYY-MM-DD。"""
    p = period.strip()
    if len(p) == 10 and p[2] == "-" and p[5] == "-":
        return f"{p[6:10]}-{p[0:2]}-{p[3:5]}"
    return p


def _period_of_report(cik: str, accession: str) -> Optional[str]:
    nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    raw = _curl(f"{base}/primary_doc.xml")
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
        for el in root.iter():
            if el.tag.split("}")[-1] == "periodOfReport" and el.text:
                return el.text.strip()
    except ET.ParseError:
        return None
    return None


def _filing_from_txt(cik: str, accession: str) -> Optional[Tuple[str, str]]:
    """一次请求拿整份申报（.txt 是 SEC 的完整提交文件，含 primary + 信息表）。

    SEC 对 3 次以上的高频抓取会直接 TLS 重置，所以"一个 filing 一个请求"
    比分开抓 primary_doc + index + infotable 稳得多。
    """
    nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    raw = _curl(f"{base}/{accession}.txt", timeout=60)
    if not raw or "informationTable" not in raw:
        return None
    m = _re.search(r"<periodOfReport>\s*([0-9\-]+)\s*</periodOfReport>", raw)
    period = m.group(1) if m else None
    chunks = []
    for mm in _re.finditer(r"<informationTable[^>]*>", raw):
        end = raw.find("</informationTable>", mm.start())
        if end < 0:
            continue
        body = raw[mm.start():end + len("</informationTable>")]
        if "xmlns" not in body[:200]:
            # SGML 包裹时元素可能没有命名空间声明，补一个
            body = body.replace("<informationTable",
                                '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"', 1)
        chunks.append((len(body), body))
    if not chunks:
        return None
    best = max(chunks, key=lambda x: x[0])[1]
    if not period:
        return None
    return period, best


def _holdings_xml(cik: str, accession: str) -> Optional[str]:
    nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    idx = _json(f"{base}/index.json")
    if not idx:
        return None
    names = [it["name"] for it in (idx.get("directory", {}).get("item") or [])]
    # 信息表通常是数字命名的 xml 或 infotable/form13fInfoTable
    cands = [n for n in names if n.lower().endswith(".xml")
             and "primary" not in n.lower()]
    if not cands:
        return None
    cands.sort(key=lambda n: ("infotable" not in n.lower(), "form13f" not in n.lower(), n))
    for n in cands[:4]:
        raw = _curl(f"{base}/{n}")
        if raw and "informationTable" in raw:
            return raw
    return None


def _parse(raw: str) -> List[Dict[str, Any]]:
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    for tbl in root.iter():
        if tbl.tag.split("}")[-1] != "infoTable":
            continue
        rec: Dict[str, Any] = {"issuer": "", "cusip": "", "value": 0.0,
                               "shares": 0.0, "put_call": ""}
        for el in tbl.iter():
            tag = el.tag.split("}")[-1]
            txt = (el.text or "").strip()
            if tag == "nameOfIssuer":
                rec["issuer"] = txt
            elif tag == "cusip":
                rec["cusip"] = txt
            elif tag == "value":
                try:
                    rec["value"] = float(txt.replace(",", ""))
                except ValueError:
                    pass
            elif tag == "sshPrnamt":
                try:
                    rec["shares"] = float(txt.replace(",", ""))
                except ValueError:
                    pass
            elif tag == "putCall":
                rec["put_call"] = txt.upper()
        if rec["cusip"]:
            out.append(rec)
    return out


def refresh(show_only: bool = False) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute(_CREATE)
    now = int(time.time())
    summary = {"managers": 0, "rows": 0, "errors": []}
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), "")
    for name, cik, person in MANAGERS:
        if only and only not in name:
            continue
        filings = _recent_13f(cik, limit=2)
        if not filings:
            time.sleep(2)
            filings = _recent_13f(cik, limit=2)   # SEC 偶发 TLS/500，重试一次
        if not filings:
            summary["errors"].append(f"{name}: 无 13F-HR")
            continue
        got = 0
        for f in filings:
            one = _filing_from_txt(cik, f["accession"])
            if one:
                period, raw = one
            else:   # 兜底：老的两步法（.txt 缺失时才多花两次请求）
                period = _period_of_report(cik, f["accession"])
                raw = _holdings_xml(cik, f["accession"])
            if not period or not raw:
                continue
            rows = _parse(raw)
            if not rows:
                continue
            report_date = _iso(period)
            # 单位判别：SEC 的 XML schema 写「美元」，但仍有 filer 按旧纸质表
            # 报「千美元」（如 Duquesne）。用每股隐含价格的中位数判定：
            # 绝大多数股票 > $5，中位数 < 5 只能是千美元口径。
            _ratios = sorted(r["value"] / r["shares"] for r in rows
                             if r["shares"] and r["value"])
            _med = _ratios[len(_ratios) // 2] if _ratios else 1.0
            _scale = 1000.0 if _med < 5 else 1.0
            if not show_only:
                conn.executemany(
                    "INSERT OR REPLACE INTO sec_13f_holdings "
                    "(manager, person, cik, report_date, filed_date, accession, issuer, "
                    " cusip, value_usd, shares, put_call, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(name, person, cik, report_date, f["filed"], f["accession"],
                      r["issuer"], r["cusip"], r["value"] * _scale, r["shares"],
                      r["put_call"], now) for r in rows],
                )
            got += len(rows)
            if not show_only:
                conn.commit()
        if got:
            summary["managers"] += 1
            summary["rows"] += got
            if show_only:
                print(f"[13F] {name}: {got} 行")
    if not show_only:
        conn.commit()
    conn.close()
    return summary


if __name__ == "__main__":
    out = refresh(show_only="--show" in sys.argv)
    print(f"[sec_13f] managers={out['managers']} rows={out['rows']} "
          f"errors={len(out['errors'])}")
    for e in out["errors"][:6]:
        print("  ⚠", e)
