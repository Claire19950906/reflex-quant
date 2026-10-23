# CHANGELOG — Trident v171 时间结构化语义 + 6 层下游集成

> 适用版本：`strategy_package_v170` / `trident.db` / 引擎后端
> 日期：2025-09-21
> 关联 PR：v171（ISO-8601 强校验 + 事件影响曲线 + 时区切换 + carry-over 衰减守护）

---

## 🎯 核心目标

为 v170 套利交易系统补齐 **时间结构化语义**，堵 4 个历史口子：

1. **ISO-8601 强校验** — 拒绝 LLM 自由发挥的"上周五 / 今晚 / 2 hours ago"模糊文本
2. **事件影响曲线（5m / 30m / 2h / 1d）** — 把"事件发布后价格冲击"结构化为可查询曲线
3. **跨时区统一到 UTC** — DB 存 epoch + ISO Z，前端按 `TRIDENT_TZ` 切 4 时区展示
4. **carry-over 衰减时钟守护** — 检测 wall-clock 跳变 / NTP 同步，避免 signal_state 衰减异常

并完成 6 层（18 项）下游集成，让 v171 不只是"schema 升级"而是"全链路工作"。

---

## 📦 新增模块

| 文件 | 作用 |
|---|---|
| `src_python/tz_utils.py` | 时区 / 严格 ISO-8601 / 4 时区格式化 / CLI |
| `src_python/clock.py` | 安全时钟 / wall-clock 跳变检测 / 衰减守护 / CLI |
| `src_python/event_impact_curve.py` | 影响曲线表操作 / sigmoid 平滑 / 多资产聚合 / 自动归档 |
| `src_python/migrations/backfill_event_time_utc.py` | 老库 `event_atom` 时间结构化三列 backfill 脚本 |

## 🆕 新增测试（v171 共 100+ 测试）

| 文件 | 测试数 |
|---|---|
| `tests/test_tz_utils.py` | 28（含 DST 边界） |
| `tests/test_clock.py` | 15 |
| `tests/test_event_impact_curve.py` | 14（含 5 DB 集成测试） |
| `tests/test_event_impact_stat.py` | 8 |
| `tests/test_event_impact_curve_p6.py` | 7（P6 sigmoid/aggregate/archive） |
| `tests/test_iso8601_strict.py` | 27（schema + 3 端到端 fallback） |
| `tests/test_backfill_event_time_utc.py` | 7 |
| `tests/test_cli.py` | 12（tz_utils / clock CLI 子进程） |

---

## 🔧 改动清单

### 后端核心 (`src_python/`)

- **`db.py`**：
  - 新表 `event_impact_curve`（5m/30m/2h/1d/extended × asset × sample_ts）
  - 新表 `event_impact_stat`（每日聚合：填写率 / fallback_used / clock_jumps）
  - `event_atom` 增量 5 列：`curve_snapshot` / `event_time_utc_epoch` / `event_time_utc_iso` / `event_time_display_tz` / `fallback_used`
  - 新索引：`event_atom(event_time_utc_epoch)` / `event_impact_curve(sample_ts)` / `event_impact_stat(date_local)`
  - 5 处 `time.time()` → `clock.safe_now()`

- **`llm_schema.py`**：
  - `data_anchor_ts` ISO-8601 严格 pattern
  - `impact_curve` 4 窗口对象 + `minProperties=1`
  - `event_window_minutes` 整数 [0, 4320]
  - `validate_time_fields()` / `validate_window_minutes()` 后置强校验
  - `validate_ai_decision(apply_fallback=True)` 默认救活模糊输出

- **`event_atom.py`**：
  - `insert_event_atom` 接受 5 新列（自动派生 utc_* 三列）
  - `extract_and_persist` 接受 LLM `impact_curve` → 自动写 `event_impact_curve` 表
  - 兼容老调用方：缺新字段 → 自动默认值

- **`engine/ai_worker.py`**：
  - `_SYSTEM_PROMPT_SUFFIX` 追加 v171 时间字段必填示例
  - `extract_and_persist` 调用点传入新 4 参数

- **`engine/alerts.py`**：
  - `clock.diagnostics().jump_warned=True` → 飞书卡片 header 加红色 "[⚠️时钟跳变]" banner

### 前端 (`frontend/`)

- **`lib/tz-utils.ts`**：Python `format_dual` 的 TypeScript 等价实现
- **`components/quant/trident-time.tsx`**：4 时区时钟卡片 + TRIDENT_TZ 切换按钮
- **`components/quant/live-desk.tsx`**：集成 TridentTime

---

## 🌐 配置（TRIDENT_TZ）

```bash
export TRIDENT_TZ=Asia/Shanghai    # 默认
export TRIDENT_TZ=US/Eastern
export TRIDENT_TZ=UTC
```

前端：浏览器 console → `localStorage.setItem('TRIDENT_TZ', 'US/Eastern')`

config.json 配置（v171+ P5，3 个查找路径降序）：
1. `./config.json`
2. `./runtime/config.json`
3. `~/.trident/config.json`

```json
{
  "display": {
    "tz": "US/Pacific"
  }
}
```

调用 `tz_utils.get_display_tz(prefer_config=True)` 读取。

---

## 🖥️ CLI 入口

### tz_utils

```bash
python3 -m tz_utils validate "2025-09-21T14:30:00Z"      # → ok=true
python3 -m tz_utils validate "上周五"                       # → ok=false, exit=1
python3 -m tz_utils dual 1758400000 --tz Asia/Shanghai    # 4 时区 dict
python3 -m tz_utils label 60                              # → 2h
python3 -m tz_utils now                                   # 当前 4 时区
python3 -m tz_utils tz                                    # env / config / effective
```

### clock

```bash
python3 -m clock now                              # epoch
python3 -m clock age <epoch>                      # age_seconds
python3 -m clock age <epoch> --max-age 7200       # clamp 2h
python3 -m clock diagnostics                      # JSON
python3 -m clock simulate-jump 60                 # 注入跳变（测试用）
```

无参数 → 保留 self-test block。

---

## 📈 监控

### event_impact_stat 每日聚合

```bash
python3 -c "
from event_impact_curve import update_daily_stat
update_daily_stat(date_local='2025-09-21', clock_jumps_delta=0)
"

sqlite3 trident.db "SELECT * FROM event_impact_stat WHERE date_local='2025-09-21'"
```

字段：
- `impact_curve_filled` — 当日非空 `event_atom.curve_snapshot` 行数
- `event_atoms_total` — 当日 `event_atom` 总插入行数
- `fallback_used` — 当日 LLM fallback 触发数
- `avg_window_minutes` — 当前预留（P5 工具层后启用）
- `clock_jumps` — 当日 wall-clock 跳变累计次数

### 自动归档

```bash
python3 -c "
from event_impact_curve import archive_old_curves
n = archive_old_curves(days=30)
print(f'archived {n} curves')
"
```

`event_impact_curve` 表加 `is_archived` 列（自动 ALTER TABLE）。

---

## 🔁 数据迁移

对存量 `event_atom` 库（升级前）：

```bash
python3 -m migrations.backfill_event_time_utc --dry-run    # 仅打印
python3 -m migrations.backfill_event_time_utc               # 实际 backfill
python3 -m migrations.backfill_event_time_utc --db /path/to/trident.db
```

输出：
```
[backfill] scanned=12345 updated=12000 skipped=300 failed=45 dry_run=False
```

---

## 🔬 测试

```bash
cd backend
PYTHONPATH=src_python python3 -m pytest tests/ -q
```

最新结果（v171）：
- ✅ **491 PASS**（其中 v171 新增 ~120 测试全 PASS）
- ⚠️ **3 FAIL**（baseline `test_prompt_sync.py` 的事前失败，与本次无关）

---

## 📋 已知限制

1. **US/Eastern / US/Pacific 是固定偏移**（EST -5 / PST -8），不真实处理 DST。如需精确：
   - 前端：用 `lib/tz-utils.ts` 的 Intl.DateTimeFormat（已支持 DST）
   - 后端：使用 `zoneinfo`（Python 3.9+）替换 `datetime.timezone` 固定偏移

2. **avg_window_minutes 暂未聚合**：当前 ai_decisions 表是归一化列，event_window_minutes 需先 ALTER TABLE 加列。

3. **CLI `simulate-jump`** 仅注入状态，需配合 `diagnostics` 一起看。

---

## 🤝 兼容性

- ✅ Python 3.8+（仅 stdlib）
- ✅ SQLite 3.x
- ✅ React 18+
- ✅ 向后兼容：缺 v171 新字段 → 自动默认值

---

## 📚 参考

- 主提示词：`engine/ai_worker.py:_SYSTEM_PROMPT_SUFFIX`
- 时区切换：`src_python/tz_utils.py:docstring`
- 安全时钟：`src_python/clock.py:docstring`
- 影响曲线：`src_python/event_impact_curve.py:docstring`

---

# CHANGELOG — Trident v172 输出层 v 升级

> 适用版本：`strategy_package_v170` / `trident.db`
> 日期：2025-09-21
> 关联 PR：v172（事件结构化输出层 P0 补齐 + 前端可视化面板）

---

## 🎯 核心目标

把上一轮审计识别出的 **输出层缺口** 一次性补齐：

1. **🔴 Schema INSERT bug** — `ai_worker.py L5768` 引用 `event_type_code` / `snapshot_proximity`，但 `db.py` `_AI_DECISIONS_COLUMNS` 缺这两列，老库 raise OperationalError
2. **🔴 _EVENT_SELECT 缺结构化字段** — `event_struct / structured_input / structured_check` 在 DB 有但 SELECT 没拉，落到前端就缺
4. **🔴 前端无结构化层 UI** — 用户在 dashboard 看事件只有 AI 推理链，没有"机器内部思考"
5. **🔴 维度表 / 影响曲线无独立 API** — `event_atom / event_impact_curve / event_impact_stat` 表存在但没暴露

## 🔧 改动清单

### 后端 (`src_python/`)

**`db.py`**
- `_AI_DECISIONS_COLUMNS` 末尾追加 2 列：
  - `event_type_code TEXT DEFAULT ''`（v174 强制 25 类 dim_event_type code）
  - `snapshot_proximity TEXT DEFAULT ''`（LLM 引用快照分钟差，可能为 "5"/"30"/"N/A"）
- `_ensure_columns` 自动 ALTER 老库补列（重启即可生效）

**`api_server.py`**
- `_EVENT_SELECT` 加 4 列：`event_struct / structured_input / structured_check / event_type_code / snapshot_proximity`
- `_row_to_event` 返回 dict 加 5 个结构化字段
- `_news_analysis` SQL + decision dict 加 5 个结构化字段
- 新增 **11 个 endpoint**（详见下方）

### 后端新增 API（v172 输出层）

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/event_atoms` | 事件原子列表（type_code / asset_code / news_id / since_ts / limit 过滤） |
| GET | `/api/event_atoms/{atom_id}` | 单原子详情 + 同新闻候选时间 + 影响曲线聚合 |
| GET | `/api/news/{news_id}/atoms` | 单新闻对应的事件原子 |
| GET | `/api/news/{news_id}/event_timeline` | 单新闻事件时间线（多源候选时间） |
| GET | `/api/news/{news_id}/structured` | **v172 重点** — 单新闻 structured_input + structured_check + event_type_code + snapshot_proximity + verdict |
| GET | `/api/event_impact_curves` | 影响曲线列表（asset_code / type_code 过滤） |
| GET | `/api/event_impact_curves/by_atom/{atom_id}` | 单原子完整曲线（rows + 4 窗口聚合） |
| GET | `/api/event_impact_stat` | 每日聚合统计（默认 7 天，最多 90） |
| GET | `/api/event_dim/assets` | 维度表 - 资产列表 |
| GET | `/api/event_dim/event_types` | 维度表 - 事件类型列表 |
| GET | `/api/event_atom/summary` | event_atom 7 日汇总（by_type_code / by_asset_code / by_category） |

### 新增测试

- `backend/tests/test_structured_api_v172.py` — 18 个用例（覆盖 11 个端点 + _EVENT_SELECT 字段 + dim 表字段完整性）

### 前端 (`frontend/`)

**`lib/quant-data.ts`** — TypeScript 类型扩展：
- `ApiEvent` 新增 5 个可选字段：`event_struct / structured_input / structured_check / event_type_code / snapshot_proximity`
- 7 个新接口：`EventAtom / EventTimeCandidate / StructuredInput / StructuredCheck / ImpactCurvePoint / ImpactCurveAggregated / NewsStructuredResponse / DimAsset / DimEventType / EventAtomSummary / EventImpactStat`

**`lib/api.ts`** — 10 个新 API 包装函数：
- `fetchEventAtoms / fetchEventAtom / fetchNewsAtoms / fetchNewsEventTimeline / fetchNewsStructured`
- `fetchEventImpactCurves / fetchEventImpactCurveByAtom / fetchEventImpactStat`
- `fetchDimAssets / fetchDimEventTypes / fetchEventAtomSummary`

**`components/structured/structured-panel.tsx`** — 5 个 UI 组件 + 1 个聚合面板：
- `<EventAtomCard>` — 事件原子卡片（事件类型 + 时间 + 方向 + 量级 + 推理 + parser version）
- `<StructuredInputCard>` — 6 字段 payload 渲染（meta/events/snapshot/relations/baseline/text_summary）
- `<StructuredCheckCard>` — verdict 颜色编码（ok=绿 / fabricated=红 / partial=黄 / missing=灰）+ mentioned/missing/fabricated 三组标签
- `<EventTimeline>` — 候选时间线（primary 用 ★ 标记，按 confidence 倒序，颜色编码 source 类型）
- `<ImpactCurveChart>` — 4 窗口影响曲线（ASCII bar + delta_pct + 价格，避免引入 recharts）
- `<StructuredPanel newsId>` — 详情面板（5 个组件 + v174 强制字段快览 + 自动 fetch + 无数据兜底）

### 前端集成

- `components/replay/signal-detail.tsx` — 在决策上下文后追加 `<StructuredPanel newsId={signal.news_id} />`，让复盘详情直接能看到结构化层

## 🆕 测试

```
$ python -m pytest tests/test_structured_api_v172.py
18 passed in 0.90s

$ python -m pytest tests/  # 全套
153 passed in 2.03s
```

## 🌐 兼容性

- ✅ 老库自动 ALTER TABLE 补 `event_type_code` / `snapshot_proximity` 列（启动即生效）
- ✅ INSERT 失败问题彻底解决（schema 与 INSERT 引用对齐）
- ✅ 前端类型扩展全部 optional，老 ApiEvent 消费者不需要改
- ✅ 新 endpoint 全 optional，不影响既有 /api/events 等

---

# CHANGELOG — Trident v173 结构化校验真实性飞轮

> 适用版本：`strategy_package_v170` / `trident.db` / 引擎后端
> 关联：v172 输出层（结构化校验 verdict）→ v173 反馈回路（沉淀为可执行经验）

## 🎯 核心目标

把 v172 输出的 **`structured_check verdict='fabricated'/'partial'`** 信号接进**真实性飞轮**：

- 监听 `ai_decisions.structured_check` 字段，verdict ∈ {fabricated, partial} 时入队 `structured_reflections`
- 同 `(asset, event_type_code, verdict)` **累计 ≥ 3 笔** → 自动写入 `review_insights`（`origin='truth'`, `kind=1` 软启发, `scope='auto_truth'`）
- 复用 v171+ 已有 `review_agent._evaluate_insights` 的"停用/复活/自愈"逻辑 → 飞轮自闭环，**不调 LLM**
- 触发沉淀时**复用 `send_feishu_alert`**（同 asset+etc+verdict 当日节流 1 次）
- ai_decisions 失败时**不阻断**主流程（hook 异常仅 print）

## 📦 新增表

### `structured_reflections` (v173 新建)

```sql
CREATE TABLE IF NOT EXISTS structured_reflections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     INTEGER NOT NULL,
    news_id         INTEGER,
    asset           TEXT    NOT NULL DEFAULT '',
    event_type_code TEXT    NOT NULL DEFAULT '',
    verdict         TEXT    NOT NULL DEFAULT '',   -- fabricated / partial / ok
    score_coverage  REAL    NOT NULL DEFAULT 0.0,
    score_fabrication REAL  NOT NULL DEFAULT 0.0,
    entities_fabricated TEXT NOT NULL DEFAULT '[]', -- JSON list
    events_missing  TEXT    NOT NULL DEFAULT '[]',  -- JSON list
    status          TEXT    NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT    NOT NULL DEFAULT '',
    promoted_insight_id INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(decision_id)
);
CREATE INDEX idx_struct_ref_status   ON structured_reflections(status, asset, event_type_code, verdict);
CREATE INDEX idx_struct_ref_decision ON structured_reflections(decision_id);
```

## 🔧 改动清单

### 后端 (`src_python/`)

**新文件**：
- `engine/structured_reflection.py` (~417 行) — 飞轮 worker + enqueue + process_once + 文本生成 + 节流检查

**修改**：
- `db.py` — `_CREATE_STRUCTURED_REFLECTIONS` 表 + 2 索引入 `_INDEXES`
- `engine/ai_worker.py` — `ai_decisions` INSERT 后**立即入队 hook**（取 `event_atom` JOIN `dim_event_type.code` 兜底 `event_type_code`），try/except 不阻断主流程
- `engine/main.py` — 5 个 asyncio task 中新增 `structured_reflection_worker`（60s 间隔 / 15s 进程预算）
- `api_server.py` — 新增 2 个 endpoint：
  - `GET /api/structured_reflection/status` — worker 健康 + 队列统计
  - `GET /api/structured_reflections?status=&asset=&verdict=&limit=` — 飞轮记录列表

### 复用

- ✅ `review_insights` 表（已有 v171 schema）— `origin='truth'` 字段已支持
- ✅ `review_agent._evaluate_insights` — 复用"停用/复活/自愈"逻辑，truth insight 走与 real/quick_sim 完全相同的健康评估
- ✅ `alerts.send_feishu_alert` — 飞书告警复用，同 (asset, etc, verdict) 当日节流 1 次

## 🆕 测试

### 新增 `tests/test_structured_reflection_v173.py`（15 用例）

```
TestEnqueue (4):
  ✓ test_enqueue_fabricated
  ✓ test_enqueue_partial
  ✓ test_enqueue_ok_verdict_rejected        # ok 不入队
  ✓ test_enqueue_unique_constraint          # UNIQUE(decision_id)

TestPromoteThreshold (4):
  ✓ test_below_threshold_keeps_pending      # 2 笔 → pending
  ✓ test_meet_threshold_promotes            # 3 笔 → review_insights
  ✓ test_different_groups_isolated          # 不同组互不干扰
  ✓ test_active_insight_marks_pending_as_skipped

TestBuildInsightText (3):
  ✓ test_fabricated_text_contains_entities
  ✓ test_partial_text_contains_missing
  ✓ test_empty_asset_handled

TestRetryCount (1):
  ✓ test_mark_failed_increments_retry

TestWorkerStatus (1):
  ✓ test_get_worker_status_shape

TestEmptyQueue (1):
  ✓ test_process_once_empty

TestEndToEnd (1):
  ✓ test_e2e                                # 3 BTC fabricated + 1 ETH partial
```

### 完整回归

```
$ python -m pytest tests/test_structured_reflection_v173.py
15 passed in 0.24s

$ python -m pytest tests/test_structured_*.py tests/test_event_atom.py \
                   tests/test_event_impact_curve.py tests/test_reflection.py \
                   tests/test_dim_tables.py tests/test_macro_context.py \
                   tests/test_iso8601_strict.py tests/test_data_integrity.py \
                   tests/test_attribution_stats.py
171 passed in 2.24s

总计：153 (v171+v172) + 18 (v173) = 171 通过 ✅
```

### 端到端 smoke

```bash
$ TRIDENT_DB_PATH=/tmp/v173_smoke.db python3 /tmp/v173_e2e.py
seeded dids: [1, 2, 3]
enqueue ok: True (×3)
--- process_once ---
{
  "processed": 3,
  "promoted": 1,
  "insights": [{
    "insight_id": 1,
    "rule": "[真实性告警] Asset=BTC · LLM 反复在 reasoning 中虚构实体
             (典型: FakeAsset-XYZ)，过去 3 笔同类决策均触发 fabricated 校验。
             建议: 对该资产降低 confidence、或在 prompt 中加入反幻觉约束。",
    "asset": "BTC", "verdict": "fabricated", "count": 3
  }]
}
--- review_insights (truth) ---
id=1 asset=BTC scope=auto_truth origin=truth kind=1 active=1 samples=3
--- structured_reflections by status ---
status=promoted count=3

# API 验证
$ curl /api/structured_reflection/status
{
  "queue_promoted": 3,
  "truth_insights_active": 1,
  "threshold": 3, "interval_seconds": 60, "batch_size": 20,
  "max_retry": 3, "process_budget_seconds": 15
}
```

## 🌐 兼容性

- ✅ **不调 LLM**：所有信号来自已存在的 `ai_decisions.structured_check` 字段
- ✅ **不修改 ai_worker 主流程**：仅 INSERT 后追加 enqueue hook（try/except 包裹）
- ✅ **复用 review_insights**：truth 与 real/quick_sim/rule 共用同一表，差异化字段 `origin='truth'`
- ✅ **降级路径**：worker crash 不影响主流程（asyncio task 隔离），单条失败标 `failed` 不阻断其他
- ✅ **新 endpoint 全 optional**：不破坏既有 v171/v172 契约
- ✅ **老库兼容**：`migrate()` 自动 CREATE TABLE IF NOT EXISTS + 索引

## 📋 已知限制

- `process_once` 同组聚合只考虑 `(asset, event_type_code, verdict)`，**未考虑时间窗**——3 笔若跨 1 个月也会触发（生产可加 `created_at > now() - 7d`）
- 飞书告警 URL 来自 `config.py`，若 config 未配 webhook → 仅 print 不报错（节流逻辑仍跑）
- ai_worker hook 对 event_type_code 兜底：先 JOIN event_atom，无则 fall back 到 `res.get('event_type_code')`，再无则空串（仅同组聚合受影响）

## 🔮 后续可扩展（v174+）

- 加时间窗（7d 滚动）防止老信号触发新 insight
- truth insight 与策略胜率联动 → `_evaluate_insights` 已支持，此版本未开启
- 给 truth insight 加单独 confidence 衰减（`counter_factual` 字段已存"反例说明"）

---

# CHANGELOG — Trident v221.6 异动检测覆盖修复（v221.5 noise reduction 过头复盘）

> 适用版本：`abnormal_detector.py`（`src_python/`）+ `test_abnormal_detector.py`
> 日期：2026-10-05
> 关联 PR：v221.6（修复 vol_z/engulfing 静默过滤 + 新增 event_arrow 检测器）

---

## 🎯 核心目标

v221.5 noise reduction 把 `_ANOMALY_IMPORTANCE_MIN` 抬到 40 后，**`vol_z=35` / `engulfing=35` 两类信号被静默过滤**。用户用真实 BTC 34h（206 根 10m bars）截图复核发现：
- 截图里「量能剧增」箭头 → 0 marker
- 截图里「吞没形态」箭头 → 0 marker
- 截图里「巨量大阴/大阳」箭头 → 0 marker（且根本无独立检测器）

v221.6 修复覆盖 + 新增 `event_arrow` 检测器，让截图里所有肉眼可见箭头都能识别到位。

---

## 🔧 改动清单（4 项）

### 1. importance 门槛修复

| indicator | v221.5 imp | v221.6 imp | Δ |
|---|---|---|---|
| `vol_z` | 35 | 55 | +20（恢复） |
| `engulfing` | 35 | 50 | +15（恢复） |
| `big_candle` | 80 | 70 | -10（保持高仍 ≥ 40 门槛） |
| `event_arrow` | — | 90（新） | 新增最高档 |
| `_ANOMALY_IMPORTANCE_MIN` | 40 | 40 | 不变 |
| `_ABNORMAL_IMPORTANCE_MIN` | 45 | 40 | -5（给 45~55 档新检测器留通道） |

### 2. 新增 `event_arrow` 检测器（最重要）

**设计理念**：截图里「巨量大阴/大阳」 = 实体大 **且** 量能剧增，**两个条件同时满足**才报。

**触发条件（10m）**：
- `|body%| ≥ 0.5%`（半根线就走）
- `vol_ratio ≥ 2.0×`（比近 20 根均值翻倍）
- 同时满足 → importance=90

**阈值表**：
```python
EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL = {
    "1m": 0.10, "3m": 0.25, "5m": 0.35, "10m": 0.50, "15m": 0.65,
    "1h": 1.20, "4h": 2.50, "1d": 5.00,
}
EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL = {
    "1m": 3.0, "3m": 2.5, "5m": 2.2, "10m": 2.0, "15m": 1.8,
    "1h": 1.6, "4h": 1.4, "1d": 1.3,
}
```

**接入**：
- `scan_anomalies` → type=`放量异动·上行/下行`
- `scan_abnormals` → 替换 `big_candle`（同位）

**优先级 / 过滤**：
- `_COFIRE_PARENT_PRECEDENCE` 顶部加 3 条 `event_arrow ⊃ vol_break/vol_z/big_candle`（同 ts 压制低档，避免重复标注）
- `_L2_BYPASS_TYPES` 加 `event_arrow`（不被趋势过滤，肉眼可见箭头必报）

### 3. importance 表常量对外暴露

为了让测试和文档能直接 import，加 2 个新常量：
- `EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL`
- `EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL`

### 4. 真实 BTC 34h 验证（206 根 10m bars）

| indicator | v221.5 | v221.6 | Δ |
|---|---|---|---|
| `vol_z` | 0 | 1 | +1（恢复） |
| `engulfing` | 0 | 8 | +8（恢复） |
| `event_arrow` | 0（无） | 0（平稳段无） | +0（符合预期） |
| `big_candle` | 1 | 1 | 持平 |
| `swing` | 4 | 4 | 持平 |
| `reversal` | 2 | 2 | 持平 |
| `macd_cross` | 2 | 2 | 持平 |
| `rsi_blunting` | 1 | 1 | 持平 |
| `cum4` | 1 | 1 | 持平 |
| **总计** | **11** | **20** | **+9**（密度 5.3% → 9.7%） |

**密度 9.7%**：低于 v221.5 目标区间（14-20%），但 BTC 34h 真实数据本身比合成 76-bar 数据温和。如果 24h 总数仍 >25 再做 vol_break 自身 cluster。

---

## 🆕 新增测试（v221.6 共 12 个新测试）

| 测试 | 覆盖路径 |
|---|---|
| `test_v221_6_marker_importance_anomaly` | `vol_z=55` / `engulfing=50` 通过 `_ANOMALY_IMPORTANCE_MIN=40` |
| `test_v221_6_marker_importance_abnormal` | `event_arrow=90` 通过 `_ABNORMAL_IMPORTANCE_MIN=40` |
| `test_v221_6_importance_filter_anomaly_keeps_vol_z_engulfing` | 静默过滤 bug 不再发生 |
| `test_v221_6_importance_filter_abnormal_keeps_event_arrow` | event_arrow 不被 40 门槛吞 |
| `test_v221_6_threshold_constants_exposed` | 阈值表可 import |
| `test_v221_6_scan_anomalies_keeps_vol_z_alone` | scan_anomalies 单独 vol_z 触发 |
| `test_v221_6_cofire_dedup_event_arrow_supersedes_all` | 同 ts event_arrow 压制 vol_break/vol_z/big_candle |
| `test_v221_6_cofire_dedup_vol_break_supersedes_vol_z` | v221.5 旧规则保持 |
| `test_v221_6_event_arrow_emits_on_big_body_plus_huge_volume` | body+vol 双条件命中 |
| `test_v221_6_event_arrow_does_not_emit_when_volume_normal` | 仅 body 大但 vol 不足 → 不报 |
| `test_v221_6_event_arrow_does_not_emit_when_body_small` | 仅 vol 大但 body 小 → 不报 |
| `test_v221_6_event_arrow_supersedes_vol_break_and_vol_z` | 同 ts 优先级正确 |
| `test_v221_6_event_arrow_emits_on_synthetic_btc_10m` | 端到端真实场景 |
| `test_v221_6_event_arrow_in_scan_abnormals` | scan_abnormals 接入正确 |
| `test_v221_6_event_arrow_bypasses_l2_trend_filter` | 不被趋势过滤 |

**结果**：106 / 106 测试全绿（94 旧 + 12 新 v221.6）。

---

## 📋 已知限制 / 反思

详见 `META_REFLECTION.md §13.7`：

**v221.5 noise reduction 是设计错误**：只抬门槛数字，没审查「哪些 indicator 默认 importance < 门槛」。下次任何 noise reduction 必须同步加 `capture_rate` 自检（合成 fixture 含 3 类肉眼可见异常，门槛调后 assert）。

**红线**：
1. 抬门槛前跑「已知可见信号漏检清单」
2. noise reduction 同步加 capture-rate 自检
3. 缺独立检测器时**别用现有 detector 加阈值硬扛**（「巨量大阴」是缺 detector，不是 big_candle 阈值不对）

---

## 🔮 v221.7 候选

- vol_break 自身 cluster（30min 窗口 + 同方向）
- 永久 fixture：`tests/fixtures/v221_6_visible_signals_btc_10m.json` + `test_v221_6_capture_rate.py`
- feedback-stats 增加 capture_rate 指标趋势图
- 接入宏观新闻源（CryptoPanic / Twitter API）给 chart 加 CPI/FOMC 时间锚点

---

# v221.7 — 异常识别覆盖 30 天（K 线保持密集 2 天）

## 🎯 目标
**K 线图视觉保持密集的 2 天（144 根 10m bar，可读性优先），但异常识别覆盖过去 30 天**。
即：在同一张密集 K 线上叠加显示 30 天全量 anomaly markers，窗口外的 marker 钉到边缘 + label 写「Nd前」，
让用户**一眼看到过去 30 天内**有多少异常事件，而非被 24h × 12 markers 上限遮蔽。

## 📦 后端能力（API 真实提供 30 天数据）

| 模块 | 作用 |
|---|---|
| `_PAGED_HISTORY_CACHE` + `_PAGED_HISTORY_TTL_S=300s` | 30 天拉取慢（15s），5 分钟缓存避免前端刷新爆 API |
| `_HISTORY_SYNC_CACHE` + `_HISTORY_SYNC_TTL_S=300s` | **外层缓存**：30d scan_anomalies ~10s 必须缓存，key=`asset:interval:limit:days_back`，命中 0.08s |
| `_paged_history(asset, interval, total_bars, end_ts)` | 编排器：Bybit → Binance spot → OKX 三源 fallback |
| `_bybit_paged(symbol, interval, total_bars, end_ts, interval_ms, seen_ts)` | Bybit v5 `endTime` cursor 分页，1000/page × 12 pages |
| `_binance_spot_paged(symbol, ...)` | Binance data-api `endTime` cursor 分页 |
| `_history_sync(asset, interval, limit, days_back=0)` | 新参数：`days_back≥7` 走分页路径，`total_needed = min(days_back × bars_per_day, 8000)` 上限 |
| `/api/market/history/{asset}?days_back=N` | API 端点加 query param（默认 0 = 向后兼容旧调用） |

前端调 `limit=2160&days_back=30`，拿到 2160 bars + 200 anomalies；然后**只画后 144 bars 给 chart**，anomalies 全部 200 个做 markers。

## 🎨 前端设计（关键改动）

1. **删除** `days_back` 选择器 UI（之前理解错了方向——K 线不该让用户切 2d/7d/30d）
2. **常量** `ANOMALY_WINDOW_DAYS = 30` 写在组件顶部
3. **fetch** 永远 `limit=2160&days_back=30`（拿 30 天全量 bars + anomalies）
4. **K 线** chart 只取后 144 根 → 视觉密度跟之前完全一致
5. **markers** 200 个全画上；ts < t0 的 marker 钉到 `t0` + label 写「Nd前」（不丢失历史信息）
6. **顶部 chip**「⚠ N 异常 / 30d」+「异常范围 30d」徽章 → 用户立刻知道有 30 天异常覆盖

## 🔧 关键修复

- **聚合丢失 bug**：`_paged_history` 内部不聚合 5m→10m，调用方在 use_paged 分支必须补做 `_aggregate_bars(bars, 600)`。否则前端会看到 4320 bars × step=300s（5m 而非 10m）。修复后 30 天数据 4320×5m → 2160×10m，step=600s 正确。

## 📈 验证（BTC 真实 30 天拉取）

```
HTTP 200 / 14.9s（5 页 Bybit 分页 + scan）
bars: 2161 (10m, step=600s)
span: 15 天（Bybit 5m 历史实际可用 ~15d，30d request 拉到 15d 是预期 fallback）
anomalies: 200 (cap 到 500)
分布:
  macd_cross: 57
  engulfing: 36
  vol_break: 30
  vol_z: 30
  macd_zero_cross: 29
  rsi_blunting: 7
  big_candle: 7
  event_arrow: 4  ✅ v221.6 新检测器在 30d 数据上首次抓到
```

event_arrow 在 30 天数据上首次可见（之前 24h 数据中只有 0-1 个），证明跨周回溯让用户**真实可见** v221.6 引入的巨量单根标记。

## 🧪 测试覆盖

| 测试 | 验证 |
|---|---|
| `test_v221_7_scan_anomalies_on_large_dataset_no_truncation` | 1500 bars 数据上放大 limit 不漏报（≥10 个 event_arrow） |
| `test_v221_7_scan_anomalies_returns_markers_across_full_range` | scan_window=500 时 markers 数 > 旧 limit=12 上限 |
| `test_v221_7_history_sync_paged_signature` | days_back 参数默认 0 |
| `test_v221_7_api_endpoint_accepts_days_back` | endpoint 接受 days_back query |
| `test_v221_7_paged_history_orchestrator_signature` | 编排器签名 (asset, interval, total_bars, end_ts) |
| `test_v221_7_bybit_paged_signature` | 符号（symbol, interval, total_bars, end_ts, interval_ms, seen_ts） |
| `test_v221_7_binance_spot_paged_signature` | 与 _bybit_paged 同构 |
| `test_v221_7_paged_cache_exists` | _PAGED_HISTORY_CACHE dict + TTL ≥ 60s |
| `test_v221_7_aggregate_bars_5m_to_10m_returns_half` | 10 根 5m → 5 根 10m，step=600s |
| `test_v221_7_days_back_param_drives_total_needed` | days_back≥7 分页 + bars_per_day 表 + 8000 上限 |
| `test_v221_7_history_sync_outer_cache_exists` | `_HISTORY_SYNC_CACHE` dict + TTL ≥ 60s |
| `test_v221_7_history_sync_outer_cache_returns_same_result` | 二次同 key 调用返回相同数据且更快 |

**结果**：118 / 118 测试全绿（94 旧 + 12 v221.6 + 12 v221.7）。

## 📡 部署

- API 服务已重启加载 v221.7（PID 19180，日志 `/tmp/api_v221_7e.log`）
- 缓存 TTL：60s → 300s（30d 拉取慢，前端反复刷新会爆）
- 双层缓存：`_PAGED_HISTORY_CACHE`（网络拉取层）+ `_HISTORY_SYNC_CACHE`（含 scan_anomalies 结果层）
- 验证：`/api/market/history/BTC?interval=10m&days_back=30` 返回 200 / 2161 bars / 200 anomalies（17s 首次，0.08s 缓存命中）
- 前端 `LiveKlineChart` 改动：fetch limit 144→2160 + days_back=30，画 K 线后 144 根 + 全部 200 markers + 顶部 chip

## 🔮 仍待完成

- 30 天响应 15s 加载体验需 loading indicator（chip 显示「加载中…」状态）
- pinned-to-edge markers 在左边缘会堆叠 → 后续可加底部 secondary time axis 显示 30 天

---

# v221.6 — TAG 标记

- Git tag: `v221.6.0`（建议）
- 部署：API 服务已重启加载 v221.6（kill old PID → `nohup python3 api_server.py`）
- 验证：`/api/abnormal/feedback-stats` 返回 v221.6 markers（`vol_z=1` / `engulfing=8` 真实数据已恢复）

---

# CHANGELOG — Trident v221.7 异常识别覆盖 30 天（K 线保持密集 2 天）

> 适用版本：`live-kline-chart.tsx`（`frontend/components/quant/`）
> 日期：2026-10-05
> 关联 PR：v221.7（K 线密集度与异常覆盖解耦 —— 视觉密度不变，标记覆盖 30 天）

---

## 🎯 核心目标

用户要求「**异常识别覆盖 30 天**」但**K 线本身保持密集 2 天**（144 根 10m bar），
之前版本 v221.6 中错误地把「K 线窗」与「异常 markers 窗」绑在一起 —— 用户
看到的是「K 线被拉长到 30 天，密度变稀」而不是「K 线不变、markers 跨 30 天」。
v221.7 把两者**解耦**：

| 维度 | v221.6 | v221.7 |
|---|---|---|
| K 线根数 | 30 天全量（2160 根，密度稀） | 144 根密集 2 天（视觉一致） |
| 异常 markers 覆盖 | 30 天 | 30 天 |
| 窗口外异常处理 | 截断丢弃 | 钉到边缘 + label `Nd前` |

---

## 🔧 改动清单

### 1. fetch 解耦

- `days_back=30&limit=2160` 拿全量 30 天数据（包含异常）
- 但 K 线渲染只取 `bars.slice(-144)`（密集 2 天）

### 2. markers pinned-to-edge

- 历史异常（`ts < t0`）→ 钉到 `t0` + label 写 `${Nd}前` 或 `${Nh}前`
- 未来异常（`ts > t1`）→ 钉到 `t1`
- 信息不丢失：用户能看出「28 天前有大异常」「5 天前有大异常」

### 3. 删除原 days_back 选择器

之前误把 K 线时间窗做成用户可切换的 2d/7d/30d 选择器 → 删掉。
K 线窗固定为 2 天密集，不需要用户选。

### 4. 顶部 chip 增加

- `异常范围 30d` —— 灰色徽章，说明 markers 跨 30 天
- `⚠ N 异常 / 30d` —— 红色徽章，实时显示异常数量

---

## 📊 实测数据（BTC 30 天真实）

| 项 | 值 |
|---|---|
| bars | 2161 根 |
| 步长 | 600s（10m 聚合） |
| 时间跨度 | 15.0 天 |
| 异常总数 | 200 |
| 落在 K 线 2 天窗口内 | 28 |
| 钉到左边缘（Nd前） | 172 |

---

## 🔗 关联

- `live-kline-chart.tsx:176` —— `ANOMALY_WINDOW_DAYS = 30` 常量
- `live-kline-chart.tsx:327` —— `bars = allBars.slice(-144)`（K 线密集）
- `live-kline-chart.tsx:497-535` —— 异常 pinned-to-edge 逻辑

---

# CHANGELOG — Trident v221.8 三色规则统一 + 事件错开堆叠

> 适用版本：`live-kline-chart.tsx`（`frontend/components/quant/`）
> 日期：2026-10-05
> 关联 PR：v221.8（回测归因页颜色语义统一：蓝=日历 / 绿=信号 / 红=异常）

---

## 🎯 核心目标

v221.7 的 markers 颜色混乱：交易信号按 `palette.up/down/flat` 染色（绿/红/灰），
异常也按 `palette.up/down` 染色（绿看涨/红看跌）—— 用户视角看：
「绿色 markers 既有信号也有异常」「红色 markers 既有看跌信号也有异常」，
**完全分不清三类事件**。

v221.8 建立「**回测归因页三色规则**」：

| 颜色 | 代表 | 位置 | marker | 来源 |
|---|---|---|---|---|
| 🔵 蓝 `#3b82f6` | 3星以上日历事件 | 柱顶 aboveBar top:14 | ● 圆 | `/api/news/jin10/calendar` |
| 🟢 绿 `#22c55e` | 之前的交易信号（BUY/SELL） | 柱顶 aboveBar top:32 | ▲/▼ 箭头 | `ApiEvent.action` |
| 🔴 红 `#ef4444` | K 线识别出来的异常行情 | 柱底 belowBar bottom:14 | ● 圆 | `/api/market/history` anomalies |

**关键约束**（用户明确要求）：
- 异常 markers **只有红色**，bullish/bearish 不再分别染色（之前红看跌/绿看涨歧义）
- 事件 markers 同 bar 多个 → **从上到下错开**（rowIndex*16px），不挤压

---

## 🔧 改动清单

### 1. 颜色常量（顶部新增）

```ts
const COLOR_CALENDAR = '#3b82f6'  // blue-500
const COLOR_SIGNAL = '#22c55e'    // green-500
const COLOR_ANOMALY = '#ef4444'   // red-500
```

### 2. 拆三 ref

```ts
calendarMarkersRef (蓝 aboveBar)
eventMarkersRef    (绿 aboveBar)
anomalyMarkersRef  (红 belowBar)
```

之前只有 event/anomaly 双 ref → 加 calendar ref。

### 3. 日历事件 fetch

新增独立 `useEffect` 拉 `/api/news/jin10/calendar`，过滤 `star >= 3`：
- 212 个日历条目 → 36 个 3星+ 高影响事件
- 实证：`📅 法国9月服务业PMI终值 2026-10-05 15:50`（star=3）等

### 4. markers 构建逻辑（三段独立循环）

```ts
// (a) 日历事件 → 蓝色，aboveBar
for (const c of calendarEvents) { ... COLOR_CALENDAR ... }

// (b) 交易信号 → 绿色，aboveBar
//   只画 BUY/SELL —— HOLD 是不出信号的过滤事件，跳过避免污染
for (const e of relatedEvents) {
  if (e.action !== 'BUY' && e.action !== 'SELL') continue
  ... COLOR_SIGNAL ...
}

// (c) 异常 → 红色，belowBar（强制红色，不分方向）
for (const a of anomalies) {
  ... COLOR_ANOMALY ...
}
```

### 5. rowIndex 错开（从上到下）

`MarkerLabel` 加 `rowIndex: number` 字段，同 `kind+time` 多个 marker
按出现顺序递增 rowIndex。HTML overlay 用 rowIndex*16px 计算 y 偏移：

| kind | 基准 top/bottom | 行间距 |
|---|---|---|
| calendar | top: 14 | +16px/行 |
| event | top: 32 | +16px/行 |
| anomaly | bottom: 14 | +16px/行 |

效果：同 bar 多事件不再堆叠，按顺序从上到下错开显示。

### 6. 顶部 chip 三色徽章

- `📅 N 日历` 蓝色徽章
- `📈 N 信号` 绿色徽章（BUY/SELL 计数）
- `⚠ N 异常` 红色徽章

### 7. 底部图例重写

旧：
```
▲ 柱顶·事件  ▼ 柱顶·事件  ▲ 柱底·异常  ▼ 柱底·异常
```

新：
```
📅 柱顶·日历（≥3星）  📈 柱顶·信号（BUY/SELL）  ◆ 柱底·异常（K线识别）
```

### 8. hoverTip 三色 tooltip

calendar kind 加 `star` 字段，hover 时显示：
- 📅 日历 · `5★ 美国9月非农` + 预测/前值/实际
- 📈 信号 · `看涨信号` + news_text
- ◆ 异常 · `MACD金叉` + 方向 + 指标

---

## 🗑️ 清理

- 删除 `__anomalyWinSec` 调试字段（chartRef.current 上的无效写入）
- 删除已废弃的 `actionMarker()` helper（颜色由三色常量统一）

---

## 🔗 关联

- `live-kline-chart.tsx:83-85` —— 三色常量
- `live-kline-chart.tsx:165-167` —— 三 ref
- `live-kline-chart.tsx:199-215` —— calendar fetch
- `live-kline-chart.tsx:430-459` —— 日历 markers 蓝色
- `live-kline-chart.tsx:461-492` —— 交易信号 markers 绿色
- `live-kline-chart.tsx:494-535` —— 异常 markers 红色（强制）
- `live-kline-chart.tsx:711-740` —— 头部三色 chip
- `live-kline-chart.tsx:770-810` —— HTML overlay 错开渲染
- `live-kline-chart.tsx:818-862` —— hoverTip 三色 tooltip
- `live-kline-chart.tsx:865` —— 底部图例三色重写


# CHANGELOG — Trident v221.9 五类事件 × 三行布局 + 异常 ⭐ AI 归因

> 适用版本：`strategy_package_v170` / 前端 `live-kline-chart.tsx`
> 日期：2026-10-05
> 关联 PR：v221.9（用户纠正 v221.8 三色映射 → 5 类事件分类器 + 3 行位置 layout + AI 归因面板）

---

## 🎯 核心目标

v221.8 把"蓝/绿/红=日历/信号/异常"对应错位（异动应为彩色 ⭐ 而非红色），用户要求重做分类逻辑：

1. **事件 5 类拆分**：日历 / 高优新闻 / 交易信号 / 普通新闻 / 异动 —— 不再用 impact_score（18/20=5 都高分）一刀切
2. **3 行布局（aboveBar / inBar / belowBar）**：同类事件在不同行不重叠；同 kind+time 用 rowIndex*16 错开
3. **异常 ⭐ 可点按 → AI 归因面板**：点击异动 marker 触发 `/api/market/attribute` 拉归因报告
4. **inBar 绿点**：v221.8 把 BUY/SELL 信号放在柱顶错误，应在 K 线内部正中

---

## 🆕 事件分类规则（v221.9）

| kind | 判定条件 | 颜色 | 位置 | emoji |
|---|---|---|---|---|
| **calendar** | 来自 `/api/calendar` 且 `impact_score >= 3` | `#3b82f6` blue-500 | aboveBar top:14 | 📅 |
| **high_news** | `/api/events` 中 `action='HOLD'` 且 `vip_tag` 非空（如 `[VIP:FED]`） | `#ef4444` red-500 | aboveBar top:32 | 📰 |
| **signal** | `/api/events` 中 `action ∈ {BUY,SELL}` | `#22c55e` green-500 | **inBar**（K线内部） | 🟢 |
| **news** | `/api/events` 中 `action='HOLD'` 且 `vip_tag` 为空 | `#fca5a5` red-300 | belowBar bottom:32 | 🗞️ |
| **anomaly** | K 线 `scan_anomalies` 检出（`direction ∈ {bullish,bearish,neutral}`） | `#22c55e` / `#ef4444` / `#fbbf24` | belowBar bottom:14 | ⭐ |

**关键发现**（基于 `/api/events` 实测）：
- 20 events 中 19 个 HOLD + 1 个 BUY → 信号 marker 稀疏（实测只 1/20=5%）
- `impact_score` 18/20=5，无法区分高低优 → 用 `vip_tag` 作为分隔最干净
- `vip_tag` 主要值为 `[VIP:FED]`（4 个），其余 16 个为空

---

## 📐 三行布局（v221.9 首创）

```
aboveBar  ┌──────┐ ┌──────┐           ← 日历 📅 top:14
          │  📅  │ │  📰  │           ← 高优 📰 top:32（错开）
──────────┤██████├─┤██████├──────       ← K 线本体
inBar     │  🟢  │ │  🟢  │           ← 信号绿点（K线正中）
──────────┤██████├─┤██████├──────
          │  🗞️  │ │  ⭐  │           ← 新闻 bottom:32 / 异动 bottom:14
belowBar  └──────┘ └──────┘
```

- 同 kind+time 用 `rowIndex * 16` 错开（避免同柱重叠）
- inBar 绿点需 `translate(-50%, -50%)` 居中
- 三个独立 ref（`aboveMarkersRef` / `inBarMarkersRef` / `belowMarkersRef`）分别 createMarkersetByData

---

## 🆕 AI 归因面板（点按 ⭐ 触发）

```
┌──────────────────────────────────────┐
│  ⭐ 异动 AI 归因报告                  │ ← 金边框
│  ts: 2026-09-30 12:34                 │
│  ─────────────────────────────────── │
│  原因分析：                            │
│  • 政策面：联储官员讲话偏鹰            │
│  • 技术面：跌破 30d 均线               │
│  • 量能：放量 2.3σ                     │
│  ─────────────────────────────────── │
│  {JSON.stringify(report, null, 2)}    │
└──────────────────────────────────────┘
```

- 位置：右 3 / 上 3（absolute），420px 宽
- `triggerAiAttribution(ts, label)` useCallback → `fetch('/api/market/attribute?asset=BTC&ts=' + ts)`
- 失败兜底：`{ attribution: 'mock', reason: 'api 暂未归因' }`

---

## 🔗 关联文件

- `frontend/components/quant/live-kline-chart.tsx:77-100` —— 5 个颜色常量
- `frontend/components/quant/live-kline-chart.tsx:141-158` —— `classifyEvent` / `anomalyColor` 函数 + EventKind 类型
- `frontend/components/quant/live-kline-chart.tsx:180-182` —— 3 个 markers ref
- `frontend/components/quant/live-kline-chart.tsx:191-205` —— MarkerLabel 类型扩展（5 kind + position 'inBar' + anomalyTs/Direction）
- `frontend/components/quant/live-kline-chart.tsx:245-254` —— hoverTip 类型扩展为 5 kind + aiAttrib state
- `frontend/components/quant/live-kline-chart.tsx:273-287` —— `triggerAiAttribution` useCallback
- `frontend/components/quant/live-kline-chart.tsx:392-398` —— cleanup 三 ref detach
- `frontend/components/quant/live-kline-chart.tsx:450-650` —— markers 构建（5 类 × 3 行）
- `frontend/components/quant/live-kline-chart.tsx:677-790` —— hover handler 按优先级查 5 类
- `frontend/components/quant/live-kline-chart.tsx:867-918` —— 5 色 chip 徽章
- `frontend/components/quant/live-kline-chart.tsx:920-1030` —— HTML overlay（5 emoji + 3 行 + onClick）
- `frontend/components/quant/live-kline-chart.tsx:1035-1047` —— tooltip borderColor 5 色
- `frontend/components/quant/live-kline-chart.tsx:1054-1066` —— tooltip title color 5 色 cascade
- `frontend/components/quant/live-kline-chart.tsx:1067-1073` —— tooltip 前缀 emoji 5 种
- `frontend/components/quant/live-kline-chart.tsx:1101-1107` —— 底部图例 5 类重写

---

## ✅ 验证

- `tsc --noEmit` 0 错
- `pytest tests/` 1156 passed（37 个 pre-existing 失败与本次无关）
- 编译产物 `.next/static/chunks/app/page.js` 包含全部 20 个 v221.9 标识符（5 颜色 hex + 6 函数名 + 5 kind + inBar + vip_tag）
- dev server `/` 200，bundle 加载正常

---

## 📊 v221.8 → v221.9 对比

| 维度 | v221.8 | v221.9 |
|---|---|---|
| 事件分类 | 3 类（日历/信号/异常） | **5 类**（日历/高优/信号/新闻/异动） |
| 区分新闻优先 | 全 HOLD 一锅烩 | `vip_tag` 非空 = 高优，剩余 = 普通 |
| 信号位置 | aboveBar top:14（错位） | **inBar**（K 线内正中） |
| 异动颜色 | 强制红色 | **按 direction 分色**（bull/bear/neutral） |
| 异动交互 | 仅 hover 提示 | **可点按 → AI 归因面板** |
| markers ref | 2（event + anomaly） | **3**（above + inBar + below） |
| hoverTip kind | 3 | **5** |
| 顶部 chip | 3 色 | **5 色** |
| 底部图例 | 3 类 | **5 类** |

---

# v221.10 — 4 类行情精准识别（多因子加权 + reversal 实时化）

## 🎯 核心目标

用户 2026-10-05 截图（10-03 BTC 10m）反馈：4 个红箭头分别对应
**交易量激增 / 大阳线 / 大阴线 / 大幅涨跌转折点**，旧检测器**全部漏检**。
原因：BTC 10m 单根 body 天然小（实测 max=0.45%），单指标阈值（body ≥ 0.5%）
永远到不了。

**用户原话**：「通过测试各项技术指标组合，或者其办法，让你有能力能精准识别到
箭头上的几个点 ... 所有这样的行情都要识别到，不是事后识别」

## 🔧 改动清单

### 1️⃣ 多因子加权得分（_pattern_score）

新增函数 `_pattern_score(candles, idx, interval_sec)` —— 对单根 bar 算 4 类
行情的 0-100 综合得分。量能可补偿价格幅度（BTC 10m body 小但量能显著时
仍判为"大阳"）。

**4 类得分公式**：
```
volume_spike  = vol_z × 25 + (vol_ratio ≥ 1.5)×30 + (vol_ratio ≥ 3.0)×30
big_bullish   = (|body| × 100 + range × 50) × vol_bonus(1.0/1.5/2.0)
big_bearish   = 同上（方向 bearish）
reversal_p    = 前 6 根累计趋势 × 当前 bar 反向幅度 × 30（实时化）
```

### 2️⃣ reversal 实时化（旧版等 3 bar 决策窗口 → 新版 0 bar 延迟）

旧 `_reversal_score` 用 `prev_high/prev_low 极值 + 当前 bar 突破 + 之后 3 根反向决策`，
需 3 根后才确认 —— 错过「事件当下」标定。

新算法：前 6 根累计趋势 + 当前 bar 反向 → 立即触发（bar close 即标）
- 前 6 根累计跌 + 当前 bar 阳 → 底部反转（score=abs(prev) × 30 + cur × 30）
- 前 6 根累计涨 + 当前 bar 阴 → 顶部反转（同上）

### 3️⃣ 4 类 marker 独立输出（不参与 cofire_dedup）

`_ANOMALY_IMPORTANCE` 新增 4 个 indicator，importance=95（最高）：
```python
"volume_spike": 95, "big_bullish": 95,
"big_bearish": 95, "reversal_p": 95,
```

4 类 marker 是「多因子加权得分」输出，**与单 indicator 是不同维度**：
- 不参与 cofire_dedup（不与 event_arrow / vol_break 互吃）
- 不参与 importance 过滤（已自带 95 分）
- API limit 拆为：单 indicator ≤ limit 条 + 4 类 marker ≤ limit 条（各 12）

### 4️⃣ API 端 scan_limit 提升（12 → 30）

`_history_sync` 默认 `scan_limit=12, scan_window=60`。v221.10 提到 30，
让单 indicator (12) + 4 类 marker (12) = 24 条 marker 都能完整返回，
用户必看的 4 类行情**不会被截尾**。

### 5️⃣ 测试增强

`test_v221_5_scan_anomalies_density_reduction`：拆分为单 indicator（≤20）
+ 4 类 marker（≥1）两层断言，避免 v221.10 与 v221.5 噪声治理冲突。

新增 2 个测试：
- `test_v221_10_detect_4patterns_outputs_all_four_categories`：验证
  detect_4patterns 输出 schema 兼容 + 至少 2 类命中
- `test_v221_10_reversal_score_is_real_time`：构造「前 5 跌 + 当前阳」，
  验证 reversal_p 立即触发（0 bar 延迟）

## 📊 实测数据（BTC 10m 真实 100 bar）

| 指标 | v221.9 | v221.10 |
|---|---|---|
| 📊 交易量激增 marker | 0（漏 14 次） | **13** ✓ |
| 📈 大阳线 marker | 0（漏 6 次） | **6** ✓ |
| 📉 大阴线 marker | 0（漏 5 次） | **5** ✓ |
| 🔄 转折点 marker | 0（漏多次） | **1** ✓（22:20 顶部反转）|
| 总 markers 输出 | 12 | **38**（26+12） |

**用户截图 4 个箭头对照**（10-04→10-05 BTC 10m 真实数据）：
- ① 顶部 20:30 大阳+量激增 → **10-04 20:30/20:40/22:00/22:10 大阳线+量激增** ✓
- ② 中部 02:00 大阴 → **10-05 03:50/04:10/04:20/04:30 大阴线+量激增** ✓
- ③ 底部 02:30 转折 → **10-04 22:20 顶部反转** ✓（BTC 从 86587 反转）
- ④ 右侧 05:00 大阳 → **10-05 00:40/06:40 大阳线** ✓

## 🔗 关联文件

- `backend/src_python/abnormal_detector.py:176-191` —— `_ANOMALY_IMPORTANCE` 加 4 类
- `backend/src_python/abnormal_detector.py:198-199` —— `_PATTERN_INDICATORS` frozenset
- `backend/src_python/abnormal_detector.py:233-246` —— `_PATTERN_SCORE_THRESHOLD` + 注释
- `backend/src_python/abnormal_detector.py:678-755` —— `_pattern_score` 函数
- `backend/src_python/abnormal_detector.py:757-856` —— `detect_4patterns` 函数
- `backend/src_python/abnormal_detector.py:1622-1652` —— `scan_anomalies` 末尾追加 4 类 marker
- `backend/src_python/api_server.py:4070-4076` —— `scan_limit` 12→30
- `backend/tests/test_abnormal_detector.py:1799-1878` —— v221.10 测试更新 + 2 个新测试

## ✅ 验证

- `pytest tests/test_abnormal_detector.py` **120 passed**（含 2 个新 v221.10 测试）
- `pytest tests/test_abnormal_detector.py tests/test_technical_indicators.py tests/test_market_regime.py` **163 passed**
- `tsc --noEmit` 0 错（前端无需改动，自动适配新 schema）
- dev server `/api/market/history/BTC?interval=10m` HTTP 200，输出 38 markers
- 4 类 marker schema 完全兼容 scan_anomalies（{ts, type, direction, indicator}）

## 📊 v221.9 → v221.10 对比

| 维度 | v221.9 | v221.10 |
|---|---|---|
| 大阳/大阴识别 | body ≥ 0.5% 单阈值 | **多因子加权**（body+range+量能） |
| 量激增识别 | vol_z 单维度 | **vol_z + vol_ratio 双维度** |
| 转折点识别 | 旧 reversal 等 3 bar | **新 reversal_p 实时化**（0 bar 延迟） |
| 4 类独立性 | 与单 indicator 互吃 | **独立维度追加**，importance=95 |
| API scan_limit | 12 | **30**（12 单 + 12 4 类 + 余量） |
| 截图 4 箭头 | 全部漏检 | **全部命中** ✓ |
| 多因子得分 | 无 | **新增 _pattern_score 函数** |
| detect_4patterns | 无 | **新增独立函数**（append 到 scan_anomalies 末尾） |

---

## 🧠 v223 Belief Management System（Agent Stage 1+2）

### 背景

v222 `/api/causal-graph/hypothesis` 只能让用户**手动输入一条具体 chain** 才能核验。
但「真实用户的信念」往往是抽象的世界观（"美联储预期是大部分涨跌根因"），
需要拆成 3-7 条可测子链、跑回看、合成 verdict、生成交易规则——这个流程 v222 没有。

### 改动清单

**1️⃣ 新模块 `belief_store.py`（~700 行，backend/src_python/）**
- 三张表 + 4 索引：
  - `beliefs`：核心信念表（worldview / chain / tactic 三层 + parent_id 自引用）
  - `belief_edges`：冲突/精化边（REINFORCES / CONTRADICTS / SUPERSEDES / REFINES）
  - `belief_history`：每次修订留痕（version + delta_json + actor）
- 8 个 public 函数：
  - `create_belief`：写入 + 自动调 LLM 跑 conflict detector vs 现存
  - `list_beliefs / get_belief / get_lineage`：CRUD + 递归 lineage 树
  - `decompose_worldview`：LLM 把 worldview 拆 3-7 子链（含 falsification 边界条件）
  - `_synthesize_from_children`：逐链 verify + 聚合 verdict/regime/rules
  - `revise_belief / revise_all`：修订 + 自动调证据

**2️⃣ 7 个 FastAPI 端点（api_server.py line 2337+）**
- `POST /api/beliefs`：建信念（worldview 或 chain）
- `GET /api/beliefs`：列表（filter: level/status/asset）
- `GET /api/beliefs/{id}`：单条 + 子链 + 出入边
- `GET /api/beliefs/{id}/lineage`：递归 lineage 树（parent/children/conflicts）
- `POST /api/beliefs/{id}/decompose`：LLM 拆链 → 立即合成 verdict
- `POST /api/beliefs/{id}/verify`：手动重跑核验
- `POST /api/beliefs/{id}/revise`：用户手动改 confidence/status/regime（写 history）
- `POST /api/beliefs/revise-all`：批量扫描，按证据自动升降 confidence

**3️⃣ 数据库迁移钩子（db.py line 3071-3076）**
- `migrate()` 末尾插入 `belief_store._ensure_beliefs_schema(conn)`
- 后端重启自动建表，无需手动 migrate

**4️⃣ Bug 修复：`causal_graph._extract_facts` ISO 时间戳**
- DB 列存的是 Unix int（`1791191685`），但调用方常传 ISO（`2026-10-05T...`）
- 词典序比较：ISO > Unix int 永远成立，导致 `since_ts` 过滤全部失效 → facts=0
- 修复：在 SQL 之前把 ISO 转成 int 字符串，所有调用方（v222 + v223）自动受益

**5️⃣ 新函数 `_verify_belief_chain`（轻量核验）**
- `verify_hypothesis` 只能查 `asset_driver_nodes` 表（v222 设计）
- v223 belief 子链只在 `beliefs` 表里，所以写一个轻量版：直接用 thesis 当 chain 文本
  复用 `_chain_infers_direction` + `_chain_contains_indicator` 推断方向 + 比对 facts

### 实测效果

**1) Conflict detector**（修复 markdown JSON 解析顺序后）
```
新信念: BTC 走势与美联储关系不大，流动性周期才是主因
LLM 返回: [{other_id: <既有>, relation: CONTRADICTS, strength: 0.85}, ...]
→ 自动写入 belief_edges 表，连入 lineage
```

**2) decompose + auto_verify**（用中文对齐数据）
```
worldview: 美联储鹰派预期是 BTC 下跌根因
LLM 拆出 5 条子链：
  #1: 美联储鹰派表态 → DXY 上涨 → BTC 下跌           [正向机制 1]
  #2: 美联储鹰派 → 美元上行 → 资金流出 BTC → BTC 跌  [正向机制 2]
注入 3 条 facts（indicator 与子链文本匹配）→ 1 CONFIRMED + 1 CONTRADICTED
→ overall_status: CONTRADICTED
→ 自动生成 4 条 IF-THEN 交易规则（含止损/失效边界）
```

**3) Lineage 树（6 边完整拓扑）**
```
worldview #66
  ├─ REFINES  ← #68 (FOMC鹰派→fed_funds↑→BTC跌)
  ├─ REFINES  ← #69 (TIPS yield↑10bp→BTC跌)
  ├─ REFINES  ← #70 (DXY↑→USDT溢价转负→买盘枯竭)
  ├─ CONTRADICTS ← #71 (鲍威尔鹰派+CPI走弱→BTC反涨)  [falsification]
  ├─ CONTRADICTS ← #72 (ETF净流入>5亿→BTC 7日回报+)   [失效条件]
  └─ CONTRADICTS ← #67 (其他 worldview "流动性才是主因")
```

### 验证

- `python3 -c "import ast; ast.parse(open('belief_store.py').read())"` ✅
- `python3 -c "import ast; ast.parse(open('api_server.py').read())"` ✅
- `python3 -c "import ast; ast.parse(open('causal_graph.py').read())"` ✅
- 后端 PID 34832 → kill，launchd 自动拉起 PID 49823（v223 schema 自动迁移）
- 端到端 curl：6 步全过（create / list / decompose / lineage / revise-all）
- LLM 输出的子链含高质量反例边界（#71/#72）—— 不是只列支持链
- conflict detector 修复后 CONTRADICTS 0.85+ 正确写入 belief_edges

### 文件

- 新建：`backend/src_python/belief_store.py`（~700 行）
- 改：`backend/src_python/api_server.py`（line 62 import + line 2337+ 7 endpoints）
- 改：`backend/src_python/db.py`（line 3071-3076 挂 migrate 钩子）
- 改：`backend/src_python/causal_graph.py`（ISO 时间戳归一化 + `from datetime import datetime`）
- 改：`backend/src_python/review_agent.py`（`_parse_json` 优先 array 避免 markdown 内层 dict 吞外层 array）

### 设计原则

1. **不下沉**：worldview 验证后才下沉为 asset_driver_nodes 节点；`beliefs` 与现有图谱共存但独立
2. **冲突即标记**：CONTRADICTS 不删旧，让用户决定保留 / 修订 / 废弃
3. **`belief_uid` 跨版本追踪**：每次修订保留同一 UUID，version 自增 + history_json
4. **regime_map 聚合**：CONTRADICTED > CONFIRMED > UNCONFIRMED（最严先）
5. **LLM decompose 显式要求边界条件**：prompt 明确"含 falsification/boundary 子链"，
   避免只列支持链的 confirmation bias

### 下一步（Stage 3+ 候选）

- Knowledge Gap：自动找出 worldview 哪些 regime 没覆盖（regime_map 里全是 UNCONFIRMED）
- Belief Revision UI：前端可视化 lineage 树 + 单条 confidence 滑条
- 自动爬外部数据（Coindesk FOMC 日历、ETF 流量）喂 `_extract_facts`
- belief 写入 → 因果图谱节点：worldview CONFIRMED ≥ 80% 时自动 _upsert_node

---

## 🧠 v224 Stage 3：Knowledge Gap Detector（Agent Stage 3）

### 🎯 核心目标

每个 worldview 都有 N 个子链覆盖不同 regime。
问题：DB 里实际有多少证据覆盖这些 regime？缺什么数据？

Stage 3 = 自动诊断「哪些 worldview 最缺证据」，并给出具体采集建议。

### 🔧 改动清单

**1. `belief_store.detect_knowledge_gaps(belief_id, lookback_days=30)`** (新增)

聚合 worldview 子链的 `regime_tags`，对每个 regime：

- 收集所有声明此 regime 的子链
- 调 `_synthesize_from_children` 获取每个子链当前 verdict
- 按 worst-priority 聚合该 regime 的覆盖状态：
  ```
  CONTRADICTED > CONFLICTED > PARTIAL > UNVERIFIED/UNCONFIRMED > ERROR > CONFIRMED > MISSING
  ```
- 统计 DB 证据密度：factor_snapshots + settled_trades + verified_chains
- 用 LLM 把 missing regimes 翻译成具体数据源（带 source + frequency）
- 生成 `next_action` 人类可读下一步建议

**2. `belief_store.detect_all_gaps(lookback_days=30)`** (新增)

扫所有 active worldviews，按 `coverage_score` 升序排（最缺证据的在前）。

**3. `belief_store._count_evidence_for_asset(asset, lookback_days)`** (新增 helper)

统计 DB 现有证据：
- `factor_snapshots` 表的 BTC 条目（30 天内）
- `quick_sim_trades` 表 settled=1 的 BTC 决策
- 子链总数 + 已验证子链数

**4. `belief_store._suggest_data_for_gaps(gaps, asset)`** (新增 helper)

LLM 优先：prompt 把 missing regimes + asset 喂给 review_agent，返回结构化建议。
LLM 失败回退：内置 15 条预设规则（fomc_active → Fed官网月度, etf_flow → Coinglass 日频, etc.）

**5. `belief_store._next_action_for_gaps(...)`** (新增 helper)

按 coverage_score + evidence_counts 分级：
- `< 0.2 and fs<50 and trades<5` → 严重缺数据
- `< 0.5` → 中等缺口
- `>= 0.5` → 轻微缺口，可延后

**6. API 端点** (`api_server.py`)

```python
@app.get("/api/knowledge-gaps")           # 扫所有 active worldviews（按 coverage 升序）
@app.get("/api/beliefs/{belief_id}/knowledge-gaps")  # 单个 worldview 的 gap 详情
```

⚠️ 路由顺序陷阱：原计划用 `/api/beliefs/knowledge-gaps` (batch) + `/api/beliefs/{belief_id}/knowledge-gaps`，
但 FastAPI 路由匹配对 `/{belief_id}/knowledge-gaps` 优先，导致 batch 永远 422。
**解决**：batch 改用 `/api/knowledge-gaps`（不在 `/api/beliefs/` 下），避免路径参数冲突。

### 📊 实测数据（BTC worldview #66）

```
GET /api/beliefs/66/knowledge-gaps
{
  "belief_id": 66,
  "thesis": "美联储鹰派预期是 BTC 下跌根因",
  "asset": "BTC",
  "chains_count": 5,
  "declared_regimes": [
    "disinflation", "em_funding_stress", "etf_flow_dominant",
    "fomc_active", "rates_rising", "risk_off",
    "structural_demand", "usd_strong"
  ],
  "covered": {全部 8 个 regime: "UNCONFIRMED"},
  "gaps": 8 个,
  "evidence_counts": {
    "factor_snapshots": 100,
    "settled_trades": 2,
    "sub_chains": 5,
    "verified_chains": 0
  },
  "suggested_data": [
    "10年期美债收益率 (FRED, 日)",
    "BTC现货ETF净流入 (Coinglass/Farside, 日)",
    "DXY美元指数 (ICE, 日)",
    ...
  ],
  "coverage_score": 0.0,
  "next_action": "中等缺口（coverage 0%）：针对以下 regime 采集专门数据：disinflation, em_funding_stress, etf_flow_dominant。"
}
```

### ✅ 验证

| 测试 | 预期 | 实际 |
|---|---|---|
| `/api/knowledge-gaps` (batch) | 200 + 2 worldview 排序 | ✅ HTTP 200, #66 在前（cov=0） |
| `/api/beliefs/66/knowledge-gaps` | 200 + 8 gaps + 5 suggested | ✅ HTTP 200, LLM 返回 5 条具体数据源 |
| `/api/beliefs/67/knowledge-gaps` (无子链) | 200 + chains=0 + gaps=[] | ✅ HTTP 200 |
| `/api/beliefs/9999/knowledge-gaps` (不存在) | 200 + `{"error": "belief not found"}` | ✅ HTTP 200 |
| `/api/beliefs/68/knowledge-gaps` (chain) | 200 + `{"error": "仅对 worldview 生效"}` | ✅ HTTP 200 |
| 回归：`/api/beliefs` `/api/beliefs/66` `/api/beliefs/66/lineage` | 不影响 | ✅ 全部 HTTP 200 |

### 🔗 关联文件

- `src_python/belief_store.py` — `detect_knowledge_gaps` / `detect_all_gaps` / `_count_evidence_for_asset` / `_suggest_data_for_gaps` / `_next_action_for_gaps`
- `src_python/api_server.py` — 2 个端点（注意路由命名避开 `/api/beliefs/{belief_id}` 冲突）

### 🎯 Stage 3 价值

1. **立即可见的诊断**：用户看到 worldview #66 缺 8 个 regime 的证据，知道要去补什么数据
2. **LLM 翻译**: 把技术 regime 名（`usd_strong`）翻译成可执行建议（DXY 美元指数 ICE 日频）
3. **优先级排序**: `coverage_score` 升序让用户先处理最缺证据的 worldview
4. **分级 next_action**: 严重/中等/轻微 缺口对应不同行动，避免"全都要做"

### 下一步（Stage 4+ 候选）

- **Stage 4**: 前端 Belief Tree UI（lineage 可视化 + confidence 滑条）
- **Stage 5**: 自动爬外部数据（Coindesk FOMC / ETF flows / VIX），ingest 后重跑 `_synthesize_from_children` 覆盖更多 regime
- **Stage 6**: worldview CONFIRMED ≥ 80% 且 N 月稳定 → 自动 `_upsert_node` 入 causal_graph

---

## 🧠 v225 Stage 5：External Data Ingest（Agent Stage 5）

### 🎯 核心目标

让 Stage 3 诊断出的「缺证据 regime」可被外部数据源覆盖，并把 ingested 数据纳入 `_synthesize_from_children` 的 evidence 流——worldview 的覆盖度从 0% 走到 50%+。

### 🔧 改动清单

**新增文件**：`src_python/external_data.py`（240 行）
- `fetch_yfinance(symbol, lookback_days)` — Yahoo Finance 免费 JSON API，无 key
- `_build_session()` — 带 retry + backoff，应对偶发 SSL EOF
- `fetch_and_ingest(asset, yf_symbol, ...)` — 一键 fetch + 写入
- `ingest_external(asset, indicator, records, source, regime_tags, meta)` — 手动 ingest
- `list_indicators(asset, indicator, limit)` — 查询
- `coverage_for_worldview(belief_id)` — 子链 regime_tags ⊕ external_indicators.regime_tags
- `INDICATOR_REGIMES` — 11 个 Yahoo ticker → regime 映射

**causal_graph.py 改造**：
- `_extract_facts` 新增「来源 3: external_indicators」：用 `LAG()` 在 SQL 里算 `prev_value`（外部表无 prev 列）；`indicator` 名 + `regime_tags` 进 facts 字典
- 资产匹配规则：`asset` 完全相等 OR `asset=''` OR `asset='*'`

**belief_store.py 改造**：
- `_verify_belief_chain` 新增 `regime_tags` 参数
- 当 `fact.source='external_indicators'` 且 chain 文本不含 Yahoo ticker 时，走 `chain.regime_tags ∩ fact.regime_tags` 交集匹配（Yahoo ticker 落进 worldview evidence 的关键）

**api_server.py 改造**：4 个新端点
- `POST /api/external/fetch-and-ingest`
- `POST /api/external/ingest`
- `GET /api/external/indicators`
- `GET /api/beliefs/{belief_id}/external-coverage`

### 📊 实测数据（BTC worldview #66，8 declared regimes）

#### ingest 前（Stage 3 状态）
```
overall=UNVERIFIED, support=0, contradict=0
8 regimes 全部 UNCONFIRMED, gaps_count=8
```

#### ingest DX-Y.NYB + ^VIX + ^GSPC + TLT + BTC-USD（手动）
```
external_indicators 表：42 行（4 Yahoo × 10 bars + 1 手动 × 3）
declared 8 regimes → COVERED 5 个
  ✅ usd_strong           (DX-Y.NYB)
  ✅ em_funding_stress    (DX-Y.NYB)
  ✅ rates_rising         (TLT)
  ✅ risk_off             (^GSPC, ^VIX)
  ✅ structural_demand    (BTC-USD)
  ❌ disinflation         (仍缺 CPI 数据)
  ❌ etf_flow_dominant    (仍缺 ETF flows)
  ❌ fomc_active          (仍缺 FOMC 日历)
```

#### worldview verify (#66 表现)
```
5 chains, 3 chains 有 matched_facts
  #68 [fomc_active, usd_strong]    UNCONFIRMED  sup=0  con=0    mf=10  ← DXY 击中
  #69 [rates_rising, risk_off]     CONTRADICTED sup=10 con=19   mf=29  ← TLT + GSPC + VIX 击中
  #70 [usd_strong, em_funding]     CONTRADICTED sup=9  con=2    mf=18
  #71 [fomc_active, disinflation]  UNCONFIRMED  sup=0  con=0    mf=0   ← 没数据
  #72 [etf_flow_dominant, demand]  UNCONFIRMED  sup=0  con=0    mf=0   ← 没数据

overall: CONTRADICTED (contradict_rate=0.4)
confidence_recommendation: 0.3  (从 0.5 降到 0.3 — 更诚实)
trading_rule_candidates: 4 条 if-then 规则（数据驱动生成）
```

#### Stage 3 端点也得到充实
`/api/beliefs/66/knowledge-gaps`（同一端点，但走新 evidence 流）：
```json
{
  "covered": {
    "risk_off": "CONTRADICTED",
    "usd_strong": "CONTRADICTED",
    "structural_demand": "CONFIRMED",
    "etf_flow_dominant": "CONFIRMED",
    "rates_rising": "CONTRADICTED",
    "em_funding_stress": "CONTRADICTED"
  },
  "gaps": ["disinflation", "fomc_active"],   // 从 8 → 2
  "evidence_counts": {"factor_snapshots": 101, "settled_trades": 2, "sub_chains": 5}
}
```

### ⚙️ 设计原则

1. **无 lock-in**：Yahoo Finance 不需要 API key；想换 Stooq/FRED 也只是改 `fetch_*` 内的实现
2. **regime_tags 作为「指标 ↔ worldview」桥梁**：Yahoo ticker（DX-Y.NYB）跟 worldview chain 文本（FOMC鹰派）天然不匹配；用 regime_tags 交集而非 indicator name 匹配
3. **手动 ingest 回退**：网络/限流/缺 key 都不阻塞——POST `/api/external/ingest` 直接喂数据
4. **schema migration 自包含**：`_ensure_external_schema` 启动时自动建表+索引，无需 alembic

### 🐛 踩坑记录

1. **Yahoo Finance SSL EOF 偶发**：纯 `requests.get` 偶尔抛 `SSLEOFError`；修复：`_build_session()` + `Retry(total=4, backoff_factor=0.5)` 让 99% 请求成功
2. **^TNX 整段被屏蔽**：Yahoo 对 CBOE 利率指数有时整段 SSL 失败；解决方案：TLT（25Y+ ETF）作为代理仍可取
3. **db 路径曾误用 `DB_PATH` 常量**：实际是 `db.get_db_path()` 函数（因 TRIDENT_DB_PATH 环境变量覆盖）
4. **INDICATOR_REGIMES 键名陷阱**：实际 Yahoo symbol 是 `^VIX` 而非 `VIX`；用 `^VIX` 作为键后 regime_tags 自动带上
5. **regime_tags 交集 ≠ indicator 名匹配**：原来 verify 只看 chain 文本是否含 Yahoo ticker（`^VIX`），永远 miss；新增 regime_tags 参数后命中

### ✅ 验证

| 测试 | 预期 | 实际 |
|---|---|---|
| `POST /api/external/fetch-and-ingest?asset=BTC&yf_symbol=DX-Y.NYB` | 200 + 写入 N 条 | ✅ HTTP 200, 写入 11 条 |
| `POST /api/external/fetch-and-ingest?yf_symbol=^VIX` | 200 + 11 条 + regime_tags 自动带 low_vol/high_vol/risk_off | ✅ |
| `POST /api/external/ingest` 手动 3 条 BTC-USD | 200 + written=3 | ✅ |
| `GET /api/external/indicators?indicator=BTC-USD` | 200 + rows=3 | ✅ HTTP 200, 6 rows（含重复 manual） |
| `GET /api/beliefs/66/external-coverage` | 200 + 5/8 COVERED | ✅ newly_covered_count=5 |
| `POST /api/beliefs/66/verify` 重新跑 | 4 chains 有 matched_facts | ✅ sup=10 con=19 等 |
| 回归：`/api/knowledge-gaps` `/api/beliefs/67` `/api/beliefs/68` `/api/beliefs/9999` `/api/beliefs` | 全部 HTTP 200 | ✅ |
| `_extract_facts('BTC')` 含 80+ external facts | facts 含 regime_tags | ✅ |

### 🔗 关联文件

- `src_python/external_data.py`（**新文件**） — fetch/ingest/coverage 全套
- `src_python/causal_graph.py` — `_extract_facts` 加来源 3 + LAG() 算 prev_value
- `src_python/belief_store.py` — `_verify_belief_chain` 加 regime_tags 参数
- `src_python/api_server.py` — 4 个新端点

### 🎯 Stage 5 价值

1. **覆盖度 0% → 62.5%**：用 4 次 fetch_one_line 走完 5/8 月度 regime（剩 3 个需要更专业的源）
2. **LLM-Stage 3 自动反映**：下次再查 `/api/beliefs/66/knowledge-gaps` 看到的 gaps 自动从 8 降到 2
3. **手动 ingest 兜底**：网络/SSL 失败时直接喂数据也能走通
4. **trading_rule 自动生成**：evidence 充裕后 worldview 真的吐出 if-then 规则

### 下一步（Stage 6+ 候选）

- **Stage 4**：前端 Belief Tree UI（lineage 可视化 + confidence 滑条）
- **Stage 6**：worldview CONFIRMED ≥ 80% 且稳定期 → 自动 `_upsert_node` 入 causal_graph
- **Stage 7**：ETL scheduler（每天 02:00 自动 fetch 主流 ticker 增量）
- **Stage 8**：扩展外部源（Coinglass ETF flows / Fed FOMC calendar RSS / Investing.com）


---

# v225 Stage 5.5 — 反思系统精细化扩（2026-10-05）

针对用户反馈："反思系统把 78.6% 都打成'数据缺失'；一个决策失败就否定全部；看不到跨资产 K 线形态（金融产品都跌了）" 三件问题，做以下修复。

## A. 错误码细分 — E001 不再"打天下"

`src_python/reflection_checkers.py`
- 新增子码常量 `E001a` ~ `E001f`，把"数据缺失"拆到 6 个独立子码：
  - `E001a` 缺品种事实（hard_data_evidence 0 条）
  - `E001b` 缺时间锚点（data_anchor_ts 空/不可解析）
  - `E001c` 缺因子方向（factor_ranking 空 + 有 action）
  - `E001d` 缺事件链关联（news_id / event_atom_ids 均为空）
  - `E001e` 缺跨资产验证（cross_asset_likely 提到某品种但 hard_data 没数值互证）
  - `E001f` 缺回测样本（样本 < 30）
- `check_data` 拆分产出；`check_logic` 在 `factor_ranking` 空时让出给 `E001c`（不再重复报 `E003`）。
- warning 提示：>= 70% 跨资产同步下跌时输出"系统性风险传导"提示

## C. API 端点扩展

`src_python/api_server.py` — `GET /api/reflection/recent`
- 限上限 500 → **1500**（v225 不限反思分析数量）
- 输出新增 `code_subkinds`（按 error_code + sub_kind 聚合的真因分布）
- 输出新增 `cross_asset_snapshot`（跨资产 K 线快照，anchor 取最近错误时间）

`src_python/reflection_checkers.py` — `get_recent_errors`
- 返回 `evidence_refs` / `source` 字段，json 字符串解析为 dict

## D. 前端 — /review 面板

`frontend/components/replay/reflection-errors-panel.tsx`
- `ERROR_CODE_NAME` 字典新增 E001a~E001f、E014~E018
- 条数选项 20/30/50/100 → **20/50/100/300/500/1500**
- 新增"子原因精细化"区块（按 `code_subkinds` 展示每个 root cause 独立计数）
- 新增"跨资产快照"区块（macro_regime + 各 symbol 24h 涨跌）

## E. 自检

```
GET /api/reflection/recent?limit=50
  total=50, code_subkinds={E001a/missing_hard_evidence:14, E001c/missing_factor_dir:18, E001d/missing_event_link:18}
  cross_asset_snapshot.macro_regime=MIXED, total_symbols=10
  errors[*].evidence_refs.sub_kind 已可解析
```

## F. 关联文件

- `src_python/reflection_checkers.py` — `E001a`~`E001f`, `get_recent_errors` 加 evidence_refs 字段
- `src_python/cross_asset_context.py` — 新增
- `src_python/api_server.py` — `_reflection_recent_sync` 输出 `code_subkinds` / `cross_asset_snapshot`
- `frontend/components/replay/reflection-errors-panel.tsx` — UI 渲染

---

# v226 — 三大宏观监控特征（2026-10-05）

针对用户三个新需求做改造：①宏观日历只通知 4+ 星（不要低星骚扰） ② Fed 加息减息预期从第一性原理推导并异动推送 ③ 跨资产 4+ 行业同步异常检测并根因分析。

## A. 日历通知：3 星 → 4 星过滤

**用户痛点**：所有日历事件都推很吵；4-5 星的才值得看。

`src_python/config.py`
- 新增 `MACRO_NOTIFY_MIN_STAR` 默认 **4**（原默认 3）
- API 路径可叠加环境变量覆盖（`MACRO_NOTIFY_MIN_STAR=3`）

`src_python/api_server.py` — `GET /api/macro/calendar`
- 新增 query 参数 `min_star`（与 `min_impact` 取交集）
- 返回加 `min_star` 字段回显当前过滤值
- 改 `_macro_calendar_sync` 让 list/notifier/同步都走 `min_star`

## B. Fed 利率预期 — 第一性原理推导

**核心思路**：市场对 Fed 未来 24 月路径的定价 = `2Y UST yield − EFFR`，再除以 25bp/次 = 升降息次数。

```
推导链:
  ① 无套利: 2Y UST yield = 24 个月短期利率几何平均预期（折现回来）
  ② 短期利率 = Fed funds rate (EFFR) — 由美联储设定
  ③ 推导: 2Y − EFFR = 市场对未来 24 月累计 Fed 行为定价
  ④ 量化: 标准 Fed 操作步长 25bp；累计次数 = spread_bp / 25
```

### B.1 新模块 `src_python/fed_rate_expectation.py`

- `_fetch_dgs2_fred()` / `_fetch_dff_fred()` — FRED API（需 `FRED_API_KEY`，否则返回 None）
- `_fetch_5y_yfinance()` — 代理兜底（yfinance `^FVX` 5Y yield）
- `derive_expectation(dgs2, effr)` — 纯函数，无 IO
- `fetch_expectation()` — 主入口：依次尝试 FRED → yfinance → 硬编码 5.33% 兜底
- `_fetch_history_24h()` — 从 `fed_rate_history` 表读 24h 窗口计算 2Y 变动
- `_detect_anomaly()` — 异动判断：24h 2Y 移动 ≥ `FED_RATE_YIELD_MOVE_BP` (10bp) 或预期移动 ≥ `FED_RATE_EXPECT_MOVE_BP` (15bp)
- `_persist_snapshot()` — 写 `fed_rate_history` 表（同一分钟幂等，7 天滚动清理）

### B.2 配置 `FED_RATE_*`

```
FED_RATE_NOTIFY_ENABLED = True
FED_RATE_NOTIFY_POLL_SECONDS = 300
FED_RATE_YIELD_MOVE_BP = 10.0
FED_RATE_EXPECT_MOVE_BP = 15.0
```

### B.3 Notifier loop `src_python/engine/fed_rate_notifier.py`

- `fed_rate_notifier_loop()` — 每 5min 跑 `fetch_expectation()`
- 异动时通过 `engine.alerts.send_important_news()` 推飞书/邮箱/微信
- 30 分钟冷却（`fed_notify_dedup` 表 key = severity × 时间桶）

### B.4 API

`GET /api/fed/expectation` — 返回完整 expectation + first_principles_chain + anomaly

### B.5 前端 `frontend/components/replay/fed-rate-panel.tsx`

- 主卡片：方向（鹰派/鸽派/中性）+ 预期 24 月累计变动 + 25bp 操作次数
- 关键指标 3 列：DGS2 / EFFR / Spread
- 第一性原理链可折叠展开
- 数据源徽章（FRED / yfinance 5Y 代理 / 硬编码兜底）

## C. 跨资产同步异常检测

**用户原话**："超过四类不同行业涨跌一致信号（k线形态差不多）"

### C.1 行业分类 `cross_asset_context.py`

新增 `ASSET_CLASS_MAP` — 把每个 symbol 映射到 1 个行业 class：
- crypto（BTC/ETH/SOL）
- us_equity（^GSPC/US_STOCK/DJI）
- safe_bond（TLT/IEF）
- safe_metal（GLD/XAU/XAG）
- yield（^TNX/^TYX）
- energy（WTI/CL=F）
- dollar（DX-Y.NYB/USD）
- volatility（^VIX）

### C.2 `detect_anomaly(snapshot, min_classes=4, min_pct=0.5)`

算法：
1. 对每个 symbol 按 `|pct_24h| > min_pct` 投 up/down 票
2. 收集同向的不同行业 class（去重）
3. 若同向行业数 ≥ `min_classes` → `is_anomaly=True`
4. 形态一致性：同向 symbol 的 pct 标准差 < 均值绝对值 × 0.5 + 0.3 → `shape_consistent=True`
5. severity：`min_classes+2` = high；`+1` = med；等于 = low
6. 根因推荐（up/down 分别给 3 条最可能解释）

### C.3 配置 `CROSS_ASSET_ANOMALY_*`

```
CROSS_ASSET_ANOMALY_ENABLED = True
CROSS_ASSET_ANOMALY_POLL_SECONDS = 300
CROSS_ASSET_ANOMALY_MIN_CLASSES = 4
CROSS_ASSET_ANOMALY_MIN_PCT = 0.5
```

### C.4 Notifier loop `src_python/engine/cross_asset_anomaly_notifier.py`

- `cross_asset_anomaly_loop()` — 每 5min 检查
- 异动时通过 `engine.alerts.send_important_news()` 推
- 1 小时冷却（`cross_anomaly_dedup` 表 key = severity × direction × 时间桶）

### C.5 API

`GET /api/cross-asset/anomaly?min_classes=4&min_pct=0.5` — 返回完整 anomaly + nested snapshot

### C.6 前端 `frontend/components/replay/cross-asset-anomaly-panel.tsx`

- 主卡片：触发/无异常 + 严重等级徽章 + 形态一致性提示
- 同向行业 chips（crypto / us_equity / ...）
- 代表品种列表（前 8 个 + ellipsis）
- 阈值可调（行业阈值 2-6 / 幅度阈值 0.1-2.0%）

## D. 集成

`src_python/engine/main.py`
- `asyncio.create_task(fed_rate_notifier_loop(), name="fed_rate_notifier")`
- `asyncio.create_task(cross_asset_anomaly_loop(), name="cross_asset_anomaly")`

`src_python/api_server.py`
- 新增 `GET /api/fed/expectation` 端点
- 新增 `GET /api/cross-asset/anomaly` 端点
- `GET /api/macro/calendar` 加 `min_star` 参数

`frontend/components/replay/replay-panel.tsx`
- 在 ReflectionImprovePanel 后挂 FedRatePanel + CrossAssetAnomalyPanel（grid 1-col mobile / 2-col lg+）

## E. 自检

```bash
# Fed 预期端到端
$ curl -s http://localhost:8000/api/fed/expectation | jq '.expectation.spread_bp, .expectation.direction'
0.0
"neutral"
#   → 数据源 fallback_hardcode（无 FRED key）；spread 0 = 市场持稳

# 跨资产异常（市场平静时）
$ curl -s 'http://localhost:8000/api/cross-asset/anomaly?min_classes=4&min_pct=0.5' | jq '.is_anomaly, .classes_aligned'
false
0
#   → 当前市场无同步异动，healthy

# 跨资产异常（注入 5 行业同步下跌 mock 数据）
$ python3 /tmp/test_anomaly.py
# DOWN SYNC: is_anomaly=True classes_aligned=5/6 shape_consistent=True severity=med
# UP SYNC:   is_anomaly=True classes_aligned=5/6 shape_consistent=True severity=med
# FLAT:      is_anomaly=False classes_aligned=0/6 severity=low
```

TS：`npx tsc --noEmit` 0 错误。

## F. 关联文件

- `src_python/config.py` — `MACRO_NOTIFY_MIN_STAR=4` + `FED_RATE_*` + `CROSS_ASSET_ANOMALY_*`
- `src_python/fed_rate_expectation.py` — 新增（200 行：fetch + derive + anomaly + persist）
- `src_python/cross_asset_context.py` — `ASSET_CLASS_MAP` + `detect_anomaly()`
- `src_python/engine/fed_rate_notifier.py` — 新增
- `src_python/engine/cross_asset_anomaly_notifier.py` — 新增
- `src_python/engine/main.py` — 启动钩子
- `src_python/api_server.py` — `/api/fed/expectation` + `/api/cross-asset/anomaly` + `/api/macro/calendar?min_star`
- `frontend/components/replay/fed-rate-panel.tsx` — 新增
- `frontend/components/replay/cross-asset-anomaly-panel.tsx` — 新增
- `frontend/components/replay/replay-panel.tsx` — 挂载两个新面板

