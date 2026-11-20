#!/bin/bash
# v453: 追加型表保留策略的每日班次（默认 dry-run，只出报告不删数据）。
#
# 为什么先 dry-run：
#   删除不可逆。retention_prune.py 只删「追加型表」，但真正要确认的是
#   「30 天窗口够不够用」——先把候选行数跑一周，看报告再决定是否 --apply。
#   要真删：把 RETENTION_APPLY=1 写进 backend/.env 或 launchd 环境变量。
#
# 产出：backend/logs/retention_prune_<日期>.log 与 reports/retention/latest.json

set -uo pipefail

TRIDENT_ROOT="/Users/claire/Desktop/金融1.0/backend"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="${TRIDENT_ROOT}/logs"
OUT_DIR="${TRIDENT_ROOT}/reports/retention"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/retention_prune_${TODAY}.log"

mkdir -p "${LOG_DIR}" "${OUT_DIR}"

cd "${TRIDENT_ROOT}" || exit 1

APPLY_FLAG=""
if [ "${RETENTION_APPLY:-0}" = "1" ]; then
  APPLY_FLAG="--apply"
fi

{
  echo "==============================================="
  echo "retention_prune @ $(date '+%Y-%m-%d %H:%M:%S') apply=${RETENTION_APPLY:-0}"
  echo "==============================================="
  "${PYTHON}" scripts/retention_prune.py ${APPLY_FLAG} --json "${OUT_DIR}/latest.json"
  EXIT_CODE=$?
  echo "exit=${EXIT_CODE}"
  echo "报告: ${OUT_DIR}/latest.json"
  echo "==============================================="
  exit ${EXIT_CODE}
} 2>&1 | tee -a "${LOG_FILE}"
