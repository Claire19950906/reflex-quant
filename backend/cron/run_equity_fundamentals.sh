#!/bin/bash
# 公司级财报日更包装脚本
# 由 launchd (com.trident.equity-fundamentals-cron.plist) 每天 05:10 触发
# 也可手动运行：bash cron/run_equity_fundamentals.sh [额外参数]
#
# 为什么单独一个日更任务（而不是塞进宏观层 22 分钟一轮）：
#   · 三表是季报，一天抓一次足够；
#   · Alpha Vantage 免费档 25 次/日、每只票最多 4 次 → 一轮只能刷 ~6 只，
#     靠 TTL(168h) + 最旧优先轮转，几天铺满全名单；
#   · FMP 免费档 statement 端点常返 402 → 自动退 AV，失败只记账不改库。
#
# 环境变量：
#   TRIDENT_CN_FUND_TICKERS  A股/港股关注清单（东财口径，不占 AV 配额）
#                             缺省 "600519.SH,00700.HK"；设为空串可关闭
#   TRIDENT_AV_THROTTLE_SEC   AV 调用间隔秒数（缺省 13，免费档 5 次/分钟）

set -uo pipefail

TRIDENT_ROOT="/Users/claire/Desktop/金融1.0/backend"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG_DIR="/Users/claire/.trident/log"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="${LOG_DIR}/equity_fundamentals_${TODAY}.log"
CN_TICKERS="${TRIDENT_CN_FUND_TICKERS-600519.SH,00700.HK}"
export TRIDENT_AV_THROTTLE_SEC="${TRIDENT_AV_THROTTLE_SEC:-13}"

mkdir -p "${LOG_DIR}"

# v406-fix: 当日互斥。引擎（api_server 的 equity_fundamentals_daily_watcher）和
# 本 launchd job 都会触发日更，靠这个状态文件保证"一天只跑一次"——否则两份
# 调度各烧一遍 25 次 AV 配额，轮到什么都不会剩。
STATE_FILE="${LOG_DIR}/equity_fundamentals.last_run"
if [ -f "${STATE_FILE}" ] && [ "$(cat "${STATE_FILE}" 2>/dev/null | tr -d '[:space:]')" = "${TODAY}" ]; then
  echo "[跳过] 今天(${TODAY})已经跑过（状态文件 ${STATE_FILE}），不再重复消耗配额"
  exit 0
fi

{
  echo "==============================================="
  echo "equity_fundamentals 自动运行 @ $(date '+%Y-%m-%d %H:%M:%S')"
  echo "==============================================="
  cd "${TRIDENT_ROOT}" || exit 1

  # 名称字典每周刷一次就够（5800 只 A股/2700 只港股，不会天天变）；用 files mtime 判周
  NAME_MAP_STAMP="${LOG_DIR}/.name_map_last"
  NEED_NAME_MAP=0
  if [ ! -f "${NAME_MAP_STAMP}" ] || [ -n "$(find "${NAME_MAP_STAMP}" -mtime +7 2>/dev/null)" ]; then
    NEED_NAME_MAP=1
  fi
  if [ "${NEED_NAME_MAP}" -eq 1 ]; then
    echo "[名称字典] 距上次刷新超过 7 天（或从未跑过），本轮先刷"
    "${PYTHON}" scripts/cron_equity_fundamentals.py --name-map || true
    touch "${NAME_MAP_STAMP}"
  fi

  # 近 7 天决策里反解出来的 A股/港股（新闻点名但模型只给了占位标签的），抓 10 只读数
  if [ "${TRIDENT_CN_AUTO-10}" != "0" ]; then
    "${PYTHON}" scripts/cron_equity_fundamentals.py --cn-auto "${TRIDENT_CN_AUTO-10}" || true
  fi

  ARGS=(scripts/cron_equity_fundamentals.py --show)
  if [ -n "${CN_TICKERS}" ]; then
    ARGS+=(--cn "${CN_TICKERS}")
  fi
  "${PYTHON}" "${ARGS[@]}" "$@"
  EXIT_CODE=$?

  # v454：三条补洞通道。都免配额（SEC 官方接口 / 本地推导 / 缓存预热），失败不阻塞主流程。
  # 不跑这三步的后果很具体：市值序列停在建表那天 → PB 的"当前值"变成化石，分位慢慢失真。
  "${PYTHON}" scripts/cron_equity_fundamentals.py --sec-fill ALL || true
  "${PYTHON}" scripts/cron_equity_fundamentals.py --mktcap-fill 1 || true
  "${PYTHON}" scripts/cron_equity_fundamentals.py --peer-warm || true
  # v455：A股/港股 估值历史（东财多期财报 × 腾讯日线），不耗配额
  "${PYTHON}" scripts/cron_equity_fundamentals.py --cn-valuation ALL || true
  # v456：口径体检（推导值 vs 供应商当期值；5% 提示、25% 报警）。不写库、只出报告。
  "${PYTHON}" scripts/fundamental_drift_check.py || true
  # v456：基本面权重离线 A/B（样本不足会自己说 insufficient，不会编胜率）
  "${PYTHON}" scripts/fundamental_ab_replay.py || true

  if [ ${EXIT_CODE} -eq 0 ]; then
    echo "${TODAY}" > "${STATE_FILE}"
    echo "✅ equity_fundamentals 跑完（已标记 ${TODAY} 完成）"
  else
    echo "❌ equity_fundamentals 失败 (exit=${EXIT_CODE})（配额耗尽/源全挂属预期内，次日继续轮转）"
    echo "FAILED_AT=$(date +%s)" > "${LOG_DIR}/.last_failure"
  fi
  echo "==============================================="
  exit ${EXIT_CODE}
} 2>&1 | tee -a "${LOG_FILE}"

# 日志只留 14 天，这个任务每天一个文件
find "${LOG_DIR}" -name 'equity_fundamentals_*.log' -mtime +14 -delete 2>/dev/null || true
