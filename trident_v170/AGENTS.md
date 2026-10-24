# AGENTS.md — AI 协作者项目约定

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

## 铁律

1. **schema 迁移只改 `backend/src_python/db.py`**。engine/api_server/init_db 都调用 `db.migrate()`；禁止在别处写 CREATE/ALTER。迁移必须幂等（列存在性检查，不用 try/except 吞错）。
2. **密钥只进 `.env` 不进代码**。新配置项加进 `config.py` + `.env.example`。任何 URL/Key 硬编码都是事故。
3. **不动生产 DB 文件** `backend/trident_event_bus.db`（及其 .bak）。测试一律用 tmp_path/内存库；conftest.py 的 `temp_db` fixture 就是干这个的。
4. **engine 业务逻辑改动需同步 `backend/tests/`**。改过滤规则/prompt/阈值/聚合逻辑时，先确认现有 257 个用例仍绿，并为新行为补用例（保持离线可跑，网络/LLM 一律 mock 或不测）。
5. **frontend 不准开 `ignoreBuildErrors`**（next.config 里的 typescript/eslint 忽略项）。类型错误就是错误，构建必须过。
6. **不改业务逻辑语义**：过滤关键词表、LLM prompt、阈值数值（VIP_SCORE_BOOST、_AGG_*、IMPACT_THRESHOLD、BATCH_SIZE）的修改属于产品决策，不夹带在重构提交里。
7. 提交前跑 `python -m pytest tests/ -q` 和涉及文件的 `py_compile`。
