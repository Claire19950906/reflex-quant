#!/usr/bin/env bash
# Trident v300 — 在 Mac 上跑:启动 cloudflared quick tunnel
# 暴露 127.0.0.1:9001 → https://xxx.trycloudflare.com

set -e
PORT="${TRIDENT_MASTER_PORT:-9001}"
LOG="$HOME/.trident/cloudflared.log"
URL_FILE="$HOME/.trident/master_url.txt"

mkdir -p "$HOME/.trident"

# 安装 cloudflared
if ! command -v cloudflared >/dev/null 2>&1; then
    echo "Installing cloudflared..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install cloudflared
    else
        echo "请先装 cloudflared: https://github.com/cloudflare/cloudflared/releases"
        exit 1
    fi
fi

echo "Starting cloudflared quick tunnel on port $PORT..."
echo ""
echo "  📋 复制下面出现的 https://xxx.trycloudflare.com URL"
echo "  📋 跑:  fly secrets set TRIDENT_MASTER_URL=刚才那个URL"
echo ""
echo "日志写入 $LOG"
echo ""

# 启动 + 实时打印 URL + 自动写入文件
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate 2>&1 | tee "$LOG" | (
    while IFS= read -r line; do
        echo "$line"
        url=$(echo "$line" | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1)
        if [ -n "$url" ] && [ ! -f "$URL_FILE" ]; then
            echo "$url" > "$URL_FILE"
            echo ""
            echo "✅ URL 已保存到 $URL_FILE"
            echo "👉 现在跑:  fly secrets set TRIDENT_MASTER_URL=$url"
        fi
    done
)
