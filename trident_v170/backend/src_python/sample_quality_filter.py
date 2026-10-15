#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反思系统的"样本质量过滤"工具 (v202+, v204 R8)

背景:
    V4-V8 ablation 跑出"XAU BUY 方向 alpha +46%"、"XAU SELL 正向 alpha"等结论。
    但 V8 验证发现: 全部 24 笔 trade 都是 trade_gate_reason 非空 + evidence_action=HOLD
    的事故样本 (gate 失败但仍 trade)。
    V4-V8 算 alpha 时没审计样本质量 → 结论完全无效。

    DEEP_REFLECTION §5 又揭示一个新盲区: 同批次生成的决策共享 trade pool (LLM batch 输出),
    所谓"独立验证"实际是同一事件的不同切片, alpha 结论也不可信。

修复:
    本模块提供反思系统的"质检门" - 任何 ablation / reflection 在算 alpha 前必须先调本模块。
    6 条硬规则:
        R1. trade_gate_reason 为空       (gate 必须通过)
        R2. evidence_action != 'HOLD'    (不能是 AI 建议观望却执行了)
        R3. pnl != 0 占位                (K 线 fallback 会让 pnl=0)
        R4. asset 在模拟盘 allowlist     (config.SIM_ALLOWED_ASSETS)
        R7. evidence_confidence >= 20    (AI 置信度阈值，v202+ 是 40；2026-10-08 shadow 投影支持降到 20)
        R8. 同批次兄弟决策 <= 3          (v204+, DEEP_REFLECTION §5 揭示的盲区)
            - 同 asset ±5s 创建的其他 ai_decisions 数量 > 3 → 标 invalid
            - 因为 LLM 一次会输出多条决策, 同批次的 trade 共享 trade pool,
              不能算"独立样本"。
        R9. status != 'REJECTED'         (v231+, 已被管理员拒签的 decision 不应 trade)

凡不满足 R1-R9 任一 → 标记 is_valid_for_alpha=0, 反思系统拒绝计入 alpha 评估。

v231+ (2026-10-10) 新增: pre_trade_check() 把 R1/R2/R4/R7/R8/R9 改造成
"建仓前预检" 供 quick_sim.open_trades() 调用, 不动 DB schema / verifier。
R3 是事后规则, 不参与预检。

不动 DB schema, 不动 verifier, 不动执行层 (quick_sim 接受外部调用, 不属于
"执行层" 范畴)。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import config  # v231+ TRADE_GATE_PASSED_REASON 单一来源


# v202+: 模拟盘允许交易的资产列表 (与 paper_trading_runner 同步)
# v231+ (C3): 不再硬编码, 默认值读 config.SIM_ALLOWED_ASSETS
def _default_allowed_assets():
    return config.SIM_ALLOWED_ASSETS  # tuple
DEFAULT_SIM_ALLOWED_ASSETS = ("XAU", "WTI")  # 兜底, 若 config import 失败用此
# v202+: AI 置信度阈值 (与 trade_gate_reason 中的"低于闸门 10.0"对齐)
# v231+ (C3): 默认读 config.EVIDENCE_CONFIDENCE_GATE (env 改了重启生效)
DEFAULT_MIN_CONFIDENCE = config.EVIDENCE_CONFIDENCE_GATE
# v204+: 同批次决策上限 (DEEP_REFLECTION §5)
DEFAULT_MAX_BATCH_SIBLINGS = 3   # 兄弟决策数 > 3 (即同批次 ≥ 4 条) → invalid
# v204+: 同批次时间窗口 (秒)
DEFAULT_BATCH_WINDOW_SEC = 5


def _connect() -> sqlite3.Connection:
    import db
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _count_sibling_decisions(decision_id: int, asset: str,
                              conn: sqlite3.Connection,
                              window_sec: int = DEFAULT_BATCH_WINDOW_SEC) -> int:
    """R8 helper: 找与给定 decision 同 asset 且 ±window_sec 创建的其他 ai_decisions 数.

    用 SQLite strftime 做 epoch 差 (注意: SQLite 把 created_at 当 UTC 解析, 与 ai_decisions
    写入时一致的 'YYYY-MM-DD HH:MM:SS' 格式保持兼容 — 差值比较仍正确).

    返回兄弟决策数 (不含自身).
    """
    if not decision_id or not asset or conn is None:
        return 0
    try:
        # 1) 拿目标 decision 的 epoch
        row = conn.execute(
            "SELECT CAST(strftime('%s', created_at) AS INTEGER) "
            "FROM ai_decisions WHERE id = ?",
            (decision_id,)
        ).fetchone()
        if not row or row[0] is None:
            return 0
        target_epoch = int(row[0])

        # 2) 找同 asset ±window_sec 的兄弟 (不含自身)
        c = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions "
            "WHERE id != ? "
            "  AND UPPER(COALESCE(target_asset, '')) = ? "
            "  AND ABS(CAST(strftime('%s', created_at) AS INTEGER) - ?) <= ?",
            (decision_id, asset.upper(), target_epoch, window_sec)
        ).fetchone()
        return int(c[0]) if c else 0
    except Exception:
        return 0  # 出错时默认 0, 不挡 (宁放过不误杀)


def _check_one(trade_row, decision_row,
               allowed_assets: tuple, min_confidence: float,
               max_batch_siblings: int = DEFAULT_MAX_BATCH_SIBLINGS,
               conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """对单条 trade 跑 R1-R8 检查, 返回 {is_valid, reasons[], flags[]}

    Args:
        trade_row / decision_row: 当前 trade 行 (含 d.* 联表字段).
                                   R8 需要 decision_id + decision_created_at.
        allowed_assets: allowlist tuple
        min_confidence: 置信度阈值
        max_batch_siblings: R8 上限 (兄弟决策数 ≤ 此值才算 valid).
                            None 或 0 = 关闭 R8 (向后兼容).
        conn: sqlite 连接 (R8 查 ai_decisions 需要).
    """
    reasons: List[str] = []
    flags: Dict[str, bool] = {}

    # R1: gate 必须通过 (trade_gate_reason 为空)
    gate = (decision_row['trade_gate_reason'] if decision_row else None) or ''
    r1_pass = '低于闸门' not in gate and gate.strip() == ''
    flags['R1_gate_passed'] = bool(r1_pass)
    if not r1_pass:
        reasons.append('R1:trade_gate_reason={}'.format(gate[:50]))

    # R2: evidence_action 不能是 HOLD
    ev_act = (decision_row['evidence_action'] if decision_row else None) or ''
    r2_pass = ev_act.strip().upper() != 'HOLD'
    flags['R2_evidence_action'] = bool(r2_pass)
    if not r2_pass:
        reasons.append('R2:evidence_action=HOLD')

    # R3: pnl 不能是 0 占位
    pnl = trade_row['pnl_pct'] or 0
    r3_pass = abs(pnl) > 0.0001
    flags['R3_pnl_nonzero'] = bool(r3_pass)
    if not r3_pass:
        reasons.append('R3:pnl=0_占位')

    # R4: asset 在 allowlist
    asset = (trade_row['asset'] or '').upper()
    r4_pass = asset in [a.upper() for a in allowed_assets]
    flags['R4_allowlist'] = bool(r4_pass)
    if not r4_pass:
        reasons.append('R4:asset={}_不在_allowlist'.format(asset))

    # R7: confidence >= 阈值
    conf = (decision_row['evidence_confidence'] if decision_row else None) or 0
    r7_pass = conf >= min_confidence
    flags['R7_confidence'] = bool(r7_pass)
    if not r7_pass:
        reasons.append('R7:confidence={}<{}'.format(conf, min_confidence))

    # R8 (v204+): 同批次兄弟决策数 ≤ max_batch_siblings
    # 决策的 "兄弟" = 同 asset ±5s 创建的其他 ai_decisions.
    # 用于拦截 DEEP_REFLECTION §5 揭示的 "batch-shared trade pool" 盲区.
    r8_pass = True
    siblings = 0
    try:
        decision_id = decision_row['decision_id'] if decision_row else None
    except (KeyError, IndexError, TypeError):
        decision_id = None
    if max_batch_siblings and conn is not None and decision_id and asset:
        siblings = _count_sibling_decisions(decision_id, asset, conn=conn)
        r8_pass = siblings <= max_batch_siblings
        flags['R8_batch_sharing'] = bool(r8_pass)
        if not r8_pass:
            reasons.append('R8:同批次兄弟决策={}>{}'.format(
                siblings, max_batch_siblings))
    else:
        # 没 conn 或没 decision_id → R8 不挡 (但记 flag 让外部能看见)
        flags['R8_batch_sharing'] = True
    flags['R8_siblings'] = siblings

    is_valid = all([r1_pass, r2_pass, r3_pass, r4_pass, r7_pass, r8_pass])
    return {
        'trade_id': trade_row['id'],
        'asset': asset,
        'action': trade_row['action'],
        'pnl_pct': pnl,
        'is_valid_for_alpha': is_valid,
        'flags': flags,
        'reasons': reasons,
    }


def audit_trades(trade_ids: Optional[List[int]] = None,
                 asset: Optional[str] = None,
                 action: Optional[str] = None,
                 allowed_assets: tuple = DEFAULT_SIM_ALLOWED_ASSETS,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 max_batch_siblings: int = DEFAULT_MAX_BATCH_SIBLINGS,
                 conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """批量 audit 一组 trade。

    Args:
        trade_ids: 限定要 audit 的 trade id 列表 (None = 全部 settled)
        asset/action: 过滤 trade
        allowed_assets: allowlist tuple
        min_confidence: 置信度阈值
        max_batch_siblings: R8 上限 (0 或 None = 关闭 R8, 向后兼容)
        conn: sqlite connection

    Returns:
        {
            'total': int, 'valid': int, 'invalid': int,
            'by_reason': {reason_str: count},
            'by_flag': {flag_key: pass_count},
            'samples': [audit_result_dict, ...]
        }
    """
    own = conn is None
    conn2 = conn or _connect()
    try:
        if trade_ids is not None:
            placeholders = ','.join('?' * len(trade_ids))
            rows = conn2.execute(
                "SELECT t.*, "
                "       d.id AS decision_id, "
                "       d.created_at AS decision_created_at, "
                "       d.trade_gate_reason, d.evidence_action, d.evidence_confidence "
                "FROM quick_sim_trades t "
                "LEFT JOIN ai_decisions d ON t.decision_id=d.id "
                "WHERE t.id IN ({}) ORDER BY t.id".format(placeholders),
                trade_ids
            ).fetchall()
        else:
            sql = ("SELECT t.*, "
                   "       d.id AS decision_id, "
                   "       d.created_at AS decision_created_at, "
                   "       d.trade_gate_reason, d.evidence_action, d.evidence_confidence "
                   "FROM quick_sim_trades t "
                   "LEFT JOIN ai_decisions d ON t.decision_id=d.id "
                   "WHERE t.settled=1")
            params: List[Any] = []
            if asset:
                sql += " AND t.asset=?"
                params.append(asset)
            if action:
                sql += " AND t.action=?"
                params.append(action)
            sql += " ORDER BY t.id"
            rows = conn2.execute(sql, params).fetchall()
    finally:
        if own: conn2.close()

    samples = []
    by_reason: Dict[str, int] = {}
    by_flag: Dict[str, int] = {}
    n_valid = 0

    for t in rows:
        audit = _check_one(t, t, allowed_assets, min_confidence,
                           max_batch_siblings=max_batch_siblings,
                           conn=conn2)
        samples.append(audit)
        if audit['is_valid_for_alpha']:
            n_valid += 1
        for reason in audit['reasons']:
            key = reason.split(':')[0]
            by_reason[key] = by_reason.get(key, 0) + 1
        for fk, fv in audit['flags'].items():
            if fv:
                by_flag[fk] = by_flag.get(fk, 0) + 1

    return {
        'total': len(samples),
        'valid': n_valid,
        'invalid': len(samples) - n_valid,
        'by_reason': by_reason,
        'by_flag': by_flag,
        'samples': samples,
    }


def get_clean_trade_ids(**kwargs) -> List[int]:
    """返回所有 is_valid_for_alpha=1 的 trade id。供 ablation 用。"""
    audit = audit_trades(**kwargs)
    return [s['trade_id'] for s in audit['samples'] if s['is_valid_for_alpha']]


def require_min_clean_samples(n_valid: int, min_required: int = 30) -> Dict[str, Any]:
    """反思系统的"硬门槛": 清洁样本 < 30 时拒绝出 alpha 结论。

    返回 {'allowed': bool, 'reason': str, 'recommendation': str}
    """
    if n_valid >= min_required:
        return {
            'allowed': True,
            'reason': '清洁样本 {} >= {} 阈值, 可以算 alpha'.format(n_valid, min_required),
        }
    return {
        'allowed': False,
        'reason': '清洁样本 {} < {} 阈值, 拒绝出 alpha 结论'.format(n_valid, min_required),
        'recommendation': (
            '建议: (1) 等更多 trade 进来; (2) 或修复 gate / K 线 fallback / '
            'allowlist 等系统 bug 后重新采集; (3) 临时小样本只能做探索性分析, '
            '不能下"alpha 存在/不存在"的硬结论。'
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# v231+ (2026-10-10): 执行层预检 — 把 R1/R2/R4/R7/R8/R9 从分析层挪到建仓前
#
# 背景:
#   ABLATION_RESULTS §D (V8) 揭示 100% trade 都是事故样本 (gate 失败 / HOLD 仍 trade)。
#   原 sample_quality_filter 是 post-hoc 分析层 filter, 不影响 trade 落库。
#   本函数把同一组规则改造成"建仓前预检", 给 quick_sim.open_trades() 调用。
#   - R3 (pnl=0) 是事后规则, 不参与预检.
#   - R9 (status=REJECTED) 是新增: 已被管理员拒签的 decision 不应 trade.
#
# 设计: 返回值与 _check_one 兼容 (is_valid_for_alpha + reasons + flags),
#       调用方据此决定是否 INSERT。
# ──────────────────────────────────────────────────────────────────────
def pre_trade_check(decision: Dict[str, Any],
                    conn: Optional[sqlite3.Connection] = None,
                    allowed_assets: Optional[tuple] = None,
                    min_confidence: Optional[float] = None,
                    max_batch_siblings: int = DEFAULT_MAX_BATCH_SIBLINGS,
                    r8_enabled: bool = True,
                    ) -> Dict[str, Any]:
    """对单个 ai_decision 跑 R1/R2/R4/R7/R8/R9 预检, 返回 audit dict。

    Args:
        decision: ai_decisions 行 (dict). 需含 id, target_asset, evidence_action,
                  evidence_confidence, trade_gate_reason, status, created_at.
        conn: sqlite 连接 (R8 查 ai_decisions 需要).
        allowed_assets: 模拟盘 allowlist tuple (默认 XAU/WTI).
        min_confidence: 置信度阈值 (默认 20.0).
        max_batch_siblings: R8 上限 (默认 3, 兄弟决策 > 3 → invalid).
        r8_enabled: 是否启用 R8 兄弟决策检查. 默认 True. 测试场景 (无 conn) 应
                    显式传 r8_enabled=False; 生产若 conn=None 但 r8_enabled=True
                    → 直接 raise, 避免静默放过 (C5 防回归).

    Returns:
        {'is_valid': bool, 'reasons': [str], 'flags': {rule: pass_bool}}
        is_valid=False 时, 字段 reason 形如 'R1:trade_gate_reason=...'
    """
    # v231+ (C3): None 表示用 config 实时值, 让 env 改动立即生效
    if allowed_assets is None:
        allowed_assets = config.SIM_ALLOWED_ASSETS
    if min_confidence is None:
        min_confidence = config.EVIDENCE_CONFIDENCE_GATE

    reasons: List[str] = []
    flags: Dict[str, bool] = {}

    # C5 防回归: R8 启用时必须给 conn, 否则 fail-loud 不允许静默放过
    if r8_enabled and conn is None:
        raise ValueError(
            "pre_trade_check: r8_enabled=True 但 conn=None. "
            "R8 兄弟决策检查需要 sqlite 连接, 生产环境必须传 conn. "
            "测试场景请显式传 r8_enabled=False 跳过 R8."
        )

    # R1: gate 必须通过 (trade_gate_reason 必须等于 _PASSED_REASON)
    gate = (decision.get('trade_gate_reason') or '').strip()
    passed_reason = config.TRADE_GATE_PASSED_REASON  # v231+ 单一来源
    r1_pass = gate == passed_reason
    flags['R1_gate_passed'] = bool(r1_pass)
    if not r1_pass:
        reasons.append('R1:trade_gate_reason={}'.format(gate[:50] or '(空)'))

    # R2: evidence_action 不能是 HOLD
    ev_act = (decision.get('evidence_action') or 'HOLD').strip().upper()
    r2_pass = ev_act != 'HOLD'
    flags['R2_evidence_action'] = bool(r2_pass)
    if not r2_pass:
        reasons.append('R2:evidence_action={}'.format(ev_act))

    # R4: asset 在 allowlist
    # v231+ (C4): 同时支持 'target_asset' (ai_decisions 列名) 和 'asset' (pending_signals SQL alias)
    asset = (decision.get('target_asset') or decision.get('asset') or '').strip().upper()
    r4_pass = asset in [a.upper() for a in allowed_assets]
    flags['R4_allowlist'] = bool(r4_pass)
    if not r4_pass:
        reasons.append('R4:asset={}_不在_allowlist'.format(asset))

    # R7: confidence >= 阈值
    conf = decision.get('evidence_confidence')
    try:
        conf_val = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf_val = 0.0
    r7_pass = conf_val >= min_confidence
    flags['R7_confidence'] = bool(r7_pass)
    if not r7_pass:
        reasons.append('R7:confidence={}<{}'.format(conf_val, min_confidence))

    # R8: 同批次兄弟决策数 ≤ max_batch_siblings
    r8_pass = True
    siblings = 0
    if r8_enabled and max_batch_siblings and conn is not None:
        try:
            siblings = _count_sibling_decisions(
                int(decision.get('id') or 0), asset, conn=conn
            )
            r8_pass = siblings <= max_batch_siblings
        except Exception:
            r8_pass = True  # 出错不挡 (宁放过不误杀)
    flags['R8_batch_sharing'] = bool(r8_pass)
    flags['R8_siblings'] = bool(siblings <= max_batch_siblings) if max_batch_siblings else True
    if not r8_pass:
        reasons.append('R8:同批次兄弟决策={}>{}'.format(siblings, max_batch_siblings))

    # R9 (v231+, H1): status 白名单. db schema CHECK 允许 5 个 status, 4 个可 trade:
    # UNREAD (新), APPROVED (用户批), REVIEWED (已查 dashboard), AUTO_APPROVED (gate 通过).
    # 只 REJECTED 不可. 改白名单后: 未来加新 status (EXPIRED/SETTLED/CANCELLED) 默认被挡,
    # 必须显式加进白名单, code review 时就能发现, 不会默默漏过黑名单.
    status = (decision.get('status') or '').strip().upper()
    _R9_TRADABLE = ("UNREAD", "APPROVED", "REVIEWED", "AUTO_APPROVED")
    r9_pass = status in _R9_TRADABLE
    flags['R9_status_not_rejected'] = bool(r9_pass)
    if not r9_pass:
        reasons.append('R9:status={}_not_tradable'.format(status or 'EMPTY'))

    is_valid = all([r1_pass, r2_pass, r4_pass, r7_pass, r8_pass, r9_pass])
    return {
        'is_valid': is_valid,
        'reasons': reasons,
        'flags': flags,
    }


def is_pre_trade_filter_enabled() -> bool:
    """环境变量 QUICK_SIM_QUALITY_FILTER=on (默认) / off。

    off 时 quick_sim 应回退到旧行为, 用于紧急回滚。
    """
    import os
    val = (os.environ.get('QUICK_SIM_QUALITY_FILTER', 'on') or 'on').strip().lower()
    return val not in ('0', 'off', 'false', 'no')


# v231+ (H4): 拦截持久化 - 把 pre_trade_check 拒掉的 decision 落库.
#
# 设计: 对最近 N 分钟的所有 ai_decisions 跑 pre_trade_check, 被拒的每个 rule 各 INSERT 一行.
#   - 唯一性: UNIQUE(decision_id, rule) + INSERT OR IGNORE → 同 decision 被同 rule 拦多次幂等
#   - filter 关闭时本函数直接返回 0 (保持 off 路径不写库)
#   - payload_json 只存关键字段 (不存 raw_news.content 等大字段), 便于 forensic
#   - 返回新插入数; 重复 audit 同一 decision 会返回 0 (全部 IGNORE 跳过)
#
# 调用入口: quick_sim.open_trades() 启动时调一次, 5 分钟级 cycle 成本可控 (~1s for 1000 decisions).
def audit_recent_rejections(
    conn: sqlite3.Connection,
    since_minutes: int = 120,
) -> int:
    """对最近 since_minutes 内的所有 ai_decisions 跑 pre_trade_check, 落库被拒记录.

    Returns: 本次新插入的 rejection 行数 (重复 audit 返回 0).
    """
    if not is_pre_trade_filter_enabled():
        return 0
    import json
    try:
        rows = conn.execute("""
            SELECT id, target_asset, suggested_action, evidence_action,
                   evidence_confidence, trade_gate_reason, status
            FROM ai_decisions
            WHERE datetime(created_at, 'localtime') > datetime('now', 'localtime', '-{} minutes')
        """.format(int(since_minutes))).fetchall()
    except Exception as _e:
        print(f"[audit_recent_rejections] query failed: {_e}", flush=True)
        return 0

    _KEYS = ('id', 'target_asset', 'suggested_action', 'evidence_action',
             'evidence_confidence', 'trade_gate_reason', 'status')
    inserted = 0
    for row in rows:
        d = {k: row[k] for k in _KEYS}
        try:
            result = pre_trade_check(d, conn=conn, r8_enabled=True)
        except ValueError:
            # r8_enabled=True 但 conn=None 会 raise. 这里 conn 必有, 但保底.
            continue
        except Exception as _e:
            print(f"[audit_recent_rejections] pre_trade_check fail id={d.get('id')}: {_e}", flush=True)
            continue
        if result['is_valid']:
            continue
        # 每条 reason 各 INSERT 一行
        for reason in result['reasons']:
            rule = reason.split(':', 1)[0]  # 'R1' / 'R2' / 'R4' / 'R7' / 'R8' / 'R9'
            try:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO quality_rejections
                        (decision_id, rule, reason, payload_json, filter_version)
                    VALUES (?, ?, ?, ?, ?)
                """, (d['id'], rule, reason,
                      json.dumps(d, default=str), 'v231+'))
                inserted += cur.rowcount if cur.rowcount is not None else 0
            except Exception as _e:
                print(f"[audit_recent_rejections] INSERT fail id={d.get('id')} rule={rule}: {_e}", flush=True)
    if inserted:
        try:
            conn.commit()
        except Exception:
            pass
    return inserted


# v231+ (H5): reflection 缓存失效标记.
#
# 背景: trade_reflections_v2.llm_depth_reflection 是 LLM 调用的"缓存" (单条反
# 思只调一次, 结果存库里). 但如果该反思的 decision 现在被 quality_rejections 标
# 记了 (H4 拦了), 那条 LLM 反思就属于"基于脏数据", 不能再当成真 lesson 用.
#
# 设计: 提供 is_decision_dirty() / get_dirty_decision_ids() 两个无副作用查询.
# 调用方决定怎么处理 (skip LLM / 标记 API 响应 / 清旧缓存).
#
#   - is_decision_dirty(conn, decision_id) → bool: 单条快速检查
#   - get_dirty_decision_ids(conn) → set[int]: 批量拉, 适合 LEFT JOIN 前过滤
#
# 性能: quality_rejections.idx_qr_decision 已建, decision_id 单查 O(log n).
# off 路径: filter 关闭时一律返 0 / 空 set (保持 off=回到 v8 之前).
def is_decision_dirty(conn: sqlite3.Connection, decision_id: int) -> bool:
    """检查 decision_id 是否在 quality_rejections 里 (即被质量门拦过).

    True = 现在是脏数据, 反思不应基于它做 LLM enrichment.
    False = 通过或未拦.

    off 路径: filter 关闭时返 False (不认为脏).
    """
    if not is_pre_trade_filter_enabled():
        return False
    if decision_id is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM quality_rejections WHERE decision_id = ? LIMIT 1",
            (int(decision_id),),
        ).fetchone() is not None
    except Exception as _e:
        print(f"[is_decision_dirty] query failed id={decision_id}: {_e}", flush=True)
        return False


def get_dirty_decision_ids(conn: sqlite3.Connection) -> set:
    """返回 quality_rejections 里所有 DISTINCT decision_id.

    用于:
      - LEFT JOIN 前批量过滤 (避免单条 N+1 查询)
      - 一次性清除/标记所有基于脏数据的 reflection 缓存
    """
    if not is_pre_trade_filter_enabled():
        return set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT decision_id FROM quality_rejections"
        ).fetchall()
        return {r[0] for r in rows if r[0] is not None}
    except Exception as _e:
        print(f"[get_dirty_decision_ids] query failed: {_e}", flush=True)
        return set()


def _PASSED_REASON_source_check() -> bool:
    """v231+ 单一来源契约: config.TRADE_GATE_PASSED_REASON 必须等于 quick_sim._PASSED_REASON。
    防回归: 防止有人改 quick_sim._PASSED_REASON 而忘改 sample_quality_filter (C2 landmine)。
    返回 True = 契约满足; False = 破裂, R1 静默开始错判。
    """
    import quick_sim  # 懒加载: 避免 sample_quality_filter 顶部循环 import
    return config.TRADE_GATE_PASSED_REASON == quick_sim._PASSED_REASON