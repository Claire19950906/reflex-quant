
# -*- coding: utf-8 -*-
"""Reflection system - LLM BlindspotReflector.

Single LLM Agent: same raw data as program checkers, find blindspots.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional

import db
import reflection_checkers as rc


BLINDSPOT_PROMPT = """You are a reflection auditor. Find blindspots program checkers MISS.

[v202+ META-REFLECTION RULE - 反思系统自己的盲区也要反思]
Before looking at this decision, ask:
  1. Is the trade actually a "valid alpha sample"? (gate_passed, evidence_action≠HOLD, pnl≠0, allowlist, confidence≥20)
  2. Did the program checkers check sample integrity (E020)? If E020 triggered, this trade is NOT a valid alpha evaluation sample — flag it.
  3. Are we (the reflection system) auditing this trade blindly without first auditing the trade's data integrity?
If any answer is "yes, we are biased", include a blindspot with:
  error_code: "META_REFLECTION"
  category: "meta"
  severity: 2
  description: "反思系统自己没审计样本质量就下结论"
  evidence: "E020 fired / sample audit failed / etc."

[Input 1: Decision]

[Input 1: Decision]
{decision_block}

[Input 2: Raw Data (same as program, analyze INDEPENDENTLY)]
News: {news_text}
Asset: {target_asset} ({asset_category})
Market: {market_snapshot}
Causal graph: {causal_context}
Active rules: {active_rules}

[Input 3: Program checkers found (reference factor)]
{checker_errors}

[Task]
Independently analyze. Find blindspots program checkers could NOT cover.

[Output strict JSON]
{{
  "blindspots": [
    {{
      "error_code": "E011/E012/E013/new",
      "category": "fact/window/assumption/bias/missing_step",
      "severity": 1|2|3,
      "description": "one-sentence blindspot",
      "evidence": "specific evidence",
      "suggested_rule": "if stable, abstract to rule",
      "applicable_assets": "comma-separated"
    }}
  ],
  "confidence": 0.0-1.0,
  "summary": "overall severity"
}}

Output JSON only. If no blindspots, output {{"blindspots": []}}.
"""


def _fetch_news_text(news_id, conn):
    if not news_id:
        return ""
    try:
        row = conn.execute(
            "SELECT title, summary FROM raw_news WHERE id = ?", (news_id,)
        ).fetchone()
        if row:
            return "Title: " + str(row[0] or "") + "\nSummary: " + str(row[1] or "")
    except sqlite3.OperationalError:
        pass
    return ""


def _fetch_market_snapshot(target_asset, conn):
    if not target_asset:
        return "no asset"
    try:
        row = conn.execute(
            """SELECT ts, close, volume FROM market_ticks
               WHERE symbol = ? ORDER BY ts DESC LIMIT 1""",
            (target_asset,),
        ).fetchone()
        if row:
            return "Latest: ts=" + str(row[0]) + ", close=" + str(row[1])
    except sqlite3.OperationalError:
        pass
    return "no market data"


def _fetch_causal_context(target_asset, conn):
    if not target_asset:
        return "no graph"
    try:
        rows = conn.execute(
            """SELECT label, category, weight FROM asset_driver_nodes
               WHERE asset = ? AND active = 1 ORDER BY weight DESC LIMIT 10""",
            (target_asset,),
        ).fetchall()
        if not rows:
            return "graph empty"
        return "; ".join(r[0] + "(" + str(r[1]) + ",w=" + str(round(r[2], 2)) + ")" for r in rows)
    except sqlite3.OperationalError:
        return "graph table not ready"


def _fetch_active_rules(conn, limit=5):
    try:
        rows = conn.execute(
            """SELECT rule FROM review_insights
               WHERE active = 1 AND kind = 0
               ORDER BY source_samples DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return "\n".join("- " + r[0] for r in rows) if rows else "no hard rules"
    except sqlite3.OperationalError:
        return "rules table not ready"


def _format_checker_errors(errors):
    if not errors:
        return "(program checkers found no errors)"
    lines = []
    for e in errors:
        sev = {1: "INFO", 2: "WARN", 3: "CRIT"}.get(int(e.get("severity", 2)), "WARN")
        lines.append("- [" + str(e.get("error_code", "")) + "/" + sev + "/" + str(e.get("source", "")) + "] " + str(e.get("detail", "")))
    return "\n".join(lines)


def _format_decision_block(decision):
    fields = [
        ("action", decision.get("suggested_action")),
        ("sentiment_score", decision.get("sentiment_score")),
        ("reasoning", decision.get("reasoning")),
        ("asset_nature", decision.get("asset_nature")),
        ("factor_ranking", decision.get("factor_ranking")),
        ("verification", decision.get("verification")),
        ("hard_data_evidence", decision.get("hard_data_evidence")),
        ("data_anchor_ts", decision.get("data_anchor_ts")),
    ]
    return "\n".join(k + ": " + str(v) for k, v in fields)


def _parse_llm_json(raw):
    if not raw:
        return {}
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        cleaned = re.sub(r"[\x00-\x1f]", " ", m.group(0))
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}


def _llm_call(prompt):
    # v187：走 reflection 通道 (盲区反思专用 key) — 优先 ai_worker.chat
    try:
        from ai_worker import chat as _chat
        out = _chat(prompt, purpose="reflection", call_site="blindspot_reflector",
                    max_tokens=1500, temperature=0.3)
        if out:
            return out
    except Exception:
        pass
    # 兜底：api_server.call_llm（保留向后兼容,旧路径可能仍被部分代码引用）
    try:
        from api_server import call_llm as _call
        return _call(prompt)
    except Exception:
        return ""


def _synthesize_tournament_json(tournament_result, prompt: str) -> str:
    """把 TournamentResult 序列化成与单次 LLM 同结构的 JSON 字符串.

    输出形态与单次 LLM 一致 (含 blindspots 列表), 以便下游 _parse_llm_json 无缝消费.
    额外字段以 _tournament_ 前缀写入 evidence_refs, 不污染现有解析逻辑.
    """
    blindspots = []
    if tournament_result.confirmed_findings:
        for cf in tournament_result.confirmed_findings:
            blindspots.append({
                "error_code": cf.error_code,
                "category": cf.category,
                "severity": cf.severity,
                "description": cf.description,
                "evidence": cf.evidence if isinstance(cf.evidence, str) else str(cf.evidence or ""),
                "confidence": round(cf.avg_confidence, 4),
                "_tournament_quorum": cf.quorum,
                "_tournament_verifier_ids": list(cf.verifier_ids),
            })
    elif tournament_result.raw_findings:
        # 没达 quorum → 仍保存 raw 但加标记, 方便审计
        for f in tournament_result.raw_findings:
            blindspots.append({
                "error_code": f.error_code,
                "category": f.category,
                "severity": f.severity,
                "description": f.description,
                "evidence": f.evidence if isinstance(f.evidence, str) else str(f.evidence or ""),
                "confidence": round(float(f.confidence), 4),
                "_tournament_no_consensus": True,
                "_tournament_verifier_ids": [f.verifier_id] if f.verifier_id else [],
            })
    import json as _json
    return _json.dumps({"blindspots": blindspots, "_source": "tournament"}, ensure_ascii=False)


def _llm_call_tournament(prompt):
    """v240+: TOURNAMENT_ENABLED 时走 k=3 verifier tournament.

    - LLM 后端在模块加载时已注入到 reflection_tournament (见模块底部 _install_llm_backend)
    - 输出合成 JSON, 与单次 LLM 输出同结构 → 下游 _parse_llm_json 无需改动
    - 失败兜底: Tournament 本身抛异常时回落到单次 _llm_call (verifier 失败不触发)
    """
    try:
        import config as _cfg
        if not getattr(_cfg, "TOURNAMENT_ENABLED", False):
            return _llm_call(prompt)
    except Exception:
        return _llm_call(prompt)

    try:
        import reflection_tournament as _rt
        import tournament_metrics as _tm

        # v240+ circuit breaker: 窗口内全失败率超阈值 → 短路回单次 LLM
        if _tm._should_short_circuit():
            return _llm_call(prompt)

        quorum = int(getattr(_cfg, "TOURNAMENT_QUORUM", 2))
        timeout = float(getattr(_cfg, "TOURNAMENT_TIMEOUT", 60.0))
        parallel = bool(getattr(_cfg, "TOURNAMENT_PARALLEL", True))

        cfg = _rt.TournamentConfig(
            verifiers=_rt.default_verifiers(),
            quorum=quorum,
            timeout_seconds=timeout,
            parallel=parallel,
        )
        result = _rt.Tournament(cfg).run(prompt)
        # v240+ 观测: 记录 metrics + 更新 circuit breaker 窗口
        _tm._record_metrics(result)
        _tm._record_tournament_outcome(result.votes)
        return _synthesize_tournament_json(result, prompt)
    except Exception as e:  # noqa: BLE001
        try:
            import logging
            logging.warning("tournament failed, falling back to single LLM: %s", e)
        except Exception:
            pass
        return _llm_call(prompt)


# ═══════════════════════════════════════════════════════════════════════════
# v240+: 把 _llm_call 注入到 reflection_tournament (一次性, 模块加载时)
# 避免 _llm_call_tournament 反复覆盖 → 测试可干净 mock set_llm_backend
# ═══════════════════════════════════════════════════════════════════════════
try:
    import reflection_tournament as _rt_mod
    _rt_mod.set_llm_backend(_llm_call)
except Exception:
    pass



def reflect_blindspots(decision, conn=None):
    own = conn is None
    conn = conn or rc._connect()
    try:
        decision_id = decision.get("id")
        news_id = decision.get("news_id")
        target_asset = decision.get("target_asset") or ""

        news_text = _fetch_news_text(news_id, conn)
        market_snap = _fetch_market_snapshot(target_asset, conn)
        causal_ctx = _fetch_causal_context(target_asset, conn)
        active_rules = _fetch_active_rules(conn)
        checker_errs = rc.get_errors_for_decision(decision_id, conn=conn)
        checker_errs_text = _format_checker_errors(checker_errs)
        decision_block = _format_decision_block(decision)

        prompt = BLINDSPOT_PROMPT.format(
            decision_block=decision_block,
            news_text=news_text or "no news text",
            target_asset=target_asset or "unspecified",
            asset_category=decision.get("market_category") or "OTHER",
            market_snapshot=market_snap,
            causal_context=causal_ctx,
            active_rules=active_rules,
            checker_errors=checker_errs_text,
        )

        raw = _llm_call_tournament(prompt)
        parsed = _parse_llm_json(raw)
        blindspots = parsed.get("blindspots") or []
        if not isinstance(blindspots, list):
            return []

        out = []
        for bs in blindspots:
            if not isinstance(bs, dict):
                continue
            err_code = str(bs.get("error_code") or "").strip()
            if not err_code:
                continue
            severity = int(bs.get("severity") or 2)
            severity = max(1, min(3, severity))
            out.append({
                "decision_id": decision_id,
                "error_code": err_code,
                "origin": "llm",
                "source": "blindspot",
                "severity": severity,
                "detail": str(bs.get("description") or "")[:500],
                "evidence_refs": {
                    "category": bs.get("category"),
                    "evidence": str(bs.get("evidence") or "")[:300],
                    "suggested_rule": str(bs.get("suggested_rule") or "")[:300],
                    "applicable_assets": bs.get("applicable_assets", ""),
                },
            })
        return out
    except Exception:
        return []
    finally:
        if own:
            conn.close()


def save_blindspot_insight(blindspot, conn=None):
    """Persist LLM-found blindspot as review_insights (active=0, pending approval)."""
    own = conn is None
    conn = conn or rc._connect()
    try:
        ev = blindspot.get("evidence_refs") or {}
        suggested = str(ev.get("suggested_rule") or "").strip()
        if not suggested:
            return None
        rule_text = suggested[:400]
        scope = str(ev.get("category") or "all")
        asset = str(ev.get("applicable_assets") or "")[:50]

        cur = conn.execute(
            """INSERT INTO review_insights
               (rule, asset, scope, active, source_samples, origin, kind, counter_factual, created_at)
               VALUES (?, ?, ?, 0, 1, 'real', 1, '', datetime('now'))""",
            (rule_text, asset, scope),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.OperationalError:
        return None
    finally:
        if own:
            conn.close()


def reflect_and_persist(decision, conn=None, auto_promote=False):
    blindspots = reflect_blindspots(decision, conn=conn)
    inserted = 0
    insight_ids = []
    if blindspots:
        inserted = rc.save_errors(blindspots, conn=conn)
        if auto_promote:
            for bs in blindspots:
                iid = save_blindspot_insight(bs, conn=conn)
                if iid:
                    insight_ids.append(iid)
    return {
        "decision_id": decision.get("id"),
        "blindspots_found": len(blindspots),
        "decision_errors_inserted": inserted,
        "insight_ids_created": insight_ids,
    }
