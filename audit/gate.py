"""同步审计门禁（W3）—— 决策落库那一刻就审，覆盖率从 0.09% 提到 100%。

和原来的反思系统的区别只有一条：**时机**。

    原来：周一 09:00 周报 → 抽一部分决策 → 写 decision_errors → 生成总结
    现在：决策 INSERT 的同一个事务里 → 每条都审 → findings 落库 → verdict 留痕

当前只有 `shadow`：只审不改。findings 写 `decision_errors`，verdict 写 `audit_gate_log`，
**不碰 suggested_action**，交易行为零变化。

enforce（真的把 reject 的 BUY/SELL 降成 HOLD）**还没接线**：
`plan()` 和在 `--enforce-sim` 里已经能算出"会拦谁"，但要让拦截生效，得改 `ai_worker`
里决定 `action` 的分支（改的是内存变量，不是 DB 里的 suggested_action），
那个改动风险比 shadow 大一个量级，需要单独授权。

环境变量：

    AUDIT_GATE=off          完全关掉（默认开）
    AUDIT_GATE_MODE=pretrade|full   审计模式（默认 pretrade，只用进单前可判的码）
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import enforce
from .checkers import legacy
from .core.context import CheckContext
from .core.pipeline import audit

VERSION = "gate.v1"

# 审计层名 → 内核的层权重键（weight = severity*10 + 层权重）
_SOURCE_MAP = {
    "data": "data",
    "logic": "logic",
    "rules": "rule",
    "causal": "causal",
    "market": "market",
    "event_causality": "event_logic",
    "reasoning": "reasoning_logic",
}

_GATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_gate_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    verdict     TEXT    NOT NULL,
    findings    INTEGER NOT NULL DEFAULT 0,
    coverage    REAL    NOT NULL DEFAULT 0,
    mode        TEXT    NOT NULL DEFAULT 'pretrade',
    gate_action TEXT    NOT NULL DEFAULT 'shadow',   -- shadow | enforced
    latency_ms  REAL    NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(decision_id)
)
"""


# ── 开关 ────────────────────────────────────────────────
def enabled() -> bool:
    return (os.environ.get("AUDIT_GATE") or "on").strip().lower() not in ("0", "off", "false", "no")


def mode_from_env() -> str:
    m = (os.environ.get("AUDIT_GATE_MODE") or "pretrade").strip().lower()
    return m if m in ("pretrade", "full") else "pretrade"


def enforce_from_env() -> bool:
    """enforce 尚未接线，这里只是占位判断，别把它当"已拦截"。"""
    return (os.environ.get("AUDIT_GATE_ENFORCE") or "").strip().lower() in ("1", "on", "true", "yes")


# ── 核心 ────────────────────────────────────────────────
def decide(decision: Dict[str, Any], ctx: CheckContext, *, mode: str = "pretrade") -> Dict[str, Any]:
    """审一条决策，给出 verdict 与是否需要降级。

    action_override 有值 ≠ 已经拦：是否真的改动作由调用方（enforce 模式）决定。
    """
    report = audit(decision, ctx, mode=mode, strict=False)
    # 拦截策略只有一处定义（audit/enforce.py）：severity>=3 且 basis=decision。
    # 这样 shadow 的"会拦谁"和将来真接线的行为必然一致。
    plan = enforce.resolve(report, decision)
    action = plan["action"]
    override = plan["override"] if plan["should_override"] else None
    return {
        "decision_id": decision.get("id"),
        "verdict": report["verdict"],
        "action": action,
        "action_override": override,
        "findings": len(report["findings"]),
        "report": report,
    }


def persist(
    report: Dict[str, Any],
    decision_id: int,
    conn: sqlite3.Connection,
    *,
    source: str = "audit_gate",
) -> int:
    """findings → decision_errors（幂等）。

    同一决策同一错误码已存在（不论来源）就跳过，避免门禁和周期反思各写一份，
    把统计分母翻倍。
    """
    rc = legacy.load()
    written = 0
    for f in report.get("findings", []):
        try:
            exists = conn.execute(
                "SELECT 1 FROM decision_errors WHERE decision_id = ? AND error_code = ? LIMIT 1",
                (int(decision_id), f["code"]),
            ).fetchone()
            if exists:
                continue
            sev = int(f.get("severity", 2))
            layer = f.get("layer") or ""
            layer_key = _SOURCE_MAP.get(layer, layer)   # 只用来算权重
            # source 固定写 audit_gate：既能和周期反思的行区分，也让回滚变成一条 DELETE
            src = source
            detail = str(f.get("detail") or "")
            if f.get("remediation"):
                detail = f"{detail}｜修复: {f['remediation']}"
            conn.execute(
                """INSERT OR IGNORE INTO decision_errors
                   (decision_id, error_code, origin, severity, detail,
                    evidence_refs, source, weight, created_at)
                   VALUES (?, ?, 'checker', ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    int(decision_id),
                    f["code"],
                    sev,
                    detail[:800],
                    json.dumps(f.get("evidence_refs") or {}, ensure_ascii=False),
                    src,
                    int(rc._compute_weight(sev, layer_key)),
                ),
            )
            written += 1
        except (sqlite3.Error, KeyError, TypeError):
            continue
    return written


def _log(conn: sqlite3.Connection, res: Dict[str, Any], *, gate_action: str, latency_ms: float) -> None:
    conn.execute(_GATE_TABLE)
    rep = res["report"]
    # v452：created_at 原来用 `datetime('now')`（UTC，无时区后缀），而决策侧写的是
    # `+08:00` 本地 ISO —— 同一条决策在两个表里差 8 小时，违反「口径可追溯」。
    # 改为带时区的本地 ISO（与 ai_decisions.created_at 同格式），存量 UTC 行不动。
    _now_local = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR REPLACE INTO audit_gate_log
           (decision_id, verdict, findings, coverage, mode, gate_action, latency_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(res["decision_id"]),
            res["verdict"],
            res["findings"],
            float(rep["coverage"]["ratio"]),
            rep["mode"],
            gate_action,
            round(latency_ms, 3),
            _now_local,
        ),
    )


def gate_row(
    conn: sqlite3.Connection,
    decision_id: int,
    *,
    mode: Optional[str] = None,
    persist_findings: bool = True,
    outdir: Optional[str] = None,
    gate_action: str = "shadow",
) -> Optional[Dict[str, Any]]:
    """对已落库的一条决策跑门禁（shadow 用这个）。

    借用调用方的连接：决策刚 INSERT、还没 commit，只有同一连接看得见。
    """
    if not enabled():
        return None
    mode = mode or mode_from_env()
    started = time.perf_counter()
    # v483: dict(row) 以前只在 conn.row_factory=sqlite3.Row 时成立。技术直出/
    # drain 两条路径的连接未必设 row_factory → TypeError → 门禁静默 skip，
    # 这就是"宣称 100% 覆盖、实测只有 50/390"的一部分真相。现在两种都兼容。
    _cur = conn.execute("SELECT * FROM ai_decisions WHERE id = ?", (int(decision_id),))
    row = _cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        decision = dict(row)
    else:
        decision = dict(zip([d[0] for d in (_cur.description or [])], row))

    ctx = CheckContext.from_connection(conn, label=f"gate:insert#{decision_id}")
    try:
        res = decide(decision, ctx, mode=mode)
    finally:
        ctx.close()  # 借用的连接不会被关

    latency = (time.perf_counter() - started) * 1000
    res["latency_ms"] = round(latency, 2)

    if persist_findings and res["findings"]:
        try:
            res["persisted"] = persist(res["report"], int(decision_id), conn)
        except sqlite3.Error as exc:
            res["persist_error"] = str(exc)
    try:
        _log(conn, res, gate_action=gate_action, latency_ms=latency)
    except sqlite3.Error as exc:
        res["log_error"] = str(exc)

    if res["verdict"] == "reject":
        _dump(res["report"], outdir)
    return res


def _dump(report: Dict[str, Any], outdir: Optional[str]) -> None:
    """reject 单独留一份报告文件，便于人工复核。"""
    try:
        d = Path(outdir or os.environ.get("AUDIT_GATE_OUT") or "audit_out/gate")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{report['trace_id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def plan(decision: Dict[str, Any], ctx: CheckContext, *, mode: Optional[str] = None) -> Dict[str, Any]:
    """INSERT 之前调用的版本（enforce 用）：只给结论，不改任何东西。"""
    return decide(decision, ctx, mode=mode or mode_from_env())


def coverage(conn: sqlite3.Connection) -> Dict[str, Any]:
    """门禁覆盖率：审计过的决策 / 全部决策。"""
    conn.execute(_GATE_TABLE)
    total = int(conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0])
    gated = int(conn.execute("SELECT COUNT(*) FROM audit_gate_log").fetchone()[0])
    rejects = int(conn.execute(
        "SELECT COUNT(*) FROM audit_gate_log WHERE verdict = 'reject'").fetchone()[0])
    return {
        "decisions": total,
        "gated": gated,
        "coverage": round(gated / total, 4) if total else 0.0,
        "rejects": rejects,
    }


def enforce_summary(decisions: List[Dict[str, Any]], ctx: CheckContext, *, mode: str = "pretrade") -> Dict[str, Any]:
    """只算不写：如果开 enforce，会拦掉哪些方向单。"""
    would_block = []
    for d in decisions:
        res = decide(d, ctx, mode=mode)
        if res["action_override"]:
            top = [f["code"] for f in res["report"]["findings"]
                   if f["severity"] >= 3 and f["basis"] == "decision"]
            would_block.append({
                "decision_id": res["decision_id"],
                "asset": d.get("target_asset"),
                "action": res["action"],
                "codes": top,
                "verdict": res["verdict"],
            })
    return {
        "decisions": len(decisions),
        "would_block": len(would_block),
        "blocked": would_block,
    }
