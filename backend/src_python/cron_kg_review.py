"""每日 KG/Review 自动审批 cron — 闭环 R2+R3 的人工审批缺口。

调度方: launchd (com.trident.kg-review-cron.plist) — 默认每天 04:00 跑一次。
入口: run_kg_review_cron.sh 通过 python3 cron_kg_review.py 调起。

职责:
  1. 调 kg_improvements.auto_approve_pending(min_confidence=0.85)
     → 把高置信度的 KG 候选从 pending 提升到 approved
  2. 调 review_agent.auto_review_attributions(min_confidence=0.85)
     → 把高置信度的归因复核从 pending 提升到 auto_reviewed

输出: JSON 行（含 ok/approved_n/reviewed_n/skipped_n/error）写入 stdout
       由 launchd 重定向到 /Users/claire/.trident/log/kg_review_cron.out.log

设计:
  - 不依赖 api daemon（直接调模块函数，单进程单脚本）
  - 不修改任何人工已审核的行（R2/R3 函数本身已做 human_status 守护）
  - dry_run=False 真正落库；如需预览可改 dry_run=True 临时手测
  - 异常时返回 ok=False + 错误信息，launchd 会记 err log

为什么不挂在 api_server lifespan:
  lifespan 是冷启动触发的；daemon 进程不死不会重跑；
  用 launchd 定时任务才是真正「每天一次」的闭环。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Dict

# 允许脚本独立跑（wrapper 不依赖 PYTHONPATH）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _is_locked(exc: BaseException) -> bool:
    """判断是否 SQLite 写锁冲突。"""
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def _retry_on_lock(fn, name: str, attempts: int = 5, base_sleep: float = 6.0):
    """写锁冲突时**指数退避重试**。

    为什么需要 (v321.3 P2):
      db.get_connection() 的 busy_timeout 只有 5s, 而 db.py 注释里实测
      forward_tracker 在 _track_cycle 期间**持写锁 30~49s** → 5s 等不到释放
      → 直接抛 "database is locked" (2026-10-29 / 10-31 / 11-09 三次 cron 失败)。

    退避序列 6/12/24/48s, 总计最多等 ~90s, 足以覆盖最坏持锁窗口。
    只对 "locked/busy" 重试; 其它异常直接抛出 (不掩盖真 bug)。
    """
    import sqlite3
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            last = exc
            if i >= attempts - 1:
                break
            wait = base_sleep * (2 ** i)
            print(f"[KG-REVIEW] ⚠️ {name} 遇写锁冲突 (第 {i + 1}/{attempts} 次) → "
                  f"{wait:.0f}s 后重试", flush=True)
            time.sleep(wait)
    assert last is not None
    raise last


def _run_kg_auto_approve() -> Dict[str, Any]:
    """R2 修复：批量自动审批 kg_improvements 中高置信度的 pending 候选。"""
    import kg_improvements
    return _retry_on_lock(
        lambda: kg_improvements.auto_approve_pending(
            min_confidence=0.85, batch_max=100, dry_run=False),
        "kg_auto_approve")


def _run_attribution_auto_review() -> Dict[str, Any]:
    """R3 修复：批量自动通过归因复核中 pending 高置信度的 LOSS 行。"""
    from review import review_agent
    return _retry_on_lock(
        lambda: review_agent.auto_review_attributions(
            min_confidence=0.85, batch_max=200, dry_run=False),
        "attr_auto_review")


def main() -> int:
    t0 = time.monotonic()
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    out: Dict[str, Any] = {
        "started_at": started,
        "ok": True,
        "steps": {},
    }
    try:
        # ── R2: KG 候选审批 ──
        # ⚠️ v321.3 P2: 每个步骤**独立容错** —— 原来 R2 抛异常会直接跳到
        # 整个 main 的 except → **R3 根本没跑**, 一次锁冲突让两个闭环全废。
        # 现在: 单步失败只记 error, 另一步照常执行, 最后统一汇总 ok。
        step_errors = []
        try:
            r2 = _run_kg_auto_approve()
            out["steps"]["r2_kg_approve"] = {
                "scanned": r2.get("scanned", 0),
                "approved_ids": r2.get("approved", []),
                "approved_count": len(r2.get("approved") or []),
                "skipped": r2.get("skipped", 0),  # R2 是 int
                "threshold": r2.get("threshold"),
                "dry_run": r2.get("dry_run", False),
            }
        except Exception as exc:
            step_errors.append(f"r2_kg_approve: {type(exc).__name__}: {str(exc)[:160]}")
            out["steps"]["r2_kg_approve"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            print(f"[KG-REVIEW] ❌ R2 失败 (R3 继续): {type(exc).__name__}: {str(exc)[:120]}",
                  flush=True)

        # ── R3: 归因复核 ──
        try:
            r3 = _run_attribution_auto_review()
            out["steps"]["r3_attr_review"] = {
                "scanned": r3.get("scanned", 0),
                "reviewed_ids": r3.get("approved", []),
                "reviewed_count": len(r3.get("approved") or []),
                "skipped_ids": r3.get("skipped", []),  # R3 是 list[int]
                "skipped_count": len(r3.get("skipped") or []),
                "threshold_confidence": r3.get("threshold_confidence"),
                "only_source": r3.get("only_source"),
                "dry_run": r3.get("dry_run", False),
            }
        except Exception as exc:
            step_errors.append(f"r3_attr_review: {type(exc).__name__}: {str(exc)[:160]}")
            out["steps"]["r3_attr_review"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            print(f"[KG-REVIEW] ❌ R3 失败: {type(exc).__name__}: {str(exc)[:120]}",
                  flush=True)

        if step_errors:
            out["ok"] = False
            out["error"] = "; ".join(step_errors)
        out["elapsed_sec"] = round(time.monotonic() - t0, 2)
        out["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        # 一行 JSON 便于日志解析
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        out["trace"] = traceback.format_exc(limit=4)
        out["elapsed_sec"] = round(time.monotonic() - t0, 2)
        sys.stderr.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())