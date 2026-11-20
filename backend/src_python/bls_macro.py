#!/usr/bin/env python3
"""BLS 美国宏观序列采集器。

为什么需要：
  · 宏观这块此前只靠 FRED，而 FRED 已断 37 天，导致"美国通胀/失业/非农"
    三个最关键的定价变量在库里是空白。
  · BLS 是这三个数字的第一手发布方（劳工统计局自己），比二手转载更准，
    且不需要 key（v2 接口每天 25 次免费额度，本采集器一天一轮）。

写入 external_indicators，source='bls'，asset='US_MACRO'：
  bls_cpi_index         CPI-U 全项目指数（最新月）
  bls_cpi_yoy           CPI 同比（%），由最近 13 个月指数算出
  bls_unemployment_rate 失业率（%）
  bls_nonfarm_payroll   非农就业人数（千人）

限制：v2 接口无 key 每天 25 次；一天一轮、3 个序列，安全。
"""
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

# 本机存在 DOUBAO_* 转发代理变量，requests 默认 trust_env=True 会误命中代理并断连；
# 实测关闭后同一请求稳定返回 200。
requests.Session.trust_env = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

# v341: v2 需要注册 key，没 key 时实测直接 SSLError/403（launchctl 里这个作业
# 长期 exit 1，BLS 宏观三个月没更新）。v1 不需要 key，实测 200 正常返回。
# 有 key 就走 v2（额度更宽松），没 key 用 v1。
URL_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
URL_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
URL = URL_V2          # 兼容旧引用
BLS_API_KEY = os.environ.get("BLS_API_KEY", "").strip()
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Content-Type": "application/json"}
TIMEOUT = 20
SERIES = {
    "CUUR0000SA0": ("bls_cpi_index", {"unit": "index", "freq": "monthly"}),
    "LNS14000000": ("bls_unemployment_rate", {"unit": "%", "freq": "monthly"}),
    "CES0000000001": ("bls_nonfarm_payroll", {"unit": "thousand", "freq": "monthly"}),
}


def _query(series_ids: List[str]) -> Optional[Dict[str, Any]]:
    payload = {"seriesid": series_ids, "startyear": str(time.gmtime().tm_year - 2),
               "endyear": str(time.gmtime().tm_year)}
    if BLS_API_KEY:
        payload["registrationkey"] = BLS_API_KEY
    # v2（有 key 时）/ v1 依次尝试；某个端点挂了不影响另一个
    urls = [URL_V2, URL_V1] if BLS_API_KEY else [URL_V1, URL_V2]
    for url in urls:
        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                js = r.json()
                if str(js.get("status", "")).upper().startswith("REQUEST_SUCCEEDED"):
                    return js
                print(f"[WARN] BLS {url.rsplit('/', 3)[1]} 状态异常: {js.get('status')}", flush=True)
            except Exception as exc:
                print(f"[WARN] BLS 拉取失败 {url.rsplit('/', 3)[1]}({attempt+1}/2): "
                      f"{type(exc).__name__}: {str(exc)[:100]}", flush=True)
                time.sleep(3 * (attempt + 1))
    return None


def _clean(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只留月度常规值，丢掉 M13 年均与带 footnote 的修订占位。"""
    out = []
    for row in rows:
        period = str(row.get("period") or "")
        if not period.startswith("M") or period == "M13":
            continue
        raw = str(row.get("value") or "").replace(",", "").strip()
        if not raw or raw in ("-", "."):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        year = int(row.get("year") or 0)
        month = int(period[1:])
        out.append({"year": year, "month": month, "value": value,
                    "ts": int(time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1)))})
    out.sort(key=lambda d: (d["year"], d["month"]))
    return out


def main() -> int:
    data = _query(list(SERIES.keys()))
    if not data or data.get("status") != "REQUEST_SUCCEEDED":
        print(f"[ERROR] BLS 返回异常: {str(data)[:200]}", flush=True)
        return 1

    by_id: Dict[str, List[Dict[str, Any]]] = {}
    for s in ((data.get("Results") or {}).get("series")) or []:
        by_id[str(s.get("seriesID"))] = _clean(s.get("data") or [])

    written = 0
    for series_id, (indicator, meta) in SERIES.items():
        rows = by_id.get(series_id) or []
        if not rows:
            print(f"[WARN] {series_id} 无有效观测", flush=True)
            continue
        latest = rows[-1]
        external_data.ingest_external(
            asset="US_MACRO", indicator=indicator,
            records=[{"ts": int(time.time()), "value": latest["value"]}],
            source="bls", meta=dict(meta, period=f"{latest['year']}-{latest['month']:02d}", observed_ts=latest["ts"]),
        )
        written += 1
        print(f"[{time.strftime('%H:%M:%S')}] {indicator}={latest['value']} "
              f"({latest['year']}-{latest['month']:02d})", flush=True)

    # CPI 同比：最近月 vs 12 个月前同月
    cpi = by_id.get("CUUR0000SA0") or []
    if len(cpi) >= 13:
        now, year_ago = cpi[-1], cpi[-13]
        yoy = (now["value"] / year_ago["value"] - 1) * 100
        external_data.ingest_external(
            asset="US_MACRO", indicator="bls_cpi_yoy",
            records=[{"ts": int(time.time()), "value": round(yoy, 3)}],
            source="bls", meta={"unit": "%", "freq": "monthly", "period": f"{now['year']}-{now['month']:02d}"},
        )
        written += 1
        print(f"[{time.strftime('%H:%M:%S')}] bls_cpi_yoy={yoy:.2f}%", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] BLS 写 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
