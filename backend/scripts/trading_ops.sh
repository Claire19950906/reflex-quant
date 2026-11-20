#!/usr/bin/env bash
# 交易链路运行态一键操作（v495）
#
# 为什么单独做一个脚本：代码改完后要生效，必须重启 bridge / 重装守护进程，
# 这些动作不适合塞进 pytest（要碰真实进程）。这里把常用动作固定下来，
# 避免每次凭记忆敲 launchctl。
#
# 用法：
#   bash scripts/trading_ops.sh status            # 看 bridge/守护/暂停标记/健康度
#   bash scripts/trading_ops.sh restart-bridge    # 重启 bridge（使新 API 路由生效）
#   bash scripts/trading_ops.sh enable-guard      # 恢复 stops-guard（暂停期间会拒绝）
#   bash scripts/trading_ops.sh enable-guard --force
#   bash scripts/trading_ops.sh resolve-alert account_reconcile:R7_LEVERAGE
set -uo pipefail

BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UID_NUM="$(id -u)"
PAUSE_FLAG="$HOME/.trident/state/DATA_PAUSED"
BRIDGE_LABEL="com.trident.bridge"
GUARD_LABEL="com.trident.stops-guard"
GUARD_PLIST="$HOME/Library/LaunchAgents/$GUARD_LABEL.plist"
BRIDGE_PORT="${BRIDGE_PORT:-8100}"

health() {
  curl -s -m 10 "http://127.0.0.1:${BRIDGE_PORT}/api/trading/health" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print("health ok=%s equity=%s lev=%s%% open=%s stale=%s warns=%s" % (d.get("ok"), d.get("equity"), d.get("leverage_pct"), d.get("n_open"), d.get("stale_open_markets"), d.get("warnings")))' \
    2>/dev/null || echo "health: 取不到（bridge 没起或路由未生效）"
}

cmd_status() {
  echo "== 暂停标记 =="
  if [ -f "$PAUSE_FLAG" ]; then echo "DATA_PAUSED=是（引擎被暂停，守护进程会被 pause-guard 压回）"; else echo "DATA_PAUSED=否"; fi
  echo "== launchd =="
  launchctl list | grep -E "com.trident.(bridge|stops-guard|pause-guard|engine)" || echo "(无相关任务)"
  echo "== health =="
  health
}

cmd_restart_bridge() {
  echo "重启 $BRIDGE_LABEL ..."
  launchctl kickstart -k "gui/${UID_NUM}/com.trident.bridge"
  for _ in $(seq 1 30); do
    sleep 2
    if curl -s -m 3 -o /dev/null "http://127.0.0.1:${BRIDGE_PORT}/api/trading/health"; then break; fi
  done
  health
}

cmd_enable_guard() {
  local force="${1:-}"
  if [ -f "$PAUSE_FLAG" ] && [ "$force" != "--force" ]; then
    echo "拒绝：DATA_PAUSED 还在（说明另一条工作线在改代码，pause-guard 会立刻把守护踢掉）。"
    echo "确认要开：先 bash ~/.trident/wrappers/resume_data.sh，或加 --force。"
    return 1
  fi
  if [ ! -f "$GUARD_PLIST" ]; then echo "缺少 plist: $GUARD_PLIST"; return 1; fi
  launchctl bootout "gui/${UID_NUM}/${GUARD_LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/${UID_NUM}" "$GUARD_PLIST" && echo "已加载 $GUARD_LABEL"
  launchctl list | grep "$GUARD_LABEL" || true
}

cmd_resolve_alert() {
  local key="${1:?用法: resolve-alert <dedupe_key>}"
  ( cd "$BACKEND" && python3 - "$key" <<'PY'
import sqlite3, sys, time
key = sys.argv[1]
c = sqlite3.connect("trident_event_bus.db")
n = c.execute("UPDATE t2_alerts SET resolved_at=? WHERE dedupe_key=? AND resolved_at IS NULL",
              (int(time.time()), key)).rowcount
c.commit()
print(f"resolved {n} 条 {key}")
PY
  )
}

case "${1:-status}" in
  status) cmd_status ;;
  restart-bridge) cmd_restart_bridge ;;
  enable-guard) shift; cmd_enable_guard "${1:-}" ;;
  resolve-alert) shift; cmd_resolve_alert "${1:-}" ;;
  *) sed -n '2,18p' "$0" ;;
esac
