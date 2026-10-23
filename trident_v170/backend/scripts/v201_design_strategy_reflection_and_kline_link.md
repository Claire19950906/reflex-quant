# v200 Summary — 反思 Filter 放宽 + LLM 反思占比修复

**日期**: 2026-09-28
**作者**: Cline (audit + fix cycle)
**状态**: ✅ 完成, **runtime verified** (+2 反思 LLM 写入, 9.5% → 12.2%)

---

# v201 Design — 策略级反思 + 日历宏观 K线联动

**日期**: 2026-09-28
**作者**: Cline
**状态**: 📝 设计稿（待用户审批）
**范围**: 三个用户提出的 UI/功能问题调研 + 设计

---

## 0. 前置修复 (P0 — 已落地, 待重启生效)

### Bug: real time event bus 一直 "waiting for data"

**根因**:
- `api_server.py:_EVENT_SELECT` 引用了 quick_sim_trades 表**不存在**的两列 (`max_price_time`, `min_price_time`)
- 同一 SELECT 又把 3 个**实际在 ai_decisions 表**的列 (`mfe_pct`, `mae_pct`, `mfe_time_mins`) 错写成 `qs.*` 前缀
- 两处都触发 `sqlite3.OperationalError` → `/api/events` 端点的 `except aiosqlite.OperationalError: return []` 把异常吞了 → HTTP 返回 `[]`
- 前端 `event-bus.tsx` loadHistory 拿空数组 → 显示 "waiting for data"

**修复**:
1. `db.py:_QUICK_SIM_TRADES_COLUMNS` 加 (`max_price_time`, `min_price_time`) → 启动时 `_ensure_columns` 自动 ALTER
2. `api_server.py:_EVENT_SELECT` L477-480 把 `qs.mfe_pct/mae_pct/mfe_time_mins` 改为 `ad.*` 前缀
3. 手动 ALTER 现网库 (`trident_event_bus.db`)，立刻加列

**待重启**: api_server 由 launchd 守护运行，旧模块在内存中，需 SIGTERM → 5 秒断连 → launchd 自动拉起新模块

**验证**: `_fetch_event_rows(limit=3)` importlib.reload 后能返回 3 条 (news_id 9450/9449/9448 等)
---

## 1. P1 需求: 模拟盘页反思·策略级 (Strategy-level Reflection)

### 1.1 现状 (现网已实现)

```
trade-level 单笔反思:                     strategy-level 整体表现 (现成统计, 未接 LLM):
trade_reflections_v2                     BuySellKpi (frontend /paper/buy-sell-kpi)
  ├ layer / sub_layer / signal_name       ├ per_asset_action: BTC/BUY 12笔 胜率58%
  ├ root_cause / lesson                   ├ by_event_strength: high/strong 胜率65%
  ├ improvement_suggestion                ├ buy_pattern: 涨后 BUY 胜率 60% / 跌后 BUY 胜率 45%
  └ depth_score                           ├ sell_pattern: 同上
       ↓ _aggregate_improvements_v2        ├ win_trigger_pattern: <60s入场胜率最高
       ↓                                  └ loss_trigger_pattern: 900s+ 入场亏损中位
strategy_param_adjustments ── 审批 ──► strategy_versions (source='reflection')
                                       paper_trading_settings (buy_min_score 等)
```

### 1.2 用户要 (策略级 vs 单笔级)

> "主要反思交易买卖方式的策略" — 不只盯单个 param，而是**策略组合表现**：
> - BUY 信号在不同 event_strength 下的胜率分布是否合理?
> - 入场时机 (win_trigger_pattern) 是否对应最佳持仓时长?
> - 整体胜率 (45%) 是否低于 50% 阈值 → 该停策略还是调阈值?
> - 高 loss 模式 (e.g. "900s+ 入场") 是该缩短持仓还是该提早止损?

### 1.3 设计: 新增 strategy_pattern_reflection

#### 1.3.1 新表 `strategy_reflections`

```sql
CREATE TABLE strategy_reflections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    period_start        TEXT    NOT NULL,
    period_end          TEXT    NOT NULL,
    period_days         INTEGER NOT NULL,
    sample_size         INTEGER NOT NULL,
    source_summary_json TEXT    NOT NULL,
    llm_summary         TEXT    NOT NULL,
    strategy_patterns_json TEXT NOT NULL,
    recommendations_json TEXT  NOT NULL,
    affected_dimension  TEXT    DEFAULT '',
    evidence_summary    TEXT    DEFAULT '',
    overall_confidence  REAL    DEFAULT 0.5,
    status              TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
    admin_note          TEXT    DEFAULT '',
    approved_at         TEXT,
    applied_strategy_version_id INTEGER,
    llm_model_id        TEXT    DEFAULT ''
);

CREATE INDEX idx_strategy_reflections_status ON strategy_reflections(status, created_at DESC);
```

#### 1.3.2 新模块 `backend/src_python/engine/strategy_reflection.py`

```python
def generate_strategy_reflection(period_days=30, asset_filter=None) -> int
def approve_strategy_reflection(reflection_id, apply_to_settings) -> Dict
def reject_strategy_reflection(reflection_id, note) -> None
def run_strategy_reflection_daemon(interval_days=7) -> None
```

#### 1.3.3 新 API

```
GET  /api/paper/strategy-reflections?status=pending&limit=10
POST /api/paper/strategy-reflections/generate    { period_days: 30, asset_filter?: "BTC" }
POST /api/paper/strategy-reflections/{id}/approve { apply_to_settings: true, note: "" }
POST /api/paper/strategy-reflections/{id}/reject  { note: "" }
```

#### 1.3.4 LLM Prompt 设计 (沿用 review_agent 风格)

```
你是量化交易策略审查员，分析最近 {period_days} 天模拟盘交易数据。

【输入】
1. 整体统计 (total_trades, overall.win_rate, overall.avg_pnl_pct)
2. BUY vs SELL 模式对比 (buy_pattern / sell_pattern)
3. 入场时机分布 (win_trigger_pattern / loss_trigger_pattern buckets)
4. Top 资产×动作 (per_asset_action[] 按笔数降序)
5. 按事件强度分层 (by_event_strength[])
6. 反思分布 (trade_reflections_v2 layer/sub_layer 计数 + root_cause 关键词 top 10)

【输出 JSON】
{
  "llm_summary": "<300字策略总结，指出最强模式 + 最弱模式>",
  "strategy_patterns": [
    { "pattern": "BUY 信号在 event_strength=high 下胜率 70%，但 low 仅 35%", "supporting_data": "high: 13/19 胜, low: 4/11 胜", "severity": "high" }
  ],
  "recommendations": [
    { "type": "param", "key": "min_event_strength", "direction": "up", "magnitude": 0.1, "confidence": 0.7, "rationale": "low 胜率拖累整体" }
  ],
  "affected_dimension": "signal_layer",
  "evidence_summary": "<200字>",
---

## 2. P1 需求: 日历宏观 ↔ K线联动 (Macro Event → K-line Impact)

### 2.1 现状

- 日历页 (`/calendar`): `BlsMacroPanel` / `MacroPanel` / `Jin10CalendarPanel` — 列表展示宏观事件 (title/time/importance/actual/forecast/previous)
- K线组件: `frontend/components/replay/kline-chart.tsx` + `live-desk.tsx` (未集成到日历/归因)
- 归因页 (`/attribution`): `MoversPanel` (涨跌板，无 K线)
- 后端 K线 API: `/api/asset/klines` 已存在

### 2.2 用户要

> "数据发布时间点对市场的影响，有影响的入经验/资产图谱" —
> 1. 宏观事件发布 → 显示受影响资产的实时 K线 (发布时刻竖线标记)
> 2. 影响超过阈值 → 自动写入 `experience_entries` (经验) + `asset_driver_edges` (资产图谱驱动边)

### 2.3 设计: event_kline_impact

#### 2.3.1 新表 `event_kline_impact`

```sql
CREATE TABLE event_kline_impact (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_source        TEXT    NOT NULL,   -- bls | fred | jin10
    event_id            TEXT    NOT NULL,   -- 来源原始 ID (避免跨表外键)
    event_title         TEXT    NOT NULL,
    event_time          INTEGER NOT NULL,   -- 发布 unix ts (秒)
    importance          TEXT    DEFAULT '',
    asset               TEXT    NOT NULL,   -- BTC/ETH/XAU/WTI/NVDA/...
    pre_event_close     REAL,
    max_high            REAL,
    min_low             REAL,
    impact_pct          REAL    NOT NULL,
    impact_window_start INTEGER NOT NULL,
    impact_window_end   INTEGER NOT NULL,
    impact_severity     TEXT    NOT NULL
        CHECK (impact_severity IN ('low', 'medium', 'high')),
    direction           TEXT    DEFAULT ''
        CHECK (direction IN ('up', 'down', 'neutral')),
    processed_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(event_source, event_id, asset)
);

CREATE INDEX idx_event_kline_impact_asset_time ON event_kline_impact(asset, event_time DESC);
CREATE INDEX idx_event_kline_impact_severity ON event_kline_impact(impact_severity, event_time DESC);
```

**severity 阈值**: `impact_pct >= 0.008 → high; >= 0.003 → medium; else low`
**direction 计算**: `(max_high - pre_close) > (pre_close - min_low) → up; else if 反之 → down; else neutral`

#### 2.3.2 新 worker `backend/scripts/event_kline_impact_worker.py`

```python
# 每 5 分钟跑一次
def process_recent_macro_events(window_minutes: int = 1440) -> int:
    """查近 24h 已发布的宏观事件 → 计算 K线影响 → 写 event_kline_impact。"""

def derive_severity(impact_pct: float) -> str: ...
def derive_direction(pre_close: float, max_high: float, min_low: float) -> str: ...

def update_asset_graph_and_experience(impact_id: int) -> None:
    """severity IN (medium, high) → 写 asset_driver_edges + experience_entries (driver_event)"""
```

#### 2.3.3 新 API

```
GET /api/macro/event-kline-impact
    ?event_source=bls|fred|jin10
    &event_id=<id>
    &asset=BTC  (可选, 不传则返回全部)
    返回: { event_source, event_id, event_title, event_time, importance,
            impacts: [{ asset, pre_event_close, max_high, min_low,
                       impact_pct, impact_severity, direction,
                       window_start, window_end }] }

GET /api/macro/event-kline-impact/recent
    ?asset=BTC&hours=24&min_severity=medium
    返回: [event_kline_impact 行, 按 event_time DESC]
```

#### 2.3.4 前端: `MacroKlineLink.tsx` 新组件 + 集成到现有 panel

**MacroKlineLink 渲染**:
- 每个宏观事件行右侧加按钮 "📊 K线影响"
- 点击 → 展开 inline `<details>` 面板:
  - 受影响资产表 (asset × impact_pct × severity)
  - 缩略 K线图 (发布时刻竖线标记, ±30min 窗口)
  - 跳转按钮 → 全屏 K线页 `/replay?asset=XAU&ts_start=...&ts_end=...`

**集成位置**:
- `bls-macro-panel.tsx`: 每个 BLS 事件行追加 `<MacroKlineLink event_source="bls" event_id={...} event_time={...} event_title={...} />`
- `macro-panel.tsx` (FRED): 同上 `event_source="fred"`
- `jin10-calendar-panel.tsx`: 同上 `event_source="jin10"`

**联动展示**:
- 经验页 (`/experiences`): 自动出现新写入的 driver_event 类型经验 (按 asset 分组)
- 资产图谱页 (`/asset-graph`): 自动出现新 driver 边 (asset → macro_event driver)

### 2.4 工作量

| 部分 | 行数 |
|---|---|
| `db.py` 新表 + ensure_columns | ~25 |
| `scripts/event_kline_impact_worker.py` | ~150 |
| `api_server.py` 新端点 2 个 | ~70 |
| `frontend/components/replay/MacroKlineLink.tsx` | ~120 |
| 集成到 bls/fred/jin10 panel (3 处) | ~30 |
| 测试 (`scripts/verify_v201_event_kline_impact.py`) | ~50 |
| **合计** | **~445 行** |

### 2.5 风险/边界

- **数据源依赖**: 需要现成的 macro 事件表 (BLS/FRED/Jin10) 都有 release_time 字段
- **K 线数据**: 受影响资产 (BTC/ETH/XAU/WTI/NVDA) 1m 或 5m K线需有覆盖
- **影响窗口**: 固定 ±30min 起步, 后续可参数化
- **资产覆盖**: 先只覆盖 `_ABNORMAL_SIG_ASSETS = ('BTC', 'ETH', 'XAU', 'SOL', 'WTI')` (api_server 已有)
- **写入经验/图谱**: severity ≥ medium 才写, 避免噪声

---

## 3. 实施顺序 (待用户确认)

```
P0   重启 api_server (修复 event bus)                              [5 min]
P1.1 v201 schema 落地 (db.py 新表 × 2)                          [15 min]
P1.2 v201 strategy_reflection 模块 + API + weekly daemon        [90 min]
P1.3 v201 reflection-improve-panel.tsx Tab                      [60 min]
P2.1 v201 event_kline_impact_worker + API                       [90 min]
P2.2 v201 MacroKlineLink + 集成到 3 个 panel                     [60 min]
P3   verify_v201_strategy_reflection.py + verify_v201_event_kline_impact.py [40 min]
```

**总计 ~5.5 小时实施 + 0.5 小时回归测试。**
  "overall_confidence": 0.7
}
```

约束:
- `recommendations[].key` 必须从 `_V182_PARAM_KEYS` 白名单选 (review_agent 已校验)
- `recommendations[].magnitude ∈ [0.01, 0.5]`, `confidence ∈ [0, 1]`
- `strategy_patterns.length ∈ [2, 6]` (强制多样性)
- `severity ∈ {low, medium, high}`

#### 1.3.5 前端: `reflection-improve-panel.tsx` 加 Tab

```tsx
<Tabs>
  <Tab label="参数调优 (Adjustments)">  // 现有 strategy_param_adjustments
  <Tab label="策略反思 (Strategy)">     // 新增 strategy_reflections
</Tabs>
```

**策略反思 Tab 渲染**:
- 列表卡片 (pending 在前)
- 每卡显示: LLM summary + patterns 列表 (severity 标色) + recommendations 列表 (key/direction/magnitude)
- 底部按钮: [审批并应用] / [驳回]
- 审批后: 同现有 adjustments 链路，回写 strategy_versions + paper_trading_settings

### 1.4 工作量

| 部分 | 行数 |
|---|---|
| `db.py` 新表 + schema + ensure_columns | ~30 |
| `engine/strategy_reflection.py` | ~180 |
| `api_server.py` 新端点 4 个 | ~80 |
| 周期 daemon (weekly_insight_apply 模式) | ~50 |
| `reflection-improve-panel.tsx` Tab + 列表 | ~140 |
| 测试 (`scripts/verify_v201_*.py`) | ~50 |
| **合计** | **~530 行** |

### 1.5 风险/边界

- **样本量**: 30 天可能 < 50 笔 → 强制 `sample_size >= 30` 才生成，否则拒绝 (返回 0)
- **LLM 成本**: 每周一次, 每次 ~3k tokens input + ~1k tokens output = 0.01 USD/次 ≈ 0.5 USD/年
- **重复建议**: 与现有 strategy_param_adjustments 去重 — 同 (param_key, direction, magnitude) 已 applied 则跳过
- **过期清理**: 90 天前的 reflection 状态自动 expired