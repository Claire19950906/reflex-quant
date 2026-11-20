#!/bin/bash
# 追加型表保留策略的【真删】班次（与 run_retention_prune.sh 分工）。
#
# 分工（避免两套口径各写一个数字）：
#   run_retention_prune.sh  dry-run，只统计候选 + 出 reports/retention/latest.json 报告；
#   run_retention.sh         真删，按 retention_prune.py 内 TABLES 的保留窗口分批删除。
#
# 窗口与时间口径（秒 / 毫秒 / 文本）全部由 scripts/retention_prune.py 维护，
# 本 wrapper 只负责：定位仓库、选解释器、记日志、按 RETENTION_APPLY 决定是否 --apply。
#
# 安全：默认 RETENTION_APPLY=1（真删）；设 RETENTION_APPLY=0 可临时退回 dry-run。
set -uo pipefail

TRIDENT_ROOT="/Users/claire/Desktop/金融1.0/backend"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"
LOG_DIR="${TRIDENT_ROOT}/logs"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/retention_apply_${TODAY}.log"

mkdir -p "${LOG_DIR}"
cd "${TRIDENT_ROOT}" || exit 1

APPLY_FLAG="--apply"
if [ "${RETENTION_APPLY:-1}" = "0" ]; then
  APPLY_FLAG=""
fi

{
  echo "==============================================="
  echo "retention apply @ $(date '+%Y-%m-%d %H:%M:%S') apply=${RETENTION_APPLY:-1}"
  echo "==============================================="
  "${PYTHON}" scripts/retention_prune.py ${APPLY_FLAG}
  EXIT_CODE=$?
  echo "exit=${EXIT_CODE}"
  echo "==============================================="
  exit ${EXIT_CODE}
} 2>&1 | tee -a "${LOG_FILE}"
