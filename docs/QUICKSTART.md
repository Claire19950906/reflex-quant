# 10 分钟上手

两条路：**只看界面**（不需要任何 key）和**接自己的数据**（需要 key）。

---

## 路线 A · 只看界面（3 分钟，不用 docker、不用任何 key）

仓库自带合成演示库 `backend/demo.db`（400 天 K 线、48 条新闻与决策、3 笔纸面持仓），
clone 下来就能看到有数据的界面。新闻与决策都带 `[DEMO]` 前缀，不会和真实库混淆。

```bash
# 终端 1：后端（:8000）
cd backend
pip install -r requirements.txt
TRIDENT_OFFLINE=1 TRIDENT_DB_PATH="$PWD/demo.db" python3 src_python/api_server.py

# 终端 2：前端（:3000）
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 npx next dev -p 3000
```

打开 <http://localhost:3000>。归因页的分钟级 K 线要另一条后端，再开一个终端：
`cd backend && TRIDENT_OFFLINE=1 TRIDENT_DB_PATH="$PWD/demo.db" python3 src_python/bridge_server.py`（:8100）。

想接自己的库：把 `TRIDENT_DB_PATH` 换成 `./backend/trident_event_bus.db`。
想重新生成演示库：`cd backend && python3 scripts/make_demo_db.py --force`。

> **别把 `docker compose up` 当第一步。** `docker-compose.yml` 和两个 Dockerfile 是
> 迁容器的**骨架**，还没逐项验证：前端镜像的 `NEXT_PUBLIC_API_URL` 要在 `npm run build`
> 阶段注入（运行时再传 env 不生效），compose 里也没有 :8000 的 `api_server`。
> 先用上面两条命令跑通，再考虑容器化。

---

## 路线 B · 接自己的数据（10 分钟）

### 1. 环境（2 分钟）

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 所有字段可留空，缺什么降什么级
```

### 2. 起后端（3 分钟）

```bash
python3 src_python/api_server.py     # :8000
# 需要归因页分钟级 K 线时，再起一个：
python3 src_python/bridge_server.py   # :8100
```

### 3. 起前端（2 分钟）

```bash
cd ../frontend
npm install
npm run dev                          # :3000
```

### 4. 填最少的三样（3 分钟）

只想要"每天早报"，填这三个就够：

| 变量 | 作用 | 不填会怎样 |
|---|---|---|
| `SERVERCHAN_SENDKEY` 或 `PUSHPLUS_TOKEN` | 微信推送 | 不推，摘要仍写日志 |
| 任意一个大模型 Key（`DEEPSEEK_API_KEY` 等） | 新闻分析 | 分析层跳过，页面仍可看行情 |
| `FRED_API_KEY`（免费） | 宏观序列 | 宏观面板空，其余不受影响 |

### 5. 验一遍

```bash
cd backend && python3 scripts/iteration_check.py     # 40 秒，三行结论
```

期望看到形如：`闭环 27/27 · 能力 26/26 · 结论：可运行，7 项等样本`。

---

## 常见卡点

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面全是"—" | 库是空的，没有样本 | 等数据进来，或看 `docs/FAQ.md` |
| 归因页 K 线空白 | 连了 8000 而不是 8100 | 见 `frontend/.env.local` 注释里的分流规则 |
| `database is locked` | 多个进程抢写 | 正常，`TRIDENT_BUSY_TIMEOUT_MS` 调大 |
| 早报没收到 | 推送 key 没填 / 被闸门去重 | 查 `cron_discovery_digest.log` |
