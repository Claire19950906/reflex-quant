#!/bin/bash
# weekly_reflection 自动运行包装脚本
# 由 launchd (com.trident.weekly_reflection.plist) 周一 9:00 触发
# 也可手动运行：bash cron/run_weekly_reflection.sh

set -euo pipefail

# ===== 配置 =====
TRIDENT_ROOT="/Users/claire/.cline/data/workspaces/chat/trident_v170/backend"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="${TRIDENT_ROOT}/logs"
REPORT_DIR="${TRIDENT_ROOT}/reports/weekly"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/weekly_reflection_${TODAY}.log"

# ===== 初始化 =====
mkdir -p "${LOG_DIR}" "${REPORT_DIR}"

# ===== 跑 =====
{
  echo "==============================================="
  echo "weekly_reflection 自动运行 @ $(date '+%Y-%m-%d %H:%M:%S')"
  echo "==============================================="
  cd "${TRIDENT_ROOT}"
  "${PYTHON}" src_python/weekly_reflection.py --days 7 --out "${REPORT_DIR}"
  EXIT_CODE=$?

  if [ ${EXIT_CODE} -eq 0 ]; then
    echo "✅ weekly_reflection 跑完，输出："
    echo "   报告: ${REPORT_DIR}/weekly_report_${TODAY}.md"
    echo "   AI prompt: ${REPORT_DIR}/weekly_ai_prompt_${TODAY}.md"
  else
    echo "❌ weekly_reflection 失败 (exit=${EXIT_CODE})"
    # 把失败标记到日志（人工/系统后续可告警）
    echo "FAILED_AT=$(date +%s)" > "${LOG_DIR}/.last_failure"
  fi
  echo "==============================================="
  exit ${EXIT_CODE}
} 2>&1 | tee -a "${LOG_FILE}"
