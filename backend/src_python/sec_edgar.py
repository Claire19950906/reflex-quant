#!/usr/bin/env python3
"""SEC EDGAR 官方公告采集器（8-K / 10-Q / 10-K）。

为什么需要：
  · 现在关于美股公司的信息，全是新闻站二手转述（"英伟达宣布…"），
    既无原文，也无法核对。8-K 是公司向 SEC 提交的**一手重大事件公告**：
    财报发布、重大合同、董监高变动、退市风险，都在里面，且比新闻早。
  · 它是唯一不需要付费、不需要授权、由监管机构强制披露的信息源。

写入 raw_news，source='sec_edgar'，status='PENDING'（交给门卫判定）。
正文按"公司 + 表单 + 事项代码中文含义"拼装，不带任何推断。
去重：content_fingerprint（accession number），同一份公告只入一次。

限制：data.sec.gov 需要 User-Agent；本机连接会偶发 TLS reset，
      靠 5 次指数退避重试补回（实测第 2 次成功）。
"""
import hashlib
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import realtime_filter

requests.Session.trust_env = False
BASE = "https://data.sec.gov/submissions/CIK{cik}.json"
HEADERS = {"User-Agent": "trident-research/1.0 (claire@zhang.local)",
           "Accept": "application/json"}
TIMEOUT = 15
SOURCE = "sec_edgar"
CST = timezone(timedelta(hours=8))

# CIK 与公司：这套系统盯的是这几家（腾讯行情里也在盯 NVDA/TSLA/MU/AMD）
COMPANIES = {
    "0001045810": ("NVDA", "英伟达"),
    "0001318605": ("TSLA", "特斯拉"),
    "0000723125": ("MU", "美光科技"),
    "0000002488": ("AMD", "超威半导体"),
    "0000320193": ("AAPL", "苹果"),
    "0000789019": ("MSFT", "微软"),
}
FORMS = {"8-K", "10-Q", "10-K", "8-K/A"}
# 8-K 事项代码 → 中文含义（只解释代码，不做任何判断）
ITEMS = {
    "1.01": "签订重大协议", "1.02": "终止重大协议", "1.03": "破产或接管",
    "2.01": "完成重大资产收购或处置", "2.02": "披露业绩与财务状况",
    "2.03": "新增重大债务", "2.05": "退出或处置成本", "2.06": "重大减值",
    "3.01": "退市或不符合上市标准", "4.01": "变更审计机构",
    "5.01": "控制权变更", "5.02": "董监高变动", "5.03": "修改公司章程",
    "7.01": "公平披露规则材料", "8.01": "其他重大事件", "9.01": "财务报表与附件",
}
LOOKBACK_DAYS = 7


def _fetch(cik: str) -> Optional[Dict[str, Any]]:
    last = None
    for attempt in range(5):
        try:
            r = requests.get(BASE.format(cik=cik), headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[WARN] CIK {cik} 拉取失败: {type(last).__name__}: {last}", flush=True)
    return None


def _recent(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rec = ((payload.get("filings") or {}).get("recent")) or {}
    out = []
    n = len(rec.get("form") or [])
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    for i in range(n):
        form = str(rec["form"][i])
        if form not in FORMS:
            continue
        date = str(rec["filingDate"][i])
        try:
            ts = int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=CST).timestamp())
        except ValueError:
            continue
        if ts < cutoff:
            continue
        out.append({
            "form": form,
            "date": date,
            "ts": ts,
            "items": str(rec["items"][i] or ""),
            "accession": str(rec["accessionNumber"][i]),
            "doc": str(rec["primaryDocument"][i] or ""),
        })
    return out


def main() -> int:
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    written = skipped = 0

    for cik, (ticker, name) in COMPANIES.items():
        payload = _fetch(cik)
        if not payload:
            continue
        for f in _recent(payload):
            codes = [x.strip() for x in f["items"].split(",") if x.strip()]
            explained = "、".join(f"{c} {ITEMS.get(c, '')}".strip() for c in codes) or "未标注事项代码"
            body = (f"{name}（{ticker}）向 SEC 提交 {f['form']}，"
                    f"披露日期 {f['date']}，涉及 {explained}。")
            fp = hashlib.md5(f"{cik}:{f['accession']}".encode()).hexdigest()
            cur.execute("SELECT 1 FROM raw_news WHERE content_fingerprint=? AND source=? LIMIT 1",
                        (fp, SOURCE))
            if cur.fetchone():
                skipped += 1
                continue
            # 必须走 evaluate_news 这个正规入口：它产出 is_noise / relevance_score /
            # gate_* 全套字段。直插 PENDING 会被下游当成未过滤行，最终被判 FILTERED。
            ev = realtime_filter.evaluate_news(body, body)
            try:
                cur.execute(
                    """INSERT INTO raw_news
                       (source, content, timestamp, ts, published_at, status,
                        is_noise, relevance_score, gate_kind, gate_verdict,
                        gate_unavailable, content_fingerprint,
                        cross_net_first_seen_ts, cross_net_source, url)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'SINGLE_NET', ?, ?)""",
                    (SOURCE, body[:3000], f["ts"], f["ts"],
                     datetime.fromtimestamp(f["ts"], tz=CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                     ev.get("status", "PENDING"), int(ev.get("is_noise") or 0),
                     float(ev.get("relevance_score") or 0),
                     ev.get("kind") or "other", ev.get("gate_verdict") or "",
                     int(ev.get("gate_unavailable") or 0), fp, SOURCE,
                     f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                     f"{f['accession'].replace('-', '')}/{f['doc']}"),
                )
                written += 1
            except sqlite3.Error as exc:
                print(f"[WARN] 写入失败: {exc}", flush=True)
        time.sleep(2)  # SEC 要求 ≤10 请求/秒，这里远低于上限

    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] SEC 公告 新增 {written} 条 / 跳过重复 {skipped} 条",
          flush=True)
    return 0 if written >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
