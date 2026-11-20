#!/usr/bin/env python3
"""SEC companyfacts —— 净资产/资产负债表的**免配额**通道（v454）。

为什么加这条通道：
  · v448 实测 10 只票里 AAPL/AMZN/GOOGL 的 `total_equity` 全为 NULL —— AV 的
    balance 端点免费档只给部分字段，FMP 直接 402。PB 分位因此 0/16。
  · SEC EDGAR 的 companyfacts 是公开、免配额、季频、**带真实申报日**的三表数据。
    对"净资产"这一个分母来说，它比 AV/FMP 都稳，而且没有配额焦虑。

防前视（这是本模块存在的第二个理由）：
  · SEC 返回的每个数据点都有 `filed`（申报日）。会计期 `end` 之后、申报日之前，
    市场是**不知道**这个数的。`equity_fundamentals._equity_asof` 原来只按
    `period_end` 匹配，等于用"季末"当"已知日"，最多提前 6 周。
    本模块把 `filed` 写进 payload 的 `filed` 字段，`_equity_asof` 会优先用它。

口径：
  · 净资产优先 us-gaap:StockholdersEquity；
  · 拿不到时用 Assets − Liabilities 派生，并在 payload 标 `derived: assets-liabilities`；
  · 同一 period_end 多次出现（年报重述/10-K 覆盖 10-Q）→ 只留 filed 最新的一条。

不做什么：
  · 不抓利润表/现金流表（AV + yfinance 已覆盖，重复抓只会引入口径冲突）；
  · 不做 ROE 反推净资产（美股源没有 ROE 字段，反推是假精度）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

_TIMEOUT = 20
_UA = "reflex-quant research z2132743607@163.com"
# SEC 要求 UA 里带联系人；速率上限 10 req/s，我们一次只抓 3 个 concept。
_RETRIES = 3
_CACHE_TTL = 86400          # 季频数据，一天一次足够
_CIK_TTL = 7 * 86400

_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
_CONCEPT = ("https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json")

_EQUITY_TAGS = ("StockholdersEquity",)
_ASSET_TAGS = ("Assets",)
_LIAB_TAGS = ("Liabilities",)

_cache: Dict[str, Tuple[float, Any]] = {}


def _cache_file(name: str) -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)),
                                            ), "runtime", "sec_cache")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = "/tmp"
    return os.path.join(base, name)


def _disk_cached(name: str, ttl: float, loader: Callable[[], Any]) -> Any:
    """进程内 + 落盘两级缓存：CIK 表一天抓一次，concept 一天一次，失败不缓存。"""
    now = time.time()
    hit = _cache.get(name)
    if hit and hit[0] > now:
        return hit[1]
    path = _cache_file(name)
    try:
        if os.path.exists(path) and now - os.path.getmtime(path) < ttl:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            _cache[name] = (now + ttl, data)
            return data
    except Exception:
        pass
    data = loader()
    _cache[name] = (now + ttl, data)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass
    return data


def _get(url: str, timeout: int = _TIMEOUT) -> Any:
    last: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = resp.headers.get("Content-Encoding") or ""
                if "gzip" in enc:
                    import gzip
                    raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    if last is not None:
        raise last
    raise RuntimeError("unreachable")


def cik_map(get: Callable[[str], Any] = _get) -> Dict[str, int]:
    """ticker → CIK。SEC 官方 ticker 表（约 1 万条），落盘缓存 7 天。"""
    def _load() -> Dict[str, int]:
        body = get(_CIK_URL) or {}
        out: Dict[str, int] = {}
        for item in (body.values() if isinstance(body, dict) else []):
            try:
                out[str(item["ticker"]).upper()] = int(item["cik_str"])
            except Exception:
                continue
        return out

    try:
        return _disk_cached("company_tickers.json", _CIK_TTL, _load)
    except Exception:
        return {}


def concept_points(ticker: str, tag: str,
                   get: Callable[[str], Any] = _get) -> List[Dict[str, Any]]:
    """取某个 us-gaap concept 的全部季频点（只保留 10-Q/10-K/10-KT）。"""
    cik = cik_map(get).get(str(ticker).upper())
    if not cik:
        return []

    def _load() -> List[Dict[str, Any]]:
        body = get(_CONCEPT.format(cik=cik, tag=tag)) or {}
        units = (body.get("units") or {}).get("USD") or []
        out: List[Dict[str, Any]] = []
        for item in units:
            if str(item.get("form") or "") not in ("10-Q", "10-K", "10-KT", "20-F", "40-F"):
                continue
            try:
                end = str(item["end"])[:10]
                filed = str(item.get("filed") or end)[:10]
                value = float(item["val"])
            except Exception:
                continue
            out.append({"end": end, "filed": filed, "val": value,
                         "form": item.get("form"), "fp": item.get("fp"), "fy": item.get("fy")})
        return out

    try:
        return _disk_cached(f"concept_{ticker.upper()}_{tag}.json", _CACHE_TTL, _load)
    except Exception:
        return []


def parse_balance(equity: List[Dict[str, Any]],
                  assets: Optional[List[Dict[str, Any]]] = None,
                  liabilities: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """把三组 concept 点拼成按季度的资产负债表读数（纯函数，便于离线测试）。

    - 同一 end 多条 → 留 filed 最新的（年报重述会覆盖同一期的旧值）；
    - 只有期末(=季末)的点参与，`frame` 里的年度累计点在 SEC 这里不出现，无需过滤；
    - 净资产缺失时用同一期的 Assets−Liabilities，标 derived。
    """
    def _by_end(points: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        best: Dict[str, Dict[str, Any]] = {}
        for p in (points or []):
            end = str(p.get("end") or "")[:10]
            if not end:
                continue
            prev = best.get(end)
            if prev is None or str(p.get("filed") or "") >= str(prev.get("filed") or ""):
                best[end] = p
        return best

    eq = _by_end(equity)
    ta = _by_end(assets)
    tl = _by_end(liabilities)
    out: List[Dict[str, Any]] = []
    for end in sorted(set(eq) | set(ta) | set(tl)):
        equity_val: Optional[float] = None
        derived = None
        if end in eq:
            equity_val = float(eq[end]["val"])
        elif end in ta and end in tl:
            cand = float(ta[end]["val"]) - float(tl[end]["val"])
            if cand > 0:
                equity_val = cand
                derived = "assets-liabilities"
        filed = str((eq.get(end) or ta.get(end) or tl.get(end) or {}).get("filed") or end)[:10]
        assets_val = float(ta[end]["val"]) if end in ta else None
        liab_val = float(tl[end]["val"]) if end in tl else None
        period = _period_label(end)
        out.append({
            "period_end": end,
            "filed": filed,
            "total_equity": equity_val,
            "total_assets": assets_val,
            "total_liabilities": liab_val,
            "derived": derived,
            "period": period,
            "fiscal_year": int(end[:4]) if end[:4].isdigit() else None,
        })
    return out


def _period_label(period_end: str) -> str:
    """季末 → Q1/Q2/Q3/Q4；非标准季末（如 AAPL 的 3-30/6-27）按月份就近归档。"""
    try:
        month = int(str(period_end)[5:7])
    except Exception:
        return ""
    idx = min(4, max(1, (month + 2) // 3))
    return f"Q{idx}"


def balance_rows(ticker: str, get: Callable[[str], Any] = _get,
                 limit: int = 12) -> List[Dict[str, Any]]:
    """一只票的最近 N 期资产负债表读数（已拼好、可直接落库）。"""
    equity = concept_points(ticker, _EQUITY_TAGS[0], get)
    assets = concept_points(ticker, _ASSET_TAGS[0], get)
    liab = concept_points(ticker, _LIAB_TAGS[0], get)
    rows = parse_balance(equity, assets, liab)
    return rows[-limit:] if limit > 0 else rows


def merge_into_db(conn: Any, ticker: str, rows: Optional[List[Dict[str, Any]]] = None,
                  dry_run: bool = False, retries: int = 6) -> Dict[str, int]:
    """把 SEC 读数写进 `equity_fundamentals`。

    只补充**该 period_end 还没有净资产**的行；已有 AV/yfinance 的行不动
    （多源之间谁的 revenue 更准各有主张，但"净资产有没有"是硬缺口，SEC 最稳）。

    写锁纪律：引擎/API 常驻进程会长时间持写锁，实测批量写会被 SQLITE_BUSY 打断。
    所以（1）网络抓取在拿锁之前完成；（2）每次只为一只票开一个短事务；
    （3）失败按指数退避重试，整段可安全重放（未提交即未生效，不会产生重复行）。
    """
    ticker = str(ticker).upper()
    rows = rows if rows is not None else balance_rows(ticker)
    stats = {"seen": len(rows), "inserted": 0, "updated": 0, "skipped": 0}
    if not rows:
        return stats
    if dry_run:
        return stats
    last: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            _merge_once(conn, ticker, rows, stats)
            return stats
        except Exception as exc:
            last = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            stats.update({"inserted": 0, "updated": 0, "skipped": 0})
            try:
                conn.rollback()
            except Exception:
                pass
            time.sleep(min(8.0, 0.6 * (2 ** attempt)))
    if last is not None:
        raise last
    return stats


def _merge_once(conn: Any, ticker: str, rows: List[Dict[str, Any]],
                stats: Dict[str, int]) -> None:
    now = int(time.time())
    for rec in rows:
        if not rec.get("total_equity"):
            stats["skipped"] += 1
            continue
        payload = json.dumps({"filed": rec.get("filed"), "derived": rec.get("derived"),
                              "sec_period": rec.get("period")}, ensure_ascii=False)
        existing = conn.execute(
            """SELECT id, total_equity FROM equity_fundamentals
               WHERE ticker=? AND period_end=?""",
            (ticker, rec["period_end"]),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO equity_fundamentals
                   (ticker, period_end, fiscal_year, period, total_assets,
                    total_liabilities, total_equity, source, fetched_at, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ticker, rec["period_end"], rec.get("fiscal_year"), rec.get("period"),
                 rec.get("total_assets"), rec.get("total_liabilities"),
                 float(rec["total_equity"]), "sec", now, payload),
            )
            stats["inserted"] += 1
        elif existing[1] is None:
            conn.execute(
                """UPDATE equity_fundamentals
                   SET total_equity=?, total_assets=COALESCE(total_assets, ?),
                       total_liabilities=COALESCE(total_liabilities, ?),
                       payload=?, source=COALESCE(NULLIF(source,''), 'sec')
                   WHERE id=?""",
                (float(rec["total_equity"]), rec.get("total_assets"),
                 rec.get("total_liabilities"), payload, existing[0]),
            )
            stats["updated"] += 1
        else:
            stats["skipped"] += 1
    conn.commit()


def equity_known_date(period_end: str, filed: Optional[str] = None) -> str:
    """「这个净资产从哪天起算已知」—— 有申报日就用申报日，没有退回季末。

    给 `_equity_asof` 用：季末之后、申报日之前，市场并不知道这个数。
    """
    pe = str(period_end or "")[:10]
    fl = str(filed or "")[:10]
    if not fl:
        return pe
    return fl if fl > pe else pe


__all__ = ["cik_map", "concept_points", "parse_balance", "balance_rows",
           "merge_into_db", "equity_known_date"]
