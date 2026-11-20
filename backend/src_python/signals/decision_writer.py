"""decision_writer — trade_decisions 的唯一写入口（v342 口径统一）

背景（v342 结构层 #⑥）：
    trade_decisions 此前有三个写入点，各写各的口径：
      · trade_decisions.log_decision      —— 有 signal_weight 事前权重，无 status/score
      · decision_engine.propose_one       —— 写 status='open'，confidence 其实取的是 algo_score
      · bridge_server./api/decision/record —— 写 status='open'，但注释明说 score/confidence
                                              "不再落库"，只塞进 context_json
    结果：同名列在不同路径含义不同，胜率 / 闸门拒绝率 / 结算率无法分层统计；
    另有两个路径写 status 列，而迁移从未建这一列（全新库直接报错）。

本模块提供单一写入口，规则：
    1) 一个 INSERT，列集合固定；缺的列写 NULL，不猜值。
    2) 口径落成显式列：decision_source / score / confidence / structure_score /
       confidence_detail；不再依赖 context_json 里的自由字段。
    3) entry_price 缺失时按决策时点回溯行情（db.resolve_entry_price），查不到留 NULL。
    4) 落库后同步写「事前权重」(signal_weight_evaluator, trigger_kind='insert')——
       三条路径行为一致，失败只留日志不阻断。
    5) 纯增量：不改表语义、不动老数据；调用方原签名保留。
"""
import json
import sqlite3
import time
from typing import Any, Dict, Optional

# 写入口标记（写进 decision_source，供统计分层）
SOURCE_LOG_DECISION = "log_decision"
SOURCE_ALGO_MOVE = "algo_move"
SOURCE_BRIDGE_API = "bridge_api"
SOURCE_WATCHDOG = "watchdog"


def _text(value: Any, limit: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    out = str(value)
    if limit is not None:
        out = out[:limit]
    return out or None


def _num(value: Any, low: Optional[float] = None, high: Optional[float] = None,
         digits: int = 4) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return round(out, digits)


def write_decision(
    conn: sqlite3.Connection,
    *,
    asset: str,
    intended_action: str,
    final_action: Optional[str] = None,
    decision_type: str = "ENTRY",
    source: str = "unknown",
    decision_ts: Optional[float] = None,
    news_id: Optional[int] = None,
    event_strength: Optional[str] = None,
    news_star: Optional[float] = None,
    entry_price: Optional[float] = None,
    planned_stop: Optional[float] = None,
    planned_target: Optional[float] = None,
    size_usdt: Optional[float] = None,
    deny_reason: Optional[str] = None,
    guard: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    quick_sim_trade_id: Optional[int] = None,
    ai_decision_id: Optional[int] = None,
    score: Optional[float] = None,
    confidence: Optional[float] = None,
    structure_score: Optional[float] = None,
    confidence_detail: Optional[Dict[str, Any]] = None,
    status: Optional[str] = "open",
    persist_signal_weight: bool = True,
    commit: bool = True,
    code_fingerprint: Optional[str] = None,
) -> int:
    """写一条 trade_decisions，返回新 id。

    参数口径:
        decision_type : ENTRY / EXIT / REJECT / HOLD / ADJUST
        source        : 写入口标记（用本模块 SOURCE_* 常量）
        score         : 方向分 -1..1（有则写，无则 NULL）
        confidence    : 0-100（**只有真是这口径才传**；algo_score 不是 confidence）
        status        : 'open' / 'settled' / None
    """
    asset_key = str(asset or "").strip().upper()
    if not asset_key:
        raise ValueError("decision_writer: asset 不能为空")
    act = str(intended_action or "HOLD").strip().upper()
    final = str(final_action).strip().upper() if final_action else act
    dtype = str(decision_type or "ENTRY").strip().upper()
    ts = float(decision_ts) if decision_ts is not None else time.time()

    # v383: 人工静默闸门 —— 用户按品种静默后，三条写入口统一降级为 HOLD。
    # 放在写入口而不是前端过滤：信号有三条写入路径，只在其中一条拦等于没拦。
    try:
        from signals import decision_actions as _da
        _et = ""
        if isinstance(context, dict):
            _et = str(context.get("event_type_code") or context.get("event_type") or "")
        _mute = _da.is_asset_muted(conn, asset_key, ts, event_type=_et)
    except Exception:
        _mute = None
    if _mute:
        deny_reason = (f"{_mute} | 原意图 {act}" if act else _mute)
        final = "HOLD"
        dtype = "REJECT"

    # entry_price 兜底（v310 P0 的规则，统一到写入口）
    if entry_price is None:
        try:
            import db as _db
            entry_price = _db.resolve_entry_price(conn, asset_key, ts)
        except Exception:
            entry_price = None

    ctx = context if isinstance(context, dict) else {}
    ctx_json = json.dumps(ctx, ensure_ascii=False)
    detail_json = (json.dumps(confidence_detail, ensure_ascii=False)
                   if isinstance(confidence_detail, dict) else None)

    # v477: 每条打点都带代码指纹 —— 原来只有 ai_decisions 有，
    # trade_decisions 换代后无法归代，复盘只能按时间猜。
    if not code_fingerprint:
        try:
            from code_fingerprint import fingerprint as _cfp_fp
            code_fingerprint = _cfp_fp()
        except Exception:
            code_fingerprint = "unknown"

    cur = conn.execute(
        """INSERT INTO trade_decisions
           (decision_ts, asset, intended_action, final_action, decision_type,
             news_id, event_strength, news_star,
             entry_price, planned_stop, planned_target, size_usdt,
             deny_reason, guard, context_json,
             quick_sim_trade_id, ai_decision_id,
             status, decision_source, score, confidence,
             structure_score, confidence_detail, code_fingerprint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts, asset_key, act, final, dtype,
            news_id, event_strength, news_star,
            entry_price, planned_stop, planned_target, size_usdt,
            _text(deny_reason, 2000), guard, ctx_json,
            quick_sim_trade_id, ai_decision_id,
            status, _text(source, 32), _num(score, -1.0, 1.0),
            _num(confidence, 0.0, 100.0, digits=2),
            _num(structure_score, -2.0, 2.0, digits=4), detail_json,
            str(code_fingerprint or ""),
        ),
    )
    new_id = int(cur.lastrowid or 0)

    # 事前权重：与 log_decision 原有行为一致（三条路径都写，不再只有一条写）
    if persist_signal_weight:
        try:
            from signals import signal_weight_evaluator as _swe
            _swe.evaluate_and_persist_trade(
                conn, new_id, trigger_kind="insert", now_ts=int(ts),
            )
        except Exception as exc:
            print(f"[decision_writer] signal_weight 失败 id={new_id}: "
                  f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)

    if commit:
        conn.commit()
    return new_id


def has_decision_columns(conn: sqlite3.Connection) -> Dict[str, bool]:
    """自检用：确认统一口径所需的列都已在库上（供验收脚本/测试调用）。"""
    need = ("status", "decision_source", "score", "confidence",
            "structure_score", "confidence_detail")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_decisions)")}
    return {name: (name in cols) for name in need}
