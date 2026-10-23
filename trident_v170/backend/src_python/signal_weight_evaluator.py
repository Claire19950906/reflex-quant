# -*- coding: utf-8 -*-
"""
v20251014 signal weight evaluator (6-dimensional composite)

Problem: ai_decisions.sentiment_score (LLM self-rating 0-1) alone cannot fully
describe "the probability this signal moves the market". This module produces
a 0-1 composite signal_weight:

    weight = base * consensus * source * reaction * regime * time

Six dimensions:
  1. base        LLM raw strength (from sentiment_score + impact_magnitude + heat)
  2. consensus   multi-model agreement (extra_models_consensus JSON parse)
  3. source      historical hit-rate (source_stats cache, closed-loop calibration)
  4. reaction    real-time market reaction (recent abnormal_signals)
  5. regime      current market style (trending / range / volatile)
  6. time        time-decay (1h/2h/6h/24h/7d tiers)

Use cases:
  - ai_worker INSERT ai_decisions -> evaluate immediately (no reaction yet, =1.0)
  - signal_weight_cron every 5min -> recompute (includes reaction)
  - Frontend K-line marker color/depth, signal card ranking, confidence display
  - After settlement: forward_pnl updates source_stats (closed-loop)

Performance: single evaluate < 1ms; batch N < N*1ms
Dependencies: only sqlite3 + json + db.py (no external API)
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional


# ============================================================================
# 6-dim coefficient functions (each 5-30 lines, easy to unit-test)
# ============================================================================

# 2026-10-22: 反应信号观察窗口。决策后 300s 内出现的价格反应才算作
# 本次决策的 reaction 证据; 超出窗口的信号与本次决策无关。
REACTION_WINDOW_SEC = 300


def _base_mult(sentiment_score, impact_magnitude, heat_score):
    """1. LLM raw strength coefficient (0.3 - 1.5)

    sentiment_score: 0-1 LLM self-rating (0.5=neutral)
    impact_magnitude: 1-100 LLM impact rating (optional)
    heat_score: 0-1 attention heat (optional)
    """
    if sentiment_score is None:
        return 1.0
    try:
        s = float(sentiment_score)
    except (TypeError, ValueError):
        return 1.0
    intensity = abs(s - 0.5) * 2.0
    if impact_magnitude is not None and impact_magnitude > 0:
        try:
            imp = min(1.0, max(0.1, float(impact_magnitude) / 100.0))
            intensity = intensity * (0.5 + 0.5 * imp)
        except (TypeError, ValueError):
            pass
    if heat_score is not None:
        try:
            h = max(0.0, min(1.0, float(heat_score)))
            intensity = intensity * (0.5 + h)
        except (TypeError, ValueError):
            pass
    return max(0.3, min(1.5, 0.5 + intensity))


def _model_family(model_id):
    """Normalise a model identifier to a provider family.

    The peer reviewer currently re-invokes the *same* model as the primary
    (model_id "MiniMax-M3" vs "minimax/MiniMax-M3"), so provider-prefix
    stripping is required to detect that there is only one model in play.
    """
    m = str(model_id or "").strip().lower()
    if not m:
        return ""
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    return m


def _consensus_mult(extra_models_consensus, main_action=None):
    """2. multi-model consensus coefficient (0.3 - 1.3)

    Accepts three real-world encodings:

    * legacy tokens: "AGREE" / "DISAGREE" / "MIXED" / "CONFLICT"
    * dict form:    {"v1": {"agree": true}, "v2": {"agree": false}}
    * list form:    [{"model_id": ..., "action": "BUY"}, ...]  <- actual
      production payload written to ai_decisions.peer_reviews

    Guards (all deliberately fail closed to 1.0, never to a boost):

    * a single reviewer cannot corroborate the main model, so consensus is
      undefined -> 1.0;
    * reviewers that are the same underlying model as each other (or as the
      main model) are collapsed into one vote, otherwise one model asking
      itself twice would manufacture unanimous "consensus";
    * when the main action is known, an action must actually match it --
      unanimous agreement on a different action is disagreement, not consent.
    """
    if not extra_models_consensus:
        return 1.0
    s = str(extra_models_consensus).strip()
    if s.upper() in ("AGREE", "ALL_AGREE"):
        return 1.3
    if s.upper() in ("DISAGREE", "MIXED", "CONFLICT"):
        return 0.6

    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return 1.0

    votes = []
    if isinstance(data, dict):
        # legacy {"v1": {"agree": bool}} shape
        for _k, v in data.items():
            if isinstance(v, dict) and "agree" in v:
                votes.append(bool(v["agree"]))
    elif isinstance(data, list):
        # production peer_reviews shape: per-model action, deduped by family
        seen, main = set(), str(main_action or "").strip().upper()
        for entry in data:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("action") or "").strip().upper()
            if not action:
                continue
            # dedup by model family *before* scoring, so one model asking
            # itself twice cannot be counted as two disagreeing voices
            fam = _model_family(entry.get("model_id") or entry.get("model_label"))
            if fam:
                if fam in seen:
                    continue
                seen.add(fam)
            votes.append(not (main and action != main))
    else:
        return 1.0

    # one lone reviewer (or one lone model family) proves nothing
    if len(votes) < 2:
        return 1.0
    ratio = sum(votes) / len(votes)
    return max(0.3, min(1.3, 0.3 + ratio * 1.0))


def _source_mult_from_cache(source_stats):
    """3. source hit-rate coefficient (0.3 - 1.5) from source_stats cache"""
    if not source_stats:
        return 1.0
    sample_n = int(source_stats.get("sample_n", 0) or 0)
    if sample_n < 5:
        return 1.0
    hit = float(source_stats.get("hit_rate", 0.5) or 0.5)
    return max(0.3, min(1.5, 0.5 + hit))


def _reaction_mult(abnormal):
    """4. real-time market reaction coefficient (0.5 - 1.5)"""
    if not abnormal:
        return 1.0
    if not abnormal.get("triggered"):
        return 1.0
    try:
        pct = float(abnormal.get("pct_change") or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        vol = float(abnormal.get("vol_ratio") or 1.0)
    except (TypeError, ValueError):
        vol = 1.0
    aligned = abnormal.get("aligned", True)
    if not aligned:
        if abs(pct) >= 0.3:
            return 0.5
        return 0.7
    if abs(pct) >= 0.5 and vol >= 1.5:
        return 1.4
    if abs(pct) >= 0.3 and vol >= 1.2:
        return 1.2
    if abs(pct) >= 0.3:
        return 1.2
    if abs(pct) >= 0.1:
        return 1.1
    if abs(pct) < 0.2:
        return 0.9
    # 2026-10-22 修正: 原代码在 0.2 <= |pct| < 0.3 且 vol < 1.2 的区间
    # 没有任何 return, 会隐式返回 None, 令 compute_signal_weight 的
    # 连乘直接 TypeError。这里显式兜底, 保证返回值永远落在 0.5~1.5。
    return 1.0


def _regime_mult(regime):
    """5. market style coefficient (0.7 - 1.2)"""
    r = (regime or "").strip().lower()
    if r in ("trending_up", "trending_down", "trending", "trend"):
        return 1.2
    if r in ("range_bound", "range", "sideways", "consolidation"):
        return 0.85
    if r in ("volatile", "high_vol", "choppy"):
        return 0.7
    return 1.0


def _time_mult(decision_ts, now_ts=None, expected_horizon=""):
    """6. time-decay coefficient (0.5 - 1.0)"""
    if not decision_ts:
        return 1.0
    now = int(now_ts or time.time())
    age_sec = max(0, now - int(decision_ts))
    age_h = age_sec / 3600.0
    if "intra" in (expected_horizon or "").lower():
        if age_h >= 4: return 0.5
        if age_h >= 2: return 0.7
        if age_h >= 1: return 0.85
        return 1.0
    if "1-3d" in (expected_horizon or "") or "short" in (expected_horizon or "").lower():
        if age_h >= 48: return 0.5
        if age_h >= 24: return 0.65
        if age_h >= 6: return 0.8
        if age_h >= 2: return 0.9
        return 1.0
    if age_h >= 168: return 0.5
    if age_h >= 24: return 0.65
    if age_h >= 6: return 0.8
    if age_h >= 2: return 0.9
    return 1.0


# ============================================================================
# Main evaluate function + tier helper
# ============================================================================


def _to_tier(weight):
    """weight -> tier label (used for frontend color/size mapping)"""
    if weight >= 0.7:
        return "strong"
    if weight >= 0.3:
        return "medium"
    if weight >= 0.1:
        return "weak"
    return "ignored"


def compute_signal_weight(decision, *, source_stats=None, abnormal=None, now_ts=None):
    """Compute signal_weight for a single decision (main entry point)

    Args:
        decision: ai_decisions row dict, at minimum containing:
            - sentiment_score (0-1)
            - impact_magnitude (1-100, optional)
            - heat_score (0-1, optional)
            - extra_models_consensus (str, optional)
            - regime_at_decision (str, optional)
            - ts (int, UTC sec, for time decay)
            - expected_horizon (str)
        source_stats: source_stats row dict (source x asset history, optional)
        abnormal: abnormal_signals row dict (recent 5min reaction, optional)
        now_ts: current timestamp (test-injectable; default time.time())

    Returns:
        {
            "signal_weight": 0.42,            # 0-1 composite
            "weight_tier":   "medium",
            "components": {                    # 6-dim breakdown (debug + tooltip)
                "base": 0.9, "consensus": 1.2, "source": 1.0,
                "reaction": 1.0, "regime": 1.0, "time": 0.9,
            },
            "explain": "base=0.90 * cons=1.20 * src=1.00 * react=1.00 * reg=1.00 * time=0.90 = 0.94",
            "cold_signal": False,
        }
    """
    base = _base_mult(
        decision.get("sentiment_score"),
        decision.get("impact_magnitude"),
        decision.get("heat_score"),
    )
    cons = _consensus_mult(
        decision.get("extra_models_consensus", "") or decision.get("peer_reviews", "") or "",
        main_action=decision.get("suggested_action", "") or "",
    )
    src = _source_mult_from_cache(source_stats)
    react = _reaction_mult(abnormal)
    reg = _regime_mult(decision.get("regime_at_decision", "") or "")
    tm = _time_mult(
        decision.get("ts") or decision.get("decision_ts") or 0,
        now_ts=now_ts,
        expected_horizon=decision.get("expected_horizon", "") or "",
    )
    weight = base * cons * src * react * reg * tm
    weight = max(0.0, min(1.0, weight))
    cold = False
    if abnormal and abnormal.get("triggered"):
        try:
            pct = float(abnormal.get("pct_change") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        if abs(pct) < 0.002:  # < 0.2%
            cold = True
    components = {
        "base": round(base, 3),
        "consensus": round(cons, 3),
        "source": round(src, 3),
        "reaction": round(react, 3),
        "regime": round(reg, 3),
        "time": round(tm, 3),
    }
    explain = (
        f"base={base:.2f} * cons={cons:.2f} * src={src:.2f} * "
        f"react={react:.2f} * reg={reg:.2f} * time={tm:.2f} = {weight:.3f}"
    )
    return {
        "signal_weight": weight,
        "weight_tier": _to_tier(weight),
        "components": components,
        "explain": explain,
        "cold_signal": cold,
    }


# ============================================================================
# DB integration: read / write / recompute
# ============================================================================


def get_source_stats(conn, news_source, asset=""):
    """Read one source_stats row; None if not found or table missing."""
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM source_stats WHERE news_source=? AND asset=?",
            (news_source, asset),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return None
    if not row:
        conn.row_factory = _orig_factory
        return None
    try:
        result = dict(row)
    except Exception:
        result = None
    conn.row_factory = _orig_factory
    return result


def get_recent_abnormal(conn, asset, since_ts, until_ts=None):
    """Return most recent abnormal_signals for the asset since since_ts.

    2026-10-22 修正取样口径: abnormal_signals 共 20788 行, 其中 17685 行是
    multi_factor (85%), 而 multi_factor 的 detail_json 里完全没有 pct_change。
    原实现 `ORDER BY ts DESC LIMIT 1` 几乎必然取到 multi_factor 行, 于是
    pct/vol 恒为 0 -> triggered 恒 False -> reaction_mult 恒 1.0。
    现改为: 只在"确实带反应幅度"的行里取最新一条。

    until_ts: 反应窗口上界。原实现没有上界, 会匹配到决策之后任意远的信号,
    等于用"未来数据"污染当期权重。现要求信号必须落在
    [since_ts, until_ts] 闭区间内。
    """
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    if until_ts is None:
        until_ts = int(since_ts) + REACTION_WINDOW_SEC
    try:
        row = conn.execute(
            """SELECT id, ts, asset, signal_kind, direction, anchor_ts, detail_json,
                      hint, status
               FROM abnormal_signals
               WHERE asset=? AND ts>=? AND ts<=? AND status='open'
                 AND detail_json LIKE '%pct_change%'
               ORDER BY ts DESC LIMIT 1""",
            (asset, int(since_ts), int(until_ts)),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return None
    if not row:
        conn.row_factory = _orig_factory
        return None
    out = dict(row)
    conn.row_factory = _orig_factory
    try:
        d = json.loads(out.get("detail_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d = {}
    out["pct_change"] = d.get("pct_change")
    out["vol_ratio"] = d.get("vol_ratio")
    out["trigger_type"] = d.get("trigger_type", "")
    # 2026-10-22 修正: triggered 判据原先是 status=='open' AND anchor_ts 非空。
    # 实测 abnormal_signals 20788 行 status 全为 'open', 其中 anchor_ts 只有
    # 2406 行非空 —— 于是 88% 的行即使有真实反应也被判成"未触发",
    # reaction_mult 恒为 1.0。
    # 正确判据是"这条信号是否真的带来了反应幅度", 即 detail_json 里有可用的
    # pct_change; anchor_ts 单位还是毫秒(13位), 与 ts(10位) 不可直接比较。
    try:
        _pct = abs(float(out.get("pct_change")))
    except (TypeError, ValueError):
        _pct = 0.0
    out["triggered"] = bool(_pct > 0)
    return out


def _check_aligned(direction, suggested_action):
    """Return True if abnormal.direction matches decision.suggested_action"""
    d = (direction or "").lower()
    a = (suggested_action or "").upper()
    if d == "bullish" and a == "BUY":
        return True
    if d == "bearish" and a == "SELL":
        return True
    if d in ("", "neutral"):
        return True
    return False


def evaluate_and_persist(conn, decision_id, trigger_kind="insert", now_ts=None):
    """Evaluate signal_weight for one decision and persist (incl. history row).

    Args:
        conn: sqlite3 connection (write-enabled)
        decision_id: ai_decisions.id
        trigger_kind: 'insert' / 'cron_react' / 'cron_settle' / 'manual'
        now_ts: test-injectable timestamp

    Returns:
        evaluate result dict (None if decision doesn't exist or columns missing)
    """
    # Ensure row returns dict-like
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        dec_row = conn.execute(
            """SELECT id, news_id, target_asset, suggested_action, sentiment_score,
                      impact_magnitude, heat_score, attention_score,
                      extra_models_consensus, peer_reviews, regime_at_decision,
                      expected_horizon, ts
               FROM ai_decisions WHERE id=?""",
            (decision_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return None
    if not dec_row:
        conn.row_factory = _orig_factory
        return None
    dec = dict(dec_row)
    # Resolve news_source from raw_news
    try:
        news_row = conn.execute(
            "SELECT source FROM raw_news WHERE id=?", (dec.get("news_id"),)
        ).fetchone()
    except sqlite3.OperationalError:
        news_row = None
    if news_row is not None:
        try:
            news_source = dict(news_row).get("source", "") or ""
        except Exception:
            news_source = ""
    else:
        news_source = ""
    conn.row_factory = _orig_factory
    dec["news_source"] = news_source
    now = int(now_ts or time.time())
    src = get_source_stats(conn, news_source, dec.get("target_asset", "") or "")
    decision_ts = int(dec.get("ts") or 0)
    # 2026-10-22: 反应窗口改为 [decision_ts, decision_ts + REACTION_WINDOW_SEC]。
    # 原实现只有下界, 会把决策之后任意远的信号也算成本次决策的反应。
    window_start = decision_ts if decision_ts else now - REACTION_WINDOW_SEC
    window_end = (decision_ts + REACTION_WINDOW_SEC) if decision_ts else now
    abn = get_recent_abnormal(conn, dec.get("target_asset", "") or "",
                             window_start, window_end)
    if abn:
        abn["aligned"] = _check_aligned(
            abn.get("direction", ""), dec.get("suggested_action", "")
        )
    result = compute_signal_weight(dec, source_stats=src, abnormal=abn, now_ts=now)
    # UPDATE ai_decisions (silent fail if columns missing on old DB)
    try:
        conn.execute(
            """UPDATE ai_decisions SET
                signal_weight = ?,
                weight_tier = ?,
                weight_explain = ?,
                weight_updated_at = ?,
                cold_signal = ?
               WHERE id = ?""",
            (
                round(result["signal_weight"], 4),
                result["weight_tier"],
                result["explain"],
                now,
                1 if result["cold_signal"] else 0,
                decision_id,
            ),
        )
    except sqlite3.OperationalError:
        return None
    # INSERT signal_weight_history (silent fail if table missing)
    try:
        comp = result["components"]
        conn.execute(
            """INSERT INTO signal_weight_history
                (decision_id, computed_at, trigger_kind, signal_weight, weight_tier,
                 base_mult, consensus_mult, source_mult, reaction_mult, regime_mult, time_mult,
                 explain, abnormal_id, source_stats_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, now, trigger_kind,
                round(result["signal_weight"], 4), result["weight_tier"],
                comp["base"], comp["consensus"], comp["source"],
                comp["reaction"], comp["regime"], comp["time"],
                result["explain"],
                (abn or {}).get("id"),
                (src or {}).get("id"),
            ),
        )
    except sqlite3.OperationalError:
        pass
    return result


# 批量写事务的提交粒度（行）。见 batch_recompute / backfill_source_stats。
# 这两个函数每轮要写数百行 ai_decisions / signal_weight_history / source_stats，
# 而 Python sqlite3 在 isolation_level="" 下首次写入即开写事务、一路持有到
# commit()——整批攒完才提交会让独占写锁被占住数秒，把 api_server / engine 的
# 写入全部挤成 "database is locked"（实测单行 INSERT 失败率一度达 56%）。
# 按行边界每 N 行提交一次可把持锁时长压到常数级。
_BULK_COMMIT_EVERY = 50


def batch_recompute(conn, hours=24, trigger_kind="cron_react", now_ts=None):
    """Recompute signal_weight for all non-HOLD decisions in the last N hours.

    Returns:
        {"processed": N, "updated": M, "cold": K, "errors": E}
    """
    now = int(now_ts or time.time())
    cutoff = now - hours * 3600
    processed = 0
    updated = 0
    cold = 0
    errors = 0
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, target_asset, suggested_action, ts
               FROM ai_decisions
               WHERE ts >= ? AND suggested_action IN ('BUY','SELL')
                 AND is_sibling = 0
               ORDER BY ts DESC""",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return {"processed": 0, "updated": 0, "cold": 0, "errors": 0}
    conn.row_factory = _orig_factory
    for r in rows:
        # 在行边界处周期提交，释放累积的写事务。放在循环体开头而不是结尾，
        # 是为了不被上面的 continue 分支跳过；且此时上一行该写的都已写完，
        # 提交点始终落在两行之间，不会把任何单行的写入对劈开。
        if processed and processed % _BULK_COMMIT_EVERY == 0:
            conn.commit()
        processed += 1
        try:
            rid = r["id"] if isinstance(r, sqlite3.Row) else r[0]
            res = evaluate_and_persist(
                conn, int(rid), trigger_kind=trigger_kind, now_ts=now,
            )
            if res is None:
                errors += 1
                continue
            updated += 1
            if res.get("cold_signal"):
                cold += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[signal_weight] recompute id={r} failed: {e}", flush=True)
    return {"processed": processed, "updated": updated, "cold": cold, "errors": errors}


# ============================================================================
# Closed-loop calibration: after settlement, update source_stats with forward_pnl
# ============================================================================


def update_source_stats_from_settlement(conn, news_source, asset,
                                        forward_pnl, is_correct,
                                        now_ts=None):
    """Incrementally update source_stats based on a settled decision (EMA smoothing).

    Args:
        news_source: raw_news.source
        asset: asset code
        forward_pnl: settled PnL (%)
        is_correct: 'WIN' / 'LOSS' / 'BREAKEVEN'
        now_ts: test-injectable timestamp

    Rules:
      - WIN  -> win_n += 1
      - LOSS -> loss_n += 1
      - sample_n += 1
      - hit_rate = win_n / (win_n + loss_n)
      - avg_forward_pnl = old * 0.9 + new * 0.1  (EMA)
      - source_mult = clamp(0.5 + hit_rate, 0.3, 1.5)
    """
    if not news_source or forward_pnl is None:
        return
    now = int(now_ts or time.time())
    win_inc = 1 if (is_correct or "").upper() == "WIN" else 0
    loss_inc = 1 if (is_correct or "").upper() == "LOSS" else 0
    _orig_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT * FROM source_stats WHERE news_source=? AND asset=?",
            (news_source, asset),
        ).fetchone()
    except sqlite3.OperationalError:
        conn.row_factory = _orig_factory
        return
    conn.row_factory = _orig_factory
    if not existing:
        sample = win_inc + loss_inc
        hit = (win_inc / sample) if sample > 0 else 0.0
        try:
            conn.execute(
                """INSERT INTO source_stats
                    (news_source, asset, sample_n, win_n, loss_n,
                     avg_forward_pnl, hit_rate, source_mult, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (news_source, asset, sample, win_inc, loss_inc,
                 round(float(forward_pnl), 4), round(hit, 4),
                 round(max(0.3, min(1.5, 0.5 + hit)), 4), now),
            )
        except sqlite3.OperationalError:
            return
        return
    new_sample = int(existing["sample_n"]) + win_inc + loss_inc
    new_win = int(existing["win_n"]) + win_inc
    new_loss = int(existing["loss_n"]) + loss_inc
    closed = new_win + new_loss
    new_hit = (new_win / closed) if closed > 0 else 0.0
    old_avg = float(existing["avg_forward_pnl"] or 0.0)
    new_avg = old_avg * 0.9 + float(forward_pnl) * 0.1
    new_mult = max(0.3, min(1.5, 0.5 + new_hit))
    try:
        conn.execute(
            """UPDATE source_stats SET
                sample_n=?, win_n=?, loss_n=?, avg_forward_pnl=?,
                hit_rate=?, source_mult=?, last_updated=?
               WHERE news_source=? AND asset=?""",
            (new_sample, new_win, new_loss, round(new_avg, 4),
             round(new_hit, 4), round(new_mult, 4), now,
             news_source, asset),
        )
    except sqlite3.OperationalError:
        return


def backfill_source_stats(conn, limit=500, now_ts=None, dry_run=False):
    """v20261023: 定时回写 source_stats —— 把已结算但尚未计入的决策增量写入。

    与 update_source_stats_from_settlement() 的区别: 后者按单条决策被手动调用
    (history_review 重判定路径)，本函数是 cron 的批量扫描入口。

    幂等性由 ai_decisions.source_stats_counted_ts 保证: 计入成功后写水位，
    后续任何一次扫描都不会重复累加同一决策。没有这个水位，cron 每 5 分钟
    跑一次会把同一批决策反复累加，sample_n 虚增、hit_rate 失真。

    只统计 settled=1 且 is_correct ∈ (WIN, LOSS) 的决策 —— BREAKEVEN 不计入
    命中分母(与 update_source_stats_from_settlement 的 win+loss 口径一致)。

    Args:
        conn: sqlite3 connection
        limit: 单次最多处理多少条，避免一次锁表过久
        now_ts: 测试注入用时间戳
        dry_run: True 时只统计不写入

    Returns:
        {"scanned": N, "counted": M, "skipped": K, "keys": J, "dry_run": bool}
    """
    now = int(now_ts or time.time())
    scanned = counted = skipped = 0

    # 以 ai_decisions 为准 —— 它是权重计算真正读取的表。
    # forward_pnl IS NOT NULL 必须放在 SQL 层: update_source_stats_from_settlement()
    # 需要 pnl 参与 EMA，若把这类行放进 Python 再跳过，它们永远拿不到水位，
    # 每轮都会被重新扫描并占用 limit 配额(实测 scanned 恒为 1)。
    try:
        rows = conn.execute(
            """SELECT ad.id, ad.news_id, ad.target_asset, ad.is_correct,
                      ad.forward_pnl, rn.source AS news_source
               FROM ai_decisions ad
               LEFT JOIN raw_news rn ON rn.id = ad.news_id
               WHERE ad.settled = 1
                 AND ad.is_correct IN ('WIN', 'LOSS')
                 AND ad.forward_pnl IS NOT NULL
                 AND COALESCE(ad.source_stats_counted_ts, 0) = 0
                 AND rn.source IS NOT NULL AND rn.source != ''
               ORDER BY ad.id
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        # 列尚未 migrate() 补齐时不静默跳过，交由调用方感知。
        raise

    for r in rows:
        # 同 batch_recompute: 在行边界处周期提交, 不把最多 limit(默认 500) 行
        # 的写入攒成一个长事务。dry_run 不写库, 也就无需提交。
        # 提交点始终在两行之间 —— 同一行的 source_stats 计数与 source_stats_counted_ts
        # 水位仍在同一事务内, 崩溃时一起回滚, 不会出现"已计数但无水位"的重复累加。
        if not dry_run and scanned and scanned % _BULK_COMMIT_EVERY == 0:
            conn.commit()
        scanned += 1
        d = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "news_id": r[1], "target_asset": r[2],
            "is_correct": r[3], "forward_pnl": r[4], "news_source": r[5],
        }
        pnl = d.get("forward_pnl")
        if not dry_run:
            if pnl is None:
                # 防御性分支: SQL 已过滤, 正常不会到这里。仍然写水位以维持
                # "扫到的行必被消费" 的不变量, 避免异常数据反复占用配额。
                conn.execute(
                    "UPDATE ai_decisions SET source_stats_counted_ts=? "
                    "WHERE id=?", (now, d["id"]),
                )
                skipped += 1
                continue
            update_source_stats_from_settlement(
                conn,
                news_source=d.get("news_source") or "",
                asset=d.get("target_asset") or "",
                forward_pnl=pnl,
                is_correct=d.get("is_correct") or "",
                now_ts=now,
            )
            # 水位与计数写在同一事务内(本行两次写入之间不提交): 崩溃时两者一起
            # 回滚, 不会出现"已计数但无水位"导致的重复累加。整批在每
            # _BULK_COMMIT_EVERY 行边界处提交一次, 以缩短写锁持有时间。
            conn.execute(
                "UPDATE ai_decisions SET source_stats_counted_ts=? WHERE id=?",
                (now, d["id"]),
            )
        elif pnl is None:
            skipped += 1
        counted += 1

    keys = 0
    try:
        keys = conn.execute("SELECT COUNT(*) FROM source_stats").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    return {"scanned": scanned, "counted": counted, "skipped": skipped,
            "keys": keys, "dry_run": bool(dry_run)}


def _self_test_consensus():
    """2026-10-22 regression tests for the consensus_mult fix.

    Covers the two production defects: (a) _consensus_mult could not parse the
    list-shaped ai_decisions.peer_reviews payload at all, and (b) a lone
    reviewer / one model asked twice must not be read as multi-model consensus.
    """
    import json as _json

    # legacy encodings must keep working
    assert _consensus_mult("") == 1.0
    assert _consensus_mult("AGREE") == 1.3
    assert _consensus_mult("DISAGREE") == 0.6
    legacy = _json.dumps({"v1": {"agree": True}, "v2": {"agree": True}})
    assert _consensus_mult(legacy) == 1.3
    legacy_mixed = _json.dumps({"v1": {"agree": True}, "v2": {"agree": False}})
    assert abs(_consensus_mult(legacy_mixed) - 0.8) < 1e-9

    # (a) the real production list shape must now parse at all
    two_models = _json.dumps([
        {"model_id": "deepseek-chat", "action": "BUY"},
        {"model_id": "gpt-4o", "action": "BUY"},
    ])
    assert _consensus_mult(two_models, main_action="BUY") == 1.3
    split = _json.dumps([
        {"model_id": "deepseek-chat", "action": "BUY"},
        {"model_id": "gpt-4o", "action": "SELL"},
    ])
    # 1 agree / 1 disagree -> ratio 0.5 -> 0.8
    assert abs(_consensus_mult(split, main_action="BUY") - 0.8) < 1e-9

    # (b) one lone reviewer is not consensus, even when it agrees
    solo = _json.dumps([{"model_id": "gpt-4o", "action": "BUY"}])
    assert _consensus_mult(solo, main_action="BUY") == 1.0

    # the same model twice (provider-prefixed or not) is still one voice
    same = _json.dumps([
        {"model_id": "minimax/MiniMax-M3", "action": "HOLD"},
        {"model_id": "MiniMax-M3", "action": "HOLD"},
    ])
    assert _consensus_mult(same, main_action="HOLD") == 1.0

    # all reviewers agreeing on a *different* action is disagreement
    disagree_all = _json.dumps([
        {"model_id": "deepseek-chat", "action": "SELL"},
        {"model_id": "gpt-4o", "action": "SELL"},
    ])
    assert abs(_consensus_mult(disagree_all, main_action="BUY") - 0.3) < 1e-9

    # malformed / unusable payloads fail closed to 1.0
    assert _consensus_mult("[not json") == 1.0
    assert _consensus_mult(_json.dumps([{"model_id": "x"}])) == 1.0
    assert _consensus_mult(_json.dumps("scalar")) == 1.0
    # every result must stay inside the documented band
    for payload in (two_models, split, solo, same, disagree_all):
        for act in ("BUY", "HOLD", ""):
            v = _consensus_mult(payload, main_action=act)
            assert 0.3 <= v <= 1.3, (payload, act, v)

    # end-to-end: compute_signal_weight must consume peer_reviews
    d = {"sentiment_score": 0.5, "suggested_action": "BUY", "ts": 1792000000,
         "peer_reviews": two_models, "extra_models_consensus": ""}
    r = compute_signal_weight(d, now_ts=1792000000)
    assert r["components"]["consensus"] == 1.3, r["components"]

    # and must fall back to 1.0 (not a boost) when the reviewers are one model
    d2 = dict(d, peer_reviews=same)          # same model twice, agreeing
    r2 = compute_signal_weight(d2, now_ts=1792000000)
    assert r2["components"]["consensus"] == 1.0, r2["components"]

    # one model disagreeing twice with itself is one dissent, not two:
    # ratio 0/1 -> lone vote -> 1.0
    self_dis = _json.dumps([
        {"model_id": "minimax/MiniMax-M3", "action": "SELL"},
        {"model_id": "MiniMax-M3", "action": "SELL"},
    ])
    assert _consensus_mult(self_dis, main_action="BUY") == 1.0
    # ...but that model plus one genuinely different model dissenting is real
    # consensus: 0 agree / 2 disagree -> ratio 0 -> 0.3
    self_dis_two = _json.dumps([
        {"model_id": "minimax/MiniMax-M3", "action": "SELL"},
        {"model_id": "MiniMax-M3", "action": "SELL"},
        {"model_id": "deepseek-chat", "action": "SELL"},
    ])
    assert abs(_consensus_mult(self_dis_two, main_action="BUY") - 0.3) < 1e-9


def _self_test_reaction():
    """2026-10-22 regression tests for the reaction_mult fix.

    Covers the three defects found in production:
      1. multi_factor rows (85% of the table, no pct_change) shadowed the real
         reaction rows under `ORDER BY ts DESC LIMIT 1`.
      2. The lookup had no upper time bound, so it could match signals from far
         in the future -- future-data leakage into current decision weights.
      3. _reaction_mult could implicitly return None for 0.2<=|pct|<0.3 with
         vol<1.2, which makes compute_signal_weight raise TypeError.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE abnormal_signals(
        id INTEGER, ts INTEGER, asset TEXT, signal_kind TEXT, direction TEXT,
        anchor_ts INTEGER, detail_json TEXT, hint TEXT, status TEXT)""")
    T = 1_000_000
    # A multi_factor row (no pct_change) sits exactly on top of a real one.
    conn.execute(
        "INSERT INTO abnormal_signals VALUES"
        "(1,?,'XAU','multi_factor','neutral',NULL,'{}','h','open')", (T,))
    conn.execute(
        "INSERT INTO abnormal_signals VALUES"
        "(2,?,'XAU','abnormal_swing','neutral',NULL,"
        "'{\"pct_change\": 0.45}','h','open')", (T + 10,))
    # A far-future reaction that must NOT be attributed to a decision at T.
    conn.execute(
        "INSERT INTO abnormal_signals VALUES"
        "(3,?,'XAU','abnormal_swing','neutral',NULL,"
        "'{\"pct_change\": 0.99}','h','open')", (T + 999_999,))

    row = get_recent_abnormal(conn, "XAU", T, T + REACTION_WINDOW_SEC)
    assert row is not None, "should find the real reaction row"
    assert row["signal_kind"] == "abnormal_swing", (
        "multi_factor row (no pct_change) must be skipped, got %r"
        % row["signal_kind"])
    assert row["triggered"] is True, "pct_change 0.45 must count as triggered"
    assert row["ts"] == T + 10, "must not match the out-of-window future row"

    # Other assets must never leak in.
    assert get_recent_abnormal(conn, "ETH", T, T + REACTION_WINDOW_SEC) is None, \
        "must not leak signals across assets"

    # When only a far-future row exists, the windowed query must return None.
    conn.execute("DELETE FROM abnormal_signals WHERE id=2")
    assert get_recent_abnormal(conn, "XAU", T, T + REACTION_WINDOW_SEC) is None, \
        "future-only signal must not match (future-data leakage)"

    # _reaction_mult must never return None across the whole input space.
    for pct in (0.0, 0.05, 0.15, 0.25, 0.35, 0.6, 1.0, 5.0, -0.25, -1.0):
        for vol in (None, 0.5, 1.0, 1.2, 1.3, 1.5, 3.0):
            m = _reaction_mult({"triggered": True, "pct_change": pct,
                                "vol_ratio": vol, "aligned": True})
            assert m is not None, "None returned for pct=%s vol=%s" % (pct, vol)
            assert 0.5 <= m <= 1.5, "out of range: %s" % m

    # The previously-unreachable 0.2<=pct<0.3 band now returns a valid value.
    assert _reaction_mult({"triggered": True, "pct_change": 0.25,
                           "vol_ratio": 1.0, "aligned": True}) == 1.1
    assert _reaction_mult({"triggered": True, "pct_change": 0.45,
                           "vol_ratio": 1.0, "aligned": True}) == 1.2

    # A real reaction must actually move the composite weight.
    base = compute_signal_weight({"target_asset": "XAU", "sentiment_score": 0.5})
    withr = compute_signal_weight(
        {"target_asset": "XAU", "sentiment_score": 0.5},
        abnormal={"triggered": True, "pct_change": 0.45,
                  "vol_ratio": 1.0, "aligned": True})
    assert base["signal_weight"] != withr["signal_weight"], \
        "reaction must change the composite weight"
    conn.close()
    return True


# ============================================================================
# CLI / self-test entry point
# ============================================================================


if __name__ == "__main__":
    import sys
    print("=== signal_weight_evaluator self-test ===")
    # 1) base_mult
    assert _base_mult(None, None, None) == 1.0
    assert _base_mult(0.5, None, None) == 0.5
    assert 0.9 <= _base_mult(0.85, 80, 0.7) <= 1.5
    # 2) consensus_mult
    assert _consensus_mult("") == 1.0
    assert _consensus_mult("AGREE") == 1.3
    assert _consensus_mult("DISAGREE") == 0.6
    assert _consensus_mult('{"v1":{"agree":true},"v2":{"agree":true}}') == 1.3
    # 3) source_mult
    assert _source_mult_from_cache(None) == 1.0
    assert _source_mult_from_cache({"sample_n": 2, "hit_rate": 0.9}) == 1.0
    assert _source_mult_from_cache({"sample_n": 10, "hit_rate": 0.7}) == 1.2
    # 4) reaction_mult
    assert _reaction_mult(None) == 1.0
    assert _reaction_mult({"triggered": False}) == 1.0
    assert _reaction_mult({"triggered": True, "pct_change": 0.8, "vol_ratio": 2.0}) == 1.4
    assert _reaction_mult({"triggered": True, "pct_change": -0.5, "vol_ratio": 2.0, "aligned": False}) == 0.5
    # 4b) reaction regression tests (2026-10-22 fix)
    _self_test_reaction()
    print("  reaction regression: OK")
    # 4c) consensus regression tests (2026-10-22 fix)
    _self_test_consensus()
    print("  consensus regression: OK")
    # 5) regime_mult
    assert _regime_mult("trending_up") == 1.2
    assert _regime_mult("range_bound") == 0.85
    assert _regime_mult("volatile") == 0.7
    # 6) time_mult
    now = 1000000
    assert _time_mult(now, now_ts=now) == 1.0
    assert _time_mult(now - 3600, now_ts=now, expected_horizon="intraday") == 0.85
    # composite
    result = compute_signal_weight({
        "sentiment_score": 0.85, "impact_magnitude": 80, "heat_score": 0.7,
        "extra_models_consensus": '{"v1":{"agree":true},"v2":{"agree":true}}',
        "regime_at_decision": "trending_up", "ts": now, "expected_horizon": "1-3d",
    }, now_ts=now)
    assert 0.5 < result["signal_weight"] <= 1.0, f"unexpected: {result}"
    print(f"  composite: weight={result['signal_weight']:.3f} tier={result['weight_tier']}")
    print(f"  explain: {result['explain']}")
    print("ALL PASSED")
    sys.exit(0)




