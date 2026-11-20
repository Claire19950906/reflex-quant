#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# =============================================================================
# Trident macOS 一键启动（LaunchAgent 守护版）
# 用法：  scripts/macos/start_all.sh
# 效果：  engine / api / 前端 三个服务交给 launchd 守护，崩溃自动拉起，
#         不再依赖手动 nohup 窗口，彻底消除 127.0.0.1 拒绝连接。
# =============================================================================

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

echo "============================================================"
echo " Trident macOS 守护启动"
echo " 根目录   : $ROOT_DIR"
echo " Python   : $PYTHON_BIN"
echo " Node     : $NODE_BIN"
echo " 端口     : api=$PORT_API  前端=$PORT_FRONTEND  engine_webhook=$PORT_ENGINE_WEBHOOK"
echo "============================================================"

# 1) 先接管端口：清掉可能残留的旧手动 nohup 进程（否则新服务端口冲突起不来）
echo "[1/4] 清理旧的手动进程/残留端口 ..."
#    engine（按命令行精确匹配）
pkill -f '[e]ngine\.py' 2>/dev/null && echo "      已停止旧 engine" || true
#    api（8000）与前端（3030）
for p in "$PORT_API" "$PORT_FRONTEND"; do
  pids="$(port_pids "$p")"
  if [ -n "$pids" ]; then
    echo "$pids" | while read -r pid; do
      [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    echo "      已释放端口 $p"
  fi
done
#    兜底：next 相关残留
pkill -f '[n]ext-server' 2>/dev/null || true
sleep 1

# 2) 生成 plist（用真实绝对路径回填）
echo "[2/4] 生成 LaunchAgent plist -> $LAUNCH_AGENTS_DIR"
write_plists
echo "      engine / api / frontend 三份 plist 已就绪"

# 3) 加载三个服务（幂等）
echo "[3/4] 注册到 launchd ..."
load_service "$LAUNCH_AGENTS_DIR/$LABEL_API.plist"
load_service "$LAUNCH_AGENTS_DIR/$LABEL_ENGINE.plist"
load_service "$LAUNCH_AGENTS_DIR/$LABEL_FRONTEND.plist"

# 4) 健康检查（最多等待 45 秒，端口就绪即通过）
echo "[4/4] 健康检查（最多等待 45s）..."
api_ok=0; fe_ok=0
for _ in $(seq 1 15); do
  [ "$api_ok" = "0" ] && http_ok "http://127.0.0.1:$PORT_API/api/health" && api_ok=1
  [ "$fe_ok" = "0" ] && http_ok "http://127.0.0.1:$PORT_FRONTEND/" && fe_ok=1
  [ "$api_ok" = "1" ] && [ "$fe_ok" = "1" ] && break
  sleep 3
done

echo
echo "------------------------------------------------------------"
echo " 结果："
echo "   API (:$PORT_API)     $([ "$api_ok" = "1" ] && echo '✅ 在线' || echo '❌ 未就绪（见 log/api.err.log）')"
echo "   前端 (:$PORT_FRONTEND) $([ "$fe_ok" = "1" ] && echo '✅ 在线' || echo '❌ 未就绪（见 log/frontend.err.log）')"
echo "   engine 守护          $([ -n "$(port_pids "$PORT_ENGINE_WEBHOOK")" ] && echo '✅ webhook 监听中' || echo '⏳ 已注册（见 log/engine.err.log）')"
echo "------------------------------------------------------------"
echo " 已由 launchd 守护：进程退出会自动拉起。"
echo " 停止：scripts/macos/stop_all.sh   状态：scripts/macos/status.sh"
echo " 日志：$LOG_DIR"