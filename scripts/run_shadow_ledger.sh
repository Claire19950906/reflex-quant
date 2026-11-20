#!/bin/bash
# 影子账本自动结算（v400）—— 手动/调试入口
#
# 正式由 launchd `com.trident.shadow-ledger` 每 4 小时触发（对齐 240 分钟结算窗口），
# 它跑的是 ~/.trident/wrappers/run_shadow_ledger.sh（内容与本文一致）。
# 为什么正式入口在 ~/.trident 而不是这里：工程在 ~/Desktop，launchd 执行桌面上的
# .sh 会被 macOS 直接拒掉（"Operation not permitted"），而 python3 有桌面权限。
#
# 干什么：
#   1. 把窗口内"闸门前的方向"（ungated_action，且 analysis_degraded=0）配上
#      决策时点的价 + 4 小时后的价，结算出 hit；
#   2. 清掉账本里语义无效的行（降级方向 / 池外 / 无方向），--commit 时自动跑；
#   3. 落 latest.json + 当日 JSON 快照到 backend/reports/shadow_ledger/，并清理 90 天前快照
#      （落盘交给 Python，因为 launchd 下的 bash 连桌面文件都读不了）。
#
# 手动运行：bash scripts/run_shadow_ledger.sh

set -uo pipefail

ROOT="/Users/claire/Desktop/金融1.0"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="${HOME}/.trident/log"
SK_DIR="${ROOT}/backend/reports/shadow_ledger"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/shadow_ledger_${TODAY}.log"

mkdir -p "${LOG_DIR}" 2>/dev/null
echo "[$(date '+%F %T')] start (manual)" >> "${LOG_FILE}"

cd "${ROOT}/backend/src_python" || exit 20

# --days 45：覆盖"上周的决策还没结算"；write() 是 upsert，重复跑不会重复记账
PYTHONPATH=. "${PY}" shadow_ledger.py --days 45 --commit \
  --snapshot-dir "${SK_DIR}" >> "${LOG_FILE}" 2>&1

echo "[$(date '+%F %T')] done (manual)" >> "${LOG_FILE}"
