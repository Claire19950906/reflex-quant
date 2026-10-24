# Trident v300 — 0 元上线指南

> **总成本：¥0**（不需要买域名）  
> **前提**：你 Mac 装 Python 3.11+、Node.js 20+、Homebrew

---

## 架构总览

```
投资人 → https://trident-portal.vercel.app/investor   (Vercel 免费)
              ↓ /api/* 代理
            https://trident-proxy.fly.dev              (Fly.io 免费)
              ↓ Cloudflare Tunnel (quick,免费)
            xxx.trycloudflare.com                       (临时 URL)
              ↓
            Mac 127.0.0.1:9001                          (master_api.py = 算法)
```

**4 个组件**：Vercel（前端）+ Fly.io（后端代理）+ Cloudflare Tunnel（暴露 Mac）+ Mac 上的 master API（算法）  
**全部免费**，每家都有免费层。

---

## 第一次部署（30 分钟）

### Step 1：本地准备（5 分钟）

```bash
# 1.1 装 cloudflared（如果你还没装）
brew install cloudflared

# 1.2 装 Vercel CLI
npm install -g vercel

# 1.3 装 Fly.io CLI
curl -L https://fly.io/install.sh | sh

# 1.4 注册 Vercel 账号（免费）
# → https://vercel.com/signup （用 GitHub 登录最快）

# 1.5 注册 Fly.io 账号（免费）
fly auth signup
```

---

### Step 2：填算法（1 分钟）

打开 `backend/src_python/master_api.py`，找到 `trident_compute_signal()`：

```python
def trident_compute_signal(symbol: str, params: Optional[dict] = None) -> dict:
    # ──────── 你填这里:调用你的算法 ────────
    raise NotImplementedError(...)
```

把 `raise NotImplementedError(...)` 删掉，改成你的算法调用，例如：

```python
from my_strategy import compute
return compute(symbol=symbol, **params or {})
```

---

### Step 3：本地测试（1 分钟）

```bash
cd /Users/claire/.cline/data/workspaces/chat/trident_v170
chmod +x scripts/*.sh
TRIDENT_MASTER_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))") \
  ./scripts/start_master.sh
```

**另开一个终端**：
```bash
curl -H "Authorization: Bearer $TRIDENT_MASTER_TOKEN" \
  http://127.0.0.1:9001/signal/BTCUSDT
```

**期望看到**：你的算法返回的 JSON。

如果返回 501 `algorithm_not_implemented` → 你忘了填算法，回到 Step 2。

---

### Step 4：部署前端到 Vercel（3 分钟）

```bash
cd /Users/claire/.cline/data/workspaces/chat/trident_v170
vercel login
vercel --prod
```

按提示选：scope（你的账号）→ 全部默认（直接回车）  
**部署完会给你一个 URL**，例如 `https://trident-portal-xxxx.vercel.app`

测试：浏览器打开 `https://你的url/investor`，应该看到 NDA 同意页。

---

### Step 5：部署后端代理到 Fly.io（5 分钟）

```bash
cd /Users/claire/.cline/data/workspaces/chat/trident_v170

# 5.1 设 master token（和本地要一致！）
export TRIDENT_MASTER_TOKEN="刚才 Step 3 生成的那个 token"
./scripts/deploy_flyio.sh
```

按提示：选 region（nrt 东京）→ 输个 app 名字（默认 trident-proxy）→ 等 2 分钟。

**部署完会给你一个 URL**，例如 `https://trident-proxy.fly.dev`

测试：
```bash
curl https://trident-proxy.fly.dev/api/master/health
# → {"ok": true, "service": "trident-proxy", ...}
```

但 `/api/signal/BTCUSDT` 还没法用 — 因为 Mac 上的 master 还没暴露。

---

### Step 6：暴露 Mac 上的 master（2 分钟）

**Mac 上另开一个终端**：
```bash
cd /Users/claire/.cline/data/workspaces/chat/trident_v170
TRIDENT_MASTER_TOKEN="和 Step 3 一样" ./scripts/start_cloudflared.sh
```

**等 3-5 秒**，会出现一行类似：
```
https://xxxx-xxxx-xxxx.trycloudflare.com
```

**复制这 URL**（不带末尾路径）。

---

### Step 7：把 cloudflared URL 告诉 Fly.io（1 分钟）

```bash
fly secrets set TRIDENT_MASTER_URL="https://xxxx-xxxx-xxxx.trycloudflare.com"
fly deploy  # 重新部署让 secrets 生效
```

**等 30 秒**让 Fly.io 重新拉起。

---

### Step 8：端到端测试（1 分钟）

**终端**：
```bash
# 测后端直接转发
curl https://trident-proxy.fly.dev/api/signal/BTCUSDT | head -c 500
# 期望:你的算法返回的 JSON

# 测前端 → 后端 → Mac
open https://你的vercel域名/investor
```

浏览器：
1. 输入名字 → 点 "我同意 NDA"
2. 输入 BTCUSDT → 点 "获取信号"
3. **期望看到**：你的算法的真实结果！

---

## 9. 之后每次开机

```bash
# 终端 A
TRIDENT_MASTER_TOKEN=... ./scripts/start_master.sh

# 终端 B
TRIDENT_MASTER_TOKEN=... ./scripts/start_cloudflared.sh
# → 看到 https://xxx-yyy-zzz.trycloudflare.com
# 复制到 Fly.io：
fly secrets set TRIDENT_MASTER_URL=https://xxx-yyy-zzz.trycloudflare.com
fly deploy
```

**问题**：cloudflared quick tunnel **重启会换 URL**。要长期固定：

### 方案 A（花 ¥80）：买 .com + 永久 tunnel
1. 在 Cloudflare Registrar 注册一个 `.com`（首年 ¥80）
2. `cloudflared tunnel login` → 一次性认证
3. `cloudflared tunnel create trident` → 拿 tunnel ID
4. `cloudflared tunnel route dns trident api.your-domain.com`
5. `cloudflared tunnel run trident` → 永久 URL！

### 方案 B（0 元，麻烦）：每次启动更新 Fly.io secrets
写个 launchd 脚本，启动 cloudflared 后自动 `fly secrets set ...`

### 方案 C（0 元，凑合用）：launchd 保持 cloudflared 不退出
Cloudflared quick tunnel 自身不重启就不会变。launchd keep-alive 即可。

---

## 故障排查

| 症状 | 原因 | 修复 |
|---|---|---|
| `algorithm_not_implemented` | 你没填 `trident_compute_signal()` | 回到 Step 2 |
| `master API timeout` | Mac 上 master 没跑 | 跑 Step 3 |
| `master API unreachable` | cloudflared 没起 / URL 没配 | 跑 Step 6-7 |
| `401 unauthorized` | Fly 和 Mac 的 token 不一致 | 重新 `fly secrets set TRIDENT_MASTER_TOKEN=...` |
| Fly.io 部署失败 | 网络问题 / 没登录 | `fly auth login` 再试 |
| Vercel 部署失败 | 没登录 | `vercel login` 再试 |
| 投资人打不开 investor 页面 | 浏览器没开 JS / 缓存 | Cmd+Shift+R 强刷 |

---

## 监控

- **Mac 端日志**：`tail -f ~/.trident/cloudflared.log`
- **Fly.io 日志**：`fly logs -a trident-proxy`
- **Vercel 日志**：Vercel dashboard → 你的项目 → Logs

---

## 安全清单 ✅

- [x] Fly.io 镜像不含算法（Dockerfile.proxy 不复制 master_api.py）
- [x] Mac 端只听 127.0.0.1（公网扫不到）
- [x] Fly ↔ Mac 用 HMAC token（防中间人）
- [x] 投资人页禁用右键 / 复制 / 选中文本
- [x] 每位投资人拿到专属水印 ID
- [x] demo 模式自动限流 + 数据脱敏 + 审计
- [x] Cloudflare Turnstile 反爬虫
- [x] v300 反爬虫 5 件套默认开

---

## 下一步

- [ ] 跑通基础流程（30 分钟）
- [ ] 给 1-2 个核心投资人试用
- [ ] 收集反馈
- [ ] 决定要不要买 .com（¥80 永久固定 URL）
- [ ] 上线会员机制（v301）
