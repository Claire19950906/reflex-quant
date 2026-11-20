#!/usr/bin/env python3
"""美股财报数据层（v405）—— 利润表 / 资产负债表 / 现金流量表 + 财报日历 + 一致预期。

为什么单独一层：
  此前 `equity_feed.py` 只从 FMP `ratios-ttm` 拿 PE/PEG/净利率/负债比四个 TTM 比率，
  没有营收、没有 EPS、没有现金流、没有财报日、没有一致预期 —— 也就是说
  「知道贵不贵」，但「不知道为什么贵、什么时候会被财报打脸」全都不知道。

数据源（全部实测 200，免费 key 可用）：
  /stable/income-statement?period=quarter&limit=8   —— 8 个季度：算 TTM + 同比
  /stable/balance-sheet-statement?limit=2           —— 资产/负债/权益
  /stable/cash-flow-statement?period=quarter&limit=8 —— 经营现金流 / 资本开支
  /stable/earnings-calendar?from=&to=               —— 财报日 + epsEstimated/Actual

铁律（与 `docs/DATA_AUTHENTICITY.md` 一致）：
  · 取不到就是 None → status=unavailable，不插值、不拿别的季度顶替
  · 财报是**季度快变量**、日历是**日频**，两者 TTL 各自分级（不是 6h 一刀切）
  · 失败做 30 分钟负缓存，避免把 250 次/日的免费配额打空
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import config

HttpGet = Callable[[str, int], Any]

_TIMEOUT = 15
_RETRIES = 2
_STMT_TTL = 86400.0    # 三表：季报，日更足够（1 次/日 × 3 端点 × N 只）
_CAL_TTL = 43200.0     # 财报日历：12h（财报日前后的 actual 需要较快回填）
_NEG_TTL = 1800.0      # 失败负缓存 30min
_QUARTERS = 8          # 同比需要「最近 4 季 vs 前 4 季」

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_av_lock = threading.Lock()
_av_last_call = 0.0
# AV 免费档硬限流 5 次/分钟：抓取脚本设 TRIDENT_AV_THROTTLE_SEC=13；
# 主决策链路不设（它只读库，不发 AV 请求），避免把事件循环睡死。
_AV_MIN_GAP = float(os.getenv("TRIDENT_AV_THROTTLE_SEC", "0") or 0.0)

# ── AV 日配额账本（v406）────────────────────────────────────────────────────
# 为什么必须记账：AV 免费档 25 次/日，超了**不报 429**，而是返回 200 + Note 文本。
# 不记账的后果是"跑了 30 只票、每只都写一条 unavailable"，看着像全市场没数据，
# 实际是自己把配额打空了 —— 属于典型的"失败不可见"。
_BUDGET_LOCK = threading.Lock()


def _budget_dir() -> str:
    """配额账本目录：与 DB 同级的 runtime/（不存在就退回临时目录，不炸）。"""
    db_path = str(getattr(config, "DB_PATH", "") or "")
    base = os.path.dirname(os.path.abspath(db_path)) if db_path else ""
    for candidate in (os.path.join(base, "runtime"), base):
        if candidate and os.path.isdir(candidate):
            return candidate
    return os.path.dirname(os.path.abspath(db_path)) or "."


def _budget_file(day: Optional[str] = None) -> str:
    day = day or time.strftime("%Y-%m-%d")
    return os.path.join(_budget_dir(), f"av_budget_{day}.json")


def budget_state(day: Optional[str] = None) -> Dict[str, int]:
    """当日 AV 调用情况：used / limit / remaining。limit=0 表示不设限。"""
    limit = int(getattr(config, "AV_DAILY_BUDGET", 25) or 0)
    used = 0
    try:
        with open(_budget_file(day), "r", encoding="utf-8") as fh:
            used = int(json.load(fh).get("used") or 0)
    except (OSError, ValueError, TypeError):
        used = 0
    return {"used": used, "limit": limit,
            "remaining": max(0, limit - used) if limit > 0 else 10 ** 9}


def _take_av_budget() -> str:
    """占用 1 次配额。返回空串=可以调用；返回文本=配额已用尽（调用方按不可用处理）。"""
    with _BUDGET_LOCK:
        state = budget_state()
        if state["limit"] > 0 and state["used"] >= state["limit"]:
            return (f"AV 日配额已用尽（{state['used']}/{state['limit']}），"
                    f"不发起请求；如需扩容改 AV_DAILY_BUDGET")
        try:
            os.makedirs(_budget_dir(), exist_ok=True)
            with open(_budget_file(), "w", encoding="utf-8") as fh:
                json.dump({"used": state["used"] + 1, "limit": state["limit"],
                           "at": int(time.time())}, fh)
        except OSError:
            pass   # 账本写不进去也不能挡住抓取本身
        return ""


# ── 基础工具 ────────────────────────────────────────────────────────────────
def _num(value: Any) -> Optional[float]:
    """转 float；None/''/'N/A'/NaN 一律 None（占位值不得冒充数据）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().upper() in ("", "N/A", "NA", "NONE", "NULL", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    """带 2 次退避重试的 JSON GET。FMP 本机直连会间歇 SSL EOF，一次失败不算死。"""
    last: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))
    assert last is not None
    raise last


def _cached(key: str, ttl: float, loader: Callable[[], Any]) -> Any:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    try:
        value = loader()
    except Exception as exc:
        value = {"_error": f"{type(exc).__name__}: {str(exc)[:80]}"}
        ttl = _NEG_TTL
    with _cache_lock:
        _cache[key] = (now + ttl, value)
    return value


def _fmp(path: str) -> Any:
    base = str(getattr(config, "FMP_BASE_URL", "") or "https://financialmodelingprep.com").rstrip("/")
    key = str(getattr(config, "FMP_API_KEY", "") or "")
    if not key:
        raise RuntimeError("未配置 FMP_API_KEY")
    return _default_get(f"{base}{path}&apikey={key}")


def _av(function: str, ticker: str) -> Any:
    """Alpha Vantage 兜底（免 key 限额 25 次/日，所以只在与 FMP 失败时用）。"""
    key = str(getattr(config, "ALPHAVANTAGE_API_KEY", "") or "")
    if not key:
        raise RuntimeError("未配置 ALPHAVANTAGE_API_KEY")
    exhausted = _take_av_budget()
    if exhausted:
        raise RuntimeError(exhausted)
    if _AV_MIN_GAP > 0:
        global _av_last_call
        with _av_lock:
            wait = _AV_MIN_GAP - (time.time() - _av_last_call)
            if wait > 0:
                time.sleep(wait)
            _av_last_call = time.time()
    return _default_get(
        f"https://www.alphavantage.co/query?function={function}&symbol={ticker}&apikey={key}")


def _av_error(body: Any) -> str:
    """AV 配额/限流在 200 里返回 Note/Information 字段——必须当失败，不能当数据。

    AV 免费档有「5 次/分钟」硬限流，超了不报 429，而是 200 + Information 文本。
    """
    if not isinstance(body, dict):
        return "非 JSON 对象"
    for key in ("Note", "Information", "Error Message"):
        if body.get(key):
            return str(body[key])[:90]
    if not body.get("quarterlyReports") and not body.get("annualReports") \
            and not body.get("quarterlyEarnings") and not body.get("symbol"):
        return f"空返回(keys={sorted(body.keys())[:4]})"
    return ""


# ── 解析（纯函数，便于离线测试）────────────────────────────────────────────
def _merge_statements(primary: List[Dict[str, Any]],
                      extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 period_end 合并两路三表：主源保留，副源只补空字段，缺的期直接追加。"""
    fields = ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
              "total_assets", "total_liabilities", "total_equity",
              "operating_cash_flow", "capex")
    idx: Dict[str, Dict[str, Any]] = {str(r.get("period_end")): dict(r) for r in primary}
    for rec in extra:
        key = str(rec.get("period_end"))
        if key not in idx:
            idx[key] = dict(rec)
            continue
        for f in fields:
            if idx[key].get(f) is None and rec.get(f) is not None:
                idx[key][f] = rec[f]
    return sorted(idx.values(), key=lambda r: str(r.get("period_end")), reverse=True)


def parse_statements(
    income: Any,
    balance: Any,
    cashflow: Any,
) -> List[Dict[str, Any]]:
    """把三个端点的返回合并成「按季度」的原始行。缺失字段一律 None，不补。

    同时支持两种返回形状：
      · FMP  —— list（每季度一行），资产负债表另取最新一期
      · AV   —— dict（{annualReports, quarterlyReports}），三张表都是 dict
    """
    if isinstance(income, dict) and income.get("quarterlyReports"):
        return parse_av_statements(income, balance, cashflow)
    if not isinstance(income, list):
        return []
    bal = balance[0] if isinstance(balance, list) and balance else {}
    cf_by_date = {}
    if isinstance(cashflow, list):
        for row in cashflow:
            if isinstance(row, dict) and row.get("date"):
                cf_by_date[str(row.get("date"))] = row

    out: List[Dict[str, Any]] = []
    for row in income:
        if not isinstance(row, dict) or not row.get("date"):
            continue
        date = str(row["date"])
        cf = cf_by_date.get(date) or {}
        out.append({
            "ticker": str(row.get("symbol") or "").upper(),
            "period_end": date,
            "fiscal_year": str(row.get("fiscalYear") or row.get("calendarYear") or ""),
            "period": str(row.get("period") or ""),
            "revenue": _num(row.get("revenue")),
            "gross_profit": _num(row.get("grossProfit")),
            "operating_income": _num(row.get("operatingIncome")),
            "net_income": _num(row.get("netIncome")),
            "eps_diluted": _num(row.get("epsDiluted")) if _num(row.get("epsDiluted")) is not None
            else _num(row.get("eps")),
            # 资产负债表只有最新一期（历史快照对"当前判断"没有增量价值）
            "total_assets": _num(bal.get("totalAssets")),
            "total_liabilities": _num(bal.get("totalLiabilities")),
            "total_equity": _num(bal.get("totalStockholdersEquity")) if _num(bal.get("totalStockholdersEquity")) is not None
            else _num(bal.get("totalEquity")),
            "operating_cash_flow": _num(cf.get("operatingCashFlow")),
            "capex": _num(cf.get("capitalExpenditure")),
            "source": "fmp",
            "payload": {
                "balance_date": str(bal.get("date") or ""),
                "income_filling_date": str(row.get("filingDate") or ""),
            },
        })
    return out


def parse_av_statements(income: Any, balance: Any, cashflow: Any) -> List[Dict[str, Any]]:
    """Alpha Vantage INCOME_STATEMENT / BALANCE_SHEET / CASH_FLOW 兜底解析。

    AV 字段是字符串、单位是美元原值；`None`/`"None"` 一律当缺失。
    """
    if not isinstance(income, dict):
        return []
    quarters = income.get("quarterlyReports")
    if not isinstance(quarters, list):
        return []
    quarters = quarters[:8]   # AV 会返回 20 年历史；同比只需要最近 8 季
    # 资产负债表必须**按季对齐**：原来只取 quarterlyReports[0]（最新一季）然后贴给
    # 全部 8 个 income 行 —— 结果是「总资产/总负债/净资产」八个季度全是同一个数。
    # 实测 CLSK 8 季 total_equity 全等于 761283000、其余票直接 NULL，就是这么来的。
    # 这类错不报错、不吭声（客户端看到的是"有数据"），但任何按季的资产负债率/ROE 都会失真。
    bal_by_date = {}
    if isinstance(balance, dict) and isinstance(balance.get("quarterlyReports"), list):
        for row in balance["quarterlyReports"]:
            if isinstance(row, dict) and row.get("fiscalDateEnding"):
                bal_by_date[str(row["fiscalDateEnding"])] = row
    cf_by_date = {}
    if isinstance(cashflow, dict) and isinstance(cashflow.get("quarterlyReports"), list):
        for row in cashflow["quarterlyReports"]:
            if isinstance(row, dict) and row.get("fiscalDateEnding"):
                cf_by_date[str(row["fiscalDateEnding"])] = row
    out: List[Dict[str, Any]] = []
    for row in quarters:
        if not isinstance(row, dict) or not row.get("fiscalDateEnding"):
            continue
        date = str(row["fiscalDateEnding"])
        cf = cf_by_date.get(date) or {}
        bal_q = bal_by_date.get(date) or {}      # 严格按季对齐；缺就是缺，不拿别的季度顶
        out.append({
            "ticker": str(income.get("symbol") or "").upper(),
            "period_end": date,
            "fiscal_year": date[:4],
            "period": "",
            "revenue": _num(row.get("totalRevenue")),
            "gross_profit": _num(row.get("grossProfit")),
            "operating_income": _num(row.get("operatingIncome")),
            "net_income": _num(row.get("netIncome")),
            "eps_diluted": _num(row.get("reportedEPS")),
            "total_assets": _num(bal_q.get("totalAssets")),
            "total_liabilities": _num(bal_q.get("totalLiabilities")),
            "total_equity": _num(bal_q.get("totalShareholderEquity")),
            "operating_cash_flow": _num(cf.get("operatingCashflow")),
            "capex": _num(cf.get("capitalExpenditures")),
            "source": "alphavantage",
            "payload": {"balance_date": str(bal_q.get("fiscalDateEnding") or ""),
                        "income_filling_date": ""},
        })
    return out



def parse_earnings(body: Any) -> List[Dict[str, Any]]:
    """FMP earnings-calendar → 事件行。只有日期、没有 estimate/actual 的行也收（日期本身有用）。"""
    if not isinstance(body, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in body:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("symbol") or "").upper()
        date = str(row.get("date") or "")[:10]
        if not ticker or not date:
            continue
        out.append({
            "ticker": ticker,
            "event_date": date,
            "eps_estimated": _num(row.get("epsEstimated")),
            "eps_actual": _num(row.get("epsActual")),
            "revenue_estimated": _num(row.get("revenueEstimated")),
            "revenue_actual": _num(row.get("revenueActual")),
            "last_updated": str(row.get("lastUpdated") or ""),
            "source": "fmp",
        })
    return out


def parse_av_earnings(body: Any) -> List[Dict[str, Any]]:
    """Alpha Vantage EARNINGS → 一致预期历史（reportedEPS / estimatedEPS / surprisePercentage）。

    这是真正意义上的「预期差」原始数据：每个季度的实际 vs 预期都在里面，
    比只有"财报日"的日历强一档。字段缺失一律 None。
    """
    if not isinstance(body, dict):
        return []
    ticker = str(body.get("symbol") or "").upper()
    out: List[Dict[str, Any]] = []
    for row in (body.get("quarterlyEarnings") or []):
        if not isinstance(row, dict):
            continue
        date = str(row.get("reportedDate") or row.get("fiscalDateEnding") or "")[:10]
        if not ticker or not date:
            continue
        out.append({
            "ticker": ticker,
            "event_date": date,
            "eps_estimated": _num(row.get("estimatedEPS")),
            "eps_actual": _num(row.get("reportedEPS")),
            "revenue_estimated": None,
            "revenue_actual": None,
            "last_updated": "",
            "source": "alphavantage",
            "surprise_pct": _num(row.get("surprisePercentage")),
        })
    return out



def summarize(stmt_rows: List[Dict[str, Any]],
              eps_history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """从季度序列算 TTM 与同比。序列按 period_end 降序（最新在前）。

    返回字段全部可能为 None：季度不足 4 期算不出 TTM，不足 8 期算不出同比。

    v406 修：EPS 的 TTM **和同比** 都由同一条 EPS 序列得出。
    利润表没给 EPS 时（AV INCOME_STATEMENT 就是这种情况）用一致预期历史的
    reportedEPS 拼序列，并在 `eps_source` 标清来源 —— 不静默混源。

    v405 的教训写在这里：当时只补了 TTM 水平、没给同比补，结果 4/4 只票
    `yoy["eps_diluted"]` 全是 None，0.65 权重那一项实际退化成了"营收增速"，
    等于"盈利修正"这个分量名不符实。
    """
    rows = [r for r in sorted(stmt_rows, key=lambda r: str(r.get("period_end") or ""), reverse=True)]
    if not rows:
        return {"ttm": {}, "yoy": {}, "n_quarters": 0, "latest": None,
                "gross_margin": None, "net_margin": None, "eps_source": "none"}

    def _sum(field: str, seq: List[Dict[str, Any]]) -> Optional[float]:
        vals = [r.get(field) for r in seq]
        if any(v is None for v in vals):
            return None
        return float(sum(vals))

    def _f(value: Any) -> Optional[float]:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    latest = rows[0]
    ttm: Dict[str, Any] = {}
    yoy: Dict[str, Any] = {}
    eps_source = "income_statement"
    if len(rows) >= 4:
        last4 = rows[:4]
        ttm = {
            "revenue": _sum("revenue", last4),
            "gross_profit": _sum("gross_profit", last4),
            "operating_income": _sum("operating_income", last4),
            "net_income": _sum("net_income", last4),
            "eps_diluted": _sum("eps_diluted", last4),
            "operating_cash_flow": _sum("operating_cash_flow", last4),
        }

    eps_now = ttm.get("eps_diluted")
    eps_prev = _sum("eps_diluted", rows[4:8]) if len(rows) >= 8 else None
    if eps_now is None and eps_history:
        hist = [e for e in eps_history if _f(e.get("eps_actual")) is not None]
        hist.sort(key=lambda e: str(e.get("event_date") or ""), reverse=True)
        if len(hist) >= 4:
            eps_now = round(sum(_f(e["eps_actual"]) for e in hist[:4]), 4)
            eps_source = "earnings_history"
        if len(hist) >= 8:
            eps_prev = round(sum(_f(e["eps_actual"]) for e in hist[4:8]), 4)
            eps_source = "earnings_history"
    if eps_now is not None:
        ttm["eps_diluted"] = eps_now
        if eps_prev is not None and eps_prev < 0:
            # 负基数陷阱：EPS 从 -2.0 变成 +1.0 时 (now/prev-1) 会算出 -150%，
            # 明明是「扭亏为盈」却被读成「盈利恶化」。基数为负时同比**数学上无意义**，
            # 直接不算 —— 留给营收增速代理，并在 payload 里说明原因。
            eps_source = f"{eps_source}_neg_base"
        elif eps_prev is not None and eps_prev > 0:
            yoy["eps_diluted"] = round((eps_now / eps_prev - 1.0) * 100.0, 2)

    if len(rows) >= 8:
        prev4 = rows[4:8]
        for field in ("revenue", "net_income", "operating_cash_flow"):
            now_v = _sum(field, rows[:4])
            prev_v = _sum(field, prev4)
            if now_v is not None and prev_v not in (None, 0):
                yoy[field] = round((now_v / prev_v - 1.0) * 100.0, 2)

    gross_margin = None
    if ttm.get("gross_profit") is not None and ttm.get("revenue"):
        gross_margin = round(ttm["gross_profit"] / ttm["revenue"] * 100.0, 2)
    net_margin = None
    if ttm.get("net_income") is not None and ttm.get("revenue"):
        net_margin = round(ttm["net_income"] / ttm["revenue"] * 100.0, 2)
    return {
        "ttm": ttm,
        "yoy": yoy,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "n_quarters": len(rows),
        "eps_source": eps_source,
        "latest": latest,
        "total_assets": latest.get("total_assets"),
        "total_liabilities": latest.get("total_liabilities"),
        "total_equity": latest.get("total_equity"),
    }


def earnings_metrics(events: List[Dict[str, Any]], now_ts: Optional[float] = None) -> Dict[str, Any]:
    """从财报日历算「下次财报」「上次预期差」。没有 estimate/actual 就返回 None。"""
    today = time.strftime("%Y-%m-%d", time.localtime(now_ts or time.time()))
    upcoming = [e for e in events if str(e.get("event_date") or "") >= today]
    next_event = min(upcoming, key=lambda e: str(e.get("event_date"))) if upcoming else None
    reported = [e for e in events
                if e.get("eps_actual") is not None and e.get("eps_estimated") is not None
                and str(e.get("event_date") or "") <= today]
    last_report = max(reported, key=lambda e: str(e.get("event_date"))) if reported else None
    surprise_pct = None
    if last_report and last_report.get("eps_estimated") not in (None, 0):
        surprise_pct = round(
            (last_report["eps_actual"] - last_report["eps_estimated"]) / abs(last_report["eps_estimated"]) * 100.0,
            2,
        )
    return {
        "next_date": next_event.get("event_date") if next_event else None,
        "eps_estimated": next_event.get("eps_estimated") if next_event else None,
        "next_days": _days_between(today, next_event.get("event_date")) if next_event else None,
        "last_report_date": last_report.get("event_date") if last_report else None,
        "last_eps_actual": last_report.get("eps_actual") if last_report else None,
        "last_eps_estimated": last_report.get("eps_estimated") if last_report else None,
        "last_surprise_pct": surprise_pct,
    }


def _days_between(start: str, end: str) -> Optional[int]:
    try:
        t0 = time.mktime(time.strptime(start, "%Y-%m-%d"))
        t1 = time.mktime(time.strptime(end, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return None
    return int(round((t1 - t0) / 86400.0))


# ── 拉取 ────────────────────────────────────────────────────────────────────
def fetch_ticker(ticker: str) -> Dict[str, Any]:
    """单只票的三表原始返回（各自独立失败，互不连坐）。

    FMP 优先（免费档对 statement 端点单独限额，会返回 402），
    失败时按端点逐个退到 Alpha Vantage（INCOME_STATEMENT / BALANCE_SHEET / CASH_FLOW）。
    两源都拿不到 → 该端点就是 unavailable，不拼凑、不拿年度数顶季度数。
    """
    ticker = str(ticker or "").upper()
    if not ticker:
        return {"_error": "空 ticker"}
    errors: List[str] = []

    def _piece(name: str, fmp_path: str, av_function: str) -> Any:
        body = _cached(f"fmp:{name}:{ticker}", _STMT_TTL, lambda: _fmp(fmp_path))
        err = str(body.get("_error") or "") if isinstance(body, dict) else ""
        if not err and isinstance(body, list) and body:
            return body
        if not err and isinstance(body, dict) and body.get("_error"):
            err = str(body["_error"])
        errors.append(f"{name}(fmp):{err or '空返回'}")
        av = _cached(f"av:{name}:{ticker}", _STMT_TTL,
                     lambda: _av(av_function, ticker))
        av_err = _av_error(av)
        if not av_err:
            return av
        errors.append(f"{name}(av):{av_err}")
        return None

    income = _piece("income", f"/stable/income-statement?symbol={ticker}&period=quarter&limit={_QUARTERS}",
                    "INCOME_STATEMENT")
    balance = _piece("balance", f"/stable/balance-sheet-statement?symbol={ticker}&period=quarter&limit=2",
                     "BALANCE_SHEET")
    cashflow = _piece("cashflow", f"/stable/cash-flow-statement?symbol={ticker}&period=quarter&limit={_QUARTERS}",
                      "CASH_FLOW")
    return {"income": income, "balance": balance, "cashflow": cashflow, "errors": errors}



def fetch_calendar(days_back: int = 7, days_fwd: int = 90) -> Any:
    """财报日历（全市场，按窗口拉一次）。窗口含过去 7 天：actual 回填需要。

    v448：前瞻窗口 21 → 90 天。原因：实测库里 816 条事件里**未来只有 7 条**，
    于是 10 只已覆盖票的 `earnings_in_days` 全是 null —— 财报从来没进过"事件窗口"，
    只能当背景板。90 天能覆盖一个完整财报季（多数票一季一次）。
    """
    now = time.time()
    start = time.strftime("%Y-%m-%d", time.localtime(now - days_back * 86400))
    end = time.strftime("%Y-%m-%d", time.localtime(now + days_fwd * 86400))
    return _cached(
        f"calendar:{start}:{end}", _CAL_TTL,
        lambda: _fmp(f"/stable/earnings-calendar?from={start}&to={end}"),
    )



def _text_get(url: str, timeout: int = _TIMEOUT) -> str:
    """纯文本 GET（AV 的 EARNINGS_CALENDAR 返回 CSV，不是 JSON）。"""
    last: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))
    assert last is not None
    raise last


def parse_av_calendar(text: str, days_back: int = 7, days_fwd: int = 90) -> List[Dict[str, Any]]:
    """解析 AV EARNINGS_CALENDAR 的 CSV（symbol,name,reportDate,fiscalDateEnding,
    estimate,currency；**没有 actual**）。纯函数，便于离线测试。

    只认窗口内的事件；日期不合法的行直接丢，不猜。
    """
    import csv as _csv
    import io as _io
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days_back * 86400))
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() + days_fwd * 86400))
    out: List[Dict[str, Any]] = []
    reader = _csv.DictReader(_io.StringIO(text))
    for row in reader:
        ticker = str(row.get("symbol") or "").strip().upper()
        date = str(row.get("reportDate") or "").strip()[:10]
        if not ticker or len(date) != 10 or not (start <= date <= end):
            continue
        out.append({
            "ticker": ticker,
            "event_date": date,
            "eps_estimated": _num(row.get("estimate")),
            "eps_actual": None,
            "revenue_estimated": None,
            "revenue_actual": None,
            "source": "alphavantage",
        })
    return out


def fetch_av_calendar(horizon: str = "3month") -> str:
    """AV 全市场财报日历（CSV）。只在 FMP 的 calendar 端点失败时用，缓存 12h。"""
    key = str(getattr(config, "ALPHAVANTAGE_API_KEY", "") or "")
    if not key:
        raise RuntimeError("未配置 ALPHAVANTAGE_API_KEY")
    exhausted = _take_av_budget()
    if exhausted:
        raise RuntimeError(exhausted)
    return _cached(
        f"av:calendar:{horizon}", _CAL_TTL,
        lambda: _text_get(
            "https://www.alphavantage.co/query?function=EARNINGS_CALENDAR"
            f"&horizon={horizon}&apikey={key}"))


_YF_INC_MAP = (("Total Revenue", "revenue"), ("Gross Profit", "gross_profit"),
               ("Operating Income", "operating_income"), ("Net Income", "net_income"),
               ("Diluted EPS", "eps_diluted"))
_YF_BS_MAP = (("Total Assets", "total_assets"),
              ("Total Liabilities Net Minority Interest", "total_liabilities"),
              ("Stockholders Equity", "total_equity"))
_YF_CF_MAP = (("Operating Cash Flow", "operating_cash_flow"),
              ("Capital Expenditure", "capex"))
_YF_STMT_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_YF_STMT_TTL = 12 * 3600


def parse_yf_statements(ticker: str, income: Any, balance: Any, cashflow: Any,
                        limit: int = 8, fetched_at: Optional[int] = None
                        ) -> List[Dict[str, Any]]:
    """yfinance 季报三表 → 本项目 equity_fundamentals 行格式。

    为什么需要第三条腿：FMP 的 statement 端点对部分票直接 402、AV 免费档 25 次/日
    且常被日历/资产负债吃掉，结果 16 只基线里 6 只根本没有三表。yfinance 不占配额，
    字段名不同但能对上"规模/盈利/质量"需要的 8 个字段。

    口径注意：balance 通常只有 5 期、income 有 7 期 —— 允许"部分期数只有利润表"，
    缺的字段留 None，别用上一期硬填（那会造出未公布过的时间点）。
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for frame, mapping in ((income, _YF_INC_MAP), (balance, _YF_BS_MAP), (cashflow, _YF_CF_MAP)):
        if frame is None or getattr(frame, "empty", True):
            continue
        idx = set(getattr(frame, "index", []))
        cols = list(getattr(frame, "columns", []))
        for item, field in mapping:
            if item not in idx:
                continue
            series = frame.loc[item]
            for col in cols:
                try:
                    raw = series[col]
                except Exception:
                    continue
                period = str(col)[:10]
                if len(period) != 10 or period[4] != "-":
                    continue
                try:
                    val = float(raw)
                    if val != val:          # NaN
                        val = None
                except (TypeError, ValueError):
                    val = None
                rec = rows.setdefault(period, {
                    "ticker": str(ticker).upper(), "period_end": period,
                    "fiscal_year": period[:4], "period": _quarter_label(period),
                    "revenue": None, "gross_profit": None, "operating_income": None,
                    "net_income": None, "eps_diluted": None, "total_assets": None,
                    "total_liabilities": None, "total_equity": None,
                    "operating_cash_flow": None, "capex": None,
                    "source": "yfinance", "payload": {"src": "yfinance"},
                })
                rec[field] = val
    out = sorted(rows.values(), key=lambda r: str(r["period_end"]), reverse=True)[:limit]
    ts = int(fetched_at or time.time())
    for rec in out:
        rec["fetched_at"] = ts
    return out


def _quarter_label(period_end: str) -> str:
    """2026-09-30 → Q3（自然季度近似；只用于展示/主键去重，不参与计算）。"""
    try:
        month = int(period_end[5:7])
    except (ValueError, TypeError):
        return ""
    return f"Q{min(4, (month - 1) // 3 + 1)}"


def fetch_yf_statements(ticker: str, limit: int = 8) -> List[Dict[str, Any]]:
    """单票季报三表（12h 缓存）。失败返回空，绝不抛。"""
    key = str(ticker).upper()
    cached = _YF_STMT_CACHE.get(key)
    if cached and time.time() - cached[0] < _YF_STMT_TTL:
        return cached[1]
    try:
        import yfinance as _yf
        t = _yf.Ticker(key)
        rows = parse_yf_statements(key, t.quarterly_income_stmt,
                                   t.quarterly_balance_sheet, t.quarterly_cashflow,
                                   limit=limit)
    except Exception as exc:
        print(f"  [三表] {key} yfinance 不可用: {type(exc).__name__}: {str(exc)[:60]}", flush=True)
        return []
    if not rows:
        print(f"  [三表] {key} yfinance 空返回（可能无季报或限流）", flush=True)
    _YF_STMT_CACHE[key] = (time.time(), rows)
    return rows


_YF_CAL_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_YF_CAL_TTL = 12 * 3600


def parse_yf_calendar(cal: Any, ticker: str, days_back: int = 7, days_fwd: int = 90,
                      today: Optional[str] = None) -> List[Dict[str, Any]]:
    """解析 yfinance 的 `Ticker.calendar`（dict，Earnings Date 可能是列表）。

    v448-fix 的现实理由：AV 的 CALENDAR_CSV 免费档已返回"权限提示"，FMP 又漏票，
    而 NVDA 明天就出财报。yfinance 不占配额、字段虽糙但日期是真的，作为第三路兜底。
    """
    if not isinstance(cal, dict):
        return []
    dates = cal.get("Earnings Date") or []
    if not isinstance(dates, (list, tuple)):
        dates = [dates]
    hi, lo = cal.get("Earnings High"), cal.get("Earnings Low")
    est = None
    if isinstance(hi, (int, float)) and isinstance(lo, (int, float)):
        est = round((float(hi) + float(lo)) / 2.0, 4)
    base = today or time.strftime("%Y-%m-%d")
    lo_day = time.strftime("%Y-%m-%d", time.localtime(
        time.mktime(time.strptime(base, "%Y-%m-%d")) - days_back * 86400))
    hi_day = time.strftime("%Y-%m-%d", time.localtime(
        time.mktime(time.strptime(base, "%Y-%m-%d")) + days_fwd * 86400))
    out: List[Dict[str, Any]] = []
    for d in dates:
        day = str(d)[:10]
        if len(day) != 10 or day[4] != "-":
            continue
        if not (lo_day <= day <= hi_day):
            continue
        out.append({"ticker": str(ticker).upper(), "event_date": day,
                    "eps_estimated": est, "eps_actual": None,
                    "revenue_estimated": None, "revenue_actual": None,
                    "last_updated": base, "source": "yfinance"})
    return out


def fetch_yf_calendar(ticker: str, days_back: int = 7, days_fwd: int = 90) -> List[Dict[str, Any]]:
    """单票下次财报日（12h 缓存）。失败返回空，绝不抛。"""
    key = str(ticker).upper()
    cached = _YF_CAL_CACHE.get(key)
    if cached and time.time() - cached[0] < _YF_CAL_TTL:
        return cached[1]
    try:
        import yfinance as _yf
        cal = _yf.Ticker(key).calendar
    except Exception as exc:
        print(f"  [日历] {key} yfinance 不可用: {type(exc).__name__}: {str(exc)[:60]}", flush=True)
        return []
    events = parse_yf_calendar(cal, key, days_back, days_fwd)
    _YF_CAL_CACHE[key] = (time.time(), events)
    return events


def _calendar_uncovered(events: List[Dict[str, Any]]) -> set:
    """关注清单里"有事件却不在日历中"的票 —— 只算仍在名单内的，避免历史票干扰。"""
    try:
        import config as _cfg
        watch = {str(t).upper() for t in (_cfg.EQUITY_TICKERS or [])}
    except Exception:
        return set()
    got = {str(e.get("ticker") or "").upper() for e in events}
    return {t for t in watch if t and t not in got}


def calendar_events(days_back: int = 7, days_fwd: int = 90) -> Dict[str, Any]:
    """财报日历统一入口：FMP → AV CSV → yfinance，三路都空才如实报错。

    v448 实测三源各自的毛病：
      · FMP `/stable/earnings-calendar` 本机 **SSL EOF 间歇**（同一分钟重试就成功），
        成功时也只给 69 条、漏掉 NVDA/MU —— 对"主力票明天出财报"这种场景等于没覆盖；
      · AV `CALENDAR_CSV` 免费档已返回权限提示（非 CSV），不能指望；
      · yfinance `Ticker.calendar` 不占配额、日期是真的（NVDA 2026-11-18 / MU 2026-12-24 实测）。
    所以策略是：任何一路拿到事件就留着，再按关注清单逐票补缺，最后如实回报来源与缺口。
    """
    errors: List[str] = []
    events: List[Dict[str, Any]] = []
    sources: List[str] = []

    raw = fetch_calendar(days_back, days_fwd)
    if isinstance(raw, dict) and raw.get("_error"):
        errors.append(f"fmp:{raw['_error']}")
    elif raw:
        events = parse_earnings(raw)
        if events:
            sources.append("fmp")
        else:
            errors.append("fmp:日历为空")
    else:
        errors.append("fmp:空响应")

    # AV CSV：一次调用尽力补；失败只记错
    try:
        text = fetch_av_calendar()
        if isinstance(text, str) and "symbol" in text.lower()[:200]:
            extra = parse_av_calendar(text, days_back, days_fwd)
            if extra:
                events = _merge_events(events, extra)
                sources.append("alphavantage")
        else:
            errors.append("av:非 CSV 返回（多为配额/权限提示）")
    except Exception as exc:
        errors.append(f"av:{type(exc).__name__}: {str(exc)[:60]}")

    # 仍缺的票逐只问 yfinance（每票 1 次 HTTP，12h 缓存，最多 8 只）
    uncovered = _calendar_uncovered(events)
    yf_events: List[Dict[str, Any]] = []
    for tk in sorted(uncovered)[:8]:
        yf_events.extend(fetch_yf_calendar(tk, days_back, days_fwd))
    if yf_events:
        events = _merge_events(events, yf_events)
        sources.append("yfinance")

    missing = sorted(_calendar_uncovered(events))[:8]
    return {
        "events": events,
        "source": "+".join(sources) if sources else "none",
        "filled": sorted(uncovered)[:8] if (uncovered and sources) else None,
        "missing": missing,
        "errors": errors,
    }


def fetch_earnings_history(ticker: str) -> List[Dict[str, Any]]:
    """单只票的一致预期历史（AV EARNINGS，12h 缓存）。失败返回空列表并记错。"""
    body = _cached(f"av:earnings:{ticker}", _CAL_TTL, lambda: _av("EARNINGS", ticker))
    if _av_error(body):
        return []
    return parse_av_earnings(body)


def _merge_events(primary: List[Dict[str, Any]], secondary: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (ticker, date) 合并两路财报事件：主源保留，副源只补空字段。"""
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for ev in primary:
        index[(ev["ticker"], ev["event_date"])] = dict(ev)
    for ev in secondary:
        key = (ev["ticker"], ev["event_date"])
        if key not in index:
            index[key] = dict(ev)
            continue
        for field in ("eps_estimated", "eps_actual", "revenue_estimated", "revenue_actual"):
            if index[key].get(field) is None and ev.get(field) is not None:
                index[key][field] = ev[field]
    return sorted(index.values(), key=lambda e: (e["ticker"], e["event_date"]))


def _freshness(conn: sqlite3.Connection, tickers: Tuple[str, ...]
               ) -> Dict[str, Tuple[int, str]]:
    """每只票的 (最近抓取时间, 最新财报期)。只读库，失败返回空（当全都过期处理）。"""
    out: Dict[str, Tuple[int, str]] = {}
    try:
        for tk, ts, period in conn.execute(
            """SELECT ticker, MAX(fetched_at), MAX(period_end)
               FROM equity_fundamentals GROUP BY ticker"""
        ):
            tk = str(tk or "").upper()
            if tk and (not tickers or tk in tickers):
                out[tk] = (int(ts or 0), str(period or ""))
    except Exception:
        return {}
    return out


def universe(conn: Optional[sqlite3.Connection] = None, days: int = 30) -> Tuple[str, ...]:
    """要抓的票 = 基线名单 ∪ 近 N 天**真的被决策过**的美股（按决策次数降序）。

    为什么不再只靠 config 里那张写死的名单：一周的 ai_decisions 里出现过 AMZN/META，
    但名单里没有它们 —— 结果是「决策时问得出基本面，库里答不上来」。
    反过来也不能无脑扩：免费配额一只票 4 次调用，全市场刷不现实，所以：
      · 基线名单（config.EQUITY_TICKERS）保证核心票常在
      · 决策过的票按热度补进来（最多补 20 只）
      · 占位标签 / 非公司标的 / A股港股一律不进（它们各自有归属）
    """
    import asset_kinds as ak
    base: List[str] = [str(t).upper() for t in (getattr(config, "EQUITY_TICKERS", ()) or ())]
    picked: List[str] = []
    owned = conn is None
    try:
        if owned:
            import db as _db
            conn = _db.get_connection()
        cutoff = int(time.time()) - max(1, days) * 86400
        rows = conn.execute(
            """SELECT target_asset, COUNT(*) AS n
               FROM ai_decisions WHERE ts >= ?
               GROUP BY target_asset ORDER BY n DESC LIMIT 200""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            asset = str((row[0] if not hasattr(row, "keys") else row["target_asset"]) or "").upper()
            if ak.classify(asset)[0] != "us":
                continue
            if asset not in base and asset not in picked:
                picked.append(asset)
    except Exception:
        picked = []
    finally:
        if owned and conn is not None:
            conn.close()
    extra = picked[:20]
    # 去重保序：基线优先，再把决策热度高的补在后面
    out: List[str] = []
    for tk in base + extra:
        if tk and tk not in out:
            out.append(tk)
    return tuple(out)


def fetch_pack(tickers: Optional[Tuple[str, ...]] = None,
               skip_fresh: bool = True,
               max_refresh: Optional[int] = None,
               conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """一次性拉「N 只票的三表 + 全市场财报日历 + 一致预期历史」。

    v406 起多了两条节流（免费配额是这里最硬的约束）：
      · `skip_fresh`：`EQUITY_STALE_HOURS`（默认 168h）内抓过的票**不重抓**，
        直接用库里的行 —— 三表是季报，一天重抓一遍纯属烧配额；
      · `max_refresh`：一轮最多刷新 `EQUITY_MAX_PER_RUN`（默认 6）只，按"最旧优先"
        轮转，靠多轮覆盖，而不是一轮把 25 次/日打空。
    被跳过但仍然需要的票，从库里读回来参与 summary —— 所以"跳过"不等于"少一层"。
    """
    if tickers is None:
        tickers = universe(conn) or ("NVDA",)
    tickers = tuple(str(t).upper() for t in tickers if str(t).strip())
    stale_hours = max(1, int(getattr(config, "EQUITY_STALE_HOURS", 168) or 168))
    limit = max_refresh if max_refresh is not None else \
        max(1, int(getattr(config, "EQUITY_MAX_PER_RUN", 3) or 3))
    # 配额自适应：每只票最多 4 次 AV 调用（三表 3 + EARNINGS 1），剩多少配额就只刷几只。
    # 不做这层，配额定死 3 只但当天只剩 5 次时，第 2 只就会一路撞 429/空返回，
    # 日志里看着"跑了 3 只"，实际第 2、3 只是白跑（配额照样被吃掉）。
    left = budget_state()["remaining"]
    if left and limit > 1:
        by_budget = max(1, left // 4)
        if by_budget < limit:
            limit = by_budget

    stored = db_pack(conn, tickers=tickers)
    owner_conn = conn
    owns = False
    if owner_conn is None:
        import db as _db
        owner_conn = _db.get_connection()
        owns = True
    try:
        fresh_map = _freshness(owner_conn, tickers)
    finally:
        if owns and owner_conn is not None:
            owner_conn.close()

    now = time.time()
    candidates: List[Tuple[float, str]] = []
    reused: List[str] = []
    for tk in tickers:
        fetched_at, _period = fresh_map.get(tk, (0, ""))
        has_rows = bool((stored.get("statements") or {}).get(tk))
        if skip_fresh and has_rows and fetched_at and (now - fetched_at) < stale_hours * 3600:
            reused.append(tk)
        else:
            candidates.append((float(fetched_at), tk))     # 最旧优先：0 = 从没抓过
    candidates.sort()
    refresh = [tk for _, tk in candidates[:limit]]
    deferred = [tk for _, tk in candidates[limit:]]

    rows_by_ticker: Dict[str, List[Dict[str, Any]]] = {
        tk: list(v) for tk, v in (stored.get("statements") or {}).items() if tk in reused
    }
    errors: List[str] = []
    per_ticker_earnings: Dict[str, List[Dict[str, Any]]] = {}
    for tk in refresh:
        piece = fetch_ticker(tk)
        stmt = parse_statements(piece.get("income"), piece.get("balance"), piece.get("cashflow"))
        # v452：FMP/AV 都失手（402 / 配额耗尽）→ 退 yfinance。只在缺行或整列为空时补，
        # 不做"缺哪期拿上一期填"的操作 —— 那是造数。
        if not stmt or all(r.get("total_equity") is None for r in stmt):
            yf_rows = fetch_yf_statements(tk)
            if yf_rows:
                stmt = _merge_statements(stmt or [], yf_rows)
                errors.append(f"{tk}:yfinance 补 {len(yf_rows)} 期三表")
        if stmt:
            rows_by_ticker[tk] = stmt
        if piece.get("errors"):
            errors.extend([f"{tk}:{e}" for e in piece["errors"]])
        per_ticker_earnings[tk] = fetch_earnings_history(tk)

    # 财报日历是全市场一次拉：只在真有票要刷新时才请求，否则纯烧配额
    events: List[Dict[str, Any]] = []
    calendar_source: Optional[str] = None
    if refresh:
        # v448：FMP 日历本机 SSL 常挂 → 统一入口自动退 AV CSV
        calendar = calendar_events(7, 90)
        events = calendar.get("events") or []
        if calendar.get("errors"):
            errors.extend([f"calendar:{e}" for e in calendar["errors"]])
        calendar_source = calendar.get("source")
    av_events = [e for evs in per_ticker_earnings.values() for e in evs]
    # 被跳过（复用库）的票，一致预期从库里带回来，别把它打成"没有预期差"
    av_events.extend([e for e in (stored.get("earnings") or [])
                      if str(e.get("ticker") or "").upper() in set(reused)])
    merged = _merge_events(events, av_events)
    ok_tickers = len(rows_by_ticker)
    status = "ok" if ok_tickers == len(tickers) else ("partial" if ok_tickers else "unavailable")
    return {
        "status": status,
        "asof": int(time.time()),
        "tickers": tickers,
        "refreshed": tuple(refresh),
        "reused": tuple(reused),
        "deferred": tuple(deferred),
        "statements": rows_by_ticker,
        "summary": {tk: summarize(rows, eps_history=[e for e in merged
                                                     if e["ticker"] == tk and e.get("eps_actual") is not None])
                    for tk, rows in rows_by_ticker.items()},
        "earnings": merged,
        "errors": errors[:8],
        "calendar_source": calendar_source,
        "av_budget": budget_state(),
    }



# ── 落库 ────────────────────────────────────────────────────────────────────
def ensure_tables(conn: sqlite3.Connection) -> None:
    """只建本模块的两张表（幂等）。

    不走 db.migrate()：那里还带 published_at 回填等一堆动作，而常驻引擎在跑时
    整个 migrate 很容易撞 "database is locked"。抓取脚本只需要自己的两张表。
    """
    import db as _db
    for sql in (getattr(_db, "_CREATE_EQUITY_FUNDAMENTALS", ""),
                getattr(_db, "_CREATE_IDX_EQ_FUND", ""),
                getattr(_db, "_CREATE_EQUITY_EARNINGS", ""),
                getattr(_db, "_CREATE_IDX_EQ_EARNINGS", "")):
        if sql:
            conn.execute(sql)
    conn.commit()


def persist(conn: sqlite3.Connection, pack: Dict[str, Any]) -> int:
    """三表 + 财报日历 upsert。返回写入行数。表不存在时静默返回 0（老库未迁移）。"""
    if not isinstance(pack, dict):
        return 0
    written = 0
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('equity_fundamentals','equity_earnings_events')").fetchall()}
        if "equity_fundamentals" in tables:
            for ticker, rows in (pack.get("statements") or {}).items():
                for row in rows:
                    conn.execute(
                        """INSERT INTO equity_fundamentals
                           (ticker, period_end, fiscal_year, period, revenue, gross_profit,
                            operating_income, net_income, eps_diluted, total_assets,
                            total_liabilities, total_equity, operating_cash_flow, capex,
                            source, fetched_at, payload)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(ticker, period_end, source) DO UPDATE SET
                             revenue=excluded.revenue,
                             gross_profit=excluded.gross_profit,
                             operating_income=excluded.operating_income,
                             net_income=excluded.net_income,
                             eps_diluted=excluded.eps_diluted,
                             total_assets=excluded.total_assets,
                             total_liabilities=excluded.total_liabilities,
                             total_equity=excluded.total_equity,
                             operating_cash_flow=excluded.operating_cash_flow,
                             capex=excluded.capex,
                             fetched_at=excluded.fetched_at,
                             payload=excluded.payload""",
                        (ticker, row["period_end"], row["fiscal_year"], row["period"],
                         row["revenue"], row["gross_profit"], row["operating_income"],
                         row["net_income"], row["eps_diluted"], row["total_assets"],
                         row["total_liabilities"], row["total_equity"],
                         row["operating_cash_flow"], row["capex"],
                         str(row.get("source") or "fmp"), int(pack.get("asof") or time.time()),
                         json.dumps(row.get("payload") or {}, ensure_ascii=False)),
                    )
                    written += 1
        if "equity_earnings_events" in tables:
            for ev in (pack.get("earnings") or []):
                conn.execute(
                    """INSERT INTO equity_earnings_events
                       (ticker, event_date, eps_estimated, eps_actual,
                        revenue_estimated, revenue_actual, source, fetched_at, last_updated)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(ticker, event_date) DO UPDATE SET
                         eps_estimated=COALESCE(excluded.eps_estimated, equity_earnings_events.eps_estimated),
                         eps_actual=COALESCE(excluded.eps_actual, equity_earnings_events.eps_actual),
                         revenue_estimated=COALESCE(excluded.revenue_estimated, equity_earnings_events.revenue_estimated),
                         revenue_actual=COALESCE(excluded.revenue_actual, equity_earnings_events.revenue_actual),
                         fetched_at=excluded.fetched_at,
                         last_updated=excluded.last_updated""",
                    (ev["ticker"], ev["event_date"], ev["eps_estimated"], ev["eps_actual"],
                     ev["revenue_estimated"], ev["revenue_actual"],
                     str(ev.get("source") or "fmp"), int(pack.get("asof") or time.time()),
                     str(ev.get("last_updated") or "")),
                )
                written += 1
        conn.commit()
    except Exception:
        return written
    return written



def _summarize_earnings(ticker: str, eps_history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """只有财报事件（没有三表）时的精简 summary。

    为什么需要：MARA 这类票三表抓不到（AV 配额/源缺），但 `equity_earnings_events` 里
    有 58 期真实公布过的 EPS —— 足够算 TTM EPS 同比和一致预期差，不该整体报 `no_data`。
    缺三表就只给"盈利面"，不给估值/毛利率/现金流，标记 `partial=True` 让人一眼看出
    这不是完整财报。
    """
    got = sorted((e for e in eps_history if e.get("eps_actual") is not None),
                 key=lambda e: str(e.get("event_date") or ""))
    eps = [float(e["eps_actual"]) for e in got]
    out: Dict[str, Any] = {"ttm": {}, "yoy": {}, "n_quarters": len(eps),
                           "eps_source": "earnings_only", "partial": True,
                           "latest": {"ticker": ticker,
                                      "period_end": str(got[-1].get("event_date") or "") if got else ""}}
    if len(eps) >= 4:
        cur = sum(eps[-4:])
        out["ttm"]["eps_diluted"] = round(cur, 4)
        if len(eps) >= 8:
            prev = sum(eps[-8:-4])
            if prev > 0:
                out["yoy"]["eps_diluted"] = round((cur / prev - 1) * 100.0, 2)
            else:
                out["eps_source"] = "earnings_only_neg_base"
    return out

_STMT_FIELDS = ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
                "total_assets", "total_liabilities", "total_equity",
                "operating_cash_flow", "capex")
_SOURCE_RANK = {"fmp": 0, "alphavantage": 1, "yfinance": 2}


_QUARTER_ALIGN_DAYS = 21   # 同一财季跨源的期末日最多差 3 周（AV 月末 / SEC 申报前几日）


def _day_key(value: Any) -> Optional[int]:
    """period_end → 天序号；解析失败返回 None（不猜日期）。"""
    text = str(value or "")[:10]
    try:
        return int(time.mktime(time.strptime(text, "%Y-%m-%d")) // 86400)
    except (ValueError, TypeError, OverflowError):
        return None


def _fiscal_key(rec: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """有 fiscal_year + period 时（yfinance）用它做同季判定，避免跨年日期漂移。"""
    fy = str(rec.get("fiscal_year") or "").strip()
    period = str(rec.get("period") or "").strip().upper()
    if fy and period:
        return fy, period
    return None


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把**同一财季**的多来源读数对齐成一季一条，并做字段级合并。

    为什么不能只按 `period_end` 完全相等去重（v488）：
      AV 的期末日是月末（AAPL 2026-09-30），SEC 的期末日是申报口径（2026-09-26），
      两者相差 4 天 → 旧实现当成两个季度。结果 `rows[:4]` 只覆盖两个真实季度，
      TTM 的营收/毛利/现金流全是 None（实测 AAPL/NVDA/MU/HUT/MARA 五只直接断档），
      毛利率、TTM 营收同比、生意质量这些读数一个都算不出来。

    口径：
      · 同 fiscal_year+period 优先合并；否则期末日相差 ≤21 天视为同一财季；
      · 每季选"非空字段最多"的一条为主记录（平手 fmp > alphavantage > yfinance），
        其余记录的**空字段**回填到主记录 —— 只补空，不覆盖已有值，不跨季借数；
      · 字段仍为空就保持 None，不插值、不拿相邻季度顶替。
    """
    buckets: List[Dict[str, Any]] = []
    for rec in sorted(rows, key=lambda r: str(r.get("period_end") or ""), reverse=True):
        day = _day_key(rec.get("period_end"))
        if day is None:
            continue
        fkey = _fiscal_key(rec)
        target = None
        for bucket in buckets:
            if fkey and bucket.get("fiscal_key") and fkey == bucket["fiscal_key"]:
                target = bucket
                break
            if abs(int(bucket["day"]) - day) <= _QUARTER_ALIGN_DAYS:
                target = bucket
                break
        if target is None:
            buckets.append({"day": day, "fiscal_key": fkey, "rows": [rec]})
        else:
            target["rows"].append(rec)
            target["fiscal_key"] = target.get("fiscal_key") or fkey

    out: List[Dict[str, Any]] = []
    for bucket in buckets:
        members = sorted(
            bucket["rows"],
            key=lambda r: (
                -sum(1 for f in _STMT_FIELDS if r.get(f) is not None),
                _SOURCE_RANK.get(str(r.get("source") or ""), 9),
            ),
        )
        base = dict(members[0])
        for rec in members[1:]:
            for field in _STMT_FIELDS:
                if base.get(field) is None and rec.get(field) is not None:
                    base[field] = rec[field]
            for field in ("fiscal_year", "period"):
                if not base.get(field) and rec.get(field):
                    base[field] = rec[field]
        out.append(base)
    return sorted(out, key=lambda r: str(r.get("period_end")), reverse=True)


def db_pack(conn: Optional[sqlite3.Connection] = None,
            tickers: Optional[Tuple[str, ...]] = None,
            days: int = 800) -> Dict[str, Any]:
    """从库里还原 pack（**零网络**）。宏观层每轮都用这个，不阻塞主循环。

    数据由 `scripts/cron_equity_fundamentals.py` 或本模块 `fetch_pack()+persist()`
    写入；这里读不到就是 unavailable，绝不在决策链路上现拉网络。
    """
    owned = conn is None
    if owned:
        import db as _db
        conn = _db.get_connection()
    out: Dict[str, Any] = {"status": "unavailable", "asof": 0, "tickers": (),
                           "statements": {}, "summary": {}, "earnings": [], "errors": []}
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('equity_fundamentals','equity_earnings_events')").fetchall()}
        if "equity_fundamentals" not in tables:
            return out
        rows = conn.execute(
            """SELECT ticker, period_end, fiscal_year, period, revenue, gross_profit,
                      operating_income, net_income, eps_diluted, total_assets,
                      total_liabilities, total_equity, operating_cash_flow, capex,
                      source, fetched_at
               FROM equity_fundamentals ORDER BY period_end DESC""").fetchall()
        by_ticker: Dict[str, List[Dict[str, Any]]] = {}
        last_fetch = 0
        columns = ("ticker", "period_end", "fiscal_year", "period", "revenue", "gross_profit",
                   "operating_income", "net_income", "eps_diluted", "total_assets",
                   "total_liabilities", "total_equity", "operating_cash_flow", "capex",
                   "source", "fetched_at")
        for row in rows:
            rec = dict(zip(columns, tuple(row)) if not hasattr(row, "keys") else
                       {k: row[k] for k in columns})
            tk = str(rec.get("ticker") or "").upper()
            if tickers and tk not in tickers:
                continue
            by_ticker.setdefault(tk, []).append(rec)
            last_fetch = max(last_fetch, int(rec.get("fetched_at") or 0))
        by_ticker = {tk: _dedupe_rows(v) for tk, v in by_ticker.items()}   # v452：跨源同季去重
        events: List[Dict[str, Any]] = []
        if "equity_earnings_events" in tables:
            cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
            for row in conn.execute(
                """SELECT ticker, event_date, eps_estimated, eps_actual,
                          revenue_estimated, revenue_actual, source, fetched_at
                   FROM equity_earnings_events WHERE event_date >= ?""", (cutoff,)).fetchall():
                ev_columns = ("ticker", "event_date", "eps_estimated", "eps_actual",
                              "revenue_estimated", "revenue_actual", "source", "fetched_at")
                rec = dict(zip(ev_columns, tuple(row)) if not hasattr(row, "keys") else
                           {k: row[k] for k in ev_columns})
                tk = str(rec.get("ticker") or "").upper()
                if tickers and tk not in tickers:
                    continue
                events.append({
                    "ticker": tk, "event_date": str(rec.get("event_date") or ""),
                    "eps_estimated": rec.get("eps_estimated"), "eps_actual": rec.get("eps_actual"),
                    "revenue_estimated": rec.get("revenue_estimated"),
                    "revenue_actual": rec.get("revenue_actual"),
                    "source": rec.get("source") or "", "last_updated": "",
                })
        out["tickers"] = tuple(sorted(by_ticker))
        out["statements"] = by_ticker
        out["summary"] = {
            tk: summarize(rows_, eps_history=[e for e in events
                                              if e["ticker"] == tk and e.get("eps_actual") is not None])
            for tk, rows_ in by_ticker.items()
        }
        # 三表缺、但财报事件够 8 期的票，补一份「只有盈利面」的 summary：
        # 不然 MARA 这种一直被决策的票会被整体判成 no_data，连 EPS 同比都读不到。
        for tk in (tickers or ()):
            if tk in out["summary"]:
                continue
            ev = [e for e in events if e["ticker"] == tk and e.get("eps_actual") is not None]
            if len(ev) >= 8:
                out["summary"][tk] = _summarize_earnings(tk, ev)
        out["earnings"] = sorted(events, key=lambda e: (e["ticker"], e["event_date"]))
        out["asof"] = last_fetch or int(time.time())
        ok = len(by_ticker)
        out["status"] = "ok" if ok else "unavailable"
    except Exception as exc:
        out["errors"] = [f"{type(exc).__name__}: {str(exc)[:80]}"]
    finally:
        if owned:
            conn.close()
    return out


def known_tickers(conn: Optional[sqlite3.Connection] = None) -> List[str]:
    """已经真的存过财报的票（给因子层限定作用域，不猜）。"""
    owned = conn is None
    if owned:
        import db as _db
        conn = _db.get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM equity_fundamentals").fetchall()
        return sorted({str(r[0]).upper() for r in rows if r and r[0]})
    except Exception:
        return []
    finally:
        if owned:
            conn.close()


# ── 给宏观层/prompt 用的紧凑指标 ─────────────────────────────────────────────
def layer_items(pack: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把财报 pack 压成 macro_context 的一层。

    每只票只出 8 条（TTM 营收 / TTM EPS / 营收同比 / EPS同比 / 毛利率 / 经营现金流 /
    下次财报天数 / 上次预期差）—— prompt 里一行一票，不占版面。
    """
    rows: List[Dict[str, Any]] = []
    if not isinstance(pack, dict) or str(pack.get("status") or "unavailable") == "unavailable":
        note = ""
        if isinstance(pack, dict) and pack.get("errors"):
            note = str(pack["errors"][0])[:120]
        return [{
            "category": "fundamentals",
            "source": "fmp",
            "metric_key": "fund.unavailable",
            "value": None,
            "unit": "",
            "status": "unavailable",
            "payload": {"note": note or "源不可用"},
        }]
    for ticker in pack.get("tickers") or ():
        summary = (pack.get("summary") or {}).get(ticker) or {}
        ttm = summary.get("ttm") or {}
        yoy = summary.get("yoy") or {}
        latest = summary.get("latest") or {}
        period = str((latest or {}).get("period_end") or "")
        ev = earnings_metrics([e for e in (pack.get("earnings") or []) if e.get("ticker") == ticker],
                              now_ts=pack.get("asof"))
        extra = {"freshness": "quarterly", "period_end": period,
                 "scope": "财报", "label": ticker}

        def _m(key: str, value: Optional[float], unit: str,
               note_extra: Optional[Dict[str, Any]] = None) -> None:
            payload = dict(extra)
            if note_extra:
                payload.update(note_extra)
            rows.append({
                "category": "fundamentals",
                "source": "fmp",
                "metric_key": key,
                "value": value,
                "unit": unit,
                "status": "ok" if value is not None else "unavailable",
                "payload": payload,
            })

        rev_ttm = ttm.get("revenue")
        _m("fund.revenue_ttm", (rev_ttm / 1e9) if rev_ttm is not None else None, "十亿美元")
        _m("fund.eps_ttm", ttm.get("eps_diluted"), "美元")
        _m("fund.revenue_yoy", yoy.get("revenue"), "%")
        _m("fund.eps_yoy", yoy.get("eps_diluted"), "%")
        _m("fund.gross_margin", summary.get("gross_margin"), "%")
        _m("fund.ocf_ttm", (ttm.get("operating_cash_flow") / 1e9)
           if ttm.get("operating_cash_flow") is not None else None, "十亿美元")
        _m("fund.next_earnings_days", ev.get("next_days"), "天",
           {"next_date": ev.get("next_date"), "eps_estimated": ev.get("eps_estimated")})
        _m("fund.last_eps_surprise", ev.get("last_surprise_pct"), "%",
           {"report_date": ev.get("last_report_date"),
            "eps_actual": ev.get("last_eps_actual"),
            "eps_estimated": ev.get("last_eps_estimated")})
    if not rows:
        rows.append({
            "category": "fundamentals",
            "source": "fmp",
            "metric_key": "fund.unavailable",
            "value": None,
            "unit": "",
            "status": "unavailable",
            "payload": {"note": "无已存财报的票"},
        })
    return rows


def pe_history(ticker: str, days: int = 365, conn=None) -> List[Tuple[int, float]]:
    """用「日线收盘 ÷ 当时已公布的 EPS TTM」倒推 PE 历史序列。

    为什么需要倒推：PE 分位原本只能靠本系统每轮落库的 eq.pe 快照累积，60 个样本要
    半个月以上；名单一扩，新加的票永远等不到分位，估值分量一直缺，分数就只剩盈利修正
    → 单边偏多。而「季报 EPS（带真实公布日）」和「日线收盘」都是现成的，拼起来就能
    还原过去一年的 PE 序列，分位当场可用，不再靠天数熬。

    防前视：EPS 只在「实际公布日（equity_earnings_events.event_date）」之后才进 TTM，
    绝不用尚未公布的季报去解释历史价格。三表里的 eps_diluted 大多为空（源不提供），
    所以 EPS 一律取财报事件表 —— 那里既有数值也有公布日。

    返回 [(ts, pe)] 升序；TTM 亏损（近四季 EPS 合计 ≤ 0）的日子直接跳过 —— 亏损公司
    PE 为负，放进分位会变成「便宜=看多」的假信号。
    """
    own = False
    try:
        if conn is None:
            import db as _db
            conn = _db.get_connection()
            own = True
        ticker = str(ticker).upper()
        quarters = conn.execute(
            """SELECT event_date, MAX(eps_actual) FROM equity_earnings_events
               WHERE ticker=? AND eps_actual IS NOT NULL
               GROUP BY event_date ORDER BY event_date""",
            (ticker,),
        ).fetchall()
        start = int(time.time()) - (int(days) + 120) * 86400
        bars = conn.execute(
            """SELECT ts, close FROM ohlcv_daily
               WHERE asset=? AND ts >= ? AND close > 0 ORDER BY ts""",
            (ticker, start),
        ).fetchall()
    except Exception:
        return []
    finally:
        if own and conn is not None:
            conn.close()

    import datetime as _dt
    known_eps: List[Tuple[int, float]] = []
    for event_date, eps in quarters:
        try:
            d = _dt.datetime.strptime(str(event_date)[:10], "%Y-%m-%d")
            known_eps.append((int(d.replace(tzinfo=_dt.timezone.utc).timestamp()), float(eps)))
        except Exception:
            continue
    known_eps.sort()

    out: List[Tuple[int, float]] = []
    for ts, close in bars:
        known = [e for (eff, e) in known_eps if eff <= int(ts)]
        if len(known) < 4:            # 季报不够 4 个 → TTM 拼不出来
            continue
        ttm = sum(known[-4:])
        if ttm <= 0:
            continue
        out.append((int(ts), float(close) / ttm))
    return out


def _equity_asof(rows: List[Tuple], day: str) -> Tuple[Optional[float], str]:
    """取「该日已公布」的最近一期净资产，并对缺失做**有标注的**反推。

    两种输入都吃，可乱序：
      · 4 元组 `(period_end, equity, assets, liabilities)` —— 季末即视为已知日；
      · 5 元组 `(known_date, period_end, equity, assets, liabilities)` —— 有申报日时用。

    **可用性按 known_date 判、挑哪一张按 period_end 排**（v456 修的真 bug）：
    SEC 的 companyconcept 会把**往年对比期**一并塞进后来的申报里 —— 实测 META 的
    2025-06-30 净资产出现在 2026-10-22 那份 10-Q 的 XBRL 里。于是这一行的"已知日"
    比 2026-09-30 那期更新；若按"已知日最新者胜"排序，**更旧**的资产负债表会被当成
    最新一期，净资产被低估、PB 被系统性抬高（实测 PB 偏差 META +39.9%、TSLA +22.0%、
    NVDA +16.4%）。排序键必须是期末，已知日只用来判断"这一天到底能不能用"。

    优先级：total_equity → (total_assets − total_liabilities) → 放弃。
    为什么不做 NI_ttm ÷ ROE 反推：美股三表里根本没有 ROE 字段，硬造一个
    "假设 ROE=15%" 的净资产等于凭空捏分母 —— 那是 fake_data.py 明令禁止的
    一类假精度。真的缺，就让它缺（PB 分位少一只票，好过给错的分位）。
    """
    best: Optional[Tuple[str, str, float, str]] = None   # (period, known, equity, basis)
    for rec in rows:
        if rec is None:
            continue
        if len(rec) >= 5:
            known = str(rec[0] or "")[:10]
            period = str(rec[1] or "")[:10]
            eqv, asset_v, liab_v = rec[2], rec[3], rec[4]
        else:
            known = str(rec[0] or "")[:10]
            period = known
            eqv, asset_v, liab_v = rec[1], rec[2], rec[3]
        if not period or (known and known > day):
            continue
        # 单行可行化：净资产为负/缺失时，尝试 Assets−Liabilities（有了就标 derived）
        if eqv is not None and float(eqv) > 0:
            cand, basis = float(eqv), "reported"
        elif asset_v is not None and liab_v is not None and float(asset_v) - float(liab_v) > 0:
            cand, basis = float(asset_v) - float(liab_v), "derived(assets-liabilities)"
        else:
            continue          # 这一期没有可用净资产 → 不参与竞选（别让空行挡住好行）
        key = (period, known)  # 先比期末（谁更新），同期再比已知日（后申报的重述优先）
        if best is None or key > (best[0], best[1]):
            best = (period, known, cand, basis)
    if best is None:
        # 区分"真没有"与"有行但都不可用"，方便体检脚本定位
        return None, "equity_missing"
    return best[2], best[3]


def _balance_rows(conn: sqlite3.Connection, ticker: str) -> List[Tuple[str, str, Any, Any, Any]]:
    """[(known_date, period_end, equity, assets, liabilities)]。

    known_date 是**这个数从哪天起算已知**：payload 里有 SEC 的 `filed`（申报日）就用它，
    否则退回 `period_end`。季末之后、申报日之前市场并不知道这个数 —— 用季末当已知日
    最多会提前 6 周（这是 v448 遗留的前视口子，v454 用 SEC 申报日补上）。

    ⚠️ 两个字段都必须留：`filed` 有时比 `period_end` 晚很多（SEC 会把往年对比期塞进后来
    的申报），只留 known_date 会让 `_equity_asof` 把旧报表当成最新一期（v456 修）。
    """
    try:
        rows = conn.execute(
            """SELECT period_end, total_equity, total_assets, total_liabilities, payload
               FROM equity_fundamentals WHERE ticker=? AND period_end IS NOT NULL""",
            (str(ticker).upper(),),
        ).fetchall()
    except Exception:
        return []
    out: List[Tuple[str, str, Any, Any, Any]] = []
    for period_end, equity, assets, liab, payload in rows:
        period = str(period_end or "")[:10]
        known = period
        try:
            filed = str((json.loads(payload or "{}") or {}).get("filed") or "")[:10]
            if filed and filed > known:
                known = filed
        except Exception:
            pass
        out.append((known, period, equity, assets, liab))
    return out


def pb_history(ticker: str, days: int = 365, conn=None) -> List[Tuple[int, float]]:
    """用「市值 ÷ 当时已公布的净资产」还原 PB 序列。

    两条分子来源，按日合并（同日取优先级高的）：
      1) `macro_snapshots.eq.mktcap` —— 估值源每轮落库的真实市值（十亿美元）；
      2) `equity_mktcap_daily` —— 股本 × 收盘 的推导市值（美元，见 equity_mktcap）。

    v454 为什么加第 2 条：v448 实测 10 只票里 MU/CLSK/HUT/SMCI 的 eq.mktcap 全 NULL
    （FMP 对这几只不返回 marketCap），PB 分位 0/10 的根因就在分子侧。推导口径
    用的是**真实历史股本**（yfinance get_shares_full）× **真实收盘**，不伪造任何数。

    决策链只读库（推导值由 cron 预热落 `equity_mktcap_daily`），不在热路径发网络请求。

    防前视：净资产按「已知日」（申报日优先）匹配，不用还没披露的季报解释历史市值。

    返回 [(ts, pb)] 升序；净资产缺失/为负的日子跳过（负净资产算 PB 没有意义）。
    """
    own = False
    try:
        if conn is None:
            import db as _db
            conn = _db.get_connection()
            own = True
        ticker = str(ticker).upper()
        start = int(time.time()) - int(days) * 86400
        caps = conn.execute(
            """SELECT ts, value FROM macro_snapshots
               WHERE metric_key='eq.mktcap' AND value IS NOT NULL AND value > 0
                 AND ts >= ? AND payload LIKE ? ORDER BY ts""",
            (start, f'%"label": "{ticker}"%'),
        ).fetchall()
        rows = _balance_rows(conn, ticker)
        derived = _derived_caps(conn, ticker, days)
    except Exception:
        return []
    finally:
        if own and conn is not None:
            conn.close()

    if not rows:
        return []

    # 单位：eq.mktcap 存的是「十亿美元」（见 feeds/equity_feed._ITEMS），
    # 净资产是美元，不换算会得到 1e-9 量级的 PB（实测 MSTR 直接显示 0.0）。
    mktcap_by_day: Dict[int, float] = {}
    values: List[float] = []
    for ts, value in caps:
        if value is None or float(value) <= 0:
            continue
        usd = float(value) * 1e9
        mktcap_by_day[int(ts) // 86400] = usd          # 同一天只留最后一条
        values.append(usd)
    # 快照市值恒定 = 源没在更新（MSTR 实测 46 天一个值不变）→ 只拦这一路，
    # 推导市值跟着收盘走，天然不会恒定，不该被这一条误伤。
    if values and max(values) - min(values) <= 1e-6 and not derived:
        return []
    for day_index, usd in derived.items():
        mktcap_by_day.setdefault(day_index, float(usd))
    if not mktcap_by_day:
        return []

    balance = [tuple(r) for r in rows]
    out: List[Tuple[int, float]] = []
    for day_index in sorted(mktcap_by_day):
        day = time.strftime("%Y-%m-%d", time.localtime(day_index * 86400 + 43200))
        equity, _basis = _equity_asof(balance, day)
        if not equity or equity <= 0:
            continue
        out.append((day_index * 86400, float(mktcap_by_day[day_index]) / equity))
    return out


def _derived_caps(conn: sqlite3.Connection, ticker: str, days: int) -> Dict[int, float]:
    """`equity_mktcap_daily` 的按日市值（美元）。表不存在/为空时返回 {}。"""
    try:
        import equity_mktcap as _em
        series = _em.db_series(conn, ticker, days)
    except Exception:
        return {}
    out: Dict[int, float] = {}
    for ts, usd in series:
        if usd and float(usd) > 0:
            out[int(ts) // 86400] = float(usd)
    return out


def pb_basis(ticker: str, conn=None) -> str:
    """PB 分母的口径说明（给 prompt 与复盘看，不参与打分）。"""
    own = False
    try:
        if conn is None:
            import db as _db
            conn = _db.get_connection()
            own = True
        rows = _balance_rows(conn, str(ticker).upper())
    except Exception:
        return "unknown"
    finally:
        if own and conn is not None:
            conn.close()
    if not rows:
        return "no_balance"
    # 按期末排（不是按已知日），与 `_equity_asof` 同一口径
    rows = sorted(rows, key=lambda r: str(r[1])[:10])
    rec = tuple(rows[-1])
    _equity, basis = _equity_asof([rec], str(rec[0] or "")[:10])
    return basis
