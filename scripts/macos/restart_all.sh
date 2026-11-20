#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# Trident macOS 一键重启 = 停止 + 启动
# 用法：  scripts/macos/restart_all.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop_all.sh"
echo
"$SCRIPT_DIR/start_all.sh"