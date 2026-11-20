# -*- coding: utf-8 -*-
"""知名投资人 13F 持仓路由（SEC EDGAR 公开数据，只读）。

与「聪明钱」面板的区别：
  · 聪明钱 = Hyperliquid 链上钱包的实时成交（分钟级）；
  · 本模块 = SEC 13F-HR 季度持仓（季末后 45 天内申报），是巴菲特/科恩/达利欧
    这类不上链的人**唯一**可核验的公开口径。

设计取舍（2026-11-20）：
  · 一次 SELECT 全表在内存里分组，而不是"每个经理 2 条 SQL"——Point72 一期
    就有 3,856 行，按经理逐条查会把接口拖到十几秒、前端 12s 超时白屏；
  · 只展示"当期拿得到"的人：表里保留历史，但 `/managers` 只返回最新一期，
    旧季报由前端 stale 标记，不会混进共识与加减仓。
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "trident_event_bus.db",
)

# 申报滞后 45 天 + 季末，超过 ~7 个月视为过期（提醒用户别当实时用）
_STALE_DAYS = 210

# 同一家公司在 13F 里常按股类/后缀拆成多条（ALPHABET INC A/C、BERKSHIRE HATHAWAY
# INC DEL / CL B…）。共识持仓按"公司"合并，否则 2 家持有 Alphabet 会被算成
# 两个"只有 1 家持有"的标的。
_DROP_TOKENS = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD",
    "LIMITED", "PLC", "NV", "SA", "SPA", "AG", "TRUST", "HOLDINGS", "GROUP",
    "DEL", "NEW", "COM", "CL", "CLASS", "A", "B", "C", "SHS", "ADR", "ADS",
    "SPONSORED", "ORD", "PAR",
}


def _norm_issuer(name: str) -> str:
    toks = [t for t in name.replace(",", " ").replace(".", " ").split()
            if t.upper() not in _DROP_TOKENS]
    return " ".join(toks).upper() or name.upper()


def _is_stale(period: str) -> bool:
    try:
        ts = time.mktime(time.strptime(period, "%Y-%m-%d"))
    except Exception:  # noqa: BLE001
        return True
    return (time.time() - ts) / 86400 > _STALE_DAYS


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _load_all() -> List[sqlite3.Row]:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT manager, person, cik, report_date, filed_date, issuer, cusip, "
            "       value_usd, shares, put_call "
            "FROM sec_13f_holdings ORDER BY cik, report_date DESC, value_usd DESC"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise HTTPException(503, f"13F 表未就绪: {exc}")
    finally:
        conn.close()


def _group(rows: List[sqlite3.Row]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """按 (cik, manager) 聚成 {period: [holding, ...]}，period 降序。"""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in rows:
        key = (r["cik"], r["manager"])
        g = out.setdefault(key, {"person": r["person"], "filed": {}, "periods": {}})
        g["filed"][r["report_date"]] = r["filed_date"]
        g["periods"].setdefault(r["report_date"], []).append(dict(r))
    for g in out.values():
        for p in g["periods"].values():
            p.sort(key=lambda h: -(h["value_usd"] or 0))
    return out


@router.get("/api/institutional/13f/managers")
def institutional_13f_managers() -> Dict[str, Any]:
    """每位投资人最新一期 13F：总市值、持仓数、前五、季环比。"""
    t0 = time.time()
    rows = _load_all()
    groups = _group(rows)
    out: List[Dict[str, Any]] = []

    for (cik, manager), g in groups.items():
        periods = sorted(g["periods"].keys(), reverse=True)
        if not periods:
            continue
        period = periods[0]
        latest = g["periods"][period]
        prev_period = periods[1] if len(periods) > 1 else None
        prev = g["periods"][prev_period] if prev_period else []
        total = sum(h["value_usd"] or 0 for h in latest)
        prev_map = {h["cusip"]: h for h in prev}
        prev_by_name = {_norm_issuer(h["issuer"]): h for h in prev}

        def _prev_of(h: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            return prev_map.get(h["cusip"]) or prev_by_name.get(_norm_issuer(h["issuer"]))

        top = []
        for h in latest[:5]:
            p = _prev_of(h)
            top.append({
                "issuer": h["issuer"],
                "value_usd": round(h["value_usd"] or 0, 2),
                "weight": round((h["value_usd"] or 0) / total, 4) if total else 0,
                "qoq_usd": round((h["value_usd"] or 0) - (p["value_usd"] if p else 0), 2),
                "is_new": bool(prev) and p is None,
            })

        # 先算一次集合：Point72 一期 3,856 行，逐行建集合会变成 O(n²)（实测 26s）
        latest_names = {_norm_issuer(h["issuer"]) for h in latest}
        new_pos = [h["issuer"] for h in latest if _prev_of(h) is None] if prev else []
        exited = [h["issuer"] for h in prev
                  if _norm_issuer(h["issuer"]) not in latest_names] if prev else []

        out.append({
            "manager": manager,
            "person": g["person"],
            "cik": cik,
            "report_date": period,
            "filed_date": g["filed"].get(period),
            "total_usd": round(total, 2),
            "positions": len(latest),
            "top10_weight": round(
                sum(h["value_usd"] or 0 for h in latest[:10]) / total, 4) if total else 0,
            "top_holdings": top,
            "prev_report_date": prev_period,
            "new_positions": new_pos[:8],
            "exited_positions": exited[:8],
            "stale": _is_stale(period),
            "has_prior": bool(prev_period),
        })

    out.sort(key=lambda m: -m["total_usd"])
    return {
        "managers": out,
        "count": len(out),
        "source": "SEC EDGAR 13F-HR（季度申报，季末后 45 天内）",
        "as_of": max((m["report_date"] for m in out), default=None),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


@router.get("/api/institutional/13f/holdings")
def institutional_13f_holdings(
    manager: str = Query(..., description="manager 全名，取 /managers 返回值"),
    period: Optional[str] = Query(None, description="report_date，缺省取最新"),
) -> Dict[str, Any]:
    conn = _conn()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT cik, person FROM sec_13f_holdings WHERE manager=? LIMIT 1", (manager,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "未收录该投资人")
    cik = row["cik"]
    if not period:
        prow = cur.execute(
            "SELECT MAX(report_date) rd FROM sec_13f_holdings WHERE cik=?", (cik,)
        ).fetchone()
        period = prow["rd"]
    rows = cur.execute(
        "SELECT issuer, cusip, value_usd, shares, put_call FROM sec_13f_holdings "
        "WHERE cik=? AND report_date=? ORDER BY value_usd DESC", (cik, period),
    ).fetchall()
    conn.close()
    total = sum((h["value_usd"] or 0) for h in rows)
    return {
        "manager": manager,
        "person": row["person"],
        "cik": cik,
        "report_date": period,
        "total_usd": round(total, 2),
        "positions": len(rows),
        "holdings": [
            {**dict(h), "weight": round((h["value_usd"] or 0) / total, 4) if total else 0}
            for h in rows
        ],
    }


@router.get("/api/institutional/13f/consensus")
def institutional_13f_consensus(limit: int = Query(12, ge=3, le=30)) -> Dict[str, Any]:
    """各经理最新一期的共识持仓 + 本季最大加仓/减仓（跨经理汇总）。"""
    rows = _load_all()
    groups = _group(rows)
    if not groups:
        raise HTTPException(503, "13F 表为空")

    holder: Dict[str, Dict[str, Any]] = {}
    changes: List[Dict[str, Any]] = []
    covered = 0

    for (cik, manager), g in groups.items():
        periods = sorted(g["periods"].keys(), reverse=True)
        latest = g["periods"][periods[0]]
        prev = g["periods"][periods[1]] if len(periods) > 1 else []
        prev_map = {h["cusip"]: h for h in prev}
        prev_by_name = {_norm_issuer(h["issuer"]): h for h in prev}
        skip_changes = _is_stale(periods[0])   # 旧季报不参与"本季加减仓"
        covered += 1

        for h in latest:
            k = _norm_issuer(h["issuer"])
            rec = holder.setdefault(k, {
                "issuer": h["issuer"], "cusip": h["cusip"],
                "holders": [], "value_usd": 0.0,
            })
            if manager not in rec["holders"]:      # 同一经理两个股类只算一家
                rec["holders"].append(manager)
            rec["value_usd"] += h["value_usd"] or 0

            if prev and not skip_changes:
                p = prev_map.get(h["cusip"]) or prev_by_name.get(k)
                delta = (h["value_usd"] or 0) - (p["value_usd"] if p else 0)
                if abs(delta) > 0:
                    changes.append({
                        "manager": manager, "issuer": h["issuer"], "cusip": h["cusip"],
                        "delta_usd": round(delta, 2),
                        "action": "new" if p is None else ("add" if delta > 0 else "trim"),
                    })

    lim = limit if isinstance(limit, int) else 12
    cons = sorted(holder.values(), key=lambda x: (-len(x["holders"]), -x["value_usd"]))[:lim]
    adds = sorted([c for c in changes if c["action"] != "trim"],
                  key=lambda x: -x["delta_usd"])[:lim]
    trims = sorted([c for c in changes if c["action"] == "trim"],
                   key=lambda x: x["delta_usd"])[:lim]
    return {
        "consensus": [
            {**c, "value_usd": round(c["value_usd"], 2),
             "holder_count": len(c["holders"]), "holders": c["holders"][:8]}
            for c in cons
        ],
        "top_adds": adds,
        "top_trims": trims,
        "managers_covered": covered,
    }
