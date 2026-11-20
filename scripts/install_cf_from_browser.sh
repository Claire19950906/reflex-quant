#!/usr/bin/env bash
# Trident v300 — 浏览器下完 cloudflared 后跑这个
# 1) 自动找 ~/Downloads 里的 cloudflared 文件
# 2) 装到 /usr/local/bin/
# 3) 启动 tunnel,拿 URL
# 4) 写到 ~/.trident/master_url

set -e

DEST="/usr/local/bin/cloudflared"
URL_FILE="$HOME/.trident/master_url"
CF_LOG="$HOME/.trident/cloudflared.log"
mkdir -p "$HOME/.trident"

# ─── 0) 清掉之前的 HTML 垃圾 ───
if [ -f "$DEST" ]; then
    HEAD=$(head -c 50 "$DEST" 2>/dev/null || true)
    if echo "$HEAD" | grep -qiE "<!doctype|<html|404|not found"; then
        echo "🗑️  清掉 $DEST 里的 HTML 垃圾"
        sudo rm -f "$DEST"
    fi
fi

# ─── 1) 找 Downloads 里的 cloudflared 文件 ───
FOUND=$(find ~/Downloads -maxdepth 2 -type f -iname "cloudflared*" 2>/dev/null | head -5)
if [ -z "$FOUND" ]; then
    echo "❌ Downloads 里没找到 cloudflared"
    echo ""
    echo "👉 用浏览器下 (curl/Python 都不通,只有浏览器能):"
    echo "   1. 浏览器开 https://github.com/cloudflare/cloudflared/releases/latest"
    echo "   2. 找 'cloudflared-darwin-amd64' (raw binary,30MB,没扩展名)"
    echo "      ⚠️  不是 .tgz (压缩包) 也不是 .pkg (安装器)"
    echo "   3. 点文件名,浏览器会下到 ~/Downloads/"
    echo "   4. 下完再跑这个脚本"
    exit 1
fi

echo "📂 找到:"
echo "$FOUND"
echo ""

# 选最可能的(优先没扩展名的,然后 tgz,然后 pkg)
FILE=""
for f in $FOUND; do
    if [ -s "$f" ] && ! echo "$f" | grep -qE "\.(tgz|pkg|msi|zip|dmg|asc)$"; then
        FILE="$f"
        echo "✅ 选 raw binary: $FILE"
        break
    fi
done
if [ -z "$FILE" ]; then
    for f in $FOUND; do
        if echo "$f" | grep -qE "\.tgz$"; then
            FILE="$f"
            echo "✅ 选 .tgz 压缩包: $FILE"
            break
        fi
    done
fi
if [ -z "$FILE" ]; then
    FILE=$(echo "$FOUND" | head -1)
    echo "⚠️  默认用第一个: $FILE"
fi

# ─── 2) 装 ───
echo ""
ls -lh "$FILE"
echo ""

# 如果是 .tgz/.zip,先解压
cd "$(dirname "$FILE")"
if echo "$FILE" | grep -qE "\.tgz$"; then
    echo "📦 解压 .tgz ..."
    tar -xzf "$(basename "$FILE")"
elif echo "$FILE" | grep -qE "\.zip$"; then
    echo "📦 解压 .zip ..."
    unzip -o "$(basename "$FILE")"
fi

# 找解压后的 binary
if [ ! -f ./cloudflared ]; then
    echo "❌ 解压后没找到 cloudflared binary"
    exit 1
fi

# 验证是 binary 不是 HTML
HEAD=$(head -c 4 ./cloudflared 2>/dev/null | xxd -p)
if echo "$HEAD" | grep -qiE "3c21444f|3c68746d"; then
    echo "❌ 这文件是 HTML 垃圾,不是 binary"
    exit 1
fi

# 装
sudo mv ./cloudflared "$DEST"
sudo chmod +x "$DEST"
echo "✅ 装好: $($DEST --version 2>&1 | head -1)"

# ─── 3) 关掉旧 tunnel ───
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

# ─── 4) 启动 tunnel ───
echo ""
echo "🚀 启动 cloudflared tunnel..."
nohup cloudflared tunnel --url http://127.0.0.1:9001 --no-autoupdate > "$CF_LOG" 2>&1 &
CF_PID=$!
echo "$CF_PID" > "$HOME/.trident/cloudflared.pid"
echo "  PID=$CF_PID,日志: $CF_LOG"

# ─── 5) 等 URL ───
echo ""
echo "⏳ 等 trycloudflare.com URL (最多 30 秒)..."
URL=""
for i in {1..30}; do
    URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$CF_LOG" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then break; fi
    sleep 1
    echo -n "."
done
echo ""

if [ -z "$URL" ]; then
    echo ""
    echo "❌ 30 秒没拿到 URL,日志尾部:"
    tail -30 "$CF_LOG"
    exit 1
fi

echo "$URL" > "$URL_FILE"
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "✅ 你的公网 master URL:"
echo ""
echo "   $URL"
echo ""
echo "   已存到: $URL_FILE"
echo "   进程 PID: $CF_PID"
echo "   日志: $CF_LOG"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "👉 把上面那行 URL 复制给 Cline(我帮你配到 Fly.io)"
