#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reflection_health.py — 反思机制的调度心跳与告警

背景（两次真实事故）
--------------------
- `reflection-verify` 连续两天 03:23 死于 `database is locked`，没人知道；
- `policy-update` 04:17 因脚本搬了目录、wrapper 还指旧路径，静默失败。
共同点：**任务"跑过"不等于"跑成"**，而日志里"没有报错"在 `except Exception` 面前等于什么都没发生。

本脚本只做一件事：查**副作用计数**，不查开关、不查日志有没有报错。
  · 影子层近 24h 是否真的新增了样本；
  · 验证器最后一次成功评估距今多久；
  · 补丁队列是否长期积压；
  · launchd 的 last exit code 是否非 0。

触发的问题写入 `reflection_alert_log`（同一 source+message 未解决则只更新时间，不刷屏）。
永远 exit 0：告警失败不能反过来拖垮主账本。

用法
    python3 reflection_health.py            # 打印 + 写告警
    python3 reflection_health.py --dry-run  # 只打印，不写
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

SHADOW_MIN_24H = 3          # 影子层每天应新增 10~12 条；<3 视为大概率没跑
VERIFIER_MAX_AGE_H = 36     # 每天 03:23 跑一次；超过 36h 说明至少漏了一轮
ENRICH_MAX_AGE_H = 72       # 富化按需跑；超过 72h 说明队列或调度有问题
PATCH_MAX_AGE_DAYS = 14     # pending 补丁超过 14 天没人审 → 提醒
JOBS = [
    "com.trident.reflection-verify",
    "com.trident.policy-update-daily",
    "com.trident.meta-reflection-daily",
    "com.trident.reflection-enrich-cron",
]


def _iso_age_h(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return None


def _launchctl_status() -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    try:
        txt = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, code, label = parts[0], parts[1], parts[2]
        if label in JOBS:
            # launchctl 三列是：PID / 上次退出码 / label；"-" 表示从未运行
            try:
                out[label] = int(code)
            except ValueError:
                out[label] = None
    return out


def collect(conn: sqlite3.Connection) -> Dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    issues: List[Dict[str, Any]] = []

    def add(level: str, source: str, message: str, detail: Dict[str, Any]) -> None:
        issues.append({"level": level, "source": source, "message": message, "detail": detail})

    # 1) 影子层近 24h 增量
    n24 = int(conn.execute(
        "SELECT COUNT(*) FROM shadow_reflections WHERE created_at >= datetime('now','-1 day')"
    ).fetchone()[0] or 0)
    total = int(conn.execute("SELECT COUNT(*) FROM shadow_reflections").fetchone()[0] or 0)
    floor = conn.execute(
        "SELECT watermark_ts FROM shadow_reflection_state WHERE name='shadow_reflections_floor'"
    ).fetchone()
    if floor and n24 < SHADOW_MIN_24H:
        add("warn", "shadow_reflections",
            f"影子反思近 24h 仅新增 {n24} 条（期望 ≥{SHADOW_MIN_24H}）",
            {"n24": n24, "total": total})

    # 2) 验证器
    age_v = _iso_age_h(conn.execute(
        "SELECT MAX(evaluated_at) FROM reflection_3layer").fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='reflection_3layer'").fetchone() else None)
    if age_v is not None and age_v > VERIFIER_MAX_AGE_H:
        add("warn", "reflection_3layer",
            f"验证器已 {age_v:.1f}h 没有成功评估（阈值 {VERIFIER_MAX_AGE_H}h）",
            {"age_h": round(age_v, 1)})

    # 3) 富化
    enrich = conn.execute(
        "SELECT MAX(enriched_at) FROM trade_reflections_v2 WHERE enrich_status='ok'").fetchone()[0]
    age_e = _iso_age_h(enrich)
    if age_e is not None and age_e > ENRICH_MAX_AGE_H:
        add("info", "cron_reflection_enrich",
            f"富化最近成功在 {age_e:.1f}h 前（阈值 {ENRICH_MAX_AGE_H}h）",
            {"age_h": round(age_e, 1)})

    # 4) 补丁积压
    oldest = conn.execute(
        "SELECT MIN(created_at) FROM policy_patch_queue WHERE status='pending'").fetchone()[0]
    if oldest:
        age_d = (time.time() - float(oldest)) / 86400.0
        if age_d > PATCH_MAX_AGE_DAYS:
            add("info", "policy_patch_queue",
                f"最老 pending 补丁已积压 {age_d:.0f} 天（阈值 {PATCH_MAX_AGE_DAYS} 天）",
                {"age_days": round(age_d, 1)})

    # 5) launchd 退出码
    codes = _launchctl_status()
    for job, code in codes.items():
        if code is not None and code != 0:
            add("warn", job, f"launchd last exit code = {code}", {"exit_code": code})
    missing = [j for j in JOBS if j not in codes]
    if missing:
        add("info", "launchd", f"未在 launchctl 列表中找到: {', '.join(missing)}", {"missing": missing})

    return {"at": now, "issues": issues, "checked": {
        "shadow_total": total, "shadow_24h": n24,
        "verifier_age_h": round(age_v, 1) if age_v is not None else None,
        "enrich_age_h": round(age_e, 1) if age_e is not None else None,
        "launchd": codes,
    }}


def persist(conn: sqlite3.Connection, issues: List[Dict[str, Any]]) -> int:
    written = 0
    for it in issues:
        detail = json.dumps(it["detail"], ensure_ascii=False)
        row = conn.execute(
            "SELECT id FROM reflection_alert_log"
            " WHERE resolved=0 AND source=? AND message=? LIMIT 1",
            (it["source"], it["message"]),
        ).fetchone()
        if row:
            conn.execute("UPDATE reflection_alert_log SET ts=?, detail=? WHERE id=?",
                         (time.strftime("%Y-%m-%dT%H:%M:%S"), detail, row[0]))
        else:
            conn.execute(
                "INSERT INTO reflection_alert_log(ts, level, source, message, detail)"
                " VALUES(?,?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), it["level"], it["source"], it["message"], detail),
            )
            written += 1
    conn.commit()
    return written


def auto_resolve(conn: sqlite3.Connection, codes: Dict[str, Optional[int]]) -> int:
    """launchd 退出码回到 0 → 把该任务的未解决告警置 resolved（避免历史告警永远亮着）。"""
    n = 0
    for job, code in codes.items():
        if code != 0:
            continue
        rows = conn.execute(
            "SELECT id FROM reflection_alert_log WHERE resolved=0 AND source=?", (job,)
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE reflection_alert_log SET resolved=1, resolved_at=?, resolved_note=?"
                " WHERE id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), "launchd exit=0，自动解除", r[0]),
            )
            n += 1
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        conn = db.get_connection()
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='reflection_alert_log'").fetchone():
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS reflection_alert_log("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,"
                    " source TEXT NOT NULL, message TEXT NOT NULL, detail TEXT DEFAULT '{}',"
                    " resolved INTEGER DEFAULT 0, resolved_at TEXT, resolved_note TEXT DEFAULT '')")
            data = collect(conn)
            if not args.dry_run:
                data["written"] = persist(conn, data["issues"])
                data["auto_resolved"] = auto_resolve(conn, data["checked"]["launchd"])
        finally:
            conn.close()
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as exc:  # 告警失败不能拖垮调用方
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
