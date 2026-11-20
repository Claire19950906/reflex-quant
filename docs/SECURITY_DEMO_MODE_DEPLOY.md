# Trident v300 — 体验模式部署手册

> 配套设计文档：[`SECURITY_DEMO_MODE.md`](./SECURITY_DEMO_MODE.md)。  
> 本文件只讲"怎么发出去"。

---

## 1. 三种发布场景

| 场景 | 推荐部署 | 镜像 | 源码 |
|---|---|---|---|
| A. 朋友内测 | Docker + Cloud Run / Fly.io | 体验镜像 | ❌ 不发 |
| B. 公开展示 | Vercel（前端）+ Modal / Lambda（后端） | 体验镜像 | ❌ 不发 |
| C. 自己玩 | 本机 | 直接 `python3 -m api_server` | ✅ 本地 |

**原则**：场景 A / B **永远不**把 `engine/` 或 `src_python/` 的完整包给体验者。
体验者拿到的就是「带 demo 中间件的 API + 带脱敏的前端」。

---

## 2. 环境变量（统一清单）

> 完整列表见 `backend/.env.example`，下面只列与体验模式相关的。

### 必填

```bash
TRIDENT_MODE=full                     # full | demo | licensed
```

### demo 模式

```bash
TRIDENT_DEMO_RPM=30                   # 每 IP 每 60s 最多 30 次
TRIDENT_DEMO_DAILY_BUDGET=5000        # 每 IP 每天最多 5000 次
TRIDENT_DEMO_DELAY_MS=200             # 注入 200ms 延迟
TRIDENT_DEMO_JITTER_MS=80             # ±80ms 抖动
TRIDENT_DEMO_HISTORY_DAYS=1           # 只返回最近 1 天
TRIDENT_DEMO_DECIMALS=2               # 数值截到 2 位小数
TRIDENT_AUDIT_DB=/var/lib/trident/trident_audit.db
```

### licensed 模式（除上面 + 鉴权）

```bash
TRIDENT_LICENSE_FILE=/etc/trident/license.lic
TRIDENT_LICENSE_SECRET=xxxxxxxxxxxx   # 32 字节随机；漏了直接拒加载
TRIDENT_API_KEY_STORE=/etc/trident/api_keys.json
TRIDENT_DEMO_RPM=120
TRIDENT_DEMO_DAILY_BUDGET=50000
TRIDENT_DEMO_DELAY_MS=50
TRIDENT_DEMO_HISTORY_DAYS=7
TRIDENT_DEMO_DECIMALS=4
```

### full 模式

```bash
TRIDENT_MODE=full                     # 上面变量全部忽略
TRIDENT_LISTEN_HOST=127.0.0.1         # 默认 127.0.0.1；改成 0.0.0.0 会告警
```

---

## 3. 场景 A：朋友内测（Docker + Fly.io）

### 3.1 生成签名密钥

```bash
export TRIDENT_LICENSE_SECRET=$(openssl rand -hex 32)
# 把这个写进 1Password / Bitwarden，**不要 commit**
```

### 3.2 签发 License

```bash
# 在你本机（admin）跑：
python3 scripts/gen_license.py issue \
    --tier pro --days 90 \
    --issued-to "bob@friend.com" \
    --secret "$TRIDENT_LICENSE_SECRET" \
    --out ./license-bob.lic

# 发给 bob（带密钥的许可证 + API Key 两条独立通道）
```

### 3.3 创建 API Key

```bash
python3 scripts/gen_api_key.py create \
    --name "bob-laptop" --tier pro --note "bob 内测" \
    --store ./api_keys.json
# 打印 trk_xxx → 单独发给 bob
```

### 3.4 构建体验镜像

```bash
# scripts/build_demo_image.sh（v300 配套）做的事：
#   1) 多阶段：builder 装依赖 + 编译 .pyc；runtime 只 COPY .pyc + 必要的 lib
#   2) 删除 /engine/*.py 中所有注释、docstring（保留字节码但不可读）
#   3) 设 ENV TRIDENT_MODE=demo
#   4) EXPOSE 8000
docker build -t trident-demo:v300 -f Dockerfile.demo .
docker push ghcr.io/yourname/trident-demo:v300
```

### 3.5 启动

```bash
fly launch --image ghcr.io/yourname/trident-demo:v300
fly secrets set \
    TRIDENT_LICENSE_SECRET=$TRIDENT_LICENSE_SECRET \
    TRIDENT_LICENSE_FILE=/secrets/license.lic \
    TRIDENT_API_KEY_STORE=/secrets/api_keys.json
fly secrets import <<< "$(cat ./license-bob.lic)"   # 或用 volume
```

### 3.6 验证

```bash
# 健康检查（含鉴权状态）
curl https://trident-demo.fly.dev/api/health/security | jq

# 朋友首次访问（带 API Key）：
curl -H "X-Trident-API-Key: trk_xxx" \
     https://trident-demo.fly.dev/api/v4/signal?symbol=BTCUSDT | jq
# → 应看到 X-Trident-Mode: licensed 头 + X-Trident-Watermark
```

---

## 4. 场景 B：公开展示（Vercel + Modal）

> 这种模式下只用 demo，不要发 license（避免给所有人分发）。
> 体验者拿不到 API Key，只能匿名 demo。

### 4.1 后端：Modal

```python
# modal_app.py
import modal
stub = modal.Stub("trident-demo")

@stub.function(
    image=modal.Image.debian_slim().pip_install_from_requirements("requirements.txt"),
    secrets=[modal.Secret.from_name("trident-demo")],
    keep_warm=1,
)
@modal.asgi()
def app():
    import os
    os.environ["TRIDENT_MODE"] = "demo"
    os.environ["TRIDENT_DEMO_RPM"] = "10"      # 公开再压一档
    os.environ["TRIDENT_DEMO_DAILY_BUDGET"] = "500"
    os.environ["TRIDENT_DEMO_DELAY_MS"] = "300"  # 公开更慢
    from api_server import app as fastapi_app
    return fastapi_app
```

```bash
modal deploy modal_app.py
# → https://yourname--trident-demo.modal.run
```

### 4.2 前端：Vercel

```bash
cd frontend
# .env.production
NEXT_PUBLIC_TRIDENT_API=https://yourname--trident-demo.modal.run
NEXT_PUBLIC_IS_DEMO=true

vercel --prod
```

### 4.3 注意事项

- 公开 demo **不要**接 `/api/admin` 或任何内部路由
- 检查 `CORSMiddleware` 的 `allow_origins` 限制为 `https://trident-demo.vercel.app`
- 公开 demo 的 `TRIDENT_DEMO_RPM=10` 改成你接受的峰值

---

## 5. 场景 C：本地开发（full）

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 默认就是 full，可省略：
# export TRIDENT_MODE=full

python3 -m api_server
# → 127.0.0.1:8000
# 启动横幅：TRIDENT_MODE=full | TRIDENT_LISTEN_HOST=127.0.0.1 | ✅ 安全
```

切到 demo 模式测一下：

```bash
TRIDENT_MODE=demo TRIDENT_DEMO_RPM=3 python3 -m api_server
# 启动横幅：TRIDENT_MODE=demo | RPM=3
# 第 4 次请求 → 429
```

---

## 6. 应急响应

### 发现有人在刷

1. 查 `trident_audit.db` 找 top 20 IP：

   ```bash
   sqlite3 /var/lib/trident/trident_audit.db \
       "SELECT client_ip, COUNT(*) c FROM audit
        WHERE ts > datetime('now','-1 day')
        GROUP BY client_ip ORDER BY c DESC LIMIT 20;"
   ```

2. 在反代（Cloudflare / Fly）拉黑这些 IP，**或者** 收紧 `TRIDENT_DEMO_RPM=5`
3. 如果是 licensed 模式：直接 revoke 对应 key：

   ```bash
   python3 scripts/gen_api_key.py revoke --name <泄漏的 key name> \
       --store /etc/trident/api_keys.json
   ```

4. 检查 `trident_audit.db` 里这个 key 调过哪些端点 → 评估暴露面

### 发现 License 泄漏

1. 改 `TRIDENT_LICENSE_SECRET`（所有 client 都要重新签发）
2. 旧 key 全部 revoke，重发新的
3. 检查 license 文件服务器访问日志

### demo 模式被深度爬

1. 收紧 `TRIDENT_DEMO_DECIMALS=0`（业务方先确认能接受）
2. 加 `TRIDENT_DEMO_HISTORY_DAYS=0`（只给当下，不给历史）
3. 上 Cloudflare Bot Fight Mode

---

## 7. 升级检查清单

每次发新版本前：

- [ ] `python3 -m pytest tests/test_demo_mode.py tests/test_license_auth.py` 全绿
- [ ] `git grep -nE "TRIDENT_(LICENSE_SECRET|MODE|DEMO_)"` 没把密钥 commit
- [ ] `TRIDENT_LICENSE_SECRET` 用 1Password / Doppler 注入，**不进** GitHub Actions log
- [ ] 体验镜像的 `Dockerfile.demo` 不 COPY `engine/` 源码目录
- [ ] `/api/health/security` 启动后状态正确
