#!/usr/bin/env bash
# Trident v300 — 在 Mac 上跑:启动 master API
# 用法: ./scripts/start_master.sh

set -e
cd "$(dirname "$0")/.."

# 1) 检查 token
if [ -z "$TRIDENT_MASTER_TOKEN" ]; then
    echo "⚠️  TRIDENT_MASTER_TOKEN 没设,使用默认 token (生产前必须改)"
    export TRIDENT_MASTER_TOKEN="trident-master-token-change-me-32bytes-min"
fi

# 2) 启动 master API
echo "=========================================="
echo "Trident Master API"
echo "  listen: 127.0.0.1:${TRIDENT_MASTER_PORT:-9001}"
echo "  token:  ${TRIDENT_MASTER_TOKEN:0:8}..."
echo "=========================================="
echo ""
echo "👉 另开一个终端,跑:"
echo "     ./scripts/start_cloudflared.sh"
echo ""
echo "   cloudflared 会给你一个 https://xxx.trycloudflare.com 临时 URL"
echo "   然后:fly secrets set TRIDENT_MASTER_URL=https://xxx.trycloudflare.com"
echo ""
echo "按 Ctrl+C 停止"
echo ""

cd backend/src_python
exec python3 master_api.py
