# 10 分钟上手

两条路：**只看界面**（不需要任何 key）和**接自己的数据**（需要 key）。

---

## 路线 A · 只看界面（3 分钟）

```bash
git clone <repo> && cd <repo>
docker compose up
```

打开 <http://localhost:3000>。首次启动数据库是空的，页面会明确显示"无可统计样本"——**这是诚实状态，不是坏了**。想看有数据的界面，走路线 B。

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
