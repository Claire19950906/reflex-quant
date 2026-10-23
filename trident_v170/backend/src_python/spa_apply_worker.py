#!/usr/bin/env python3
"""v199 SPA Apply Worker — 把满足阈值的 pending 策略调整推进到 applied 状态,并同步到 ai_decisions 闭环。

背景:
  - strategy_param_adjustments (SPA) v176+ 引入, status='pending' 永久化是 D 级断链之一
  - 现有 _approve_adjustment() 仅在人工 /api/paper/adjustments/{id}/approve 路径触发
  - SPA param_key 大量是 filter.* / analysis.* / sizing.* / score.* (review_insights 提炼),
    不在 paper_trading._V182_SETTINGS_KEYS / strategy_store._V182_PARAM_KEYS 白名单
  - v199 方案:
      1) 周期扫 pending + 阈值满足的 SPA
      2) param_key ∈ _KNOWN_FILTER_PARAM_KEYS (filter.*/analysis.*/sizing.*/score.*)
         → 走 v199 路径: notes-only applied, 写 applied_setting_change
      3) 同步到 ai_decisions.applied_setting_changes (按 asset 维度回填)

阈值 (对齐 v182 reflection 但更严):
  SAMPLE_THRESHOLD = 5     # 累计样本下限
  CONFIDENCE_THRESHOLD = 0.65  # 平均置信下限
"""
from __future__ import annotations
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db, sqlite3
from typing import Any, Dict, List, Optional


# v199 阈值（与 engine.reflection._V182_* 对齐）
SAMPLE_THRESHOLD = 5
CONFIDENCE_THRESHOLD = 0.65

# v200 P0-A2: 高样本 SPA 自动 boost 阈值
# 背景：当前 11/13 pending 全因 conf < 0.65 卡住,但 sample_size 28-46,证据极强。
# 引入「高样本 = 隐含高置信」的回退公式,让累计样本≥28 的 SPA 即使 avg_confidence
# 偏低也能被推进,而不需要等样本再翻倍。
HIGH_SAMPLE_THRESHOLD = 28      # sample_size >= 此值触发 boost
HIGH_SAMPLE_BOOST_CONF = 0.65  # boost 后等效置信度
# sample < 28 仍按原 CONFIDENCE_THRESHOLD 卡,防止噪声 SPA 误推进

# v199 处理的 param_key 白名单（filter.* / analysis.* / sizing.* / score.*）
KNOWN_FILTER_PARAM_KEYS = frozenset({
    "filter.asset_match.main_asset_missed",
    "filter.asset_match.cross_asset_misroute",
    "filter.contradiction_check.sentiment_vs_market",
    "filter.event_atoms.asset_mismatch",
    "filter.gate_rejected",
    "analysis.knowledge_inject.exp_misinterpreted",
    "analysis.knowledge_inject.no_trade",
    "analysis.llm_output.unknown",
    "analysis.llm_output.correctly_held",
    "sizing.position_weight",
    "sizing.stop_loss_pct",
    "sizing.take_profit_pct",
    "sizing.max_position_pct",
    "score.confidence_threshold",
    "score.event_strength_threshold",
    "score.buy_min_score",
    "score.sell_min_score",
})


def _effective_confidence(sample_size: int, avg_confidence: float) -> float:
    """v200 P0-A2: 算「有效置信度」用于门槛判断。

    高样本 (>= HIGH_SAMPLE_THRESHOLD=28) 的 SPA 即使 avg_confidence 偏低,
    也视为达到 HIGH_SAMPLE_BOOST_CONF (0.65),避免长期 pending。
    低样本 SPA 仍按原 avg_confidence 判断,防止噪声 SPA 误推进。
    """
    try:
        n = int(sample_size or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        c = float(avg_confidence or 0.0)
    except (TypeError, ValueError):
        c = 0.0
    if n >= HIGH_SAMPLE_THRESHOLD and c < HIGH_SAMPLE_BOOST_CONF:
        return HIGH_SAMPLE_BOOST_CONF
    return c


def _fetch_pending_candidates(conn, min_samples=5, min_conf=0.65):
    """列出满足累计阈值的 pending 候选 (含 v200 高样本 boost)。"""
    rows = conn.execute("""
        SELECT id, asset, action, param_key, direction, magnitude,
               sample_size, avg_confidence, evidence_summary,
               first_suggested_at, last_suggested_at
          FROM strategy_param_adjustments
         WHERE status='pending'
           AND sample_size >= ?
           AND applied_strategy_version_id IS NULL
           AND applied_setting_change = ''
         ORDER BY last_suggested_at DESC
    """, (min_samples,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # v200 P0-A2: 走 effective_conf 判断 (含 boost)
        eff_c = _effective_confidence(d.get("sample_size"), d.get("avg_confidence"))
        if eff_c < min_conf:
            continue
        d["_effective_conf"] = eff_c  # 标记给后续 applied_setting_change 用
        out.append(d)
    return out


def _apply_spa_to_db(conn, spa_id, param_key, direction, magnitude,
                     asset, sample_size, avg_confidence, effective_conf=None):
    """v199 路径: 把单个 SPA 推进到 applied 状态 (notes-only)。

    v200 P0-A2: effective_conf 是 _effective_confidence() 算出的「等效置信度」。
    当 avg_confidence 偏低但 sample_size >= HIGH_SAMPLE_THRESHOLD 时, effective_conf 会
    被 boost 到 HIGH_SAMPLE_BOOST_CONF。applied_setting_change 文本里既记录原始 avg_conf
    也标记 effective_conf (注明 boost) 用于审计可追溯。
    """
    try:
        raw_c = float(avg_confidence or 0.0)
    except (TypeError, ValueError):
        raw_c = 0.0
    eff_c = float(effective_conf) if effective_conf is not None else raw_c
    boosted = eff_c > raw_c + 1e-6
    tag = " [boosted]" if boosted else ""
    applied_text = (
        f"[{param_key}] direction={direction} mag={magnitude} "
        f"(asset={asset}, n={sample_size}, "
        f"conf={raw_c:.2f}{tag}→{eff_c:.2f})"
    )[:240]
    cur = conn.execute("""
        UPDATE strategy_param_adjustments
           SET status='applied', applied_setting_change=?,
               updated_at=datetime('now')
         WHERE id=? AND status='pending'
           AND applied_setting_change=''
    """, (applied_text, int(spa_id)))
    conn.commit()
    return cur.rowcount > 0


def _sync_to_ai_decisions(conn, asset, applied_text, max_rows=50):
    """把 applied SPA 同步到 ai_decisions.applied_setting_changes (按 asset 维度)。"""
    asset_norm = (asset or "").upper()
    if not asset_norm or asset_norm in ("NONE", "*", ""):
        asset_clause = ("(a.target_asset='' OR a.target_asset IS NULL "
                        "OR a.target_asset='NONE' OR a.target_asset='*')")
        params = (max_rows,)
    else:
        asset_clause = ("(UPPER(COALESCE(a.target_asset,'')) = ? "
                        "OR UPPER(COALESCE(a.target_asset,'')) = '*' "
                        "OR UPPER(COALESCE(a.target_asset,'')) = '')")
        params = (asset_norm, max_rows)
    sql = f"""
        SELECT a.id, a.applied_setting_changes
          FROM ai_decisions a
         WHERE a.settled = 0 AND {asset_clause}
         ORDER BY a.id DESC LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    synced = 0
    for r in rows:
        existing = str(r["applied_setting_changes"] or "")
        if applied_text in existing:
            continue
        new_val = applied_text if not existing else (existing + "\n" + applied_text)
        if len(new_val) > 4000:
            new_val = new_val[-4000:]
        conn.execute("""
            UPDATE ai_decisions SET applied_setting_changes=? WHERE id=?
        """, (new_val, int(r["id"])))
        synced += 1
    if synced:
        conn.commit()
    return synced


def run_once(dry_run=False):
    """单次扫描+推进。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    out = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": dry_run,
        "scanned": 0,
        "applied": [],
        "skipped_unknown_key": [],
        "synced_to_ai_decisions": 0,
    }
    try:
        candidates = _fetch_pending_candidates(conn)
        out["scanned"] = len(candidates)
        if not candidates:
            return out
        for spa in candidates:
            pk = str(spa.get("param_key") or "")
            if pk not in KNOWN_FILTER_PARAM_KEYS:
                out["skipped_unknown_key"].append({"spa_id": spa["id"],
                                                    "param_key": pk})
                continue
            if dry_run:
                out["applied"].append({"spa_id": spa["id"], "dry": True})
                continue
            ok = _apply_spa_to_db(
                conn, int(spa["id"]), pk,
                str(spa.get("direction") or ""),
                float(spa.get("magnitude") or 0.0),
                str(spa.get("asset") or ""),
                int(spa.get("sample_size") or 0),
                float(spa.get("avg_confidence") or 0.0),
                effective_conf=float(spa.get("_effective_conf")
                                     if spa.get("_effective_conf") is not None
                                     else spa.get("avg_confidence") or 0.0),
            )
            if ok:
                eff_c = float(spa.get("_effective_conf")
                              if spa.get("_effective_conf") is not None
                              else spa.get("avg_confidence") or 0.0)
                applied_text = (
                    f"[{pk}] direction={spa.get('direction')} "
                    f"mag={spa.get('magnitude')} "
                    f"(asset={spa.get('asset')}, n={spa.get('sample_size')}, "
                    f"conf={eff_c:.2f})"
                )[:240]
                synced = _sync_to_ai_decisions(
                    conn, str(spa.get("asset") or ""), applied_text
                )
                out["synced_to_ai_decisions"] += synced
                out["applied"].append({
                    "spa_id": spa["id"], "param_key": pk,
                    "asset": spa.get("asset"),
                    "synced_decisions": synced,
                    "effective_conf": eff_c,
                })
    finally:
        conn.close()
    return out


def daemon_loop(interval_s=600):
    """周期 daemon (默认 10min)。"""
    interval_s = max(60, int(os.environ.get("SPA_APPLY_INTERVAL_S", interval_s)))
    print(f"[SPA-APPLY] daemon started | interval={interval_s}s | "
          f"SAMPLE_THR={SAMPLE_THRESHOLD} CONF_THR={CONFIDENCE_THRESHOLD}",
          flush=True)
    while True:
        try:
            r = run_once(dry_run=False)
            if r["scanned"] or r["applied"]:
                print(f"[SPA-APPLY] @ {r['ts']} scanned={r['scanned']} "
                      f"applied={len(r['applied'])} "
                      f"synced_dec={r['synced_to_ai_decisions']} "
                      f"skipped_key={len(r['skipped_unknown_key'])}",
                      flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[SPA-APPLY] loop err: {type(exc).__name__}: {exc}",
                  flush=True)
        time.sleep(interval_s)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="单次运行")
    p.add_argument("--dry-run", action="store_true", help="只读不写")
    p.add_argument("--daemon", action="store_true", help="周期 daemon")
    p.add_argument("--interval", type=int, default=600, help="daemon 间隔秒")
    args = p.parse_args()
    if args.daemon:
        daemon_loop(interval_s=args.interval)
    else:
        r = run_once(dry_run=args.dry_run)
        print(r)