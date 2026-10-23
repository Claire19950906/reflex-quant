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


def _run_kg_auto_approve() -> Dict[str, Any]:
    """R2 修复：批量自动审批 kg_improvements 中高置信度的 pending 候选。"""
    import kg_improvements
    return kg_improvements.auto_approve_pending(
        min_confidence=0.85, batch_max=100, dry_run=False
    )


def _run_attribution_auto_review() -> Dict[str, Any]:
    """R3 修复：批量自动通过归因复核中 pending 高置信度的 LOSS 行。"""
    import review_agent
    return review_agent.auto_review_attributions(
        min_confidence=0.85, batch_max=200, dry_run=False
    )


def main() -> int:
    t0 = time.monotonic()
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    out: Dict[str, Any] = {
        "started_at": started,
        "ok": True,
        "steps": {},
    }
    try:
        # R2: KG 候选审批
        r2 = _run_kg_auto_approve()
        out["steps"]["r2_kg_approve"] = {
            "scanned": r2.get("scanned", 0),
            "approved_ids": r2.get("approved", []),
            "approved_count": len(r2.get("approved") or []),
            "skipped": r2.get("skipped", 0),  # R2 是 int
            "threshold": r2.get("threshold"),
            "dry_run": r2.get("dry_run", False),
        }

        # R3: 归因复核
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