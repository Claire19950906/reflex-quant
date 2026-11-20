#!/bin/bash
# 基本面权重影子账本 —— 每日结算包装
# 由 launchd (com.trident.fundamental-shadow.plist) 每天 06:10 触发（排在财报日更 05:10 之后）
# 手动运行：bash cron/run_fundamental_shadow.sh [--days N]
#
# 干什么：把 fundamental_shadow 表里未结算的决策，用 ohlcv_daily 的日线收盘
# 回填 +1/+3/+5 天收益，算出「含基本面权重」和「剔除基本面权重」两组的命中，
# 结果写到 backend/reports/fundamental_shadow/latest.json。
#
# 为什么每天跑而不是每 4 小时：这套账结算用的是日线，4 小时窗口内没有新数据，
# 空转只会把 AI 复盘日志刷满。日报一次足够，且与财报日历（日频）对齐。

set -uo pipefail

TRIDENT_ROOT="/Users/claire/Desktop/金融1.0/backend"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="/Users/claire/.trident/log"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/fundamental_shadow_${TODAY}.log"

mkdir -p "${LOG_DIR}"

{
  echo "==============================================="
  echo "fundamental_shadow 结算 @ $(date '+%Y-%m-%d %H:%M:%S')"
  echo "==============================================="
  cd "${TRIDENT_ROOT}/src_python" || exit 1
  # --days 30：只结最近 30 天，老行已经 settled=1，重复跑是幂等的
  PYTHONPATH=. "${PYTHON}" fundamental_shadow.py --days 30 "$@"
  echo "==============================================="
} 2>&1 | tee -a "${LOG_FILE}"

find "${LOG_DIR}" -name 'fundamental_shadow_*.log' -mtime +30 -delete 2>/dev/null || true
