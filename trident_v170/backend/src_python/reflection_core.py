#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v232 reflection_core: 6-layer content audit for trade_reflections_v2."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import db


_SEG_ORDER = [
    ("事件识别", ("事件识别", "事件", "news", "新闻")),
    ("因素定位", ("因素定位", "因素", "factor")),
    ("情绪评估", ("情绪评估", "情绪", "sentiment")),
    ("跨资产传导", ("跨资产传导", "跨资产", "cross_asset", "cross-asset", "传导")),
    ("人物权重", ("人物权重", "人物", "actor", "persona")),
    ("时间窗口", ("时间窗口", "窗口", "window")),
    ("驱动力", ("驱动力", "驱动", "driver")),
    ("总结", ("总结", "summary")),
]

_BULL_KEYWORDS = (
    "涨", "多", "long", "up", "buy", "rise", "bull", "bullish",
    "看涨", "上涨", "走高", "拉升", "利好", "强势", "反弹",
    "增加", "流入", "宽松", "降息", "刺激", "复苏",
)
_BEAR_KEYWORDS = (
    "跌", "空", "short", "down", "sell", "fall", "bear", "bearish",
    "看跌", "下跌", "走低", "利空", "弱势", "回调",
    "减少", "流出", "紧缩", "加息", "衰退", "通缩",
)


def _split_segments(reasoning_path: str) -> List[Tuple[str, str]]:
    """Split reasoning_path by [segname] markers."""
    if not reasoning_path:
        return []
    out: List[Tuple[str, str]] = []
    seg_names = [n for n, _ in _SEG_ORDER]
    seg_indices: List[Tuple[int, str]] = []
    for i, ch in enumerate(reasoning_path):
        if ch == "[":
            for j in range(i + 1, min(i + 12, len(reasoning_path))):
                if reasoning_path[j] == "]":
                    head = reasoning_path[i + 1:j]
                    if head in seg_names:
                        seg_indices.append((i, head))
                    break
    for idx, (pos, name) in enumerate(seg_indices):
        start = pos
        end = seg_indices[idx + 1][0] if idx + 1 < len(seg_indices) else len(reasoning_path)
        content = reasoning_path[start:end].strip()
        content = re.sub(r"^\[\S+?\]", "", content).strip()
        if content:
            out.append((name, content))
    if not out and reasoning_path.strip():
        out.append(("事件识别", reasoning_path.strip()))
    return out


def _polarity(text: str) -> float:
    """Text polarity [-1, +1], 0=neutral."""
    if not text:
        return 0.0
    t = text.lower()
    bull = sum(1 for k in _BULL_KEYWORDS if k in t)
    bear = sum(1 for k in _BEAR_KEYWORDS if k in t)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 4)


# N1: chain audit
def audit_chain(reasoning_path: str) -> List[Dict[str, Any]]:
    segs = _split_segments(reasoning_path or "")
    if not segs:
        return [{"seg": "_empty", "claim": "", "polarity": 0.0,
                 "link_verified": False, "conflict_with": [], "evidence_score": 0.0,
                 "note": "reasoning_path empty"}]
    out: List[Dict[str, Any]] = []
    prev_polarity = 0.0
    prev_seg = ""
    for seg_name, content in segs:
        pol = _polarity(content)
        claim_summary = content[:80].strip() if content else ""
        link_verified = True
        conflict_with: List[str] = []
        if prev_seg and prev_polarity != 0.0 and pol != 0.0:
            if prev_polarity * pol < -0.3:
                link_verified = False
                conflict_with.append(prev_seg)
        evidence_score = 0.0
        if content:
            has_num = bool(re.search(r"\d+(?:\.\d+)?%?", content))
            has_specific = bool(re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", content))
            evidence_score = 0.4 if has_specific else 0.0
            if has_num:
                evidence_score += 0.3
            if len(content) >= 60:
                evidence_score += 0.2
            evidence_score = min(1.0, round(evidence_score, 4))
        out.append({
            "seg": seg_name, "claim": claim_summary, "polarity": pol,
            "link_verified": link_verified, "conflict_with": conflict_with,
            "evidence_score": evidence_score,
        })
        prev_polarity = pol
        prev_seg = seg_name
    return out


# N2: counter-evidence audit
_OMITTED_WINDOW_MIN = 30


def audit_counter_evidence(conn, reasoning_path, hard_data_evidence,
                            news_content, decision_ts_ms, target_asset,
                            suggested_action):
    out = {"omitted_count": 0, "omitted_signals_against": [],
           "omitted_weight_total": 0.0, "omission_rate": 0.0,
           "critical_omitted": False, "note": ""}
    if not target_asset or suggested_action not in ("BUY", "SELL"):
        out["note"] = "HOLD skip"
        return out
    cited_blob = ((reasoning_path or "") + " " + (hard_data_evidence or "") +
                  " " + (news_content or "")).lower()
    if not decision_ts_ms or decision_ts_ms <= 0:
        out["note"] = "no_decision_ts"
        return out
    try:
        rows = conn.execute(
            """SELECT n.id, n.title, n.content, n.impact_score, n.sentiment_score
                 FROM raw_news n
                WHERE n.first_seen_ts >= ? AND n.first_seen_ts <= ?
                  AND (LOWER(n.title) LIKE ? OR LOWER(n.content) LIKE ?)
                ORDER BY n.first_seen_ts DESC LIMIT 30""",
            (datetime.fromtimestamp((decision_ts_ms - _OMITTED_WINDOW_MIN * 60 * 1000) / 1000,
                                     tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             datetime.fromtimestamp(decision_ts_ms / 1000,
                                     tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             f"%{target_asset.lower()}%", f"%{target_asset.lower()}%"),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        out["note"] = "no related news"
        return out
    total_checked = len(rows)
    omitted: List[Dict[str, Any]] = []
    weight_total = 0.0
    target_polarity = +1.0 if suggested_action == "BUY" else -1.0
    for r in rows:
        title = str(r[1] or "")
        content = str(r[2] or "")
        try:
            impact = float(r[3] or 0.0)
        except (TypeError, ValueError):
            impact = 0.0
        try:
            sentiment = float(r[4] or 0.0)
        except (TypeError, ValueError):
            sentiment = 0.0
        news_polarity = sentiment if abs(sentiment) > 0.1 else _polarity(title + " " + content[:200])
        if news_polarity == 0:
            continue
        if news_polarity * target_polarity < -0.1:
            t_low = (title + " " + content[:200]).lower()
            cited = False
            for snippet in re.split(r"[，。\s,.\n;]+", cited_blob):
                snippet = snippet.strip()
                if len(snippet) >= 6 and snippet in t_low:
                    cited = True
                    break
            if not cited:
                weight = abs(impact) / 100.0 + abs(news_polarity)
                omitted.append({"news_id": int(r[0]), "title": title[:60],
                                "sentiment": round(news_polarity, 2),
                                "impact": impact, "weight": round(weight, 2)})
                weight_total += weight
    out["omitted_count"] = len(omitted)
    out["omitted_signals_against"] = omitted[:10]
    out["omitted_weight_total"] = round(weight_total, 2)
    out["omission_rate"] = round(len(omitted) / max(total_checked, 1), 2)
    out["critical_omitted"] = bool(len(omitted) >= 3 and weight_total >= 1.5)
    return out


# N3: post-outcome truth score
def audit_truth_score(reflection_row, chain_audit, cross_sync_score,
                       market_confirm, pnl_pct):
    win = pnl_pct > 0.005
    loss = pnl_pct < -0.005
    pnl_correctness = 1.0 if win else (0.0 if loss else 0.5)
    n_breaks = sum(1 for s in chain_audit if not s.get("link_verified", True))
    chain_consistency = max(0.0, 1.0 - n_breaks * 0.25)
    mc_match = 0.5
    if market_confirm in ("yes", "positive"):
        mc_match = 1.0 if win else 0.3
    elif market_confirm in ("no", "negative", "denied"):
        mc_match = 0.3 if win else 1.0
    cross_match = 0.5
    if abs(cross_sync_score) >= 0.3:
        cross_match = 1.0 if (cross_sync_score > 0) == win else 0.3
    truth = (0.4 * pnl_correctness + 0.3 * chain_consistency +
             0.2 * mc_match + 0.1 * cross_match)
    truth = round(max(0.0, min(1.0, truth)), 4)
    return {
        "truth_score": truth, "pnl_correctness": pnl_correctness,
        "chain_consistency": round(chain_consistency, 4),
        "market_confirm_match": mc_match, "cross_asset_match": cross_match,
        "reasoning_wrong_but_win": bool(win and n_breaks >= 2),
        "reasoning_right_but_loss": bool(loss and n_breaks == 0 and chain_consistency >= 0.8),
        "n_chain_breaks": n_breaks,
    }


# N4: cross-decision pattern alerts
_PATTERN_MIN_SAMPLES = 5
_PATTERN_LOSS_RATE = 0.6


def detect_pattern_alerts(conn, target_asset, sub_layer, signal_name,
                            layer, window_days=30):
    alerts = []
    if target_asset and sub_layer and signal_name:
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n,
                          AVG(pnl_pct) AS avg_pnl
                     FROM trade_reflections_v2
                    WHERE target_asset = ? AND sub_layer = ? AND signal_name = ?
                      AND created_at >= datetime('now', ?)
                      AND enrich_status IN ('ok', 'partial')""",
                (target_asset, sub_layer, signal_name,
                 f"-{int(window_days)} days"),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row:
            n = int(row[0] or 0)
            loss_n = int(row[1] or 0)
            avg_pnl = float(row[2] or 0.0)
            loss_rate = loss_n / max(n, 1)
            if n >= _PATTERN_MIN_SAMPLES and loss_rate >= _PATTERN_LOSS_RATE:
                alerts.append({
                    "pattern_key": f"{target_asset}x{sub_layer}x{signal_name}",
                    "sample_n": n, "loss_n": loss_n,
                    "avg_pnl": round(avg_pnl, 4),
                    "loss_rate": round(loss_rate, 2),
                    "alert_level": "high" if loss_rate >= 0.75 else "medium",
                })
    if layer:
        try:
            row2 = conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n,
                          AVG(pnl_pct) AS avg_pnl
                     FROM trade_reflections_v2
                    WHERE layer = ?
                      AND created_at >= datetime('now', ?)
                      AND enrich_status IN ('ok', 'partial')""",
                (layer, f"-{int(window_days)} days"),
            ).fetchone()
        except sqlite3.OperationalError:
            row2 = None
        if row2:
            n = int(row2[0] or 0)
            loss_n = int(row2[1] or 0)
            avg_pnl = float(row2[2] or 0.0)
            loss_rate = loss_n / max(n, 1)
            if n >= _PATTERN_MIN_SAMPLES * 3 and loss_rate >= _PATTERN_LOSS_RATE:
                alerts.append({
                    "pattern_key": f"layer:{layer}",
                    "sample_n": n, "loss_n": loss_n,
                    "avg_pnl": round(avg_pnl, 4),
                    "loss_rate": round(loss_rate, 2),
                    "alert_level": "high" if loss_rate >= 0.75 else "medium",
                })
    alerts.sort(key=lambda a: (-a["loss_rate"], -a["sample_n"]))
    return alerts[:3]


# N5: judgment vs execution
def audit_judgment_breakdown(reflection_row, kline_ctx, market_confirm,
                              pnl_pct, mfe_pct, mae_pct):
    action = str(reflection_row.get("suggested_action") or "HOLD").upper()
    win = pnl_pct > 0.005
    loss = pnl_pct < -0.005
    if action == "HOLD":
        direction_correct = True
    elif win:
        direction_correct = True
    elif loss:
        direction_correct = False
    else:
        direction_correct = (market_confirm in ("yes", "positive"))
    atr = float((kline_ctx or {}).get("atr_pct") or 0.0)
    sizing_correct = True
    if atr > 0:
        ratio = abs(pnl_pct) / atr
        if abs(pnl_pct) > atr * 2:
            sizing_correct = False
        elif ratio < 0.3:
            sizing_correct = False
    tp_correct = True
    if atr > 0 and mfe_pct > 0:
        if mfe_pct > pnl_pct + 0.5 * atr:
            tp_correct = False
    sl_correct = True
    if atr > 0 and mae_pct < 0:
        if abs(mae_pct) > 2 * atr:
            sl_correct = False
    judgment_score = (1.0 if direction_correct else 0.0) * 0.6 + \
                     (1.0 if sizing_correct else 0.0) * 0.4
    execution_score = (1.0 if tp_correct else 0.0) * 0.5 + \
                      (1.0 if sl_correct else 0.0) * 0.5
    return {
        "direction_correct": bool(direction_correct),
        "sizing_correct": bool(sizing_correct),
        "tp_correct": bool(tp_correct),
        "sl_correct": bool(sl_correct),
        "judgment_score": round(judgment_score, 4),
        "execution_score": round(execution_score, 4),
    }


# N6: self-meta audit
def audit_self_meta(conn, layer, sub_layer, signal_name, window_days=7):
    out = {"self_classify_drift": 0.0, "layer_bias": 0.0,
           "signal_hit_drift": 0.0, "self_overuse_count": 0,
           "self_underuse_count": 0, "note": ""}
    if not layer or not sub_layer:
        out["note"] = "layer/sub_layer empty"
        return out
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n
                 FROM trade_reflections_v2
                WHERE layer = ? AND created_at >= datetime('now', ?)
                  AND enrich_status IN ('ok', 'partial')""",
            (layer, f"-{int(window_days)} days"),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        n = int(row[0] or 0)
        loss_n = int(row[1] or 0)
        out["layer_bias"] = round(loss_n / max(n, 1), 4)
    if signal_name:
        try:
            cur = conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n
                     FROM trade_reflections_v2
                    WHERE signal_name = ? AND created_at >= datetime('now', ?)
                      AND enrich_status IN ('ok', 'partial')""",
                (signal_name, f"-{int(window_days)} days"),
            ).fetchone()
            prev = conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n
                     FROM trade_reflections_v2
                    WHERE signal_name = ?
                      AND created_at >= datetime('now', ?)
                      AND created_at < datetime('now', ?)
                      AND enrich_status IN ('ok', 'partial')""",
                (signal_name,
                 f"-{int(window_days + 14)} days",
                 f"-{int(window_days)} days"),
            ).fetchone()
        except sqlite3.OperationalError:
            cur = None
            prev = None
        if cur and prev:
            cur_n = int(cur[0] or 0)
            prev_n = int(prev[0] or 0)
            cur_lr = int(cur[1] or 0) / max(cur_n, 1)
            prev_lr = int(prev[1] or 0) / max(prev_n, 1)
            out["signal_hit_drift"] = round(cur_lr - prev_lr, 4)
    if out["layer_bias"] >= 0.65:
        out["self_classify_drift"] = round(out["layer_bias"] - 0.5, 4)
        out["self_overuse_count"] = 1
    elif out["layer_bias"] <= 0.35:
        out["self_classify_drift"] = round(out["layer_bias"] - 0.5, 4)
        out["self_underuse_count"] = 1
    return out


# Overall grade
def compute_overall_grade(truth_score, counter_evidence, pattern_alerts,
                           judgment_breakdown, meta_audit):
    truth_comp = float(truth_score)
    omit_rate = float(counter_evidence.get("omission_rate", 0.0))
    critical = bool(counter_evidence.get("critical_omitted", False))
    counter_comp = max(0.0, 1.0 - omit_rate - (0.3 if critical else 0.0))
    high_alerts = sum(1 for a in pattern_alerts if a.get("alert_level") == "high")
    pattern_comp = max(0.0, 1.0 - 0.2 * len(pattern_alerts) - 0.3 * high_alerts)
    judgment_comp = (
        float(judgment_breakdown.get("judgment_score", 0.5)) * 0.6 +
        float(judgment_breakdown.get("execution_score", 0.5)) * 0.4
    )
    score = 0.5 * truth_comp + 0.2 * counter_comp + 0.15 * pattern_comp + 0.15 * judgment_comp
    score = round(max(0.0, min(1.0, score)), 4)
    if score >= 0.85:
        return "A"
    if score >= 0.70:
        return "B"
    if score >= 0.50:
        return "C"
    if score >= 0.30:
        return "D"
    return "F"


def _load_decision_full(conn, decision_id):
    try:
        conn.row_factory = sqlite3.Row
        # 注: raw_news schema 没 title 列 (只有 content/cross_net_*), title 改为空串
        row = conn.execute(
            """SELECT a.id, a.ts, a.target_asset, a.suggested_action,
                      a.reasoning, a.reasoning_path, a.hard_data_evidence,
                      a.news_id, a.event_strength, a.market_confirmation,
                      a.completeness_score, a.factor_ranking,
                      r.content AS news_content, '' AS news_title,
                      r.first_seen_ts AS news_first_seen_ts
                 FROM ai_decisions a
                 LEFT JOIN raw_news r ON r.id = a.news_id
                WHERE a.id = ?""",
            (int(decision_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def _load_reflection(conn, reflection_id):
    try:
        conn.row_factory = sqlite3.Row
        # 注: schema 列名是 asset/action(不是 target_asset/suggested_action);
        # market_confirmation 由 reflection_deep.enrich_reflection 写到 market_regime_detail JSON.
        row = conn.execute(
            """SELECT id, decision_id, layer, sub_layer, signal_name,
                      asset AS target_asset, action AS suggested_action,
                      pnl_pct, mfe_pct, mae_pct,
                      kline_at_decision,
                      luck_vs_signal_score, cross_asset_sync_score
                 FROM trade_reflections_v2 WHERE id = ?""",
            (int(reflection_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def enrich_reflection_core(reflection_id, *, write=True):
    """v232 核心本质内容审计主入口."""
    conn = db.get_connection()
    out = {"reflection_id": int(reflection_id), "ok": False,
           "enrich_status": "failed", "enrich_error": ""}
    try:
        row = _load_reflection(conn, int(reflection_id))
        if not row:
            out["enrich_error"] = "reflection_not_found"
            return out
        decision_id = int(row.get("decision_id") or 0)
        dec = _load_decision_full(conn, decision_id) if decision_id else None
        if not dec:
            out["enrich_error"] = "decision_not_found"
            return out
        target_asset = (row.get("target_asset") or dec.get("target_asset") or "").upper().strip()
        suggested_action = str(row.get("suggested_action") or
                                dec.get("suggested_action") or "HOLD").upper()
        layer = str(row.get("layer") or "")
        sub_layer = str(row.get("sub_layer") or "")
        signal_name = str(row.get("signal_name") or "")
        reasoning_path = str(dec.get("reasoning_path") or dec.get("reasoning") or "")
        hard_data_evidence = str(dec.get("hard_data_evidence") or "")
        news_content = str(dec.get("news_content") or "")
        market_confirm = str(row.get("market_confirmation") or
                              dec.get("market_confirmation") or "").lower()
        pnl_pct = float(row.get("pnl_pct") or 0.0)
        mfe_pct = float(row.get("mfe_pct") or 0.0)
        mae_pct = float(row.get("mae_pct") or 0.0)
        cross_sync = float(row.get("cross_asset_sync_score") or 0.0)
        kline_ctx = {}
        try:
            kline_ctx = json.loads(row.get("kline_at_decision") or "{}")
        except (json.JSONDecodeError, TypeError):
            kline_ctx = {}
        decision_ts_ms = 0
        try:
            decision_ts_ms = int(dec.get("ts") or 0)
            if 0 < decision_ts_ms < 10**12:
                decision_ts_ms *= 1000
        except (TypeError, ValueError):
            decision_ts_ms = 0
        chain_audit = audit_chain(reasoning_path)
        counter_evidence = audit_counter_evidence(
            conn, reasoning_path, hard_data_evidence, news_content,
            decision_ts_ms, target_asset, suggested_action,
        )
        truth_dict = audit_truth_score(
            row, chain_audit, cross_sync, market_confirm, pnl_pct,
        )
        pattern_alerts = detect_pattern_alerts(
            conn, target_asset, sub_layer, signal_name, layer, window_days=30,
        )
        judgment_breakdown = audit_judgment_breakdown(
            row, kline_ctx, market_confirm, pnl_pct, mfe_pct, mae_pct,
        )
        meta_audit = audit_self_meta(conn, layer, sub_layer, signal_name, window_days=7)
        grade = compute_overall_grade(
            truth_dict["truth_score"], counter_evidence, pattern_alerts,
            judgment_breakdown, meta_audit,
        )
        out.update({
            "core_chain_audit": json.dumps(chain_audit, ensure_ascii=False),
            "core_counter_evidence": json.dumps(counter_evidence, ensure_ascii=False),
            "core_truth_score": truth_dict["truth_score"],
            "core_pattern_alert": json.dumps(pattern_alerts, ensure_ascii=False),
            "core_judgment_breakdown": json.dumps(judgment_breakdown, ensure_ascii=False),
            "core_meta_audit": json.dumps(meta_audit, ensure_ascii=False),
            "core_overall_grade": grade,
            "enrich_status": "ok",
            "_truth_detail": truth_dict,
        })
        if write:
            try:
                conn.execute(
                    """UPDATE trade_reflections_v2
                          SET core_chain_audit = ?, core_counter_evidence = ?,
                              core_truth_score = ?, core_pattern_alert = ?,
                              core_judgment_breakdown = ?, core_meta_audit = ?,
                              core_overall_grade = ?,
                              core_enriched_at = datetime('now'),
                              core_enrich_status = ?, core_enrich_error = ?
                        WHERE id = ?""",
                    (out["core_chain_audit"], out["core_counter_evidence"],
                     out["core_truth_score"], out["core_pattern_alert"],
                     out["core_judgment_breakdown"], out["core_meta_audit"],
                     out["core_overall_grade"], out["enrich_status"], "",
                     int(reflection_id)),
                )
                conn.commit()
                out["ok"] = True
            except sqlite3.OperationalError as _e:
                out["enrich_error"] = f"write_failed:{_e}"
                out["enrich_status"] = "failed"
                try:
                    conn.rollback()
                except Exception:
                    pass
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def enrich_pending_loop(batch_size=50, *, max_per_run=200,
                         only_enrich_status="pending"):
    """daemon 入口."""
    conn = db.get_connection()
    out = {"scanned": 0, "ok": 0, "partial": 0, "failed": 0,
           "skipped": 0, "errors": []}
    try:
        rows = conn.execute(
            """SELECT id FROM trade_reflections_v2
                WHERE (core_enrich_status = ? OR core_enrich_status = ''
                       OR core_enrich_status IS NULL)
                ORDER BY id DESC LIMIT ?""",
            (only_enrich_status, int(max_per_run)),
        ).fetchall()
    except sqlite3.OperationalError as _e:
        out["errors"].append(str(_e))
        conn.close()
        return out
    for r in rows:
        rid = int(r[0])
        out["scanned"] += 1
        try:
            r_out = enrich_reflection_core(rid, write=True)
            st = r_out.get("enrich_status") or "failed"
            if st == "ok":
                out["ok"] += 1
            elif st == "partial":
                out["partial"] += 1
            else:
                out["failed"] += 1
                if r_out.get("enrich_error"):
                    out["errors"].append(f"id={rid}: {r_out['enrich_error']}")
        except Exception as _e:
            out["failed"] += 1
            out["errors"].append(f"id={rid}: {type(_e).__name__}: {_e}")
        if out["scanned"] >= batch_size:
            break
    conn.close()
    return out


def get_core_stats(window_days=30):
    """统计近 N 天 core_* 字段填充 + 综合评级分布."""
    conn = db.get_connection()
    out = {"window_days": int(window_days)}
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN core_enrich_status = 'ok' THEN 1 ELSE 0 END) AS ok_n,
                      SUM(CASE WHEN core_enrich_status = 'partial' THEN 1 ELSE 0 END) AS partial_n,
                      SUM(CASE WHEN core_enrich_status = 'failed' THEN 1 ELSE 0 END) AS failed_n,
                      AVG(core_truth_score) AS avg_truth,
                      SUM(CASE WHEN core_overall_grade = 'A' THEN 1 ELSE 0 END) AS a_n,
                      SUM(CASE WHEN core_overall_grade = 'B' THEN 1 ELSE 0 END) AS b_n,
                      SUM(CASE WHEN core_overall_grade = 'C' THEN 1 ELSE 0 END) AS c_n,
                      SUM(CASE WHEN core_overall_grade = 'D' THEN 1 ELSE 0 END) AS d_n,
                      SUM(CASE WHEN core_overall_grade = 'F' THEN 1 ELSE 0 END) AS f_n
                 FROM trade_reflections_v2
                WHERE created_at >= datetime('now', ?)""",
            (f"-{int(window_days)} days",),
        ).fetchone()
        if row:
            out.update({
                "total": int(row[0] or 0),
                "ok": int(row[1] or 0),
                "partial": int(row[2] or 0),
                "failed": int(row[3] or 0),
                "avg_truth_score": round(float(row[4] or 0.5), 4),
                "grade_distribution": {
                    "A": int(row[5] or 0), "B": int(row[6] or 0),
                    "C": int(row[7] or 0), "D": int(row[8] or 0),
                    "F": int(row[9] or 0),
                },
            })
    except sqlite3.OperationalError as _e:
        out["error"] = str(_e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


__all__ = [
    "audit_chain", "audit_counter_evidence", "audit_truth_score",
    "detect_pattern_alerts", "audit_judgment_breakdown", "audit_self_meta",
    "compute_overall_grade", "enrich_reflection_core", "enrich_pending_loop",
    "get_core_stats",
]


