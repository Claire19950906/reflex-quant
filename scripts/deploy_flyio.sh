#!/usr/bin/env bash
# Trident v300 — 一键部署到 Fly.io (首次 / 重新部署)
# 用法: ./scripts/deploy_flyio.sh

set -e
cd "$(dirname "$0")/.."

# 1) 检查 flyctl
if ! command -v fly >/dev/null 2>&1; then
    echo "❌ flyctl 没装"
    echo "👉 安装: curl -L https://fly.io/install.sh | sh"
    echo "👉 登录: fly auth signup (免费账号)"
    exit 1
fi

# 2) 检查 token(没设就生成)
if [ -z "$TRIDENT_MASTER_TOKEN" ]; then
    NEW_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo "🔑 生成 master token: $NEW_TOKEN"
    echo "⚠️  这个 token 必须和 Mac 上的 master_api.py 一致!"
    echo "   在 Mac 上跑: export TRIDENT_MASTER_TOKEN=$NEW_TOKEN"
    echo ""
    fly secrets set TRIDENT_MASTER_TOKEN="$NEW_TOKEN"
else
    fly secrets set TRIDENT_MASTER_TOKEN="$TRIDENT_MASTER_TOKEN"
fi

# 3) 部署
echo ""
echo "🚀 部署到 Fly.io..."
fly deploy --ha=false

# 4) 显示 URL
echo ""
echo "✅ 部署完成!"
echo ""
echo "👉 Mac 上跑 cloudflared,拿 URL 后:"
echo "   fly secrets set TRIDENT_MASTER_URL=https://xxx.trycloudflare.com"
echo "   fly deploy  # 重新部署让 secrets 生效"
echo ""
echo "👉 然后打开:"
fly status
