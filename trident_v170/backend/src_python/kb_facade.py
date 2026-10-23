#!/usr/bin/env python3
"""kb_facade.py — 知识库门面（KB Facade）v182.2

统一查询现有 51 张表的经验 / 因果 / 回测 / 异动 / 洞察数据，供：
  - reflection_checkers（按维度检索上下文，**不注入 prompt，纯数据查询**）
  - causal_graph_panel（图谱节点补全）
  - 前端 KB 面板（资产全息视图）

设计原则：
  - **只读不写**：不创建新表、不迁移现有数据、不写现有表
  - **0 token**：不注入 prompt，纯 SQL 聚合门面
  - **多维度**：asset / since / dim_code / origin / source 任意组合过滤
  - **兜底**：表/列缺失 → 返回空结构，不崩（sqlite3.OperationalError 兜底）

表覆盖（不重建）：
  decision_errors / decision_reflection / structured_reflections / trade_reflections
  review_insights / causal_chains / asset_driver_nodes / asset_driver_edges
  abnormal_signals / backtest_event_outcomes / quick_sim_trades
  kline_snapshots / factor_snapshots / news_events
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import db


# ── 时间窗口解析 ──────────────────────────────────────────────────────────
def parse_since(since) -> int:
    """'7d' / '24h' / '30d' / '1w' / 0 / unix ts → unix ts 起点。无效 → 7 天前。

    since='all' 或 0 / '0' → 0（不过滤）。
    """
    if since is None or since == "" or since == "all" or since == 0 or since == "0":
        return 0
    if isinstance(since, (int, float)) and since > 0:
        return int(since)
    s = str(since).strip().lower()
    for unit, mult in (("w", 86400 * 7), ("d", 86400), ("h", 3600), ("m", 60)):
        if s.endswith(unit):
            try:
                n = int(s[:-1])
                if n <= 0:
                    break
                return int(time.time()) - n * mult
            except ValueError:
                break
    return int(time.time()) - 7 * 86400


# ── 维度代码常量（与 reflection_checkers.check_xxx 一一对应）─────────────
DIM_DATA = "data"           # E001/E002 数据缺失/陈旧
DIM_LOGIC = "logic"         # E003 逻辑矛盾 / E006 / E008 / E009 / E013
DIM_CAUSAL = "causal"       # E004 因果断裂 + E012 + causal_chains
DIM_RULES = "rules"         # E005 规则违反 + review_insights
DIM_MARKET = "market"       # E007/E010/E011 + abnormal_signals + factor_snapshots

ALL_DIMS: Tuple[str, ...] = (DIM_DATA, DIM_LOGIC, DIM_CAUSAL, DIM_RULES, DIM_MARKET)

ERRCODE_TO_DIM: Dict[str, str] = {
    "E001": DIM_DATA, "E002": DIM_DATA,
    "E003": DIM_LOGIC,
    "E004": DIM_CAUSAL,
    "E005": DIM_RULES,
    "E006": DIM_LOGIC, "E008": DIM_LOGIC, "E009": DIM_LOGIC, "E013": DIM_LOGIC,
    "E007": DIM_MARKET, "E010": DIM_MARKET, "E011": DIM_MARKET,
    "E012": DIM_CAUSAL,
}

CHECKER_TO_DIM: Dict[str, str] = {
    "check_data":   DIM_DATA,
    "check_logic":  DIM_LOGIC,
    "check_rules":  DIM_RULES,
    "check_causal": DIM_CAUSAL,
    "check_market": DIM_MARKET,
}


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _safe_json(raw: Any, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _close(own: bool, conn: sqlite3.Connection) -> None:
    if own:
        try:
            conn.close()
        except Exception:
            pass


# ── 1. 资产全息视图（get_for_asset） ──────────────────────────────────────
def get_for_asset(asset: str, since: str = "7d",
                  limit_per_kind: int = 20,
                  conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """聚合某资产在 since 窗口内的所有相关事实（13 类，缺失表全兜底 []）。

    返回结构（每个 key 都兜底 []）：
      asset / since_ts / now_ts
      errors / reflections / structured_reflections / trade_reflections
      review_insights / causal_chains
      asset_driver_nodes / asset_driver_edges
      abnormal_signals / backtest_outcomes
      trades / kline_snapshots / factor_snapshots
    """
    own = conn is None
    conn = conn or _connect()
    since_ts = parse_since(since)
    now_ts = int(time.time())
    asset_u = (asset or "").upper()
    asset_like = f"%{asset_u}%"

    out: Dict[str, Any] = {
        "asset": asset_u, "since_ts": since_ts, "now_ts": now_ts,
        "errors": [], "reflections": [], "structured_reflections": [],
        "trade_reflections": [], "review_insights": [],
        "causal_chains": [], "asset_driver_nodes": [], "asset_driver_edges": [],
        "abnormal_signals": [], "backtest_outcomes": [], "trades": [],
        "kline_snapshots": [], "factor_snapshots": [],
    }

    def _since_clause(unix_col: bool) -> Tuple[str, List[Any]]:
        if since_ts <= 0:
            return ("1=1", [])
        if unix_col:
            return ("ts >= ?", [since_ts])
        return ("created_at >= datetime(?, 'unixepoch')", [since_ts])

    try:
        # errors（JOIN ai_decisions 拿 asset）
        clause, params = _since_clause(False)
        rows = conn.execute(
            f"""SELECT de.decision_id, de.error_code, de.severity, de.detail,
                       de.evidence_refs, de.origin, de.source, de.created_at,
                       ad.target_asset, ad.suggested_action
                FROM decision_errors de
                LEFT JOIN ai_decisions ad ON ad.id = de.decision_id
                WHERE (ad.target_asset = ? OR ad.target_asset LIKE ?)
                  AND {clause}
                ORDER BY de.severity DESC, de.created_at DESC LIMIT ?""",
            (asset_u, asset_like, *params, limit_per_kind),
        ).fetchall()
        for r in rows:
            d = dict(r)
            d["evidence_refs"] = _safe_json(d.get("evidence_refs"), {})
            out["errors"].append(d)

        # review_insights
        rows = conn.execute(
            """SELECT id, rule, asset, scope, active, source_samples, origin, kind,
                      counter_factual, created_at
               FROM review_insights
               WHERE (asset = ? OR asset = '' OR asset = 'all') AND active = 1
                 AND manual_deactivated = 0
               ORDER BY source_samples DESC, created_at DESC LIMIT ?""",
            (asset_u, limit_per_kind),
        ).fetchall()
        out["review_insights"] = [dict(r) for r in rows]

        # causal_chains（不按 asset 过滤：宏观链跨品种通用）
        rows = conn.execute(
            """SELECT id, name, cause, effect, trigger_words, weight, mechanism,
                      precondition, invalidation, origin, updated_at
               FROM causal_chains WHERE enabled = 1 ORDER BY weight DESC LIMIT ?""",
            (limit_per_kind,),
        ).fetchall()
        out["causal_chains"] = [dict(r) for r in rows]

        # asset_driver_nodes / edges
        rows = conn.execute(
            """SELECT id, asset, label, kind, is_core, confidence, evidence, version, active
               FROM asset_driver_nodes
               WHERE (asset = ? OR asset = 'all') AND active = 1
               ORDER BY is_core DESC, confidence DESC LIMIT ?""",
            (asset_u, limit_per_kind),
        ).fetchall()
        nodes = [dict(r) for r in rows]
        out["asset_driver_nodes"] = nodes
        if nodes:
            node_ids = tuple(n["id"] for n in nodes)
            placeholders = ",".join("?" * len(node_ids))
            rows = conn.execute(
                f"""SELECT e.id, e.from_id, e.to_id, e.kind, e.weight, e.evidence,
                           fn.label AS from_label, fn.asset AS from_asset,
                           tn.label AS to_label, tn.asset AS to_asset
                    FROM asset_driver_edges e
                    LEFT JOIN asset_driver_nodes fn ON fn.id = e.from_id
                    LEFT JOIN asset_driver_nodes tn ON tn.id = e.to_id
                    WHERE e.from_id IN ({placeholders}) OR e.to_id IN ({placeholders})
                    ORDER BY e.weight DESC LIMIT ?""",
                (*node_ids, *node_ids, limit_per_kind),
            ).fetchall()
            out["asset_driver_edges"] = [dict(r) for r in rows]

        # abnormal_signals
        clause, params = _since_clause(True)
        rows = conn.execute(
            f"""SELECT id, ts, asset, signal_kind, direction, hint, status, created_at
                FROM abnormal_signals WHERE asset = ? AND {clause}
                ORDER BY ts DESC LIMIT ?""",
            (asset_u, *params, limit_per_kind),
        ).fetchall()
        out["abnormal_signals"] = [dict(r) for r in rows]

        # backtest_event_outcomes
        clause, params = _since_clause(True)
        rows = conn.execute(
            f"""SELECT id, type_code, asset, hit, pnl_pct, pnl_bps, direction,
                       window_label, sample_ts, attributes
                FROM backtest_event_outcomes
                WHERE asset = ? AND {clause}
                ORDER BY sample_ts DESC LIMIT ?""",
            (asset_u, *params, limit_per_kind),
        ).fetchall()
        for r in rows:
            d = dict(r)
            d["attributes"] = _safe_json(d.get("attributes"), {})
            out["backtest_outcomes"].append(d)

        # quick_sim_trades
        clause, params = _since_clause(True)
        rows = conn.execute(
            f"""SELECT id, asset, action, score, pnl_usdt, pnl_pct, entry_ts, exit_ts,
                       horizon_minutes, settled
                FROM quick_sim_trades WHERE asset = ? AND {clause}
                ORDER BY entry_ts DESC LIMIT ?""",
            (asset_u, *params, limit_per_kind),
        ).fetchall()
        out["trades"] = [dict(r) for r in rows]

        # kline_snapshots（始终最新一帧）
        rows = conn.execute(
            """SELECT symbol, timeframe, close, pos_pct_7d, range_high_7d, range_low_7d,
                      atr_pct, trend, status, payload, ts
               FROM kline_snapshots WHERE symbol = ? AND status = 'ok'
               ORDER BY symbol, timeframe""",
            (asset_u,),
        ).fetchall()
        for r in rows:
            d = dict(r)
            d["payload"] = _safe_json(d.get("payload"), {})
            out["kline_snapshots"].append(d)

        # factor_snapshots
        clause, params = _since_clause(True)
        rows = conn.execute(
            f"""SELECT asset, factors, ts, news_id FROM factor_snapshots
                WHERE asset = ? AND {clause}
                ORDER BY ts DESC LIMIT ?""",
            (asset_u, *params, limit_per_kind),
        ).fetchall()
        for r in rows:
            d = dict(r)
            d["factors"] = _safe_json(d.get("factors"), [])
            out["factor_snapshots"].append(d)

        # decision_reflection / structured_reflections / trade_reflections
        clause, params = _since_clause(False)
        rows = conn.execute(
            f"""SELECT dr.id, dr.decision_id, dr.kind, dr.summary, dr.detail,
                       dr.severity, dr.created_at, ad.target_asset
                FROM decision_reflection dr
                LEFT JOIN ai_decisions ad ON ad.id = dr.decision_id
                WHERE (ad.target_asset = ? OR ad.target_asset LIKE ?)
                  AND {clause}
                ORDER BY dr.created_at DESC LIMIT ?""",
            (asset_u, asset_like, *params, limit_per_kind),
        ).fetchall()
        out["reflections"] = [dict(r) for r in rows]

        rows = conn.execute(
            f"""SELECT sr.id, sr.decision_id, sr.category, sr.pattern, sr.outcome,
                       sr.confidence, sr.created_at, ad.target_asset
                FROM structured_reflections sr
                LEFT JOIN ai_decisions ad ON ad.id = sr.decision_id
                WHERE (ad.target_asset = ? OR ad.target_asset LIKE ?)
                  AND {clause}
                ORDER BY sr.created_at DESC LIMIT ?""",
            (asset_u, asset_like, *params, limit_per_kind),
        ).fetchall()
        out["structured_reflections"] = [dict(r) for r in rows]

        rows = conn.execute(
            f"""SELECT tr.id, tr.trade_id, tr.lesson, tr.outcome, tr.asset,
                       tr.created_at, qs.asset AS trade_asset
                FROM trade_reflections tr
                LEFT JOIN quick_sim_trades qs ON qs.id = tr.trade_id
                WHERE (tr.asset = ? OR qs.asset = ?)
                  AND {clause}
                ORDER BY tr.created_at DESC LIMIT ?""",
            (asset_u, asset_u, *params, limit_per_kind),
        ).fetchall()
        out["trade_reflections"] = [dict(r) for r in rows]

    except sqlite3.OperationalError:
        pass  # 任意表/列缺失 → 已写入的 key 保留，其他 []
    finally:
        _close(own, conn)
    return out


# ── 2. 维度检索（get_dim） ────────────────────────────────────────────────
def get_dim(dim_code: str, asset: Optional[str] = None,
            since: str = "7d", limit: int = 20,
            conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """按反思维度检索（dim_code ∈ {data, logic, causal, rules, market}）。

    返回该维度最近 N 条事实（混合结构），每条带 _dim 字段标识归属维度。
    """
    if dim_code not in ALL_DIMS:
        return []
    own = conn is None
    conn = conn or _connect()
    since_ts = parse_since(since)
    asset_u = asset.upper() if asset else None
    asset_like = f"%{asset_u}%" if asset_u else None
    out: List[Dict[str, Any]] = []

    ERR_BY_DIM = {
        DIM_DATA:   ["E001", "E002"],
        DIM_LOGIC:  ["E003", "E006", "E008", "E009", "E013"],
        DIM_CAUSAL: ["E004", "E012"],
        DIM_RULES:  ["E005"],
        DIM_MARKET: ["E007", "E010", "E011"],
    }

    try:
        # 1) decision_errors（按 dim_code 映射的 error_codes）
        codes = ERR_BY_DIM[dim_code]
        if codes:
            placeholders = ",".join("?" * len(codes))
            params: List[Any] = list(codes)
            sql = f"""SELECT de.decision_id, de.error_code, de.severity, de.detail,
                             de.evidence_refs, de.origin, de.source, de.created_at,
                             ad.target_asset
                      FROM decision_errors de
                      LEFT JOIN ai_decisions ad ON ad.id = de.decision_id
                      WHERE de.error_code IN ({placeholders})"""
            if asset_u:
                sql += " AND (ad.target_asset = ? OR ad.target_asset LIKE ?)"
                params.extend([asset_u, asset_like])
            if since_ts > 0:
                sql += " AND de.created_at >= datetime(?, 'unixepoch')"
                params.append(since_ts)
            sql += " ORDER BY de.severity DESC, de.created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                d = dict(r)
                d["evidence_refs"] = _safe_json(d.get("evidence_refs"), {})
                d["_dim"] = dim_code
                d["_kind"] = "decision_error"
                out.append(d)

        # 2) causal 维度 → causal_chains
        if dim_code == DIM_CAUSAL:
            sub = limit // 2 or 1
            rows = conn.execute(
                """SELECT id, name, cause, effect, weight, mechanism, precondition,
                          invalidation, origin
                   FROM causal_chains WHERE enabled = 1 ORDER BY weight DESC LIMIT ?""",
                (sub,),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["_dim"] = DIM_CAUSAL
                d["_kind"] = "causal_chain"
                out.append(d)

        # 3) market 维度 → abnormal_signals + factor_snapshots
        if dim_code == DIM_MARKET:
            sub = max(2, limit // 3)
            params = [since_ts] if since_ts > 0 else []
            sql = """SELECT id, ts, asset, signal_kind, direction, hint, status, created_at
                     FROM abnormal_signals WHERE 1=1"""
            if since_ts > 0:
                sql += " AND ts >= ?"
            if asset_u:
                sql += " AND asset = ?"
                params.append(asset_u)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(sub)
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                d = dict(r)
                d["_dim"] = DIM_MARKET
                d["_kind"] = "abnormal_signal"
                out.append(d)

            sub = max(2, limit // 3)
            params = [since_ts] if since_ts > 0 else []
            sql = "SELECT asset, factors, ts, news_id FROM factor_snapshots WHERE 1=1"
            if since_ts > 0:
                sql += " AND ts >= ?"
            if asset_u:
                sql += " AND asset = ?"
                params.append(asset_u)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(sub)
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                d = dict(r)
                d["factors"] = _safe_json(d.get("factors"), [])
                d["_dim"] = DIM_MARKET
                d["_kind"] = "factor_snapshot"
                out.append(d)

    except sqlite3.OperationalError:
        pass
    finally:
        _close(own, conn)
    return out


# ── 3. 跨维度搜索（search） ───────────────────────────────────────────────
def search(query: str, dims: Optional[Iterable[str]] = None,
           asset: Optional[str] = None, since: str = "7d",
           limit: int = 20,
           conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """跨维度文本搜索（LIKE 匹配）。

    dims 不指定 → 搜所有维度；query 必须非空。
    返回混合结构，每条带 _kind/_dim 字段。
    """
    q = (query or "").strip()
    if not q:
        return []
    own = conn is None
    conn = conn or _connect()
    since_ts = parse_since(since)
    asset_u = asset.upper() if asset else None
    pat = f"%{q}%"
    selected: List[str] = [d for d in (dims or list(ALL_DIMS)) if d in ALL_DIMS]
    if not selected:
        selected = list(ALL_DIMS)
    out: List[Dict[str, Any]] = []

    DIM_ERR = {
        DIM_DATA:   ["E001", "E002"],
        DIM_LOGIC:  ["E003", "E006", "E008", "E009", "E013"],
        DIM_CAUSAL: ["E004", "E012"],
        DIM_RULES:  ["E005"],
    }

    try:
        # 1) decision_errors.detail
        relevant_codes: List[str] = []
        for d in selected:
            if d in DIM_ERR:
                relevant_codes.extend(DIM_ERR[d])
        if relevant_codes:
            placeholders = ",".join("?" * len(relevant_codes))
            params: List[Any] = list(relevant_codes)
            sql = f"""SELECT de.decision_id, de.error_code, de.severity, de.detail,
                             de.origin, de.created_at, ad.target_asset
                      FROM decision_errors de
                      LEFT JOIN ai_decisions ad ON ad.id = de.decision_id
                      WHERE de.error_code IN ({placeholders}) AND de.detail LIKE ?"""
            params.append(pat)
            if asset_u:
                sql += " AND (ad.target_asset = ? OR ad.target_asset LIKE ?)"
                params.extend([asset_u, f"%{asset_u}%"])
            if since_ts > 0:
                sql += " AND de.created_at >= datetime(?, 'unixepoch')"
                params.append(since_ts)
            sql += " ORDER BY de.severity DESC, de.created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                d = dict(r)
                d["_kind"] = "decision_error"
                d["_dim"] = ERRCODE_TO_DIM.get(d.get("error_code", ""), DIM_LOGIC)
                out.append(d)

        # 2) causal_chains
        if DIM_CAUSAL in selected:
            sql = """SELECT id, name, cause, effect, weight, mechanism, precondition,
                            invalidation
                     FROM causal_chains
                     WHERE enabled = 1
                       AND (cause LIKE ? OR effect LIKE ?
                            OR mechanism LIKE ? OR name LIKE ?)"""
            params = [pat, pat, pat, pat]
            sql += " ORDER BY weight DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                d = dict(r)
                d["_kind"] = "causal_chain"
                d["_dim"] = DIM_CAUSAL
                out.append(d)

        # 3) review_insights.rule
        sql = """SELECT id, rule, asset, scope, origin, source_samples, created_at
                 FROM review_insights
                 WHERE active = 1 AND manual_deactivated = 0
                   AND rule LIKE ?
                   AND (asset = '' OR asset = 'all'"""
        params2: List[Any] = [pat]
        if asset_u:
            sql += " OR asset = ?"
            params2.append(asset_u)
        sql += ")"
        if since_ts > 0:
            sql += " AND created_at >= datetime(?, 'unixepoch')"
            params2.append(since_ts)
        sql += " ORDER BY source_samples DESC, created_at DESC LIMIT ?"
        params2.append(limit)
        rows = conn.execute(sql, params2).fetchall()
        for r in rows:
            d = dict(r)
            d["_kind"] = "review_insight"
            d["_dim"] = DIM_RULES
            out.append(d)

        # 4) abnormal_signals
        if DIM_MARKET in selected:
            sql = """SELECT id, ts, asset, signal_kind, direction, hint, status
                     FROM abnormal_signals
                     WHERE (hint LIKE ? OR detail_json LIKE ?)"""
            params3 = [pat, pat]
            if asset_u:
                sql += " AND asset = ?"
                params3.append(asset_u)
            if since_ts > 0:
                sql += " AND ts >= ?"
                params3.append(since_ts)
            sql += " ORDER BY ts DESC LIMIT ?"
            params3.append(limit)
            rows = conn.execute(sql, params3).fetchall()
            for r in rows:
                d = dict(r)
                d["_kind"] = "abnormal_signal"
                d["_dim"] = DIM_MARKET
                out.append(d)

    except sqlite3.OperationalError:
        pass
    finally:
        _close(own, conn)
    return out


# ── 4. 错误维度检索（reflection 包装） ──────────────────────────────────────
def get_recent_errors(asset: Optional[str] = None, code: Optional[str] = None,
                      origin: Optional[str] = None, limit: int = 10,
                      since: str = "7d",
                      conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """包装 reflection_checkers.get_recent_errors + asset 过滤 + since 窗口。

    asset 过滤走 ai_decisions.target_asset 关联（原版不过滤）。
    """
    own = conn is None
    conn = conn or _connect()
    since_ts = parse_since(since)
    asset_u = asset.upper() if asset else None
    try:
        clauses: List[str] = []
        params: List[Any] = []
        if since_ts > 0:
            clauses.append("de.created_at >= datetime(?, 'unixepoch')")
            params.append(since_ts)
        if code:
            clauses.append("de.error_code = ?")
            params.append(code)
        if origin:
            clauses.append("de.origin = ?")
            params.append(origin)
        if asset_u:
            clauses.append("(ad.target_asset = ? OR ad.target_asset LIKE ?)")
            params.extend([asset_u, f"%{asset_u}%"])
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"""SELECT de.id, de.decision_id, de.error_code, de.severity,
                         de.detail, de.evidence_refs, de.origin, de.source, de.created_at,
                         ad.target_asset
                  FROM decision_errors de
                  LEFT JOIN ai_decisions ad ON ad.id = de.decision_id
                  WHERE {where}
                  ORDER BY de.severity DESC, de.created_at DESC LIMIT ?"""
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence_refs"] = _safe_json(d.get("evidence_refs"), {})
            d["_dim"] = ERRCODE_TO_DIM.get(d.get("error_code", ""), DIM_LOGIC)
            out.append(d)
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        _close(own, conn)


# ── 5. 因果链 / 图谱节点 / 异动 / 回测 / 洞察（单维度便捷方法） ──────────
def get_causal_chains(asset: Optional[str] = None, since: str = "all",
                      limit: int = 30,
                      conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """拉因果链；since='all' / 空 → 不过滤时间。"""
    own = conn is None
    conn = conn or _connect()
    try:
        sql = """SELECT id, name, cause, effect, trigger_words, weight, mechanism,
                       precondition, invalidation, origin, updated_at
                FROM causal_chains WHERE enabled = 1"""
        params: List[Any] = []
        if asset:
            pat = f"%{asset.upper()}%"
            sql += " AND (trigger_words LIKE ? OR cause LIKE ? OR effect LIKE ?)"
            params.extend([pat, pat, pat])
        if since and since != "all":
            try:
                cutoff = parse_since(since)
                if cutoff > 0:
                    sql += " AND updated_at >= datetime(?, 'unixepoch')"
                    params.append(cutoff)
            except Exception:
                pass
        sql += " ORDER BY weight DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        _close(own, conn)


def get_asset_driver_graph(asset: str, include_all: bool = True,
                           conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """拉某资产的驱动图谱（nodes + edges）。"""
    own = conn is None
    conn = conn or _connect()
    asset_u = (asset or "").upper()
    try:
        if include_all:
            rows = conn.execute(
                """SELECT id, asset, label, kind, is_core, confidence, evidence, version
                   FROM asset_driver_nodes
                   WHERE (asset = ? OR asset = 'all') AND active = 1
                   ORDER BY is_core DESC, confidence DESC""",
                (asset_u,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, asset, label, kind, is_core, confidence, evidence, version
                   FROM asset_driver_nodes
                   WHERE asset = ? AND active = 1
                   ORDER BY is_core DESC, confidence DESC""",
                (asset_u,),
            ).fetchall()
        nodes = [dict(r) for r in rows]
        edges: List[Dict[str, Any]] = []
        if nodes:
            node_ids = tuple(n["id"] for n in nodes)
            placeholders = ",".join("?" * len(node_ids))
            rows = conn.execute(
                f"""SELECT e.id, e.from_id, e.to_id, e.kind, e.weight, e.evidence,
                           fn.label AS from_label, fn.asset AS from_asset,
                           tn.label AS to_label, tn.asset AS to_asset
                    FROM asset_driver_edges e
                    LEFT JOIN asset_driver_nodes fn ON fn.id = e.from_id
                    LEFT JOIN asset_driver_nodes tn ON tn.id = e.to_id
                    WHERE e.from_id IN ({placeholders}) OR e.to_id IN ({placeholders})
                    ORDER BY e.weight DESC""",
                (*node_ids, *node_ids),
            ).fetchall()
            edges = [dict(r) for r in rows]
        return {"asset": asset_u, "nodes": nodes, "edges": edges}
    except sqlite3.OperationalError:
        return {"asset": asset_u, "nodes": [], "edges": []}
    finally:
        _close(own, conn)


def get_abnormal_signals(asset: Optional[str] = None, since: str = "7d",
                         status: Optional[str] = None, limit: int = 20,
                         conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """拉异动信号。"""
    own = conn is None
    conn = conn or _connect()
    since_ts = parse_since(since)
    clauses: List[str] = []
    params: List[Any] = []
    if since_ts > 0:
        clauses.append("ts >= ?")
        params.append(since_ts)
    if asset:
        clauses.append("asset = ?")
        params.append(asset.upper())
    if status:
        clauses.append("status = ?")
        params.append(status)
    sql = f"""SELECT id, ts, asset, signal_kind, direction, hint, status, created_at
              FROM abnormal_signals WHERE {' AND '.join(clauses) or '1=1'}
              ORDER BY ts DESC LIMIT ?"""
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        _close(own, conn)


def get_backtest_outcomes(asset: Optional[str] = None, type_code: Optional[str] = None,
                          since: str = "30d", limit: int = 50,
                          conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """拉回测事件结果。type_code 如 'CPI' / 'NFP' / 'FOMC'。"""
    own = conn is None
    conn = conn or _connect()
    since_ts = parse_since(since)
    clauses: List[str] = []
    params: List[Any] = []
    if since_ts > 0:
        clauses.append("sample_ts >= ?")
        params.append(since_ts)
    if asset:
        clauses.append("asset = ?")
        params.append(asset.upper())
    if type_code:
        clauses.append("type_code = ?")
        params.append(type_code)
    sql = f"""SELECT id, type_code, asset, hit, pnl_pct, pnl_bps, direction,
                     window_label, sample_ts, attributes
              FROM backtest_event_outcomes WHERE {' AND '.join(clauses) or '1=1'}
              ORDER BY sample_ts DESC LIMIT ?"""
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["attributes"] = _safe_json(d.get("attributes"), {})
            out.append(d)
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        _close(own, conn)


def get_review_insights(asset: Optional[str] = None, active_only: bool = True,
                        limit: int = 20,
                        conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """拉复盘洞察（rule 经验）。"""
    own = conn is None
    conn = conn or _connect()
    clauses = ["1=1"]
    params: List[Any] = []
    if active_only:
        clauses.append("active = 1 AND manual_deactivated = 0")
    if asset:
        clauses.append("(asset = ? OR asset = '' OR asset = 'all')")
        params.append(asset.upper())
    sql = f"""SELECT id, rule, asset, scope, origin, kind, source_samples,
                     counter_factual, created_at
              FROM review_insights WHERE {' AND '.join(clauses)}
              ORDER BY source_samples DESC, created_at DESC LIMIT ?"""
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        _close(own, conn)

# ── 6. reflection_checkers 接入用 ─────────────────────────────────────────
def get_context_for_checker(checker_name, decision, conn=None):
    """给 reflection_checkers 提供上下文数据（**不注入 prompt**，纯数据查询）。

    decision 至少包含 target_asset 或 asset。
    返回 {"dim": str|None, "context": [...]}
    """
    asset = (decision.get("target_asset") or decision.get("asset") or "").upper()
    if not asset:
        return {"dim": None, "context": []}
    dim = CHECKER_TO_DIM.get(checker_name)
    if not dim:
        return {"dim": None, "context": []}
    ctx = get_dim(dim, asset=asset, since="7d", limit=10, conn=conn)
    return {"dim": dim, "context": ctx}


# ── 7. 维度统计 / 资产概览（仪表盘用） ────────────────────────────────────
def get_dim_stats(days=7, conn=None):
    """最近 N 天每个 error_code 出现次数（仪表盘热力图）。"""
    own = conn is None
    conn = conn or _connect()
    since_ts = int(time.time()) - max(1, int(days)) * 86400
    try:
        rows = conn.execute(
            """SELECT error_code, COUNT(*) AS cnt FROM decision_errors
               WHERE created_at >= datetime(?, 'unixepoch')
               GROUP BY error_code ORDER BY cnt DESC""",
            (since_ts,),
        ).fetchall()
        return {r["error_code"]: int(r["cnt"]) for r in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        _close(own, conn)


def get_asset_summary(days=7, conn=None):
    """最近 N 天各资产的错误 / 异动 / 回测 / 洞察 计数概览。"""
    own = conn is None
    conn = conn or _connect()
    since_ts = int(time.time()) - max(1, int(days)) * 86400
    out = []
    try:
        rows = conn.execute(
            """SELECT ad.target_asset AS asset,
                      COUNT(DISTINCT de.id) AS error_count,
                      COUNT(DISTINCT ad.id) AS decision_count
               FROM ai_decisions ad
               LEFT JOIN decision_errors de ON de.decision_id = ad.id
                 AND de.created_at >= datetime(?, 'unixepoch')
               WHERE ad.target_asset IS NOT NULL AND ad.target_asset != ''
                 AND ad.created_at >= ?
               GROUP BY ad.target_asset ORDER BY error_count DESC""",
            (since_ts, since_ts),
        ).fetchall()
        for r in rows:
            d = dict(r)
            asset_u = d.get("asset")
            for table, key, span_clause in (
                ("abnormal_signals", "abnormal_count", " AND ts >= ?"),
                ("backtest_event_outcomes", "backtest_count", " AND sample_ts >= ?"),
            ):
                try:
                    cnt = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE asset = ?{span_clause}",
                        (asset_u, since_ts),
                    ).fetchone()
                    d[key] = int(cnt["c"]) if cnt else 0
                except sqlite3.OperationalError:
                    d[key] = 0
            try:
                ins = conn.execute(
                    """SELECT COUNT(*) AS c FROM review_insights
                       WHERE (asset = ? OR asset = '' OR asset = 'all')
                         AND active = 1 AND manual_deactivated = 0""",
                    (asset_u,),
                ).fetchone()
                d["insight_count"] = int(ins["c"]) if ins else 0
            except sqlite3.OperationalError:
                d["insight_count"] = 0
            out.append(d)
    except sqlite3.OperationalError:
        pass
    finally:
        _close(own, conn)
# ── v182.4: 相似历史事件检索（KB similarity）────────────────────────────────
# 给定一条 abnormal_signals.id，找同 asset / 同 direction / 因子重叠的历史事件，
# 用于「上次类似情况」的 RAG 展示；附上对应 abnormal_attributions 摘要。
# 无嵌入模型，纯 SQL + 因子集合 Jaccard 评分；abnormal_attributions 不存在/为空时静默跳过。

def _jaccard_kb(a: List[str], b: List[str]) -> float:
    sa, sb = set(a or []), set(b or [])
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


def find_similar_signals(
    signal_id: int,
    since: str = "30d",
    max_results: int = 5,
    min_factor_overlap: int = 1,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """找与 signal_id 相似的历史异常签名（含归因摘要），按相似度排序。

    相似度评分（0-1）：
      - 0.6: asset 完全相同
      - 0.3: direction 完全相同
      - 0.1 * 因子 Jaccard 相似度

    Returns:
      {current, since_ts, results:[...], summary, fallback}
      表缺失/当前信号不存在 → {"error": str} 或空 results。
    """
    own = conn is None
    conn = conn or _connect()
    out: Dict[str, Any] = {
        "current": None,
        "since_ts": 0,
        "results": [],
        "summary": "",
        "fallback": False,
    }
    try:
        since_ts = parse_since(since)
        out["since_ts"] = int(since_ts)
        cur = conn.execute(
            "SELECT id, ts, asset, signal_kind, direction, detail_json, hint FROM abnormal_signals WHERE id = ?",
            (int(signal_id),),
        ).fetchone()
        if not cur:
            out["error"] = f"signal_id {signal_id} not found"
            return out
        cur_asset = (cur[2] or "").upper()
        cur_dir = (cur[4] or "")
        try:
            cur_detail = json.loads(cur[5] or "{}")
        except Exception:
            cur_detail = {}
        cur_factors = cur_detail.get("factors") or []
        out["current"] = {
            "signal_id": int(cur[0]),
            "ts": int(cur[1]),
            "asset": cur_asset,
            "direction": cur_dir,
            "factors": cur_factors,
        }

        rows = conn.execute(
            """SELECT id, ts, asset, direction, detail_json, hint
               FROM abnormal_signals
               WHERE id != ? AND asset = ? AND direction = ? AND ts >= ?
                 AND signal_kind IN ('multi_factor','sector')
               ORDER BY ts DESC LIMIT 200""",
            (int(signal_id), cur_asset, cur_dir, int(since_ts)),
        ).fetchall()

        scored: List[Dict[str, Any]] = []
        for r in rows:
            try:
                d = json.loads(r[4] or "{}")
            except Exception:
                d = {}
            f = d.get("factors") or []
            overlap = len(set(cur_factors) & set(f))
            if overlap < min_factor_overlap:
                continue
            score = 0.6 + 0.3 + 0.1 * _jaccard_kb(cur_factors, f)
            delta_h = (cur[1] - int(r[1])) / 3600.0
            scored.append({
                "signal_id": int(r[0]),
                "ts": int(r[1]),
                "asset": (r[2] or "").upper(),
                "direction": r[3] or "",
                "factors": f,
                "similarity_score": round(score, 4),
                "time_delta_h": round(delta_h, 1),
                "hint": r[5] or "",
            })

        scored.sort(key=lambda x: -x["similarity_score"])
        top = scored[:max_results]

        attr_map: Dict[int, Dict[str, Any]] = {}
        try:
            ids = [x["signal_id"] for x in top]
            if ids:
                placeholders = ",".join("?" * len(ids))
                ar = conn.execute(
                    f"""SELECT signal_id, summary, drivers_json, cross_json,
                               actionable, severity_hint
                        FROM abnormal_attributions
                        WHERE signal_id IN ({placeholders})
                        ORDER BY id DESC""",
                    ids,
                ).fetchall()
                seen = set()
                for row in ar:
                    if row[0] in seen:
                        continue
                    seen.add(row[0])
                    try:
                        drivers = json.loads(row[2] or "[]")
                    except Exception:
                        drivers = []
                    try:
                        cross = json.loads(row[3] or "[]")
                    except Exception:
                        cross = []
                    attr_map[row[0]] = {
                        "summary": row[1] or "",
                        "drivers": drivers,
                        "cross_asset_likely": cross,
                        "actionable": row[4] or "观望",
                        "severity_hint": int(row[5] or 1),
                    }
        except sqlite3.OperationalError:
            out["fallback"] = True

        for x in top:
            x["attribution"] = attr_map.get(x["signal_id"])
        out["results"] = top

        n = len(top)
        if n == 0:
            out["summary"] = (
                f"近 30d {cur_asset} 同方向无相似历史事件"
                f"（因子重叠 < {min_factor_overlap}）"
            )
        else:
            sev_counts: Dict[int, int] = {}
            for x in top:
                sev = (x.get("attribution") or {}).get("severity_hint", 0)
                sev_counts[sev] = sev_counts.get(sev, 0) + 1
            if sev_counts:
                sev_str = ", ".join(f"sev{s}×{c}" for s, c in sorted(sev_counts.items()))
                out["summary"] = (
                    f"找到 {n} 条历史相似事件（{cur_asset} 同 {cur_dir}）：{sev_str}"
                )
            else:
                out["summary"] = (
                    f"找到 {n} 条历史相似事件（{cur_asset} 同 {cur_dir}，暂无归因）"
                )
        return out
    except sqlite3.OperationalError as exc:
        out["error"] = f"kb similar OperationalError: {exc}"
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        _close(own, conn)
    return out
