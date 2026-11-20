#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# =============================================================================
# Trident macOS 进程守护 —— 公共库
# 提供：路径探测 / LaunchAgent plist 生成 / launchctl 加载与卸载 / 端口健康检查
# 被 start_all.sh / stop_all.sh / restart_all.sh / status.sh source 使用
# =============================================================================

set -euo pipefail

# ---- 路径定位（本文件位于 scripts/macos/ 下，项目根为上两级）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_SRC="$ROOT_DIR/backend/src_python"
FRONTEND_DIR="$ROOT_DIR/frontend"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$SCRIPT_DIR/log"

# ---- 运行时探测 ----
# Python：优先 macOS 官方 3.12 framework 的 python3（与当前手动运行一致），否则回退 PATH
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

# Node（nvm 安装，绝对路径；launchd 默认 PATH 很窄，必须显式带上其目录）
NODE_BIN="$(command -v node || true)"
if [ -z "$NODE_BIN" ]; then
  echo "[ERROR] 未找到 node，请先安装 Node 18+（或确认 nvm 已加入 PATH）" >&2
  exit 1
fi
NODE_DIR="$(dirname "$NODE_BIN")"

# 供 LaunchAgent 使用的 PATH（含 nvm node 目录与常见系统目录）
SERVICE_PATH="$NODE_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 三个服务的 Label 与端口
LABEL_ENGINE="com.trident.engine"
LABEL_API="com.trident.api"
LABEL_FRONTEND="com.trident.frontend"
PORT_API="${API_PORT:-8000}"
PORT_FRONTEND="${FRONTEND_PORT:-3030}"
PORT_ENGINE_WEBHOOK="${TREE_NEWS_PORT:-9000}"   # engine 的 webhook 端口（仅探活用，不强制）

mkdir -p "$LOG_DIR" "$LAUNCH_AGENTS_DIR"

# ---- 生成三份 LaunchAgent plist（运行时用真实绝对路径回填）----
write_plists() {
  # 根因：项目在 ~/Desktop 受 TCC 保护，launchd chdir 到 Desktop 被拒(exit 78)。
  # 用 bash wrapper 先 cd 到 $HOME/Trident(英文 symlink) 再 exec；日志落 ~/.trident/log。
  TRIDENT_HOME="$HOME/.trident"; TRIDENT_WRAP="$TRIDENT_HOME/wrappers"; TRIDENT_LOG="$TRIDENT_HOME/log"; EN_LINK="$HOME/Trident"
  mkdir -p "$TRIDENT_WRAP" "$TRIDENT_LOG" "$LAUNCH_AGENTS_DIR"
  [ -e "$EN_LINK" ] || ln -s "$ROOT_DIR/.." "$EN_LINK" 2>/dev/null || true
  cat > "$TRIDENT_WRAP/run_engine.sh" <<EOF
#!/bin/bash
cd "$EN_LINK/strategy_package_v170/backend/src_python" || exit 1
exec "$PYTHON_BIN" engine.py
EOF
  cat > "$TRIDENT_WRAP/run_api.sh" <<EOF
#!/bin/bash
cd "$EN_LINK/strategy_package_v170/backend/src_python" || exit 1
exec "$PYTHON_BIN" api_server.py
EOF
  cat > "$TRIDENT_WRAP/run_frontend.sh" <<EOF
#!/bin/bash
cd "$EN_LINK/strategy_package_v170/frontend" || exit 1
exec "$NODE_BIN" node_modules/next/dist/bin/next dev --hostname 0.0.0.0 --port "$PORT_FRONTEND"
EOF
  chmod +x "$TRIDENT_WRAP"/run_*.sh

  cat > "$LAUNCH_AGENTS_DIR/$LABEL_ENGINE.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_ENGINE</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$TRIDENT_WRAP/run_engine.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TRIDENT_NO_AUTO_SETUP</key><string>1</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>$SERVICE_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$TRIDENT_LOG/engine.out.log</string>
  <key>StandardErrorPath</key><string>$TRIDENT_LOG/engine.err.log</string>
</dict>
</plist>
EOF

  cat > "$LAUNCH_AGENTS_DIR/$LABEL_API.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_API</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$TRIDENT_WRAP/run_api.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TRIDENT_NO_AUTO_SETUP</key><string>1</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>API_HOST</key><string>0.0.0.0</string>
    <key>API_PORT</key><string>$PORT_API</string>
    <key>PATH</key><string>$SERVICE_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$TRIDENT_LOG/api.out.log</string>
  <key>StandardErrorPath</key><string>$TRIDENT_LOG/api.err.log</string>
</dict>
</plist>
EOF

  cat > "$LAUNCH_AGENTS_DIR/$LABEL_FRONTEND.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_FRONTEND</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$TRIDENT_WRAP/run_frontend.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NEXT_TELEMETRY_DISABLED</key><string>1</string>
    <key>PATH</key><string>$SERVICE_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$TRIDENT_LOG/frontend.out.log</string>
  <key>StandardErrorPath</key><string>$TRIDENT_LOG/frontend.err.log</string>
</dict>
</plist>
EOF
}

# ---- launchctl 加载 / 卸载（新语法优先，老语法回退）----
launch_target() { echo "gui/$(id -u)"; }

load_service() {
  local plist="$1"
  local domain
  domain="$(launch_target)"
  if launchctl bootstrap "$domain" "$plist" 2>/dev/null; then
    echo "  [loaded] $(basename "$plist")"
  else
    # 已加载则 bootout 后重载（幂等）
    launchctl bootout "$domain" "$plist" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "$domain" "$plist" 2>/dev/null \
      || launchctl load "$plist" 2>/dev/null \
      || echo "  [warn] 无法加载 $(basename "$plist")"
  fi
}

unload_service() {
  local plist="$1"
  local domain
  domain="$(launch_target)"
  launchctl bootout "$domain" "$plist" 2>/dev/null \
    || launchctl unload "$plist" 2>/dev/null \
    || true
  echo "  [unloaded] $(basename "$plist")"
}

# ---- 端口探活：返回监听该端口的 pid（可能多个，换行分隔）----
port_pids() {
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

# ---- 健康检查：HTTP 200 即视为存活 ----
http_ok() {
  local url="$1"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || echo 000)"
  [ "$code" = "200" ]
}