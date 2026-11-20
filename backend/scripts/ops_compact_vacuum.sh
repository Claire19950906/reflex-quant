#!/bin/bash
# 低峰期整库 VACUUM：回收 auto_vacuum=none 留下的空闲页（当前约 220MB / 20.8%）
#
# 流程：停主服务 + 采集/写入 worker → 备份 → VACUUM → 完整性校验 → 拉起全部服务
# 任一阶段失败自动回滚备份并拉起服务。
#
# 用法:
#   bash ops_compact_vacuum.sh --dry-run   # 只体检，不停服
#   bash ops_compact_vacuum.sh             # 执行停机 VACUUM
set -u

BACKEND="/Users/claire/Desktop/金融1.0/backend"
DB="$BACKEND/trident_event_bus.db"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LA="$HOME/Library/LaunchAgents"
LOG="/Users/claire/.trident/log/ops_vacuum.log"
UID_=$(id -u)

# 常驻主服务（KeepAlive，需先停，避免拉起采集/写库子进程）
# v454: api(8000) 已停用，全套路由由 bridge(8100) 托管，清单不再含 com.trident.api
MAIN_SERVICES=(com.trident.engine com.trident.bridge com.trident.master)

exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') COMPACT VACUUM START ==="

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { echo "  [$(date '+%T')] $*"; }

count_write_procs() {
  pgrep -f "src_python/" | grep -v $$ | wc -l | tr -d ' '
}

if [ "$DRY" = "1" ]; then
  "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys, os
c = sqlite3.connect(sys.argv[1])
pc = c.execute("pragma page_count").fetchone()[0]
fl = c.execute("pragma freelist_count").fetchone()[0]
ps = c.execute("pragma page_size").fetchone()[0]
print(f"  当前 {os.path.getsize(sys.argv[1])/1e9:.2f}GB, 空闲 {fl}/{pc} 页 = {fl*ps/1e6:.0f}MB ({fl/pc*100:.1f}%)")
c.close()
PYEOF
  echo "  [DRY-RUN] 不做任何停服/VACUUM"
  echo "=== $(date '+%F %T') ABORTED DRY-RUN ==="
  exit 0
fi

# ── 1) 停主服务 ────────────────────────────────────────────────
echo "[1/6] 停主服务"
for s in "${MAIN_SERVICES[@]}"; do
  launchctl bootout "gui/$UID_/$s" >/dev/null 2>&1 || launchctl stop "$s" >/dev/null 2>&1
  log "bootout $s"
done
sleep 5

# ── 2) 停残留采集/写入 worker（cron 触发后正在跑的 python）────────
echo "[2/6] 停残留采集/写入 worker"
pkill -f "src_python/engine.py" >/dev/null 2>&1 || true
pkill -f "src_python/" >/dev/null 2>&1 || true
sleep 3
left=$(count_write_procs)
log "残留 src_python 进程: $left"
# 再补一轮，等待退出
for i in 1 2 3; do
  left=$(count_write_procs)
  [ "$left" = "0" ] && break
  pkill -f "src_python/" >/dev/null 2>&1 || true
  sleep 2
done
left=$(count_write_procs)
log "二次确认残留: $left"

# ── 3) 备份（保留最近一次，失败可回滚）──────────────────────────
echo "[3/6] 备份"
BK="$DB.pre_vacuum_$(date +%Y%m%d_%H%M%S)"
cp -p "$DB" "$BK" && log "备份 -> $(basename "$BK") ($(du -h "$BK" | cut -f1))"

# 清掉 WAL/SHM，让 VACUUM 基于单文件
rm -f "$DB-wal" "$DB-shm"

# ── 4) VACUUM ─────────────────────────────────────────────────
echo "[4/6] VACUUM"
T0=$(date +%s)
"$PY" - "$DB" <<'PYEOF'
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], timeout=60)
c.execute("PRAGMA busy_timeout=60000")
t0 = time.time()
c.execute("VACUUM")
c.close()
print(f"  VACUUM 用时 {time.time()-t0:.1f}s")
PYEOF
RC=$?
T1=$(date +%s)
log "VACUUM rc=$RC 用时 $((T1-T0))s"

restart_all() {
  echo "[拉起] 主服务"
  for s in "${MAIN_SERVICES[@]}"; do
    plist="$LA/$s.plist"
    if [ -f "$plist" ]; then
      launchctl bootstrap "gui/$UID_" "$plist" >/dev/null 2>&1 || launchctl kickstart "gui/$UID_/$s" >/dev/null 2>&1
    fi
  done
  # cron 由 launchd 按各自间隔自然拉起，无需手动 bootstrap
  sleep 20
  pgrep -f "src_python/engine.py" >/dev/null 2>&1 && log "engine OK" || log "engine 未起"
  code=$(curl -s -m 8 -o /dev/null -w "%{http_code}" "http://127.0.0.1:8100/health")
  log "bridge(8100) -> $code"
}

if [ $RC -ne 0 ]; then
  echo "  VACUUM 失败 → 回滚"
  rm -f "$DB" "$DB-wal" "$DB-shm"
  cp -p "$BK" "$DB"
  restart_all
  echo "=== $(date '+%F %T') ABORTED VACUUM FAILED ==="
  exit 1
fi

# ── 5) 校验 ───────────────────────────────────────────────────
echo "[5/6] 校验"
"$PY" - "$DB" <<'PYEOF'
import sqlite3, sys, os
c = sqlite3.connect(sys.argv[1])
r = c.execute("PRAGMA integrity_check").fetchone()[0]
pc = c.execute("pragma page_count").fetchone()[0]
fl = c.execute("pragma freelist_count").fetchone()[0]
print(f"  integrity={r} size={os.path.getsize(sys.argv[1])/1e9:.2f}GB 空闲页={fl}/{pc}")
for t in ("news_events", "critical_price_moves", "macro_snapshots"):
    try:
        print(f"  {t}: {c.execute(f'SELECT COUNT(*) FROM \"{t}\"').fetchone()[0]}")
    except Exception as e:
        print(f"  {t}: ({e})")
c.close()
PYEOF
CHK=$?

if [ $CHK -ne 0 ]; then
  echo "  校验失败 → 回滚"
  rm -f "$DB" "$DB-wal" "$DB-shm"
  cp -p "$BK" "$DB"
  restart_all
  echo "=== $(date '+%F %T') ABORTED CHECK FAILED ==="
  exit 1
fi

# ── 6) 拉起服务 ───────────────────────────────────────────────
echo "[6/6] 拉起服务"
restart_all

# 只保留最近两次备份
ls -1t "$DB".pre_vacuum_* 2>/dev/null | tail -n +3 | xargs -r rm -f

echo "=== $(date '+%F %T') COMPACT VACUUM DONE ==="
