#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cron_reflection_enrich.py — 反思富化调度入口 (v337)

为什么需要这个模块
------------------
trade_reflections_v2 有 6,174 条反思，两条富化链路都**没有调度**：

    列                     ok     pending
    enrich_status（深度）    291    5,883     ← reflection_deep.enrich_reflection
    core_enrich_status     118    6,056     ← reflection_core.enrich_reflection_core

两个 enrich_pending_loop 都写好了「daemon 每 5 分钟跑一次」的 docstring，
但 launchd / wrapper 里一个都没注册 → 反思只停在"贴标签"：
error_layer 空 96.9%、counterfactual 空 100%、grade C 占 99.1%。

实测还发现一个隐藏断点：pending 里有 1,214 条 `decision_id` 为空（早期
SKHYNIX 等非核心品种反思），core 富化必须靠 ai_decisions 才能算出因果链/
反证据，所以这些行**永远不可能成功**；而原循环 ORDER BY id DESC，每次
先撞上这批死行，白烧 batch。本脚本因此自己挑「有决策链接」的行，并把
无链接的标成 `unlinked`（可 --reset-unlinked 回滚），让"待办数"回归真实。

两条链路都是纯本地计算、不调 LLM（单条 10~30ms）。

用法:
    python3 cron_reflection_enrich.py                 # 默认 400 条 / 15 分钟预算
    python3 cron_reflection_enrich.py --total 1200    # 一次性清积压
    python3 cron_reflection_enrich.py --dry-run       # 只报待办数
    python3 cron_reflection_enrich.py --mark-unlinked # 把无决策链接的标成 unlinked
    python3 cron_reflection_enrich.py --reset-unlinked
退出码: 0 = 正常；1 = 本轮 0 条成功
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db
from review import reflection_core
from review import reflection_deep


def _conn():
    """取一个可写连接并等到锁（WAL 下允许并发写者排队）。"""
    conn = db.get_connection()
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _counts() -> dict:
    conn = _conn()
    try:
        linked = "COALESCE(decision_id,'')<>''"
        deep = conn.execute(
            f"""SELECT SUM(CASE WHEN COALESCE(enrich_status,'pending')='pending' AND {linked} THEN 1 ELSE 0 END),
                       SUM(CASE WHEN COALESCE(enrich_status,'pending')='pending' AND NOT ({linked}) THEN 1 ELSE 0 END)
                  FROM trade_reflections_v2"""
        ).fetchone()
        core = conn.execute(
            f"""SELECT SUM(CASE WHEN COALESCE(core_enrich_status,'pending')='pending' AND {linked} THEN 1 ELSE 0 END),
                       SUM(CASE WHEN COALESCE(core_enrich_status,'pending')='pending' AND NOT ({linked}) THEN 1 ELSE 0 END)
                  FROM trade_reflections_v2"""
        ).fetchone()
        return {
            "deep_pending_linked": int(deep[0] or 0),
            "deep_pending_unlinked": int(deep[1] or 0),
            "core_pending_linked": int(core[0] or 0),
            "core_pending_unlinked": int(core[1] or 0),
        }
    finally:
        conn.close()


def _take_ids(column: str, limit: int) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            f"""SELECT id FROM trade_reflections_v2
                 WHERE (COALESCE({column},'pending')='pending')
                   AND COALESCE(decision_id,'')<>''
                 ORDER BY id ASC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [int(r[0]) for r in rows]
    finally:
        conn.close()


def mark_unlinked(reset: bool = False) -> dict:
    conn = _conn()
    try:
        if reset:
            n1 = conn.execute(
                "UPDATE trade_reflections_v2 SET core_enrich_status='pending' WHERE core_enrich_status='unlinked'"
            ).rowcount
            n2 = conn.execute(
                "UPDATE trade_reflections_v2 SET enrich_status='pending' WHERE enrich_status='unlinked'"
            ).rowcount
            conn.commit()
            return {"reset_core": n1, "reset_deep": n2}
        n1 = conn.execute(
            """UPDATE trade_reflections_v2
                  SET core_enrich_status='unlinked',
                      core_enrich_error='no_decision_link'
                WHERE COALESCE(core_enrich_status,'pending')='pending'
                  AND COALESCE(decision_id,'')=''"""
        ).rowcount
        n2 = conn.execute(
            """UPDATE trade_reflections_v2
                  SET enrich_status='unlinked',
                      enrich_error='no_decision_link'
                WHERE COALESCE(enrich_status,'pending')='pending'
                  AND COALESCE(decision_id,'')=''"""
        ).rowcount
        conn.commit()
        return {"core_marked": n1, "deep_marked": n2}
    finally:
        conn.close()



def _mark_failed(rid: int, column: str, error: str) -> None:
    """把确定性失败（找不到反思/找不到决策）落库成 failed，避免无限重试。"""
    conn = _conn()
    try:
        conn.execute(
            f"UPDATE trade_reflections_v2 SET {column}='failed', "
            f"{column.replace('_status', '_error')}=? WHERE id=?",
            (error[:200], int(rid)),
        )
        conn.commit()
    finally:
        conn.close()

def run(total: int = 400, budget_sec: int = 900, batch: int = 50) -> dict:
    t0 = time.time()
    out = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "before": _counts(),
        "deep": {"scanned": 0, "ok": 0, "partial": 0, "failed": 0},
        "core": {"scanned": 0, "ok": 0, "partial": 0, "failed": 0},
        "errors": [],
        "rounds": 0,
    }
    done = 0
    idle = 0
    while done < total and (time.time() - t0) < budget_sec:
        n = min(batch, total - done)
        ids_deep = _take_ids("enrich_status", n)
        ids_core = _take_ids("core_enrich_status", n)
        if not ids_deep and not ids_core:
            break
        out["rounds"] += 1
        advanced = 0
        for rid in ids_deep:
            out["deep"]["scanned"] += 1
            try:
                r = reflection_deep.enrich_reflection(rid, write=True)
                st = r.get("enrich_status") or "failed"
                if st == "ok":
                    out["deep"]["ok"] += 1
                    advanced += 1
                elif st == "partial":
                    out["deep"]["partial"] += 1
                    advanced += 1
                else:
                    out["deep"]["failed"] += 1
                    err = str(r.get("enrich_error") or "")
                    if err in ("reflection_not_found",):
                        _mark_failed(rid, "enrich_status", err)
                    if err and len(out["errors"]) < 10:
                        out["errors"].append(f"deep id={rid}: {err}")
            except Exception as exc:
                out["deep"]["failed"] += 1
                if len(out["errors"]) < 10:
                    out["errors"].append(f"deep id={rid}: {type(exc).__name__}: {exc}")
        for rid in ids_core:
            out["core"]["scanned"] += 1
            try:
                r = reflection_core.enrich_reflection_core(rid, write=True)
                st = r.get("enrich_status") or "failed"
                if st == "ok":
                    out["core"]["ok"] += 1
                    advanced += 1
                elif st == "partial":
                    out["core"]["partial"] += 1
                    advanced += 1
                else:
                    out["core"]["failed"] += 1
                    err = str(r.get("enrich_error") or "")
                    if err in ("decision_not_found", "reflection_not_found"):
                        _mark_failed(rid, "core_enrich_status", err)
                    if err and len(out["errors"]) < 10:
                        out["errors"].append(f"core id={rid}: {err}")
            except Exception as exc:
                out["core"]["failed"] += 1
                if len(out["errors"]) < 10:
                    out["errors"].append(f"core id={rid}: {type(exc).__name__}: {exc}")
        done += max(len(ids_deep), len(ids_core))
        print(
            f"  [enrich] round={out['rounds']} deep ok/part/fail="
            f"{out['deep']['ok']}/{out['deep']['partial']}/{out['deep']['failed']} "
            f"core ok/part/fail={out['core']['ok']}/{out['core']['partial']}/{out['core']['failed']}",
            flush=True,
        )
        if advanced == 0:
            idle += 1
            if idle >= 2:
                out["errors"].append("连续两轮 0 成功，提前退出")
                break
        else:
            idle = 0

    out["after"] = _counts()
    out["elapsed_sec"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=400)
    ap.add_argument("--budget-sec", type=int, default=900)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mark-unlinked", action="store_true")
    ap.add_argument("--reset-unlinked", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.mark_unlinked or args.reset_unlinked:
        print(json.dumps(mark_unlinked(reset=args.reset_unlinked), ensure_ascii=False))
        return 0
    if args.dry_run:
        print(json.dumps(_counts(), ensure_ascii=False))
        return 0

    res = run(total=args.total, budget_sec=args.budget_sec, batch=args.batch)
    # v413: 无事可做就别刷日志（此前每 10 分钟打一行 JSON，日志一天 250KB，
    # 且"什么都没扫到"和"扫了但全失败"在日志里长得一样）。
    # 判据用「扫描量」而不是「待办数为 0」：待办为 0 但有新行会被扫到，仍需留痕。
    scanned_total = res["deep"]["scanned"] + res["core"]["scanned"]
    if scanned_total == 0 and not args.json:
        return 0
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(
            f"[reflection-enrich] rounds={res['rounds']} · {res['elapsed_sec']}s\n"
            f"  before: {res['before']}\n  after : {res['after']}"
        )
        if res["errors"]:
            print("  errors:", "; ".join(res["errors"][:3]))
    ok = res["deep"]["ok"] + res["deep"]["partial"] + res["core"]["ok"] + res["core"]["partial"]
    scanned = res["deep"]["scanned"] + res["core"]["scanned"]
    # 没有待办 → 0（"无事可做"不是故障）；有待办但全失败 → 1（需要人看）
    return 0 if (ok > 0 or scanned == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
