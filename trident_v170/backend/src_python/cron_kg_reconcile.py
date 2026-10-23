"""v302+ 每日 KG reconcile cron - 闭环 R2 的人工审批缺口。

调度方: launchd (com.trident.kg-reconcile-cron.plist) - 默认每天 04:30 跑一次。
入口: run_kg_reconcile_cron.sh 通过 python3 cron_kg_reconcile.py 调起。

调用顺序:
  1. cron_kg_review.py @ 04:00 已经把 status=pending 提升到 status=approved
  2. 本脚本 @ 04:30 把 status=approved 真正写回 causal_graph / causal_chains / 规则库
  3. 失败的写 apply_error (status 保持 'approved' 让人工重试)

设计:
  - 不依赖 api daemon (直接调模块函数, 单进程单脚本)
  - dry_run=False 真正落库
  - 异常时返回 ok=False + 错误信息, launchd 会记 err log

为什么必须独立于 cron_kg_review:
  kg_improvements.approved 是「审批完成」状态, 不是「已应用」状态。
  本脚本职责单一: 写回图谱/规则库, 与审批逻辑解耦。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Dict


_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _run_reconcile() -> Dict[str, Any]:
    """调用 kg_reconcile.reconcile_approved 把 approved 但未 applied 的 proposal 写回。"""
    import kg_reconcile
    return kg_reconcile.reconcile_approved(batch_max=50, dry_run=False)


def main() -> int:
    t0 = time.monotonic()
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    out: Dict[str, Any] = {
        "started_at": started,
        "ok": True,
        "steps": {},
    }
    try:
        r = _run_reconcile()
        out["steps"]["kg_reconcile_apply"] = {
            "scanned": r.get("scanned", 0),
            "applied_ids": r.get("applied_ids", []),
            "applied_count": len(r.get("applied_ids") or []),
            "applied_targets": r.get("applied_targets", {}),
            "failed": r.get("failed", []),
            "failed_count": len(r.get("failed") or []),
            "dry_run": r.get("dry_run", False),
        }
        out["elapsed_sec"] = round(time.monotonic() - t0, 2)
        out["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:
        out["ok"] = False
        out["error"] = type(exc).__name__ + ": " + str(exc)[:200]
        out["trace"] = traceback.format_exc(limit=4)
        out["elapsed_sec"] = round(time.monotonic() - t0, 2)
        sys.stderr.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
