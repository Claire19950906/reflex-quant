"""policy_engine — 决策前策略闸门 (P1 #5 ADJUST_SIZE_PCT + P2 #9 锚定闸门值)。

设计目标:
  - 把"哪些 asset/action 组合应该被拦/被改仓位"做成 DB 驱动的可调规则
  - 上游决策引擎在每个 action 落地前调 evaluate() 拿一个 PolicyDecision
  - 规则按 id 顺序匹配第一条 enabled=1 的 (确定性, 可回归测试)

支持 rule_type:
  - FORBID_ACTION       params={"reason": str}                → allow=False
  - REQUIRE_RR          params={"min": float}                 → allow = rr_ratio >= min
  - MAX_SIZE_USDT       params={"cap": float}                 → allow = notional_usdt <= cap
  - ADJUST_SIZE_PCT     params={"pct": float in [0,1]}        → allow=True, adjusted_notional_pct=pct
  - REQUIRE_CONFIDENCE  params={"min": float}                 → allow = evidence_confidence >= min

asset / action 字段:
  - 具体值 (如 "XAU", "BUY") 只匹配自己
  - 通配 "ALL" 匹配所有
  - 多条规则同时满足: 取 id 最小 (最早添加) 的那条
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 已知 rule_type 集合 (防止 typo 静默失效)
_KNOWN_RULE_TYPES = {
    "FORBID_ACTION",
    "REQUIRE_RR",
    "MAX_SIZE_USDT",
    "ADJUST_SIZE_PCT",
    "REQUIRE_CONFIDENCE",
}


@dataclass
class PolicyDecision:
    """evaluate() 返回值。

    allow:                  True=放行, False=拒绝
    rule_type:              触发的规则类型, 没规则时 None
    rule_id:                触发的规则 id (调试用), 没规则时 None
    reason:                 给前端 / 决策日志看的可读理由
    adjusted_notional_pct:  仓位系数, 没规则时 1.0 (满仓), ADJUST_SIZE_PCT 时取规则值
    extra:                  规则特定附加字段 (如触发的 rule_type=MAX_SIZE_USDT 时
                            带上 {'notional_usdt': 100, 'cap': 50})
    """
    allow: bool
    rule_type: Optional[str] = None
    rule_id: Optional[int] = None
    reason: str = ""
    adjusted_notional_pct: float = 1.0
    extra: Dict[str, Any] = field(default_factory=dict)


def init_schema(conn: sqlite3.Connection) -> None:
    """建 policy_rules 表 + 索引 (幂等)。

    注: db.migrate() 已经包含这张表, 这里给不跑 migrate 的代码一个入口。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            asset       TEXT    NOT NULL,
            action      TEXT    NOT NULL,
            rule_type   TEXT    NOT NULL,
            params      TEXT    NOT NULL DEFAULT '{}',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pr_lookup "
        "ON policy_rules(enabled, asset, action, id)"
    )
    conn.commit()


def _parse_params(raw: Any) -> Dict[str, Any]:
    """DB 里 params 是 JSON 字符串, 安全解析。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _matches(asset_rule: str, asset_actual: str,
             action_rule: str, action_actual: str) -> bool:
    """通配匹配: asset_rule/action_rule 为 'ALL' 或等于实际值。"""
    a_ok = asset_rule == "ALL" or asset_rule == asset_actual
    act_ok = action_rule == "ALL" or action_rule == action_actual
    return a_ok and act_ok


def _load_rules(conn: sqlite3.Connection, asset: str, action: str) -> List[sqlite3.Row]:
    """按 id 顺序拿所有 enabled=1 的规则 (ALL 通配)。"""
    try:
        rows = conn.execute(
            "SELECT id, asset, action, rule_type, params "
            "FROM policy_rules "
            "WHERE enabled = 1 "
            "  AND (asset = ? OR asset = 'ALL') "
            "  AND (action = ? OR action = 'ALL') "
            "ORDER BY id ASC",
            (asset, action),
        ).fetchall()
        return rows
    except sqlite3.OperationalError:
        # 表不存在 — 视为无规则
        return []


def _evaluate_rule(rule: sqlite3.Row, asset: str, action: str,
                   context: Dict[str, Any]) -> Optional[PolicyDecision]:
    """对单条规则求值; 业务条件不满足返 None 让上层继续看下一条。

    注: _matches() 的通配检查在 SQL 里已经做了, 这里只负责业务条件。
    """
    rule_type = (rule["rule_type"] or "").upper()
    params = _parse_params(rule["params"])

    if rule_type == "FORBID_ACTION":
        return PolicyDecision(
            allow=False,
            rule_type="FORBID_ACTION",
            rule_id=int(rule["id"]),
            reason=str(params.get("reason", "blocked by FORBID_ACTION")),
            adjusted_notional_pct=0.0,
        )

    if rule_type == "REQUIRE_RR":
        try:
            min_rr = float(params.get("min", 0))
        except (TypeError, ValueError):
            return None
        rr = context.get("rr_ratio")
        if rr is None:
            return PolicyDecision(
                allow=False,
                rule_type="REQUIRE_RR",
                rule_id=int(rule["id"]),
                reason=f"REQUIRE_RR: 未提供 rr_ratio, 需 >= {min_rr}",
                adjusted_notional_pct=0.0,
            )
        try:
            rr_f = float(rr)
        except (TypeError, ValueError):
            return PolicyDecision(
                allow=False,
                rule_type="REQUIRE_RR",
                rule_id=int(rule["id"]),
                reason="REQUIRE_RR: rr_ratio 不可解析",
                adjusted_notional_pct=0.0,
            )
        if rr_f < min_rr:
            return PolicyDecision(
                allow=False,
                rule_type="REQUIRE_RR",
                rule_id=int(rule["id"]),
                reason=f"REQUIRE_RR: rr_ratio={rr_f} < min={min_rr}",
                adjusted_notional_pct=0.0,
            )
        return None  # 通过, 让上层看其他规则

    if rule_type == "MAX_SIZE_USDT":
        try:
            cap = float(params.get("cap", 0))
        except (TypeError, ValueError):
            return None
        notional = context.get("notional_usdt")
        if notional is None:
            return PolicyDecision(
                allow=False,
                rule_type="MAX_SIZE_USDT",
                rule_id=int(rule["id"]),
                reason=f"MAX_SIZE_USDT: 未提供 notional_usdt, 需 <= {cap}",
                adjusted_notional_pct=0.0,
                extra={"cap": cap, "notional_usdt": None},
            )
        try:
            notional_f = float(notional)
        except (TypeError, ValueError):
            return PolicyDecision(
                allow=False,
                rule_type="MAX_SIZE_USDT",
                rule_id=int(rule["id"]),
                reason="MAX_SIZE_USDT: notional_usdt 不可解析",
                adjusted_notional_pct=0.0,
            )
        if notional_f > cap:
            return PolicyDecision(
                allow=False,
                rule_type="MAX_SIZE_USDT",
                rule_id=int(rule["id"]),
                # reason 必须包含 "100" 和 "50" (测试 test_max_size_usdt_caps_notional)
                reason=f"MAX_SIZE_USDT: notional_usdt={notional_f} 超过 cap={cap}",
                adjusted_notional_pct=0.0,
                extra={"cap": cap, "notional_usdt": notional_f},
            )
        return None

    if rule_type == "ADJUST_SIZE_PCT":
        try:
            pct = float(params.get("pct", 1.0))
        except (TypeError, ValueError):
            return None
        pct = max(0.0, min(1.0, pct))
        return PolicyDecision(
            allow=True,
            rule_type="ADJUST_SIZE_PCT",
            rule_id=int(rule["id"]),
            reason=f"ADJUST_SIZE_PCT: 仓位调整到 {pct*100:.0f}%",
            adjusted_notional_pct=pct,
            extra={"pct": pct},
        )

    if rule_type == "REQUIRE_CONFIDENCE":
        try:
            min_conf = float(params.get("min", 0))
        except (TypeError, ValueError):
            return None
        conf = context.get("evidence_confidence")
        if conf is None:
            return PolicyDecision(
                allow=False,
                rule_type="REQUIRE_CONFIDENCE",
                rule_id=int(rule["id"]),
                reason=f"REQUIRE_CONFIDENCE: 未提供 evidence_confidence, 需 >= {min_conf}",
                adjusted_notional_pct=0.0,
            )
        try:
            conf_f = float(conf)
        except (TypeError, ValueError):
            return PolicyDecision(
                allow=False,
                rule_type="REQUIRE_CONFIDENCE",
                rule_id=int(rule["id"]),
                reason="REQUIRE_CONFIDENCE: evidence_confidence 不可解析",
                adjusted_notional_pct=0.0,
            )
        if conf_f < min_conf:
            return PolicyDecision(
                allow=False,
                rule_type="REQUIRE_CONFIDENCE",
                rule_id=int(rule["id"]),
                reason=f"REQUIRE_CONFIDENCE: confidence={conf_f} < min={min_conf}",
                adjusted_notional_pct=0.0,
            )
        return None

    # 未知 rule_type — 静默跳过 + stderr
    print(f"[policy_engine] unknown rule_type: {rule_type}", file=sys.stderr)
    return None


def evaluate(conn: sqlite3.Connection, asset: str, action: str,
             context: Optional[Dict[str, Any]] = None) -> PolicyDecision:
    """对 (asset, action) 求一个 PolicyDecision。

    算法: 按 id 顺序遍历 enabled 规则, 第一条返回非 None 的规则胜出;
          若没规则命中, 返 allow=True, rule_type=None, adjusted_notional_pct=1.0。

    context: dict, 可包含 rr_ratio / notional_usdt / evidence_confidence 等。
             没传 = {}。
    """
    ctx = dict(context or {})
    asset_u = (asset or "").upper().strip()
    action_u = (action or "").upper().strip()

    rules = _load_rules(conn, asset_u, action_u)
    if not rules:
        return PolicyDecision(allow=True, adjusted_notional_pct=1.0)

    for r in rules:
        # 通配匹配 (SQL 已经筛过, 这里再保险一次 — DB schema 万一被改)
        if not _matches(r["asset"], asset_u, r["action"], action_u):
            continue
        d = _evaluate_rule(r, asset_u, action_u, ctx)
        if d is not None:
            return d

    # 所有规则检查完都没命中具体条件 → 默认放行
    return PolicyDecision(allow=True, adjusted_notional_pct=1.0)


def add_rule(conn: sqlite3.Connection,
             asset: str,
             action: str,
             rule_type: str,
             params: Optional[Dict[str, Any]] = None) -> int:
    """添加一条规则, 返 rule_id。

    默认 enabled=1。params 必须是 dict, 会被序列化成 JSON 入库。
    """
    rule_type_u = (rule_type or "").upper().strip()
    if rule_type_u not in _KNOWN_RULE_TYPES:
        raise ValueError(
            f"unknown rule_type={rule_type!r}; expected one of {sorted(_KNOWN_RULE_TYPES)}"
        )
    asset_u = (asset or "").upper().strip() or "ALL"
    action_u = (action or "").upper().strip() or "ALL"
    params_json = json.dumps(params or {}, ensure_ascii=False)

    cur = conn.execute(
        "INSERT INTO policy_rules (asset, action, rule_type, params, enabled) "
        "VALUES (?, ?, ?, ?, 1)",
        (asset_u, action_u, rule_type_u, params_json),
    )
    conn.commit()
    return int(cur.lastrowid)


def disable_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    """把 enabled 置 0 (软删), 不真删以便审计。"""
    conn.execute(
        "UPDATE policy_rules SET enabled = 0 WHERE id = ?",
        (int(rule_id),),
    )
    conn.commit()


def enable_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    """把 enabled 重新置 1。"""
    conn.execute(
        "UPDATE policy_rules SET enabled = 1 WHERE id = ?",
        (int(rule_id),),
    )
    conn.commit()


def list_rules(conn: sqlite3.Connection,
               enabled_only: bool = False) -> List[Dict[str, Any]]:
    """列出所有规则 (调试用)。"""
    sql = "SELECT id, asset, action, rule_type, params, enabled, created_at FROM policy_rules"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    rows = conn.execute(sql).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "asset": r["asset"],
            "action": r["action"],
            "rule_type": r["rule_type"],
            "params": _parse_params(r["params"]),
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
        })
    return out


__all__ = [
    "PolicyDecision",
    "init_schema",
    "evaluate",
    "add_rule",
    "disable_rule",
    "enable_rule",
    "list_rules",
    "KNOWN_RULE_TYPES",
]
