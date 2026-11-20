"""审计门禁接入点（W3）。

ai_worker 只调这里一个函数，目的是把"路径/开关/异常"全部挡在这层，
主流程的 diff 保持在几行以内，且**任何情况下都不会让落库失败**。

开关（环境变量，可在线改，重启生效）：

    AUDIT_GATE=off                  关掉门禁（默认开，shadow）
    AUDIT_GATE_MODE=pretrade|full   审计口径（默认 pretrade）
    AUDIT_GATE_ENFORCE=1            预留（enforce 未接线，见 audit/gate.py）
    AUDIT_GATE_OUT=<dir>            reject 报告落盘目录（默认 audit_out/gate）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]  # backend/src_python/ → backend/ → 仓库根

_gate_row = None  # 首次调用时解析，避免每条决策都走 import 机制


def _load():
    global _gate_row
    if _gate_row is not None:
        return _gate_row
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from audit.gate import gate_row as _fn

    _gate_row = _fn
    return _fn


def gate(conn, decision_id: Optional[int], *, tag: str = "insert") -> Optional[Dict[str, Any]]:
    """同步审一条刚落库的决策。返回 None = 未启用/失败（调用方无需关心）。"""
    if not decision_id:
        return None
    # v421: 规则直出（技术信号）不是"推理"，拿八段标准审它只会制造假 E014，
    # 并让 canary 误拦规则交易。这类决策按设计**不进审计**，只留一行 skip 日志。
    try:
        _row = conn.execute(
            "SELECT COALESCE(reasoning,''), COALESCE(analysis_degraded,0)"
            "  FROM ai_decisions WHERE id=?", (int(decision_id),)
        ).fetchone()
        if _row and (str(_row[0]).startswith("[技术信号直出]") or int(_row[1] or 0) == 1):
            # v483: 以前这里直接 return None，审计表里既没有 verdict 也没有 skip
            # 记录 —— 于是"该不该审"没法核对，覆盖率的分子分母全靠猜。现在留一行
            # skipped，把"按设计不审"和"漏审"区分开。
            _why = "tech_direct" if str(_row[0]).startswith("[技术信号直出]") else "degraded"
            try:
                from audit.gate import _GATE_TABLE as _gate_ddl
                conn.execute(_gate_ddl)   # 临时库里这张表可能还没建
            except Exception:
                pass
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO audit_gate_log"
                    " (decision_id, verdict, findings, coverage, mode, gate_action,"
                    "  latency_ms, created_at)"
                    " VALUES (?, 'skipped', 0, 0.0, ?, 'none', 0,"
                    "         datetime('now','localtime'))",
                    (int(decision_id), _why),
                )
            except Exception as _exc_skip:
                # 不静默：留痕失败本身也要能被看见（0 号铁律要求可追溯）
                print(f"  [AUDIT-GATE:{tag}] skip 留痕失败: "
                      f"{type(_exc_skip).__name__}: {_exc_skip}", flush=True)
            return None
    except Exception as _exc_probe:
        print(f"  [AUDIT-GATE:{tag}] 读决策失败（按未审处理）: "
              f"{type(_exc_probe).__name__}: {_exc_probe}", flush=True)
    try:
        res = _load()(conn, int(decision_id))
    except Exception as exc:
        print(f"  [AUDIT-GATE:{tag}] skipped: {type(exc).__name__}: {exc}", flush=True)
        return None
    if res and res.get("verdict") == "reject":
        print(
            f"  [AUDIT-GATE:{tag}] decision#{decision_id} "
            f"verdict=reject findings={res.get('findings')} "
            f"latency={res.get('latency_ms')}ms (shadow, 未拦截)",
            flush=True,
        )
    return res


def enabled() -> bool:
    return (os.environ.get("AUDIT_GATE") or "on").strip().lower() not in ("0", "off", "false", "no")


def enforce_enabled() -> bool:
    return (os.environ.get("AUDIT_GATE_ENFORCE") or "0").strip().lower() in ("1", "true", "on", "yes")


def should_block(res: Optional[Dict[str, Any]]) -> str:
    """canary 拦截：只拦"推理链缺失"这一类（critical 码仅 E014）。

    其它 reject（E007 过期、E001x 等）继续只记不改，避免一次开闸把方向单全砍掉。
    返回非空字符串 = 拦截理由。
    """
    if not enforce_enabled() or not res:
        return ""
    if res.get("verdict") != "reject":
        return ""
    findings = (res.get("report") or {}).get("findings") or []
    crit = {f.get("code") for f in findings if (f.get("severity") or 0) >= 3}
    if crit and crit <= {"E014"}:
        return "E014 推理链缺失（八段不齐/空路径）"
    return ""
