#!/usr/bin/env python3
"""美股基本面/估值层 — FMP(/stable/) 主力 + Alpha Vantage 兜底，内置限时缓存。

免费配额（务必 TTL 缓存，防高频烧份额）：
  - FMP 免费 250 次/日：关注标的 × 2 端点（quote + ratios-ttm）= 10 次/轮，
    TTL 默认 6h → 约 40 次/日，留足余量。
  - Alpha Vantage 免费 25 次/日：FMP 失败标的逐只用 AV OVERVIEW 补齐（≤5 次/轮），
    配合 6h 缓存最坏 ~20 次/日，留余量。

标的：config.EQUITY_TICKERS（默认加密高相关 NVDA/MSTR/COIN/MARA/RIOT）。
估值/基本面为慢变量，标注 freshness=delayed → 仅作背景/估值对照，勿当即时信号。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from feeds import source_status

HttpGet = Callable[[str, int], Any]
_TIMEOUT = 15
_RETRIES = 5   # v405: FMP 直连 SSL EOF 间歇率高（实测 3 次仍有 ~1/3 落空），5 次 + 递增退避
_AV_TTL = 43200  # 12h，Alpha Vantage 配额极紧

# 进程内缓存：key -> (expire_ts, rows)
_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
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


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _metric(
    metric_key: str,
    value: Optional[float],
    unit: str,
    label: str,
    extra: Optional[Dict[str, Any]] = None,
    source: str = "fmp",
) -> Dict[str, Any]:
    payload = dict(extra or {})
    payload["label"] = label
    return {
        "category": "equity",
        "source": source,
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def _set_cache(key: str, rows: List[Dict[str, Any]], ttl: float) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + ttl, list(rows))


# v405: 指标从 5 条扩到 12 条 —— 原来只有「市值/PE/PEG/净利率/负债权益」，
# 毛利率、ROE、PB、EPS、营收与 EPS 增速全缺，等于只有估值没有基本面。
# 每条的 unit 与语义固定，渲染层按同一份定义出图，不许各自加工。
_ITEMS: Tuple[Tuple[str, str], ...] = (
    ("eq.mktcap", "十亿美元"),
    ("eq.pe", "x"),
    ("eq.peg", "x"),
    ("eq.pb", "x"),
    ("eq.ps", "x"),
    ("eq.eps_ttm", "美元"),
    ("eq.gross_margin", "%"),
    ("eq.operating_margin", "%"),
    ("eq.net_margin", "%"),
    ("eq.roe", "%"),
    ("eq.debt_to_equity", "x"),
)

_PE_WINDOW_DAYS = 365
_PE_MIN_SAMPLES = 60   # AGENTS.md：有效样本 <60 不构成结论


def _unavailable_all() -> List[Dict[str, Any]]:
    tickers = config.EQUITY_TICKERS or ("NVDA",)
    rows: List[Dict[str, Any]] = []
    for ticker in tickers:
        rows.extend(_unavailable_one(ticker))
    return rows


def _unavailable_one(ticker: str) -> List[Dict[str, Any]]:
    return [_metric(key, None, unit, ticker, {"freshness": "delayed", "note": "源不可用"})
            for key, unit in _ITEMS]


def _pe_percentile(ticker: str, pe: Optional[float]) -> Dict[str, Any]:
    """PE 的历史分位（自算，不靠外部分位数据）。

    值来自本系统自己每轮落库的 `macro_snapshots.eq.pe`，窗口 1 年。样本 <60 条
    只标注不给结论（AGENTS.md 铁律）。任何异常都降级为「无分位」，不影响主链路。
    """
    if pe is None:
        return {}
    # 亏损公司的 PE 是负数：把它当成"低估值分位"会得出「便宜=看多」的假信号，
    # 这里直接不给分位（亏损股看 PE 本身就是错的指标）。
    if pe <= 0:
        return {"pe_invalid": "PE≤0（TTM 亏损，分位无意义）"}
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            cutoff = int(time.time()) - _PE_WINDOW_DAYS * 86400
            rows = conn.execute(
                """SELECT value FROM macro_snapshots
                   WHERE metric_key='eq.pe' AND value IS NOT NULL AND ts >= ?
                     AND payload LIKE ?""",
                (cutoff, f'%"label": "{ticker}"%'),
            ).fetchall()
        finally:
            conn.close()
        values = sorted(float(r[0]) for r in rows if r[0] is not None)
        if not values:
            return {}
        below = sum(1 for v in values if v <= pe)
        n = len(values)
        return {
            "pe_position": round(below / n * 100.0, 1),
            "pe_window_days": _PE_WINDOW_DAYS,
            "pe_samples": n,
            "pe_samples_ok": n >= _PE_MIN_SAMPLES,
            "pe_p25": round(values[int(n * 0.25)], 2),
            "pe_p75": round(values[min(n - 1, int(n * 0.75))], 2),
        }
    except Exception:
        return {}


def _from_values(values: Dict[str, Optional[float]], ticker: str, source: str,
                 extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把「内部字段名 → 数值」压成指标行。缺失的写 unavailable，不占位。"""
    extra = dict(extra)
    row = _metric("eq.pe", values.get("pe"), "x", ticker, extra, source=source)
    if source == "fmp":
        row["payload"].update(_pe_percentile(ticker, values.get("pe")))
    rows = [row]
    for key, unit in _ITEMS:
        if key == "eq.pe":
            continue
        rows.append(_metric(key, values.get(key.split(".", 1)[1]), unit, ticker, extra, source=source))
    return rows


def _fmp_one(get: HttpGet, ticker: str) -> List[Dict[str, Any]]:
    """拉单只股票的估值 + 基本面（quote 市值/EPS + ratios-ttm 12 项）。失败标 unavailable。"""
    quote: Any = None
    ratios: Any = None
    try:
        q = get(f"{config.FMP_BASE_URL}/stable/quote?symbol={ticker}&apikey={config.FMP_API_KEY}", _TIMEOUT)
        quote = q[0] if isinstance(q, list) and q else None
    except Exception:
        quote = None
    try:
        r = get(f"{config.FMP_BASE_URL}/stable/ratios-ttm?symbol={ticker}&apikey={config.FMP_API_KEY}", _TIMEOUT)
        ratios = r[0] if isinstance(r, list) and r else None
    except Exception:
        ratios = None

    qd = quote if isinstance(quote, dict) else {}
    rd = ratios if isinstance(ratios, dict) else {}

    def _pct(v: Any) -> Optional[float]:
        n = _num(v)
        return (n * 100.0) if n is not None else None

    mktcap = _num(qd.get("marketCap"))
    # ratios-ttm 实测字段（2026-11-16 抓 NVDA 全量 key 确认）：
    # 没有 returnOnEquityTTM，但给了 netIncomePerShareTTM 与 shareholdersEquityPerShareTTM，
    # ROE 用两者相除自算 —— 同源同口径，不跨源拼。
    nibps = _num(rd.get("netIncomePerShareTTM"))
    eqps = _num(rd.get("shareholdersEquityPerShareTTM"))
    roe = None
    if nibps is not None and eqps not in (None, 0):
        roe = round(nibps / eqps * 100.0, 2)
    values = {
        "mktcap": (mktcap / 1e9) if mktcap is not None else None,
        "pe": _num(rd.get("priceToEarningsRatioTTM")) or _num(rd.get("priceToEarningsDilutedRatioTTM")),
        "peg": _num(rd.get("priceToEarningsGrowthRatioTTM")),
        "pb": _num(rd.get("priceToBookRatioTTM")),
        "ps": _num(rd.get("priceToSalesRatioTTM")),
        "eps_ttm": nibps,
        "gross_margin": _pct(rd.get("grossProfitMarginTTM")),
        "operating_margin": _pct(rd.get("operatingProfitMarginTTM")),
        "net_margin": _pct(rd.get("netProfitMarginTTM")),
        "roe": roe,
        "debt_to_equity": _num(rd.get("debtToEquityRatioTTM")),
    }
    extra: Dict[str, Any] = {"freshness": "delayed", "scope": "基本面/估值"}
    return _from_values(values, ticker, "fmp", extra)


def _snapshot_starved(tickers: List[str],
                      counts: Optional[Dict[str, int]] = None) -> List[str]:
    """按「近 7 天 eq.pe 快照条数」升序返回 —— 样本最少的票先拿 AV 配额。

    为什么需要：估值分位要 60 个样本才算数，而配额只够每天补一只左右。若按配置顺序
    排队，名单末尾的票永远拿不到第 1 条样本，分位就永远不来（名单从 5 只扩到 16 只时
    就是这么被饿死的）。这里只读库算计数，异常一律退回原顺序，不影响主链路。
    """
    if counts is not None:      # 单测直接注入样本数，不依赖库里的真实数据
        base = {t: i for i, t in enumerate(tickers)}
        return sorted(tickers, key=lambda t: (counts.get(str(t).upper(), 0), base[t]))
    counts = {}
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            cutoff = int(time.time()) - 7 * 86400
            for label, n in conn.execute(
                """SELECT json_extract(payload, '$.label') AS lbl, COUNT(*)
                   FROM macro_snapshots
                   WHERE metric_key='eq.pe' AND ts >= ? GROUP BY lbl""", (cutoff,)
            ):
                counts[str(label or "").upper()] = int(n)
        finally:
            conn.close()
    except Exception:
        counts = {}
    base = {t: i for i, t in enumerate(tickers)}
    return sorted(tickers, key=lambda t: (counts.get(str(t).upper(), 0), base[t]))


def _av_fallback(get: HttpGet, ticker: str) -> Optional[List[Dict[str, Any]]]:
    """Alpha Vantage OVERVIEW 补齐单只标的（FMP 该标的失败时调用一次）。

    v406：AV 的 25 次/日是**两个消费者共用**的（本层 1 次/票 拿 eq.pe/eq.pb 快照，
    `scripts/cron_equity_fundamentals.py` 4 次/票 拿三表）。名单从 5 只扩到 16 只后，
    如果本层不记账，6h 一轮 × 16 只就能把当日配额吃干 → 财报 cron 静默饿死，
    表现是"三表永远是上周的"而没人报错。所以这里必须走同一个账本，
    并且**给财报 cron 留出下限**（默认 8 次）：快照缺一轮下轮再补，三表缺则是整季缺口。
    """
    try:
        import equity_fundamentals as _ef
        reserve = max(0, int(getattr(config, "AV_SNAPSHOT_RESERVE", 8) or 0))
        state = _ef.budget_state()
        if state["limit"] > 0 and state["remaining"] <= reserve:
            return None        # 配额留给三表抓取，本层本轮跳过（不抛错，上一层会标 unavailable）
        if _ef._take_av_budget():
            return None
    except Exception:
        pass                   # 账本不可用时不阻塞主链路（宁可多花一次，也别让整层挂掉）
    try:
        body = get(
            f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={ticker}"
            f"&apikey={config.ALPHAVANTAGE_API_KEY}",
            _TIMEOUT,
        )
    except Exception:
        return None
    if not isinstance(body, dict) or (not body.get("Symbol") and not body.get("PERatio")):
        return None

    def _pct(v: Any) -> Optional[float]:
        n = _num(v)
        return (n * 100.0) if n is not None else None

    mktcap = _num(body.get("MarketCapitalization"))
    values = {
        "mktcap": (mktcap / 1e9) if mktcap is not None else None,
        "pe": _num(body.get("PERatio")),
        "peg": _num(body.get("PEGRatio")),
        "pb": _num(body.get("PriceToBookRatio")),
        "ps": _num(body.get("PriceToSalesRatioTTM")),
        "eps_ttm": _num(body.get("EPS")),
        "gross_margin": None,          # AV OVERVIEW 不给毛利，留空不猜
        "operating_margin": _pct(body.get("OperatingMarginTTM")),
        "net_margin": _pct(body.get("ProfitMargin")),
        "roe": _pct(body.get("ReturnOnEquityTTM")),
        "debt_to_equity": None,
    }
    extra: Dict[str, Any] = {"freshness": "delayed", "scope": "估值/基本面(AV)",
                             "forward_pe": _num(body.get("ForwardPE")),
                             "target_price": _num(body.get("AnalystTargetPrice"))}
    return _from_values(values, ticker, "alphavantage", extra)



def fetch_snapshot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """美股基本面层（带 TTL 缓存）。FMP 主力 + Alpha Vantage 逐标的补齐。

    FMP 免费 key 直连不稳定（间歇被墙），对 FMP 失败的标的用 AV OVERVIEW 补齐
    （AV 免 key、国内直连可达；免费 25 次/日，配合 6h 缓存最坏 ~20 次/日，留余量）。
    """
    now = time.time()
    key = "equity"
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return list(cached[1])

    if not config.FMP_API_KEY and not config.ALPHAVANTAGE_API_KEY:
        source_status.report("fmp", False, error="未配置 FMP_API_KEY / ALPHAVANTAGE_API_KEY")
        rows = _unavailable_all()
        _set_cache(key, rows, config.EQUITY_CACHE_TTL)
        return rows

    tickers = config.EQUITY_TICKERS or ("NVDA",)
    rows: List[Dict[str, Any]] = []
    fmp_ok = 0
    av_ok = 0

    # ── 阶段 1：FMP 优先（不耗 AV 配额），全量尝试 ──────────────────────────
    # v406 把「逐票 FMP→AV」改成分两阶段。原来边跑边兜底，AV 配额会在名单**前几只**
    # 上耗光，后面的票永远轮不到 —— 实测新增 11 只 eq.pe 一条都没有，就是这个原因。
    pending: List[Tuple[str, Optional[List[Dict[str, Any]]]]] = []
    for ticker in tickers:
        piece = _fmp_one(get, ticker) if config.FMP_API_KEY else None
        if piece and any(r["status"] == "ok" for r in piece):
            fmp_ok += 1
            rows.extend(piece)
        else:
            pending.append((ticker, piece))

    # ── 阶段 2：剩下的票按「快照样本最少优先」排队走 AV ─────────────────────
    # 估值分位要 60 个样本才算数，所以谁的样本少谁先拿配额；否则名单一长，
    # 末尾的票永远停在 1~2 条，永远拿不到分位。
    if pending:
        order = _snapshot_starved([t for t, _ in pending])
        rank = {t: i for i, t in enumerate(order)}
        pending.sort(key=lambda kv: rank.get(kv[0], 10 ** 6))
        for ticker, piece in pending:
            av = _av_fallback(get, ticker) if config.ALPHAVANTAGE_API_KEY else None
            if av and any(r["status"] == "ok" for r in av):
                av_ok += 1
                rows.extend(av)
            elif piece:
                rows.extend(piece)
            else:
                rows.extend(_unavailable_one(ticker))

    if fmp_ok:
        source_status.report("fmp", True, detail=f"美股基本面 {fmp_ok}/{len(tickers)} 只(FMP)")
    else:
        source_status.report("fmp", False, error="FMP 无成功标的" if config.FMP_API_KEY else "未配置 FMP_API_KEY")
    if av_ok:
        source_status.report("alphavantage", True, detail=f"{av_ok} 只(AV 补齐)")

    _set_cache(key, rows, config.EQUITY_CACHE_TTL)
    return rows