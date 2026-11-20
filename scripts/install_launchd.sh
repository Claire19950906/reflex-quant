#!/usr/bin/env bash
# Trident v300 — 装 launchd 开机自启
# 用法: sudo ./scripts/install_launchd.sh
# 之后: 开机自动起,完全脱离 Cline / Terminal

set -e
PLIST_NAME="com.trident.master"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$(which python3)"

# token 持久化
TOKEN_FILE="$HOME/.trident/master_token"
if [ ! -f "$TOKEN_FILE" ]; then
    mkdir -p "$HOME/.trident"
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "🔑 新 token 已生成: $(cat $TOKEN_FILE)"
else
    echo "🔑 用已有 token: $(cat $TOKEN_FILE | head -c 8)..."
fi

# 写 plist
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_NAME</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$PROJECT_ROOT/backend/src_python/master_api.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TRIDENT_MASTER_TOKEN</key>
    <string>$(cat $TOKEN_FILE)</string>
    <key>TRIDENT_MASTER_PORT</key>
    <string>9001</string>
    <key>TRIDENT_MASTER_HOST</key>
    <string>127.0.0.1</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>$PROJECT_ROOT/backend/src_python</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
    <key>Crashed</key>
    <true/>
  </dict>
  <key>StandardOutPath</key>
  <string>$HOME/.trident/master.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.trident/master.err</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF

# 装
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

echo ""
echo "✅ 已装好 launchd service: $PLIST_NAME"
echo ""
echo "操作:"
echo "  启动:   launchctl start $PLIST_NAME"
echo "  停止:   launchctl stop $PLIST_NAME"
echo "  状态:   launchctl list | grep trident"
echo "  日志:   tail -f $HOME/.trident/master.log"
echo "  卸载:   launchctl unload $PLIST_PATH && rm $PLIST_PATH"
echo ""
echo "🎉 master 现在完全脱离 Cline / Terminal,开机自启"
echo "🎉 Mac 重启也会自动起来"
echo ""
echo "下一步:在 Mac Terminal.app 测一下:"
echo "  TOKEN=\$(cat $TOKEN_FILE)"
echo "  curl -H \"Authorization: Bearer \$TOKEN\" http://127.0.0.1:9001/signal/BTCUSDT"
