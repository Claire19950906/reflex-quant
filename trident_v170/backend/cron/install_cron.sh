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

TRIDENT_CRON_DIR="/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/cron"

# Job 注册表: label → plist + 脚本路径 + 时间描述
JOBS=(
  "weekly_reflection|com.trident.weekly_reflection.plist|run_weekly_reflection.sh|周一 09:00"
  "kg-reconcile-cron|com.trident.kg-reconcile-cron.plist|run_kg_reconcile.sh|每天 04:30"
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
  local LOG_DIR="/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/logs"
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
  weekly_reflection    周一 09:00 跑一次反思报告
  kg-reconcile-cron    每天 04:30 把 approved KG 候选写回图谱

不指定 job 时操作全部。
EOF
    exit 1;;
esac
