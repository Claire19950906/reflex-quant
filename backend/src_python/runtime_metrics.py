# -*- coding: utf-8 -*-
"""runtime_metrics.py — 只读运行指标聚合（P1-3 可观测性）。

背景（2026-11-19 工程体检）
--------------------------
此前失败率/丢弃数/耗时分布要分别翻日志、拼多张表。本模块把"链路健康"需要的
关键指标从库表只读聚合出来，供健康端点 / 看板 / 每日报告复用。

边界：
  * 只读，不写库、不改业务状态；
  * 任何表缺列/缺失都降级为 None，不让指标聚合反过来打断主链路；
  * 时间窗口用秒级 epoch；默认近 24h，可传 since_ts。

用法：
    import runtime_metrics
    snap = runtime_metrics.snapshot()
    # snap["decisions"], snap["trades"], snap["llm"], snap["silent_drops"] ...
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from typing import Any, Dict, List, Optional

try:
    import db as _db
    _GET_CONN = getattr(_db, "get_connection", None)
except Exception:
    _GET_CONN = None


def _connect() -> Optional[sqlite3.Connection]:
    if _GET_CONN is not None:
        try:
            return _GET_CONN()
        except Exception:
            return None
    try:
        import db
        return db.get_connection()
    except Exception:
        return None


def _scalar(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Optional[int]:
    try:
        return conn.execute(sql, args).fetchone()[0]
    except sqlite3.Error:
        return None


def _group(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Dict[str, int]:
    try:
        return {str(r[0] if r[0] is not None else "NULL"): r[1]
                for r in conn.execute(sql, args)}
    except sqlite3.Error:
        return {}


def _decisions(conn: sqlite3.Connection, lo: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["total"] = _scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts>=?", (lo,))
    out["by_action"] = _group(
        conn, "SELECT suggested_action, COUNT(*) FROM ai_decisions "
              "WHERE ts>=? GROUP BY 1", (lo,))
    out["by_evidence_action"] = _group(
        conn, "SELECT evidence_action, COUNT(*) FROM ai_decisions "
              "WHERE ts>=? GROUP BY 1", (lo,))
    out["directional"] = _scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts>=? "
              "AND suggested_action IN ('BUY','SELL')", (lo,))
    out["filled"] = _scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts>=? "
              "AND entry_price IS NOT NULL", (lo,))
    out["missing_price"] = _scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts>=? "
              "AND suggested_action IN ('BUY','SELL') AND entry_price IS NULL", (lo,))
    out["missing_reason"] = _scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts>=? "
              "AND suggested_action IN ('BUY','SELL') AND entry_price IS NULL "
              "AND COALESCE(no_trade_reason,'')=''", (lo,))
    out["degraded"] = _scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts>=? "
              "AND COALESCE(analysis_degraded,0)=1", (lo,))
    return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _gate(conn: sqlite3.Connection, lo: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not _table_exists(conn, "gate_evaluations"):
        return {"total": None, "passed": None, "blocked": None,
                "block_rate": None, "by_reject_code": {}}
    out["total"] = _scalar(
        conn, "SELECT COUNT(*) FROM gate_evaluations WHERE evaluated_at>=?", (lo,))
    out["passed"] = _scalar(
        conn, "SELECT COUNT(*) FROM gate_evaluations WHERE evaluated_at>=? AND passed=1", (lo,))
    out["blocked"] = _scalar(
        conn, "SELECT COUNT(*) FROM gate_evaluations WHERE evaluated_at>=? AND passed=0", (lo,))
    total = out["total"] or 0
    out["block_rate"] = round(out["blocked"] / total, 4) if total else None
    out["by_reject_code"] = _group(
        conn, "SELECT COALESCE(NULLIF(reject_code,''),'unspecified') AS rc, COUNT(*) "
              "FROM gate_evaluations WHERE evaluated_at>=? GROUP BY 1 "
              "ORDER BY 2 DESC LIMIT 8", (lo,))
    return out


def _quarantine(conn: sqlite3.Connection, lo: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not _table_exists(conn, "kline_bars_quarantine"):
        return {"total": None, "by_reason": {}}
    out["total"] = _scalar(
        conn, "SELECT COUNT(*) FROM kline_bars_quarantine WHERE quarantined_at>=?", (lo,))
    out["by_reason"] = _group(
        conn, "SELECT COALESCE(NULLIF(reason,''),'unspecified') AS r, COUNT(*) "
              "FROM kline_bars_quarantine WHERE quarantined_at>=? GROUP BY 1 "
              "ORDER BY 2 DESC LIMIT 8", (lo,))
    return out


def _trades(conn: sqlite3.Connection, lo: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not _table_exists(conn, "t2_positions"):
        return {"open": None, "closed": None, "closed_with_pnl": 0,
                "win": 0, "loss": 0, "total_pnl": None, "reflections": None}
    out["open"] = _scalar(
        conn, "SELECT COUNT(*) FROM t2_positions WHERE status='open'")
    out["closed"] = _scalar(
        conn, "SELECT COUNT(*) FROM t2_positions WHERE status='closed'")
    closed = conn.execute(
        "SELECT realized_pnl FROM t2_positions WHERE status='closed' "
        "AND realized_pnl IS NOT NULL").fetchall()
    pnls = [r[0] for r in closed if isinstance(r[0], (int, float))]
    out["closed_with_pnl"] = len(pnls)
    out["win"] = sum(1 for p in pnls if p > 0)
    out["loss"] = sum(1 for p in pnls if p <= 0)
    out["total_pnl"] = round(sum(pnls), 4) if pnls else None
    out["reflections"] = _scalar(
        conn, "SELECT COUNT(*) FROM trade_reflections_v2") \
        if _table_exists(conn, "trade_reflections_v2") else None
    return out


def _llm(conn: sqlite3.Connection, lo: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["total"] = _scalar(
        conn, "SELECT COUNT(*) FROM ai_call_stats WHERE ts>=?", (lo,))
    out["by_status"] = _group(
        conn, "SELECT status, COUNT(*) FROM ai_call_stats WHERE ts>=? GROUP BY 1", (lo,))
    out["by_error"] = _group(
        conn, "SELECT error_code, COUNT(*) FROM ai_call_stats WHERE ts>=? "
              "AND COALESCE(error_code,'')!='' GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (lo,))
    err = _scalar(
        conn, "SELECT COUNT(*) FROM ai_call_stats WHERE ts>=? AND status!='ok'", (lo,))
    total = out["total"] or 0
    out["error_rate"] = round(err / total, 4) if total else None
    out["tokens_in"] = _scalar(
        conn, "SELECT COALESCE(SUM(tokens_in),0) FROM ai_call_stats WHERE ts>=?", (lo,))
    out["tokens_out"] = _scalar(
        conn, "SELECT COALESCE(SUM(tokens_out),0) FROM ai_call_stats WHERE ts>=?", (lo,))
    return out


def _silent_drops() -> Dict[str, Any]:
    """读丢写账本 JSONL（内存计数进程内才有效；落盘部分只读尾部聚合）。"""
    ledger = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "runtime", "silent_drops.jsonl")
    out: Dict[str, Any] = {"file": os.path.abspath(ledger), "by_key": {}}
    try:
        if not os.path.exists(ledger):
            return out
        agg: Dict[str, int] = {}
        with open(ledger, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    k = str(rec.get("key", "?"))
                    agg[k] = agg.get(k, 0) + int(rec.get("dropped", 1))
                except Exception:
                    continue
        out["by_key"] = dict(sorted(agg.items(), key=lambda x: -x[1]))
        out["total"] = sum(out["by_key"].values())
    except Exception:
        pass
    return out


# 常见信号退出码映射（launchctl 状态列为负 = 被信号终止，绝对值是信号号）。
_SIGNAL_NAMES: Dict[int, str] = {
    1: "SIGHUP", 2: "SIGINT", 9: "SIGKILL", 10: "SIGBUS", 11: "SIGSEGV",
    13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM",
}


def _classify_exit(status: int) -> Dict[str, Any]:
    """把 launchctl 第二列状态码归类。

    launchctl list 列：PID / Status / Label。
      * PID 非 "-" 且 status=0 → 运行中；
      * status=0 → 上次正常退出；
      * status<0 → 被信号终止（绝对值为信号号），如 -15=SIGTERM、-9=SIGKILL；
      * status>0 → 非零退出码（异常退出）。
    """
    if status < 0:
        sig = -status
        return {"state": "signal", "signal": sig,
                "signal_name": _SIGNAL_NAMES.get(sig, f"signal_{sig}"),
                "abnormal": True}
    if status == 0:
        return {"state": "exited", "signal": None, "signal_name": None, "abnormal": False}
    return {"state": "exit", "signal": None, "signal_name": None,
            "exit_code": status, "abnormal": True}


def _launchd_jobs() -> Dict[str, Any]:
    """只读扫描本机 trident launchd 作业的最近退出状态。

    边界：
      * 仅 macOS launchd；非 macOS / 无 launchctl 时降级为 {"available": False}；
      * 只读 `launchctl list`，不启停任何作业；
      * 退出状态是 launchd 记录的"上一次"结果，运行中作业显示当前 PID。
    """
    out: Dict[str, Any] = {"available": False, "jobs": [], "abnormal": []}
    try:
        raw = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=5, check=False)
        if raw.returncode != 0:
            return out
        out["available"] = True
        for line in raw.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] == "Label":
                continue
            label = parts[2]
            if not label.startswith("com.trident."):
                continue
            pid = None if parts[0] == "-" else int(parts[0])
            try:
                status = int(parts[1])
            except ValueError:
                continue
            cls = _classify_exit(status)
            job = {"label": label, "pid": pid, "status": status, **cls}
            out["jobs"].append(job)
            if cls["abnormal"]:
                out["abnormal"].append({
                    "label": label, "pid": pid, "status": status,
                    "state": cls["state"], "signal": cls.get("signal"),
                    "signal_name": cls.get("signal_name"),
                    "exit_code": cls.get("exit_code"),
                })
        out["total"] = len(out["jobs"])
        out["abnormal_count"] = len(out["abnormal"])
    except (FileNotFoundError, subprocess.SubprocessError, OSError, ValueError):
        return out
    return out


def snapshot(hours: float = 24.0) -> Dict[str, Any]:
    now = int(time.time())
    lo = int(now - hours * 3600)
    snap: Dict[str, Any] = {
        "generated_at": now,
        "window_hours": hours,
    }
    conn = _connect()
    if conn is None:
        snap["error"] = "db_unavailable"
        snap["jobs"] = _launchd_jobs()
        return snap
    try:
        snap["decisions"] = _decisions(conn, lo)
        snap["gate"] = _gate(conn, lo)
        snap["quarantine"] = _quarantine(conn, lo)
        snap["trades"] = _trades(conn, lo)
        snap["llm"] = _llm(conn, lo)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    snap["silent_drops"] = _silent_drops()
    snap["jobs"] = _launchd_jobs()
    return snap


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
