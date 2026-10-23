#!/bin/bash
# KG reconcile 自动运行包装脚本
# 由 launchd (com.trident.kg-reconcile-cron.plist) 每天 04:30 触发
# 也可手动运行：bash cron/run_kg_reconcile.sh
#
# 调度顺序（不要改）：
#   04:00  kg_review_cron  - 把 status=pending 提升到 approved
#   04:10  causal_expand    - LLM 反推新因果链
#   04:30  kg_reconcile     - 本脚本，把 approved 真正写回图谱
#   06:00  causal_graph_refresh (每 6h)

set -euo pipefail

TRIDENT_ROOT="/Users/claire/.cline/data/workspaces/chat/trident_v170/backend"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="${TRIDENT_ROOT}/logs"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/kg_reconcile_${TODAY}.log"

mkdir -p "${LOG_DIR}"

{
  echo "==============================================="
  echo "kg_reconcile 自动运行 @ $(date '+%Y-%m-%d %H:%M:%S')"
  echo "==============================================="
  cd "${TRIDENT_ROOT}"
  "${PYTHON}" src_python/cron_kg_reconcile.py
  EXIT_CODE=$?

  if [ ${EXIT_CODE} -eq 0 ]; then
    echo "✅ kg_reconcile 跑完"
  else
    echo "❌ kg_reconcile 失败 (exit=${EXIT_CODE})"
    echo "FAILED_AT=$(date +%s)" > "${LOG_DIR}/.last_failure"
  fi
  echo "==============================================="
  exit ${EXIT_CODE}
} 2>&1 | tee -a "${LOG_FILE}"
