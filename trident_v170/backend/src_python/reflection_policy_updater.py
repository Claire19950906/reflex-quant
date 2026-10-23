#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v232.6 P1: 反思 -> policy 闭环 - 把仪表盘变成引擎。

P0 已经把反思系统升级到带数字锚点 (pnl/mfe/mae/方向判定/同 layer 命中率/跨资产同步)
+ 自评 credibility (reflection_self_score),但这只是仪表盘。ai_worker 下次遇到同一类
信号并不会自动学到"E001 累计亏 81%"这个事实。本文件做两件事让闭环开始转起来:

1. generate_policy_candidates(window_days, min_sample, pnl_threshold)
   扫 decision_errors JOIN ai_decisions + trade_reflections_v2,
   按 4 类规则产出策略补丁候选,写入 policy_patch_queue 表:
     - prompt_addition:   在 ai_worker prompt 末尾加一段提示
     - threshold_change:  调整 risk_manager 阈值
     - rule_addition:     新增 gate 规则
     - pattern_warning:   LLM 反思系统性问题

2. find_sibling_diffs(window_days, min_consistency)
   利用 P0 的 is_sibling 字段:同一 content_fingerprint 在 +-60s 内产出的多个决策,
   按 (winner, loser) 配对,提取"赢家独有 / 输家独有"的 feature vector。
   真实因果推断的最小差异 = 控制信号相同,看哪个变量让 W 变 L。

设计原则:
  - candidate 生成 zero-risk: 只写新表,不动 ai_worker / risk_manager
  - UNIQUE(target_module, patch_payload_hash) 防止无限重复
  - 默认生成后 status='pending',需要人工 review 才允许 apply

执行:
  - python -m reflection_policy_updater                 # 一次性跑一轮
  - python -m reflection_policy_updater --daemon         # 每 24h 跑一次
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import db


# --- 配置 ---
DEFAULT_WINDOW_DAYS = 7
DEFAULT_MIN_SAMPLE = 5
DEFAULT_PNL_THRESHOLD = -0.30   # 累计 pnl < -30% 才视为值得提补丁
DEFAULT_MIN_CONSISTENCY = 0.65  # 兄弟决策 diff 重复比例 >= 65% 才视为真实因果
DEFAULT_RETENTION_DAYS = 90
DEFAULT_PATCH_TTL_DAYS = 90     # 候选 90 天未审 -> expired

_LESSON_OPENERS = (
    "LLM ", "HOLD", "事件", "出价", "确认", "波动", "政策", "新闻", "入场", "止损",
    "风控", "止损位", "窗口", "rate", "rule",
)


# --- helpers ---
def _now() -> int:
    return int(time.time())


def _hash_payload(payload: Dict[str, Any]) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _safe_json_dumps(obj: Any, default: str = "[]") -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return default


def _upsert_patch(
    conn: sqlite3.Connection,
    patch_type: str,
    target_module: str,
    payload: Dict[str, Any],
    source_kind: str,
    source_reflection_ids: List[int],
    source_error_codes: List[str],
    sample_size: int,
    cumulative_pnl_impact: float,
    confidence_score: float,
    rationale: str,
    ttl_days: int = DEFAULT_PATCH_TTL_DAYS,
) -> Tuple[int, bool]:
    """插入新候选;同 (target_module, payload_hash) 已存在则跳过。

    返回 (patch_id, was_inserted):
      - patch_id: int, 新建或已存在的 row id
      - was_inserted: bool, True=本次真插入, False=已存在跳过
    """
    payload_hash = _hash_payload(payload)
    now = _now()
    expires = now + ttl_days * 86400
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO policy_patch_queue (
                patch_type, target_module, patch_payload, patch_payload_hash,
                source_kind, source_reflection_ids, source_error_codes,
                sample_size, cumulative_pnl_impact, confidence_score,
                rationale, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                str(patch_type),
                str(target_module),
                json.dumps(payload, ensure_ascii=False),
                payload_hash,
                str(source_kind),
                _safe_json_dumps(source_reflection_ids),
                _safe_json_dumps(source_error_codes),
                int(sample_size),
                float(cumulative_pnl_impact),
                round(float(confidence_score), 4),
                str(rationale)[:500],
                now,
                expires,
            ),
        )
        # SQLite: cur.rowcount == 1 表示真插入,0 表示被 UNIQUE 忽略
        if cur.rowcount and cur.rowcount > 0 and cur.lastrowid:
            return int(cur.lastrowid), True
        # 已存在: 找回来
        row = conn.execute(
            "SELECT id FROM policy_patch_queue WHERE target_module=? AND patch_payload_hash=?",
            (target_module, payload_hash),
        ).fetchone()
        return (int(row[0]), False) if row else (-1, False)
    except Exception as exc:  # pragma: no cover
        print(f"[policy_updater] upsert failed: {exc}", flush=True)
        return -1, False


# --- 候选生成主入口 ---
def generate_policy_candidates(
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    pnl_threshold: float = DEFAULT_PNL_THRESHOLD,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """扫近期反思 + 错误码,产出 4 类候选。"""
    own_conn = conn is None
    if conn is None:
        conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    summary: Dict[str, Any] = {
        "n_created": 0, "n_skipped": 0, "by_type": Counter(),
        "window_days": window_days, "min_sample": min_sample,
        "pnl_threshold": pnl_threshold, "candidates": [],
    }
    try:
        now = _now()
        window_start = now - window_days * 86400

        # 1. error_code_pnl 候选
        rows = conn.execute(
            """
            SELECT
              e.error_code                         AS code,
              COUNT(DISTINCT e.decision_id)        AS n_errors,
              COUNT(DISTINCT CASE WHEN d.settled = 1 THEN e.decision_id END) AS n_settled,
              SUM(CASE WHEN d.settled = 1 AND d.forward_pnl IS NOT NULL
                       THEN d.forward_pnl ELSE 0 END) AS total_pnl,
              AVG(CASE WHEN d.settled = 1 AND d.forward_pnl IS NOT NULL
                       THEN d.forward_pnl END) AS avg_pnl,
              GROUP_CONCAT(DISTINCT d.target_asset) AS assets
            FROM decision_errors e
            LEFT JOIN ai_decisions d ON d.id = e.decision_id
            WHERE e.error_code IS NOT NULL AND e.error_code != ''
              AND d.created_at >= datetime(?, 'unixepoch')
            GROUP BY e.error_code
            HAVING n_settled >= ?
              AND total_pnl <= ?
            ORDER BY total_pnl ASC
            """,
            (window_start, min_sample, pnl_threshold),
        ).fetchall()

        for r in rows:
            code = r["code"]
            n_settled = int(r["n_settled"] or 0)
            total_pnl = float(r["total_pnl"] or 0.0)
            avg_pnl = float(r["avg_pnl"] or 0.0)
            assets = (r["assets"] or "").strip()
            confidence = round(min(1.0, 0.4 * min(n_settled / 20.0, 1.0)
                                  + 0.4 * min(abs(total_pnl), 1.0)
                                  + 0.2 * (1.0 if n_settled >= 10 else 0.5)), 4)

            # 候选 A: prompt_addition
            prompt_payload = {
                "injection_target": "ai_worker.system_prompt.tail",
                "addition": (
                    f"[反思警告] 错误码 {code} 在近 {window_days}d 内的 {n_settled} 笔"
                    f"已结算决策中累计 {total_pnl:+.2%} (平均 {avg_pnl:+.2%})。"
                    f"涉及品种: {assets or '未限定'}。"
                    f"建议: 遇到同类信号时降低置信度或 HOLD,等待二次确认。"
                ),
                "trigger": {"error_code": code},
                "ttl_decisions": 50,
            }
            pid_a, inserted_a = _upsert_patch(
                conn, "prompt_addition", "ai_worker",
                prompt_payload, "error_code_pnl", [], [code],
                n_settled, total_pnl, confidence,
                f"错误码 {code} 累计 pnl {total_pnl:+.2%} ({n_settled} 笔, 平均 {avg_pnl:+.2%})",
            )
            if pid_a > 0 and inserted_a:
                summary["n_created"] += 1
                summary["by_type"]["prompt_addition"] += 1
                summary["candidates"].append({"id": pid_a, "type": "prompt_addition", "code": code})
            elif pid_a > 0:
                summary["n_skipped"] += 1

            # 候选 B: threshold_change (累计亏 >= 阈值的 1.5x 且 样本 >= 8)
            if total_pnl <= pnl_threshold * 1.5 and n_settled >= max(min_sample, 8):
                threshold_payload = {
                    "threshold_name": f"risk.{code}.position_scale",
                    "old_value": 1.0,
                    "new_value": max(0.3, 1.0 + total_pnl),
                    "duration_days": window_days,
                    "reason": f"错误码 {code} 累计 pnl {total_pnl:+.2%}, 临时降级仓位",
                }
                pid_b, inserted_b = _upsert_patch(
                    conn, "threshold_change", "risk_manager",
                    threshold_payload, "error_code_pnl", [], [code],
                    n_settled, total_pnl, confidence,
                    f"错误码 {code} 持续亏损, 建议 {window_days}d 内仓位缩至 {threshold_payload['new_value']:.2f}",
                )
                if pid_b > 0 and inserted_b:
                    summary["n_created"] += 1
                    summary["by_type"]["threshold_change"] += 1
                    summary["candidates"].append({"id": pid_b, "type": "threshold_change", "code": code})
                elif pid_b > 0:
                    summary["n_skipped"] += 1
            else:
                summary["n_skipped"] += 1

        # 2. reflection_pattern 候选 (LLM self_score<0.4 高频短语)
        low_self_rows = conn.execute(
            """
            SELECT id, layer, signal_name, lesson
            FROM trade_reflections_v2
            WHERE created_at >= datetime(?, 'unixepoch')
              AND reflection_self_score IS NOT NULL
              AND reflection_self_score < 0.4
              AND lesson IS NOT NULL AND lesson != ''
            ORDER BY reflection_self_score ASC
            LIMIT 50
            """,
            (window_start,),
        ).fetchall()

        if low_self_rows:
            n_low = len(low_self_rows)
            short_phrases = Counter()
            for r in low_self_rows:
                lesson = str(r["lesson"] or "")
                for opener in _LESSON_OPENERS:
                    if lesson.startswith(opener) or f"。{opener}" in lesson:
                        short_phrases[opener] += 1
            top_phrases = ", ".join(
                f"{p}({c})" for p, c in short_phrases.most_common(5)
            ) or "无"
            pattern_payload = {
                "injection_target": "trade_reflection_llm._DEPTH_PROMPT_TEMPLATE",
                "replace_or_add": "add_section_warning",
                "warning_text": (
                    f"近 {window_days}d 有 {n_low} 条反思的 self_score<0.4, 高频开头/词: {top_phrases}。"
                    "反思深度不足, 请每条 lesson 给出 (1) 具体规则代码或数据点"
                    " (2) 可量化阈值 (3) 反事实估计, 避免空泛形容词。"
                ),
            }
            pid_p, inserted_p = _upsert_patch(
                conn, "pattern_warning", "decision_prompt",
                pattern_payload, "reflection_pattern",
                [int(r["id"]) for r in low_self_rows], [],
                n_low, 0.0, round(min(1.0, n_low / 20.0), 4),
                f"近期 {n_low} 条反思 self_score<0.4, 高频短语: {top_phrases}",
            )
            if pid_p > 0 and inserted_p:
                summary["n_created"] += 1
                summary["by_type"]["pattern_warning"] += 1
                summary["candidates"].append({"id": pid_p, "type": "pattern_warning"})
            elif pid_p > 0:
                summary["n_skipped"] += 1

        # 3. 兄弟决策因果差分
        sibling_rules = find_sibling_diffs(
            window_days=window_days, min_consistency=DEFAULT_MIN_CONSISTENCY,
            conn=conn, write_to_queue=True,
        )
        summary["sibling_rules"] = sibling_rules
        summary["n_created"] += sibling_rules.get("n_created", 0)
        summary["by_type"]["rule_addition"] += sibling_rules.get("n_created", 0)

        conn.commit()
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
    return summary


def _discretize_features(decs: List[Dict[str, Any]]) -> Counter:
    """对决策列表的数值特征做 q1/q3 分位离散化,文本特征原样。"""
    feats: Counter = Counter()
    vals: Dict[str, List[float]] = defaultdict(list)
    for d in decs:
        for k in ("sentiment_score", "impact_magnitude",
                  "attention_score", "heat_score",
                  "evidence_confidence", "impact_score"):
            v = d.get(k)
            if isinstance(v, (int, float)):
                vals[k].append(float(v))
    q: Dict[str, Tuple[float, float]] = {}
    for k, vs in vals.items():
        if len(vs) >= 2:
            sv = sorted(vs)
            q1 = sv[len(sv) // 4]
            q3 = sv[3 * len(sv) // 4]
            q[k] = (q1, q3)
    for d in decs:
        for k in ("sentiment_score", "impact_magnitude",
                  "attention_score", "heat_score",
                  "evidence_confidence", "impact_score"):
            v = d.get(k)
            if isinstance(v, (int, float)) and k in q:
                lo, hi = q[k]
                if v < lo:
                    feats[f"{k}<q1"] += 1
                elif v > hi:
                    feats[f"{k}>q3"] += 1
                else:
                    feats[f"{k}~mid"] += 1
        for tk in ("event_strength", "market_confirmation", "asset_nature"):
            v = d.get(tk)
            if v:
                feats[f"{tk}={v}"] += 1
    total = max(1, len(decs))
    return Counter({k: c / total for k, c in feats.items() if c / total >= 0.5})


def _process_sibling_group(
    conn: sqlite3.Connection,
    fp: str,
    asset: str,
    winners: List[Dict[str, Any]],
    losers: List[Dict[str, Any]],
    now: int,
    min_consistency: float,
    min_pairs: int,
    write_to_queue: bool,
    summary: Dict[str, Any],
) -> None:
    """单组兄弟决策的 winner/loser 差异处理: 写 sibling_diff_rules + rule_addition 候选。"""
    w_feats = _discretize_features(winners)
    l_feats = _discretize_features(losers)
    shared_keys = {k for k in w_feats if k in l_feats and
                   abs(w_feats[k] - l_feats[k]) < 0.15}
    # shared_features 存 dict {k: (w, l)} — 便于人审时看双方都有但值相近
    shared = {k: [round(w_feats[k], 3), round(l_feats[k], 3)]
              for k in shared_keys}
    winner_only = {k: round(w_feats[k], 3) for k in w_feats
                   if k not in shared_keys}
    loser_only = {k: round(l_feats[k], 3) for k in l_feats
                  if k not in shared_keys}
    if not winner_only and not loser_only:
        return

    rule_sig = _hash_payload({
        "fp": fp, "asset": asset,
        "w_only": sorted(winner_only.keys()),
        "l_only": sorted(loser_only.keys()),
    })
    n_pairs = min(len(winners), len(losers))
    w_pnl_avg = sum(d["forward_pnl"] for d in winners) / max(1, len(winners))
    l_pnl_avg = sum(d["forward_pnl"] for d in losers) / max(1, len(losers))
    pnl_gap = w_pnl_avg - l_pnl_avg

    consistency = 1.0 if n_pairs >= 2 else 0.5
    confidence = round(
        0.4 * consistency + 0.3 * min(1.0, n_pairs / 5.0)
        + 0.3 * min(1.0, abs(pnl_gap) / 0.05),
        4,
    )

    cur = conn.execute(
        """
        INSERT INTO sibling_diff_rules (
            rule_signature, fingerprint, asset,
            shared_features, winner_only_features, loser_only_features,
            winner_decision_ids, loser_decision_ids,
            n_pairs, consistency_score, confidence_score,
            pnl_gap_avg, discovered_at, last_seen_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate')
        ON CONFLICT(rule_signature) DO UPDATE SET
            n_pairs = excluded.n_pairs,
            pnl_gap_avg = excluded.pnl_gap_avg,
            last_seen_at = excluded.last_seen_at,
            consistency_score = excluded.consistency_score,
            confidence_score = excluded.confidence_score
        """,
        (
            rule_sig, fp, asset,
            _safe_json_dumps(shared),
            _safe_json_dumps(winner_only),
            _safe_json_dumps(loser_only),
            _safe_json_dumps([d["did"] for d in winners]),
            _safe_json_dumps([d["did"] for d in losers]),
            n_pairs, consistency, confidence,
            round(pnl_gap, 6), now, now,
        ),
    )
    sid = int(cur.lastrowid or 0)
    summary["n_rules"] += 1
    rule_entry = {
        "rule_id": sid, "signature": rule_sig[:12],
        "fp": fp[:16], "asset": asset,
        "n_pairs": n_pairs, "consistency": consistency,
        "confidence": confidence,
        "pnl_gap": round(pnl_gap, 4),
        "winner_only": list(winner_only.keys())[:5],
        "loser_only": list(loser_only.keys())[:5],
    }
    summary["rules"].append(rule_entry)

    if write_to_queue and confidence >= min_consistency and n_pairs >= min_pairs:
        rule_payload = {
            "rule_signature": rule_sig,
            "asset": asset,
            "fp_prefix": fp[:16],
            "trigger_features": list(loser_only.keys()),
            "block_features": list(winner_only.keys()),
            "suggested_gate": (
                f"if any([{', '.join(repr(k) for k in loser_only)}]) "
                f"and asset == {asset!r}: reduce_confidence=0.5"
            ),
            "supporting_pairs": n_pairs,
            "pnl_gap_avg": round(pnl_gap, 6),
        }
        pid, inserted_rule = _upsert_patch(
            conn, "rule_addition", "gate",
            rule_payload, "sibling_diff", [], [asset],
            n_pairs, pnl_gap, confidence,
            f"兄弟决策差分: {asset} 同 fp 在 {n_pairs} 对 (W,L) 中, "
            f"赢家特征 {list(winner_only.keys())[:3]}, "
            f"输家特征 {list(loser_only.keys())[:3]} (pnl gap {pnl_gap:+.2%})",
        )
        if pid > 0 and inserted_rule:
            summary["n_created"] += 1
            rule_entry["patch_id"] = pid


def find_sibling_diffs(
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_consistency: float = DEFAULT_MIN_CONSISTENCY,
    min_pairs: int = 3,
    conn: Optional[sqlite3.Connection] = None,
    write_to_queue: bool = True,
) -> Dict[str, Any]:
    """找同 content_fingerprint 的兄弟决策,提取 winner/loser 差异 feature,
    写入 sibling_diff_rules 表, 同时产出 rule_addition 候选。"""
    own_conn = conn is None
    if conn is None:
        conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    summary: Dict[str, Any] = {
        "n_groups": 0, "n_rules": 0, "n_created": 0,
        "rules": [], "window_days": window_days,
    }
    try:
        now = _now()
        window_start = now - window_days * 86400

        rows = conn.execute(
            """
            SELECT a.id AS did, a.target_asset, a.suggested_action,
                   a.is_correct, a.forward_pnl,
                   a.sentiment_score, a.impact_magnitude, a.attention_score,
                   a.heat_score, a.evidence_confidence, a.event_strength,
                   a.market_confirmation, a.impact_score, a.asset_nature,
                   n.content_fingerprint AS fp
            FROM ai_decisions a
            JOIN raw_news n ON n.id = a.news_id
            WHERE a.settled = 1 AND a.forward_pnl IS NOT NULL
              AND n.content_fingerprint IS NOT NULL AND n.content_fingerprint != ''
              AND a.created_at >= datetime(?, 'unixepoch')
            ORDER BY n.content_fingerprint, a.created_at
            """,
            (window_start,),
        ).fetchall()
        if not rows:
            return summary

        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            fp = str(r["fp"])
            groups[fp].append({
                "did": int(r["did"]),
                "asset": str(r["target_asset"] or ""),
                "action": str(r["suggested_action"] or ""),
                "is_correct": str(r["is_correct"] or ""),
                "forward_pnl": float(r["forward_pnl"] or 0.0),
                "sentiment_score": r["sentiment_score"],
                "impact_magnitude": r["impact_magnitude"],
                "attention_score": r["attention_score"],
                "heat_score": r["heat_score"],
                "evidence_confidence": r["evidence_confidence"],
                "event_strength": str(r["event_strength"] or ""),
                "market_confirmation": str(r["market_confirmation"] or ""),
                "impact_score": r["impact_score"],
                "asset_nature": str(r["asset_nature"] or ""),
            })
        summary["n_groups"] = sum(1 for g in groups.values() if len(g) >= 2)

        for fp, decs in groups.items():
            if len(decs) < 2:
                continue
            by_asset: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for d in decs:
                if d["asset"]:
                    by_asset[d["asset"]].append(d)
            for asset, group_decs in by_asset.items():
                if len(group_decs) < 2:
                    continue
                winners = [d for d in group_decs if d["is_correct"] == "WIN"]
                losers = [d for d in group_decs if d["is_correct"] == "LOSS"]
                if not winners or not losers:
                    continue
                _process_sibling_group(conn, fp, asset, winners, losers, now,
                                       min_consistency, min_pairs,
                                       write_to_queue, summary)

        conn.commit()
    except Exception as exc:
        print(f"[policy_updater] sibling_diffs failed: {exc}", flush=True)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
    return summary


def expire_old_patches(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """把 pending / approved 超过 retention 的 patch 标为 expired。"""
    conn = db.get_connection()
    try:
        now = _now()
        cur = conn.execute(
            """
            UPDATE policy_patch_queue
               SET status = 'expired'
             WHERE status IN ('pending', 'approved')
               AND created_at < ?
            """,
            (now - retention_days * 86400,),
        )
        n = cur.rowcount or 0
        conn.commit()
        return int(n)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_policy_update_cycle(
    window_days: int = DEFAULT_WINDOW_DAYS,
    do_expire: bool = True,
) -> Dict[str, Any]:
    """一次性完整跑一轮: 生成候选 + 兄弟差分 + 过期清理。"""
    t0 = time.time()
    candidates = generate_policy_candidates(window_days=window_days)
    n_expired = expire_old_patches() if do_expire else 0
    return {
        "elapsed_sec": round(time.time() - t0, 3),
        "n_expired": n_expired,
        "candidates": candidates,
    }


def _daemon_main(interval_s: int) -> None:
    print(f"[policy_updater] daemon started, interval={interval_s}s", flush=True)
    while True:
        try:
            r = run_policy_update_cycle()
            print(f"[policy_updater] cycle done: {r}", flush=True)
        except Exception as exc:
            print(f"[policy_updater] cycle error: {exc}", flush=True)
        time.sleep(interval_s)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="reflection -> policy 闭环")
    parser.add_argument("--daemon", action="store_true", help="周期循环模式")
    parser.add_argument("--interval", type=int, default=86400, help="daemon 间隔秒")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    args = parser.parse_args()
    if args.daemon:
        _daemon_main(args.interval)
    else:
        result = run_policy_update_cycle(window_days=args.window_days)
        print(json.dumps(result, ensure_ascii=False, indent=2))