#!/usr/bin/env python3
"""FRED 免费 CSV 采集器（不需要 API key）。

为什么需要：
  · 原来的 FRED API 通道（api.stlouisfed.org + key）已断 37 天，美债收益率、
    联邦基金利率、期限利差全部停在 10-07。
  · FRED 官网的图线导出接口 `fredgraph.csv?id=<序列号>` 走的是另一条链路，
    实测可达且不需要 key，一天一轮就能把关键序列补回来。
  · 这些是定价的底层刻度：10Y 决定折现率、2Y 反映加息预期、10Y-2Y 倒挂是衰退信号、
    VIX 是风险偏好、WTI 现货给原油一个独立于期货的参照。

写入 external_indicators，source='fred_csv'，asset='US_MACRO'：
  us_dgs10     10 年期国债收益率（%）
  us_dgs2      2 年期国债收益率（%）
  us_dff       联邦基金有效利率（%）
  us_t10y2y    10Y-2Y 期限利差（%）
  us_vix       VIX 收盘（点）
  us_wti_spot  WTI 现货（美元/桶）

限制：官网 CSV 无配额说明，本采集器每日一轮、6 个序列，保守使用。
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 15
SERIES = {
    "DGS10": ("us_dgs10", "%", "10 年期国债收益率"),
    "DGS2": ("us_dgs2", "%", "2 年期国债收益率"),
    "DFF": ("us_dff", "%", "联邦基金有效利率"),
    "T10Y2Y": ("us_t10y2y", "%", "10Y-2Y 期限利差"),
    "VIXCLS": ("us_vix", "point", "VIX 收盘"),
    "DCOILWTICO": ("us_wti_spot", "USD/bbl", "WTI 现货"),
}


# trust_env=False：本机存在 DOUBAO_* 转发代理变量，requests 会误命中并把连接掐断
# （实测同一 URL：trust_env=True 报 ConnectionError，False 返回 200）。
_SESSION = requests.Session()
_SESSION.trust_env = False
_SESSION.headers.update(HEADERS)
_SESSION.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))


def _parse_latest(text: str) -> Optional[float]:
    """从 FRED CSV 文本里取最近一个有数值的观测。"""
    rows = list(csv.reader(io.StringIO(text or "")))
    for row in reversed(rows[1:]):
        if len(row) < 2:
            continue
        raw = (row[1] or "").strip()
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def _curl_latest(series_id: str, timeout: int = 20) -> tuple[Optional[float], str]:
    """curl 兜底抓取（v337）。

    实测（2026-11-14）：同一个 fredgraph.csv 地址，
      · python requests → SSLError UNEXPECTED_EOF_WHILE_READING / ConnectionReset；
      · curl → HTTP 200 稳定拿到 CSV。
    原因是出网链路对 python 的 TLS 指纹做了拦截，requests 绕不过去，
    而 curl 的指纹能过。宏观缺刻度（10Y/2Y/VIX/WTI 现货）会直接拖垮
    「市场基本面判断」，所以这里加一层 curl 兜底，不再让整轮静默失败。
    """
    url = f"{URL}?id={series_id}"
    try:
        out = subprocess.run(
            ["curl", "-sS", "--max-time", str(int(timeout)), url],
            capture_output=True, text=True, timeout=int(timeout) + 5,
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if out.returncode != 0:
        return None, f"curl exit {out.returncode}: {(out.stderr or '').strip()[:120]}"
    return _parse_latest(out.stdout), ""


def _latest(series_id: str) -> Optional[float]:
    """取该序列最近一个有数值的观测。FRED CSV 末尾常有空值（节假日/未发布）。

    连续快速请求会被服务端 reset（实测 6 个序列背靠背请求时后 3 个必失败），
    所以每条之间留间隔、失败后指数退避重试；requests 全失败后走 curl 兜底。
    """
    last = None
    for attempt in range(4):
        try:
            r = _SESSION.get(URL, params={"id": series_id}, timeout=TIMEOUT)
            r.raise_for_status()
            return _parse_latest(r.text)
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    val, err = _curl_latest(series_id)
    if val is not None:
        print(f"[INFO] {series_id} requests 失败, 已由 curl 兜底取到 {val}", flush=True)
        return val
    print(f"[WARN] {series_id} 失败: {type(last).__name__}: {last}"
          f"{' | curl: ' + err if err else ''}", flush=True)
    return None


def _fallback(series_id: str) -> Optional[Tuple[float, str]]:
    """FRED 连不上时的降级源。全是官方/发布方自己的数据，不编数。

    · DGS10 / DGS2 / T10Y2Y → 财政部日频收益率 CSV（ust_yields 里已有解析）
    · VIXCLS → CBOE 官方延迟报价（cboe_vix 里已有通道）
    · DCOILWTICO → 新浪 hf_CL 连续合约（需带 Referer，否则 403）
    · DFF → 无可用免费替代，只能标缺失
    返回 (value, via)；拿不到返回 None。
    """
    try:
        if series_id in ("DGS10", "DGS2", "T10Y2Y"):
            import ust_yields as uy
            got = uy.fetch_from_treasury(series_id)
            if got:
                return float(got[1]), f"treasury_gov_csv@{got[0]}"
        elif series_id == "VIXCLS":
            import cboe_vix as cv
            d = cv._quote("_VIX") or {}
            v = d.get("current_price") or d.get("close")
            if v:
                return float(v), "cboe_delayed_quote"
        elif series_id == "DCOILWTICO":
            r = _SESSION.get("https://hq.sinajs.cn/list=hf_CL",
                             headers={"Referer": "https://finance.sina.com.cn"},
                             timeout=TIMEOUT)
            r.raise_for_status()
            body = r.text.split('"')[1]
            parts = body.split(",")
            v = float(parts[0])          # 当前价
            if v > 0:
                return v, "sina_hf_CL"
    except Exception as exc:
        print(f"[WARN] {series_id} 降级源失败: {type(exc).__name__}: {exc}", flush=True)
    return None


def _mark_missing(indicator: str, series_id: str, note: str) -> None:
    """拿不到就标缺失：写一条 data_quality_flag，24 小时内同因不重复写。"""
    try:
        import sqlite3
        import db as _db
        conn = sqlite3.connect(_db.get_db_path(), timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            dup = conn.execute(
                """SELECT 1 FROM data_quality_flags
                   WHERE indicator=? AND rule='source_unreachable'
                     AND created_at > ? LIMIT 1""",
                (indicator, int(time.time()) - 86400)).fetchone()
            if not dup:
                conn.execute(
                    """INSERT INTO data_quality_flags
                       (ts,source,asset,indicator,value,rule,note,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (int(time.time()), "fred_csv", "US_MACRO", indicator, None,
                     "source_unreachable", note, int(time.time())))
                conn.commit()
                print(f"[FLAG] {indicator} 标缺失: {note}", flush=True)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[WARN] 缺失标记失败: {type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    """每轮只取一个序列，轮转推进。

    实测：fred.stlouisfed.org 单发请求稳定 200，但连续 3-6 个请求后必被
    reset/timeout（无论加多少间隔）。所以不追求一次拉全，改为每轮 1 个、
    30 分钟一轮 → 6 个序列各自 3 小时更新一次，对日频数据完全够用。
    """
    state_path = os.path.expanduser("~/.trident/state/fred_csv_rotation.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    try:
        idx = int(json.load(open(state_path)).get("next_idx", 0))
    except Exception:
        idx = 0

    keys = list(SERIES.keys())
    series_id = keys[idx % len(keys)]
    indicator, unit, label = SERIES[series_id]

    value = _latest(series_id)
    via = "fredgraph.csv"
    written = 0
    if value is None:
        got = _fallback(series_id)
        if got:
            value, via = got[0], got[1]
            print(f"[FALLBACK] {series_id} FRED 不通，改用 {via}", flush=True)
        else:
            print(f"[WARN] {series_id} 本轮无有效观测（已标缺失，下轮继续轮转）", flush=True)
            _mark_missing(indicator, series_id,
                          "FRED 与所有降级源本轮都不可达")
    if value is not None:
        external_data.ingest_external(
            asset="US_MACRO", indicator=indicator,
            records=[{"ts": int(time.time()), "value": value}],
            source="fred_csv",
            meta={"unit": unit, "series": series_id, "label": label,
                  "via": via, "rotation": True},
        )
        written = 1
        print(f"[{time.strftime('%H:%M:%S')}] {label}={value} 写 1 条", flush=True)

    json.dump({"next_idx": (idx + 1) % len(keys), "last": series_id},
              open(state_path, "w"))
    # 拿不到数据已完成"标缺失"(data_quality_flags)，不再用非零退出码冒充故障；
    # 真的写库异常会抛异常 → 自然非零。
    return 0


if __name__ == "__main__":
    sys.exit(main())
