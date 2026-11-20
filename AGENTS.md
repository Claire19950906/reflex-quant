# AGENTS.md — AI 协作者项目约定

## 数据真实性铁律（v396，最高优先级）

改任何"取价 / 结算 / 记账 / 报表"代码前，先读 `docs/DATA_AUTHENTICITY.md`。六条不可违反：

1. **不猜价** —— 取不到就 `None`，不许拿旧价/日线/邻近日顶替
2. **不补价** —— 缺口不插值、不回填；缺数据就 `ABANDONED`
3. **不跨源拼价** —— 入场与出场必须能被同一条行情序列解释
4. **不占位** —— `0` / `''` / `None` 不得作为有效值参与统计
5. **单一真值源** —— 账户/前端/报表读同一张表同一口径
6. **口径可追溯** —— 每个数字都要能点回"哪张表、哪条 SQL、哪个时刻"

守门在三处：`sample_quality_filter` R1–R10（建仓）、`src_python/data_authenticity.py` S1–S7（结算审计，每日 07:12 跑）、`tests/test_data_authenticity_v396.py`（回归）。
绩效一律"扣费后"（`pnl_pct` 净值 / `gross_pnl_pct` 毛值），有效样本 < 60 时任何均值都不构成结论。

## 0 号铁律：系统里不许有假数据（v403，凌驾于其他所有规则）

用户原话："以后在这个系统里不能出现任何假的数据。"

**定义（唯一的判据）**：一个数/一段文本，如果它无法追溯到真实观测（真实 tick、真实新闻原文、真实 LLM 原文），它就是假的。占位值、默认值、模板拼出来的"判断"全部算假 —— 哪怕它看起来很像真的。

三条处置方式，只允许前两种：

| 情形 | 正确做法 | 禁止的做法 |
|---|---|---|
| 这个值本来就没算过（降级路径没跑 LLM、门卫没打分） | 置 `NULL`，语义是"不知道" | 留着 `0` / `5.0` / `''` 当有效值参与统计 |
| 这条记录本身不该存在（引用降级决策的成交/样本） | 删行 | 保留一行"看起来正常"的记录 |
| —— | —— | **绝不允许编一个合理值把它填平**（这是最严重的违规） |

**强制守卫**：`backend/src_python/fake_data.py`

```bash
cd backend/src_python && PYTHONPATH=. python3 fake_data.py           # 审计（只读）
cd backend/src_python && PYTHONPATH=. python3 fake_data.py --guard    # 有假数据退出码 1
```

`--guard` 已挂在每日 07:12 的 `run_funnel_report.sh` 班次上，结果写 `backend/reports/health/fake_data_guard_<日期>.json`。
**加任何"回填 / 补数 / 默认值"逻辑前，先问一句：这个值是我算出来的，还是我编出来的？**

已知九类形态（`fake_data.py` 逐条覆盖）：F1 置信度 0 占位、F2 影响力 5.0 默认值、F3 反思 confidence 占位、F4 引用降级决策的假成交、F5 影子样本引用降级决策、F6 demo/mock/合成源、F7 孤儿决策、F8 回放混进真实绩效表、F9 回放行缺少来源标签。

编写新代码时必须遵守的两条推论：

1. **降级路径不许落"有效数值"**：LLM 超时/解析失败时，`evidence_confidence`、`impact_score` 必须写 `NULL`，不许写默认值；同时 `analysis_degraded=1` 必须置位。
2. **降级决策不许建仓**（R11）：`sample_quality_filter` 拒绝后不得有任何旁路（含 quick_sim、shadow_ledger、回放脚本）能给它生成成交/样本。
3. **回放 ≠ 绩效**（v407）：历史回放样本（`trident2` 的 `t2_sim_trades`，source=`news_replay`/`decision_replay`）只是研究材料 —— 它能反复重跑、能调参重选，**永远不许进入胜率、Sharpe、累计盈亏、置信度校准等任何对外绩效口径**。绩效只有一个来源：`quick_sim_trades` 里的真实纸面成交（`Context.live_trades()`）。报告"样本不足"时必须报出被隔离的回放条数，不许拿回放凑数。

**造数代码一律不许存在**（v407）：`master_api.py` 的 random 演示信号已改为直接 `NotImplementedError`（端点返回 501）；`seed_trade_reflections.py` 已停用（`[REFUSED]` 退出码 2）。任何"为了演示/为了让页面好看"而 `random.uniform/randint` 生成信号、置信度、RSI、曲线、反思的代码都不得提交。

## v444 · 产品定策：断档恢复后**不补历史新闻**（2026-11-18，用户原话，长期有效）

> "以后所有的 LLM 通道恢复之后，都不去做历史的新闻……它只分析现在进来的，
>  包括现在到以后进来的新闻，他不用分析以前的新闻。否则这样如果堆积的多的话，
>  我这个打不住的。以后就这样设置。"

**规则（不容改的语义）**：LLM 通道断过档 → 恢复的那一刻起，**只分析恢复时刻之后入库的新闻**；
断档期间积压的旧闻一律不送 LLM，直接归档留痕（`raw_news.status='FILTERED'` +
`filter_reason='pre_recovery_skip'`，原始行保留可查）。**不许"尽快把积压补干净"** —— 那是反规则。

**实现（单一真源）**：`backend/src_python/news_cutoff.py`
- 持久化 epoch 水位 `floor_ts`，状态文件 `backend/runtime/news_cutoff.json`（跨进程、只前进不后退、原子替换写）。
- 断档判定：`ai_call_stats` 里 `purpose IN ('main','analysis') AND status='ok'` 的最近一次成功调用
  距今 ≥ `NEWS_RECOVERY_GAP_MIN`（默认 15 分钟）→ 判定断档；水位每 60s 被推到"当前时刻"，
  所以通道真正恢复时水位天然就是恢复时刻（不需要额外的状态机）。
- 判定只看**主分析**成功：门卫（`gate`）额度耗尽时也会偶发 ok（2026-11-18 实测 16:26 就有），
  它成功不代表决策链活着 —— 这正是那次"门卫在响、决策为 0"的分界点。

**四处共用同一口径**（改任一处必须同步其余三处）：
| 位置 | 作用 |
|---|---|
| `engine/ai_worker.py::_build_claim_sql` | 认领加 `ts >= floor` |
| `engine/ai_worker.py::_archive_below_floor` | 水位之下 PENDING → FILTERED/`pre_recovery_skip` |
| `engine/gate_replay_guard.py` | 门卫回放不碰水位之下的行（否则额度照样被历史烧光） |
| `news_funnel.py::funnel_stats` | 面板"待分析"扣掉水位之下的行，数字与实际可分析量一致 |
另外 `news_score_worker.py` 的 6h 硬闸取 `max(6h_cutoff, floor)`。

**开关**：`NEWS_SKIP_BACKLOG_ON_RECOVERY=0` 关闭策略；`NEWS_RECOVERY_GAP_MIN=0` 关闭断档判定。
只有"重放历史做回归"时才允许关 —— 关掉后 2026-11-18 那种断档 5 小时的场景会立刻把额度烧光。

**回归**：`backend/tests/test_v444_news_cutoff.py`（12 条，全离线；覆盖只前进、断档判定、
门卫 ok 不算健康、认领过滤、归档留痕、面板同口径）。



### v447 新闻配额（2026-11-18）

`paper_trading_settings.news_daily_target` 与 `feeds/news_sources._SOURCE_DAILY_CAP` 是
**新闻能不能进来**的总闸。配额设小 = 下午就把所有源掐死。现在 12000/日、单源 600–1200，
调小之前先确认能消化（门卫 + 主分析吞吐）。

### v445 · 吞吐与队列容量（2026-11-18）

- **不限速**是用户明确指令：`LLM_MAX_RPM=0` + `LLM_MIN_INTERVAL_S=0`。不要"为了稳"把这两项改回去，
  除非用户在明确要求恢复限速。
- 吞吐靠 `AI_BATCH_SIZE`（16）与 `LLM_MAX_CONCURRENCY`（16）撑；批越大单批越慢，
  所以 `NEWS_RECOVERY_GAP_MIN` 必须是 30 分钟（15 分钟会误判大批次为断档并清空队列）。
- 队列兜底：`NEWS_BACKLOG_CAP=50` / `NEWS_BACKLOG_KEEP=20`。队列超 50 条时自动只留最新 20 条，
  老的走 `pre_recovery_skip` 归档。这是用户"堆多了打不住"的直接对应机制，不是 bug。

## 目录速览

- `backend/src_python/engine/` — 新闻引擎包（ingest / ai_worker / forward / webhook / alerts / prices / ws_client / utils / main），`engine.py` 是薄入口
- `backend/src_python/api_server.py` — FastAPI + SSE 服务（127.0.0.1:8000）
- `backend/src_python/config.py` — 唯一配置入口（.env 加载、密钥、阈值常量）
- `backend/src_python/db.py` — schema 迁移唯一权威 + `assert_readonly_sql`
- `backend/src_python/integrity_check.py` — P0 数据对齐：11 条外键孤儿 SQL 检测（read-only）
- `backend/src_python/observability.py` — P1 可观测性：`heartbeat.tick` / `dlq.record` helpers
- `backend/src_python/circuit_breaker.py` — v172 熔断器（`CircuitBreaker.get(name, fail_threshold=3, open_seconds=60.0)`），open-期短路返回不消耗网络/额度
- `backend/tests/` — pytest 冒烟套件（全离线）。当前 **189 用例 ~63s**（含 reflection 的 budget 测试 sleep 20s × 3）
- `frontend/` — Next.js 14 前端（:3000）
- `dashboard/` — Streamlit 离线复盘看板（:8501）
- `deploy/` — systemd + nginx 部署配置
- 更详细的模块说明见 `backend/README.md`，部署见 `deploy/README.md`

## v170 修复与运行约束（本轮审计后）

**TradingProcessManager fail-closed 默认开启**（A1 修复）。`.env` 不显式 `TRADING_MANAGER_ENABLED=1` 时，`POST /api/v1/trading-manager/start` 与 `/authorize-trading-manager` 均返回 **503 / 拒绝**，防止本地开发环境下自动进程误开实盘。需启用时显式置 `TRADING_MANAGER_ENABLED=1/true/yes/on`。

**`.env` 中 `BINANCE_API_KEY` / `BINANCE_API_SECRET` 默认为空 —— 不发包，仅 fetch 行情。** `BINANCE_NOTIONAL_USDT` 已从 30 改为 5000（F1：避免测试订单绕过测试币阈值下沿）。本地若没有 key 就留空，不要提交带 key 的 `.env`。

## v171 双层契约 + 数据对齐 / 可观测性（增量，非破坏性）

> 本轮用"双层契约"框架落地两块增量，**未改 schema 语义、未碰 ai_worker INSERT/UPDATE、零破坏性升级**：

**P0 — 数据对齐：外键孤儿检测**（`backend/src_python/integrity_check.py`）

11 条 SQL 检测，read-only。覆盖 `raw_news → news_events/factor_snapshots/hermes_observations/ai_decisions` 与 `ai_decisions → quick_sim_trades/signal_performance/hermes_observations/trade_reflections` 的双亲孤儿 + `quick_sim_trades.news_id` 双亲。导出 `run_checks()` / `summarize()` / `is_clean()` / `run_and_print()`。**新增 19 个测试用例** 在 `tests/test_data_integrity.py`（覆盖空库、11 条 link 各 1 孤儿用例、健康全链、修复后清除、CLI smoke、`PRAGMA foreign_keys=OFF` legacy 兜底）。

**P1 — 稳定运行：worker 心跳 + DLQ**（`backend/src_python/observability.py`）

新增 2 张表：
- `worker_heartbeat (worker_name UNIQUE, last_tick_at, last_status ok/busy/error, last_error, processed, detail)` — 每个 worker task 每 tick upsert
- `dlq_events (kind, source, payload截4KB, last_error, retry_count, resolved 0/1, resolved_at)` — 永久死信队列（**≠ 重试队列**）

`heartbeat.tick(conn, name, processed, detail, status, error)` / `dlq.record(conn, kind, source, payload, error, retry_count)` / `list_stale_workers(conn, stale_after_sec=90)` / `list_unresolved_dlq(conn, limit)` / `resolve_dlq(conn, id, note)`。**新增 16 个测试用例** 在 `tests/test_observability.py`。helper 自带 try/except 兜底：DB 异常不会杀死调用方 worker。

> 当前 worker 进程尚未 wire-in `heartbeat.tick` / `dlq.record` 调用点（避免触动 ai_worker / ingest / reflection 主路径）。后续可逐 worker 小 PR 接入，每个 worker 一个 `_safe_tick_xxx()` 装饰器包裹 tick 即可。

**测试统计**：171 用例 PASSED in ~63s（含 reflection budget sleep 20s × 2），EXIT=0。

## v172 通道探针改造 — circuit_breaker wire-in + 单真相源 + 失败分类（增量，非破坏性）

> 修复 `/api/health/models` 显示 5 通道异常但缺少根因诊断的问题。**只动 `api_server.py` 的 probe 子树，未碰 ai_worker 主路径，零破坏性**：

**1. 熔断器 wire-in（`circuit_breaker.py` → `_probe_channel`）**

每个 LLM 通道独立 `circuit_breaker.get(f"llm:{name}", fail_threshold=3, open_seconds=60.0)`。**3 次失败 → 60s 内短路**：
```python
breaker = circuit_breaker.get(f"llm:{name}", fail_threshold=3, open_seconds=60.0)
if not breaker.allow():
    return {"ok": False, "category": "circuit_open", "status_code": None, ...}
try:
    resp = requests.post(url, ...)
except Exception as e:
    breaker.record_failure()
    return {"ok": False, "category": _classify_probe_failure(e, None), ...}
breaker.record_success(); ...
```
open 期间直接返回 `category="circuit_open"`，**不消耗网络 / 不消耗额度**。已 wire-in 到 `_probe_channel` 一个调用点（**未**穿透到 ai_worker 主推理循环，避免引入未知风险）。

**2. 单真相源（`_probe_model_channels` 改用 `config.AI_MODEL_ROSTER`）**

旧 `_probe_model_channels()` 维护了一份内嵌 `probe_specs`，与 `config.AI_MODEL_ROSTER` 双写。**改成 `for m in config.AI_MODEL_ROSTER`**，消除"roster 改了但 probe_specs 没改"的漂移：
```python
def _probe_model_channels():
    out = []
    for m in config.AI_MODEL_ROSTER:  # ← 唯一真相源
        name = m["name"]
        api_key = m["api_key"]  # 来自 .env，不硬编码
        out.append(_probe_channel(name, api_key))
    return out
```
任何新通道只改 `config.AI_MODEL_ROSTER` 即可被自动探针。

**3. 失败分类（`_classify_probe_failure(e, status_code)`）**

| 状态码 / 异常 | `category` | 可执行修复 |
|---|---|---|
| 200 + JSON ok | `ok` | — |
| `requests.exceptions.*` / `None` | `network` | 配 `HTTP_PROXY` / 检查 DNS / 海外直连 |
| 401 / 403 | `auth` | 检查 `OPENROUTER_API_KEY` 等是否过期/被吊销 |
| 429 | `rate_limit` | 错峰调用 / 申请更高 QPS / 换 key |
| 400-499（除 401/403/429）/ 500-599 | `api_error` | 检查 `model` 名是否被中转后端废弃 |

**4. 新增 18 个测试用例** 在 `tests/test_probe_health.py`：
- `_classify_probe_failure` 映射表全覆盖（5 类 × 多状态）
- circuit_breaker open-期短路（不调网络）/ close 恢复
- `_probe_channel` 异常路径（`requests.exceptions.Timeout` / `ConnectionError` / `HTTPError`）
- `_probe_model_channels` 单真相源（roster 改 → probe 自动跟随）
- `_probe_channel` 中 `breaker.record_failure` 触发 3 次后 open

**测试统计**：**189 用例 PASSED** in ~63s（53 + 79 + 20 + 17 + 20 = 189），EXIT=0。分块验证：v171 三模块 (probe_health 18 + observability 16 + data_integrity 19) = **53** 全绿；其余 136 旧用例零回归。

**5 通道根因速查表**（当前 `/api/health/models` 现状）：
| 通道 | category | 根因 | 修复动作 |
|---|---|---|---|
| `OpenRouter` | `network` | 海外直连无 HTTP_PROXY | 在 `config.py` 加 `HTTP_PROXY=...` 走代理 |
| `MiniMax` | `network` | 同上 | 同上 |
| `MiniMax#2` | `network` | 同上 | 同上 |
| `nassaapi` | `api_error` | `gpt-5.4-mini` 模型名被中转后端废弃（400） | 改 `nassaapi` 的 `model` 字段为后端支持的等价模型 |
| `xinshu` | `rate_limit` | 中转商触发 QPS 上限（429） | 错峰（worker 间隔从 5s → 15s）/ 申请更高 QPS |

## v321.2 能力验证修复（5 项，全部有实测证据）

> 触发：能力验收实测暴露 5 处真实缺陷。修复后验收 **PASS=24 / FAIL=0**（原 21/3）。

### ① 资产错配（400 条）→ `news_score_worker.py`
`news_events.asset` 存的是采集端 gate 的**板块中文标签**（"原油"/"个股"/"加密主流"），
双关键词标题（"比特币 ETF 净流出 + 油价承压"）会被判成"原油"。错配经
`event_move_attribution` / `event_kline_impact` / `missed_signal_taxonomy`
三处继承 → 归因把 BTC 的消息算到 WTI 头上。

**修法**：
- 新增 `_extract_codes_strict()` —— 只看**标题首行**、ticker 须独立成词、
  "比特"须配"比特币"。**不能**用宽松的 `_extract_codes()`（全文扫描会把正文尾部
  的 `[美股 NVDA]` 提示行当品种指称，实测造成 809 条误改）。
- 新增 `_subject_text()` / `_strip_ticker_prefix()` / `_alias_in_subject()` ——
  资产必须出现在**主体位置**（标题前 8 字，且剥掉 `[hash:…]` 与 `道琼斯：` 这类
  ticker 前缀）才算事件主体；宽基指数是主体时不改。
- 新增 `--fix-asset` 模式：**只修 asset**，不动 action/score/status
  （`--rescore-done` 会连交易决策结论一起洗掉，副作用超出"修资产"范围）。
- 板块标签归一化为 ticker（复用 `server_verdict._CN_ASSET_MAP`）：
  原油→WTI / 黄金→XAU / 加密主流→BTC / 个股→INDIVIDUAL_STOCK / 美股→US_STOCK。
  **"山寨"保留**（无单一代表品种，改成任一 ticker 都是错的）。

### ② 权重 β 污染 → `capability_acceptance.py`
验收 #2 用 `ABS(forward_move_pct)` 当"对市场的影响"，**混进了市场 beta**：
普涨/普跌窗口里所有资产的 |涨跌| 都大，权重高与"跌得多"偶然同向/反向。
滚动 600 条窗口实测 ρ 在 **+0.74 ~ −0.54** 之间反复翻转（6/13 窗口反向）。

**修法**：新增 `_market_beta_benchmark()`（同小时横截面收益**中位数**），
超额口径 `|fmv_signed − 基准|` 作为**主判据**（绝对口径保留为参考）。
> ⚠️ 必须用**带符号**的收益减基准 —— 拿 ABS 后的值去减会让符号信息全丢
> （实测 ρ 从 +0.2215 掉到 +0.0537）。
> 结果：ρ = **+0.222**（原 +0.016），窗口反向数 6/13 → 1/13。

### ③ 策略反思停摆 5 周 → `engine/main.py`
`engine/strategy_reflection.py` 的 `run_strategy_reflection_daemon()` 写好了，
但**从未被任何调度注册** —— `strategy_reflections` 停在 2026-09-30，
`strategy_versions` 停在 09-23，而 `strategy_param_adjustments` 一直在更新到 11-04
（参数在调，"为什么调"的反思断了）。

**修法**：新增 `async def strategy_reflection_loop()` 并注册进 tasks。
不用原函数是因为它是**阻塞式 `time.sleep`** —— 放进 asyncio 会卡死整个事件循环
（engine 里 12 个 worker 共用一个 loop）。启动后**先跑一次**（不等 7 天）。

### ④ 快涨检测权限报错 → `~/Library/LaunchAgents/com.trident.fast-move-detector-cron.plist`
`WorkingDirectory` 指向 `~/Desktop/金融1.0/...`，而 `Desktop` 是 `drwx------`
（macOS TCC 保护）→ launchd 进程 `getcwd` 报 `Operation not permitted`。
**修法**：`WorkingDirectory` 改 `/Users/claire`（wrapper 内已有 `cd "$SRC"`）。
> 注意：`inserted=0` **不是**缺陷 —— 实测 48h 内只有 WTI 达到 ±1.5% 阈值，
> 其余品种市场平静。去重逻辑工作正常。

### ⑤ belief 恒 0 证据 → `belief_store.py`
`_belief_extra_facts` 产出的事实 indicator 固定为 "ai_decisions"/"news_events"，
只有 thesis **字面提到表名**才能匹配 → 14 条 belief 里 9 条恒 0/0
（验收 #4B 判 FAIL 的直接原因）。

**修法**：这些事实是**按资产查出**的（`WHERE target_asset=?`），资产就是天然锚点。
thesis 提到该资产（BTC/比特币…）即进入方向比对。
> 方向判定**完全不变**（chain_direction vs fact.direction）—— 不是无条件算支持。

### 修复结果
| 指标 | 修复前 | 修复后 |
|---|---|---|
| 验收 PASS / FAIL | 21 / 3 | **24 / 0** |
| #2 权重 Spearman | ρ=+0.016 | **ρ=+0.222** |
| #4B belief 有证据 | 5/14 | **12/14** |
| 因果链三问覆盖 | 356/632 | **632/632**（active 425/425） |
| 标题含BTC但asset非BTC | 400 | **73**（残余为主体是他物的合理项） |
| 策略反思调度 | 未注册 | 已注册，启动即跑 |

**新增测试**：`backend/tests/test_v321_fixes.py`（22 用例，全离线）。
回归：全量 **2812 用例**（2026-11-09 实测：2810 passed / 2 skipped / 0 failed，约 22 分钟）。
> 旧口径"3073 用例"与实际不符，已更正。跑法必须用系统 Python：
> `/Library/Frameworks/Python.framework/Versions/3.14/bin/pytest tests/ -q`
> （sandbox 的 `python3` 没有 pytest / dotenv，会出假失败）。

## v337 能力验证缺陷修复 — 因果链接进决策 + 归因去自指（除 LLM 外全修）

> 触发：v336 能力验收交付后，用户要求「除了 LLM 断了，其他的全修复一下」。

**根因与修法（6 项，全部有实测证据）**

1. **因果图谱没接进决策**：`decision_engine.py` 全文没有 `causal_chain` 引用，724 条链只做事后评估。
   新增 `backend/src_python/chain_prior.py`（`match_by_headline` / `match_by_time` /
   `aggregate_direction` / `conflict_veto` / `build_prompt_block`）：
   - 排序 = 证据充分优先 → hit_rate → posterior → 命中关键词数；`_NON_ASSET_VALUES` 排除跨资产链。
   - `propose_one` 注入 `evidence["chain_prior"/"chain_direction"/"chain_veto"]`，强链反向 → `action=HOLD`
     并在 rationale 记 `chain_veto:<chain_id>`；异常只打日志不阻断落库。
   - `engine/ai_worker._build_chain_prior_block` 把链先验拼进 prompt（补上 v176.3 停用 `_build_causal_block`
     后从未替代的空洞）。
2. **链触发词污染**：`trigger_words` 里混着 `BUY/SELL/ETH/XAU/verdict/prompt/market_event`，
   等于任意 ETH 新闻命中任意 ETH 链。加 `_GENERIC_TOKENS` 黑名单 + 英文关键词**词边界**匹配
   （原先 `oil` 命中 `boils`）。实测 500 条真实新闻：27% → **12.4%（62 条）**，精度优先。
3. **归因把结果当原因**：`_is_price_echo` 从 v325 写好却**从未调用**，107/268 是「跌破/涨破/24 小时跌幅」。
   - 候选循环加闸门（stat `price_echo_skipped`）；`_PRICE_ECHO_VERBS` 补 涨破/跌穿/涨穿。
   - 新增归档窗口 `_news_start`（早于新闻归档的异动不进分母，避免用 2021–2026 的历史异动做分母）。
   - `purge_price_echo()` / `repair_orphans()` + CLI `--purge-price-echo` / `--repair-orphans`，
     迁移用 **SAVEPOINT**（不用 BEGIN IMMEDIATE，否则 pytest 里报
     `cannot start a transaction within a transaction`）。
   - 生产库实跑（先备份 `backend/backups/event_move_attribution_20261114.csv` 263 行）：
     迁出 **87 孤儿 + 45 播报** → 重算 **252 行 / 431 异动 = 70.1%**（修前 26.2%），孤儿 0、播报 0。
4. **盘口 39 次调度全空转**：`depth_watch.py --commit` 分支只 `print("已存盘口")`，
   **从未调用 `store()`** → `depth_snapshots` 长期 0 行、验收 #14 判「订单簿不可用」。
   改为 `_rows = store(conn, asset, s)`。实测落库 3 行（BTC 29.9%/0.01bp、ETH 59.0%/0.05bp、SOL 47.1%/1.33bp）。
5. **验收口径 3 处失真**：
   - C4 只认 `runtime/pool_blocked_alert.json`，文件被人工清空（`reason=""`）就误判「通道正常」→
     补第二路证据「`ai_decisions` 停摆 >6h」→ `_llm_outage(conn=conn)`。
   - #14 `--no-http` 从不调端点却判「订单簿不可用」（那是没测）→ 离线读 `depth_snapshots`。
   - #11 用绝对资产数（清掉播报后 10→9 就 FAIL，能力没变）→ 改「≥10 个 **或** 可归因资产覆盖率 ≥60%」。
   - #12 判据文案标注「(已剔行情播报类)」，`n_causal_effective = n_causal - n_price_echo`。
6. **写锁打断作业**：`gate_calibrator._connect` / `depth_watch` 的 `sqlite3.connect` 默认 5s 锁等待，
   wrapper 里出现过 `database is locked` 把校准作业整体打断 → `timeout=30.0` + `PRAGMA busy_timeout=30000`。

**验收**（两个模式都要跑，结论以带 HTTP 为准）：
- `--no-http`：22/1/2/1 → **24 PASS / 0 FAIL / 1 WARN / 1 SKIP**（SKIP 是 C6）。
- 带 HTTP：**24 PASS / 1 FAIL / 1 WARN / 0 SKIP**。C6 转 PASS（端点↔库差值 0）；
  #12 翻成 FAIL —— **真因果 34.65%**（`rule_verdict` 三票自检 confirm 88 / partial 72 / reject 3）。
  这不是改口径改坏的：离线那次跑在 `rule_verdict` 刷新之前（254 行全 confirm），
  刷新后的 34.65% 才是真值。**以后报能力必须带 HTTP，否则 #12 会给出假 PASS。**
- 唯一 WARN 是 #1（真实开仓 41/78=52.6%，样本 <100），属 LLM 停摆导致的样本不足，不是代码缺陷。
- 全量回归（2812 例）本次**没跑完**：56% 处有用例挂在真实外网请求（`CLOSE_WAIT`）不动，
  21 min 无进展已终止。改跑相关子集 10 文件 = **149 passed / 0 failed**。

**新增测试**：`backend/tests/test_v337_fixes.py`（18 用例，全离线 in-memory）。
**未做**：engine / api_server 未重启 → `ai_worker` 的链先验与 `decision_engine` 的否决逻辑**尚未加载**（需重启生效）。

## v323 AI 通道修复 — 探针误判本地通道 + 「额度耗尽」单独成类

> 触发：能力验收实测发现 **AI 通道 100% 熔断、实时决策链路停摆 27 小时**
> （`ai_decisions` 最后一条 2026-11-09 05:04，之后 0 条）。

### ① 根因：4 家远程通道的额度/账号全废（**必须人工处理，代码修不了**）

逐个直连实测（2026-11-10）：

| 通道 | 真实返回 | 处置 |
|---|---|---|
| minimax 账户1（5 个 key 同一个 key，hash `ffa1d406`） | 429 `已达到 Token Plan 用量上限` | 充值 / 换 key |
| minimax 账户2 | 402 `insufficient_balance_error` | 充值 / 换 key |
| xinshu | 429 `API_KEY_QUOTA_EXHAUSTED`（额度已用完） | 充值 / 换 key |
| openrouter | 401 `User not found.` | key 已失效，换 key |
| nassaapi | `AIPING_ENABLED` 未开 + 模型名疑已废弃 | 未启用 |
| ark | `ARK_API_KEY` **为空** → 通道压根没建 | 填 key 即多一个国内直连通道 |
| 本地 ollama `qwen2.5:3b` | **HTTP 200 好着**（冷启动 25~74s，热态 1.1s） | 见 ② |

> 引擎侧 v322 的保护**本来就在正常工作**：全池熔断 → 不认领、零额度消耗、
> 6 小时节流告警（`runtime/pool_blocked_alert.json` → 微信/邮件），告警文案已含
> 「充值 / rotate_minimax_keys.py / 重启引擎」三步处置。

### ② 本地通道被探针误判成 network（**真 bug，已修**）

`_probe_channel` 的 read timeout 是 `(6, 15)`，而本地 ollama 冷启动实测 **25~74s**
→ 必然 read timeout → 被 `_classify_probe_failure` 归成 `network` → 连续 3 次后
熔断 → **面板上 3 个本地兜底通道全挂，实际它们一直好着**。

**修法**（`backend/src_python/api_server.py`）：
- 本地通道（`is_local`，由 roster 透传）超时放宽到 `(10, 120)`（覆盖 `.env` 记录的历史最坏情况「加载 1.9GB 权重后被 swap，chat/completions 90 秒不返回」），远程仍保持 `(6, 15)`；
- **本地通道的超时不计入熔断失败**（慢 ≠ 坏）：冷启动 25~74s 是常态，连着 3 次冷启动就把兜底通道摘掉是错的；
- 本地熔断窗口 180s（远程 60s）—— 一次冷启动失败不该把兜底通道判死几分钟；
- `_classify_probe_failure` 新增 `timeout` 类（与「连不上」区分）。

**实测效果**：本地 3 通道 `network` → **`ok` (HTTP 200)**。

### ③ 「额度耗尽」单独成类 + 静默窗口（**面板要能说清红因**）

旧口径把 429 一律归 `rate_limit`（语义=瞬时限流，可重试），402 归 `api_error`
→ 面板看不出「套餐已经用完，要充值」，探针还每 60s 再打一次必然失败的通道。

**修法**：
- `_classify_probe_failure(status_code, exc, body)` 新增 `body` 参数：
  402 → `quota_exhausted`；429 且 body 命中明确额度标记
  （`Token Plan` / `用量上限` / `QUOTA_EXHAUSTED` / `额度已用完` / `insufficient_balance`）
  → `quota_exhausted`；**普通 429 仍是 `rate_limit`**（不混淆瞬时限流与套餐耗尽）；
- `quota_exhausted` / `auth` 进入**静默窗口**（quota 30min / auth 15min）：
  窗口内**零网络请求**，直接返回已知分类；窗口过后自动复探 → **充值/换 key 之后
  自愈，不必重启进程**；
- 前端 `components/quant/stat-cards.tsx` 新增标签：
  `quota_exhausted → 额度耗尽`、`timeout → 超时`。

**实测效果**：9 个 minimax 通道 `rate_limit/api_error` → **`quota_exhausted`**（面板直显要充值）。

### ④ 验收脚本端点重试（消除假 FAIL）

`capability_acceptance.py::_http` 失败重试一次（超时 8s → 20s）。
实测：2026-11-09 20:14 全量跑 #14「市场基本面」判 FAIL（体制 0），复跑同一份
数据就 PASS（体制 6）—— 端点在 WAL 写锁竞争下偶发 >8s 超时，同一份数据两个结论
= 抖动不是事实。

### 测试

- `backend/tests/test_probe_health.py`：18 → **27 用例**（新增 9 条：402/额度 429/
  普通 429/超时分类、本地与远程超时、静默窗口进/出/鉴权）。
- 回归：`python3 -m pytest tests/ -q` 全量 —— **2863 passed / 2 skipped / 0 failed**
  （2026-11-10 实测，21m59s；全离线）。

### ⑤ 顺带查出：验收 #2「权重校准」判据处在临界点上（**未改阈值**）

同一天两次跑验收：第一次 ρ=+0.207 PASS，第二次 ρ=+0.200 FAIL（阈值 0.20）。
用同一口径复算**时间序滚动 600 条窗口**（2026-11-10 只读实测）：

```
逐窗口 ρ: +0.680  +0.054  +0.423  +0.520  +0.146  +0.195  -0.055
范围 [-0.055, +0.680]   中位数 +0.195   反向 1/7   低于 0.20 的窗口 4/7
前半段(较早): +0.680  +0.054  +0.423   ← 反向 0/3
后半段(较近): +0.204  +0.188  -0.068   ← 反向 1/3, 2/3 低于 0.20
```

即：**全样本 ρ=+0.20 是"早期强 + 近期衰减"平均出来的**，不是稳定的预测力；
最近三个窗口有 2 个低于阈值、1 个已经反向 → PASS/FAIL 会随每周新数据随机翻转。
与「84% 权重是事后回填（cron_react）」互相印证：**事前权重与结果的关联在变弱**。

> 阈值属于产品决策（铁律 6），**本次不改**。建议的口径（供决策）：
> 主判据换成「时间序滚动窗口 ρ 中位数 ≥ 0.15 且 反向窗口 ≤ 1/7」，
> 而不是全样本单点 ρ ≥ 0.20。

### ⚠️ 仍然需要人工做的两件事（代码解决不了）

1. **充值或更换 LLM key**（minimax 账户1/2、xinshu、openrouter）→ 主分析通道恢复；
   或填上 `ARK_API_KEY` 多一条国内直连通道。
2. **配出网代理**（`~/.trident/env` 里 `HTTP_PROXY/HTTPS_PROXY` 目前是**空的**）：
   launchd 拉起的 api/bridge/engine **不继承 shell 环境变量**，所以 Yahoo /
   Polygon / FRED / 翻译 API 在常驻进程里全是 SSL EOF —— 实测日志
   `[PRE-TRANSLATE] 🚨 被迫降级为英文原文进入数据库！`（新闻标题没翻译就入库，
   直接削弱中文关键词归因）。

## v324 本地通道熔断串味 + 权重判别力诊断 + 出网体检

> 触发：2026-11-10 能力验收实测（生产库只读）。
> 结构层 26 项：**PASS 24 / WARN 1 / FAIL 1**（#2 权重）；统计层 S1–S10：达标 2 / 未达线 7 / 样本不足 1。

### ① 本地通道熔断串味（真 bug，已修）— `backend/src_python/config.py`

`v322` 想按用途给三个本地通道去重，但判据写成「模型名 == `LOCAL_LLM_FAST_MODEL`」，
而 `.env` 里三个本地模型名**全是 `qwen2.5:3b`** → 第一个 `if` 永远命中 →
三个 id 全变成 `local/qwen2.5:3b-fast`，**依然完全相同**。

熔断器名按 `key_alias` 拼（`realtime_filter._llm_breaker` → `llm_net:{alias}` /
`llm_auth:{alias}`），于是三个本地通道共用一组熔断器 → **任一通道超时，三个一起被拉黑**。
实测 `llm_net:local` 与 `llm_auth:local` 同时 `open`，面板显示本地全挂，
而直连 ollama `/v1/chat/completions` 实测 **HTTP 200**（7.7s / 1.4s / 2.3s）。

**修法**：`_local_id` 一律按**用途**加后缀（与模型名无关）；`key_alias` 同样按用途独立。
实测：3 个本地通道 × 4 组熔断器全部 `closed`；`chat(purpose="advisor")` 返回 `OK`（7.8s）。

> ⚠️ 仍然**不能**开 `LOCAL_LLM_MAIN_ENABLED=1`：主分析需要 `qwen2.5-coder:7b` 及以上
> （当前只有 `qwen2.5:3b`，且 `ollama pull` 因出网不通失败）。本地通道现在覆盖
> `anomaly / advisor / sql / optimizer / gate / reflection`，**主分析仍只认 minimax**。

### ② 权重判别力的真相 — 新增 `backend/src_python/signal_weight_diagnostics.py`

验收 #2 的**单一全样本 Spearman**（ρ=+0.199，阈值 0.20）把三件事混在一起。分层实测（n=4355）：

| 口径 | ρ | 含义 |
|---|---|---|
| 全样本 | +0.1974 | 验收口径 |
| tier 内最高 | +0.1666 | 连续权重的增量信息很弱 |
| 2026-10 | +0.3410 | 早期强 |
| 2026-11 | **−0.1383** | 近期**已反向** |

**乘子坍缩自检**（本轮新发现，比 ρ 本身更重要）：7 个乘子里
`reaction`/`time` **100% 恒为 1.0**、`source` 95.7% 恒为 1.0、
`base` **85.7% 恒为 0.55**、`regime` 仅 2 取值 —— **只有 `impact` 一维真正在动**。

`base` 坍缩的根因（上游是 LLM 不打分）：
`sentiment_score` 95.9% 是 0 · `impact_score` 94.7% 是 5 · `heat_score` 94.7% 是 5
→ `_base_mult` 的凸组合 `strength = 0.5·0 + 0.3·0.5 + 0.2·0.5 = 0.25`
→ `base = 0.4 + 0.6·0.25 = 0.55`（与实测众数完全吻合）。

即：**权重排序几乎完全由「该品种平时能走多少」决定，「信号本身有多强」这一维已失效**。
恢复 LLM 打分（或补一次重打分回填）才是这一维复活的前提 —— 调阈值治不了。

```bash
cd backend/src_python
python3 signal_weight_diagnostics.py            # 人读报告
python3 signal_weight_diagnostics.py --json     # 机读
```

### ③ 出网通路体检 — 新增 `backend/src_python/net_path_probe.py`

实测（2026-11-10）：Binance / 百度 / OpenRouter **通**；
**Yahoo / FRED / GoogleNews / Ollama Registry 全被堵**（SSL UNEXPECTED_EOF / 超时）；
本机**无任何代理端口在监听** → `ollama pull` 直接 `connection reset by peer`。

```bash
cd backend/src_python
python3 net_path_probe.py            # 只测 + 给修复建议
python3 net_path_probe.py --write-env   # 探测到端口才写 ~/.trident/env (写前备份)
```

> 修复后必须重启常驻进程（launchd 不继承 shell 环境变量）：
> `launchctl kickstart -k gui/$(id -u)/com.trident.{api,bridge,engine}`

### ④ 无代理环境下行情全断 → 国内直连兜底 — 新增 `backend/src_python/cn_prices.py`

用户明确「**没有代理**」后，`feed_probe` 实测失败率 **46.7%**，且全集中在行情三源：
yahoo（429 / SSL handshake timeout）、yfinance（cookie/crumb 取不到）、asia（同样只走 yfinance）。
同机国内源**直连全通**：腾讯 `qt.gtimg.cn` 200/265ms、新浪 `hq.sinajs.cn` 200/210ms
（**必须带 `Referer: https://finance.sina.com.cn`**，否则 403）。

**关键不是加个兜底函数，而是接进驱动链**：`multi_source_fetcher` 此前只有
`yahoo / yfinance / equity(恒 None) / asia` 四个 PRICE fetcher —— 无代理环境下
18 个资产的 fallback 链**全断** → 价格全缺失 → 反思系统把「数据缺失」
当成判断错的理由，**掩盖真正的推理/因果错误**。

v324 接入四处（`cn` 源注册为 `multi_source_fetcher._cn_fetch_price`，
并加入 `feed_redund_policy.FEED_REDUND_POLICY` 全部 18 个资产的 PRICE 链末尾）：

| 位置 | 兜底顺序 |
|---|---|
| `multi_source_fetcher` | yahoo → yfinance → equity → asia → **cn**（反思系统主干）|
| `stock_prices.current_price_for_symbol` | Yahoo → Polygon → **cn**（建仓/结算价）|
| `asia_prices.fetch_price` | yfinance → **cn**（A 股指数）|
| `engine/prices.py` | 新浪/东财 → **cn**（XAU/WTI）|

实测：应用层取价 **20/20 成功**（接入前全废），24h 涨跌齐备
（NVDA −4.27% / XAU −0.57% / WTI −2.68%），与源端涨跌幅字段自洽。

**前收价解析的三个坑**（都踩过并修正，勿再改反）：

1. 腾讯 `hf_` 期货载荷 `parts[1]` 是**涨跌幅%**不是涨跌额 ——
   `(4090.72−4114.50)/4114.50 = −0.578% ≈ −0.58`；**昨收在 `parts[7]`**。
2. 新浪 `hf_` 同构，昨收也在 `parts[7]`（`parts[6]` 是**时间串**）——
   曾误取 `parts[6]` → 前收恒为 None。
3. 新浪 `gb_` 美股靠「现价 − 涨跌额」反推昨收，但涨跌额**可为负** ——
   `_to_float` 只收正数，需用 `_to_signed`。

其余格式：腾讯 `~` 载荷现价 `parts[3]` / 昨收 `parts[4]`；
新浪 A 股指数现价 `parts[3]` / 昨收 `parts[2]`；新浪港股现价 `parts[6]` / 昨收 `parts[3]`。

**K 线也要补**（否则前端只能「模拟反推」，回测看到的是假 K 线）：
腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get` 已接入为 `"cn"` history 源。

- 美股**必须带交易所后缀**（`usNVDA.OQ` 纳斯达克 / `usIBM.N` 纽交所）——
  不带只返回 2 根，故实现为「先试 `.OQ`，行数不足再试 `.N`」。
- 行格式 `[日期, **开, 收, 高, 低**, 量]` —— **不是 OHLC 顺序**，写反高低点就错位。
- ts 用**本地时区当日 00:00**（与 `quote_sources.fetch_naver_candles` 同口径）。
- 腾讯**不提供**分钟线与期货 K 线（param error）→ 本模块不覆盖。
- 自检：末根收盘价应与实时现价一致（实测美股 5/5 差 0.00%）。

### ⑤ 分钟线搬运没定时化 → 归因页 K 线停更（v324）

2026-11-12 复检：`ohlcv_intraday` 停在 11-07 08:59（落后 122 小时）。

```
market_ticks (实时 ✅) → kline_bars (实时 ✅)
  → ohlcv_intraday (停在 11-07 08:59 ❌)   ← 断点
    → 归因页 / 复盘分钟级 K 线
```

`ohlcv_intraday` 由 `backfill_intraday_from_kline_bars.py` 从 `kline_bars` 搬运，
但**只被手工跑过一次**，从未定时化；wrapper 的链路注释里也没有这一步。

**修法**：`~/.trident/wrappers/run_kline_aggregate_cron.sh` 补第四步
（纯本地、零网络、幂等 `INSERT OR IGNORE`，失败不阻断后续）。
实测一次搬运新增 **114,493 行**（129,771 → 244,302），数据到 11-12 11:04，
`/api/market/history/{BTC,WTI,XAU}?interval=5m` 随即返回真实分钟 bar。

⚠️ 另修 `backfill_intraday_from_kline_bars.py` 的 **dry-run 谎报 bug**：
`--dry-run` 硬编码 `inserted = 0` → 永远显示「新增 0」，会被误判成「没有新数据可搬」。
现按主键真算差集。

> ⚠️ **别把 `trade_decisions` 停滞误判成断链**：`fast_move_detector.load_tick_bars`
> 直接读 `market_ticks`（一直实时），**不经过 `ohlcv_intraday`**。
> 48h 窗口内仍能检出异动（实测 SOL 1 个 / WTI 1 个，只是已被 `_existing_keys` 去重）；
> 最近 40h 没有**新的**达阈值异动（`THRESH_FAST_1H_PCT=1.5`）→ 没有新的
> `critical_price_moves` → 没有新的 `trade_decisions`。**这是真实市场状态，不是故障。**

### ⑥ `#14 市场基本面` 的 FAIL 是抖动，不是能力缺失

首次跑判 FAIL（体制 0），复跑 **PASS 体制 7**（`deleveraging` / `state_fund_inflow` /
`cn_state_fund_exit` / `leverage_buildup` / `commercial_hedging` / AI 主题退潮）。
根因是 bridge 端点在 WAL 写锁竞争下偶发 >8s 超时 —— `capability_acceptance._http`
已有重试，但首轮仍可能落空。**判读 #14 前先复跑一次**。

### 测试

- `backend/tests/test_signal_weight_diagnostics.py`：27 用例，全离线。
- `backend/tests/test_net_path_probe.py`：11 用例，全离线。
- `backend/tests/test_cn_prices.py`：35 用例，全离线（解析函数 + 兜底接线）。
- 相邻回归（`test_probe_health` / `test_v322_breaker_epoch_and_gate_freeze` /
  `test_gate_deferred_v320` / `test_minimax_weight_override` + 新增两组）：**106 passed**。

> ⚠️ 跑全量回归用 `python -m pytest tests/ -q`（**不要**在 backend 根目录裸跑 `pytest`）——
> `scripts/test_budget_gate.py` 是脚本不是测试，收集期 `raise SystemExit(1)` 会让 pytest
> 直接 INTERNALERROR（"no tests ran"）。

## 常用命令

```bash
# 启动引擎（在 backend/src_python 下）
python engine.py
# 启动 API（在 backend/src_python 下）
uvicorn api_server:app --host 127.0.0.1 --port 8000
# 测试（在 backend 下，全离线；v170 起 257 用例）
python -m pytest tests/ -q
# 数据对齐扫描（直接 print 11 条 link 状态，1=any, 0=无孤儿）
python -c "import sys; sys.path.insert(0,'src_python'); import db, integrity_check; \
  conn=db.get_connection(); conn.row_factory=None; \
  sys.exit(integrity_check.run_and_print(conn))"
# 前端
cd frontend && npm run dev        # 开发
cd frontend && npm run build      # 生产构建
# 语法快速检查
python -m py_compile <file>
```

Windows 一键开发启动：`scripts\dev_start.bat`。

## v173–v176 增量摘要

- **v176 风险指标**（`src_python/risk_metrics.py` + `history_review.news_trade_detail.aggregate`）：
  `compute_drawdown`（max_dd / current_dd / peak_pnl / trough_pnl）+ `compute_sharpe`（年化，可配 `periods_per_year` / `risk_free_per_period`）+ `compute_winrate`（pnl>0 胜 / <0 负 / ==0 不计分母）+ `compute_portfolio_metrics`（聚合）。`history_review` 的 `aggregate` 字段新增 `max_drawdown / current_drawdown / sharpe` 三个键，供 LLM 归因分析与 weekly review 使用。

## v233 规律发现·深入自审（`pattern_audits`）

**入口与职责**（`backend/src_python/review_agent.py`）：
- `_PATTERN_AUDIT_MAX_PATTERNS=3` / `_PATTERN_AUDIT_MAX_DEPTH=5`：单次报告最多 3 patterns × 5 深度 = 15 次 LLM 调用，超出后 `stop_reason=max_depth`。
- `_audit_pattern_deep(pattern, trades, agg, *, llm_call=None)`：从 `_PATTERN_AUDIT_FIRST_QUESTION`（"涨跌驱动因子是什么"）出发，每轮把 LLM 返回的 `next_question` 喂回去递归，直到：
  1. LLM 把 `next_question` 置 null → `stop_reason=no_further_why`
  2. 达到 `_PATTERN_AUDIT_MAX_DEPTH` → `stop_reason=max_depth`
  3. 答案包含"反转 / 矛盾 / 推翻"标记 → `stop_reason=contradicted_by_evidence`
  4. 输入 patterns 为空 → 立即返回空 skeleton
  5. LLM 返回 None / 无法解析 → `stop_reason=llm_failed`（区别于 `no_further_why`）
- `_audit_patterns_batch(...)`：被 `_review_block` 在 `summary` 出来之后统一调用；`pattern_audits` 已存在则跳过（避免重复）。
- `_validate_audit_evidence(audit, trade_ids)`：扫 evidence 里所有 `#<数字>`，不在 `trade_ids` 集合 → 标 `fabricated_evidence`，强制 `confidence=0.3` + 追加"疑似编造"caution。若 LLM 已主动 confidence < 0.4，不再二次覆盖（保留 LLM 原 caution）。
- `_fallback_summary(...)` 自动挂 skeleton：`stop_reason=no_llm` + `confidence=None`，前端 `audits?.length` 守卫避免老数据反序列化报错。

**前端**（`frontend/components/replay/review-panel.tsx`）：
- `PatternAudit` 类型 + `ReviewReport.summary.pattern_audits?: PatternAudit[]`
- `<PatternAuditItem>`：claim 头 + 置信度徽章（高/中/低 三档配色）+ stop_reason 徽章 + 引用存疑徽章 + 醒目 caution callout（contradicted/fabricated/no_llm 三色）+ why_chain 折叠 + 三列 (适用/反条件/过拟合) + 证据折叠 (fabricated 划线)
- `<PatternAuditBlock>`：空 audits 返回 `null`，否则在 规律发现 ListBlock 下方渲染整组

**测试**：`tests/test_review_pattern_audit.py` 共 18 用例，全部离线 green。相邻 5 个测试文件（test_review_pattern_audit / test_ai_worker_review / test_fallback_rigor / test_v200_p3_fallback_honesty / test_discovery_v227）共 70 通过 1 跳过。

## v316 Crowd Pulse — 二级市场人群画像 / 权威度 / 月榜

> 回答「现在二级市场的人在关注什么、是哪一群人」。**数据全部真实抓取**，每条原始条目
> 带 source+external_id+url+collected_at，可逐条回溯原页面；解析失败一律返回空，
> **绝不用占位/编造内容填充**（采集失败如实记入 `crowd_source_runs.errors_json`）。

**模块**（`backend/src_python/`）
| 文件 | 职责 |
|---|---|
| `crowd_sources.py` | 5 个源的真实抓取与解析（`parse_*` 是纯函数，离线可测） |
| `crowd_pulse.py` | 归一化 / 话题归并 / 权威度 / 人群画像 / 落库 / 读榜 |
| `crowd_worker.py` | 定时采集 worker（launchd `com.trident.crowd-pulse`，间隔 60min） |

**数据源**（2026-10-30 实测 HTTP 200）
| 源 | 端点 | 覆盖 |
|---|---|---|
| `baidu_hot` | `top.baidu.com/api/board?tab=realtime` | 大众财经关注度（51 条） |
| `baidu_finance` | `top.baidu.com/api/board?tab=finance` | 财经榜（30 条） |
| `ths_hot` | `dq.10jqka.com.cn/fuyao/hot_list_data/...` | A股散户 + 概念标签（100 条） |
| `coingecko` | `api.coingecko.com/api/v3/search/trending` | 加密社区 + 贵金属（28 条） |
| `gnews` | `news.google.com/rss/search?q=...` | 财经媒体（100 条，带媒体名） |

> **被反爬拦住、故未纳入**（不做假数据替代）：reddit / stocktwits / 雪球 / 知乎 / 微博（401/403）。
> **抓取注意**：必须走 `requests` 且显式带 `Accept` 头 —— urllib 默认不发 `Accept`，
> 百度/Google/CoinGecko 会回**空体但 HTTP 200**，表现为"解析失败返回空列表"。

**表**（schema 只在 `db.py` 定义，铁律 1）
`crowd_source_items`（原始条目）· `crowd_topic_daily`（话题×日×源快照）·
`crowd_monthly_rankings`（月榜+权威度拆解）· `crowd_audience_segments`（人群画像 9 维）·
`crowd_source_authority`（源权威度）· `crowd_source_runs`（采集运行日志）

**三个关键设计（都是实测踩出来的）**
1. **热度必须按源归一**：同花顺 `rate` 是 50 万级、百度只有 0~1、Google News 恒 0。
   不归一会让榜单退化成"同花顺热度榜"（platform 维度 share 变成 1.000 / 0.000）。
   统一用 `1/rank` 作可比热度，原始值仍存 `crowd_source_items.heat` 可回溯。
2. **交叉验证按"平台"不按"榜单"**：百度实时榜 + 百度财经榜是同一平台，只算 1 次。
3. **题材级聚合层**：细粒度话题（个股/完整文本）跨平台几乎归并不上，
   另写 `theme:<题材>` 聚合键 —— 权威度的跨平台权重靠它才落得了地。

**权威度公式**（`authority_score`，每条带 `explain` 可追问"凭什么"）
`8.0×log1p(热度) + 10.0×跨平台数 + 2.0×持续天数 + 6.0×动能 + 4.0×媒体权重`

**人群画像 9 维**：platform / market / asset_class / theme / sentiment /
time_window / influence_tier / media_tier / media_channel

**API**
`GET /api/crowd/ranking?month=&limit=&kind=&sort=`（sort: authority|heat|momentum）·
`GET /api/crowd/audience?month=&dimension=` · `GET /api/crowd/sources?month=` ·
`GET /api/crowd/items?limit=&source=` · `POST /api/crowd/collect`（手动触发采集）

**配置**（`config.py` + `.env.example`）
`CROWD_PULSE_ENABLED` / `CROWD_PULSE_SOURCES` / `CROWD_PULSE_INTERVAL_MINUTES` /
`CROWD_PULSE_GNEWS_QUERY` / `CROWD_PULSE_TOP_N` / `CROWD_PULSE_HTTP_TIMEOUT`

**测试**：`backend/tests/test_crowd_pulse.py`（15 用例，全离线，含"解析失败返回空"
与"share 不被单源独占"两条底线回归）。

## v323 · 两层验收（结构层 / 统计层）

**问题**：`capability_acceptance.py` 同时验"有没有"和"准不准"，导致每次体检都大面积红——
准确率是统计量，n<100 时 54.7% 与 60% 没有统计差异；口径一改历史样本作废，看起来又退步；
同一件事（如事件方向命中率）在页面/脚本/临时查询里有三套口径（日格平均 33.5% vs 事件加权 50.2%）。

**解法**（`docs/CAPABILITY_SCORECARD.md`）：

```bash
cd backend/src_python
python3 capability_scorecard.py                 # 两层总览（结构层自动跑，约 2 分钟）
python3 capability_scorecard.py --no-structure  # 只看统计层（秒级）
```

- **结构层**：复用 `capability_acceptance.py`（26 项，PASS/FAIL），**必须全绿**；退出码由它决定。
- **统计层**：10 个指标（S1–S10），只报 **值 + 95% Wilson 区间 + n + 相对毕业线**，状态只有 `达标/接近达标/未达线/样本不足`，**永不 FAIL**。
- **口径指纹**：每个指标对分子/分母/窗口/门槛做 hash；口径一改标记「口径变更」，旧对比自动作废。
- **快照**：`backend/runtime/capability_scorecard/history.jsonl`，报告显示 Δ值 / Δn。

**纪律**：
1. 统计层指标**不许判 PASS/FAIL**，也不许进 CI 卡口；它只用来排优先级。
2. 改指标定义必须改 `METRICS` 里的 `definition` 字符串 → 指纹变化 → 历史对比作废，想"改分母让它变绿"会被抓到。
3. 样本 < gate 一律写"样本不足"，禁止下结论。
4. 第 14/15 项结构判据依赖 bridge，跑结构层时**不要加 `--no-http`**（否则假 FAIL）。

## v379–v384 · 新模块索引（语义记忆 / 反事实 / 问库 / 成绩单 / 处置）

| 模块 | 作用 | 只读? | 入口 |
|---|---|---|---|
| `semantic_memory.py` | 反思文本 → 字符 n-gram TF-IDF 向量，按品种×事件×新闻召回 top-5 注入 prompt | 是 | `python semantic_memory.py --asset XAU --query "..."`；`GET /api/v1/memory/search` |
| `counterfactual.py` | 单笔决策重放「实际/不进场/反向/延迟/半仓」五条路径 | 是 | `python counterfactual.py 11555`；`GET /api/v1/counterfactual/{id}` |
| `nlq.py` | 自然语言问库：意图路由 + 白名单参数化 SQL（**禁止 LLM 自由写 SQL**） | 是 | `python nlq.py "黄金最近为什么跌"`；`GET /api/nlq?q=` |
| `scorecard_share.py` | 自包含 HTML 成绩单（KPI/漏斗/闸门/错因/漏检/来源） | 是 | `python scorecard_share.py` → `docs/信号成绩单-YYYY-MM-DD.html` |
| `decision_actions.py` | 处置账：静默品种 / 忽略该笔 / 反手影子 | 写 | `POST /api/v1/decisions/{id}/action` |

**注意**
1. `decision_actions` 表由 `db.py` 迁移创建；`ai_decisions.quarantined_at/quarantine_reason`
   在线上库早已存在但 schema 从未声明，v383 补成正式迁移（全新库不再缺列）。
2. 静默在 `decision_writer.write_decision` 里统一拦截 —— 不要在别处再加过滤，否则三条写入口口径会再次分裂。
3. 反事实/覆盖查询遇到没有 K 线的品种一律返回 `no_kline` + `available_timeframes`，
   **禁止拿相邻 K 线冒充**（v378 的教训）。
4. `nlq.py` 的 ts 阈值必须走 `_ts_threshold()` 自适应：库内 `critical_price_moves.ts` 是秒、
   `kline_bars.ts` 是毫秒，写死单位会把整张表过滤成空。

## 铁律

1. **schema 迁移只改 `backend/src_python/db.py`**。engine/api_server/init_db 都调用 `db.migrate()`；禁止在别处写 CREATE/ALTER。迁移必须幂等（列存在性检查，不用 try/except 吞错）。
2. **密钥只进 `.env` 不进代码**。新配置项加进 `config.py` + `.env.example`。任何 URL/Key 硬编码都是事故。
3. **不动生产 DB 文件** `backend/trident_event_bus.db`（及其 .bak）。测试一律用 tmp_path/内存库；conftest.py 的 `temp_db` fixture 就是干这个的。
4. **engine 业务逻辑改动需同步 `backend/tests/`**。改过滤规则/prompt/阈值/聚合逻辑时，先确认现有 257 个用例仍绿，并为新行为补用例（保持离线可跑，网络/LLM 一律 mock 或不测）。
5. **frontend 不准开 `ignoreBuildErrors`**（next.config 里的 typescript/eslint 忽略项）。类型错误就是错误，构建必须过。
6. **不改业务逻辑语义**：过滤关键词表、LLM prompt、阈值数值（VIP_SCORE_BOOST、_AGG_*、IMPACT_THRESHOLD、BATCH_SIZE）的修改属于产品决策，不夹带在重构提交里。
7. 提交前跑 `python -m pytest tests/ -q` 和涉及文件的 `py_compile`。

## v342 判断链颗粒度 — 结构因子进入闸门 + confidence 三量（纯增量）

### ① 推理里的结构，此前没落成数（真缺口）
prompt 的「盘面结构分析 v2」要求算价位位置 / ATR 分档 / 周期共振 / 结构评分 −2~+2，
但落库只有 `market_confirmation` 三值枚举，实测 98.6% 是 `unknown`。
→ 推理里算过的结构，到闸门时已经丢了。

**修复**：新增 `structure_factor.py`（纯函数、零依赖），用快照里已有的
`price / stats_7d.highest|lowest|trend|atr_pct / mtf.4h|1h` 重算 `bias ∈ [-2,+2]`：
`7d趋势(±1) + 4h/1h 共振(±0.5) × 位置阻尼(贴上沿不追多) × 波动倍数(低波动 1.2 / 高波动 0.8)`。
**取不到 ≠ 中性**：缺任一字段 → `available=False`，不拿 0 占位。

### ② 闸门接入（权重变动属产品决策，单列在此）
`DIRECTIONAL_WEIGHTS`：`market_confirmation` 0.25 → 0.10，新增 `structure` 0.15，权重和仍为 1.0。
- 结构分按 AI 方向对齐：结构偏多 + 新闻看多 = 支持；结构偏空 + 新闻看多 = 反对。
- `_CONFIRMATION_PROXY_WEIGHT` 0.12 → 0.05（跟随主权重减半；否则可得权重和 >1，coverage 读到 1.02）。
- 结果新增 `structure` / `confidence_split` 两个字段，**原有字段一个不动**。

实测（sentiment=0.6 BUY）：多头中位 +0.557→+0.618；贴上沿追多 +0.557→+0.542；
空头结构 +0.125→−0.013；无结构字段老快照 +0.471→+0.471（完全不变）。

### ③ confidence 拆三量（不动原值）
新增 `confidence_split.py`：`direction`（可得因子权重占比，有矛盾扣分）/ `reliability`
（胜率 Wilson 95% 下界，样本 <10 返回 None）/ `odds`（impact_curve 预期波动 / ATR）。
`unified_confidence` 的 0-100 原值不变，只是另存三量供复盘分开看。

### 测试
```
python -m pytest tests/test_v342_judgment_grain.py tests/test_decision_guard_v318.py \
                 tests/test_v322_direction_guard.py tests/test_decision_evidence.py -q
→ 57 passed（其中新增 15 条）
```
注意：本机系统 python 缺 fastapi 等运行依赖，全量 `tests/` 收集会报 85 个 import 错误，
与本次改动无关；判断链 4 个测试文件全绿。

### 仍未做（要动写入口/结算，风险更高，另开版本）
- `confidence` 本身仍来自 `algo_score`（写入端未改成三量合成）
- 三条 `trade_decisions` 写入口径未统一
- 多周期结算（日内判断目前用 5 天结算）
- `surprise` 因子（econ_calendar 有 actual/forecast，未进判断）

## v342 续 · trade_decisions 写入口径统一（decision_writer）

### ① 三个写入点、三种口径（真缺口）
| 写入点 | status | score/confidence | 事前权重 |
|---|---|---|---|
| `trade_decisions.log_decision` | ✗ | ✗ | ✓ |
| `decision_engine.propose_one` | ✓ | `algo_score` 冒充 confidence | ✗ |
| `bridge_server./api/decision/record` | ✓ | 只塞 `context_json`，注释写明「不再落库」 | ✗ |

附带查出一个真 bug：**`status` 列从未被迁移建过**，只在两条 INSERT 里出现 →
全新库跑这两条路径直接 `no such column: status`。

### ② 修复
- `db.py` 迁移补列（幂等、可空）：`status / decision_source / score / confidence /
  structure_score / confidence_detail`。不回填、不改老行。
- 新增 `decision_writer.py` 作为**唯一写入口**：一个 INSERT、列集合固定、缺值写 NULL；
  `entry_price` 缺失走 `db.resolve_entry_price`（查不到留 NULL）；越界夹住
  （score ±1 / confidence 0-100 / structure_score ±2）；落库后统一写事前权重
  （`trigger_kind='insert'`, `now_ts=decision_ts`）。
- 三个调用点改为调用它，`decision_source` 分别是 `log_decision` / `algo_move` / `bridge_api`。
- **口径修正**：`algo_score`（方向分 -1..1）落到 `score`，`confidence` 留 NULL，
  不再把方向分当 0-100 置信度；bridge 的 score/confidence 从 context 提为显式列，
  context 里那份保留（老读者不受影响）。

### ③ 不变量（刻意没动）
动作、拒绝原因、新闻回链、结算口径、`context_json` 结构、`log_decision` 签名与权重时机——全部照旧。

### 测试
```
python -m pytest tests/test_v342_decision_writer.py tests/test_v342_judgment_grain.py \
                 tests/test_decision_guard_v318.py -q     → 45 passed
```
新增 9 条（`tests/test_v342_decision_writer.py`）：三源列集合一致 / algo_score 不冒充
confidence / bridge 落列 / 老签名兼容 / 无行情留 NULL / 越界夹住 / 空 asset 抛错。

基线对照（把本版 4 个文件 stash 掉再跑）：失败集完全一致（15 failed / 96 passed），
说明本版**没有引入新失败**；那 15 条是环境与 python3.14 兼容问题（asyncio 用法、
probe 名单、静默吞异常审计命中 `build_series_registry.py`、`check_integrity.py`）。

### 遗留
- 存量行 `decision_source` 仍为 NULL；按 guard/decision_type 推断属于猜测，未做
- 三条路径的候选去重仍不统一（只有 `decision_engine` 有 `_is_already_decided`）

## v342 续 · 多周期结算（multi_horizon_settle）

### ① 缺口
每条决策都带 `expected_horizon`（intraday / 1-3d / 1w+），结算却只有 5 天一个窗口
（`decision_engine.HORIZON_S`）→ 日内判断被 5 天结算，"方向对、节奏错"被判成错，
错还进 `trade_reflections_v2` 污染 E 码。

### ② 做法（纯增量）
- 新表 `decision_outcomes_horizon`（PK `decision_id,horizon`，幂等）：15m/1h/4h/1d/5d 各一条，
  存 `fwd_price / outcome_pct / pnl_pct / hit / status / note`。
- 新模块 `multi_horizon_settle.py`：
  - 15m/1h/4h/1d 用 `ohlcv_intraday`（容差 15m/30m/2h/12h），日线回退；
  - **5d 直接复用 `db.forward_price(settle_days=5)`**，与主结算同源，数字必须一致；
  - T+1 品种在 `cost_model.earliest_exit_ts` 之前 → `not_executable`（AAPL 实测约 26h，
    连 1d 都不可执行）；
  - `|收益|>90%` → `implausible`，不写 hit；
  - 取不到价 → `no_price`，`fwd_price/outcome_pct` 留 NULL。
- `run_settle_only()` 结算完主口径后顺带 `run_once()`；失败只留日志，不阻断主结算。

### ③ 不变量
`settle_one` / `trade_decisions.actual_outcome_pct` / `trade_reflections_v2` 口径一字未改；
5 天仍是主口径；老数据不回填（无分钟线的历史决策如实记 no_price）。

### 测试
```
python -m pytest tests/test_v342_multi_horizon.py -q                          → 9 passed
python -m pytest tests/test_v342_*.py tests/test_execution_cost_settlement.py \
                 tests/test_decision_guard_v318.py -q                        → 76 passed
```
新增 9 条：5 周期落库与 hit 方向 / 未到期不写 / 幂等 / 无价不猜 / 异常拒写 /
T+1 不可执行 / SELL 净收益判 hit / run_once 批量。

### 遗留
- `/api/decision/horizons` 端点未挂（`read_horizons()` 已实现）
- 前端复盘页按周期分列未做
- E 码反思未加 horizon 维度（"方向错"与"节奏错"仍混在一起）

## v342 续 · 预期差因子（surprise）

### ① 缺口
`econ_events` 存了 actual/forecast/previous，判断链从没读 → CPI/非农方向上
"超预期 0.1" 与 "超预期 2.0" 被当成同一件事。

### ② 关键取舍：只做反方证据，不给方向
同一个超预期对不同资产方向不同（CPI 热→风险资产偏空；失业率上行→偏多），
写死符号等于制造假精度。因此 **`DIRECTIONAL_WEIGHTS` 一字未改**，只做：
- `norm`（超预期归一 -1..+1，尺度优先取 |forecast−previous|）
- 「方向相反 且 |norm|≥0.35」→ 方向分折减（最多 −25%）+ 记 `news_vs_surprise` 矛盾

### ③ 实现
- 新增 `surprise_factor.py`：`parse_number`（%/K/M/B/括号负数/N/A→None）、`compute`、
  `polarity`（**只给表内已知指标，未知一律 None**）、`find_event`（±30min 窗口 + 标题关键词）、
  `enrich` / `enrich_by_news_id`、`conflict_with_action`
- `decision_guard.py`：读 `row["surprise"]` → 折减 + 矛盾；新增输出 `surprise` / `pre_surprise_score`
- 接线：`engine/ai_worker.py`（落库前真实闸门）与 `api_server._factor_result`（因子预览）

### ④ 不变量
无 `surprise` 输入时闸门输出逐位不变（`pre_surprise_score == final_score`）；
`econ_events` 只读，不改 schema。

### 测试
```
python -m pytest tests/test_v342_surprise_factor.py -q                        → 13 passed
python -m pytest tests/test_v342_*.py tests/test_decision_guard_v318.py -q    → 67 passed
python -m pytest tests/test_v336_optimizations.py -q                          → 66 passed
python -c "import engine.ai_worker"                                           → 导入 OK
```

### 遗留
- 极性表未覆盖 WTI / 美债等（宁缺不猜）
- `econ_events` 本身样本少（自述"40 天 26 个可交易事件"），覆盖是下一个瓶颈
- 前端未展示预期差；阈值 0.35 / −25% 是经验值，应随样本用 gate 校准

## v342 续 · 复盘接入 + 错因拆分

### ① 读数出口（三处）
- `GET /api/decision/{id}/horizons` — 单条决策的 15m/1h/4h/1d/5d 读数
- `GET /api/horizons/stats?days=&asset=` — 按周期聚合胜率/平均净收益
- `python3 scripts/report_horizons.py [--days 30] [--asset BTC] [--json]`

口径：胜率分母**只算 `status='settled'`**；`no_price`（没取到价）与
`not_executable`（T+1 品种时点不可平仓）单列，**不算判断错**。
样本 < 30 时输出自带 ⚠️ —— 先有量再谈结论。

### ② 方向错 vs 节奏错（`horizon_reflection.py`）
旧反思对"SELL 亏了"一律记判断错，但：
- 5d 也亏 → `direction_wrong`：该改推理
- 短期亏、5d 赚 → `timing_wrong`：该改入场时点，**不要推翻方向**
- 原始涨跌全正但净收益≈0 → `cost_drag`
- 1d/5d `not_executable` → `horizon_mismatch`：周期选错，不是判断错

判定优先级：`horizon_mismatch` > 长周期判方向（5d 退 1d）> 节奏 > 成本 > partial/no_data。
落库 `decision_horizon_reflection`（decision_id 主键，幂等覆盖写），
挂在 `multi_horizon_settle.run_once` 末尾；判定失败只记日志，不阻断结算。
`db.migrate` 里同步 `horizon_reflection.init_schema`。

### 测试
```
python -m pytest tests/test_v342_horizon_endpoint.py -q     → 4 passed
python -m pytest tests/test_v342_horizon_reflection.py -q   → 8 passed
python -m pytest tests/test_v342_*.py tests/test_decision_guard_v318.py \
                 tests/test_trading_signals_endpoint.py -q  → 104 passed
```

### 遗留
- `/review` 独立"周期矩阵"区块未接（需改 `_spec.ts` + 面板注册 + vitest）
- 真实库里带 `entry_price` 的决策很少（本次仅 2 条），样本是当前瓶颈
- 阈值 −0.5% / +0.5% / 0.2% 为经验值，应随成本分布校准

### ③ 前端「周期矩阵」（v342 收尾）
- `/review` 新增 section：`_spec.ts` 的 `horizonMatrix`（id `horizon-matrix`，label 周期矩阵，
  modes 仅 `all`/`journal`）
- 面板 `components/replay/horizon-matrix-panel.tsx`：读 `/api/horizons/stats?days=`，
  一行一个持有期，5 天行高亮；无样本胜率显示「—」而不是 0%；样本<30 标 ⚠️；
  底部明写「无价 / 不可执行」不计入分母的理由
- 验证：`npx tsc --noEmit` 无报错；`npx vitest run app/review` → 152 passed
- 主库现状（只读核对）：`ai_decisions` 259 条中 BUY 仅 2 条、`sentiment_score=0` 占 231 条、
  `market_confirmation='unknown'` 258 条 → **样本瓶颈在上游情感分退化，不在结算层**

### ④ 降级文案跟着品种走（v342，修的是归因不是判定）
- 实测：168 条核心品种决策里 **93 条（55.4%）** 的 `reasoning_path` 写着
  「目标资产 NONE…该标的**不在**核心监控清单」，实际品种是 XAU/WTI/BTC
- 根因：降级兜底按 NONE 写文案 → `ASSETFIX`/`MAINFIX` 改了 `target_asset`
  却没重写文案 → 反思把「LLM 空输出」学成「品种不在监控范围」
- 修复：`rerender_degrade_text_for_asset(result, asset)`，接线两处；
  **只改文案与归因，不动 `suggested_action` / `sentiment_score`**
- 坑：检测串必须写 `**不在**核心监控清单`，原文中间隔着 markdown `**`
- 只读核查脚本：`python3 scripts/audit_asset_scope_text.py`
- 历史 93 条**未自动回填**（改历史文本 = 改证据），回填方案待用户拍板

### ⑤ 降级探针：把"没方向"量出来（v342）
- 实测 272 条决策：真实八段 **31 条（11.4%）**、完整降级·范围外 **191 条（70.2%）**、
  只有 reasoning 摘要 50 条（18.4%）→ **89% 情感分为 0 是兜底造成的，不是模型偏好**
- 新增 `llm_degrade_probe.py`：表 `llm_degrade_probe`（archetype / raw_len / raw_head /
  path_len / rsn_len / action / score）；`note_raw` 在 `_presanitize_for_schema` 入口登记
  原始输出长度与开头 80 字，落库侧 `record` 写入；失败静默，不影响主流程
- `raw_head` 用来事后区分：空输出 / `<think>` 未闭合 / JSON 截断 —— 不用开 dump 重跑
- 只读脚本：
  - `python3 scripts/report_llm_degrade.py`（历史基线 + 探针分布 + raw 样本）
  - `python3 scripts/audit_asset_scope_text.py`（93 条文案错位清单）
- 导出：`docs/v342-降级文案错位清单-2026-11-16.csv`（93 行，待用户决定是否回填）

### ⑥ 上游空转定位（v342）
- 用 `ai_call_stats` 排除了两个嫌疑：`purpose='main'` 近 3 天 195 次**全部 ok**、
  `tokens_out` 800–2000（max 2260，远低于 4000）→ **不是截断、不是主链路超时**
- 429(737) / ReadTimeout(325) 打的是 `gate`/`anomaly`/`advisor` 旁路，不是出决策那一路
- 真正断点：模型给了 800–2000 token，但**缺 `target_asset`、没写八段** →
  下游只能按 NONE 记账 → 70.2% 降级
- 修复两处（不改判定）：
  1. `is_empty_llm_output()` + 空输出**补一次**（只一次，短 HOLD JSON 不触发）
  2. `target_asset` 进"必填缺口"，让既有 schema 补全 retry 真正触发
- 下一步（需重启 worker）：看真实八段占比是否回升；若 `raw_head` 显示
  "有内容但无 target_asset"，则应在 prompt 把 `target_asset` 前置，而不是继续加 retry
