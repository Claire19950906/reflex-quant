#!/usr/bin/env python3
"""A股 / 港股基本面（v406）—— 东方财富财务指标 + 腾讯行情，口径与美股层拉齐。

为什么要有这一层：
  v405 之前（包括 v405）这个系统对 A股/港股的公司基本面是 **0**：没有 akshare、
  没有 tushare、没有财报表。决策里出现 `CN_STOCK`（一周 17 次）、`HK_STOCK` 时，
  因子层直接返回「非公司标的，无公司级基本面」—— 那句是**错的**：
  贵州茅台当然有财报，只是我们没接。把「缺口」说成「不适用」，
  等于让复盘误以为"A股这类资产本来就不用看基本面"。

数据源（本机实测 200，无需 key，国内直连）：
  A股  `datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD`
        —— 主要财务指标：营收/归母净利/加权ROE/EPS/BPS/毛利率/经营现金流每股
  HK   `...reportName=RPT_HKF10_FN_MAININDICATOR`
        —— 直接给 EPS_TTM / 营收同比 / 归母净利同比 / ROE_YEARLY / 毛利率
  价格 `cn_prices.fetch_batch(['sh600519'|'hk00700'])`（腾讯，本机实测 200）
  → 有 EPS_TTM + 价格才能算 PE；有 BPS + 价格才能算 PB。

口径三条铁律（与 docs/DATA_AUTHENTICITY.md 一致）：
  · A股财报是**累计口径**（三季报 EPS 是 9 个月累计），不能当单季相加 —— 先还原 TTM；
  · PE ≤ 0（亏损）**不给分位**：亏损股看 PE 本身就是错指标，给"低分位"会读出"便宜=看多"；
  · PE 分位来自**本系统自己累积的快照**（cn_valuation_snapshots），样本 <60 不作结论。

与美股层的差别（写在代码里，避免以后有人问"为什么两边不对称"）：
  美股 = cron 落库 + 决策链零网络读库（因为 AV 只有 25 次/日，必须在链路外慢慢抓）；
  A股/港股 = 按需抓取 + 24h TTL + 落库（东财是国内直连、~300ms，且目前只有代码真出现时才会触发）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.request
from urllib.parse import quote, urlencode
from typing import Any, Dict, List, Optional, Tuple

import config
import cn_depth
from asset_kinds import resolve_a_share, resolve_hk

_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_TIMEOUT = 8
_PE_WINDOW_DAYS = 365
_PE_MIN_SAMPLES = int(getattr(config, "CN_PE_MIN_SAMPLES", 60) or 60)
_TTL = int(getattr(config, "CN_FUNDAMENTALS_TTL", 86400) or 86400)

_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_fetch_lock = threading.Lock()


# ── 基础 ────────────────────────────────────────────────────────────────────
def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().upper() in ("", "N/A", "NA", "NONE", "NULL", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _get(url: str) -> Any:
    """东财 datacenter：必须带 UA + Referer，否则偶发 RemoteDisconnected。"""
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": "https://data.eastmoney.com/",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    assert last is not None
    raise last


def _em(report: str, flt: str, size: int = 16, sort: str = "REPORT_DATE") -> List[Dict[str, Any]]:
    key = f"{report}:{flt}:{size}:{sort}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    url = (f"{_EM_URL}?reportName={report}&columns=ALL"
           f"&filter={quote(flt)}&pageSize={size}"
           f"&sortColumns={sort}&sortTypes=-1&source=WEB&client=WEB")
    try:
        body = _get(url)
        rows = ((body or {}).get("result") or {}).get("data") or []
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []
    with _cache_lock:
        _cache[key] = (now + _TTL, rows)
    return rows


# ── A股：累计口径 → TTM ─────────────────────────────────────────────────────
_A_ANNUAL = {"年报", "年度报告", "年报(合并)", "年报(母公司)"}


def _a_quarter_count(label: str) -> Optional[int]:
    """按报告类型给累计季度数：一季报=1 / 半年报=2 / 三季报=3 / 年报=4。"""
    text = str(label or "")
    if "一季" in text or "第一季度" in text:
        return 1
    if "半年" in text or "中报" in text:
        return 2
    if "三季" in text or "第三季度" in text:
        return 3
    if any(k in text for k in _A_ANNUAL):
        return 4
    return None


def _a_ttm(rows: List[Dict[str, Any]], field: str) -> Optional[float]:
    """累计值还原 TTM：TTM = 本期累计 + 上一年年报 − 去年同类型累计。

    三季报 EPS 51.53 是"9 个月累计"，直接当 TTM 会把 EPS 少算一个季度；
    直接 4 季相加又会把累计重复相加 —— 两种都是错的。
    """
    if not rows:
        return None
    latest = rows[0]
    latest_cum = _num(latest.get(field))
    if latest_cum is None:
        return None
    label = str(latest.get("DATEMMDD") or "")
    if _a_quarter_count(label) == 4:      # 最新就是年报：TTM 就是年报本身
        return latest_cum
    year = str(latest.get("REPORTDATE") or "")[:4]
    fiscal = next((r for r in rows
                   if str(r.get("DATEMMDD") or "") in _A_ANNUAL
                   and str(r.get("REPORTDATE") or "")[:4] == str(int(year) - 1 if year.isdigit() else "")
                   ), None)
    prev_same = next((r for r in rows
                      if str(r.get("DATEMMDD") or "") == label
                      and str(r.get("REPORTDATE") or "")[:4] == str(int(year) - 1 if year.isdigit() else "")
                      ), None)
    fy = _num(fiscal.get(field)) if fiscal else None
    prev_cum = _num(prev_same.get(field)) if prev_same else None
    if fy is None or prev_cum is None:
        return None
    return round(latest_cum + fy - prev_cum, 4)


def parse_a_share(rows: List[Dict[str, Any]], price: Optional[float] = None
                  ) -> Dict[str, Any]:
    """东财 A股主要财务指标 → 统一读数（TTM 规模 + 官方累计同比 + 估值）。"""
    if not rows:
        return {}
    latest = rows[0]
    label = str(latest.get("DATEMMDD") or "")
    quarters = _a_quarter_count(label) or 1
    eps_ttm = _a_ttm(rows, "BASIC_EPS")
    revenue_ttm = _a_ttm(rows, "TOTAL_OPERATE_INCOME")
    net_ttm = _a_ttm(rows, "PARENT_NETPROFIT")
    roe_ytd = _num(latest.get("WEIGHTAVG_ROE"))
    roe_annual = round(roe_ytd * 4.0 / quarters, 2) if roe_ytd is not None and quarters else None
    bps = _num(latest.get("BPS"))
    pe = round(price / eps_ttm, 2) if price and eps_ttm and eps_ttm > 0 else None
    pb = round(price / bps, 2) if price and bps and bps > 0 else None
    return {
        "ticker": str(latest.get("SECUCODE") or latest.get("SECURITY_CODE") or ""),
        "market": "A",
        "name": str(latest.get("SECURITY_NAME_ABBR") or ""),
        "industry": str(latest.get("BOARD_NAME") or latest.get("PUBLISHNAME") or ""),
        "report_date": str(latest.get("REPORTDATE") or "")[:10],
        "period_label": label,
        "eps_ttm": eps_ttm,
        "revenue_ttm": revenue_ttm,
        "net_profit_ttm": net_ttm,
        # 官方口径：本期**累计**同比（不是单季同比），标签必须写清
        "revenue_yoy": _num(latest.get("YSTZ")),
        "net_profit_yoy": _num(latest.get("SJLTZ")),
        "roe": roe_ytd,
        "roe_annualized": roe_annual,
        "gross_margin": _num(latest.get("XSMLL")),
        "bps": bps,
        "price": price,
        "pe_ttm": pe,
        "pb": pb,
        "source": "eastmoney",
    }


# ── 港股：东财直接给 TTM，不需要还原 ───────────────────────────────────────
def _hk_quarter_count(label: str) -> Optional[int]:
    """港股报告类型 → 累计季度数（一季报=1 / 中报=2 / 三季报=3 / 年报=4）。"""
    text = str(label or "")
    if "一季" in text or "第一季度" in text:
        return 1
    if "中报" in text or "半年" in text or "中期" in text:
        return 2
    if "三季" in text or "第三季度" in text:
        return 3
    if "年报" in text or "年度" in text:
        return 4
    return None


def _hk_ttm(rows: List[Dict[str, Any]], idx: int, field: str) -> Optional[float]:
    """港股累计口径还原 TTM：本期累计 + 上一年年报 − 去年同类型累计。

    东财港股的 `OPERATE_INCOME` / `HOLDER_PROFIT` 是**年内累计**（Q3 是 9 个月），
    不是 TTM；旧实现直接当 TTM，会把 Q3 营收少算一个季度。
    `EPS_TTM` 本身已是 TTM，不走这里。
    """
    if not rows or idx >= len(rows):
        return None
    latest = rows[idx]
    cum = _num(latest.get(field))
    if cum is None:
        return None
    label = str(latest.get("REPORT_TYPE") or "")
    quarters = _hk_quarter_count(label)
    if quarters == 4:
        return cum
    if quarters is None:
        return None
    year = str(latest.get("REPORT_DATE") or "")[:4]
    if not year.isdigit():
        return None
    prev_year = str(int(year) - 1)
    fiscal = next((r for r in rows
                   if _hk_quarter_count(str(r.get("REPORT_TYPE") or "")) == 4
                   and str(r.get("REPORT_DATE") or "")[:4] == prev_year), None)
    prev_same = next((r for r in rows
                      if _hk_quarter_count(str(r.get("REPORT_TYPE") or "")) == quarters
                      and str(r.get("REPORT_DATE") or "")[:4] == prev_year), None)
    fy = _num(fiscal.get(field)) if fiscal else None
    prev_cum = _num(prev_same.get(field)) if prev_same else None
    if fy is None or prev_cum is None:
        return None
    return round(cum + fy - prev_cum, 4)


def parse_hk(rows: List[Dict[str, Any]], price: Optional[float] = None) -> Dict[str, Any]:
    if not rows:
        return {}
    latest = rows[0]
    eps_ttm = _num(latest.get("EPS_TTM"))
    bps = _num(latest.get("BPS"))
    pe = round(price / eps_ttm, 2) if price and eps_ttm and eps_ttm > 0 else None
    pb = round(price / bps, 2) if price and bps and bps > 0 else None
    return {
        "ticker": str(latest.get("SECUCODE") or ""),
        "market": "HK",
        "name": str(latest.get("SECURITY_NAME_ABBR") or ""),
        "industry": "",
        "report_date": str(latest.get("REPORT_DATE") or "")[:10],
        "period_label": str(latest.get("REPORT_TYPE") or ""),
        "eps_ttm": eps_ttm,
        "revenue_ttm": _hk_ttm(rows, 0, "OPERATE_INCOME"),
        "net_profit_ttm": _hk_ttm(rows, 0, "HOLDER_PROFIT"),
        "revenue_yoy": _num(latest.get("OPERATE_INCOME_YOY")),
        "net_profit_yoy": _num(latest.get("HOLDER_PROFIT_YOY")),
        "roe": _num(latest.get("ROE_AVG")),
        "roe_annualized": _num(latest.get("ROE_YEARLY")),
        "gross_margin": _num(latest.get("GROSS_PROFIT_RATIO")),
        "net_margin": _num(latest.get("NET_PROFIT_RATIO")),
        "bps": bps,
        "debt_asset_ratio": _num(latest.get("DEBT_ASSET_RATIO")),
        "price": price,
        "pe_ttm": pe,
        "pb": pb,
        "source": "eastmoney",
    }


# ── 抓取（A股 / 港股）───────────────────────────────────────────────────────
def _price(market: str, code: str) -> Optional[float]:
    """现价：A股 sh/sz 前缀、港股 hk 前缀，走腾讯（本机实测 200）。"""
    try:
        import cn_prices
        digits = code.split(".")[0]
        if market == "A":
            suffix = code.split(".")[-1] if "." in code else ""
            if suffix:
                prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[suffix]
            else:
                prefix = "sh" if digits[0] in ("6", "9") else "sz"
            symbol = f"{prefix}{digits}"
        else:
            symbol = f"hk{digits}"
        got = cn_prices.fetch_batch([symbol], "tencent")
        return got.get(symbol)
    except Exception:
        return None


def parse_a_share_history(rows: List[Dict[str, Any]], price: Optional[float] = None
                          ) -> List[Dict[str, Any]]:
    """A股多期读数（v489）：同一份东财返回里把每一期都还原成 TTM。

    `parse_a_share` 只取最新一期，等于把接口已经给出的 16 期历史全丢了；
    没有历史就算不出盈利位置、TTM 同比与利润率/ROE 趋势。
    每一期都用「该期及更早的行」做 `_a_ttm` —— 不用未来的年报回填过去。
    """
    ordered = sorted(rows, key=lambda r: str(r.get("REPORTDATE") or ""), reverse=True)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for idx, row in enumerate(ordered):
        report_date = str(row.get("REPORTDATE") or "")[:10]
        if not report_date or report_date in seen:
            continue
        seen.add(report_date)
        label = str(row.get("DATEMMDD") or "")
        quarters = _a_quarter_count(label) or 1
        eps_ttm = _a_ttm(ordered[idx:], "BASIC_EPS")
        revenue_ttm = _a_ttm(ordered[idx:], "TOTAL_OPERATE_INCOME")
        net_ttm = _a_ttm(ordered[idx:], "PARENT_NETPROFIT")
        roe_ytd = _num(row.get("WEIGHTAVG_ROE"))
        roe_annual = round(roe_ytd * 4.0 / quarters, 2) if roe_ytd is not None and quarters else None
        bps = _num(row.get("BPS"))
        is_latest = idx == 0
        pe = round(price / eps_ttm, 2) if is_latest and price and eps_ttm and eps_ttm > 0 else None
        pb = round(price / bps, 2) if is_latest and price and bps and bps > 0 else None
        out.append({
            "ticker": str(row.get("SECUCODE") or row.get("SECURITY_CODE") or ""),
            "market": "A",
            "name": str(row.get("SECURITY_NAME_ABBR") or ""),
            "industry": str(row.get("BOARD_NAME") or row.get("PUBLISHNAME") or ""),
            "report_date": report_date,
            "period_label": label,
            "eps_ttm": eps_ttm,
            "revenue_ttm": revenue_ttm,
            "net_profit_ttm": net_ttm,
            # 官方口径：本期累计同比（不是 TTM 同比）；趋势另由 TTM 序列算
            "revenue_yoy": _num(row.get("YSTZ")),
            "net_profit_yoy": _num(row.get("SJLTZ")),
            "roe": roe_ytd,
            "roe_annualized": roe_annual,
            "gross_margin": _num(row.get("XSMLL")),
            "bps": bps,
            "price": price if is_latest else None,
            "pe_ttm": pe,
            "pb": pb,
            "source": "eastmoney",
        })
    return out


def parse_hk_history(rows: List[Dict[str, Any]], price: Optional[float] = None
                     ) -> List[Dict[str, Any]]:
    """港股多期读数（v489）：接口每行本身就是 TTM，逐行保留即可，不做累计还原。"""
    ordered = sorted(rows, key=lambda r: str(r.get("REPORT_DATE") or ""), reverse=True)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for idx, row in enumerate(ordered):
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        if not report_date or report_date in seen:
            continue
        seen.add(report_date)
        eps_ttm = _num(row.get("EPS_TTM"))
        bps = _num(row.get("BPS"))
        is_latest = idx == 0
        pe = round(price / eps_ttm, 2) if is_latest and price and eps_ttm and eps_ttm > 0 else None
        pb = round(price / bps, 2) if is_latest and price and bps and bps > 0 else None
        out.append({
            "ticker": str(row.get("SECUCODE") or ""),
            "market": "HK",
            "name": str(row.get("SECURITY_NAME_ABBR") or ""),
            "industry": "",
            "report_date": report_date,
            "period_label": str(row.get("REPORT_TYPE") or ""),
            "eps_ttm": eps_ttm,
            "revenue_ttm": _hk_ttm(ordered, idx, "OPERATE_INCOME"),
            "net_profit_ttm": _hk_ttm(ordered, idx, "HOLDER_PROFIT"),
            "revenue_yoy": _num(row.get("OPERATE_INCOME_YOY")),
            "net_profit_yoy": _num(row.get("HOLDER_PROFIT_YOY")),
            "roe": _num(row.get("ROE_AVG")),
            "roe_annualized": _num(row.get("ROE_YEARLY")),
            "gross_margin": _num(row.get("GROSS_PROFIT_RATIO")),
            "net_margin": _num(row.get("NET_PROFIT_RATIO")),
            "bps": bps,
            "debt_asset_ratio": _num(row.get("DEBT_ASSET_RATIO")),
            "price": price if is_latest else None,
            "pe_ttm": pe,
            "pb": pb,
            "source": "eastmoney",
        })
    return out


def fetch_history(market: str, code: str) -> Dict[str, Any]:
    """一次抓取返回**多期**读数（v489）。

    东财接口本来就按 `pageSize=16` 返回多期，旧实现只解析 `rows[0]`，
    历史被白扔；这里保留全部并逐期还原 TTM。价格只挂最新一期 ——
    历史 PE 不能用今天的价格算，那是假的。
    """
    if not getattr(config, "CN_FUNDAMENTALS_ENABLED", True):
        return {"error": "CN_FUNDAMENTALS_ENABLED=false"}
    market = str(market or "").upper()
    digits = str(code or "").split(".")[0]
    price = _price(market, code)
    if market == "A":
        # 注意：A股报表的排序列名是 REPORTDATE（无下划线），写成 REPORT_DATE 会返回
        # "REPORT_DATE排序列不存在" —— 看着像"没有数据"，其实是参数名错了。
        rows = _em("RPT_LICO_FN_CPD", f'(SECURITY_CODE="{digits}")', sort="REPORTDATE")
        records = parse_a_share_history(rows, price)
    elif market == "HK":
        rows = _em("RPT_HKF10_FN_MAININDICATOR", f'(SECUCODE="{digits}.HK")')
        records = parse_hk_history(rows, price)
    else:
        return {"error": f"不支持的 market={market}"}
    if not records:
        return {"error": "东财未返回该代码的财务指标（代码不存在或已退市）"}
    return {"records": records, "latest": records[0], "count": len(records)}


def fetch_one(market: str, code: str) -> Dict[str, Any]:
    """兼容旧入口：只返回最新一期。"""
    got = fetch_history(market, code)
    if got.get("error"):
        return got
    return got["latest"]


# ── 落库 / 读库 ─────────────────────────────────────────────────────────────
_CREATE_CN_NAME_MAP = """
CREATE TABLE IF NOT EXISTS cn_name_map (
    name        TEXT NOT NULL,
    code        TEXT NOT NULL,
    market      TEXT NOT NULL,
    updated_at  INTEGER,
    PRIMARY KEY (name, market)
)
"""
_CREATE_IDX_CN_NAME_MAP = "CREATE INDEX IF NOT EXISTS idx_cn_name_map_code ON cn_name_map(code)"


def ensure_tables(conn: sqlite3.Connection) -> None:
    import db as _db
    for name in ("_CREATE_CN_EQUITY_FUNDAMENTALS", "_CREATE_IDX_CN_EQ_FUND",
                 "_CREATE_CN_VALUATION_SNAPSHOTS", "_CREATE_IDX_CN_VAL"):
        sql = getattr(_db, name, "")
        if sql:
            conn.execute(sql)
    conn.execute(_CREATE_CN_NAME_MAP)
    conn.execute(_CREATE_IDX_CN_NAME_MAP)
    conn.commit()


def persist(conn: sqlite3.Connection, rec: Dict[str, Any]) -> int:
    """一只标的的一期读数 upsert + 一条估值快照。失败静默（不影响判断）。"""
    if not rec or rec.get("error"):
        return 0
    ticker = str(rec.get("ticker") or "")
    market = str(rec.get("market") or "")
    if not ticker or market not in ("A", "HK"):
        return 0
    now = int(time.time())
    written = 0
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('cn_equity_fundamentals','cn_valuation_snapshots')").fetchall()}
        if "cn_equity_fundamentals" in tables:
            conn.execute(
                """INSERT INTO cn_equity_fundamentals
                   (ticker, market, name, industry, report_date, period_label,
                    eps_ttm, revenue_ttm, net_profit_ttm, revenue_yoy, net_profit_yoy,
                    roe, roe_annualized, gross_margin, net_margin, bps,
                    debt_asset_ratio, price, pe_ttm, pb, source, fetched_at, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker, market, report_date) DO UPDATE SET
                     eps_ttm=excluded.eps_ttm, revenue_ttm=excluded.revenue_ttm,
                     net_profit_ttm=excluded.net_profit_ttm,
                     revenue_yoy=excluded.revenue_yoy, net_profit_yoy=excluded.net_profit_yoy,
                     roe=excluded.roe, roe_annualized=excluded.roe_annualized,
                     gross_margin=excluded.gross_margin, net_margin=excluded.net_margin,
                     bps=excluded.bps, debt_asset_ratio=excluded.debt_asset_ratio,
                     price=excluded.price, pe_ttm=excluded.pe_ttm, pb=excluded.pb,
                     fetched_at=excluded.fetched_at, payload=excluded.payload""",
                (ticker, market, rec.get("name") or "", rec.get("industry") or "",
                 str(rec.get("report_date") or ""), str(rec.get("period_label") or ""),
                 _num(rec.get("eps_ttm")), _num(rec.get("revenue_ttm")),
                 _num(rec.get("net_profit_ttm")), _num(rec.get("revenue_yoy")),
                 _num(rec.get("net_profit_yoy")), _num(rec.get("roe")),
                 _num(rec.get("roe_annualized")), _num(rec.get("gross_margin")),
                 _num(rec.get("net_margin")), _num(rec.get("bps")),
                 _num(rec.get("debt_asset_ratio")), _num(rec.get("price")),
                 _num(rec.get("pe_ttm")), _num(rec.get("pb")),
                 str(rec.get("source") or "eastmoney"), now,
                 json.dumps(rec, ensure_ascii=False, default=str)),
            )
            written += 1
        if "cn_valuation_snapshots" in tables:
            pe, pb = _num(rec.get("pe_ttm")), _num(rec.get("pb"))
            if pe is not None or pb is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO cn_valuation_snapshots
                       (ticker, market, ts, pe, pb, source) VALUES (?,?,?,?,?,?)""",
                    (ticker, market, now, pe, pb, str(rec.get("source") or "eastmoney")),
                )
                written += 1
        conn.commit()
    except Exception:
        return written
    return written


def persist_history(conn: sqlite3.Connection, records: List[Dict[str, Any]]) -> int:
    """多期落库：逐期 upsert（冲突键 ticker+market+report_date）。

    只有最新一期带价格，历史期的 PE/PB 为 None，因此不会写入假的估值快照。
    """
    return sum(persist(conn, rec) for rec in (records or []) if rec)


def read_one(conn: Optional[sqlite3.Connection] = None,
             ticker: str = "", market: str = "") -> Dict[str, Any]:
    """读最近一期读数（零网络）。"""
    owned = conn is None
    if owned:
        import db as _db
        conn = _db.get_connection()
    try:
        row = conn.execute(
            """SELECT name, industry, report_date, period_label, eps_ttm, revenue_ttm,
                      net_profit_ttm, revenue_yoy, net_profit_yoy, roe, roe_annualized,
                      gross_margin, net_margin, bps, debt_asset_ratio, price, pe_ttm, pb,
                      source, fetched_at
               FROM cn_equity_fundamentals
               WHERE ticker=? AND market=? ORDER BY report_date DESC LIMIT 1""",
            (ticker, market),
        ).fetchone()
    except Exception:
        return {}
    finally:
        if owned and conn is not None:
            conn.close()
    if not row:
        return {}
    keys = ("name", "industry", "report_date", "period_label", "eps_ttm", "revenue_ttm",
            "net_profit_ttm", "revenue_yoy", "net_profit_yoy", "roe", "roe_annualized",
            "gross_margin", "net_margin", "bps", "debt_asset_ratio", "price", "pe_ttm",
            "pb", "source", "fetched_at")
    rec = dict(zip(keys, tuple(row)) if not hasattr(row, "keys") else
               {k: row[k] for k in keys})
    rec["ticker"] = ticker
    rec["market"] = market
    return rec


def read_history(conn: Optional[sqlite3.Connection] = None,
                 ticker: str = "", market: str = "", limit: int = 16) -> List[Dict[str, Any]]:
    """读多期历史（零网络，按报告期倒序）。样本不足就返回实际条数，不补空行。"""
    owned = conn is None
    if owned:
        import db as _db
        conn = _db.get_connection()
    try:
        rows = conn.execute(
            """SELECT name, industry, report_date, period_label, eps_ttm, revenue_ttm,
                      net_profit_ttm, revenue_yoy, net_profit_yoy, roe, roe_annualized,
                      gross_margin, net_margin, bps, debt_asset_ratio, price, pe_ttm, pb,
                      source, fetched_at
               FROM cn_equity_fundamentals
               WHERE ticker=? AND market=?
               ORDER BY report_date DESC LIMIT ?""",
            (ticker, market, int(limit)),
        ).fetchall()
    except Exception:
        return []
    finally:
        if owned and conn is not None:
            conn.close()
    keys = ("name", "industry", "report_date", "period_label", "eps_ttm", "revenue_ttm",
            "net_profit_ttm", "revenue_yoy", "net_profit_yoy", "roe", "roe_annualized",
            "gross_margin", "net_margin", "bps", "debt_asset_ratio", "price", "pe_ttm",
            "pb", "source", "fetched_at")
    out: List[Dict[str, Any]] = []
    for row in rows:
        rec = dict(zip(keys, tuple(row)) if not hasattr(row, "keys") else
                   {k: row[k] for k in keys})
        rec["ticker"] = ticker
        rec["market"] = market
        out.append(rec)
    return out


def read_all(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """库里所有 A股/港股代码的最近一期读数（零网络，供宏观层渲染）。

    只返回 `cn_equity_fundamentals` 里真实存在的行 —— 表空就返回空，
    绝不因为"新闻里提到 A股"就去抓（那会把 22 分钟的宏观轮询拖成网络等待）。
    """
    owned = conn is None
    if owned:
        import db as _db
        conn = _db.get_connection()
    try:
        pairs = conn.execute(
            "SELECT DISTINCT ticker, market FROM cn_equity_fundamentals").fetchall()
    except Exception:
        return []
    finally:
        if owned and conn is not None:
            conn.close()
    out: List[Dict[str, Any]] = []
    for ticker, market in pairs:
        rec = read_one(None if owned else conn, str(ticker), str(market))
        if rec and not rec.get("error"):
            out.append(rec)
    return sorted(out, key=lambda r: (r.get("market") or "", r.get("ticker") or ""))


def pe_percentile(conn: sqlite3.Connection, ticker: str, market: str,
                  window_days: int = _PE_WINDOW_DAYS,
                  min_samples: Optional[int] = None) -> Dict[str, Any]:
    """自身 PE / PB 在近一年的分位（样本自累积）。样本不足或 PE≤0 一律不给分位。"""
    need = int(min_samples or _PE_MIN_SAMPLES)
    cutoff = int(time.time()) - window_days * 86400
    out: Dict[str, Any] = {"pe_samples": 0, "pb_samples": 0}
    try:
        for field, tag in (("pe", "pe"), ("pb", "pb")):
            rows = conn.execute(
                f"""SELECT ts, {field} FROM cn_valuation_snapshots
                    WHERE ticker=? AND market=? AND ts>=? AND {field} IS NOT NULL
                      AND {field} > 0 ORDER BY ts""",
                (ticker, market, cutoff),
            ).fetchall()
            values = [float(r[1]) for r in rows]
            n = len(values)
            out[f"{tag}_samples"] = n
            if n and n < need:
                continue
            if not n:
                continue
            current = values[-1]
            ordered = sorted(values)
            below = sum(1 for v in ordered if v <= current)
            out[tag] = round(current, 2)
            out[f"{tag}_position"] = round(below / n * 100.0, 1)
            out[f"{tag}_p25"] = round(ordered[int(n * 0.25)], 2)
            out[f"{tag}_p75"] = round(ordered[min(n - 1, int(n * 0.75))], 2)
    except Exception:
        return out
    return out


# ── v455：估值历史序列（让 A股/港股也有"一年分位"，而不是等 60 天快照）────
#
# 问题：`cn_valuation_snapshots` 每天只落 1 个点（东财当期 PE/PB），每票 1~7 个点，
# 门槛 60 → 估值分量恒为 None，分数只剩"盈利增速"一把尺。实测后果很具体：
# 东山精密 / 新易盛 的 score 直接打满 1.0 —— 没有估值约束的高增长就是满分。
#
# 解法与美股 v454 同构：两个**已公布的真实数**相除（当日收盘 ÷ 当时已公布的 EPS TTM），
# 不重放采集、不补积压。区别只有一处：A股季报的"已知日"没有逐条申报日可用，
# 所以用**法定披露截止日**（宁可晚用，不可早用 —— 前视比滞后危险得多）。

_LEGAL_KNOW = {"Q1": (4, 30), "H1": (8, 31), "Q3": (10, 31), "FY": (4, 30)}


def _label_kind(label: str) -> Optional[str]:
    """报告标签 → Q1/H1/Q3/FY（年报的截止日在次年）。"""
    n = _a_quarter_count(label)
    if n == 1:
        return "Q1"
    if n == 2:
        return "H1"
    if n == 3:
        return "Q3"
    if n == 4:
        return "FY"
    return None


def known_date(report_date: str, label: str) -> str:
    """报告期 + 标签 → **对外可知**的最晚日期（YYYY-MM-DD）。

    A股法定披露截止：一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30。
    拿同一天当"已公布"是安全的保守做法：真实公告只会更早，因此不会产生前视；
    反过来用报告期末当已知日，就等于提前 1~4 个月知道还没披露的业绩。
    """
    text = str(report_date or "")[:10]
    if not text[:4].isdigit():
        return text
    kind = _label_kind(label)
    if not kind:
        return text
    month, day = _LEGAL_KNOW[kind]
    year = int(text[:4]) + (1 if kind == "FY" else 0)
    return f"{year:04d}-{month:02d}-{day:02d}"


def ttm_at(rows: List[Dict[str, Any]], field: str, idx: int = 0) -> Optional[float]:
    """第 idx 期（rows 按报告期倒序）的 TTM 值。

    `_a_ttm` 只算 rows[0]；这里靠切片复用同一套「本期累计 + 上年年报 − 上年同期累计」，
    避免为每个历史期另写一套还原逻辑（两套逻辑迟早会不一致）。
    """
    if idx < 0 or idx >= len(rows):
        return None
    return _a_ttm(rows[idx:], field)


def _period_points(rows: List[Dict[str, Any]], market: str
                   ) -> List[Tuple[int, Optional[float], Optional[float]]]:
    """财报序列 → [(可知日 ts, EPS_TTM, BPS)]，按可知日升序。"""
    out: List[Tuple[int, Optional[float], Optional[float]]] = []
    for idx, row in enumerate(rows):
        # A股列名是 REPORTDATE / DATEMMDD，港股（东财 F10）是 REPORT_DATE / REPORT_TYPE ——
        # 只认一套会让港股整票推导出 0 个点（实测首轮就是这样静默空掉的）。
        rd = str(row.get("REPORTDATE") or row.get("REPORT_DATE")
                 or row.get("STD_REPORT_DATE") or "")[:10]
        label = str(row.get("DATEMMDD") or row.get("REPORT_TYPE") or "")
        if not rd:
            continue
        kd = known_date(rd, label)
        try:
            ts = int(time.mktime(time.strptime(kd, "%Y-%m-%d")))
        except Exception:
            continue
        if market == "HK":
            eps = _num(row.get("EPS_TTM"))
            bps = _num(row.get("BPS")) or _num(row.get("BOOK_VALUE_PER_SHARE"))
        else:
            eps = ttm_at(rows, "BASIC_EPS", idx)
            bps = _num(row.get("BPS"))
        if eps is None and bps is None:
            continue
        out.append((ts, eps, bps))
    out.sort(key=lambda x: x[0])
    return out


def valuation_series(rows: List[Dict[str, Any]], bars: List[List[float]],
                     market: str = "A", days: int = _PE_WINDOW_DAYS
                     ) -> List[Tuple[int, Optional[float], Optional[float]]]:
    """[财报序列 × 日线] → [(当日 ts, PE, PB)]，只保留近 `days` 天。

    每个交易日都用「该日之前**已公布**的最后一期财报」：这是防前视的唯一实现方式，
    也是本函数唯一值得被单测的地方 —— 用未来财报解释历史价格会凭空造出"便宜"。
    """
    points = _period_points(rows, market)
    if not points or not bars:
        return []
    cutoff = int(time.time()) - days * 86400
    out: List[Tuple[int, Optional[float], Optional[float]]] = []
    j = -1
    for bar in bars:
        try:
            ts = int(float(bar[0]) / 1000.0) if float(bar[0]) > 1e11 else int(bar[0])
            close = float(bar[4])
        except (TypeError, ValueError, IndexError):
            continue
        if close <= 0:
            continue
        while j + 1 < len(points) and points[j + 1][0] <= ts:
            j += 1
        if ts < cutoff:
            continue
        if j < 0:
            out.append((ts, None, None))      # 财报还没公布：留空，不猜
            continue
        _kd, eps, bps = points[j]
        pe = round(close / eps, 2) if eps and eps > 0 else None
        pb = round(close / bps, 2) if bps and bps > 0 else None
        out.append((ts, pe, pb))
    return out


def backfill_valuation(conn: sqlite3.Connection, ticker: str, market: str,
                       days: int = _PE_WINDOW_DAYS, bars_limit: int = 320,
                       dry_run: bool = False) -> Dict[str, int]:
    """把推导出的 PE/PB 日序列写进 `cn_valuation_snapshots`（幂等，可重放）。

    写库而不是让决策链现算：决策链是热路径且**不能联网**（见 build 的注释）。
    真实东财快照同日已有则**不覆盖**（`source != 'eastmoney'` 才更新）—— 采到的优先于推出来的。
    """
    import cn_prices as _cp
    stats = {"rows": 0, "pe": 0, "pb": 0, "skipped": 0}
    market = str(market or "").upper()
    if market == "A":
        digits = str(ticker or "").split(".")[0]
        rows = _em("RPT_LICO_FN_CPD", f'(SECURITY_CODE="{digits}")', sort="REPORTDATE")
    elif market == "HK":
        digits = str(ticker or "").split(".")[0]
        rows = _em("RPT_HKF10_FN_MAININDICATOR", f'(SECUCODE="{digits}.HK")')
    else:
        return stats
    if not rows:
        return stats
    bars = _cp.fetch_stock_history(ticker, market, limit=bars_limit) or []
    series = valuation_series(rows, bars, market=market, days=days)
    if not series:
        return stats
    if dry_run:
        stats["rows"] = len(series)
        stats["pe"] = sum(1 for _, pe, _ in series if pe is not None)
        stats["pb"] = sum(1 for _, _, pb in series if pb is not None)
        return stats
    for ts, pe, pb in series:
        if pe is None and pb is None:
            stats["skipped"] += 1
            continue
        try:
            conn.execute(
                """INSERT INTO cn_valuation_snapshots(ticker, market, ts, pe, pb, source)
                   VALUES (?,?,?,?,?, 'derived')
                   ON CONFLICT(ticker, market, ts) DO UPDATE SET
                     pe=excluded.pe, pb=excluded.pb, source=excluded.source
                   WHERE cn_valuation_snapshots.source != 'eastmoney'""",
                (ticker, market, int(ts), pe, pb))
            stats["rows"] += 1
            stats["pe"] += 1 if pe is not None else 0
            stats["pb"] += 1 if pb is not None else 0
        except Exception:
            stats["skipped"] += 1
    try:
        conn.commit()
    except Exception:
        pass
    return stats


def load(conn: Optional[sqlite3.Connection] = None, ticker: str = "",
         market: str = "", ttl: Optional[int] = None) -> Dict[str, Any]:
    """取一只标的的（尽可能新）读数：库里够新就用库，否则抓一次并落库。

    **这是本模块唯一会对网络发起请求的入口**，且只在"代码真的解析出来"时才被调用。
    """
    ttl = _TTL if ttl is None else int(ttl)
    owned = conn is None
    if owned:
        import db as _db
        conn = _db.get_connection()
    try:
        rec = read_one(conn, ticker, market)
        fresh = rec and (time.time() - int(rec.get("fetched_at") or 0)) < ttl
        if fresh:
            return rec
        with _fetch_lock:
            rec2 = read_one(conn, ticker, market)      # 双检：别的线程可能刚抓过
            if rec2 and (time.time() - int(rec2.get("fetched_at") or 0)) < ttl:
                return rec2
            fetched = fetch_history(market, ticker)
            if fetched.get("error"):
                return rec or fetched
            ensure_tables(conn)
            persist_history(conn, fetched.get("records") or [])
            return fetched.get("latest") or {}
    finally:
        if owned and conn is not None:
            conn.close()


# ── 给 prompt 用的一行（与美股层同 shape，只是字段名多几个）──────────────────
def layer_items(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """一只 A股/港股 → macro_context 的一层条目（每票 8 条，与美股层对齐）。"""
    if not rec or rec.get("error"):
        return []
    label = f"{rec.get('ticker') or ''}"
    extra = {"freshness": "quarterly", "period_end": str(rec.get("report_date") or ""),
             "scope": "财报", "label": label, "market": rec.get("market"),
             "name": rec.get("name") or "", "industry": rec.get("industry") or ""}
    rows: List[Dict[str, Any]] = []

    def _m(key: str, value: Optional[float], unit: str) -> None:
        rows.append({"category": "fundamentals", "source": str(rec.get("source") or "eastmoney"),
                     "metric_key": key, "value": value, "unit": unit,
                     "status": "ok" if value is not None else "unavailable",
                     "payload": dict(extra)})

    rev = _num(rec.get("revenue_ttm"))
    _m("fund.revenue_ttm", (rev / 1e8) if rev is not None else None, "亿")
    _m("fund.eps_ttm", _num(rec.get("eps_ttm")), "元")
    _m("fund.revenue_yoy", _num(rec.get("revenue_yoy")), "%")
    _m("fund.net_profit_yoy", _num(rec.get("net_profit_yoy")), "%")
    _m("fund.roe", _num(rec.get("roe_annualized")) if rec.get("market") == "A"
       else _num(rec.get("roe_annualized")), "%")
    _m("fund.gross_margin", _num(rec.get("gross_margin")), "%")
    _m("fund.pe_ttm", _num(rec.get("pe_ttm")), "x")
    _m("fund.pb", _num(rec.get("pb")), "x")
    return rows



# ── 名称 → 代码 字典 ──────────────────────────────────────────────────────
# 为什么要这张表：新闻正文里出现的往往是"东山精密""中科飞测"这种简称，模型给的是
# `CN_STOCK` 占位标签 —— 只要没有"简称→代码"的对照，A股/港股基本面就永远触发不了。
# 实测占位决策 28 条里只有 4 条能被静态别名表解出（14%），缺口全在"没收录的简称"。
# 抓取只由定时任务做，决策链路只读这张表（零联网）。

_NAME_LIST_SOURCES = {
    # reportName, 日期字段名（A股是 REPORTDATE、港股是 REPORT_DATE —— 两者不一样，别写错）
    "A": ("RPT_LICO_FN_CPD", "REPORTDATE"),
    "HK": ("RPT_HKF10_FN_MAININDICATOR", "REPORT_DATE"),
}


def _fetch_name_page(market: str, page: int, size: int,
                     report_date: str = "") -> Tuple[List[Dict[str, Any]], int]:
    """取一页"代码+简称"。

    必须带 `filter=(日期字段='某一期')`：不带过滤时接口按代码排序、同一只票会把
    它上百个报告期全吐出来（实测 500 行里只有 5 只票），翻 500 页也凑不齐。
    加上单期过滤后 A股 ≈ 5800 只、12 页就能拿完。
    """
    report, date_field = _NAME_LIST_SOURCES[market]
    cols = ("SECURITY_CODE", "SECURITY_NAME_ABBR")
    params = {"reportName": report, "columns": ",".join(cols),
              "pageSize": str(size), "pageNumber": str(page), "source": "WEB", "client": "WEB"}
    if report_date:
        params["filter"] = f"({date_field}='{report_date}')"
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urlencode(params)
    body = _get(url)
    if not isinstance(body, dict):
        return [], 0
    res = body.get("result") or {}
    return list(res.get("data") or []), int(res.get("count") or 0)




def _quarter_ends(n: int = 8) -> List[str]:
    """最近 n 个季末（港股各家用不同报告期，按自然季末去试，命中率比"按接口排序取日期"高）。

    实测：2026-06-30 有 2230 家、2025-12-31 有 2258 家，而 2026-08-31 只有 9 家 ——
    所以必须挑"大报告期"，不能只取接口默认排序的头几个日期。
    """
    import datetime as _dt
    today = _dt.date.today()
    out: List[str] = []
    year, month = today.year, ((today.month - 1) // 3) * 3 + 3
    while len(out) < n:
        if month > 12:
            year, month = year + 1, 3
            continue
        last_day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
        d = _dt.date(year, month, last_day)
        if d <= today:
            out.append(d.strftime("%Y-%m-%d"))
        month -= 3
        if month <= 0:
            year, month = year - 1, 12
    return out


def _recent_report_dates(market: str, limit: int = 12) -> List[str]:
    """该市场最近若干个报告期（港股各家报告期分散，单期只有一小撮公司）。"""
    report, date_field = _NAME_LIST_SOURCES[market]
    params = {"reportName": report, "columns": "SECURITY_CODE," + date_field, "pageSize": "600",
              "pageNumber": "1", "sortColumns": date_field, "sortTypes": "-1",
              "source": "WEB", "client": "WEB"}
    url = _EM_URL + "?" + urlencode(params)
    body = _get(url)
    rows = ((((body or {}).get("result") or {}).get("data")) or []) if isinstance(body, dict) else []
    out: List[str] = []
    for r in rows:
        d = str(r.get(date_field) or "")[:10]
        if d and d not in out:
            out.append(d)
        if len(out) >= limit:
            break
    return out


def _latest_report_date(market: str) -> str:
    """取该市场最新一期报告期（用于单期过滤，保证一票一行）。"""
    report, date_field = _NAME_LIST_SOURCES[market]
    params = {"reportName": report, "columns": "SECURITY_CODE," + date_field, "pageSize": "1",
              "pageNumber": "1", "sortColumns": date_field, "sortTypes": "-1",
              "source": "WEB", "client": "WEB"}
    url = _EM_URL + "?" + urlencode(params)
    body = _get(url)
    rows = ((((body or {}).get("result") or {}).get("data")) or []) if isinstance(body, dict) else []
    if not rows:
        return ""
    return str(rows[0].get(date_field) or "")[:10]


def refresh_name_map(conn: Optional[sqlite3.Connection] = None, markets: Tuple[str, ...] = ("A", "HK"),
                     page_size: int = 500, max_pages: int = 20) -> Dict[str, int]:
    """把全市场的"简称→代码"拉下来入库（A股约 5400 只、港股约 2600 只）。

    幂等：按 (name, market) upsert；重复跑只会刷新 updated_at。
    只在 cron 里调，决策链路绝不调（会联网、要几十秒）。
    """
    owned = conn is None
    if owned:
        import db as _db
        conn = _db.get_connection()
    written: Dict[str, int] = {}
    try:
        ensure_tables(conn)
        for market in markets:
            seen: set = set()
            now = int(time.time())
            rows_out = 0
            total = None
            # A股：5800 只同一期出齐，一期就够；港股：单期只挂在 290 家（各家报告期不同），
            # 所以港股要扫最近若干个报告期再合并，否则字典只有 290 个名字。
            dates = _quarter_ends(8) if market == "HK" else []
            report_date = _latest_report_date(market)
            if not report_date:
                written[market] = 0
                continue
            batches = dates or [report_date]
            for rd in batches:
                seen_rd = seen if rd == batches[0] else set()
                total = None
                for page in range(1, max_pages + 1):
                    rows, count = _fetch_name_page(market, page, page_size, rd)
                    if total is None:
                        total = count
                    if not rows:
                        break
                    for r in rows:
                        code = str(r.get("SECURITY_CODE") or "").strip()
                        name = str(r.get("SECURITY_NAME_ABBR") or "").strip()
                        if not code or not name or (name, market) in seen_rd:
                            continue
                        seen_rd.add((name, market))
                        norm = resolve_hk(code) if market == "HK" else resolve_a_share(code)
                        if not norm:
                            continue
                        conn.execute(
                            "INSERT INTO cn_name_map(name, code, market, updated_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(name, market) DO UPDATE SET code=excluded.code, "
                            "updated_at=excluded.updated_at",
                            (name, norm, market, now))
                        rows_out += 1
                    if total and page * page_size >= total:
                        break
            written[market] = rows_out
        conn.commit()
    finally:
        if owned and conn is not None:
            conn.close()
    return written


def name_map(conn: Optional[sqlite3.Connection] = None, market: Optional[str] = None,
             max_age_days: int = 30) -> Dict[str, Tuple[str, str]]:
    """读"简称→(kind, code)"；只读库，不联网。超过 max_age_days 太旧的直接不用（宁可解不出）。"""
    owned = conn is None
    if owned:
        import db as _db
        try:
            conn = _db.get_connection()
        except Exception:
            return {}
    out: Dict[str, Tuple[str, str]] = {}
    try:
        ensure_tables(conn)
        cutoff = int(time.time()) - int(max_age_days) * 86400
        sql = "SELECT name, code, market FROM cn_name_map"
        args: Tuple[Any, ...] = ()
        if market:
            sql += " WHERE market=?"
            args = (market,)
        for name, code, mkt in conn.execute(sql, args):
            kind = "hk" if str(mkt).upper() == "HK" else "a_share"
            out[str(name)] = (kind, str(code))
    except Exception:
        return {}
    finally:
        if owned and conn is not None:
            conn.close()
    return out


def build(ticker: str = "", market: str = "", conn: Optional[sqlite3.Connection] = None,
          use_cache: bool = True, allow_fetch: bool = False) -> Dict[str, Any]:
    """A股/港股的基本面方向读数 `score ∈ [-1,1]`（与 fundamental_factor 同 shape）。

    分量口径（为什么这么配）：
      · 盈利修正 0.65：净利累计同比 0.45 / 营收累计同比 0.35 / 年化ROE 0.20
        —— 单看净利同比会被一次性损益带偏，所以营收和 ROE 都要进来；
      · 估值分位 0.35：PE 分位（亏损则退 PB 分位），样本 <60 不作结论。
    """
    market = str(market or "").upper()
    if market not in ("A", "HK") or not ticker:
        return {"available": False, "score": 0.0, "ticker": ticker or None,
                "components": {}, "evidence": [],
                "detail": "非 A股/港股代码，无本地基本面", "market": market or None}

    def _clamp(v: float) -> float:
        return max(-1.0, min(1.0, float(v)))

    # 决策链路默认**不联网**（allow_fetch=False）：库里读到什么就用什么，宁可旧一点。
    # 定时任务（cron_equity_fundamentals.py）负责把新数据抓进来。
    # 为什么必须这样分开：A股/港股代码一旦被新闻反解出来，决策链路就会开始真调 CN 分支，
    # 若这里保留"过期就同步抓东财"的行为，每条相关新闻都可能 stall 最多 _TIMEOUT=8 秒，
    # 还带着锁 —— 一条新闻卡住，整批决策排队。
    if allow_fetch:
        rec = (load(conn, ticker, market) if use_cache else fetch_one(market, ticker))
    else:
        _own = conn is None
        _c = conn
        if _own:
            try:
                import sqlite3
                _c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
            except Exception:
                _c = None
        try:
            rec = read_one(_c, ticker, market) if _c is not None else {}
        finally:
            if _own and _c is not None:
                _c.close()
    if not rec or rec.get("error"):
        return {"available": False, "score": 0.0, "ticker": ticker,
                "components": {}, "evidence": [],
                "detail": f"{ticker} 无财务读数（{(rec or {}).get('error') or '东财空返回'}）",
                "market": market}
    # 不联网时读到的可能是旧值：如实标出「几天没更新」，别把过期读数当成当期
    stale_days = 0
    try:
        fetched_at = int(rec.get("fetched_at") or 0)
        if fetched_at:
            stale_days = max(0, int((time.time() - fetched_at) // 86400))
    except Exception:
        stale_days = 0

    # v489：多期历史（零网络）。读不到就退回静态缺口，不影响方向分。
    history: List[Dict[str, Any]] = []
    try:
        import db as _db
        hconn = conn if conn is not None else _db.get_connection()
        try:
            history = read_history(hconn, ticker, market)
        finally:
            if conn is None and hconn is not None:
                hconn.close()
    except Exception:
        history = []
    depth = cn_depth.readout(history)

    evidence: List[str] = []
    bits: List[str] = []
    np_yoy = _num(rec.get("net_profit_yoy"))
    rev_yoy = _num(rec.get("revenue_yoy"))
    roe = _num(rec.get("roe_annualized"))
    parts: List[Tuple[Optional[float], float]] = []
    if np_yoy is not None:
        parts.append((_clamp(np_yoy / 40.0), 0.45))
        evidence.append(f"归母净利累计同比 {np_yoy:+.1f}%")
    if rev_yoy is not None:
        parts.append((_clamp(rev_yoy / 30.0), 0.35))
        evidence.append(f"营收累计同比 {rev_yoy:+.1f}%")
    if roe is not None:
        parts.append((_clamp((roe - 10.0) / 15.0), 0.20))
        evidence.append(f"年化ROE {roe:.1f}%")
    revision = None
    if parts:
        wsum = sum(w for _, w in parts)
        revision = round(sum(v * w for v, w in parts) / wsum, 4)

    if depth.get("revenue_ttm_yoy") is not None:
        evidence.append(f"TTM营收同比 {depth['revenue_ttm_yoy']:+.1f}%"
                        f"（{depth.get('points')} 期）")
    if depth.get("earnings_position") is not None:
        evidence.append(f"盈利位置 {depth['earnings_position'] * 100:.0f}%"
                        f"（{depth.get('earnings_zone')}，n={depth.get('points')}）")

    valuation = None
    owner = conn is None
    val_conn = conn
    if owner:
        import db as _db
        val_conn = _db.get_connection()
    try:
        pct = pe_percentile(val_conn, ticker, market)
    finally:
        if owner and val_conn is not None:
            val_conn.close()
    pe_pos, pb_pos = pct.get("pe_position"), pct.get("pb_position")
    if pe_pos is not None:
        valuation = round(_clamp((50.0 - float(pe_pos)) / 50.0), 4)
        evidence.append(f"PE {pct.get('pe'):.2f}x，近一年 {pe_pos:.0f}% 分位（n={pct.get('pe_samples')}）")
    elif pb_pos is not None:
        valuation = round(_clamp((50.0 - float(pb_pos)) / 50.0), 4)
        evidence.append(f"PE 不可用（亏损或样本不足），退 PB {pct.get('pb'):.2f}x 分位 {pb_pos:.0f}%（n={pct.get('pb_samples')}）")
    else:
        if _num(rec.get("pe_ttm")) is None:
            bits.append(f"PE 不适用（TTM EPS {rec.get('eps_ttm') or 'N/A'}，亏损股不给分位）")
        bits.append(f"估值分位样本不足（PE n={pct.get('pe_samples')} / PB n={pct.get('pb_samples')}，<{_PE_MIN_SAMPLES} 不作结论）")

    comps = [(v, w) for v, w in ((valuation, 0.35), (revision, 0.65)) if v is not None]
    available = bool(comps)
    score = 0.0
    if available:
        wsum = sum(w for _, w in comps)
        score = round(sum(v * w for v, w in comps) / wsum, 4)
    return {
        "available": available,
        "score": score,
        "ticker": ticker,
        "market": market,
        "name": rec.get("name") or "",
        "components": {"valuation_percentile": valuation, "earnings_revision": revision,
                       "pe_percentile": pe_pos, "pb_percentile": pb_pos,
                       # v489：深度读数（只读入，不进 score）
                       "depth_revenue_ttm_yoy": depth.get("revenue_ttm_yoy"),
                       "depth_net_profit_ttm_yoy": depth.get("net_profit_ttm_yoy"),
                       "depth_eps_ttm_yoy": depth.get("eps_ttm_yoy"),
                       "depth_gross_margin_ttm": _num(rec.get("gross_margin")),
                       # 与美股 company_depth 的键名对齐，decision_guard 才能统一渲染
                       "depth_gm_delta_pp": depth.get("gross_margin_delta_pp"),
                       "depth_gross_margin_delta_pp": depth.get("gross_margin_delta_pp"),
                       "depth_roe_delta_pp": depth.get("roe_delta_pp"),
                       "depth_earnings_position": depth.get("earnings_position"),
                       "depth_earnings_zone": depth.get("earnings_zone"),
                       "depth_leverage": depth.get("leverage"),
                       "depth_points": depth.get("points")},
        "evidence": evidence[:5],
        "flags": depth.get("flags") or [],
        "report_date": rec.get("report_date") or "",
        "period_label": rec.get("period_label") or "",
        "earnings_in_days": None,
        "stale_days": stale_days,
        # v489：多期历史接上后，「无季度历史」不再是固定借口；真实缺口由 cn_depth 给。
        "gaps": depth.get("gaps") or [
            "无三表：只有东财主要指标（营收/净利/ROE/毛利率/每股经营现金流）",
            "无季度历史：库里只有最新一期，算不出盈利位置/利润率趋势",
            "无同业可比：8 票跨 8 个行业，样本不足不造数",
            "无财报日历：不知道下一次业绩披露是什么时候",
            "无宏观传导：宏观倾斜只对美股板块生效",
        ],
        "detail": (("；".join(bits) if bits else "净利/营收/ROE + 估值分位均可得")
                   + (f"；读数 {stale_days} 天未更新（不联网，等定时任务补）" if stale_days > 1 else "")),
        "asof": int(rec.get("fetched_at") or time.time()),
    }
