#!/usr/bin/env python3
"""美国财政部官方收益率曲线（一手源，免 key）。

为什么换成它：
  FRED 在这台机器上"单发可用、连发必断"，`DGS10` 全历史 CSV 常常 ConnectionError，
  所以 10Y 一直是靠 `2Y + (10Y-2Y)` 推导出来的 `us_dgs10_calc`，官方值迟迟落不了库。

  财政部自己发布的 daily treasury yield curve 是一手官方数据，而且**一次请求返回整条曲线**
  （1M/3M/6M/1Y/2Y/3Y/5Y/7Y/10Y/20Y/30Y），比逐个 FRED 序列稳得多。

  实测：2026-11-12 的 10Y = 5.10，与 FRED `DGS10` 同日值一致，可互相校验。

命名沿用已有口径，避免下游改代码：
  · 2Y  → `us_dgs2`      （已存在，覆盖升级为一手源）
  · 10Y → `us_dgs10`     （已存在，官方值终于能落库，优先于 `us_dgs10_calc`）
  · 其余期限 → `us_t_1m/3m/6m/1y/3y/5y/7y/20y/30y`

注意：本机 `requests` 默认 `trust_env=True` 会命中办公代理，必须关掉。
"""
from __future__ import annotations

import argparse
import csv
import io
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import external_data

import requests

URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# 表头 → 指标名。保持 2Y/10Y 的旧名，其余用 us_t_ 前缀
COLUMN_MAP = {
    "1 Mo": "us_t_1m",
    "2 Mo": "us_t_2m",
    "3 Mo": "us_t_3m",
    "4 Mo": "us_t_4m",
    "6 Mo": "us_t_6m",
    "1 Yr": "us_t_1y",
    "2 Yr": "us_dgs2",
    "3 Yr": "us_t_3y",
    "5 Yr": "us_t_5y",
    "7 Yr": "us_t_7y",
    "10 Yr": "us_dgs10",
    "20 Yr": "us_t_20y",
    "30 Yr": "us_t_30y",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 必须：否则会走办公代理并直接被掐断
    s.headers.update({"User-Agent": UA, "Accept": "text/csv,*/*"})
    return s


def _looks_like_csv(text: str) -> bool:
    """必须是"首行以 Date 开头的 CSV"。

    原来只判 `"Date" in text[:200]` —— 抓回一个 HTML 错误页时，页头里的
    `Date: Mon, 16 Nov 2026...` 也能命中这个判断，于是"抓取成功但解析出 0 个点"。
    """
    head = (text or "").lstrip("\ufeff \r\n")[:200]
    return head.startswith("Date,") and "\n" in head


def _curl_fetch(url: str, timeout: int = 25) -> str | None:
    """用 curl 兜底抓取。

    实测本机 Python 走 TLS 到 home.treasury.gov 必报
    `SSLEOFError: UNEXPECTED_EOF_WHILE_READING`（requests 重试 4 次全灭），
    同一台机器 curl 却能过 —— 这是 OpenSSL/证书链差异，不是网络不可达。
    所以 TLS 失败时降级到 curl，而不是直接判"源不可用"。
    """
    try:
        out = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout), "-A", UA,
             "-H", "Accept: text/csv,*/*", url],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        if out.returncode == 0 and _looks_like_csv(out.stdout or ""):
            return out.stdout
    except Exception:
        pass
    return None


def fetch_curve(year: int | None = None, retries: int = 4) -> str:
    year = year or datetime.now(timezone.utc).year
    url = URL.format(year=year)
    last: Exception | None = None
    for i in range(retries):
        try:
            r = _session().get(url, timeout=25)
            if r.status_code == 200 and _looks_like_csv(r.text):
                return r.text
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as exc:
            last = exc
            csv_text = _curl_fetch(url)          # TLS EOF → 试 curl
            if csv_text:
                return csv_text
        time.sleep(1.5 * (2 ** i))  # 境外站首次常 TLS reset，指数退避
    raise RuntimeError(f"财政部曲线抓取失败: {last}")


def parse(text: str) -> dict[str, list[dict]]:
    """解析成 {indicator: [{ts, value}, ...]}。空值 / 失败日期直接跳过，不补零。"""
    reader = csv.DictReader(io.StringIO(text))
    by_ind: dict[str, list[dict]] = {}
    for row in reader:
        date_s = (row.get("Date") or "").strip()
        if not date_s:
            continue
        try:
            dt = datetime.strptime(date_s, "%m/%d/%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                dt = datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        ts = int(dt.timestamp())
        for col, ind in COLUMN_MAP.items():
            raw = (row.get(col) or "").strip()
            if not raw:
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            by_ind.setdefault(ind, []).append({"ts": ts, "value": val})
    return by_ind


def derive_spreads(series: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """用官方两个期限直接作差，得到 10Y-2Y 与 10Y-3M。

    不覆盖 FRED 官方发布的 T10Y2Y（那是另一条已验证序列），单独命名 `*_gov`，
    meta 里写明是"官方两腿相减"，看板与规则可以按新鲜度择优取用。
    """
    def _by_ts(ind: str) -> dict[int, float]:
        return {r["ts"]: r["value"] for r in series.get(ind, [])}

    ten, two, three_m = _by_ts("us_dgs10"), _by_ts("us_dgs2"), _by_ts("us_t_3m")
    out: dict[str, list[dict]] = {}
    common = set(ten) & set(two)
    if common:
        out["us_t10y2y_gov"] = [{"ts": t, "value": round(ten[t] - two[t], 3)} for t in sorted(common)]
    common = set(ten) & set(three_m)
    if common:
        out["us_t10y3m_gov"] = [{"ts": t, "value": round(ten[t] - three_m[t], 3)} for t in sorted(common)]
    return out


def ingest(series: dict[str, list[dict]], days: int = 90) -> dict:
    cutoff = int(time.time()) - days * 86400
    stats = {}
    for ind, recs in series.items():
        recs = [r for r in recs if r["ts"] >= cutoff]
        if not recs:
            continue
        n = external_data.ingest_external(
            asset="US_MACRO",
            indicator=ind,
            records=recs,
            source="treasury_gov",
            meta={
                "unit": "%",
                "official": True,
                "publisher": "U.S. Department of the Treasury",
                "note": "daily treasury yield curve, 一手官方源",
            },
        )
        stats[ind] = n
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = fetch_curve(args.year)
    series = parse(text)
    if args.dry_run:
        for ind, recs in sorted(series.items()):
            latest = max(recs, key=lambda r: r["ts"])
            print(f"{ind:<12} {len(recs):>4} 点  最新 {latest['value']}  "
                  f"{datetime.fromtimestamp(latest['ts'], timezone.utc):%Y-%m-%d}")
        return
    spreads = derive_spreads(series)
    series = {**series, **spreads}
    stats = ingest(series, days=args.days)

    def _num(r):
        """ingest_external 返回 dict 或 int —— 两种都兼容，只取写入条数。"""
        if isinstance(r, dict):
            for key in ("inserted", "written", "rows", "count"):
                if isinstance(r.get(key), int):
                    return r[key]
            return 0
        return int(r or 0)

    total = sum(_num(v) for v in stats.values())
    print(f"[TREASURY] 入库 {len(stats)} 条序列, 共 {total} 个点")
    for k, v in sorted(stats.items()):
        print(f"  {k:<12} +{_num(v)}")


if __name__ == "__main__":
    main()
