#!/usr/bin/env bash
# 卸载 trident master launchd service
PLIST="$HOME/Library/LaunchAgents/com.trident.master.plist"
launchctl unload "$PLIST" 2>/dev/null && echo "已停止" || echo "未运行"
rm -f "$PLIST"
echo "✅ 已卸载"
