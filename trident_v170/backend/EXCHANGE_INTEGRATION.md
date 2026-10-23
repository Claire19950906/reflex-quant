# 接交易所设计稿（P2-15）

> 当前系统：信号生成 + paper trading 全闭环。实盘下单层留有 `risk_manager.evaluate_trade`
> 作为最后一道门。本文档定义从 paper → 实盘的接入设计，不包含具体代码实现（仅设计骨架）。

---

## 1. 目标

在不破坏 paper 链路的前提下，提供**安全的实盘下单接入**：

- 仅下单**策略层通过的风控**信号（status=ACTIVE / suggested_action ∈ {LONG, SHORT, BUY, SELL}）
- 单一品种单笔规模由 `risk_manager.evaluate_trade` 决定（**服务端权威上限**）
- 用户在前端独立输入交易所 API key（**后端不持久化裸密钥**，仅 server 内存 session）
- 强制 dry-run 模式开启，演练 7 天后再切真单

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│ ai_worker → ai_decisions（status=ACTIVE, settled=0）         │
│      │                                                         │
│      ▼                                                         │
│ trade_executor (new, paper + live 双模式)                      │
│      │  ├── read ai_decisions (every 5s)                      │
│      │  ├── risk_manager.evaluate_trade() → {allow, size}     │
│      │  ├── if allow → order_adapter.place_order()             │
│      │  └── write signal_performance / ai_decisions entry_price│
│      ▼                                                         │
│ order_adapter (exchange-specific: binance / okx / bybit)      │
│      │                                                          │
│      ▼                                                         │
│ Binance Futures Testnet (默认) → Mainnet                     │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 关键模块

### 3.1 trade_executor

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | enum {`paper`, `live_dry_run`, `live_real`} | 默认 `live_dry_run`（演练模式，不真下单只记录意图） |
| `poll_interval_sec` | int=5 | 轮询 ai_decisions 间隔 |
| `max_concurrent_orders` | int=3 | 同时持仓上限（per asset 全局） |
| `cooldown_after_loss_min` | int=15 | 亏损后冷却时间 |
| `whitelist_assets` | list[str] | 仅允许交易的品种（默认 BTC, ETH, SOL, XAU, WTI） |

### 3.2 order_adapter (交易所适配)

```
class OrderAdapter(ABC):
    def place_order(symbol, side, qty, price_type, stop_loss, take_profit) -> OrderResult
    def cancel_order(order_id) -> bool
    def get_position(symbol) -> Position
    def get_balance(asset) -> float
```

实现：
- `BinanceFuturesAdapter`（优先，流动性最好）
- `OKXSwapAdapter`（已有 quote_sources.py OKX REST 客户端）
- `BybitAdapter`（备用）

### 3.3 风控接入

每次下单前**强制**调用 `risk_manager.evaluate_trade()`：

```python
gate = risk_manager.evaluate_trade(
    asset=decision['target_asset'],
    action=decision['suggested_action'],
    suggested_size_usdt=position_size_usdt,
)
if not gate['allow']:
    log + skip
else:
    actual_size = min(position_size_usdt, gate['max_allowed_size_usdt'])
    adapter.place_order(...)
```

---

## 4. 状态机

```
paper_trading_run.status:
  CREATED → RUNNING → PAUSED → STOPPED → ARCHIVED

trade_executor.lifecycle (per decision):
  PENDING → RISK_PASSED → RISK_FAILED
        → ORDER_PLACED → ORDER_FILLED → SETTLED (with is_correct, forward_pnl)
        → ORDER_REJECTED → CANCELLED
```

---

## 5. 安全护栏（7 条强制规则）

1. **API key 不持久化**：仅存 session 内存，重启需重输；不在日志/DB 出现
2. **白名单品种**：trade_executor 启动时硬编码 whitelist，DB 里写死的也算在白名单
3. **单笔硬上限**：MAX_POSITION_USDT=5000（与 risk_manager 一致）
4. **演练期 7 天**：live_dry_run 必须 ≥ 7 天且零异常，才能切 live_real
5. **熔断开关**：前端一键 STOP_ALL，立即撤所有单 + 拒接新单
6. **IP 白名单**：仅允许固定出口 IP 调用交易所 API（在 adapter 层 enforce）
7. **审计日志**：每笔下单记录到 `trade_audit_log` 表（含时间/品种/规模/原因/operator）

---

## 6. 数据库新增表（草案）

```sql
CREATE TABLE trade_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    decision_id INTEGER,
    operator TEXT NOT NULL,           -- "system" / "user:<name>"
    asset TEXT NOT NULL,
    action TEXT NOT NULL,
    intended_size_usdt REAL,
    actual_size_usdt REAL,
    risk_gate_reason TEXT,
    adapter TEXT NOT NULL,            -- "binance_futures_testnet"
    order_id TEXT,                    -- 交易所返回的 order_id
    status TEXT NOT NULL,             -- "placed" / "filled" / "rejected"
    error TEXT
);

CREATE TABLE exchange_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator TEXT NOT NULL,           -- user / system
    exchange TEXT NOT NULL,           -- binance / okx / bybit
    api_key_encrypted BLOB NOT NULL,  -- Fernet encrypted
    api_secret_encrypted BLOB NOT NULL,
    passphrase_encrypted BLOB,        -- okx 才有
    created_at INTEGER NOT NULL,
    revoked_at INTEGER                -- NULL = active
);
```

---

## 7. 切换流程（运维手册）

### Day 0-7: live_dry_run
1. 用户在 UI 输入 Binance Testnet API key
2. `trade_executor.mode = live_dry_run`（默认）
3. 每天检查 `trade_audit_log` 与 `signal_performance.forward_pnl` 是否一致
4. **不真下单**：只把下单请求写到 `paper_trading_orders` 模拟表

### Day 8+: 切 live_real
1. 前端 UI 显式点击「切真单」按钮 → 二次确认
2. 重启 `trade_executor.mode = live_real`
3. 前 24 小时开 0.5x 规模上限（前端额外乘子）
4. 第二天检查：成交率/滑点/盈亏 → 决定是否恢复 1.0x

---

## 8. 已知风险 & 缓解

| 风险 | 缓解 |
|---|---|
| 滑点 > 1% | 下单前 0.5s 拉盘口检查；若 spread 异常 → 自动降规模或取消 |
| API key 泄漏 | 不持久化 + Fernet 加密 + IP 白名单 |
| 交易所 downtime | adapter 自动重试 3 次（指数退避），失败 → RISK_FAILED + STOP_ALL |
| 决策延迟 | trade_executor 5s 轮询；LLM 决策时刻 t，订单最迟 t+10s 发出 |
| 同一信号重复下单 | ai_decisions.id + ORDER_KEY UNIQUE（防重） |

---

## 9. 接入路线图（顺序依赖）

| 阶段 | 任务 | 依赖 | 预估 |
|---|---|---|---|
| Phase 1 | `BinanceFuturesAdapter`（testnet）+ `trade_executor` paper 模式 | 无 | 3 天 |
| Phase 2 | `risk_manager` 接入 + audit_log 表 | Phase 1 | 1 天 |
| Phase 3 | live_dry_run 跑 7 天（不真下单，只记录） | Phase 2 | 7 天观察 |
| Phase 4 | UI「切真单」按钮 + exchange_credentials 表 | Phase 3 | 1 天 |
| Phase 5 | live_real + 0.5x 规模 + 监控面板 | Phase 4 | 1 天 |
| Phase 6 | Bybit/OKX adapter | Phase 5 | 2 天 |

---

## 10. v191 每日归档系统（生产已上线）

> 每天的反思都要归档，包括总结和每一笔信号的分析过程到交易过程，每天重置数据的时间点归档。

### 10.1 目的

实盘接入后，每天会触发数十～数百条 AI 决策 + 交易审计 + LLM 反思。  
为了 ops 复盘 / 监管追溯 / LLM 训练数据准备，需要把**每日的「全链路数据」**和**「数据重置时点」**永久落盘。

### 10.2 三张新表（生产已 migrate）

| 表 | 字段核心 | 写入触发点 |
|---|---|---|
| `daily_reset_log` | `reset_date`, `trigger`, `trade_mode`, `run_id`, `notes` | `engine/main.py` 启动 / 每日本地 23:59:30 / mode_change / kill_switch |
| `signal_chain_archive` | `decision_id` UNIQUE, `decision_ts`, `target_asset`, `final_action`, `audit_status`, `audit_ts`, `exit_reason`, `pnl_usdt`, 全链路 JSON | `trade_executor._archive_signal_chain` 在每次 `_insert_audit` 之后调用（幂等 INSERT OR REPLACE） |
| `daily_reflection_summary` | `summary_date`, `run_id`, `n_decisions`, `n_archived`, `n_executed`, `n_risk_rejected`, `asset_breakdown`, `action_breakdown`, `layer_breakdown` | `engine/main.py` 每日本地 23:59:30 调 `build_daily_summary` |

详见 [`src_python/db.py`](./src_python/db.py) 的 DDL 块（行 1945+）和 [`src_python/daily_archive.py`](./src_python/daily_archive.py)。

### 10.3 API 端点（GET-only，与 v190 同前缀）

| 端点 | 查询参数 | 用途 |
|---|---|---|
| `GET /api/v190/exchange/daily_summary` | `date=YYYY-MM-DD`, `run_id`, `limit=30` | 每日反思总结（按日期倒序） |
| `GET /api/v190/exchange/daily_resets` | `limit=30`, `trigger=auto_daily\|startup\|mode_change\|kill_switch\|manual` | 每日重置时点归档 |
| `GET /api/v190/exchange/signal_archive` | `decision_id`, `asset`, `audit_status`, `date`, `limit=50` | 单笔决策全链路（raw_news → ai_decisions → trade_audit_log → reflection） |

#### 示例

```bash
# 最近 30 天每日总结
curl http://localhost:8000/api/v190/exchange/daily_summary

# 某一天的全链路决策
curl 'http://localhost:8000/api/v190/exchange/signal_archive?date=2026-09-29&asset=BTC'

# 查询某个具体 decision_id
curl 'http://localhost:8000/api/v190/exchange/signal_archive?decision_id=12345'

# 查询启动时点归档
curl 'http://localhost:8000/api/v190/exchange/daily_resets?trigger=startup&limit=10'
```

返回示例（`daily_summary`）：

```json
{
  "count": 1,
  "rows": [{
    "summary_date": "2026-09-29",
    "run_id": "prod-voyage-e2c1",
    "n_decisions": 142,
    "n_archived": 142,
    "n_executed": 87,
    "n_risk_rejected": 23,
    "asset_breakdown": {"BTC": 45, "ETH": 38, "SOL": 30, ...},
    "action_breakdown": {"LONG": 60, "SHORT": 40, "FLAT": 42},
    "layer_breakdown": {"L1_macro": 45, "L2_quant": 60, "L3_news": 37},
    "total_pnl_usdt": 312.45,
    "win_rate": 0.55
  }]
}
```

### 10.4 触发时点

| 触发源 | 写入表 | 时机 | 实现位置 |
|---|---|---|---|
| **startup** | `daily_reset_log` | engine 进程启动时 | `engine/main.py:daily_archive_loop()` 入口 |
| **auto_daily** | `daily_reset_log` + `daily_reflection_summary` | 每日本地时间 **23:59:30 ~ 23:59:59** | `engine/main.py:daily_archive_loop()` 主循环 |
| **手动** | `daily_reset_log`（`trigger=manual`） | ops 通过 `POST /api/v190/exchange/manual_reset`（待开发） | 未来 ops 工具 |

> **注意**：当前 `daily_archive_loop` 用本地时区 `datetime.now()`，未做 UTC ↔ 北京时间转换。如果服务器部署在 UTC 时区，凌晨归档会跑在北京时间 08:00。生产部署时请确保 `TZ=Asia/Shanghai`。

### 10.5 幂等性保证

| 表 | UNIQUE 约束 | 重复写入语义 |
|---|---|---|
| `signal_chain_archive` | `decision_id` | `archive_signal_chain` 多次调用 → INSERT OR REPLACE（同一决策多 audit 状态时只保留最新） |
| `daily_reflection_summary` | `(summary_date, run_id)` | 同一天同一 run 多次 `build_daily_summary` → 自动 upsert |
| `daily_reset_log` | 无 UNIQUE | 重复 trigger 会有多行（按 `id DESC` 取最新） |

### 10.6 上线 checklist

- [x] `db.migrate()` 注入 3 张表 + 12 indexes（生产已生效）
- [x] `daily_archive.py` 单元测试 10/10 通过
- [x] `trade_executor` 4 处 audit 写入路径都接 `_archive_signal_chain`
- [x] `engine/main.py` 注册 `daily_archive_loop` asyncio task
- [x] `api_server.py` 注册 3 个 GET endpoint（FastAPI 已识别）
- [ ] 服务器 tz 设为 `Asia/Shanghai`（或显式 `TZ=Asia/Shanghai`）
- [ ] 监控告警：`daily_reflection_summary` 当天无 row 时告警（每日 23:59 检查 cron）
- [ ] UI 页面：每日反思查看器（调用 `/daily_summary`）

---

**本文件为设计骨架，不直接含代码。实现以 `trade_executor.py` + `adapters/` 目录下的子模块为准。**