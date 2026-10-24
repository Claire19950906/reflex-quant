<div align="center">

# Reflex Quant 🪞
### *Every signal audited. Every mistake coded. Every blind spot hunted.*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Stars](https://img.shields.io/github/stars/Claire19950906/reflex-quant?style=social)](https://github.com/Claire19950906/reflex-quant)
[![Version](https://img.shields.io/badge/version-v225-blueviolet)](#)
[![Python](https://img.shields.io/badge/python-3.11+-green)](./backend/requirements.txt)
[![Tests](https://img.shields.io/badge/tests-189%20passing-success)](./backend/tests/)

**大多数 AI 量化在比谁的信号更"准"。Reflex Quant 比的是——谁先抓到自己的错。**

**This is the Chinese developer README.** For the English investor pitch, see [`README_INVESTORS.md`](./README_INVESTORS.md). For the deck, see [`PITCH_DECK.md`](./PITCH_DECK.md).

</div>

---

大多数 LLM 量化系统都是「开火-遗忘」式——出信号、看胜率、自评。
但**用来评价自己的指标，恰好就是它没做好的那一项**，盲区永远抓不到。

Reflex Quant 反过来——**先把每个信号审一遍，再决定信不信**：

- **27 个错误码 / 7 层反思（E001–E027）**：事件因果、因果方向、样本完整性、假设/反事实/校准/历史/跨品种/机理/置信度。每条都有明确的可执行修复建议。
- **元反思循环（meta-reflection loop）**：当 7 层全部漏掉同一笔坏交易时，系统会把这个 *盲点的盲点* 记下来，并主动提议新的反思器（E028+）。

> Reflex Quant 不会随时间变得更"自信"，而会随时间变得更"诚实"。
> 这是 LLM 驱动的 quant 系统里少有的差异化方向，也是 E028+ 向社区开放协作的入口。

**信号端背景**（不算差异化，一句话）：双源新闻采集（FinancialJuice WebSocket + TreeNews webhook）→ L1 关键词 + L2 LLM 双层去噪 → Kimi K3 推演 → 2h 真实行情前向验证 → SSE 推送 Next.js 前端 + 飞书群机器人。完整链路见下方架构图。

## 架构

```
 FinancialJuice WSS ──┐
                      ├─→ realtime_filter (L1 关键词 / L2 LLM) ─→ raw_news (SQLite, WAL)
 TreeNews webhook ────┘        (engine/ingest.py, engine/webhook.py)        │
 127.0.0.1:9000                                                             ▼
                                              engine/ai_worker.py (Kimi K3 via OpenRouter,
                                              市场快照 + 历史绩效注入, 父子信号聚合)
                                                                             │
                                                                             ▼
                                                                      ai_decisions 表
                                                                       │           │
                                                       engine/forward.py            │
                                                       2h 前向验证 (MFE/MAE/PnL,    │
                                                       WIN/LOSS 结算)               │
                                                                                   ▼
                                            api_server.py (FastAPI: REST + SSE, 127.0.0.1:8000)
                                                             │                │
                                                             ▼                ▼
                                            frontend/ (Next.js 14, :3000)   飞书告警
                                                                            (engine/alerts.py)

 dashboard/ (Streamlit :8501) — 离线复盘看板，直接读 SQLite，不在实时链路内
```

## 目录结构

```
Trident_Agent_MVP/
├── backend/
│   ├── src_python/
│   │   ├── engine.py            # 薄入口：python engine.py 启动引擎
│   │   ├── engine/              # 引擎包
│   │   │   ├── main.py          # 启动编排（DB migrate + 任务调度）
│   │   │   ├── ingest.py        # Task A: FinancialJuice WS 采集
│   │   │   ├── ai_worker.py     # Task B: LLM 批量分析 + 信号落库
│   │   │   ├── forward.py       # 2h 前向验证结算
│   │   │   ├── webhook.py       # TreeNews webhook + 英文前置翻译
│   │   │   ├── alerts.py        # 飞书告警推送
│   │   │   ├── prices.py        # 黄金/BTC/WTI 行情源
│   │   │   ├── ws_client.py     # stdlib WebSocket 客户端 (RFC 6455)
│   │   │   └── utils.py         # VIP 检测/去重 hash/垃圾文本判定等
│   │   ├── api_server.py        # FastAPI + SSE 后端服务
│   │   ├── realtime_filter.py   # L1 关键词 + L2 LLM 实时新闻过滤
│   │   ├── review_excel_export.py # 反思页 XLSX 一键导出（5 sheet，含冻结首行+自动筛选）
│   │   ├── market_snapshot.py   # BTC/XAU 市场快照（prompt 注入用）
│   │   ├── treenews_bridge.py   # TreeNews WS → 本地 webhook 桥（独立进程）
│   │   ├── config.py            # 唯一配置入口（.env 加载/密钥/阈值）
│   │   └── db.py                # schema 迁移（唯一权威）+ SQL 只读安全门
│   ├── scripts/                 # 回测、历史新闻注入等辅助脚本
│   ├── tests/                   # pytest 冒烟套件（43 用例，全离线）
│   ├── init_db.py               # 全新部署的 DB 初始化（幂等，不删数据）
│   ├── requirements.txt         # 运行依赖
│   └── requirements-dev.txt     # 测试依赖
├── frontend/                    # Next.js 14 实时信号台
├── dashboard/                   # Streamlit 离线复盘看板（见 dashboard/README.md）
├── deploy/                      # systemd units + nginx 示例 + 部署文档
├── scripts/dev_start.bat        # Windows 一键开发启动
├── docs/                        # 设计文档（archive/ 为已下架方向的存档）
└── .env.example                 # 环境变量模板（复制为 backend/.env）
```

## 快速开始（Windows）

前置：Python 3.11+、Node.js 18+。

```cmd
copy .env.example backend\.env
REM 编辑 backend\.env，至少填入 OPENROUTER_API_KEY

pip install -r backend\requirements.txt
cd frontend && npm ci && cd ..

scripts\dev_start.bat
```

`dev_start.bat` 会开三个窗口：engine、api_server（127.0.0.1:8000）、frontend（localhost:3000）。

手动方式（三个终端）：

```cmd
cd backend\src_python && python engine.py
cd backend\src_python && uvicorn api_server:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```

可选第四条链路（TreeNews 快讯源）：

```cmd
cd backend\src_python && python treenews_bridge.py
```

## 环境变量

复制 `.env.example` 为 `backend/.env` 后填写。完整注释见模板文件。

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENROUTER_API_KEY` | 是 | Kimi K3 主模型 + 实时过滤 L2 的调用通道 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 否 | Agent Chat 数据副驾（前端问答） |
| `AGENT_LLM_BASE_URL` / `AGENT_LLM_API_KEY` / `AGENT_LLM_MODEL` | 否 | Agent Chat 专用配置，默认可复用 DeepSeek |
| `XAI_API_KEY` | 否 | Grok 备用 |
| `DOUBAO_API_KEY` / `DOUBAO_MODEL` / `DOUBAO_BASE_URL` | 否 | 火山引擎（当前停用，DOUBAO_MODEL 须为 Endpoint ID） |
| `FEISHU_WEBHOOK_URL` | 建议 | 飞书群机器人告警地址；为空则跳过推送 |
| `CORS_ALLOW_ORIGINS` | 否 | API 跨域白名单，逗号分隔，默认 `http://localhost:3000,http://127.0.0.1:3000` |
| `HTTP_PROXY` | 视网络 | 国内机器访问 OpenRouter/Binance 需要；海外直连不要设 |
| `TREE_NEWS_PORT` | 否 | engine webhook 监听端口，默认 9000 |
| `TRIDENT_DB_PATH` | 否 | 覆盖 SQLite 路径，默认 `backend/trident_event_bus.db` |
| `MT5_*` / `BINANCE_*` | 否 | MetaTrader 5 本地行情 / 币安账户（均非必需） |

### v240+ 反思增强：K-Bayes / BOCPD / Tournament

| 字段 | 默认 | 说明 |
|---|---|---|
| `REFLECTION_BAYES_ENABLED` | `0` | 启用 Beta-Binomial 后验概率追踪每条反思器真实胜率；启用后 evidence_refs 增 `_bayes_p_curr_lt_base` |
| `REFLECTION_BAYES_ALPHA_PRIOR` | `2` | Beta 先验 α（胜例先验样本） |
| `REFLECTION_BAYES_BETA_PRIOR` | `2` | Beta 先验 β（负例先验样本） |
| `REFLECTION_BAYES_ABS_DELTA` | `0.05` | 触发"真实胜率显著偏离基线"的最小后验-先验差 |
| `REFLECTION_BAYES_MIN_SAMPLES` | `10` | 样本数低于此值不判定（避免小样本误判） |
| `REFLECTION_BOCPD_ENABLED` | `0` | 启用 Bayesian Online Change-Point Detection 检测信号突变点；触发时写入 evidence_refs `_bocpd_fresh_run_prob` |
| `REFLECTION_BOCPD_HAZARD` | `200` | BOCPD 先验"突变概率倒数"（值越大越保守） |
| `REFLECTION_BOCPD_THRESHOLD` | `0.5` | 突变后验概率阈值；≥ 此值记为 fresh-run 信号 |
| `TOURNAMENT_ENABLED` | `0` | 启用 k=3 LLM verifier 投票替代单次 LLM；开启后 evidence_refs 增 `_tournament_quorum` / `_tournament_verifier_ids` |
| `TOURNAMENT_QUORUM` | `2` | quorum 数（k=3 中至少 2 个同意才 confirm） |
| `TOURNAMENT_TIMEOUT` | `60` | 单次 tournament wall-clock 上限（秒） |
| `TOURNAMENT_PARALLEL` | `1` | 并发跑 verifier；设 `0` 走 serial（调试用） |
| `TOURNAMENT_CB_THRESHOLD` | `0.7` | Circuit breaker：滑动窗口内"全 verifier 失败"率 ≥ 此值触发 OPEN，短路回单次 LLM |
| `TOURNAMENT_CB_WINDOW` | `10` | 滑动窗口大小（最近 N 次调用） |
| `TOURNAMENT_CB_COOLDOWN` | `20` | OPEN 后冷却 N 次调用再 half-open 重试 |

**全部默认 `0`（opt-in）** — 开启任意一项即可立即获得更细粒度的反射审计，零破坏性。

观测端点：
```bash
curl http://127.0.0.1:8000/api/health/tournament
# → tournament metrics + circuit breaker state + 当前 config
#   { "metrics": {total_runs, confirmed_count, avg_elapsed_ms, quorum_distribution, ...},
#     "circuit_breaker": {is_open, recent_calls, ...}, "config": {...} }
```

## 测试

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests/ -q     # 43 个用例，全部离线
```

## 体验模式（v300 新增）— 给朋友/公开展示用

Trident 的核心算法是私有 IP。v300 起提供三种部署模式，由 `TRIDENT_MODE` 切换：

| 模式 | 场景 | 体验者看到 |
|---|---|---|
| `full`（默认） | 自己玩 | 全精度 + 全部功能 + 源码 |
| `demo` | 公开展示 / 朋友初探 | 2 位小数 + 1 天历史 + 限流 + 水印 |
| `licensed` | 给受邀请用户 | 4 位小数 + 7 天历史 + 凭 License + API Key |

体验者拿到的是**只读镜像**（Docker / Modal），永远拿不到 `engine/` 源码。
每次响应带 `X-Trident-Watermark` 头，截图泄漏可定位。

快速体验：

```bash
# 1. 在你本机签发许可证（仅 licensed 模式需要）
python3 scripts/gen_license.py issue --tier pro --days 30 \
    --issued-to "bob@friend.com" \
    --secret "$TRIDENT_LICENSE_SECRET" \
    --out ./license.lic

# 2. 创一个 API Key
python3 scripts/gen_api_key.py create --name bob-laptop --tier pro \
    --store ./api_keys.json
# → trk_xxxxxxxxxxxxxxxx

# 3. 启动体验模式
export TRIDENT_MODE=licensed
export TRIDENT_LICENSE_SECRET=...        # openssl rand -hex 32
export TRIDENT_LICENSE_FILE=$(pwd)/license.lic
export TRIDENT_API_KEY_STORE=$(pwd)/api_keys.json
python3 -m api_server
```

完整设计、5 层防御、应急响应见 [`docs/SECURITY_DEMO_MODE.md`](./docs/SECURITY_DEMO_MODE.md)
和 [`docs/SECURITY_DEMO_MODE_DEPLOY.md`](./docs/SECURITY_DEMO_MODE_DEPLOY.md)。

## 反思系统 XLSX 导出

`/review` 页面右下角 `↓ 导出 XLSX` 按钮调后端 `GET /api/review/export.xlsx`，
返回 5 个 sheet 的 .xlsx（Overview / Trades / Reflections / Insights / Experiences），
每个 sheet 都冻结首行 + 开启自动筛选，方便 Excel 中筛选分析。

```bash
curl -OJ http://127.0.0.1:8000/api/review/export.xlsx
# → trident_review_YYYYMMDD_HHMMSS.xlsx  (~70KB, cold 12s / warm 0.5s)
```

CLI 自检（不依赖前端）：

```bash
cd backend/src_python && python review_excel_export.py
# → ./trident_review_YYYYMMDD_HHMM.xlsx
```

导出逻辑复用 `_safe_query` + `pnl_pct>0/<0` 派生 win/loss，不引入新 schema 假设。

## 部署

Linux 生产部署（systemd + nginx）见 [deploy/README.md](deploy/README.md)。

## 子项目

- `frontend/` — Next.js 14 实时信号台。开发 `npm run dev`（:3000），生产 `npm run build && npm start`。环境变量见 `frontend/.env.example`。
- `dashboard/` — Streamlit 离线复盘看板（:8501），直接读 SQLite/Excel 做历史信号分析，不在实时链路内。详见 `dashboard/README.md`。
- `docs/` — 架构设计文档；`docs/archive/` 是已移除的 Hermes 研究 Agent 层设计存档。

---

## 📬 联系与贡献

- **邮箱（创始人）：** z2132743607@163.com
- **Issues / 提案：** [github.com/Claire19950906/reflex-quant/issues](https://github.com/Claire19950906/reflex-quant/issues)
- **贡献指南：** [`CONTRIBUTING.md`](./CONTRIBUTING.md)
- **行为准则：** [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md)
- **安全问题：** [`SECURITY.md`](./SECURITY.md)

**想成为 27+ 反思器的协作者？** 元反思循环（E028+）是开放的——如果你抓到
一次所有反思器都漏掉的坏交易，欢迎提一个 issue 描述场景并提议新的 checker。

## 📄 License

MIT — 见 [`LICENSE`](./LICENSE)。

## 🌟 Star History

如果你认可"LLM 驱动的 quant 系统需要反思层"这个方向，请点 ⭐ — 这是我们唯一的
分发渠道，也是吸引潜在合伙人/投资人最直接的信号。

<sub>v225 — 2026-10-06</sub>
