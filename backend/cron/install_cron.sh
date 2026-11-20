#!/bin/bash
# 安装/卸载/查看 launchd jobs
# v302+ 支持多 job: weekly_reflection / kg-reconcile-cron
# 用法：
#   bash cron/install_cron.sh install   # 加载所有 plist
#   bash cron/install_cron.sh install weekly_reflection  # 单个
#   bash cron/install_cron.sh uninstall kg-reconcile-cron
#   bash cron/install_cron.sh status
#   bash cron/install_cron.sh run kg-reconcile-cron
#   bash cron/install_cron.sh test kg-reconcile-cron

set -euo pipefail

# v406: 项目已从 ~/.cline/data/workspaces/chat/trident_v170 迁到 ~/Desktop/金融1.0，
# 这里写死旧路径会让 install 找不到 plist（"⚠ not found; 跳过"）。改为按本脚本位置推导。
TRIDENT_CRON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRIDENT_ROOT="$(cd "${TRIDENT_CRON_DIR}/.." && pwd)"
LOG_BASE="${HOME}/.trident/log"

# Job 注册表: label → plist + 脚本路径 + 时间描述
JOBS=(
  "weekly_reflection|com.trident.weekly_reflection.plist|run_weekly_reflection.sh|周一 09:00"
  "kg-reconcile-cron|com.trident.kg-reconcile-cron.plist|run_kg_reconcile.sh|每天 04:30"
  "equity-fundamentals-cron|com.trident.equity-fundamentals-cron.plist|run_equity_fundamentals.sh|每天 05:10 财报日更"
  "fundamental-shadow|com.trident.fundamental-shadow.plist|run_fundamental_shadow.sh|每天 06:10 基本面权重结算"
  "macro-calibration|com.trident.macro-calibration.plist|run_macro_calibration.sh|每月 1 日 07:20 宏观驱动系数校准"
  # v483: 漏斗日报此前只有 plist、没进任何安装清单，也没进 launchd → 07:12 班次从未存在。
  "funnel-report|com.trident.funnel-report.plist|run_funnel_report.sh|每天 07:12 漏斗日报"
)

# 解析目标 jobs (默认全部)
TARGETS=()
if [ "${2:-}" = "" ]; then
  while IFS='|' read -r label plist script desc; do
    TARGETS+=("$label")
  done < <(printf '%s\n' "${JOBS[@]}" | sed 's/|.*|.*|.*|/| /')
  # fallback: 用临时文件
  if [ ${#TARGETS[@]} -eq 0 ]; then
    for j in "${JOBS[@]}"; do TARGETS+=("${j%%|*}"); done
  fi
else
  TARGETS=("$2")
fi

_install_one() {
  local label="$1"
  local plist="$2"
  local script="$3"
  local desc="$4"
  local src="${TRIDENT_CRON_DIR}/${plist}"
  local dst="${HOME}/Library/LaunchAgents/${plist}"
  local spath="${TRIDENT_CRON_DIR}/${script}"
  if [ ! -f "${src}" ]; then echo "  ⚠ ${src} not found; 跳过"; return; fi
  if [ ! -f "${spath}" ]; then echo "  ⚠ ${spath} not found; 跳过"; return; fi
  chmod +x "${spath}"
  mkdir -p "${HOME}/Library/LaunchAgents"
  cp "${src}" "${dst}"
  launchctl unload "${dst}" 2>/dev/null || true
  launchctl load -w "${dst}"
  echo "  ✅ ${label}: ${desc}"

  # v406-fix: macOS TCC 检查。
  # 实测：项目在 ~/Desktop 下，launchd 后台进程没有"桌面文件夹"访问权 →
  # kickstart 退出码 126，日志写 "Operation not permitted"，**脚本一次都执行不了**。
  # 也就是说 load 成功 ≠ 能跑。这里当场验一次，把真相说出来。
  local errlog="${LOG_BASE}/${label}.launchd.err.log"
  if [ -d "${HOME}/Desktop" ] && [[ "${TRIDENT_ROOT}" == "${HOME}/Desktop/"* ]]; then
    echo "  ⚠ 项目在 ~/Desktop 下：launchd 默认无桌面访问权（TCC），本 job 大概率跑不起来。"
    echo "    实测症状：launchctl kickstart 退出码 126 / 日志 'Operation not permitted'。"
    echo "    可跑的两条路：① 给 /bin/bash 开「完全磁盘访问」（系统设置→隐私与安全性）；"
    echo "                  ② 不用 launchd，靠常驻引擎自带调度（api_server 里已内置，默认开）。"
    [ -f "${errlog}" ] && tail -1 "${errlog}" | sed 's/^/    上次错误: /'
  fi
}

_uninstall_one() {
  local label="$1"
  local plist="$2"
  local dst="${HOME}/Library/LaunchAgents/${plist}"
  launchctl unload "${dst}" 2>/dev/null || true
  rm -f "${dst}"
  echo "  ✅ 已卸载 ${label}"
}

_status_one() {
  local label="$1"
  local plist="$2"
  local script="$3"
  local desc="$4"
  local dst="${HOME}/Library/LaunchAgents/${plist}"
  local spath="${TRIDENT_CRON_DIR}/${script}"
  echo "  ${label} (${desc})"
  if launchctl list | grep -q "${label}"; then
    echo "    状态: loaded"
  else
    echo "    状态: not loaded"
  fi
  if [ -f "${dst}" ]; then echo "    plist: ${dst}"; else echo "    plist: (未安装)"; fi
  if [ -f "${spath}" ]; then echo "    script: ${spath}"; fi
  local LOG_DIR="${LOG_BASE}"
  local job_log="${LOG_DIR}/${label}.log"
  if [ -f "${job_log}" ]; then
    echo "    最近日志 (5 行):"
    tail -5 "${job_log}" | sed 's/^/      /'
  fi
}

_run_one() {
  local label="$1"
  local plist="$2"
  local script="$3"
  local spath="${TRIDENT_CRON_DIR}/${script}"
  if [ ! -f "${spath}" ]; then echo "  ⚠ ${spath} not found"; return; fi
  echo "=== 手动跑 ${label} ==="
  bash "${spath}"
}

_test_one() {
  local label="$1"
  local script="$2"
  local spath="${TRIDENT_CRON_DIR}/${script}"
  if [ ! -f "${spath}" ]; then echo "  ⚠ ${spath} not found"; return; fi
  echo "=== 直接测试 ${label} 脚本 ==="
  bash -n "${spath}" && echo "  脚本语法 OK"
  bash "${spath}"
}

_iterate_jobs() {
  local action="$1"
  shift
  for j in "${JOBS[@]}"; do
    IFS='|' read -r label plist script desc <<< "$j"
    # 是否匹配 TARGETS (无第二参数 = 全部)
    local matched=0
    for t in "$@"; do
      if [ "$t" = "$label" ]; then matched=1; break; fi
    done
    [ $matched -eq 1 ] && "$action" "$label" "$plist" "$script" "$desc"
  done
}

case "${1:-help}" in
  install)   _iterate_jobs _install_one   "${TARGETS[@]}";;
  uninstall) _iterate_jobs _uninstall_one "${TARGETS[@]}";;
  status)    _iterate_jobs _status_one   "${TARGETS[@]}";;
  run)       _iterate_jobs _run_one      "${TARGETS[@]}";;
  test)      _iterate_jobs _test_one     "${TARGETS[@]}";;
  help|*)
    cat <<EOF
用法: $0 {install|uninstall|status|run|test} [job]

可用 jobs:
  weekly_reflection         周一 09:00 跑一次反思报告
  kg-reconcile-cron         每天 04:30 把 approved KG 候选写回图谱
  equity-fundamentals-cron  每天 05:10 抓美股三表 + A股港股财务读数并落库

不指定 job 时操作全部。
EOF
    exit 1;;
esac
