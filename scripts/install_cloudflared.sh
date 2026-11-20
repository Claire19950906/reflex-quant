#!/usr/bin/env bash
# Trident v300 — 装 cloudflared (Mac 自动检测架构,带镜像 fallback)
set -e
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64"
else
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64"
fi
DEST="/usr/local/bin/cloudflared"
echo "检测到架构: $ARCH"
echo "尝试 1: 官方 GitHub ..."
if curl -fsSL --connect-timeout 15 --max-time 60 "$URL" -o "$DEST.tmp" 2>/dev/null; then
    mv "$DEST.tmp" "$DEST"
else
    echo "  失败,用 ghproxy 镜像 ..."
    MIRROR="https://ghproxy.com/$URL"
    if curl -fL --connect-timeout 15 --max-time 90 "$MIRROR" -o "$DEST.tmp" 2>/dev/null; then
        mv "$DEST.tmp" "$DEST"
    else
        echo "  镜像也失败,试 npmmirror ..."
        MIRROR2="https://registry.npmmirror.com/-/binary/cloudflared/latest/cloudflared-darwin-${ARCH//x86_64/amd64}.tgz"
        if curl -fL --connect-timeout 15 --max-time 90 "$MIRROR2" -o /tmp/cf.tgz 2>/dev/null; then
            tar -xzf /tmp/cf.tgz -C /tmp/
            mv /tmp/cloudflared "$DEST"
        else
            echo "❌ 全部失败 — 检查网络 / 代理 / VPN"
            exit 1
        fi
    fi
fi
chmod +x "$DEST"
echo ""
echo "✅ 已装到 $DEST"
echo "版本: $(cloudflared --version 2>&1)"
