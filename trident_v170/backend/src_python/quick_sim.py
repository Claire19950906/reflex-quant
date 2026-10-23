"""快速模拟交易评测通道。

把 ai_decisions 里所有方向性信号（BUY/SELL）按真实行情建仓，经过
QUICK_SIM_HORIZON_MINUTES 后按真实行情结算，用于盈利率评测。

与正式模拟盘的区别：
- 不使用多证据闸门与策略闸门作为准入条件，闸门结论只作为分组标记保存；
- 持仓周期短（QUICK_SIM_HORIZON_MINUTES 配置，默认 20 分钟），只服务于统计评测，不写 ai_decisions。

盈亏一律来自真实行情，取不到价格就不建仓、不结算，绝不补造数据。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional

import config
import db
import news_sources
from position_sizing import compute_notional

_PASSED_REASON = config.TRADE_GATE_PASSED_REASON  # v231+ 单一来源, 改 config 即可


def _resolve_window(
    hours: Optional[int],
    from_ts: Optional[int],
    to_ts: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """把 hours / from_ts / to_ts 三种入口归一为 (lo, hi)（UTC 秒）。

    v180 P0+：与 /api/history/news-trades 同一口径。
    - from_ts / to_ts 都给 → 直接用
    - 只给 from_ts → hi=None（开区间到当前）
    - 只给 to_ts   → lo=None（开区间到历史起点）
    - 给 hours     → lo = now - hours*3600（秒），hi=None
    - 都不给       → (None, None)，调用方走全量
    """
    if from_ts is not None or to_ts is not None:
        return from_ts, to_ts
    if hours is not None and hours > 0:
        return int(time.time()) - hours * 3600, None
    return None, None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = previous


def pending_signals(
    conn: sqlite3.Connection,
    limit: int = 50,
    min_created_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """尚未进入快速模拟的方向性信号（仅近 120 分钟，避免把历史旧信号一次性补建仓，
    导致评测统计失真）。

    min_created_at：重置模拟盘后的时间戳（UTC，与 created_at 同格式）。
    非空时只评测该时间之后产生的信号——重置后快速评测从零开始，
    不会被重置前保留的历史信号重新建仓结算。

    v231+ (2026-10-10): 在 SQL 层加 R1/R2/R4/R7/R9 预过滤, 直接从源头堵掉
    ABLATION §D 揭示的 24/24 笔不合约 trade. 详 sample_quality_filter.pre_trade_check.
    关闭: env QUICK_SIM_QUALITY_FILTER=off (紧急回滚).
    """
    # v231+: 默认开质量门, env 可关
    import os
    filter_on = (os.environ.get('QUICK_SIM_QUALITY_FILTER', 'on') or 'on').strip().lower() \
        not in ('0', 'off', 'false', 'no')

    if filter_on:
        # v231+ (C3): 阈值/白名单统一读 config.py, 不再硬编码
        # 任何改 EVIDENCE_CONFIDENCE_GATE / SIM_ALLOWED_ASSETS 后, 重启 backend 生效
        allowed = config.SIM_ALLOWED_ASSETS
        if not allowed:
            raise RuntimeError(
                "config.SIM_ALLOWED_ASSETS 不能为空, 至少 1 个资产; "
                "改 env SIM_ALLOWED_ASSETS= 重启 backend 即可"
            )
        in_placeholders = ",".join("?" for _ in allowed)
        # v231+ (H1): R9 改白名单. 现状 db schema CHECK 允许 5 个 status
        # ('UNREAD','APPROVED','REJECTED','REVIEWED','AUTO_APPROVED'), 其中
        # REVIEWED = 用户已查 dashboard (api_server.mark-decisions-read),
        # APPROVED = 用户手动批准, 都仍需 trade. 只 REJECTED 拦.
        # 显式列 4 个白名单的好处: 未来加新 status (EXPIRED/SETTLED/CANCELLED)
        # 必须主动加进白名单, code review 时就能发现, 不会默默漏过黑名单.
        tradable_statuses = ("UNREAD", "APPROVED", "REVIEWED", "AUTO_APPROVED")
        tradable_placeholders = ",".join("?" for _ in tradable_statuses)
        # R1: gate_reason == _PASSED_REASON
        # R2: evidence_action != 'HOLD'
        # R4: target_asset in config.SIM_ALLOWED_ASSETS (动态 IN)
        # R7: evidence_confidence >= config.EVIDENCE_CONFIDENCE_GATE
        # R9: status IN tradable_statuses (白名单, 显式列 4 个)
        sql = f"""SELECT ad.id, ad.news_id, UPPER(ad.target_asset) AS asset,
                        UPPER(ad.suggested_action) AS action, ad.sentiment_score AS score,
                        ad.evidence_confidence, ad.trade_gate_reason, ad.evidence_action,
                        ad.strategy_id, ad.strategy_version_id, ad.paper_trading_run_id,
                        ad.event_strength, ad.status,
                        rn.source AS news_source
                 FROM ai_decisions ad
                 LEFT JOIN raw_news rn ON rn.id = ad.news_id
                 LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                 WHERE qs.id IS NULL
                   AND UPPER(ad.suggested_action) IN ('BUY', 'SELL')
                   AND UPPER(ad.target_asset) NOT IN ('', 'NONE')
                   AND datetime(ad.created_at, 'localtime') > datetime('now', 'localtime', '-120 minutes')
                   AND (? IS NULL OR datetime(ad.created_at, 'localtime') >= datetime(?, 'localtime'))
                   AND ad.trade_gate_reason = ?
                   AND UPPER(COALESCE(ad.evidence_action, 'HOLD')) != 'HOLD'
                   AND UPPER(ad.target_asset) IN ({in_placeholders})
                   AND COALESCE(ad.evidence_confidence, 0) >= ?
                   AND UPPER(COALESCE(ad.status, '')) IN ({tradable_placeholders})
                 ORDER BY ad.id DESC LIMIT ?"""
        params = (min_created_at, min_created_at,
                  _PASSED_REASON, *allowed, config.EVIDENCE_CONFIDENCE_GATE,
                  *tradable_statuses, limit)
    else:
        # 紧急回滚路径: 旧 SQL, 无质量门
        sql = """SELECT ad.id, ad.news_id, UPPER(ad.target_asset) AS asset,
                        UPPER(ad.suggested_action) AS action, ad.sentiment_score AS score,
                        ad.evidence_confidence, ad.trade_gate_reason, ad.strategy_id,
                        ad.strategy_version_id, ad.paper_trading_run_id,
                        ad.event_strength,
                        rn.source AS news_source
                 FROM ai_decisions ad
                 LEFT JOIN raw_news rn ON rn.id = ad.news_id
                 LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                 WHERE qs.id IS NULL
                   AND UPPER(ad.suggested_action) IN ('BUY', 'SELL')
                   AND UPPER(ad.target_asset) NOT IN ('', 'NONE')
                   AND datetime(ad.created_at, 'localtime') > datetime('now', 'localtime', '-120 minutes')
                   AND (? IS NULL OR datetime(ad.created_at, 'localtime') >= datetime(?, 'localtime'))
                 ORDER BY ad.id DESC LIMIT ?"""
        params = (min_created_at, min_created_at, limit)

    return _rows(conn, sql, params)


def gate_sell_signal(signal: Dict[str, Any]) -> Tuple[bool, str]:
    """P0.5b：建仓前的 SELL 信号门槛。

    返回 (allowed, reason)：
      - allowed=True  → 正常建仓
      - allowed=False → 拒签，reason 是简短标签（high_strength / asset_BTC
                        / asset_WTI / disabled）
    BUY 永远放行。门槛关闭时也放行。
    """
    if not config.SELL_GATE_ENABLED:
        return True, "disabled"
    action = (signal.get("action") or "").upper()
    if action != "SELL":
        return True, "buy_passthrough"
    if config.SELL_GATE_BLOCK_HIGH_STRENGTH and (
        (signal.get("event_strength") or "").lower() == "high"
    ):
        return False, "high_strength"
    asset = (signal.get("asset") or "").upper()
    if asset in config.SELL_GATE_BLOCKED_ASSETS:
        return False, f"asset_{asset}"
    return True, "passed"


def open_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """给未建仓的方向性信号按真实行情建仓，返回新建仓明细。

    v230+：cycle 级安全护栏（P0.3 日亏熔断 + P0.4 事件窗口）检查，
    任一拒绝时整批信号跳过、不建仓。失败时降级放行，不阻断正常流程。
    """
    horizon = config.QUICK_SIM_HORIZON_MINUTES
    notional = config.QUICK_SIM_NOTIONAL_USDT
    sized_meta: List[Dict[str, Any]] = []
    created: List[Dict[str, Any]] = []
    sell_gated: List[Dict[str, Any]] = []
    _pe_skip_reasons: List[Dict[str, Any]] = []  # v208: policy_engine 拒绝
    _quality_rejected: List[Dict[str, Any]] = []  # v231+: sample_quality_filter 拒绝
    price_cache: Dict[str, float] = {}
    # 单源开关只过滤模拟盘交易（含快速评测建仓），不影响新闻采集。
    news_settings = news_sources.get_settings(conn)
    # 重置时间戳：重置后只评测重置之后产生的信号，旧信号不再重新建仓。
    reset_row = conn.execute(
        "SELECT quick_sim_reset_at FROM paper_trading_settings WHERE id=1"
    ).fetchone()
    min_created_at = reset_row[0] if reset_row is not None and reset_row[0] else None

    # ── v230+: cycle 级安全护栏 ──────────────────────────────────────────
    # 任一拒绝 → 整批跳过；失败降级放行，避免 bug 影响正常交易。
    try:
        from safety_guards import evaluate_all
        decision = evaluate_all(conn)
    except Exception as exc:
        print(f"[SAFETY] guard check failed, falling through: "
              f"{type(exc).__name__}: {exc}", flush=True)
        decision = None
    if decision is not None and not decision.allow:
        print(f"[SAFETY] open_trades skipped this cycle: "
              f"guard={decision.guard} reason={decision.reason} "
              f"metadata={decision.metadata}", flush=True)
        conn.commit()
        return []

    # ── v231+ (H4): 拦截持久化 - 把最近 2h 被 quality_filter 拒掉的 decision 落库
    # 用于 C1 假阳性 A/B 决策 / 后续 forensic. off 路径自动跳过 (filter 关闭时返 0).
    # 注意: 下面 for 循环里 per-signal pre_trade_check 仍跑 (R8 需要 per-decision),
    # 这里 audit 是"全景快照", 不是替代 per-signal 检查.
    try:
        import sample_quality_filter as _sqf
        _audit_n = _sqf.audit_recent_rejections(conn, since_minutes=120)
        if _audit_n:
            print(f"[H4] audit_recent_rejections: 新增 {_audit_n} 条拦截记录", flush=True)
    except Exception as _exc_audit:
        print(f"[H4] audit_recent_rejections 失败, 不影响 open_trades 主流程: "
              f"{type(_exc_audit).__name__}: {_exc_audit}", flush=True)

    for signal in pending_signals(conn, limit, min_created_at=min_created_at):
        if not news_sources.source_enabled(signal.get("news_source") or "", news_settings):
            continue
        # ── v231+ (2026-10-10): R8 兄弟决策数预检 (DEEP_REFLECTION §5) ──
        # SQL 层已滤 R1/R2/R4/R7/R9, R8 因需 per-decision sibling count
        # 留在 Python 层做. SQL 已被 filter, 这里做最后一道防御.
        # v231+ (C4): QUICK_SIM_QUALITY_FILTER=off 时整个质量门关闭, 包括 Python R1-R9,
        # 与 SQL 路径行为一致, 让"off=回到 V8 之前"完整生效.
        try:
            import sample_quality_filter as _sqf
            if not _sqf.is_pre_trade_filter_enabled():
                _qc = {"is_valid": True, "reasons": [], "flags": {}}  # off 路径: 跳过 R1-R9
            else:
                _qc = _sqf.pre_trade_check(signal, conn=conn)
            if not _qc["is_valid"]:
                _quality_rejected.append({
                    "decision_id": signal["id"], "asset": signal["asset"],
                    "action": signal["action"], "reasons": _qc["reasons"],
                })
                # v231+: 决策打点 — QUALITY_FILTER 拒绝
                try:
                    import trade_decisions as _td
                    _td.log_decision(
                        conn, asset=signal["asset"],
                        intended_action=signal["action"],
                        final_action="REJECTED", decision_type="REJECT",
                        news_id=signal.get("news_id"),
                        event_strength=signal.get("event_strength"),
                        deny_reason=";".join(_qc["reasons"])[:200],
                        guard="QUALITY_FILTER",
                        ai_decision_id=signal.get("id"),
                        context={"score": signal.get("score"),
                                 "evidence_confidence": signal.get("evidence_confidence"),
                                 "flags": _qc["flags"]},
                    )
                except Exception as _exc3:
                    print(f"[trade_decisions] QUALITY_FILTER 打点失败: {_exc3}",
                          flush=True)
                continue
        except Exception as _exc_qc:
            # 质量门自身出错 → 兜底放行 (宁放过不误杀, 与 SELL_GATE 兜底策略一致)
            print(f"[QUALITY_FILTER] 预检异常, 兜底放行: {_exc_qc}", flush=True)
        # ── v208: policy_engine — per-asset 自定义规则（先于 SELL_GATE） ──
        # v230.1: 修复 adjusted_notional_pct 未实际生效的 bug（之前 ADJUST_SIZE_PCT 规则被静默忽略）
        _pd_adj_pct: float = 1.0
        _pd_rule_id: Optional[int] = None
        _pd_rule_type: Optional[str] = None
        try:
            import policy_engine as _pe
            _pd = _pe.evaluate(
                conn,
                asset=signal["asset"],
                action=signal["action"],
                context={
                    "notional_usdt": signal.get("notional_usdt"),
                    "evidence_confidence": signal.get("evidence_confidence"),
                    "event_strength": signal.get("event_strength"),
                },
            )
            if not _pd.allow:
                _pe_skip_reasons.append({
                    "decision_id": signal["id"], "asset": signal["asset"],
                    "action": signal["action"], "reason": _pd.reason,
                    "rule_type": _pd.rule_type, "rule_id": _pd.rule_id,
                })
                try:
                    import trade_decisions as _td
                    _td.log_decision(
                        conn, asset=signal["asset"],
                        intended_action=signal["action"],
                        final_action="REJECTED", decision_type="REJECT",
                        news_id=signal.get("news_id"),
                        event_strength=signal.get("event_strength"),
                        deny_reason=_pd.reason, guard="POLICY_ENGINE",
                        ai_decision_id=signal.get("id"),
                        context={"rule_id": _pd.rule_id,
                                 "rule_type": _pd.rule_type,
                                 "score": signal.get("score"),
                                 "evidence_confidence": signal.get("evidence_confidence")},
                    )
                except Exception as _exc2:
                    print(f"[trade_decisions] POLICY_ENGINE 打点失败: {_exc2}", flush=True)
                continue
            # 记录软调整（ADJUST_SIZE_PCT 等），下面 compute_notional 之后乘上去
            if _pd.adjusted_notional_pct and _pd.adjusted_notional_pct < 1.0:
                _pd_adj_pct = float(_pd.adjusted_notional_pct)
                _pd_rule_id = _pd.rule_id
                _pd_rule_type = _pd.rule_type
        except Exception as _exc:
            # policy_engine 调用失败不要阻断正常交易（兜底放行）
            print(f"[policy_engine] 调用失败，兜底放行: {_exc}", flush=True)
        # P0.5b：SELL 信号门槛——建仓前拒签高强度 / 黑名单资产。
        allowed, gate_reason = gate_sell_signal(signal)
        if not allowed:
            sell_gated.append({
                "decision_id": signal["id"], "asset": signal["asset"],
                "action": signal["action"],
                "event_strength": signal.get("event_strength"),
                "reason": gate_reason,
            })
            # ── v208: 决策打点 — SELL 门槛拒绝 ──
            try:
                import trade_decisions as _td
                _td.log_decision(
                    conn, asset=signal["asset"],
                    intended_action=signal["action"],
                    final_action="REJECTED", decision_type="REJECT",
                    news_id=signal.get("news_id"),
                    event_strength=signal.get("event_strength"),
                    deny_reason=gate_reason, guard="SELL_GATE",
                    ai_decision_id=signal.get("id"),
                    context={"score": signal.get("score"),
                             "evidence_confidence": signal.get("evidence_confidence")},
                )
            except Exception as _exc:
                print(f"[trade_decisions] SELL_GATE 打点失败: {_exc}", flush=True)
            continue
        # 同一资产只取一次行情（避免每条信号都打外部价格源，CPU/请求量大减）
        if signal["asset"] not in price_cache:
            price_cache[signal["asset"]] = price_fn(signal["asset"])
        price = price_cache[signal["asset"]]
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        # P0.1: 按 evidence_confidence 分层调整名义金额（默认开启；env 关掉）
        signal_notional, sizing_meta = compute_notional(signal, notional, conn)
        # v230.1: policy_engine ADJUST_SIZE_PCT 软调整 — 在 evidence 分层之后再乘
        if _pd_adj_pct < 1.0:
            pre_policy_notional = signal_notional
            signal_notional = round(signal_notional * _pd_adj_pct, 2)
            sizing_meta["policy_adjust_pct"] = _pd_adj_pct
            sizing_meta["policy_rule_id"] = _pd_rule_id
            sizing_meta["policy_rule_type"] = _pd_rule_type
            sizing_meta["pre_policy_notional"] = pre_policy_notional
        sized_meta.append({
            "decision_id": signal["id"], "asset": signal["asset"],
            "action": signal["action"],
            "confidence": signal.get("evidence_confidence"),
            **sizing_meta,
            "notional": signal_notional,
        })
        reason = signal["trade_gate_reason"] or ""
        entry_ts = int(time.time())
        cur = conn.execute(
            """INSERT OR IGNORE INTO quick_sim_trades
               (decision_id, news_id, asset, action, score, evidence_confidence,
                gate_passed, gate_reason, strategy_id, strategy_version_id,
                paper_trading_run_id, notional_usdt, entry_price, entry_ts, horizon_minutes,
                max_price, min_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal["id"], signal["news_id"], signal["asset"], signal["action"],
                signal["score"], signal["evidence_confidence"],
                int(reason == _PASSED_REASON), reason,
                signal["strategy_id"], signal["strategy_version_id"],
                signal["paper_trading_run_id"], signal_notional,
                float(price), entry_ts, horizon,
                float(price), float(price),
            ),
        )
        # P0: 反向同步 ai_decisions — 让下游 (backtest/reflection/risk_manager) 能读到 entry 数据
        # 用 COALESCE 保护：若 ai_decisions 已有 entry_price 不覆盖（avoid missing 是 replay）。
        # 但 horizon_minutes / entry_time 必须每次刷新以匹配最新 quick_sim 配置。
        if cur.rowcount > 0:
            from datetime import datetime as _dt
            entry_iso = _dt.fromtimestamp(entry_ts).isoformat(timespec="seconds")
            conn.execute(
                """UPDATE ai_decisions
                   SET entry_price             = COALESCE(entry_price, ?),
                       entry_time              = COALESCE(NULLIF(entry_time, ''), ?),
                       holding_horizon_minutes = COALESCE(holding_horizon_minutes, ?)
                   WHERE id = ?""",
                (float(price), entry_iso, horizon, signal["id"]),
            )
            # ── v208: 决策打点 — ENTRY 成功 ──
            try:
                import trade_decisions as _td
                _td.log_decision(
                    conn, asset=signal["asset"],
                    intended_action=signal["action"],
                    final_action=signal["action"], decision_type="ENTRY",
                    news_id=signal.get("news_id"),
                    event_strength=signal.get("event_strength"),
                    entry_price=float(price),
                    size_usdt=signal_notional,
                    ai_decision_id=signal.get("id"),
                    guard="NONE",
                    context={"horizon_minutes": horizon,
                             "evidence_confidence": signal.get("evidence_confidence"),
                             "strategy_id": signal.get("strategy_id"),
                             "strategy_version_id": signal.get("strategy_version_id")},
                )
            except Exception as _exc:
                print(f"[trade_decisions] ENTRY 打点失败: {_exc}", flush=True)
        created.append({
            "decision_id": signal["id"], "asset": signal["asset"],
            "action": signal["action"], "entry_price": float(price),
            "notional": signal_notional,
        })
    if sell_gated:
        print(f"[SELL_GATE] {len(sell_gated)} SELL signal(s) rejected"
              f" this cycle: {sell_gated[:5]}{'...' if len(sell_gated) > 5 else ''}",
              flush=True)
    if _pe_skip_reasons:
        print(f"[POLICY_ENGINE] {len(_pe_skip_reasons)} signal(s) blocked"
              f" this cycle: {_pe_skip_reasons[:5]}{'...' if len(_pe_skip_reasons) > 5 else ''}",
              flush=True)
    if _quality_rejected:
        print(f"[QUALITY_FILTER] {len(_quality_rejected)} signal(s) blocked"
              f" this cycle: {_quality_rejected[:5]}{'...' if len(_quality_rejected) > 5 else ''}",
              flush=True)
    # v231+ (C4): 在返回 list 上挂 quality_rejected 属性, 避免破坏现有 caller 的
    # `for x in created` / `len(created)` 协议. 想看拒绝明细的 caller 读
    # `created.quality_rejected` (类型: List[Dict], 每条含 decision_id/asset/action/reasons)
    try:
        created.quality_rejected = _quality_rejected  # type: ignore[attr-defined]
        created.quality_rejected_count = len(_quality_rejected)  # type: ignore[attr-defined]
    except AttributeError:
        # 极端 case: caller 把返回值当不可变 list 用; 这种情况直接返回元数据 dict
        pass
    if sized_meta:
        # 只打样本前 5 条 + 聚合计数，避免日志洪水
        sample = sized_meta[:5]
        n_low = sum(1 for m in sized_meta if m["tier"] == "low")
        n_mid = sum(1 for m in sized_meta if m["tier"] == "medium")
        n_high = sum(1 for m in sized_meta if m["tier"] == "high")
        print(f"[POSITION_SIZING] {len(sized_meta)} signal(s) sized "
              f"(low={n_low} mid={n_mid} high={n_high}) "
              f"sample={sample}",
              flush=True)
    conn.commit()
    return created


def settle_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
) -> List[Dict[str, Any]]:
    """到期的快速模拟仓位按真实行情结算，返回结算明细。

    P0 改造：结算时同时反向回填 ai_decisions 表的结算字段
    （is_correct/settled/forward_pnl/exit_price/mfe_pct/mae_pct）。
    这样下游模块（reflection/risk_manager/backtest/causal_graph/
    trade_reflection_llm/gate_reflection）继续按既有 SQL 就能读到数据。
    """
    now = int(time.time())
    due = _rows(
        conn,
        """SELECT id, decision_id, asset, action, entry_price, entry_ts,
                  horizon_minutes, notional_usdt, max_price, min_price
           FROM quick_sim_trades
           WHERE settled = 0 AND entry_ts + horizon_minutes * 60 <= ?""",
        (now,),
    )
    settled: List[Dict[str, Any]] = []
    price_cache: Dict[str, float] = {}
    for trade in due:
        # 同一资产只取一次行情（避免每条到期仓都打外部价格源）
        if trade["asset"] not in price_cache:
            price_cache[trade["asset"]] = price_fn(trade["asset"])
        price = price_cache[trade["asset"]]
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        entry = float(trade["entry_price"])
        exit_price = float(price)
        pnl_pct = (exit_price - entry) / entry * 100
        if trade["action"] == "SELL":
            pnl_pct = -pnl_pct
        # ── v301+ P0+：显式交易成本（手续费 + 滑点，往返）──
        # 扣完再落库；cost_bps 列存"如果当时走 fee/slip 模型该扣多少 bps"，用于审计。
        from cost_model import apply_costs, round_trip_cost_bps
        cost_bps = round_trip_cost_bps(trade["asset"])
        pnl_pct_net = apply_costs(pnl_pct, trade["asset"], trade["action"])
        pnl_usdt = float(trade["notional_usdt"]) * pnl_pct_net / 100
        verdict = "WIN" if pnl_pct_net > 0 else "LOSS" if pnl_pct_net < 0 else "BREAKEVEN"

        # ── P0: 计算 mfe_pct / mae_pct（用 update_open_extremes 已跟踪的 max/min）──
        # 注意：SQL UPDATE 时 max_price = MAX(COALESCE(max_price, entry_price), exit_price)
        # 所以"结算后最终 max_price" = max(trade["max_price"], exit_price)（fallback 到 entry 再 exit）。
        # 必须用"更新后"的 max/min 计算，不能直接 SELECT 旧值。
        final_max = exit_price if trade["max_price"] is None else max(trade["max_price"], exit_price)
        final_min = exit_price if trade["min_price"] is None else min(trade["min_price"], exit_price)
        if trade["action"] == "BUY":
            mfe_pct = (final_max - entry) / entry * 100 if entry > 0 else 0.0
            mae_pct = (entry - final_min) / entry * 100 if entry > 0 else 0.0
        else:  # SELL
            mfe_pct = (entry - final_min) / entry * 100 if entry > 0 else 0.0
            mae_pct = (final_max - entry) / entry * 100 if entry > 0 else 0.0
        mfe_pct = max(0.0, round(mfe_pct, 4))
        mae_pct = max(0.0, round(mae_pct, 4))

        conn.execute(
            """UPDATE quick_sim_trades
               SET exit_price=?, exit_ts=?, pnl_pct=?, pnl_usdt=?, verdict=?, settled=1,
                   mfe_pct=?, mae_pct=?, cost_bps=?,
                   max_price = MAX(COALESCE(max_price, entry_price), ?),
                   min_price = MIN(COALESCE(min_price, entry_price), ?)
               WHERE id=?""",
            (round(exit_price, 6), now, round(pnl_pct_net, 6), round(pnl_usdt, 6),
             verdict, mfe_pct, mae_pct, cost_bps,
             float(exit_price), float(exit_price), trade["id"]),
        )

        # ── P0: 反向回填 ai_decisions ──
        # 用 COALESCE 保护：若 ai_decisions 已有 is_correct/exit_price/mfe/mae 不覆盖
        # （避免 replay 阶段被覆盖回旧数据）。forward_pnl/settled 总是最新值。
        # v210.1: 同步 max_price/min_price，否则 analysis_process.list_recent 拿不到 MFE。
        if trade["decision_id"] is not None:
            conn.execute(
                """UPDATE ai_decisions
                   SET is_correct   = COALESCE(NULLIF(is_correct, ''), ?),
                       settled      = 1,
                       forward_pnl  = ?,
                       exit_price   = COALESCE(exit_price, ?),
                       max_price    = COALESCE(max_price, ?),
                       min_price    = COALESCE(min_price, ?),
                       mfe_pct      = COALESCE(mfe_pct, ?),
                       mae_pct      = COALESCE(mae_pct, ?)
                   WHERE id = ?""",
                (verdict, round(pnl_pct_net, 4), round(exit_price, 6),
                 final_max, final_min,
                 mfe_pct, mae_pct, trade["decision_id"]),
            )

        settled.append({
            "id": trade["id"], "decision_id": trade["decision_id"],
            "asset": trade["asset"], "action": trade["action"],
            "entry_price": entry, "exit_price": exit_price,
            "pnl_pct": round(pnl_pct_net, 4), "verdict": verdict,
            "mfe_pct": mfe_pct, "mae_pct": mae_pct,
            "cost_bps": cost_bps,
        })
    conn.commit()
    return settled


def cut_bleeding_trades(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
    max_loss_pct: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """v230+ P0.5：MAE-based soft stop-loss。

    经验观察（30 天 / 59 笔 quick_sim_trades）：

        ┌──────────┬────────────────┬────────────────┐
        │ action   │ verdict        │ max_mae_pct    │
        ├──────────┼────────────────┼────────────────┤
        │ BUY      │ WIN  n=20      │ 0.224 %        │
        │ BUY      │ LOSS n=16      │ 0.373 %        │
        │ SELL     │ WIN  n=10      │ 0.251 %        │
        │ SELL     │ LOSS n=12      │ 0.931 %        │
        └──────────┴────────────────┴────────────────┘

    max_mae_pct = 0.3 时，回放历史可砍 4 笔 LOSS（1 BUY + 3 SELL）、
    0 笔 WIN 误杀；累计浮亏回收约 0.96 个百分点。

    行为：
    1. 取所有 settled=0 的未平仓；
    2. 用当前行情计算单边浮亏（BUY 看 PAX，SELL 看 LIX）；
    3. 浮亏 > max_loss_pct → 以当前价记为当前价，强制 settled=1、
       verdict=LOSS，同步更新 ai_decisions。

    返回被砍的明细列表；max_loss_pct 为 None / 0 / 负 → 返回空。
    错误隔离：不抛异常；价格取不到 → 跳过该资产。
    """
    if max_loss_pct is None or max_loss_pct <= 0:
        return []
    open_rows = conn.execute(
        """SELECT id, decision_id, asset, action, entry_price, notional_usdt,
                  max_price, min_price
           FROM quick_sim_trades
           WHERE settled = 0"""
    ).fetchall()
    if not open_rows:
        return []
    price_cache: Dict[str, Optional[float]] = {}
    cut: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for row in open_rows:
        asset = row["asset"]
        if asset not in price_cache:
            price_cache[asset] = price_fn(asset)
        cur_price = price_cache[asset]
        if not isinstance(cur_price, (int, float)) or cur_price <= 0:
            continue
        entry = float(row["entry_price"])
        action = row["action"]
        if action == "BUY":
            loss_pct = (entry - cur_price) / entry * 100.0
            pnl_pct = (cur_price - entry) / entry * 100.0
        else:
            loss_pct = (cur_price - entry) / entry * 100.0
            pnl_pct = (entry - cur_price) / entry * 100.0
        if loss_pct < max_loss_pct:
            continue
        notional = float(row["notional_usdt"] or 0.0)
        pnl_usdt = notional * pnl_pct / 100.0 if notional else 0.0

        # v230.2 P0.2 平行修：MAE 用 update_open_extremes 已跟踪的极值，
        # 不用 cur_price（cur_price 只是触发条件，本轮内若 min_price/max_price
        # 更深，则实际 MAE 更大）。同 _settle_sl/_settle_tp2 的 final_* 算法。
        stored_max = row["max_price"]
        stored_min = row["min_price"]
        final_max = max(float(stored_max) if stored_max is not None else entry, cur_price)
        final_min = min(float(stored_min) if stored_min is not None else entry, cur_price)
        if action == "BUY":
            mae_pct = (entry - final_min) / entry * 100.0
        else:  # SELL
            mae_pct = (final_max - entry) / entry * 100.0
        mae_pct = round(max(0.0, mae_pct), 4)

        conn.execute(
            """UPDATE quick_sim_trades
               SET settled = 1,
                   exit_price = ?,
                   exit_ts = ?,
                   pnl_pct = ?,
                   pnl_usdt = ?,
                   verdict = 'LOSS',
                   mae_pct = ?,
                   max_price = MAX(COALESCE(max_price, entry_price), ?),
                   min_price = MIN(COALESCE(min_price, entry_price), ?)
               WHERE id = ? AND settled = 0""",
            (float(cur_price), now_ts, round(pnl_pct, 4),
             round(pnl_usdt, 6), mae_pct,
             float(cur_price), float(cur_price), int(row["id"])),
        )
        if row["decision_id"] is not None:
            conn.execute(
                """UPDATE ai_decisions
                   SET is_correct  = COALESCE(NULLIF(is_correct, ''), ?),
                       settled     = 1,
                       forward_pnl = COALESCE(forward_pnl, ?),
                       exit_price  = COALESCE(NULLIF(exit_price, 0), ?),
                       max_price   = COALESCE(NULLIF(max_price, 0), ?),
                       min_price   = COALESCE(NULLIF(min_price, 0), ?),
                       mae_pct     = COALESCE(mae_pct, ?)
                   WHERE id = ?""",
                ("LOSS", round(pnl_pct, 4), float(cur_price),
                 final_max, final_min,
                 mae_pct, int(row["decision_id"])),
            )
        cut.append({
            "id": row["id"],
            "asset": asset,
            "action": action,
            "entry_price": entry,
            "exit_price": float(cur_price),
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usdt": round(pnl_usdt, 6),
            "loss_pct": round(loss_pct, 4),
            "mae_pct": mae_pct,
            "reason": f"mae>={max_loss_pct}%",
        })
        # ── v208: 决策打点 — EXIT 强制平仓（loss cap） ──
        try:
            import trade_decisions as _td
            _td.log_decision(
                conn, asset=asset, intended_action=action,
                final_action="CLOSE", decision_type="EXIT",
                news_id=None, entry_price=entry,
                size_usdt=notional if notional else None,
                ai_decision_id=row.get("decision_id"),
                quick_sim_trade_id=int(row["id"]),
                guard="MAX_LOSS",
                context={"exit_price": float(cur_price),
                         "pnl_pct": round(pnl_pct, 4),
                         "mae_pct": mae_pct,
                         "reason": f"mae>={max_loss_pct}%"},
            )
        except Exception as _exc:
            print(f"[trade_decisions] EXIT(loss_cap) 打点失败: {_exc}", flush=True)
    if cut:
        conn.commit()
    return cut


def check_tp_sl(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
) -> List[Dict[str, Any]]:
    """v230+ P0.2：TP/SL 强制结算（enforce hardened exits）。

    读 paper_trading_settings（tp_enabled + tp1/tp2_mult + sl_mult +
    tp1/tp2_ratio），对每个未平仓触发：
      1. 触发器用 max_price / min_price（每轮 update_open_extremes 累计）+ 当前价；
      2. **TP1 命中**（当前价 OR min/max 到达阈值）且该 trade partial 尚未发生
         → 在 ai_decisions 上写部分平仓（partial_exit_price/ratio/pnl_pct），
         trade 仍保留 settled=0 等 TP2 或 SL 触发（或到期）。
      3. **TP2 命中**（trade 已 TP1 过，或价格一步到位） → 全平：混合 pnl =
         partial_pnl_pct（已锁定的 TP1） + (1 - partial_exit_ratio) × tp2_pnl_pct，
         verdict=WIN。
      4. **SL 命中**（价格不利方向越界） → 全平：混合 pnl = partial_pnl_pct +
         (1 - partial_exit_ratio) × sl_pnl_pct，verdict=LOSS。
      5. 优先级：先判 SL，再判 TP1，再判 TP2。同一 tick 不可能既 hit SL 又
         hit TP1（方向相反）。

    全局开关：config.TP_SL_ENABLED=0 → 整个函数立刻 return []，便于回滚。
    运行级开关：paper_trading_settings.tp_enabled=0 → 跳过。
    返回本轮触发明细（含 TP1_partial / TP2_close / SL_close 三类）。
    """
    if not getattr(config, "TP_SL_ENABLED", True):
        return []
    settings_row = _rows(
        conn,
        "SELECT tp_enabled, tp1_mult, tp2_mult, tp1_ratio, tp2_ratio, "
        "sl_mult FROM paper_trading_settings WHERE id = 1",
    )
    if not settings_row:
        return []
    s = settings_row[0]
    if not s.get("tp_enabled"):
        return []
    tp1_mult = float(s.get("tp1_mult") or 0)
    tp2_mult = float(s.get("tp2_mult") or 0)
    tp1_ratio = float(s.get("tp1_ratio") or 0)
    tp2_ratio = float(s.get("tp2_ratio") or 0)
    sl_mult = float(s.get("sl_mult") or 0)
    if tp1_mult <= 0 or tp2_mult <= 0 or sl_mult <= 0:
        return []
    if tp1_ratio <= 0 or tp2_ratio <= 0:
        return []
    if tp1_ratio + tp2_ratio > 1.0:
        return []
    open_rows = _rows(
        conn,
        """SELECT t.id, t.decision_id, t.asset, t.action, t.entry_price,
                  t.notional_usdt, t.max_price, t.min_price,
                  t.sl_price AS sl_price_override,
                  COALESCE(d.partial_exit_ratio, 0.0) AS partial_exit_ratio,
                  COALESCE(d.partial_pnl_pct, 0.0)    AS partial_pnl_pct,
                  d.partial_exit_price                 AS partial_exit_price
           FROM quick_sim_trades t
           LEFT JOIN ai_decisions d ON d.id = t.decision_id
           WHERE t.settled = 0""",
    )
    if not open_rows:
        return []
    price_cache: Dict[str, Optional[float]] = {}
    out: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for row in open_rows:
        asset = row["asset"]
        if asset not in price_cache:
            price_cache[asset] = price_fn(asset)
        cur_price = price_cache[asset]
        if not isinstance(cur_price, (int, float)) or cur_price <= 0:
            continue
        cur_price = float(cur_price)
        entry = float(row["entry_price"])
        if entry <= 0:
            continue
        action = row["action"]
        sgn = 1 if action == "BUY" else -1
        max_price = row["max_price"]
        min_price = row["min_price"]
        ref_max = max(max_price or entry, cur_price, entry)
        ref_min = min(min_price or entry, cur_price, entry)
        tp1_price = entry * (1.0 + tp1_mult / 100.0 * sgn)
        tp2_price = entry * (1.0 + tp2_mult / 100.0 * sgn)
        # P0.2 part B: TP1 后的 breakeven-move 覆写默认 SL。
        # 若该 trade 已 TP1 过，sl_price_override 由 _record_tp1_partial 写入 entry±offset；
        # 未 TP1 时仍按 entry×(1±sl_mult) 公式。
        sl_override = row.get("sl_price_override")
        if sl_override is not None and float(sl_override) > 0:
            sl_price = float(sl_override)
        else:
            sl_price = entry * (1.0 - sl_mult / 100.0 * sgn)
        if sgn == 1:
            tp1_hit = ref_max >= tp1_price
            tp2_hit = ref_max >= tp2_price
            sl_hit = ref_min <= sl_price
        else:
            tp1_hit = ref_min <= tp1_price
            tp2_hit = ref_min <= tp2_price
            sl_hit = ref_max >= sl_price
        if sl_hit:
            _settle_sl(conn, row, cur_price, entry, sgn, sl_price,
                       tp1_ratio, out, now_ts)
            continue
        if tp2_hit:
            _settle_tp2(conn, row, cur_price, entry, sgn, tp1_price,
                        tp2_price, tp1_ratio, tp2_ratio, out, now_ts)
            continue
        if tp1_hit:
            _record_tp1_partial(conn, row, cur_price, entry, sgn, tp1_price,
                                tp1_ratio, out)
    if out:
        conn.commit()
    return out


def _settle_sl(conn, row, cur_price, entry, sgn, sl_price,
               tp1_ratio_unused, out, now_ts):
    """SL 全平 helper（被 check_tp_sl 内部调用）。

    tp1_ratio_unused 是签名占位（SL 路径只看 partial_exit_ratio，不看 ratio 配置）。
    """
    partial_ratio = float(row["partial_exit_ratio"] or 0.0)
    partial_pnl = float(row["partial_pnl_pct"] or 0.0)
    sl_pnl_theory = sgn * (sl_price - entry) / entry * 100.0
    if partial_ratio == 0.0:
        blended = sl_pnl_theory
    else:
        blended = partial_pnl + (1.0 - partial_ratio) * sl_pnl_theory
    # v230.2 P0.1：verdict 由 blended pnl 实际符号决定，不再强制 LOSS。
    # 场景：TP1 已锁利 + SL 在 breakeven 触发时，blended = partial_pnl > 0，
    # 旧逻辑写 LOSS 会让"实际赚钱"的交易被错判，下游 winrate 与 is_correct 全错位。
    verdict = "WIN" if blended > 0 else "LOSS" if blended < 0 else "BREAKEVEN"
    notional = float(row["notional_usdt"] or 0.0)
    pnl_usdt = notional * blended / 100.0 if notional else 0.0
    # v230.2 P0.2：mae 用 update_open_extremes 已跟踪的 max/min 计算。
    # row["max_price"/"min_price"] 在 check_tp_sl 之前已被 update_open_extremes
    # 用本轮 cur_price 更新过（fallback 到 entry_price），所以直接拿来算即可。
    # 旧逻辑 abs(sgn*(cur_price-entry)/entry*100) 把 cur_price 当 mae，
    # 在 breakeven-move SL 场景下会写出 mae≈0，掩盖真实回撤幅度。
    final_max = float(row["max_price"] or entry)
    final_min = float(row["min_price"] or entry)
    if sgn == 1:  # BUY：adverse = entry - min
        mae_now = max(0.0, (entry - final_min) / entry * 100.0)
    else:  # SELL：adverse = max - entry
        mae_now = max(0.0, (final_max - entry) / entry * 100.0)
    conn.execute(
        """UPDATE quick_sim_trades
           SET settled = 1, exit_price = ?, exit_ts = ?,
               pnl_pct = ?, pnl_usdt = ?,
               verdict = ?, mae_pct = ?,
               max_price = MAX(COALESCE(max_price, entry_price), ?),
               min_price = MIN(COALESCE(min_price, entry_price), ?)
           WHERE id = ? AND settled = 0""",
        (float(cur_price), now_ts, round(blended, 4),
         round(pnl_usdt, 6), verdict, round(mae_now, 4),
         float(cur_price), float(cur_price), int(row["id"])),
    )
    if row["decision_id"] is not None:
        conn.execute(
            """UPDATE ai_decisions
               SET is_correct  = COALESCE(NULLIF(is_correct, ''), ?),
                   settled     = 1,
                   forward_pnl = COALESCE(forward_pnl, ?),
                   exit_price  = COALESCE(NULLIF(exit_price, 0), ?),
                   max_price   = COALESCE(NULLIF(max_price, 0), ?),
                   min_price   = COALESCE(NULLIF(min_price, 0), ?),
                   mae_pct     = COALESCE(mae_pct, ?)
               WHERE id = ?""",
            (verdict, round(blended, 4), float(cur_price),
             final_max, final_min,
             round(mae_now, 4), int(row["decision_id"])),
        )
    out.append({
        "id": int(row["id"]),
        "asset": row["asset"],
        "action": row["action"],
        "trigger": "SL",
        "entry_price": entry,
        "sl_price": round(sl_price, 6),
        "exit_price": cur_price,
        "partial_exit_ratio": partial_ratio,
        "pnl_pct": round(blended, 4),
        "pnl_usdt": round(pnl_usdt, 6),
    })


def _settle_tp2(conn, row, cur_price, entry, sgn, tp1_price, tp2_price,
                tp1_ratio, tp2_ratio, out, now_ts):
    """TP2 全平 helper：blended pnl。

    若 partial 未发生：按理论 tp1/tp2 pnl 加权（一步到位假设）。
    若 partial 已发生：partial_pnl + (1 - partial_ratio) × cur_pnl。
    """
    exit_pnl_pct = sgn * (cur_price - entry) / entry * 100.0
    partial_ratio = float(row["partial_exit_ratio"] or 0.0)
    partial_pnl = float(row["partial_pnl_pct"] or 0.0)
    tp1_pnl_theory = sgn * (tp1_price - entry) / entry * 100.0
    tp2_pnl_theory = sgn * (tp2_price - entry) / entry * 100.0
    if partial_ratio == 0.0:
        blended = tp1_ratio * tp1_pnl_theory + tp2_ratio * tp2_pnl_theory
    else:
        blended = partial_pnl + (1.0 - partial_ratio) * exit_pnl_pct
    # v230.2 P0.1：verdict 由 blended pnl 实际符号决定，不再强制 WIN。
    # 场景：TP1 partial 后价格反转跌破 TP1 → 在更低价触发 TP2 时 blended 可能为负，
    # 旧逻辑写 WIN 会把"实际亏钱"的交易错判为胜。
    verdict = "WIN" if blended > 0 else "LOSS" if blended < 0 else "BREAKEVEN"
    notional = float(row["notional_usdt"] or 0.0)
    pnl_usdt = notional * blended / 100.0 if notional else 0.0
    # v230.2 P0.2：mfe 用 update_open_extremes 已跟踪的 max/min 计算（同 mae 原理）。
    # 旧逻辑把 cur_price 当 mfe，会在 TP2 触发瞬间低估"持仓期间最大有利偏移"，
    # 尤其是先冲到 TP1 中途后回撤再冲 TP2 的场景。
    final_max = float(row["max_price"] or entry)
    final_min = float(row["min_price"] or entry)
    if sgn == 1:  # BUY：favorable = max - entry
        mfe_now = max(0.0, (final_max - entry) / entry * 100.0)
    else:  # SELL：favorable = entry - min
        mfe_now = max(0.0, (entry - final_min) / entry * 100.0)
    conn.execute(
        """UPDATE quick_sim_trades
           SET settled = 1, exit_price = ?, exit_ts = ?,
               pnl_pct = ?, pnl_usdt = ?,
               verdict = ?, mfe_pct = ?,
               max_price = MAX(COALESCE(max_price, entry_price), ?),
               min_price = MIN(COALESCE(min_price, entry_price), ?)
           WHERE id = ? AND settled = 0""",
        (float(cur_price), now_ts, round(blended, 4),
         round(pnl_usdt, 6), verdict, round(mfe_now, 4),
         float(cur_price), float(cur_price), int(row["id"])),
    )
    if row["decision_id"] is not None:
        conn.execute(
            """UPDATE ai_decisions
               SET is_correct  = COALESCE(NULLIF(is_correct, ''), ?),
                   settled     = 1,
                   forward_pnl = COALESCE(forward_pnl, ?),
                   exit_price  = COALESCE(NULLIF(exit_price, 0), ?),
                   max_price   = COALESCE(NULLIF(max_price, 0), ?),
                   min_price   = COALESCE(NULLIF(min_price, 0), ?),
                   mfe_pct     = COALESCE(mfe_pct, ?)
               WHERE id = ?""",
            (verdict, round(blended, 4), float(cur_price),
             final_max, final_min,
             round(mfe_now, 4), int(row["decision_id"])),
        )
    out.append({
        "id": int(row["id"]),
        "asset": row["asset"],
        "action": row["action"],
        "trigger": "TP2",
        "entry_price": entry,
        "tp2_price": round(tp2_price, 6),
        "exit_price": cur_price,
        "partial_exit_ratio": partial_ratio,
        "pnl_pct": round(blended, 4),
        "pnl_usdt": round(pnl_usdt, 6),
    })


def _record_tp1_partial(conn, row, cur_price, entry, sgn, tp1_price,
                        tp1_ratio, out):
    """TP1 部分平仓 helper：trade 保持 open，仅写 ai_decisions.partial_*.

    v230+ P0.2 part B：breakeven-move。
    TP1 命中后把 quick_sim_trades.sl_price 设为 entry × (1 ± offset × sgn)，
    后续轮次的 check_tp_sl 看到该 override 后即把 SL 上移到 breakeven。
    """
    partial_ratio = float(row["partial_exit_ratio"] or 0.0)
    if partial_ratio >= tp1_ratio:
        return
    tp1_pnl_actual = sgn * (cur_price - entry) / entry * 100.0
    partial_pnl_added = tp1_ratio * tp1_pnl_actual
    notional = float(row["notional_usdt"] or 0.0)
    partial_usdt = notional * partial_pnl_added / 100.0 if notional else 0.0
    mfe_now = abs(sgn * (cur_price - entry) / entry * 100.0)
    if row["decision_id"] is not None:
        conn.execute(
            """UPDATE ai_decisions
               SET partial_exit_price = ?,
                   partial_exit_ratio = ?,
                   partial_pnl_pct    = COALESCE(partial_pnl_pct, 0) + ?,
                   max_price         = COALESCE(max_price, ?),
                   min_price         = COALESCE(min_price, ?),
                   mfe_pct            = COALESCE(mfe_pct, ?)
               WHERE id = ?""",
            (round(tp1_price, 6), float(tp1_ratio),
             round(partial_pnl_added, 4),
             float(cur_price), float(cur_price),
             round(mfe_now, 4),
             int(row["decision_id"])),
        )
    breakeven_applied = False
    be_sl_price: Optional[float] = None
    if getattr(config, "TP_SL_BREAKEVEN_ENABLED", True):
        offset_pct = float(getattr(config, "TP_SL_BREAKEVEN_OFFSET_PCT", 0.0) or 0.0)
        be_sl_price = entry * (1.0 + offset_pct / 100.0 * sgn)
        # 仅在该 trade 尚未设置过 override 时覆写（保持幂等：第二轮 TP1 不会把价格重写）。
        existing = row.get("sl_price_override")
        if existing is None:
            conn.execute(
                "UPDATE quick_sim_trades SET sl_price = ? WHERE id = ?",
                (round(be_sl_price, 6), int(row["id"])),
            )
            breakeven_applied = True
    out.append({
        "id": int(row["id"]),
        "decision_id": row["decision_id"],
        "asset": row["asset"],
        "action": row["action"],
        "trigger": "TP1_partial",
        "entry_price": entry,
        "tp1_price": round(tp1_price, 6),
        "exit_price": cur_price,
        "partial_exit_ratio": float(tp1_ratio),
        "partial_pnl_pct": round(partial_pnl_added, 4),
        "partial_usdt": round(partial_usdt, 6),
        "breakeven_applied": breakeven_applied,
        "breakeven_sl_price": round(be_sl_price, 6) if be_sl_price is not None else None,
    })


def update_open_extremes(
    conn: sqlite3.Connection,
    price_fn: Callable[[str], Optional[float]],
) -> int:
    """给未结算持仓按当前行情刷新期间最高/最低价（每轮 watcher 调用，累计过程质量）。

    只用真实行情，取不到价就跳过对应资产；返回更新的持仓行数。
    """
    open_rows = _rows(conn, "SELECT DISTINCT asset FROM quick_sim_trades WHERE settled = 0")
    price_cache: Dict[str, float] = {}
    updated = 0
    for row in open_rows:
        asset = row["asset"]
        if asset not in price_cache:
            price_cache[asset] = price_fn(asset)
        price = price_cache[asset]
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        cur = conn.execute(
            """UPDATE quick_sim_trades
               SET max_price = MAX(COALESCE(max_price, entry_price), ?),
                   min_price = MIN(COALESCE(min_price, entry_price), ?)
               WHERE settled = 0 AND asset = ?""",
            (float(price), float(price), asset),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def _profitability(rows: List[Dict[str, Any]], notional: float) -> Dict[str, Any]:
    wins = sum(row["verdict"] == "WIN" for row in rows)
    losses = sum(row["verdict"] == "LOSS" for row in rows)
    decided = wins + losses
    total_pnl_pct = sum(float(row["pnl_pct"] or 0.0) for row in rows)
    total_pnl_usdt = sum(float(row["pnl_usdt"] or 0.0) for row in rows)
    invested = sum(float(row["notional_usdt"] or 0.0) for row in rows) or notional * len(rows)
    return {
        "settled": len(rows),
        "wins": wins,
        "losses": losses,
        "breakeven": len(rows) - decided,
        "winrate_pct": round(wins / decided * 100, 2) if decided else None,
        "avg_pnl_pct": round(total_pnl_pct / len(rows), 4) if rows else None,
        "total_pnl_pct": round(total_pnl_pct, 4),
        "total_pnl_usdt": round(total_pnl_usdt, 4),
        "return_on_notional_pct": round(total_pnl_usdt / invested * 100, 4) if invested else None,
        "profitable": total_pnl_usdt > 0 if rows else None,
    }


def evaluation(
    conn: sqlite3.Connection,
    recent_limit: int = 20,
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """快速模拟交易的盈利率评测，全部来自真实结算记录。

    v180 P0+：新增 hours / from_ts / to_ts（UTC 秒）三选一过滤，与
    /api/history/news-trades 同一套口径 —— 今日/近7天/近30天用 hours，
    自定义时段直接传 UTC 秒对。三个参数都缺省时维持原"全量聚合"行为，
    避免影响旧调用方（如 e2e_channel_trace.py）。
    """
    # 窗口 → (lo, hi)；缺省 = (-inf, +inf)，等价于全量
    lo, hi = _resolve_window(hours, from_ts, to_ts)
    has_window = lo is not None or hi is not None
    settled_sql = (
        """SELECT qt.asset, qt.action, qt.gate_passed, qt.verdict, qt.pnl_pct,
                  qt.pnl_usdt, qt.notional_usdt,
                  COALESCE(NULLIF(ad.agent_model_id, ''), '(未知)') AS model
           FROM quick_sim_trades qt
           LEFT JOIN ai_decisions ad ON ad.id = qt.decision_id
           WHERE qt.settled = 1"""
    )
    settled_params: tuple = ()
    if lo is not None:
        settled_sql += " AND qt.exit_ts >= ?"
        settled_params = (lo,) + settled_params
    if hi is not None:
        settled_sql += " AND qt.exit_ts <= ?"
        settled_params = settled_params + (hi,)
    settled_rows = _rows(conn, settled_sql, settled_params)

    open_rows_sql = (
        """SELECT id, decision_id, asset, action, entry_price, entry_ts, horizon_minutes,
                  gate_passed, gate_reason
           FROM quick_sim_trades WHERE settled = 0"""
    )
    open_rows_params: tuple = ()
    # v302+ P0+【修复】open 也要按窗口过滤 —— 否则 B 卡片"待结算 4 · 已结算 2"
    # 会把全库的 open 单全塞进来,total/avg_pnl 口径被 open 单污染。
    # 与 settled_rows 用 exit_ts 一致(open 单没有 exit_ts,用 entry_ts)。
    if lo is not None:
        open_rows_sql += " AND entry_ts >= ?"
        open_rows_params = (lo,) + open_rows_params
    if hi is not None:
        open_rows_sql += " AND entry_ts <= ?"
        open_rows_params = open_rows_params + (hi,)
    open_rows_sql += " ORDER BY entry_ts DESC"
    open_rows = _rows(conn, open_rows_sql, open_rows_params)
    # recent：默认按 entry_ts 过滤窗口（如设了窗口），保证 "近 N 笔" 严格在窗口内
    recent_sql = (
        """SELECT id, decision_id, asset, action, entry_price, exit_price, pnl_pct,
                  pnl_usdt, verdict, gate_passed, gate_reason, entry_ts, exit_ts, settled
           FROM quick_sim_trades"""
    )
    recent_params: tuple = ()
    if has_window:
        clauses = []
        if lo is not None:
            clauses.append("entry_ts >= ?")
            recent_params = (lo,) + recent_params
        if hi is not None:
            clauses.append("entry_ts <= ?")
            recent_params = recent_params + (hi,)
        recent_sql += " WHERE " + " AND ".join(clauses)
    recent_sql += " ORDER BY id DESC LIMIT ?"
    recent = _rows(conn, recent_sql, recent_params + (recent_limit,))
    notional = config.QUICK_SIM_NOTIONAL_USDT
    by_asset: Dict[str, List[Dict[str, Any]]] = {}
    for row in settled_rows:
        by_asset.setdefault(row["asset"], []).append(row)
    # 按模型归因（JOIN ai_decisions.agent_model_id）→ 对比各模型分析质量的快样本口径
    by_model_raw: Dict[str, List[Dict[str, Any]]] = {}
    for row in settled_rows:
        by_model_raw.setdefault(row["model"], []).append(row)
    by_model = [
        {"model": model, **_profitability(rows, notional)}
        for model, rows in sorted(by_model_raw.items(), key=lambda kv: -len(kv[1]))
    ]
    overall = _profitability(settled_rows, notional)
    # 口径统一：与 /api/replay/stats 的 paper overall 字段对齐（total=建仓总笔数，
    # open=跟踪中、settled=已结算），让「快速评测」与「真实持仓」以同一字段结构呈现，
    # 前端反思系统 / 模拟盘两处可直接同源复用。
    overall["total"] = len(settled_rows) + len(open_rows)
    overall["open"] = len(open_rows)
    return {
        "enabled": config.QUICK_SIM_ENABLED,
        "horizon_minutes": config.QUICK_SIM_HORIZON_MINUTES,
        "notional_usdt": notional,
        "open_trades": len(open_rows),
        "overall": overall,
        "gate_passed": _profitability([row for row in settled_rows if row["gate_passed"]], notional),
        "gate_rejected": _profitability([row for row in settled_rows if not row["gate_passed"]], notional),
        "by_model": by_model,
        "by_asset": [
            {"asset": asset, **_profitability(rows, notional)}
            for asset, rows in sorted(by_asset.items())
        ],
        "open": open_rows[:recent_limit],
        "recent": recent,
        "method": "全部 BUY/SELL 信号按真实行情建仓，到期按真实行情结算；闸门结论仅作分组标记",
    }


def run_cycle(price_fn: Callable[[str], Optional[float]]) -> Dict[str, Any]:
    """一次完整周期：建仓 + 结算，返回本轮变化。"""
    if not config.QUICK_SIM_ENABLED:
        return {"opened": [], "settled": []}
    conn = db.get_connection()
    try:
        opened = open_trades(conn, price_fn)
        update_open_extremes(conn, price_fn)
        # v230+ P0.2：TP/SL 强制结算（TP1 partial + TP2/SL 全平）
        tp_sl_hits = check_tp_sl(conn, price_fn)
        # v230+ P0.5：先砍失控单，再让 settle_trades 处理到期单
        cut = cut_bleeding_trades(
            conn, price_fn,
            max_loss_pct=(config.EARLY_LOSS_CUT_PCT if config.EARLY_LOSS_CUT_ENABLED else None),
        )
        settled = settle_trades(conn, price_fn)
        return {"opened": opened, "tp_sl_hits": tp_sl_hits,
                "cut": cut, "settled": settled,
                # v231+ (C4): 报告 QUALITY_FILTER 拦截数给 API/dashboard
                "quality_rejected_count": getattr(opened, 'quality_rejected_count', 0),
                "quality_rejected_sample": getattr(opened, 'quality_rejected', [])[:5]}
    finally:
        conn.close()


def snapshot(
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """给 API 用的评测快照。

    v180 P0+：转发 hours / from_ts / to_ts 到 evaluation()，让反思页"快速评测"
    B 卡能按用户选定的时段过滤；保留无参调用以兼容旧缓存 / e2e 测试。
    """
    conn = db.get_connection()
    try:
        return evaluation(conn, hours=hours, from_ts=from_ts, to_ts=to_ts)
    finally:
        conn.close()
