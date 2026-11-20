#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# Trident macOS 守护状态查看
# 用法：  scripts/macos/status.sh

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

echo "==================== launchd 服务状态 ===================="
for label in "$LABEL_ENGINE" "$LABEL_API" "$LABEL_FRONTEND"; do
  line="$(launchctl list 2>/dev/null | grep -w "$label" || true)"
  if [ -n "$line" ]; then
    # launchctl list 输出列：PID  状态码  Label
    pid="$(echo "$line" | awk '{print $1}')"
    code="$(echo "$line" | awk '{print $2}')"
    if [ "$pid" != "-" ] && [ -n "$pid" ]; then
      echo "  ✅ $label   pid=$pid"
    else
      echo "  ⚠️  $label   已注册但未运行（LastExitCode=$code）"
    fi
  else
    echo "  ❌ $label   未注册"
  fi
done

echo
echo "==================== 端口监听 ===================="
for p in "$PORT_API" "$PORT_FRONTEND" "$PORT_ENGINE_WEBHOOK"; do
  pids="$(port_pids "$p")"
  if [ -n "$pids" ]; then
    echo "  ✅ :$p   pid=$(echo "$pids" | tr '\n' ' ')"
  else
    echo "  ❌ :$p   未监听"
  fi
done

echo
echo "==================== HTTP 探活 ===================="
http_ok "http://127.0.0.1:$PORT_API/api/health" \
  && echo "  ✅ API /api/health = 200" \
  || echo "  ❌ API /api/health 无响应"
http_ok "http://127.0.0.1:$PORT_FRONTEND/" \
  && echo "  ✅ 前端 / = 200" \
  || echo "  ❌ 前端无响应"

echo
echo "日志目录：$LOG_DIR"

# 若有异常，给出最近错误日志片段
echo
echo "==================== 最近错误日志（各文件尾部 5 行）===================="
for f in engine api frontend; do
  if [ -s "$LOG_DIR/$f.err.log" ]; then
    echo "—— $f.err.log ——"
    tail -n 5 "$LOG_DIR/$f.err.log" | sed 's/^/    /'
  fi
done