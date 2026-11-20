#!/bin/bash
# 低峰期维护：VACUUM 收缩 1.5GB 的库（43.8% 是空闲页，约 650MB 浪费）
#
# 为什么必须停机
#   · auto_vacuum=0，无法增量回收 —— 只能整库 VACUUM；
#   · VACUUM 对整库加排他锁，写入方全部排队/报错；
#   · 若用 VACUUM INTO 边跑边做，快照之后的写入会丢。
# 所以流程是：停写者 → VACUUM → 校验 → 拉起服务 → 体检。
#
# 用法: bash ops_vacuum.sh [--dry-run]
set -u
BACKEND="/Users/claire/Desktop/金融1.0/backend"
DB="$BACKEND/trident_event_bus.db"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG="/Users/claire/.trident/log/ops_vacuum.log"
# v454: api(8000) 已停用，全套路由由 bridge(8100) 托管，清单不再含 com.trident.api
SERVICES=(com.trident.engine com.trident.bridge com.trident.master)
UID_=$(id -u)

exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') VACUUM START ==="

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

restart_all() {
  for s in "${SERVICES[@]}"; do
    plist="$HOME/Library/LaunchAgents/$s.plist"
    if [ -f "$plist" ]; then
      launchctl bootstrap "gui/$UID_" "$plist" >/dev/null 2>&1 || \
        launchctl kickstart "gui/$UID_/$s" >/dev/null 2>&1
    fi
  done
  # master 起来后会托管 engine，等一会儿再看
  sleep 20
  if pgrep -f "src_python/engine.py" >/dev/null; then echo "  engine OK"; else
    echo "  engine 未起，兜底拉起"; bash /Users/claire/.trident/wrappers/run_engine.sh &
  fi
  code=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "http://127.0.0.1:8100/health")
  echo "  bridge(8100) -> $code"
}

if [ "$DRY" = "1" ]; then
  "$PY" - "$DB" <<'PYEOF'
import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1]); pc=c.execute("pragma page_count").fetchone()[0]
fl=c.execute("pragma freelist_count").fetchone()[0]
print(f"  当前 {os.path.getsize(sys.argv[1])/1e9:.2f}GB, 空闲 {fl}/{pc} 页 = {fl*4096/1e6:.0f}MB")
PYEOF
  echo "  [DRY-RUN] 不做任何停服/VACUUM"
  exit 0
fi

echo "[1/5] 停写服务"
for s in "${SERVICES[@]}"; do
  launchctl bootout "gui/$UID_/$s" >/dev/null 2>&1 || launchctl stop "$s" >/dev/null 2>&1
done
# 残留的 python 写者（engine/worker）也停掉，避免 VACUUM 被写锁挡住
pkill -f "src_python/engine.py" >/dev/null 2>&1 || true
sleep 5

echo "[2/5] 备份（保留最近一次，失败可回滚）"
BK="$DB.pre_vacuum"
[ -f "$BK" ] && rm -f "$BK"
cp -p "$DB" "$BK" && echo "  备份 -> $BK ($(du -h "$BK" | cut -f1))"

echo "[3/5] VACUUM"
T0=$(date +%s)
"$PY" - "$DB" <<'PYEOF'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1], timeout=60)
c.execute("PRAGMA busy_timeout=60000")
c.execute("VACUUM")
c.close()
PYEOF
RC=$?
T1=$(date +%s)
echo "  VACUUM rc=$RC 用时 $((T1-T0))s"
if [ $RC -ne 0 ]; then
  echo "  VACUUM 失败 → 回滚"
  cp -p "$BK" "$DB"
  restart_all
  echo "=== $(date '+%F %T') ABORTED ==="
  exit 1
fi

echo "[4/5] 校验"
"$PY" - "$DB" <<'PYEOF'
import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1])
r=c.execute("PRAGMA integrity_check").fetchone()[0]
pc=c.execute("pragma page_count").fetchone()[0]
fl=c.execute("pragma freelist_count").fetchone()[0]
print(f"  integrity={r} size={os.path.getsize(sys.argv[1])/1e9:.2f}GB 空闲页={fl}/{pc}")
print("  关键表:", c.execute("select count(*) from news_events").fetchone()[0],
      c.execute("select count(*) from ai_decisions").fetchone()[0],
      c.execute("select count(*) from critical_price_moves").fetchone()[0])
PYEOF

echo "[5/5] 拉起服务"
restart_all

echo "=== $(date '+%F %T') VACUUM DONE ==="
