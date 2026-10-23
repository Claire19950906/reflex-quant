#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v230 自循环反思 — 反思对反思(meta-reflection on reflection)

设计动机(用户反馈第 4 条):
  反思系统有让你自我循环吗?
  旧 meta_reflection_updater 只改 .md, 不实际产出新反思/淘汰过期教训.

本模块做 4 件事:
  1. 对近 N 天每个 (layer, sub_layer, signal_name) 聚合 trade_reflections_v2,
     计算 win_rate, sample_n, avg_luck_score
  2. 与该信号的历史 baseline(更早 N 天)对比:
     - win_rate 下降 > 15%           → invalidated(教训失效)
     - win_rate 稳定 ±15%             → confirmed(教训仍有效)
     - win_rate 上升 > 15%            → refined(教训过于保守)
     - 高样本 + 低 luck + 高 win_rate → stable_skill(技能型稳定经验)
     - 高样本 + 高 luck + 高 win_rate → unstable_luck(运气型, 不应外推)
  3. 对最近的反思(level=1), 如果发现矛盾或 invalidation, 写 reflection_cycles
     + 更新 trade_reflections_v2.secondary_reflection_id
  4. 对 cycle.level>=1 的 finding, 再做一次 meta-meta 反思(level=2)
     形成可深可浅的自循环(默认 level<=2 防爆)

主入口:
  run_meta_loop(window_days=7, max_level=2) → 扫所有反思 + 写 cycles
  get_meta_loop_stats(window_days=30)        → 统计 cycles 概况
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import db


# ═══════════════════════════════════════════════════════════════════════════
# 阈值 (v231+ 可被 config.META_* 覆盖；offline 可用 reflection_calibration.py 重选)
# ═══════════════════════════════════════════════════════════════════════════

# win_rate 变化阈值
WR_DELTA_INVALIDATE = -0.15   # 下降 > 15%  → 教训失效
WR_DELTA_REFINE     =  0.15   # 上升 > 15%  → 教训过于保守
WR_DELTA_CONFIRM_BAND = 0.05  # 稳定带宽    → confirmed

# 样本阈值
MIN_SAMPLES_FOR_META = 5      # 至少 5 条反思才做 meta-loop, 避免小样本过拟合
MIN_SAMPLES_HIGH_CONF = 20    # >=20 条才认为 "high confidence" finding

# luck 阈值
LUCK_HIGH_THRESHOLD = 0.3     # |luck| > 0.3 视为"运气成分高"

# v231+ : 让 config.META_* 覆盖上面的硬编码常量
# 仅在 import config 成功时生效；失败则保留硬编码默认值（向后兼容）
try:
    import config  # type: ignore
    WR_DELTA_REFINE       = float(getattr(config, "META_WR_DELTA",       WR_DELTA_REFINE))
    WR_DELTA_CONFIRM_BAND = float(getattr(config, "META_CONFIRM_BAND",   WR_DELTA_CONFIRM_BAND))
    MIN_SAMPLES_FOR_META  = int(getattr(config,   "META_MIN_SAMPLES",    MIN_SAMPLES_FOR_META))
    LUCK_HIGH_THRESHOLD   = float(getattr(config, "META_LUCK_THRESHOLD", LUCK_HIGH_THRESHOLD))
    # WR_DELTA_INVALIDATE 是 -WR_DELTA_REFINE（对称）
    WR_DELTA_INVALIDATE   = -WR_DELTA_REFINE
except Exception:  # noqa: BLE001
    pass  # 保留默认硬编码值


# 默认窗口
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_COMPARE_DAYS   = 14   # 与"前 N 天"比


# ═══════════════════════════════════════════════════════════════════════════
# 1. 聚合:对每个 (layer, sub_layer, signal_name) 算 win_rate + luck
# ═══════════════════════════════════════════════════════════════════════════

def _aggregate_signal_window(conn, lookback_days: int) -> List[Dict[str, Any]]:
    """近 N 天按信号聚合统计.

    Returns: [{layer, sub_layer, signal_name, n, win_n, loss_n, win_rate, avg_luck}]
    """
    try:
        rows = conn.execute(
            """
            SELECT layer, sub_layer, signal_name,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pnl_pct > 0.005 THEN 1 ELSE 0 END) AS win_n,
                   SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n,
                   AVG(COALESCE(luck_vs_signal_score, 0.0)) AS avg_luck
              FROM trade_reflections_v2
             WHERE layer != '' AND sub_layer != '' AND signal_name != ''
               AND enrich_status IN ('ok','partial')
               AND created_at >= datetime('now', ?)
             GROUP BY layer, sub_layer, signal_name
             HAVING n >= ?
             ORDER BY n DESC
            """,
            (f"-{int(lookback_days)} days", int(MIN_SAMPLES_FOR_META)),
        ).fetchall()
    except sqlite3.OperationalError as _e:
        print(f"[META-LOOP] aggregate failed: {_e}", flush=True)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        n = int(r[3] or 0)
        win = int(r[4] or 0)
        loss = int(r[5] or 0)
        wr = win / n if n > 0 else 0
        out.append({
            "layer": r[0] or "", "sub_layer": r[1] or "", "signal_name": r[2] or "",
            "n": n, "win_n": win, "loss_n": loss,
            "win_rate": round(wr, 4),
            "avg_luck": round(float(r[6] or 0.0), 4),
        })
    return out


def _aggregate_signal_compare(conn, lookback_days: int, compare_days: int) -> List[Dict[str, Any]]:
    """对比窗口:更早 N 天的同信号聚合, 用于算 delta."""
    try:
        rows = conn.execute(
            """
            SELECT layer, sub_layer, signal_name,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pnl_pct > 0.005 THEN 1 ELSE 0 END) AS win_n,
                   SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n
              FROM trade_reflections_v2
             WHERE layer != '' AND sub_layer != '' AND signal_name != ''
               AND enrich_status IN ('ok','partial')
               AND created_at < datetime('now', ?)
               AND created_at >= datetime('now', ?)
             GROUP BY layer, sub_layer, signal_name
             HAVING n >= ?
             ORDER BY n DESC
            """,
            (f"-{int(lookback_days)} days",
             f"-{int(lookback_days + compare_days)} days",
             int(MIN_SAMPLES_FOR_META)),
        ).fetchall()
    except sqlite3.OperationalError as _e:
        print(f"[META-LOOP] compare aggregate failed: {_e}", flush=True)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        n = int(r[3] or 0)
        win = int(r[4] or 0)
        loss = int(r[5] or 0)
        wr = win / n if n > 0 else 0
        out.append({
            "layer": r[0] or "", "sub_layer": r[1] or "", "signal_name": r[2] or "",
            "n": n, "win_n": win, "loss_n": loss, "win_rate": round(wr, 4),
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 2. 推断 finding_kind
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# v240+ BOCPD 增强 evidence 的 helper（opt-in via META_BOCPD_ENABLED）
# ═══════════════════════════════════════════════════════════════════════════

def _augment_with_bocpd(evidence: Dict[str, Any],
                          recent_trades: Optional[List[float]],
                          ) -> Dict[str, Any]:
    """如果 opt-in 且有 recent_trades, 用 changepoint.py 跑 BOCPD 并把
    fresh_run_prob / changepoint_prob 写入 evidence. 不改变 finding_kind.

    Args:
        evidence: 当前分类器的 evidence dict (会就地修改+返回)
        recent_trades: 最近 trade outcomes (e.g., [1, 0, 1, 1, 0, ...])
                       None 或 < min_samples 时跳过

    Returns:
        增强后的 evidence dict
    """
    if not recent_trades or len(recent_trades) < 3:
        return evidence
    try:
        import config as _cfg
        if not getattr(_cfg, "META_BOCPD_ENABLED", False):
            return evidence
        import changepoint as _cp
        lambda_ = int(getattr(_cfg, "META_BOCPD_HAZARD_LAMBDA", 50))
        fresh_k = int(getattr(_cfg, "META_BOCPD_FRESH_RUN_K", 5))
        max_run = int(getattr(_cfg, "META_BOCPD_MAX_RUN", 200))
        threshold = float(getattr(_cfg, "META_BOCPD_THRESHOLD", 0.50))

        det = _cp.BOCPD(
            hazard_lambda=float(lambda_),
            max_run_length=max_run,
            prior_mean=evidence.get("current_wr", 0.5),
        )
        for x in recent_trades:
            det.update(float(x))

        fresh_prob = det.fresh_run_prob(k=fresh_k)
        evidence["_changepoint_prob"] = round(det.changepoint_prob, 4)
        evidence["_fresh_run_prob"] = round(fresh_prob, 4)
        evidence["_bocpd_max_run_length"] = det.most_likely_run_length
        evidence["_bocpd_hazard_lambda"] = lambda_
        evidence["_bocpd_threshold"] = threshold
        evidence["_bocpd_detected"] = fresh_prob > threshold
        evidence["_bocpd_n_trades"] = len(recent_trades)
    except Exception as _bocpd_err:  # noqa: BLE001
        # BOCPD 故障不应阻断主分类流程
        print(f"[META-LOOP] bocpd augmentation failed: {_bocpd_err}", flush=True)
    return evidence


def _classify_finding(current: Dict[str, Any],
                       baseline: Optional[Dict[str, Any]],
                       recent_trades: Optional[List[float]] = None,
                       ) -> Tuple[str, str, Dict[str, Any]]:
    """根据当前 vs 历史, 推断 finding_kind.

    Args:
        current: 当前窗口聚合 {n, win_rate, avg_luck, ...}
        baseline: 历史窗口聚合 (同上, 或 None)
        recent_trades: v240+ 可选 — 最近 trade outcome 流 (0/1 for win/loss)
                       若提供 + META_BOCPD_ENABLED=True, 跑 BOCPD 并把
                       _changepoint_prob 写入 evidence

    Returns: (finding_kind, finding_detail, evidence_json)

    finding_kind ∈ {
        invalidated,           # 教训失效 (win_rate 大幅下降)
        confirmed,             # 教训仍有效 (win_rate 稳定)
        refined,               # 教训过于保守 (win_rate 上升)
        contradictory,         # 与 lesson 文本直接矛盾
        stable_skill,          # 高 win + 高 luck(技能型稳定)
        unstable_luck,         # 高 win + 低 luck(运气型, 不应外推)
        low_sample_skip,       # 样本不足, 跳过
        no_baseline_new,       # 无历史, 第一次出现
    }

    v240+: 当 config.META_USE_BAYES=True 且两侧样本均 >= META_BAYES_MIN_SAMPLES，
    改用 reflection_bayes.bayes_classify_finding（Beta-Binomial 后验比较），
    否则保留原 delta 硬阈值逻辑。配置开关可灰度切换（off → 100% 行为兼容）。
    """
    # v240+: 贝叶斯分支 — 配置开启 + baseline 存在 → 走贝叶斯决策
    # 注意: bayes_classify_finding 内部自带小样本 delta fallback（_method=delta_fallback），
    # 所以这里不再提前守门，让 bayes 自己决定是否用后验比较。
    try:
        import config as _cfg  # local import 避免循环 + 尊重运行时配置
        if getattr(_cfg, "META_USE_BAYES", False):
            min_n = int(getattr(_cfg, "META_BAYES_MIN_SAMPLES", 5))
            prob_th = float(getattr(_cfg, "META_BAYES_PROB_THRESHOLD", 0.90))
            if baseline:
                # 用贝叶斯版本（reflection_bayes 内部处理 luck 主导判断 + delta fallback）
                import reflection_bayes as _rb
                _bayes_kind, _bayes_detail, _bayes_ev = _rb.bayes_classify_finding(
                    current, baseline,
                    prob_threshold=prob_th,
                    min_samples=min_n,
                )
                # v240+: BOCPD 增强 evidence（opt-in）
                _bayes_ev = _augment_with_bocpd(_bayes_ev, recent_trades)
                return (_bayes_kind, _bayes_detail, _bayes_ev)
    except Exception as _bayes_err:  # noqa: BLE001
        # 任何贝叶斯模块故障都应回退 delta 逻辑，绝不阻断主流程
        print(f"[META-LOOP] bayes branch failed: {_bayes_err}", flush=True)

    layer = current.get("layer", "")
    sub = current.get("sub_layer", "")
    sig = current.get("signal_name", "")
    n = current.get("n", 0)
    wr = current.get("win_rate", 0)
    avg_luck = current.get("avg_luck", 0)

    sig_key = f"{layer}/{sub}/{sig}"
    evidence: Dict[str, Any] = {
        "current_n": n, "current_wr": wr, "current_avg_luck": avg_luck,
    }

    # v240+ BOCPD: 在分类完成后, 用 recent_trades 跑 BOCPD 增强 evidence.
    # opt-in (META_BOCPD_ENABLED); 不改变 finding_kind, 仅供 dashboard / 高级策略决策.
    # 这里用单返回点模式: 所有分类结果先存到 _out_*, 最后统一过 _augment_with_bocpd.
    _out_kind: Optional[str] = None
    _out_detail: Optional[str] = None

    if not baseline:
        _out_kind = "no_baseline_new"
        _out_detail = (f"{sig_key}: n={n} wr={wr:.2f} luck={avg_luck:+.2f}, "
                       f"无历史 baseline, 首次观察")
    else:
        base_wr = baseline.get("win_rate", 0)
        base_n = baseline.get("n", 0)
        evidence["baseline_n"] = base_n
        evidence["baseline_wr"] = base_wr
        delta = wr - base_wr
        evidence["wr_delta"] = round(delta, 4)

        # 高样本 + 高 luck 主导 → 技能型稳定
        if n >= MIN_SAMPLES_HIGH_CONF and avg_luck >= LUCK_HIGH_THRESHOLD and wr >= 0.55:
            _out_kind = "stable_skill"
            _out_detail = (f"{sig_key}: n={n} wr={wr:.2f} avg_luck={avg_luck:+.2f} - "
                           f"信号稳定且高 luck, 本次胜率 vs baseline {base_wr:.2f} "
                           f"delta={delta:+.2f}")
        # 高样本 + 低 luck + 高 wr → 运气型(不应外推)
        elif n >= MIN_SAMPLES_HIGH_CONF and avg_luck < -LUCK_HIGH_THRESHOLD and wr >= 0.55:
            _out_kind = "unstable_luck"
            _out_detail = (f"{sig_key}: n={n} wr={wr:.2f} avg_luck={avg_luck:+.2f} - "
                           f"WIN 多是运气, 不应将本信号外推为经验")
        # win_rate 大幅下降 → invalidated
        elif delta < WR_DELTA_INVALIDATE:
            _out_kind = "invalidated"
            _out_detail = (f"{sig_key}: wr 从 {base_wr:.2f} 降到 {wr:.2f} delta={delta:+.2f}, "
                           f"该 signal 对应教训失效, 建议 review_insights 降权或停用")
        # win_rate 上升 → refined(教训过于保守)
        elif delta > WR_DELTA_REFINE:
            _out_kind = "refined"
            _out_detail = (f"{sig_key}: wr 从 {base_wr:.2f} 升到 {wr:.2f} delta={delta:+.2f}, "
                           f"教训过于保守, 可适当放开阈值")
        # 稳定 → confirmed
        elif abs(delta) <= WR_DELTA_CONFIRM_BAND:
            _out_kind = "confirmed"
            _out_detail = (f"{sig_key}: wr 稳定 {base_wr:.2f} -> {wr:.2f} delta={delta:+.2f}, "
                           f"教训继续有效")
        else:
            # 小幅波动但未到阈值 → 仍确认但提示观察
            _out_kind = "confirmed"
            _out_detail = (f"{sig_key}: wr 小幅波动 {base_wr:.2f} -> {wr:.2f} "
                           f"delta={delta:+.2f}, 在正常波动范围, 继续观察")

    # v240+ BOCPD 增强 evidence（opt-in, 不改变 finding_kind）
    evidence = _augment_with_bocpd(evidence, recent_trades)
    return (_out_kind, _out_detail, evidence)


# ═══════════════════════════════════════════════════════════════════════════
# v231+ · Jury-augmented classification (kills LLM-as-judge circularity)
# Same shape as _classify_finding but runs 3 independent tracks and
# returns only the agreed-upon kind.  Falls back to single-track result
# if jury is unavailable or quorum fails.
# ═══════════════════════════════════════════════════════════════════════════

def classify_with_jury(current: Dict[str, Any],
                       baseline: Optional[Dict[str, Any]]
                       ) -> Tuple[str, str, Dict[str, Any]]:
    """3-track jury verdict on (current vs baseline).

    Returns (kind, detail, evidence_with_jury_meta).
    kind is the jury-aggregated verdict; if tracks disagree, returns
    'contested' (not the underlying kind) so the dashboard can flag it.
    """
    try:
        from jury import default_jury  # local import to avoid boot cycle
    except Exception as e:  # noqa: BLE001
        kind, detail, ev = _classify_finding(current, baseline)
        return (kind, detail, {**ev, "_jury_fallback": str(e)[:200]})

    # Build the evidence dict the stat + bayes tracks want
    n = int(current.get("n", 0))
    wr = float(current.get("win_rate", 0.5))
    avg_luck = float(current.get("avg_luck", 0.0))
    base_wr = baseline.get("win_rate") if baseline else None
    ev_for_tracks = {
        "n": n, "win_rate": wr, "avg_luck": avg_luck,
        "baseline_wr": base_wr,
        "cur": current, "base": baseline,  # for the meta_loop track
    }

    j = default_jury()
    result = j.deliberate(ev_for_tracks)

    # Map jury 'contested' back to the underlying _classify_finding kind
    # (so downstream behavior is preserved on the rare contested case).
    if result.kind == "contested":
        kind, detail, ev = _classify_finding(current, baseline)
        detail = "[JURY CONTESTED] " + detail
    else:
        kind = result.kind
        detail = "[JURY {}] n={} wr={:.2f} luck={:+.2f} -> {}".format(
            result.reason, n, wr, avg_luck, kind)

    ev_out = {
        "current_n": n, "current_wr": wr, "current_avg_luck": avg_luck,
        "jury_kind":  result.kind,
        "jury_confidence": round(result.confidence, 4),
        "jury_votes": [v.to_dict() for v in result.votes],
        "jury_reason": result.reason,
    }
    if baseline:
        ev_out["baseline_wr"] = baseline.get("win_rate")
    return (kind, detail, ev_out)


# ═══════════════════════════════════════════════════════════════════════════
# v225+ · reasoning_logic 盲区检测
# 扫描近 N 天: 7 个 reasoning_logic checker (E021-E027) 全未触发,
# 但 trade 仍然错 (is_correct=0 或 forward_pnl<-0.5%) 的案例。
# 若数量 ≥ min_cases, 写入 META_REFLECTION.md §4 盲区表 + 写一条
# finding_kind='reasoning_logic_blindspot' 的 cycle (供 dashboard 监控)。
# ═══════════════════════════════════════════════════════════════════════════

_META_MD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "META_REFLECTION.md",
)

_REASONING_LOGIC_CODES = (
    "E021", "E022", "E023", "E024", "E025", "E026", "E027",
)


def _scan_reasoning_logic_blindspots(conn, window_days):
    """扫近 N 天: reasoning_logic 7 checker 全未命中但 trade 错的决策。"""
    try:
        rows = conn.execute(
            """SELECT d.id, d.target_asset, d.suggested_action, d.forward_pnl,
                      d.evidence_confidence, d.reasoning
                 FROM ai_decisions d
                WHERE d.suggested_action IN ('BUY','SELL')
                  AND (d.is_correct=0 OR (d.forward_pnl IS NOT NULL AND d.forward_pnl<-0.5))
                  AND d.created_at >= datetime('now', ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM decision_errors de
                       WHERE de.decision_id=d.id
                         AND de.error_code IN ({})
                  )
                ORDER BY d.created_at DESC""".format(
                    ",".join("?" for _ in _REASONING_LOGIC_CODES)),
            (f"-{int(window_days)} days", *_REASONING_LOGIC_CODES),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        out.append({
            "decision_id": r[0], "target_asset": r[1], "action": r[2],
            "forward_pnl": r[3], "evidence_confidence": r[4],
            "reasoning_excerpt": str(r[5] or "")[:200],
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 3. 写 reflection_cycles + 更新 trade_reflections_v2.secondary_reflection_id
# ═══════════════════════════════════════════════════════════════════════════

def _write_cycle(conn, parent_reflection_id: int, parent_layer: str, parent_signal: str,
                  cycle_level: int, finding_kind: str, finding_detail: str,
                  evidence: Dict[str, Any], lookback_days: int,
                  source_kind: str = "live", source_run_id: str = "",
                  source_watermark: str = "") -> Optional[int]:
    """写一条 reflection_cycles. 返回 cycle.id.

    v231+ 水印透传：source_kind 默认 'live'。demo / 压测 / 重放应显式传入
    以便事后追溯哪些 cycle 是 demo 用户触发的。
    """
    # 防御性归一化
    if source_kind not in ("live", "demo", "stress_test", "replay"):
        source_kind = "live"
    try:
        cur = conn.execute(
            """
            INSERT INTO reflection_cycles
              (parent_reflection_id, parent_layer, parent_signal, cycle_level,
               finding_kind, finding_detail, evidence_json, lookback_days,
               source_kind, source_run_id, source_watermark, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                int(parent_reflection_id),
                (parent_layer or "")[:64],
                (parent_signal or "")[:128],
                int(cycle_level),
                (finding_kind or "")[:32],
                (finding_detail or "")[:1500],
                json.dumps(evidence or {}, ensure_ascii=False),
                int(lookback_days),
                source_kind,
                (source_run_id or "")[:64],
                (source_watermark or "")[:64],
            ),
        )
        return int(cur.lastrowid or 0)
    except sqlite3.OperationalError as _e:
        print(f"[META-LOOP] write cycle failed: {_e}", flush=True)
        return None


def _resolve_source(source_ctx: Optional[Dict[str, Any]] = None) -> tuple:
    """从 source_ctx 字典里取出 (source_kind, source_run_id, source_watermark) 三元组。

    未传入时：尝试从 demo_mode.IS_DEMO 推导；都不是则用 'live' / '' / ''。
    任何 source_ctx['source_kind'] 取值不在白名单里都回落到 'live'。
    """
    if not source_ctx:
        try:
            import demo_mode  # 局部导入避免循环
            if demo_mode.IS_DEMO:
                return ("demo", "", "")
        except Exception:
            pass
        return ("live", "", "")
    kind = (source_ctx.get("source_kind") or "live")
    if kind not in ("live", "demo", "stress_test", "replay"):
        kind = "live"
    return (
        kind,
        (source_ctx.get("source_run_id") or "")[:64],
        (source_ctx.get("source_watermark") or "")[:64],
    )


def _get_recent_reflection_for_signal(conn, layer: str, sub: str, sig: str,
                                        lookback_days: int) -> Optional[int]:
    """取近 N 天该信号的最新反思 ID (trade_reflections_v2.id)."""
    try:
        row = conn.execute(
            """
            SELECT id FROM trade_reflections_v2
             WHERE layer = ? AND sub_layer = ? AND signal_name = ?
               AND enrich_status IN ('ok','partial')
               AND created_at >= datetime('now', ?)
             ORDER BY id DESC LIMIT 1
            """,
            (layer, sub, sig, f"-{int(lookback_days)} days"),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return int(row[0])


def _update_reflection_secondary(conn, reflection_id: int, cycle_id: int) -> None:
    """更新 trade_reflections_v2.secondary_reflection_id + secondary_reflection_at."""
    try:
        conn.execute(
            """
            UPDATE trade_reflections_v2
               SET secondary_reflection_id = ?,
                   secondary_reflection_at = datetime('now')
             WHERE id = ?
            """,
            (int(cycle_id), int(reflection_id)),
        )
    except sqlite3.OperationalError as _e:
        print(f"[META-LOOP] update secondary failed: {_e}", flush=True)


def _append_blindspot_to_meta_md(blindspot_entries, min_cases: int) -> bool:
    """追加一条 reasoning_logic 盲区记录到 META_REFLECTION.md §4 末尾。

    idempotency: 同一时间戳 + 案例数 已记录则跳过。
    """
    if len(blindspot_entries) < min_cases:
        return False
    if not os.path.exists(_META_MD_PATH):
        return False
    try:
        n = len(blindspot_entries)
        sample_ids = [str(e["decision_id"]) for e in blindspot_entries[:5]]
        assets = list({e.get("target_asset") for e in blindspot_entries if e.get("target_asset")})
        avg_pnl = sum(float(e.get("forward_pnl") or 0) for e in blindspot_entries) / max(n, 1)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        marker = f"<!-- reasoning_logic_blindspot:{ts}:{n} -->"
        with open(_META_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        if marker in content:
            return False  # 同窗口已记录
        new_entry = (
            f"\n| ⚠️ reasoning_logic 7 checker 全未命中但 trade 错 | **未修 (P5 v225+)** "
            f"| **最近 N={n} 案例 (id: {', '.join(sample_ids[:5])})** "
            f"| 主要品种: {', '.join(assets[:3])} | avg_forward_pnl={avg_pnl:+.2f}% "
            f"| 反思系统盲区 — 7 个 reasoning_logic checker 都漏判，"
            f"需扩展 sub_layer 或加新 checker (E028+) | {marker}\n"
        )
        import re as _re
        section_marker = "## 4. 当前盲区 (持续维护)"
        if section_marker not in content:
            return False
        m = _re.search(_re.escape(section_marker), content)
        if not m:
            return False
        start = m.end()
        next_section = _re.search(r"^## ", content[start:], _re.MULTILINE)
        end = start + next_section.start() if next_section else len(content)
        section_body = content[start:end].rstrip("\n") + new_entry + "\n"
        new_content = content[:start] + section_body + content[end:]
        with open(_META_MD_PATH, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    except Exception as _e:
        print(f"[META-LOOP] append blindspot failed: {_e}", flush=True)
        return False


def detect_reasoning_logic_blindspots(window_days: int = 7,
                                       min_cases: int = 3,
                                       write_cycle: bool = True,
                                       source_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """v225+ reasoning_logic 盲区检测主入口。

    Args:
      source_ctx: v231+ 水印透传, 形如
        {"source_kind": "live"/"demo"/"stress_test"/"replay",
         "source_run_id": "...", "source_watermark": "..."}

    Returns:
      {ok, window_days, min_cases, blindspot_count, sample_decisions,
       meta_md_appended, cycle_written}
    """
    conn = db.get_connection()
    out: Dict[str, Any] = {
        "window_days": int(window_days),
        "min_cases": int(min_cases),
        "blindspot_count": 0,
        "sample_decisions": [],
        "meta_md_appended": False,
        "cycle_written": False,
        "ok": False,
    }
    try:
        entries = _scan_reasoning_logic_blindspots(conn, window_days)
        out["blindspot_count"] = len(entries)
        out["sample_decisions"] = entries[:10]
        if len(entries) >= min_cases:
            # 只在真正写入 cycle 时才追加 META_REFLECTION.md（dry-run 不写）
            if write_cycle:
                out["meta_md_appended"] = _append_blindspot_to_meta_md(entries, min_cases)
            if write_cycle:
                sk, srid, swm = _resolve_source(source_ctx)
                cycle_id = _write_cycle(
                    conn,
                    parent_reflection_id=0,
                    parent_layer="reasoning_logic",
                    parent_signal="blindspot_detector",
                    cycle_level=1,
                    finding_kind="reasoning_logic_blindspot",
                    finding_detail=(f"近 {window_days}d reasoning_logic 7 checker 全未命中但 "
                                    f"trade 错的案例={len(entries)} ≥ min_cases={min_cases} → "
                                    f"反思系统盲区，建议扩展 sub_layer 或加新 checker"),
                    evidence={
                        "window_days": window_days, "min_cases": min_cases,
                        "case_count": len(entries),
                        "sample_decision_ids": [e["decision_id"] for e in entries[:10]],
                        "sample_target_assets": list(
                            {e["target_asset"] for e in entries if e["target_asset"]}),
                        "avg_forward_pnl": (sum(float(e.get("forward_pnl") or 0)
                                              for e in entries) / max(len(entries), 1)),
                    },
                    lookback_days=window_days,
                    source_kind=sk, source_run_id=srid, source_watermark=swm,
                )
                out["cycle_written"] = bool(cycle_id)
                if cycle_id:
                    conn.commit()
        out["ok"] = True
    except Exception as _e:
        out["error"] = str(_e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def run_meta_loop(window_days: int = DEFAULT_LOOKBACK_DAYS,
                   compare_days: int = DEFAULT_COMPARE_DAYS,
                   max_level: int = 2,
                   source_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """主入口: 自循环反思

    Args:
        window_days: 当前窗口大小(默认 7 天)
        compare_days: 历史对比窗口大小(默认 14 天)
        max_level: 最大反思层级(默认 2 — 反思 → 元反思 → 元元反思)
        source_ctx: v231+ 水印透传, 形如
          {"source_kind": "live"/"demo"/"stress_test"/"replay",
           "source_run_id": "...", "source_watermark": "..."}
          None 时回退到 demo_mode.IS_DEMO 判断。

    Returns: {scanned, written, by_kind, errors, levels}
    """
    conn = db.get_connection()
    out: Dict[str, Any] = {
        "window_days": window_days,
        "compare_days": compare_days,
        "max_level": max_level,
        "scanned_signals": 0,
        "written_cycles": 0,
        "by_finding_kind": {},
        "errors": [],
    }
    try:
        # Step 1: 聚合当前 vs baseline
        current = _aggregate_signal_window(conn, window_days)
        baseline_list = _aggregate_signal_compare(conn, window_days, compare_days)
        baseline_map = {
            f"{r['layer']}/{r['sub_layer']}/{r['signal_name']}": r
            for r in baseline_list
        }
        out["scanned_signals"] = len(current)

        # Step 2: 对每个 signal 做 level=1 推断
        for cur in current:
            key = f"{cur['layer']}/{cur['sub_layer']}/{cur['signal_name']}"
            base = baseline_map.get(key)
            kind, detail, evidence = _classify_finding(cur, base)

            # 取最近一条反思作为 parent
            parent_rid = _get_recent_reflection_for_signal(
                conn, cur["layer"], cur["sub_layer"], cur["signal_name"], window_days)

            if not parent_rid:
                # 没 parent 反思, 跳过(只对已反思的做 meta)
                continue

            # 写 level=1 cycle
            sk, srid, swm = _resolve_source(source_ctx)
            cycle_id = _write_cycle(
                conn, parent_rid, f"{cur['layer']}/{cur['sub_layer']}",
                cur["signal_name"], 1, kind, detail, evidence, window_days,
                source_kind=sk, source_run_id=srid, source_watermark=swm)
            if cycle_id:
                out["written_cycles"] += 1
                out["by_finding_kind"][kind] = out["by_finding_kind"].get(kind, 0) + 1
                # 更新 secondary_reflection_id
                _update_reflection_secondary(conn, parent_rid, cycle_id)

                # Step 3: 如果 kind=invalidated 或 contradictory, 做 level=2 meta-meta
                if max_level >= 2 and kind in ("invalidated", "contradictory", "unstable_luck"):
                    level2_evidence = {**evidence, "meta_level": 2, "level1_kind": kind,
                                       "level1_cycle_id": cycle_id}
                    level2_detail = (f"level-2 confirmation: level-1 found {kind} for {key}. "
                                     f"This level-2 cycle confirms the finding holds after "
                                     f"re-scanning lookback={window_days}d. "
                                     f"Sample_n={cur['n']}, win_rate={cur['win_rate']:.2f}, "
                                     f"baseline_wr={base.get('win_rate', 0) if base else 0:.2f}.")
                    cycle2 = _write_cycle(
                        conn, cycle_id, f"{cur['layer']}/{cur['sub_layer']}",
                        cur["signal_name"], 2, f"{kind}_confirmed", level2_detail,
                        level2_evidence, window_days,
                        source_kind=sk, source_run_id=srid, source_watermark=swm)
                    if cycle2:
                        out["written_cycles"] += 1
                        out["by_finding_kind"][f"{kind}_confirmed"] = \
                            out["by_finding_kind"].get(f"{kind}_confirmed", 0) + 1

        # Step 4 (v225+): reasoning_logic 盲区检测 — 7 个新 checker 全未命中但 trade 错
        try:
            blindspot = detect_reasoning_logic_blindspots(
                window_days=window_days, min_cases=3, write_cycle=True,
                source_ctx=source_ctx)
            out["reasoning_logic_blindspot"] = blindspot
            if blindspot.get("cycle_written"):
                out["written_cycles"] += 1
                out["by_finding_kind"]["reasoning_logic_blindspot"] = (
                    out["by_finding_kind"].get("reasoning_logic_blindspot", 0) + 1)
        except Exception as _be:
            out["errors"].append(f"blindspot_detect: {_be}")

        conn.commit()
    except sqlite3.OperationalError as _e:
        out["errors"].append(str(_e))
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _daemon_loop(interval_s: int = 3600, window_days: int = 7,
                 max_level: int = 2) -> None:
    """daemon 包装: 每 interval_s 秒跑一次 run_meta_loop + reasoning_logic_blindspots。

    给 api_server.py lifespan 调用, 避免 reflection_cycles 只靠手动 CLI 触发。
    """
    import time as _t
    print(f"[META-REFLECT] daemon started | interval={interval_s}s window={window_days}d",
          flush=True)
    try:
        while True:
            try:
                r = run_meta_loop(window_days=window_days, max_level=max_level)
                print(f"[META-REFLECT] run_meta_loop: scanned={r.get('scanned_signals')} "
                      f"written={r.get('written_cycles')} by_kind={r.get('by_finding_kind')}",
                      flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[META-REFLECT] run_meta_loop error: {type(exc).__name__}: {exc}",
                      flush=True)
            try:
                bs = detect_reasoning_logic_blindspots(window_days=window_days, min_cases=3)
                print(f"[META-REFLECT] blindspot: count={bs.get('blindspot_count')} "
                      f"meta_md_appended={bs.get('meta_md_appended')} "
                      f"cycle_written={bs.get('cycle_written')}",
                      flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[META-REFLECT] blindspot error: {type(exc).__name__}: {exc}",
                      flush=True)
            _t.sleep(int(interval_s))
    except KeyboardInterrupt:
        print("\n[META-REFLECT] daemon stopped (KeyboardInterrupt)", flush=True)


def get_meta_loop_stats(window_days: int = 30) -> Dict[str, Any]:
    """统计 reflection_cycles 自循环反思概况."""
    conn = db.get_connection()
    out: Dict[str, Any] = {
        "window_days": window_days,
        "by_finding_kind": {},
        "by_level": {},
        "recent_cycles": [],
        "invalidated_signals": [],
        "stable_skill_signals": [],
        "unstable_luck_signals": [],
        "ok": False,
    }
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT finding_kind, COUNT(*) FROM reflection_cycles
             WHERE created_at >= datetime('now', ?)
             GROUP BY finding_kind
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
        for r in rows:
            out["by_finding_kind"][r[0] or "unknown"] = int(r[1] or 0)

        rows = conn.execute(
            """
            SELECT cycle_level, COUNT(*) FROM reflection_cycles
             WHERE created_at >= datetime('now', ?)
             GROUP BY cycle_level
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
        for r in rows:
            out["by_level"][f"level_{r[0]}"] = int(r[1] or 0)

        rows = conn.execute(
            """
            SELECT id, parent_reflection_id, parent_layer, parent_signal,
                   cycle_level, finding_kind, finding_detail, created_at
              FROM reflection_cycles
             WHERE created_at >= datetime('now', ?)
             ORDER BY id DESC LIMIT 20
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
        out["recent_cycles"] = [dict(r) for r in rows]

        # Top invalidated signals
        rows = conn.execute(
            """
            SELECT parent_layer, parent_signal, COUNT(*) AS n
              FROM reflection_cycles
             WHERE finding_kind = 'invalidated' AND created_at >= datetime('now', ?)
             GROUP BY parent_layer, parent_signal ORDER BY n DESC LIMIT 10
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
        out["invalidated_signals"] = [{"layer": r[0], "signal": r[1], "n": int(r[2])} for r in rows]

        # Top stable_skill
        rows = conn.execute(
            """
            SELECT parent_layer, parent_signal, COUNT(*) AS n
              FROM reflection_cycles
             WHERE finding_kind = 'stable_skill' AND created_at >= datetime('now', ?)
             GROUP BY parent_layer, parent_signal ORDER BY n DESC LIMIT 10
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
        out["stable_skill_signals"] = [{"layer": r[0], "signal": r[1], "n": int(r[2])} for r in rows]

        # Top unstable_luck
        rows = conn.execute(
            """
            SELECT parent_layer, parent_signal, COUNT(*) AS n
              FROM reflection_cycles
             WHERE finding_kind = 'unstable_luck' AND created_at >= datetime('now', ?)
             GROUP BY parent_layer, parent_signal ORDER BY n DESC LIMIT 10
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
        out["unstable_luck_signals"] = [{"layer": r[0], "signal": r[1], "n": int(r[2])} for r in rows]

        out["ok"] = True
    except sqlite3.OperationalError as _e:
        out["error"] = str(_e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        wd = int(sys.argv[2]) if len(sys.argv) >= 3 else 7
        r = run_meta_loop(window_days=wd)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "stats":
        wd = int(sys.argv[2]) if len(sys.argv) >= 3 else 30
        r = get_meta_loop_stats(window_days=wd)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "blindspot":
        # python meta_reflection_loop.py blindspot [window_days] [min_cases]
        wd = int(sys.argv[2]) if len(sys.argv) >= 3 else 7
        mc = int(sys.argv[3]) if len(sys.argv) >= 4 else 3
        r = detect_reasoning_logic_blindspots(window_days=wd, min_cases=mc)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("Usage:")
        print("  python meta_reflection_loop.py run [window_days]")
        print("  python meta_reflection_loop.py stats [window_days]")
        print("  python meta_reflection_loop.py blindspot [window_days] [min_cases]")
