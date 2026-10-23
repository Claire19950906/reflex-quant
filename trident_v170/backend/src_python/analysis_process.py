"""v203+: 分析过程聚合查询 —— 把一条新闻从入库到经验沉淀的完整生命周期捞出来。

设计原则
---------
1. 纯聚合查询:不写任何数据,只把已有 8 张表拼成一个 dict 返回。
2. 不调外部 API:不调 LLM,不调别的服务。查询 SQLite + Python 拼装。
3. 字段全保留:每张表的字段尽量全返回,空字段允许,不删任何信息。
4. 失败容错:任意单表查询失败不抛错,该段返回空 list / null。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

import db as _db


def _conn() -> sqlite3.Connection:
    c = _db.get_connection()
    c.row_factory = sqlite3.Row
    return c


def _row(r):
    if r is None:
        return None
    return {k: r[k] for k in r.keys()}


def _rows(rs):
    return [{k: r[k] for k in r.keys()} for r in rs]


def _safe_json(s):
    if s is None:
        return None
    if isinstance(s, (dict, list)):
        return s
    if not isinstance(s, str):
        return s
    s = s.strip()
    if not s or s[0] not in "{[":
        return s
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


def _truncate(s, n=200):
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return s[:n] + "..." if len(s) > n else s


def _calc_news_age_hours(rn) -> Optional[float]:
    """v181: 算新闻「真实发布 → 入库」的小时数；用于 12h 过期门可视化。

    优先级:published_at > timestamp;
    入参可接受 epoch(秒/毫秒)、ISO 字符串、'YYYY-MM-DD HH:MM:SS' 字符串。
    返回 None 表示无法计算。
    """
    if rn is None:
        return None
    import time as _t
    from datetime import datetime as _dt

    def _parse(v):
        if v is None or v == 0 or v == "":
            return None
        if isinstance(v, (int, float)):
            f = float(v)
            if f > 1e12:
                f = f / 1000.0
            return f
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            # ISO 8601 (含 T)
            try:
                if "T" in s or "+" in s[10:] or "-" in s[10:]:
                    dt = _dt.fromisoformat(s.replace("Z", "+00:00"))
                    return dt.timestamp()
            except (ValueError, TypeError):
                pass
            # 'YYYY-MM-DD HH:MM:SS'
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
                try:
                    dt = _dt.strptime(s, fmt)
                    return dt.timestamp()
                except ValueError:
                    continue
            # 纯数字字符串
            try:
                f = float(s)
                if f > 1e12:
                    f = f / 1000.0
                return f
            except ValueError:
                return None
        return None

    pub_ts = _parse(rn["published_at"] if "published_at" in rn.keys() else None)
    if pub_ts is None:
        pub_ts = _parse(rn["timestamp"] if "timestamp" in rn.keys() else None)
    if pub_ts is None:
        return None
    now = _t.time()
    if pub_ts > now + 86400 * 30:
        return None
    age_s = max(0.0, now - pub_ts)
    return round(age_s / 3600.0, 2)


def list_recent(limit: int = 20, only_with_decisions: bool = False) -> List[Dict[str, Any]]:
    """最近 N 条已分析新闻 + 关键摘要(给左栏列表用)。

    Args:
        limit: 最多返回多少条
        only_with_decisions: True 只列出至少有 1 个 ai_decision 的新闻
    """
    where_clause = ("WHERE EXISTS (SELECT 1 FROM ai_decisions ad "
                   "WHERE ad.news_id = rn.id)") if only_with_decisions else ""
    sql = (
        "SELECT id AS news_id, timestamp AS ts, source AS source, content AS content, "
        "status AS status, gate_main_asset AS main_asset, gate_kind AS gate_kind, "
        "gate_strength AS gate_strength, gate_verdict AS gate_verdict, "
        "gate_impact_1to100 AS gate_impact_1to100, "
        "published_at AS published_at "
        f"FROM raw_news rn {where_clause} "
        "ORDER BY id DESC LIMIT ?"
    )
    out: List[Dict[str, Any]] = []
    try:
        with _conn() as c:
            for r in c.execute(sql, (limit,)).fetchall():
                stats = c.execute(
                    "SELECT COUNT(*) AS n, "
                    "SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS n_settled, "
                    "SUM(CASE WHEN settled=1 AND forward_pnl > 0 THEN 1 ELSE 0 END) AS n_win, "
                    "MAX(id) AS latest_id "
                    "FROM ai_decisions WHERE news_id = ?",
                    (r["news_id"],),
                ).fetchone()
                latest = None
                if stats and stats["latest_id"]:
                    latest = c.execute(
                        "SELECT id AS decision_id, suggested_action, target_asset, "
                        "ROUND(forward_pnl, 4) AS pnl, is_correct, settled, "
                        "entry_price, max_price, min_price, exit_price, "
                        "trade_gate_reason, no_trade_reason, market_category, "
                        "entry_time, max_price_time, min_price_time "
                        "FROM ai_decisions WHERE id = ?",
                        (stats["latest_id"],),
                    ).fetchone()
                out.append({
                    "news_id": r["news_id"],
                    "ts": r["ts"],
                    "source": r["source"],
                    "status": r["status"],
                    "content_preview": _truncate(r["content"], 140),
                    "gate_main_asset": r["main_asset"],
                    "gate_kind": r["gate_kind"],
                    "gate_strength": r["gate_strength"],
                    "gate_verdict": r["gate_verdict"],
                    "gate_impact_1to100": r["gate_impact_1to100"],
                    "news_age_hours": _calc_news_age_hours(r),
                    "published_at": r["published_at"],
                    "decisions_total": stats["n"] if stats else 0,
                    "decisions_settled": stats["n_settled"] if stats else 0,
                    "decisions_win": stats["n_win"] if stats else 0,
                    "latest_decision_id": latest["decision_id"] if latest else None,
                    "latest_action": latest["suggested_action"] if latest else None,
                    "latest_asset": latest["target_asset"] if latest else None,
                    "latest_pnl": latest["pnl"] if latest else None,
                    "latest_verdict": latest["is_correct"] if latest else None,
                    "latest_settled": bool(latest["settled"]) if latest else False,
                    "latest_market_category": latest["market_category"] if latest else None,
                    "latest_entry_price": latest["entry_price"] if latest else None,
                    "latest_exit_price": latest["exit_price"] if latest else None,
                    "latest_max_price": latest["max_price"] if latest else None,
                    "latest_min_price": latest["min_price"] if latest else None,
                    "latest_entry_time": latest["entry_time"] if latest else None,
                    "latest_trade_gate_reason": latest["trade_gate_reason"] if latest else None,
                    "latest_no_trade_reason": latest["no_trade_reason"] if latest else None,
                })
    except Exception as exc:
        print(f"[analysis_process.list_recent] failed: {type(exc).__name__}: {exc}",
              flush=True)
    return out


def get_lifecycle(news_id: int) -> Dict[str, Any]:
    """聚合 8 张表,返回一条新闻从入库到经验沉淀的完整 dict。"""
    out: Dict[str, Any] = {
        "news_id": news_id,
        "news": None, "gate": None,
        "decisions": [], "primary_decision_id": None,
        "reasoning": None, "llm_call_meta": None,
        "quick_sim": [], "paper_run": [],
        "trade_reflections": [], "structured_reflections": [],
        "attribution_reviews": [], "news_trade_review": None,
        "review_insights": [], "experience_entries": [],
        "causal_chain_links": [],
    }
    try:
        with _conn() as c:
            rn = c.execute("SELECT * FROM raw_news WHERE id = ?", (news_id,)).fetchone()
            if rn is None:
                return out
            out["news"] = _row(rn)
            out["gate"] = {
                "main_asset": rn["gate_main_asset"],
                "kind": rn["gate_kind"],
                "timing": rn["gate_timing"],
                "strength": rn["gate_strength"],
                "verdict": rn["gate_verdict"],
                "core_impact": rn["gate_core_impact"],
                "assets": _safe_json(rn["gate_assets"]),
                "is_material": rn["gate_is_material"],
                # v181: 1-100 影响力打分（<20 一律排除）
                "impact_1to100": rn["gate_impact_1to100"] if "gate_impact_1to100" in rn.keys() else None,
                # v181: 新闻时效（published_at / ingested_ts 距 now 秒数）
                "news_age_hours": _calc_news_age_hours(rn),
            }
            ads = c.execute(
                "SELECT * FROM ai_decisions WHERE news_id = ? ORDER BY id ASC",
                (news_id,),
            ).fetchall()
            decisions = _rows(ads)
            out["decisions"] = decisions
            primary = next((d for d in decisions if not d.get("is_sibling")),
                           decisions[0] if decisions else None)
            out["primary_decision_id"] = primary["id"] if primary else None

            if primary:
                rp = primary.get("reasoning_path") or ""
                rp_segments: List[Dict[str, str]] = []
                for line in rp.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("[") and "]" in line:
                        k, _, v = line[1:].partition("]")
                        rp_segments.append({"key": k.strip(), "value": v.strip()})
                    else:
                        rp_segments.append({"key": "", "value": line})
                factor_raw = primary.get("factor_ranking") or ""
                try:
                    factor_ranking = json.loads(factor_raw) if factor_raw else []
                except (json.JSONDecodeError, TypeError):
                    factor_ranking = factor_raw
                el_raw = primary.get("elasticity_window") or ""
                try:
                    elasticity = json.loads(el_raw) if el_raw else {}
                except (json.JSONDecodeError, TypeError):
                    elasticity = {"raw": el_raw} if el_raw else {}
                ver_raw = primary.get("verification") or ""
                try:
                    verification = json.loads(ver_raw) if ver_raw else {}
                except (json.JSONDecodeError, TypeError):
                    verification = ver_raw

                out["reasoning"] = {
                    "decision_id": primary["id"],
                    "reasoning": primary.get("reasoning") or "",
                    "reasoning_path_raw": rp,
                    "reasoning_segments": rp_segments,
                    "factor_ranking": factor_ranking if isinstance(factor_ranking, list) else [],
                    "elasticity_window": elasticity if isinstance(elasticity, dict) else {},
                    "asset_reasoning": primary.get("asset_reasoning") or "",
                    "verification": verification,
                    "hard_data_evidence": primary.get("hard_data_evidence") or "",
                    "invalidation_condition": primary.get("invalidation_condition") or "",
                    "is_correct": primary.get("is_correct") or "",
                    "forward_pnl": primary.get("forward_pnl"),
                    "expected_horizon": primary.get("expected_horizon") or "",
                }
                out["llm_call_meta"] = {
                    "agent_model_id": primary.get("agent_model_id") or "",
                    "evidence_confidence": primary.get("evidence_confidence"),
                    "evidence_action": primary.get("evidence_action") or "",
                    "market_confirmation": primary.get("market_confirmation") or "",
                    "event_strength": primary.get("event_strength") or "",
                    "impact_magnitude": primary.get("impact_magnitude"),
                    "attention_score": primary.get("attention_score"),
                    "event_phase": primary.get("event_phase") or "",
                    "timeframe_match": primary.get("timeframe_match") or "",
                    "trade_gate_reason": primary.get("trade_gate_reason") or "",
                    "no_trade_reason": primary.get("no_trade_reason") or "",
                }

                decision_ids = [d["id"] for d in decisions]
                if decision_ids:
                    placeholders = ",".join("?" * len(decision_ids))
                    qs_rows = c.execute(
                        f"SELECT * FROM quick_sim_trades WHERE decision_id IN "
                        f"({placeholders}) ORDER BY id DESC",
                        decision_ids,
                    ).fetchall()
                    out["quick_sim"] = _rows(qs_rows)
                    try:
                        paper_rows = c.execute(
                            f"SELECT * FROM paper_trading_run WHERE decision_id IN "
                            f"({placeholders}) ORDER BY id DESC",
                            decision_ids,
                        ).fetchall()
                        out["paper_run"] = _rows(paper_rows)
                    except sqlite3.OperationalError as e:
                        if "no such table" not in str(e):
                            raise
                        out["paper_run"] = []
                    tr_rows = c.execute(
                        f"SELECT * FROM trade_reflections WHERE decision_id IN "
                        f"({placeholders}) ORDER BY reflected_at DESC",
                        decision_ids,
                    ).fetchall()
                    out["trade_reflections"] = _rows(tr_rows)
                    sr_rows = c.execute(
                        f"SELECT * FROM structured_reflections WHERE decision_id IN "
                        f"({placeholders}) ORDER BY id DESC",
                        decision_ids,
                    ).fetchall()
                    out["structured_reflections"] = _rows(sr_rows)

                ar_rows = c.execute(
                    "SELECT * FROM attribution_reviews WHERE ref_id = ? OR trade_id = ? "
                    "ORDER BY id DESC",
                    (news_id, primary["id"]),
                ).fetchall()
                out["attribution_reviews"] = _rows(ar_rows)

            ntr = c.execute(
                "SELECT * FROM news_trade_reviews WHERE news_id = ?", (news_id,)
            ).fetchone()
            if ntr is not None:
                ntr_dict = _row(ntr)
                sm_raw = ntr_dict.get("summary") or ""
                try:
                    ntr_dict["summary_parsed"] = json.loads(sm_raw) if sm_raw else {}
                except (json.JSONDecodeError, TypeError):
                    ntr_dict["summary_parsed"] = {"raw": sm_raw}
                out["news_trade_review"] = ntr_dict

            asset_filter: List[str] = []
            if primary:
                if primary.get("target_asset"):
                    asset_filter.append(primary["target_asset"])
                if primary.get("market_category"):
                    asset_filter.append(primary["market_category"])
            if asset_filter:
                placeholders = ",".join("?" * len(asset_filter))
                ri_rows = c.execute(
                    f"SELECT * FROM review_insights WHERE asset IN ({placeholders}) "
                    "ORDER BY id DESC LIMIT 10",
                    asset_filter,
                ).fetchall()
                out["review_insights"] = _rows(ri_rows)

            try:
                ee_rows = c.execute(
                    "SELECT * FROM experience_entries ORDER BY id DESC LIMIT 20"
                ).fetchall()
                out["experience_entries"] = _rows(ee_rows)
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e):
                    raise
                out["experience_entries"] = []

            try:
                # causal_chains 表无 asset 列,只按 updated_at 倒排拿最近 10 条
                    cc_rows = c.execute(
                        "SELECT * FROM causal_chains "
                        "ORDER BY updated_at DESC LIMIT 10"
                    ).fetchall()
                    out["causal_chain_links"] = _rows(cc_rows)
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e):
                    raise
                out["causal_chain_links"] = []
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[analysis_process.get_lifecycle {news_id}] failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
    return out


def healthcheck() -> Dict[str, Any]:
    return {"ok": True, "module": "analysis_process", "version": "v203"}