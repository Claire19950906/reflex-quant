#!/usr/bin/env bash
# Trident v300 — 一键 cloudflared (下载 + 安装 + 启动 + 显示 URL)
# 你只跑这一条,什么都不用懂

set -e

DEST="/usr/local/bin/cloudflared"
URL_FILE="$HOME/.trident/master_url"
CF_LOG="$HOME/.trident/cloudflared.log"
mkdir -p "$HOME/.trident"

# ─── 0) 自动检测代理 (Clash / 系统代理) ───
echo "🔍 探测代理端口..."
PROXY_PORT=""
for port in 7890 7891 7897 10809 1080 8888 8080 1087 7892 10808; do
    if lsof -i :$port -sTCP:LISTEN >/dev/null 2>&1; then
        PROXY_PORT=$port
        break
    fi
done
if [ -n "$PROXY_PORT" ]; then
    export http_proxy="http://127.0.0.1:$PROXY_PORT"
    export https_proxy="http://127.0.0.1:$PROXY_PORT"
    export all_proxy="http://127.0.0.1:$PROXY_PORT"
    echo "  ✅ 代理端口 $PROXY_PORT 已配"
else
    echo "  ⚠️  没探测到代理,继续直连(可能下不了)"
    echo "  👉 启动 Clash Verge / 其它代理后重跑"
fi
echo ""


# ─── 1) 下载 cloudflared (多源 fallback) ───
# 先清掉之前的 HTML 垃圾
if [ -f "$DEST" ]; then
    HEAD=$(head -c 50 "$DEST" 2>/dev/null || true)
    if echo "$HEAD" | grep -qiE "<!doctype|<html|404|not found"; then
        echo "🗑️  清掉 $DEST 里的 HTML 垃圾"
        sudo rm -f "$DEST"
    fi
fi

# 找 ~/Downloads 下的文件
if [ ! -x "$DEST" ]; then
    DLF=$(find ~/Downloads -maxdepth 2 -iname "cloudflared*" 2>/dev/null | head -3)
    if [ -n "$DLF" ]; then
        echo "📂 Downloads 里找到:"
        echo "$DLF"
        F=$(echo "$DLF" | head -1)
        cd "$(dirname "$F")"
        if echo "$F" | grep -qE "\.tgz$"; then tar -xzf "$(basename "$F")"; fi
        if echo "$F" | grep -qE "\.zip$"; then unzip -o "$(basename "$F")"; fi
        if [ -f ./cloudflared ]; then
            sudo mv ./cloudflared "$DEST"
            sudo chmod +x "$DEST"
            echo "✅ 从 Downloads 装好"
        fi
    fi
fi

# 多源 Python 下载
if [ ! -x "$DEST" ]; then
    echo "🔍 多源下载 cloudflared..."
    python3 << 'PYEOF'
import urllib.request, os, sys, json

def is_bad(data):
    h = data[:200].lower()
    if b'<!doctype' in h or b'<html' in h or b'404' in h or b'not found' in h:
        return True
    if data[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                     b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                     b'\xca\xfe\xba\xbe', b'\x7fELF'):
        return False
    return sum(1 for b in data[:200] if 32 <= b < 127) > 150

SOURCES = [
    ('GitHub API', 'api'),
    ('ghproxy.com',       'https://ghproxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64'),
    ('mirror.ghproxy',    'https://mirror.ghproxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64'),
    ('gh-proxy.com',      'https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64'),
    ('jsdelivr',          'https://cdn.jsdelivr.net/gh/cloudflare/cloudflared@latest/cmd/cloudflared/cloudflared-darwin-amd64'),
    ('GitHub 直链',       'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64'),
]

for name, url in SOURCES:
    print(f'  {name}...', end=' ', flush=True)
    try:
        if url == 'api':
            api = 'https://api.github.com/repos/cloudflare/cloudflared/releases/latest'
            req = urllib.request.Request(api, headers={'User-Agent': 'Mozilla/5.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
            for asset in data.get('assets', []):
                if 'darwin-amd64' in asset['name'] and not asset['name'].endswith(('.tgz','.zip','.pkg','.msi','.asc')):
                    url = asset['browser_download_url']
                    break
            else:
                print('❌ 没找到 amd64')
                continue

        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = urllib.request.urlopen(req, timeout=120).read()

        if is_bad(data):
            print(f'❌ HTML ({len(data)} bytes)')
            continue

        open('/tmp/cloudflared', 'wb').write(data)
        os.chmod('/tmp/cloudflared', 0o755)
        print(f'✅ ({len(data)//1024} KB)')
        sys.exit(0)
    except Exception as e:
        print(f'❌ {type(e).__name__}')

print('  ❌ 所有源失败', file=sys.stderr)
print('👉 浏览器手动下:', file=sys.stderr)
print('   1. 开 https://github.com/cloudflare/cloudflared/releases/latest', file=sys.stderr)
print('   2. 找 cloudflared-darwin-amd64 (raw binary,不要 .tgz/.pkg)', file=sys.stderr)
print('   3. 浏览器下到 ~/Downloads', file=sys.stderr)
print('   4. 再跑这个脚本', file=sys.stderr)
sys.exit(1)
PYEOF
    sudo mv /tmp/cloudflared "$DEST"
    sudo chmod +x "$DEST"
    echo ""
    echo "✅ 装好: $($DEST --version 2>&1 | head -1)"
fi

# ─── 2) 关掉旧 tunnel (如果跑着) ───
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

# ─── 3) 启动 tunnel (后台) ───
echo ""
echo "🚀 启动 cloudflared tunnel..."
nohup cloudflared tunnel --url http://127.0.0.1:9001 --no-autoupdate > "$CF_LOG" 2>&1 &
CF_PID=$!
echo "$CF_PID" > "$HOME/.trident/cloudflared.pid"
echo "  PID=$CF_PID,日志: $CF_LOG"

# ─── 4) 等 URL 出现 (最多 30 秒) ───
echo ""
echo "⏳ 等 trycloudflare.com URL 出现..."
URL=""
for i in {1..30}; do
    URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$CF_LOG" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then break; fi
    sleep 1
    echo -n "."
done
echo ""

if [ -z "$URL" ]; then
    echo "❌ 30 秒没拿到 URL,看日志:"
    tail -30 "$CF_LOG"
    exit 1
fi

# ─── 5) 保存 + 显示 ───
echo "$URL" > "$URL_FILE"
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "✅ 你的公网 master URL:"
echo "   $URL"
echo ""
echo "   已存到: $URL_FILE (之后脚本可读)"
echo "   进程 PID: $CF_PID"
echo "   日志: $CF_LOG"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "👉 现在:把上面那行 URL 复制给 Cline(我帮你配到 Fly.io)"
echo ""
echo "🛑 想关:  kill $CF_PID"
echo "📊 看日志:  tail -f $CF_LOG"
