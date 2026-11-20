"""enforce：把 reject 落成"降级为 HOLD"的动作覆盖。

**这个模块没有接线。** 它只负责把"该不该拦、拦的理由"算成一个纯数据结论；
真正的接线点在 `ai_worker` 决定 `action` 的分支——那一步会改变交易行为，需要单独授权。

为什么必须分开：决策链里的 action 是内存变量。落库后再 UPDATE `suggested_action`
拦不住已经发生的成交，所以拦截只能发生在 INSERT 之前；
而"算结论"这件事可以先在这里做好、测好、冻住，接线时只差一个 if。
"""

from __future__ import annotations

import os

from typing import Any, Dict, List, Optional

VERSION = "enforce.v1"

DEFAULT_MIN_SEVERITY = 3
DEFAULT_OVERRIDE = "HOLD"

# 默认拦截策略：只认"决策当时可判 + severity >= 3"的码。
# 这条规则的实际意义：E001a（缺硬数据，severity 2）占全部 finding 的 94%，
# 如果它也参与拦截，等于把所有方向单一起拦死——那是关系统，不是审计。
POLICY: Dict[str, Any] = {
    "min_severity": DEFAULT_MIN_SEVERITY,
    "basis": "decision",          # 事后型码（E007/E016/E017/E018）不参与
    "allow_codes": None,          # None = 不限码；给列表则只允许这些码触发
    "override": DEFAULT_OVERRIDE,
}

HOLD_ACTIONS = ("BUY", "SELL")


def policy_from_env() -> Dict[str, Any]:
    """v413: 把"放开单层拦截"做成一个环境变量，而不是改代码。

    AUDIT_GATE_ENFORCE_CODES=E001c   只让这一码参与拦截（其余保持 shadow）
    AUDIT_GATE_MIN_SEVERITY=2        允许 severity 2 的码进拦截线
    AUDIT_GATE_ENFORCE=1             真正动 action（否则只算不动，见 ai_worker）

    默认什么也不开 —— 2026-11-18 复核 7 条 reject 的结论是：E014/E028 属文本
    合规检查，E001c 虽是真数据缺陷但覆盖 39 条方向单里的 26 条（67%），直接开
    等于关系统。所以开关先备好、默认关闭，等 factor_ranking 补齐后再逐码放开。
    """
    codes = (os.environ.get("AUDIT_GATE_ENFORCE_CODES") or "").strip()
    allow = [c.strip() for c in codes.split(",") if c.strip()] or None
    try:
        min_sev = int(os.environ.get("AUDIT_GATE_MIN_SEVERITY") or DEFAULT_MIN_SEVERITY)
    except ValueError:
        min_sev = DEFAULT_MIN_SEVERITY
    return {"min_severity": min_sev, "allow_codes": allow}


def resolve(report: Dict[str, Any], decision: Dict[str, Any],
            policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """算出这条决策该不该被降级。纯函数，不改任何输入。

    Returns:
        {
          "should_override": bool,
          "action": 原动作,
          "override": "HOLD" 或 None,
          "reasons": [{"code","severity","basis","detail"}...],
          "policy": 生效的策略
        }
    """
    p = dict(POLICY, **(policy if policy is not None else policy_from_env()))
    action = str(decision.get("suggested_action") or "").strip().upper()

    reasons: List[Dict[str, Any]] = []
    for f in report.get("findings", []):
        if int(f.get("severity", 0)) < int(p["min_severity"]):
            continue
        if p.get("basis") and f.get("basis") != p["basis"]:
            continue
        if p.get("allow_codes") and f.get("code") not in set(p["allow_codes"]):
            continue
        reasons.append({
            "code": f.get("code"),
            "severity": f.get("severity"),
            "basis": f.get("basis"),
            "layer": f.get("layer"),
            "detail": (f.get("detail") or "")[:200],
        })

    should = bool(reasons) and action in HOLD_ACTIONS
    return {
        "decision_id": decision.get("id", decision.get("decision_id")),
        "should_override": should,
        "action": action,
        "override": p["override"] if should else None,
        "reasons": reasons,
        "policy": {
            "min_severity": p["min_severity"],
            "basis": p["basis"],
            "allow_codes": p["allow_codes"],
        },
        "version": VERSION,
    }


def explain(plan: Dict[str, Any]) -> str:
    """一行可读的理由，给日志用。"""
    if not plan.get("should_override"):
        return f"decision#{plan.get('decision_id')} {plan.get('action')}: 不拦截"
    codes = "/".join(sorted({r["code"] for r in plan["reasons"]}))
    return (f"decision#{plan.get('decision_id')} {plan.get('action')} → {plan['override']}，"
            f"依据 {codes}（severity≥{plan['policy']['min_severity']}，"
            f"basis={plan['policy']['basis']}）")
