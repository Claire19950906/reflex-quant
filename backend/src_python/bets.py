# -*- coding: utf-8 -*-
"""单押注检测（v449，只读）。

问题不是"信号够不够多"，而是**这些信号是不是同一注**：

  11-16～11-18 的 13 笔未平仓里，WTI 6 笔、XAU 7 笔；BTC/ETH/SOL 的
  60 日收益相关性 0.85–0.93，WTI 与它们 -0.24，XAU 与它们 ~0。
  也就是说：**13 笔"仓位"其实只有 3 注独立方向**（WTI 多 / WTI 空 / XAU 空）。

这会影响两件事：
  1. 风控：同簇同向重复下注，等于把一份风险放大 N 倍；
  2. 统计：样本独立性被高估，"13 笔"不能当 13 个证据。

本模块只做计算与分组，不改仓、不下单。
"""
from __future__ import annotations

import datetime as _dt
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# v480：库路径统一走 db_location（唯一来源，支持 TRIDENT_DB_PATH 覆盖）。
# 旧版硬编码生产库，测试设了 TRIDENT_DB_PATH 也绕不过去 —— v483 记录的
# 「pytest 写进生产库」事故，这类硬编码就是其中一条路径。
from db_location import DB_PATH

TRADABLE = ("BTC", "ETH", "SOL", "WTI", "XAU")
# 正相关到这个程度，视为"同一个风险源"
CORR_CLUSTER = 0.70
# 至少要有这么多共同交易日才计算相关性
MIN_COMMON_DAYS = 20
DEFAULT_WINDOW_DAYS = 60


def _ro_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


# ────────────────────────── 相关性 ──────────────────────────
def _daily_closes(conn: sqlite3.Connection, asset: str,
                  since_ts: int) -> Dict[_dt.date, float]:
    rows = conn.execute(
        "SELECT ts, close FROM kline_bars "
        "WHERE symbol=? AND timeframe='1d' AND ts>=? ORDER BY ts ASC",
        (asset.upper(), since_ts * 1000),
    ).fetchall()
    out: Dict[_dt.date, float] = {}
    for r in rows:
        ts, close = int(r[0]), r[1]
        ts = ts // 1000 if ts > 10_000_000_000 else ts
        if not close:
            continue
        out[_dt.datetime.fromtimestamp(ts, _dt.timezone.utc).date()] = float(close)
    return out


def correlation_matrix(conn: sqlite3.Connection, window_days: int = DEFAULT_WINDOW_DAYS,
                       assets: Sequence[str] = TRADABLE) -> Dict[str, Any]:
    """日收益相关矩阵。只用真实日线，共同交易日不足就不算。"""
    now = int(time.time())
    data = {a: _daily_closes(conn, a, now - (window_days + 15) * 86400) for a in assets}
    common = set.intersection(*[set(d) for d in data.values()]) if data else set()
    dates = sorted(common)[-window_days:]
    matrix: Dict[str, Dict[str, Optional[float]]] = {a: {} for a in assets}
    if len(dates) < MIN_COMMON_DAYS:
        for a in assets:
            for b in assets:
                matrix[a][b] = None
        return {"assets": list(assets), "matrix": matrix, "common_days": len(dates),
                "note": f"共同交易日 {len(dates)} < {MIN_COMMON_DAYS}，相关性不下结论"}
    rets: Dict[str, List[float]] = {}
    for a in assets:
        s = [data[a][d] for d in dates]
        rets[a] = [s[i] / s[i - 1] - 1.0 for i in range(1, len(s)) if s[i - 1] > 0]
    for a in assets:
        for b in assets:
            ra, rb = rets[a], rets[b]
            n = min(len(ra), len(rb))
            if n < MIN_COMMON_DAYS:
                matrix[a][b] = None
                continue
            ma, mb = sum(ra[:n]) / n, sum(rb[:n]) / n
            cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n)) / n
            va = sum((x - ma) ** 2 for x in ra[:n]) / n
            vb = sum((x - mb) ** 2 for x in rb[:n]) / n
            sd = math.sqrt(va * vb)
            matrix[a][b] = round(cov / sd, 3) if sd > 0 else None
    return {"assets": list(assets), "matrix": matrix, "common_days": len(dates),
            "from": dates[0].isoformat(), "to": dates[-1].isoformat()}


def clusters_from_matrix(corr: Dict[str, Any],
                         threshold: float = CORR_CLUSTER) -> List[List[str]]:
    """正相关 ≥ 阈值的连通分量。负相关不合并（那是分散，不是同注）。"""
    assets = corr.get("assets") or []
    m = corr.get("matrix") or {}
    seen = set()
    out: List[List[str]] = []
    for a in assets:
        if a in seen:
            continue
        stack = [a]
        group: List[str] = []
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            group.append(cur)
            for b in assets:
                if b in seen:
                    continue
                v = (m.get(cur) or {}).get(b)
                if v is not None and v >= threshold:
                    stack.append(b)
        out.append(group)
    return out


# ────────────────────────── 持仓 / 分注 ──────────────────────────
def _last_price(conn: sqlite3.Connection, asset: str,
                tol_days: int = 7) -> Optional[float]:
    row = conn.execute(
        "SELECT price FROM market_ticks WHERE symbol=? AND price>0 "
        "ORDER BY ts DESC LIMIT 1", (asset.upper(),),
    ).fetchone()
    return float(row[0]) if row else None


def open_positions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """未平仓的模拟仓（trade_decisions.status='open'）。"""
    rows = conn.execute(
        "SELECT id, asset, final_action, entry_price, size_usdt, decision_ts "
        "FROM trade_decisions WHERE status='open' "
        "AND final_action IN ('BUY','SELL') ORDER BY decision_ts ASC",
    ).fetchall()
    out = []
    for r in rows:
        asset = str(r[1] or "").upper()
        action = str(r[2] or "").upper()
        entry = float(r[3] or 0)
        now = _last_price(conn, asset)
        pnl_pct = None
        if now and entry > 0:
            pnl_pct = ((now / entry - 1) * 100 if action == "BUY"
                       else (entry / now - 1) * 100)
        out.append({
            "id": int(r[0]), "asset": asset, "action": action,
            "entry_price": entry, "size_usdt": float(r[4] or 0),
            "decision_ts": int(float(r[5] or 0)),
            "last_price": now,
            "pnl_pct": round(pnl_pct, 3) if pnl_pct is not None else None,
        })
    return out


def build_bets(positions: List[Dict[str, Any]],
               clusters: List[List[str]]) -> Dict[str, Any]:
    """把仓位按「簇 + 方向」合并成注。同簇同向 = 一注。"""
    cluster_of = {a: i for i, group in enumerate(clusters) for a in group}
    label_of = {i: "/".join(group) for i, group in enumerate(clusters)}
    groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for p in positions:
        idx = cluster_of.get(p["asset"])
        if idx is None:
            # 不在可算相关性的白名单里，单独成一注
            idx = 1000 + len(groups)
            label_of[idx] = p["asset"]
        groups.setdefault((idx, p["action"]), []).append(p)

    bets = []
    for (idx, action), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        total_size = sum(float(x["size_usdt"] or 0) for x in items)
        weighted = (sum(float(x["entry_price"]) * float(x["size_usdt"] or 0) for x in items)
                    / total_size) if total_size else None
        pnls = [x["pnl_pct"] for x in items if x["pnl_pct"] is not None]
        bets.append({
            "cluster": label_of.get(idx, "?"),
            "direction": action,
            "raw_positions": len(items),
            "assets": sorted({x["asset"] for x in items}),
            "total_size_usdt": round(total_size, 2),
            "avg_entry": round(weighted, 6) if weighted else None,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 3) if pnls else None,
            "ids": [x["id"] for x in items],
        })

    # 同品种反方向：净敞口小，但两边成本都付了
    by_asset: Dict[str, set] = {}
    for p in positions:
        by_asset.setdefault(p["asset"], set()).add(p["action"])
    opposing = sorted([a for a, dirs in by_asset.items() if len(dirs) > 1])

    warnings: List[str] = []
    if positions and len(bets) < len(positions):
        warnings.append(
            f"{len(positions)} 笔仓位实际只有 {len(bets)} 注独立方向："
            "同簇同向被合并，风控按注算，不能按笔算"
        )
    for a in opposing:
        warnings.append(f"{a} 同时有多/空仓：净敞口不大，但两条腿都在付成本")
    if bets:
        top = max(bets, key=lambda b: b["total_size_usdt"] or 0)
        share = (top["total_size_usdt"] / max(1e-9, sum(b["total_size_usdt"] for b in bets)))
        if share >= 0.40:
            warnings.append(
                f"{top['cluster']} {top['direction']} 占总敞口 {share:.0%}，集中度偏高"
            )
    return {"bets": bets, "warnings": warnings, "opposing_assets": opposing}


def scan(db_path: Optional[Path] = None,
         window_days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    conn = _ro_conn(db_path)
    try:
        corr = correlation_matrix(conn, window_days=window_days)
        clusters = clusters_from_matrix(corr)
        positions = open_positions(conn)
        built = build_bets(positions, clusters)
    finally:
        conn.close()

    raw_n = len(positions)
    effective_n = len(built["bets"])
    exposure = sum(b["total_size_usdt"] for b in built["bets"])
    return {
        "generated_at": int(time.time()),
        "window_days": window_days,
        "corr_threshold": CORR_CLUSTER,
        "correlation": corr,
        "clusters": [{"cluster": "/".join(g), "assets": g} for g in clusters],
        "positions": positions,
        "bets": built["bets"],
        "warnings": built["warnings"],
        "opposing_assets": built["opposing_assets"],
        "raw_positions": raw_n,
        "effective_bets": effective_n,
        "total_exposure_usdt": round(exposure, 2),
        "headline": (
            f"{raw_n} 笔未平仓 → {effective_n} 注独立方向"
            + ("；" + built["warnings"][0] if built["warnings"] else "")
            if raw_n else "当前没有未平仓仓位"
        ),
        "note": ("相关性用日线收益（共同交易日不足 20 天不下结论）；"
                 "仓位来自 trade_decisions.status='open'；同簇同向按一注合并。"),
    }


def _main() -> int:
    import json
    rep = scan()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
