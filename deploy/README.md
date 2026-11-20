# Trident Agent MVP — Linux 部署指南

> **Windows 机器请改看 [`README_windows.md`](README_windows.md)** —— 那条路径不依赖
> systemd：三个 `.bat` 脚本（引擎 / API / 前端）各自完成准备与启动，你只需敲三条命令。

目标环境：Ubuntu 22.04+，部署路径 `/opt/trident`（可自行调整，systemd unit 里的路径同步改即可）。

三个常驻进程：

| systemd unit | 进程 | 说明 |
|---|---|---|
| `trident-engine.service` | `python engine.py` | 新闻引擎：WS 采集 + AI worker + 前向验证 + webhook:9000 |
| `trident-api.service` | `uvicorn api_server:app` | FastAPI + SSE，127.0.0.1:8000 |
| `trident-bridge.service` | `python treenews_bridge.py` | TreeNews → engine webhook 桥（可选） |

前端 Next.js 生产模式可用 `npm start` 跑在 3000 端口（用 pm2 或自行包一个 unit），由 Nginx 反代对外。

## 1. 系统依赖

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv nodejs npm nginx git
# Node 建议用 nvm 装 18+，apt 源版本可能过旧
```

## 2. 拉取代码 + Python 环境

```bash
sudo mkdir -p /opt/trident
sudo useradd -r -s /usr/sbin/nologin trident   # 专用用户
sudo chown trident:trident /opt/trident
sudo -u trident git clone https://github.com/Claire19950906/reflex-quant.git /opt/trident

cd /opt/trident
sudo -u trident python3.11 -m venv venv
sudo -u trident venv/bin/pip install -r backend/requirements.txt
# requirements.txt 已包含 websocket-client（treenews_bridge 需要）；
# 若用的是旧版 requirements，请单独补装：venv/bin/pip install websocket-client
```

## 3. 配置环境变量

```bash
sudo -u trident cp .env.example backend/.env
sudo -u trident nano backend/.env
```

必填：`OPENROUTER_API_KEY`（Kimi K3 主模型）。

`FEISHU_WEBHOOK_URL`：飞书群机器人告警地址，从机器人后台复制填入。
不配置则引擎只在启动时警告一次并跳过告警，不影响其他功能。

国内服务器调用 OpenRouter 需要代理，在 `.env` 里设置 `HTTP_PROXY`；
直连可通的海外服务器不要设置该项。

## 4. 初始化数据库

```bash
cd /opt/trident
sudo -u trident venv/bin/python backend/init_db.py
```

幂等，只建表补列，不会删除已有数据。日常启动无需重复执行（engine/api 启动时会自动 migrate）。
`init_db.py` 会同时预置因果链库的 8 条默认种子（幂等，重复执行不会重复插入）。

### 4.1 配置数据（提示库内容）—— 已自动导入，无需手动操作

部署包**不含** `*.db`（生产库含真实交易数据与账户信息），因此提示库里的可编辑内容
（品种专项规则 / 复盘经验 / 因果链 / 过滤关键词 / 自定义指令 / 主 prompt 覆盖）
不会随代码走。包里另带一份只含**配置类表**的种子库 `backend/seed_config.db`。

**这一步已经自动化**：`init_db.py`、引擎 `engine.py`、API `api_server.py`
三者启动时都会调用 `seed_restore.auto_import_from_seed()`，把种子里
「目标库还不存在的行」逐行补齐。所以正常部署**不需要手动跑任何命令**：

```
[SEED] 已从 seed_config.db 补齐配置：ai_asset_rules 6 条、review_insights 110 条、
       causal_chains 28 条、filter_keywords 362 条
```

安全边界（为什么可以放心让它自动跑）：

- **逐行比对，不覆盖本机改动**：只补目标库缺失的行；你在页面上改过的规则、
  启停过的经验都不会被种子还原。想强制全量覆盖才需要显式操作。
- **幂等**：目标库已含种子的全部行 → 零写入，每次启动都不会重复写。
- **失败不阻断**：种子文件缺失（源码环境）或导入异常 → 只打印一行提示，服务照常启动。

需要手动介入时（都用同一套逻辑，行为一致）：

```bash
cd /opt/trident
venv/bin/python deploy/restore_seed.py --dry-run   # 只看会补哪些行，不写入
venv/bin/python deploy/restore_seed.py             # 按需补齐（默认模式）
venv/bin/python deploy/restore_seed.py --force     # 强制全量覆盖同主键行
RESTORE_SEED=1 启动引擎                             # 等价于 --force
SKIP_SEED_RESTORE=1 启动引擎                        # 完全跳过自动导入
```

导入内容包括：

| 表 | 内容 | 提示库位置 |
| --- | --- | --- |
| `ai_asset_rules` | 品种专项分析规则（含各品种示例与 `all` 兜底） | 提示库 → 品种专项规则 |
| `review_insights` | 复盘经验（含生效与停用） | 提示库 → 当前生效经验规则 |
| `causal_chains` | 因果链库 | 因果链库页 |
| `filter_keywords` | 过滤关键词（JUNK/CORE） | 采集过滤页 |
| `ai_prompts` | 用户自定义分析指令 | 提示库 → 自定义指令 |
| `ai_main_prompts` | 主 prompt 覆盖（页面上编辑过的） | 提示库 → 主 Prompt |

导入后重启 engine/api 即生效（提示库相关读取有 5s 缓存）。

**新环境的经验规则会全部生效**：种子里的经验是 `active=0`（沿用来源机的启停状态），
但引擎启动时的自愈会把「停用且无命中样本」的经验恢复启用 —— 新库还没有交易历史，
等于没有任何证据证明它们差，所以先全用，随后由每次复盘按命中胜率自然筛选：

```
[INSIGHT] 启动自愈：恢复 N 条「停用但无命中样本」的经验（旧连坐停用遗留）
```

`ai_prompts` / `ai_main_prompts` 在种子库里通常为 0 条 —— 只有你在页面上编辑过
提示词才会有内容；为空表示使用代码内置的主 prompt（正常状态）。

## 5. 前端构建

```bash
cd /opt/trident/frontend
sudo -u trident npm ci
sudo -u trident npm run build
# 生产跑在 3000 端口（可用 pm2 守护）：
sudo -u trident npm start
```

生产环境确认 `frontend/.env.local` 中 `NEXT_PUBLIC_API_URL` 为空（走 Nginx 相对路径），
详见 `frontend/.env.example` 注释。

## 6. 安装 systemd units

```bash
sudo cp /opt/trident/deploy/trident-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trident-engine trident-api trident-bridge
```

## 7. Nginx

```bash
sudo cp /opt/trident/deploy/nginx.conf.example /etc/nginx/sites-available/trident
sudo nano /etc/nginx/sites-available/trident   # 改 server_name
sudo ln -s /etc/nginx/sites-available/trident /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 8. 首次启动验证清单

```bash
# 1) 三个服务都在跑
systemctl status trident-engine trident-api trident-bridge

# 2) 引擎日志：应看到 DB VERIFIED、Webhook server listening on 127.0.0.1:9000、
#    [INGEST] Connected
journalctl -u trident-engine -f

# 3) API 健康检查
curl http://127.0.0.1:8000/api/health

# 4) REST 拉取事件列表（有数据时返回 JSON 数组）
curl http://127.0.0.1:8000/api/events

# 5) SSE 流（保持连接，新信号实时推送，Ctrl-C 退出）
curl -N http://127.0.0.1:8000/api/events/stream

# 6) 通过 Nginx 验证全链路
curl -N http://<你的域名>/api/events/stream
```

如果 engine 日志里 `[SNAPSHOT]` 持续报 Binance 超时而服务器在国内，
检查 `.env` 的 `HTTP_PROXY` 是否生效（日志首行会打印代理状态）。
