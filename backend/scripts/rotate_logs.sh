#!/bin/bash
# [已被取代 2026-11-19] 本脚本是旧版日志轮转（archive/ 子目录、保留 7 份、20MB 阈值）。
# 现行机制是 ~/.trident/wrappers/run_log_rotate.sh（代际 .0/.1/.2、保留 3 份、80MB 阈值，
# 由 com.trident.log-rotate.plist 每日 03:40 触发）。旧作业 com.trident.logrotate 已卸载归档，
# 本文件仅留作回溯，不要再挂 launchd 或手动启用；如需调整轮转策略，改新版 wrapper。
#
# 日志轮转（copytruncate 模式，不需要 sudo，不打断正在写的进程）
#
# 背景：~/.trident/log/api.out.log 已 79MB、api.err.log 13MB，无任何轮转。
# 做法：超过阈值 → 复制一份到 archive/ 并 gzip，然后把原文件截断为 0。
#      launchd 进程持有的文件描述符不变，继续写同一个 inode，因此不会丢日志。
# 用法：由 com.trident.logrotate.plist 每日触发，也可手动执行。

set -u
LOG_DIR="${TRIDENT_LOG_DIR:-$HOME/.trident/log}"
ARCHIVE="$LOG_DIR/archive"
MAX_MB="${TRIDENT_LOG_MAX_MB:-20}"
KEEP="${TRIDENT_LOG_KEEP:-7}"

mkdir -p "$ARCHIVE"
STAMP=$(date +%Y%m%d-%H%M%S)
rotated=0

for f in "$LOG_DIR"/*.log; do
  [ -e "$f" ] || continue
  size=$(stat -f%z "$f" 2>/dev/null || echo 0)
  if [ "$size" -ge $((MAX_MB * 1024 * 1024)) ]; then
    base=$(basename "$f" .log)
    cp "$f" "$ARCHIVE/${base}-${STAMP}.log" 2>/dev/null
    : > "$f"                     # 截断，保持 inode 不变
    gzip -9 "$ARCHIVE/${base}-${STAMP}.log" 2>/dev/null
    echo "[rotate] ${base}.log ${size} bytes -> ${base}-${STAMP}.log.gz"
    rotated=$((rotated + 1))
  fi
done

# 只保留最近 KEEP 份归档
ls -1t "$ARCHIVE"/*.log.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -f "$old"
  echo "[rotate] 清理旧归档 $(basename "$old")"
done

echo "[rotate] 完成，本轮轮转 ${rotated} 个文件；当前目录占用 $(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)"
