#!/bin/bash
# 宏观驱动系数校准包装脚本
# 由 launchd (com.trident.macro-calibration.plist) 每月 1 日 07:20 触发
# 也可手动运行：bash cron/run_macro_calibration.sh [--years N] [--refresh]
#
# 为什么是月频而不是日频：
#   · 驱动系数是"慢变量之上的慢结论"，多跑一天不会改变结论，只会多消耗 FRED 请求；
#   · 但必须周期跑 —— 系数一旦漂移，tilt 会静默失效，没人会去看它。
#
# 环境变量：
#   TRIDENT_MACRO_CALIB_YEARS  回看年数（缺省 5）
#   TRIDENT_MACRO_CALIB_LOG_DAYS 日志保留天数（缺省 365，月频不必频繁清理）

set -uo pipefail

TRIDENT_ROOT="/Users/claire/Desktop/金融1.0/backend"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="/Users/claire/.trident/log"
MONTH=$(date +%Y-%m)
LOG_FILE="${LOG_DIR}/macro_calibration_${MONTH}.log"
YEARS="${TRIDENT_MACRO_CALIB_YEARS:-5}"
KEEP_DAYS="${TRIDENT_MACRO_CALIB_LOG_DAYS:-365}"

mkdir -p "${LOG_DIR}"

{
  echo "==============================================="
  echo "macro_driver_calibration 自动运行 @ $(date '+%Y-%m-%d %H:%M:%S')"
  echo "==============================================="
  cd "${TRIDENT_ROOT}" || exit 1
  # 失败不标记、不重试：FRED 抖一下下次月度再跑即可，写进日志就够
  "${PYTHON}" scripts/macro_driver_calibration.py --years "${YEARS}" "$@"
  EXIT_CODE=$?
  if [ ${EXIT_CODE} -eq 0 ]; then
    echo "✅ 校准跑完，报告 backend/reports/macro_calibration/latest.json"
  else
    echo "❌ 校准失败 (exit=${EXIT_CODE}) —— 数据源抖动，下月重跑"
  fi
  echo "==============================================="
  exit ${EXIT_CODE}
} 2>&1 | tee -a "${LOG_FILE}"

find "${LOG_DIR}" -name 'macro_calibration_*.log' -mtime +"${KEEP_DAYS}" -delete 2>/dev/null || true
