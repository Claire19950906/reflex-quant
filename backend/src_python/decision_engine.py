"""
decision_engine.py — 2026-10-19 v1 (Q1 根因修复)
=====================================================
闭环 producer: critical_price_moves 强信号 → trade_decisions → settle → trade_reflections_v2

根因: bridge 已有 /api/decision/record + /settle-now + /list (P1), 但从未有 producer 调用它们。
     trade_decisions 表常驻仅 5 行 (测试桩), 致 missed_signals 永远 = 强信号全集。

设计:
  - 直接 DB 写入 (in-process 速度, 跳过 HTTP roundtrip)。生产环境若需要 API 校验可改为调用 bridge。
  - 幂等去重: evidence_json 嵌入 {"source":"decision_engine","signal_id":K,"move_ts":T},
    插入前查询 LIKE '%signal_id=N%' 防重复。
  - action 派生: move_pct>0 → BUY, =0 → HOLD, <0 → SELL (保守阈值 ±0.1% 内判 HOLD)
  - price_at_decision: end_price (若为 0/null 改用 peak_price, 还 0 改用 ohlcv_daily 当日 close)
  - created_at: 对历史回填用 move_ts (使 5d horizon 立即可结算); 对实时 producer 用 wall clock now
  - settlement: target close = ohlcv_daily 5d (或最接近 432000s 后) 的 close

用法:
  python3 src_python/decision_engine.py backfill --days 90      # 回填 90 天, 立即结算到期项
  python3 src_python/decision_engine.py live --once             # 单遍实时扫描 (新信号)
  python3 src_python/decision_engine.py settle                  # 仅结算到期 open 决策
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# v310 P1: 结算口径统一到 db 模块(与 bridge / 回填脚本同一实现)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as _shared_db
import cost_model

ALGO_THRESHOLD = 0.7            # 仅处理 algo_score >= 0.7
DEDUP_WINDOW_S = 86400           # ±24h 内已有决策则跳过
HOLD_BAND_PCT = 0.10             # |move_pct| < 此值判 HOLD
HORIZON_S = 5 * 86400            # 5 天结算
OVERSHOOT_TOL_S = 12 * 3600      # 找 5d 后价格时允许 12h 漂移
SCHEMA_VERSION = "de_v1_2026-10-19"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _is_already_decided(conn: sqlite3.Connection, signal_id: int) -> bool:
    """幂等检查: context_json 是否已引用 signal_id=N。

    v453：原来查 `evidence_json`，但 `trade_decisions` 早已把这一列改名为
    `context_json`（写入走 signals.decision_writer，字段名同 evidence 字典）。
    列不存在 → 每轮 `no such column` 崩在 propose_one 之前，异动决策产线整天零输出。
    """
    # v512: 原来只做子串匹配 `"signal_id": 7`，会把 `"signal_id": 70` 也算成"已决策"
    #       → 7 号异动的决策被静默跳过（一天少一单，且日志只写"已被决策"，看不出是误判）。
    #       先用 LIKE 粗筛，再解析 JSON 比较整数，只认真正相等的那条。
    pat = f'"signal_id": {signal_id}'
    rows = conn.execute(
        "SELECT context_json FROM trade_decisions WHERE context_json LIKE ? LIMIT 500",
        (f"%{pat}%",),
    ).fetchall()
    for r in rows:
        raw = r[0]
        try:
            ctx = json.loads(raw or "{}")
            if isinstance(ctx, dict) and int(ctx.get("signal_id") or -1) == int(signal_id):
                return True
        except (TypeError, ValueError):
            # JSON 坏了也不能因此漏判：退回严格字面匹配（后面必须跟 , 或 }）
            if f'"signal_id": {signal_id},' in str(raw) or f'"signal_id": {signal_id}}}' in str(raw):
                return True
    return False


def _opt(row, key, default=None):
    """安全取列值：`critical_price_moves` 的行是 sqlite3.Row，**没有 .get()**。

    v513: propose_one 原来写 `move.get("news_id")`，而调用方传的都是 sqlite3.Row，
    每一行都会 AttributeError 抛在 write_decision 之前 —— 决策产线实际零产出
    （run_backfill 里异常上抛，整批中断）。这里统一按"取不到就是 None"处理。
    """
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _action_from_move(move_pct: float) -> str:
    if move_pct > HOLD_BAND_PCT:
        return "BUY"
    if move_pct < -HOLD_BAND_PCT:
        return "SELL"
    return "HOLD"


def _price_at_decision(conn: sqlite3.Connection, asset: str, move_ts: int,
                       end_p: Optional[float], peak_p: Optional[float]) -> Optional[float]:
    """价格回退: end_price → peak_price → ohlcv_daily 当日 close。"""
    if end_p and end_p > 0:
        return float(end_p)
    if peak_p and peak_p > 0:
        return float(peak_p)
    row = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (asset, move_ts),
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def _forward_price(conn: sqlite3.Connection, asset: str, decision_ts: int) -> Optional[float]:
    """决策后 5 天的远期价 —— **与入场价同源**(分钟线优先, 日线回退)。

    v310 P1: 原来这里固定取 ohlcv_daily.close, 而入场价是
    db.resolve_entry_price 回填的**分钟线**价 → 两源不同源。实测两源同刻价差
    可达数个百分点(WTI 3.22% / ETH 1.32%), 而 5 日收益本身只有 1~4%,
    口径噪声能把它盖掉, 极端时算出 ±90% 以上的假收益(2026-10-26 污染 199 行)。
    现在统一走 db.forward_price —— bridge / 回填脚本同一实现, 口径永不漂移。

    v319+: 交收约束 —— T+1 品种 (股票) 当日买入当日不能卖, 远期价必须落在
    "最早可平仓时点" 之后, 否则结算出来的是实盘根本做不到的当日买卖收益。
    """
    min_ts = cost_model.earliest_exit_ts(asset, decision_ts)
    return _shared_db.forward_price(conn, asset, decision_ts, settle_days=5,
                                    min_ts=min_ts)


# ─── producer ─────────────────────────────────────────
def propose_one(conn: sqlite3.Connection, move: sqlite3.Row) -> Dict[str, Any]:
    """为单个 critical_price_move 创建一条 trade_decisions。

    Returns:
        {"action": "inserted"|"skipped", "decision_id": int|None, "reason": str}
    """
    sid = move["id"]
    asset = move["asset"]
    if _is_already_decided(conn, sid):
        return {"action": "skipped", "decision_id": None, "reason": f"signal_id={sid} 已被决策"}

    move_pct = move["move_pct"] or 0.0
    action = _action_from_move(move_pct)
    price = _price_at_decision(conn, asset, move["ts"], move["end_price"], move["peak_price"])
    confidence = float(move["algo_score"] or 0.0)

    # ── v337: 因果链先验接入 (需求 #8 "出现新闻先按因果链决策") ──────────
    # 旧实现: 决策链从不读 causal_chains, 724 条链只做事后评估 → 学得再准也
    # 影响不到下单。现在: 异动发生前 1h 内若找到同品种新闻, 用它匹配链;
    # 证据充分的链 (hit_rate≥60% 且 n≥2 且 posterior≥0.85) 若与本次方向相反,
    # 否决本次下单并落 deny_reason。链不足则只 annotate, 不改动作。
    chain_priors: list = []
    chain_direction = "UNKNOWN"
    vetoed_chain = None
    try:
        import chain_prior as _cp
        chain_priors = _cp.match_by_time(conn, asset, int(move["ts"]), window_sec=3600)
        if chain_priors:
            chain_direction = _cp.aggregate_direction(chain_priors)
            vetoed_chain = _cp.conflict_veto(chain_priors, action)
            if vetoed_chain:
                action = "HOLD"
                print(
                    f"[DECISION-ENGINE] chain_veto signal_id={sid} asset={asset} "
                    f"chain={vetoed_chain['chain_id']} chain_dir={vetoed_chain['direction']} "
                    f"hit={vetoed_chain['hit_rate']:.2f}/n={vetoed_chain['n_outcomes']}",
                    flush=True,
                )
    except Exception as exc:
        print(f"[DECISION-ENGINE] chain prior skipped: {type(exc).__name__}: {exc}",
              flush=True)

    evidence = {
        "source": "decision_engine",
        "version": SCHEMA_VERSION,
        "signal_id": int(sid),
        "move_ts": int(move["ts"]),
        "move_kind": move["move_kind"],
        "move_pct": float(move_pct),
        "duration_min": int(move["duration_min"] or 0),
        "context_json": move["context_json"],
    }
    if chain_priors:
        evidence["chain_prior"] = chain_priors
        evidence["chain_direction"] = chain_direction
    if vetoed_chain:
        evidence["chain_veto"] = {
            "chain_id": vetoed_chain["chain_id"],
            "name": vetoed_chain["name"],
            "direction": vetoed_chain["direction"],
            "hit_rate": vetoed_chain["hit_rate"],
            "n_outcomes": vetoed_chain["n_outcomes"],
            "posterior": vetoed_chain["posterior"],
        }
    rationale = (
        f"algo_signal:{move['move_kind']} score={confidence:.2f} "
        f"move_pct={move_pct:+.2f}% (signal_id={sid})"
    )
    if vetoed_chain:
        rationale += (
            f" | chain_veto:{vetoed_chain['chain_id']}"
            f"({vetoed_chain['direction']},hit={vetoed_chain['hit_rate']:.0%}"
            f"/n={vetoed_chain['n_outcomes']})"
        )
    # v342: 统一走 decision_writer（与 log_decision / bridge 同一 INSERT）。
    # 口径修正：algo_score 是「方向分」，不是 0-100 的 confidence —— 以前直接当
    # confidence 用，导致这个口径与 bridge 的 confidence 混在一起无法分层。
    from signals import decision_writer as _dw
    _ctx = dict(evidence) if isinstance(evidence, dict) else {"evidence": evidence}
    _ctx["algo_score"] = float(confidence)
    _ctx["move_pct"] = float(move_pct)
    _ctx["signal_id"] = int(sid)
    decision_id = _dw.write_decision(
        conn,
        asset=asset,
        intended_action=action,
        final_action=action,
        decision_type="ENTRY" if action in ("BUY", "SELL") else "REJECT",
        source=_dw.SOURCE_ALGO_MOVE,
        decision_ts=int(move["ts"]),
        news_id=_opt(move, "news_id"),
        entry_price=price,
        planned_stop=_opt(move, "planned_stop"),
        planned_target=_opt(move, "planned_target"),
        size_usdt=_opt(move, "size_usdt"),
        deny_reason=None if action in ("BUY", "SELL") else rationale[:2000],
        context=_ctx,
        score=confidence,
        status="open",
        persist_signal_weight=True,
        commit=True,
    )
    # ── v321.3 P0: 回链新闻 (需求 #5) ────────────────────────────────────────
    # 旧实现: 决策写完就结束, news_events.trade_decision_id 永远是空的 →
    #         "新闻 → 决策"断链, 断链的新闻进不了归因/反思闭环。
    # 现在: 通过 news_decision_link 多路匹配 (直接 id / 同源键 / ai_decisions
    #       两跳), 命中才挂, 挂不上就**不挂** —— 挂错比不挂更危险。
    try:
        from feeds.news_decision_link import link_news_to_decision as _lnk
        _lnk(conn, decision_id, _opt(move, "news_id"))
    except Exception as exc:
        print(f"[DECISION-ENGINE] news link skipped: {type(exc).__name__}: {exc}",
              flush=True)
    return {"action": "inserted", "decision_id": decision_id, "reason": action}


def list_critical_moves(conn: sqlite3.Connection, days: int, threshold: float) -> List[sqlite3.Row]:
    cutoff = int(time.time()) - days * 86400
    # ↓ 量纲污染过滤 (v308): move_pct 的 -9867% 之类脏值会让交易决策彻底失真
    #   90 = MAX_PLAUSIBLE_MOVE_PCT; 审计器 v308_audit_move_readers.py 认这个上界
    return list(conn.execute(
        "SELECT id, asset, ts, start_price, peak_price, end_price, move_pct, "
        "       move_kind, duration_min, algo_score, context_json "
        "FROM critical_price_moves "
        "WHERE algo_score >= ? AND ts >= ? "
        "AND ABS(COALESCE(move_pct,0)) <= 90 "
        "ORDER BY ts ASC",
        (threshold, cutoff),
    ).fetchall())


# ─── settler ──────────────────────────────────────────
def _confidence_from(dec: sqlite3.Row, action: str) -> float:
    """trade_decisions 没有 confidence 列 —— 从 context_json.score 取, 否则按 event_strength 映射。"""
    try:
        cj = json.loads(dec["context_json"] or "{}")
        s = cj.get("score")
        if isinstance(s, (int, float)) and 0.0 <= float(s) <= 1.0:
            return float(s)
    except Exception:
        pass
    return {"high": 0.8, "medium": 0.5, "low": 0.3}.get(dec["event_strength"], 0.5)


def _resolve_dimension_for_decision(conn: sqlite3.Connection, decision_id: int) -> tuple:
    """按 v309 口径解析该决策的 (dimension, subcategory)。

    口径定义在 reflection_dimension_map.py (单一权威) —— 取数优先级:
      1. decision_errors.error_code (E001a-E033, 程序化校验, 最精确)
      2. attribution_reviews.reason_code (LLM 失败框架)
      3. v188 ontology 三层标签 (本函数没有, 跳过)

    解析不出来就返回 ("", "") —— 绝不塞默认桶, 那会让三维度统计失真。
    """
    try:
        from review import reflection_dimension_map as _rdm
    except Exception:
        return ("", "")
    codes: List[str] = []
    reason_code = None
    try:
        # v399 P1: 带 severity —— 维度归类是"严重度优先", 不带就会被低频高危码
        # 被高频低危码(E001a 缺事实)压掉, 三维细分退化成一种 (门禁 G11)。
        rows = conn.execute(
            "SELECT error_code, severity FROM decision_errors WHERE decision_id = ?",
            (int(decision_id),),
        ).fetchall()
        codes = [f"{r[0]}:{int(r[1] or 0)}" for r in rows if r and r[0]]
    except Exception:
        codes = []
    try:
        row = conn.execute(
            "SELECT reason_code FROM attribution_reviews "
            "WHERE trade_id = ? ORDER BY id DESC LIMIT 1",
            (int(decision_id),),
        ).fetchone()
        if row and row[0] is not None:
            reason_code = row[0]
    except Exception:
        reason_code = None
    try:
        hit = _rdm.resolve_dimension(error_codes=codes, reason_code=reason_code)
    except Exception:
        hit = None
    if not hit:
        return ("", "")
    dimension, subcategory = hit
    if not _rdm.is_valid_subcategory(dimension, subcategory):
        return ("", "")
    return (dimension, subcategory)


def settle_one(conn: sqlite3.Connection, dec: sqlite3.Row) -> Dict[str, Any]:
    """单条决策结算: 计算 outcome_pct, 写 status=settled, 落 trade_reflections_v2。"""
    did = dec["id"]
    asset = dec["asset"]
    action = dec["action"]
    price_at = dec["price_at_decision"]
    if not price_at or price_at <= 0:
        return {"action": "skipped", "decision_id": did, "reason": "no price_at_decision"}
    fwd = _forward_price(conn, asset, dec["created_at"])
    if fwd is None:
        return {"action": "skipped", "decision_id": did, "reason": "no forward price within ±12h of ts+5d"}
    outcome_pct = round((fwd - price_at) / price_at * 100.0, 3)
    # v310 P1: |收益| > 90% 必是取价异常 —— 拒写(保持 open 等重新取价),
    # 绝不能让它进 trade_decisions / trade_reflections_v2 污染统计。
    if _shared_db.is_implausible_outcome(outcome_pct):
        return {"action": "skipped", "decision_id": did,
                "reason": f"implausible outcome {outcome_pct:+.2f}% (entry={price_at}, fwd={fwd})"}
    # ── v319+: 执行成本口径 ───────────────────────────────────────────────
    # 裸价收益 ≠ 实际到手收益: 一次往返要付 fee + slippage (XAU 16bps /
    # BTC 30bps)。XAU 日均波幅才 0.5% 量级, 16bps ≈ 1/3 个日波幅 ——
    # 不扣成本的话, "扣完成本还赚吗" 这一个问题就会让整套统计失真。
    #   actual_outcome_pct → **裸价**收益 (历史口径, 不改语义)
    #   pnl_pct            → **扣完成本**的净收益 (这才是"实际到手")
    #   hit                → 按**净收益**判 (打平/微赢但被成本吃掉 = 没赢)
    outcome_pct_net = round(float(cost_model.apply_costs(outcome_pct, asset, action)), 3)
    if action == "BUY":
        hit = 1 if outcome_pct_net > 0 else 0
    elif action == "SELL":
        hit = 1 if outcome_pct_net < 0 else 0
    else:
        hit = 0
    conn.execute(
        "UPDATE trade_decisions SET status='settled', settled_at=strftime('%s','now'), "
        "actual_outcome_pct=? WHERE id=? AND status='open'",
        (outcome_pct, did),
    )
    # confidence: trade_decisions 无该列 → 从 context_json.score 取, 回退 event_strength
    conf = _confidence_from(dec, action)

    # v301-fix: 原来硬编码 dimension='decision_engine' / subcategory='BUY|SELL'
    # —— 两者都不是合法取值(A/B/C + 12 个小类), 会让
    # /api/v237/missed-signals/by-dimension 的三维度统计失真。
    # 改走 reflection_dimension_map 的统一口径; 解析不出就留空。
    _dim, _subcat = _resolve_dimension_for_decision(conn, did)
    # v398: 降级决策不写洞察。结算账务照常(状态/收益必须留), 但反思行不落 ——
    # 降级路径没有真分析, 写进去只会让"错因分布"变成"没看见的分布"。
    reflection_skipped = False
    try:
        import insight_pool as _ip
        _ai_id = dec["ai_decision_id"] if "ai_decision_id" in dec.keys() else None
        if _ip.is_degraded_decision(conn, _ai_id):
            reflection_skipped = True
    except Exception:
        reflection_skipped = False

    # 唯一键保护: trade_reflections_v2.decision_id 是 UNIQUE, 而
    # paper_reflection_worker 用的是**另一个 id 空间**(ai_decisions.id)。
    # 冲突时只更新「结算行」(paper_run_id IS NULL), 绝不覆盖 worker 的反思行。
    if not reflection_skipped:
        conn.execute(
            "INSERT INTO trade_reflections_v2 "
            "(decision_id, asset, dimension, subcategory, confidence_at_time, "
            " actual_outcome_pct, pnl_pct, settled, hit, action, entry_price, created_at) "
            "VALUES (?,?,?,?,?,?,?,1,?,?,?,strftime('%s','now')) "
            "ON CONFLICT(decision_id) DO UPDATE SET "
            "asset=excluded.asset, "
            "dimension=excluded.dimension, subcategory=excluded.subcategory, "
            "confidence_at_time=excluded.confidence_at_time, "
            "actual_outcome_pct=excluded.actual_outcome_pct, "
            "pnl_pct=excluded.pnl_pct, settled=1, "
            "hit=excluded.hit, action=excluded.action, entry_price=excluded.entry_price "
            "WHERE trade_reflections_v2.paper_run_id IS NULL",
            (did, asset, _dim, _subcat, conf,
             outcome_pct, outcome_pct_net, hit, action, float(price_at)),
        )
    return {"action": "settled", "decision_id": did, "outcome_pct": outcome_pct,
            "outcome_pct_net": outcome_pct_net, "hit": hit,
            "reflection": "skipped_degraded" if reflection_skipped else "written"}


def list_due_settlements(conn: sqlite3.Connection, now_ts: Optional[int] = None) -> List[sqlite3.Row]:
    """扫待结算决策。

    列名真相 (2026-10-22 实测 trade_decisions 22 列):
      - 没有 action / confidence / price_at_decision
      - 真实列是 final_action / entry_price / decision_ts
      - created_at 是 TEXT(datetime('now')), 不能和整数 epoch 比较 → 用 decision_ts

    只取 final_action IN ('BUY','SELL'):
      'REJECTED' 表示信号被 guard 挡下、从未成交, 没有盈亏, 永远不该进结算队列。
      (实测 1993 笔 open 中 1974 笔是 REJECTED, 真实可结算仅 19 笔)
    """
    now_ts = now_ts or int(time.time())
    cutoff = now_ts - HORIZON_S
    return list(conn.execute(
        "SELECT id, asset, final_action AS action, "
        "       entry_price AS price_at_decision, "
        "       decision_ts AS created_at, "
        "       event_strength, context_json, ai_decision_id "
        "FROM trade_decisions "
        "WHERE status='open' "
        "  AND final_action IN ('BUY','SELL') "
        "  AND entry_price IS NOT NULL AND entry_price > 0 "
        "  AND decision_ts IS NOT NULL AND decision_ts <= ? "
        "ORDER BY decision_ts ASC",
        (cutoff,),
    ).fetchall())


# ─── orchestrators ────────────────────────────────────
def run_backfill(days: int = 90, threshold: float = ALGO_THRESHOLD) -> Dict[str, Any]:
    """一次性回填: 处理 [now-days, now] 所有 algo_score>=threshold 的 critical_price_moves。"""
    conn = _db()
    summary: Dict[str, Any] = {
        "phase": "backfill",
        "days": days,
        "threshold": threshold,
        "moves_seen": 0,
        "inserted": 0,
        "skipped_dup": 0,
        "settled": 0,
        "settle_skip": 0,
        "action_dist": {"BUY": 0, "SELL": 0, "HOLD": 0},
    }
    try:
        # ensure table (idempotent — bridge 已装 create IF NOT EXISTS, 但本脚本独立跑也要安全)
        moves = list_critical_moves(conn, days, threshold)
        summary["moves_seen"] = len(moves)
        # ── phase 1: propose all ──
        for mv in moves:
            r = propose_one(conn, mv)
            if r["action"] == "inserted":
                summary["inserted"] += 1
                summary["action_dist"][r["reason"]] += 1
            else:
                summary["skipped_dup"] += 1
        conn.commit()
        # ── phase 2: settle all due ──
        due = list_due_settlements(conn)
        for d in due:
            r = settle_one(conn, d)
            if r["action"] == "settled":
                summary["settled"] += 1
            else:
                summary["settle_skip"] += 1
        conn.commit()
    finally:
        conn.close()
    return summary


def run_live_once(threshold: float = ALGO_THRESHOLD) -> Dict[str, Any]:
    """单遍实时扫描: 只看过去 1 小时。生产可挂 systemd timer / cron。"""
    conn = _db()
    summary: Dict[str, Any] = {
        "phase": "live_once",
        "threshold": threshold,
        "inserted": 0, "skipped_dup": 0, "settled": 0, "settle_skip": 0,
        "action_dist": {"BUY": 0, "SELL": 0, "HOLD": 0},
    }
    try:
        cutoff = int(time.time()) - 3600
        # ↓ 量纲污染过滤 (v308): 同 list_critical_moves —— 90 = MAX_PLAUSIBLE_MOVE_PCT
        moves = list(conn.execute(
            "SELECT id, asset, ts, start_price, peak_price, end_price, move_pct, "
            "       move_kind, duration_min, algo_score, context_json "
            "FROM critical_price_moves WHERE algo_score >= ? AND ts >= ? "
            "AND ABS(COALESCE(move_pct,0)) <= 90",
            (threshold, cutoff),
        ).fetchall())
        for mv in moves:
            r = propose_one(conn, mv)
            if r["action"] == "inserted":
                summary["inserted"] += 1
                summary["action_dist"][r["reason"]] += 1
            else:
                summary["skipped_dup"] += 1
        conn.commit()
        for d in list_due_settlements(conn):
            r = settle_one(conn, d)
            if r["action"] == "settled":
                summary["settled"] += 1
            else:
                summary["settle_skip"] += 1
        conn.commit()
    finally:
        conn.close()
    return summary


def run_settle_only() -> Dict[str, Any]:
    """仅结算: 不新增决策, 仅把已到期的 open 决策结算掉。"""
    conn = _db()
    summary = {"phase": "settle_only", "settled": 0, "skipped": 0}
    try:
        for d in list_due_settlements(conn):
            r = settle_one(conn, d)
            summary["settled" if r["action"] == "settled" else "skipped"] += 1
        conn.commit()
        # v342: 同一趟顺带补多周期读数（15m/1h/4h/1d/5d）。主口径不受影响；
        # 失败只留日志，绝不阻断主结算。
        try:
            import multi_horizon_settle as _mhs
            summary["horizons"] = _mhs.run_once(conn)
        except Exception as _mh_exc:
            summary["horizons"] = {
                "error": f"{type(_mh_exc).__name__}: {str(_mh_exc)[:120]}"}
            print(f"[DECISION-ENGINE] 多周期结算跳过: {type(_mh_exc).__name__}: "
                  f"{str(_mh_exc)[:120]}", flush=True)
    finally:
        conn.close()
    return summary


# ─── CLI ───────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", help="一次性回填 N 天所有强信号")
    bf.add_argument("--days", type=int, default=90)
    bf.add_argument("--threshold", type=float, default=ALGO_THRESHOLD)

    lv = sub.add_parser("live", help="单遍实时扫描 (近 1h)")
    lv.add_argument("--once", action="store_true", default=True)

    sub.add_parser("settle", help="仅结算到期 open 决策")

    args = p.parse_args()
    if args.cmd == "backfill":
        s = run_backfill(days=args.days, threshold=args.threshold)
    elif args.cmd == "live":
        s = run_live_once()
    else:
        s = run_settle_only()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())