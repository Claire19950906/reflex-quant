#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# =============================================================================
# Trident macOS 一键停止
# 用法：  scripts/macos/stop_all.sh
# 效果：  从 launchd 卸载三个服务，并清理残留端口进程。
# =============================================================================

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

echo "============================================================"
echo " Trident macOS 守护停止"
echo "============================================================"

echo "[1/3] 从 launchd 卸载服务 ..."
unload_service "$LAUNCH_AGENTS_DIR/$LABEL_API.plist"
unload_service "$LAUNCH_AGENTS_DIR/$LABEL_ENGINE.plist"
unload_service "$LAUNCH_AGENTS_DIR/$LABEL_FRONTEND.plist"

echo "[2/3] 清理残留端口进程 ..."
pkill -f '[e]ngine\.py' 2>/dev/null && echo "      停止 engine" || true
for p in "$PORT_API" "$PORT_FRONTEND"; do
  pids="$(port_pids "$p")"
  if [ -n "$pids" ]; then
    echo "$pids" | while read -r pid; do
      [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    echo "      释放端口 $p"
  fi
done
pkill -f '[n]ext-server' 2>/dev/null || true
sleep 1

echo "[3/3] 完成。"
echo "  重新启动：scripts/macos/start_all.sh"