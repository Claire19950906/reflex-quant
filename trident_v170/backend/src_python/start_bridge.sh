#!/usr/bin/env bash
# start_bridge.sh — 启动 trident_v170 桥接服务
# ===================================================
# 8000 端口:api_server.py 已在跑(PID 12420),不需要重启
# 8100 端口:本脚本启动的 bridge_server.py,提供 8000 缺失的 5 个端点
# 数据库:trident_event_bus.db (与 8000 共享,WAL 模式)
#
# 用法:
#   bash src_python/start_bridge.sh
#
# 日志:/tmp/bridge.log
# 停止:kill $(lsof -ti:8100)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
DB_PATH="$BACKEND_DIR/trident_event_bus.db"

cd "$BACKEND_DIR"

# 检查 DB
if [ ! -f "$DB_PATH" ]; then
  echo "[FATAL] DB not found: $DB_PATH"
  exit 1
fi

# 1) 先跑迁移(确保 6 张表都存在,schema 正确)
echo "[1/2] Running migrations..."
python3 src_python/migrate_missing_tables.py

# 2) 后台启动 bridge
echo "[2/2] Starting bridge on :8100..."
pkill -f bridge_server.py 2>/dev/null || true
sleep 1
nohup python3 src_python/bridge_server.py > /tmp/bridge.log 2>&1 &
disown
sleep 2

# 3) 验证
echo "=== health check ==="
curl -s -m 3 http://localhost:8100/api/health | python3 -m json.tool
echo
echo "=== quick endpoint smoke ==="
for ep in v237/missed-signals/by-dimension v291/causal-graph v291/attribution-stats; do
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://localhost:8100/api/$ep?asset=MU&days=90&hours=8760")
  echo "  $ep => $code"
done

echo
echo "[OK] bridge started, log: /tmp/bridge.log, port: 8100"
echo "     stop:  kill \$(lsof -ti:8100)"