# audit — 决策审计层

> 对一条**已经下过的判断**做 7 层审计，输出结构化的 finding 与可执行修复动作。
> 不预测行情、不产生信号、不下单。

这个包是从 `backend/src_python` 的反思内核里抽出来的**独立交付层**，边界只有一条：

> **输入一条决策记录 + 一组数据源，输出一份审计报告。** 不 import `db`、不写库、不改任何业务表。

---

## 快速开始

```bash
cd <repo root>

python3 -m audit.cli selftest                      # 自检：3 个构造样本，不需要数据库
python3 -m audit.cli export-providers --db backend/trident_event_bus.db --out audit_out/providers
python3 -m audit.cli replay --db backend/trident_event_bus.db --limit 516 --out audit_out/run
python3 -m audit.cli replay --db backend/trident_event_bus.db --limit 516 --out audit_out/pre --mode pretrade
python3 -m audit.cli run --input decisions.jsonl --providers audit_out/providers --out audit_out/file
python3 -m audit.cli gate --db backend/trident_event_bus.db --enforce-sim    # 只算：真拦会拦掉谁
python3 -m audit.cli gate --db backend/trident_event_bus.db --limit 516 --commit  # 补审并留痕
python3 -m pytest audit/tests -q                   # 21 条离线测试
```

**同步门禁（W3）**：`ai_worker` 在决策 INSERT 之后、提交之前调 `audit_gate_hook.gate()`，
每条决策都审，findings 落 `decision_errors`（`source='audit_gate'`）、verdict 落 `audit_gate_log`。
默认 shadow 只审不改（真拦尚未接线，`--enforce-sim` 能算"会拦谁"）。
覆盖率、延迟、回滚方式见 `W3-同步门禁.md`。

程序化调用：

```python
from audit import audit, CheckContext

with CheckContext.from_file("audit_out/providers") as ctx:     # 或 from_sqlite / from_http
    report = audit(decision, ctx)
report["verdict"]        # clean | warn | reject | insufficient
report["findings"][0]    # {code, severity, layer, detail, rule_id, remediation, evidence_refs, basis}
```

---

## 目录

```
audit/
├── contracts/          decision.v1 / audit_report.v1 两个 JSON Schema + 轻量校验
├── core/
│   ├── context.py      CheckContext：唯一外部依赖入口（4 个构造器）
│   ├── report.py       Finding / LayerResult / verdict / grade / summarize（纯函数）
│   └── pipeline.py     契约校验 → 完整性短路 → 7 层 → 报告
├── checkers/
│   ├── legacy.py       适配现有 reflection_checkers（注入空壳 db，不复制代码）
│   ├── remediation.py  修复建议补齐 + basis（决策当时可判 / 需事后行情）
│   └── layers/         7 层各一个模块：data / logic / rules / causal / market / event / reasoning
├── gate.py             同步门禁：decide / persist / gate_row / coverage（W3）
├── providers/          null / file / http / sqlite 四种数据源 + 内存快照
├── examples/           selftest 样本
└── tests/              离线回归（契约 / provider / 等价性）
```

---

## 数据源解耦（W2）

内核只依赖 **5 张表**（`review_insights` / `asset_driver_nodes` / `signal_performance` /
`quick_sim_trades` / `ai_decisions`），把它们抽成 4 组 provider 数据：

| provider | 数据 | 失败时行为 |
|---|---|---|
| `rules` | 铁律（id/rule/active/kind） | 层报 not_applicable + 原因 |
| `drivers` | 驱动因子节点（asset/label/is_core/active） | 因果层降级 |
| `history` | 历史样本 / 成交样本 / 决策最小列 | 事件因果层降级 |
| `kline` | K 线（**内核当前不用，接口预留**） | — |

### 文件格式（`providers.v1`）

```
providers/
├── rules.json    {"version":"providers.v1","rules":[{"id":1,"rule":"...","active":1,"kind":0}]}
├── drivers.json  {"version":"providers.v1","nodes":[{"asset":"XAU","label":"实际利率","is_core":1,"active":1}]}
├── history.json  {"version":"providers.v1","samples":[...],"trades":[...],"decisions":[...]}
└── kline.json    {"version":"providers.v1","bars":[]}
```

`history.json` 三段对应内核的三类查询：

- `samples`：`signal_performance`（`decision_id` `asset` `is_correct` `forward_pnl` `ts`）
- `trades`：`quick_sim_trades`（`id` `decision_id` `asset` `action` `pnl_pct` `settled`）
- `decisions`：`ai_decisions` 最小列（`id` `target_asset` `event_type_code` `trade_gate_reason` `evidence_action` `evidence_confidence`）

### 实现方式：内存快照，而不是重写校验器

不重写 1,601 行内核，而是把 provider 数据灌进一个内存 SQLite（`providers/snapshot.py`），
表结构按内核**真实查询的列**建，再让旧校验器照常跑 SQL。这样：

- 零复制：33 码、修复表、7 个 checker 全部复用；
- 零回归风险：`backend/` 一行没改；
- 可替换：文件 / HTTP / 直连库 三种来源，产出应逐条一致。

### 等价性验证（实测）

同 516 条决策，`from_sqlite`（直连）vs `from_file`（provider 文件）：

```
决策数 516 | 差异条数 0 | verdict 一致 516/516 | reject 2 条不变
```

**注意数据会漂移**。库是活的，`--limit N` 的回放都是时间点快照：如果拿"旧导出"去比"现在的库"，
会看到差异。这不是 provider 不一致，而是两次运行之间数据变了。实测遇到过一次：
生产系统清理了 2 笔废弃成交样本，导致 E020 在其中 2 条决策上消失（差异 1:1 对应被删样本）。
完整排查过程见 `audit_out/equivalence.md`。

因此每份 `audit_report.v1` 都带 `source` 字段（数据源标识 + 各层数据是否具备 + 加载告警），
报告可追溯到具体数据源，而不是"某个时刻跑过一次"。

> 推论：**审计的 ground truth 不能放在会被上游清理的表里**。事故样本（3 笔 LOSS）已被系统当废弃样本删掉 2 笔，
> 原始导出存档在 `audit_out/providers-incident-3trades/`。审计库应独立落盘、只追加不删（W3）。

---

## 输入契约（decision.v1）

26 个字段，分五组。完整定义见 `contracts/decision.v1.schema.json`。

| 组 | 字段 | 缺失后果 |
|---|---|---|
| 身份/判定 | `id`（或 `decision_id`）、`suggested_action`、`target_asset` | 契约错：不出报告 |
| 推演正文 | `reasoning`、`reasoning_path`、`hard_data_evidence`、`factor_ranking` | E001a–E001f / E019 |
| 结构上下文 | `data_anchor_ts`、`event_type_code`、`sentiment_score`、`evidence_confidence`、`completeness_score`、`cross_asset*`、`backtest_sample_size` | 对应层降级为 not_applicable |
| 新闻溯源 | `news_id`、`news_text`、`news_summary`、`event_atom_ids` | 事件因果层不可用 |
| 结果回填（**可选**） | `forward_pnl`、`mfe_pct`、`mae_pct`、`is_correct`、`settled` | 只影响 `basis=outcome` 的码 |

> 最后一行是独立化的关键：审计在**进单前**就能跑，不依赖结算。

---

## 输出契约（audit_report.v1）

```json
{
  "trace_id": "dec-11483",
  "mode": "pretrade",
  "verdict": "reject",
  "grade": 0.35,
  "subject": {"asset": "WTI", "action": "BUY", "confidence": 25.84, "degraded": false},
  "findings": [{
    "code": "E020", "severity": 1, "layer": "event_causality",
    "detail": "trade 不满足样本质量 5 条硬规则: R1:trade_gate_reason非空",
    "rule_id": "reflection_checkers:event_causality",
    "remediation": "该样本不满足 5 条硬规则（见 evidence.reasons）：先修 gate/结算链…",
    "evidence_refs": {"trade_id": 258, "asset": "WTI", "pnl_pct": -1.21, "reasons": ["R1:..."]},
    "basis": "decision"
  }],
  "coverage": {"checked": 7, "not_applicable": 0, "error": 0, "total_layers": 7, "ratio": 1.0},
  "layers": [{"layer": "data", "status": "checked", "elapsed_ms": 0.1, "findings": 3}],
  "incomplete": [],
  "elapsed_ms": 3.2
}
```

三条硬约束：

1. 每条 finding 必须同时有 `remediation` 与 `evidence_refs`；缺一即进 `incomplete`，报告不合格。
2. 未执行的层必须写 `not_applicable` + 原因，**禁止静默跳过**（这正是原系统 0.09% 覆盖率的成因）。
3. `basis=outcome` 的码（E007/E016/E017/E018）在 `mode=pretrade` 下不参与 verdict。

---

## 7 层与依赖

| 层（模块） | 内核函数 | 依赖 | 语义 |
|---|---|---|---|
| `layers/data.py` | `check_data` | — | 硬证据/时间锚点/因子方向/事件关联 |
| `layers/logic.py` | `check_logic` | — | 因子方向与结论自洽 |
| `layers/rules.py` | `check_rules` | rules | 铁律引用 |
| `layers/causal.py` | `check_causal` | history | 因果链节点 |
| `layers/market.py` | `check_market` | — | 市场确认 / 反向（部分码需事后行情） |
| `layers/event.py` | `check_event_logic` + `check_historical_counterfactual` + `check_sample_integrity` | history | 事件链、历史反例、样本 5 条硬规则 |
| `layers/reasoning.py` | `check_reasoning_logic` | — | 假设/反事实/校准/历史/机理/置信度 |

依赖不满足 → `not_applicable`，不是"通过"。`CheckContext.null()` 下仍有 3 层能跑（data / logic / market），
所以**没有任何外部数据也能出报告**，只是结论范围更窄。

---

## 基线（2026-11-17 实测，516 条真实决策）

| 指标 | pretrade 模式（进单前口径） |
|---|---|
| 决策 | 516（1 条完整性不足 → insufficient） |
| finding | 268（0.52/条） |
| 缺 remediation / evidence | **0** |
| verdict | clean 298 / warn 215 / **reject 2** / insufficient 1 |
| basis | `decision` 265 / `outcome` 3（pretrade 下事后型不参与拦截） |
| 延迟 P50 / P95 / max | 0.55 / 5.66 / 329 ms |

错误码分布：E001h 185 / E001i 27 / E001g 24 / E001c 6 / E026 5 / E021 4 / E024 4 /
E007 3 / E020 3 / E001a 2 / E014 2 / E022 2 / E019 1。

两条值得单独看的事实：

1. **E001 家族仍占 89%（238/268）**，但已按落库的 `hard_data_status` 拆开：
   `missing:feed_stale` 185 / `unknown_symbol` 27 / `no_local_feed` 24 / 其余 2。
   本质还是同一个系统性缺口（生产端 `hard_data_evidence` 空或坏），只是原因可读了。
   这也解释 99.4% HOLD：没有硬证据就不该给方向。
2. **方向单 6 条里只有 2 条 reject**：`11909`（SELL WTI）、`11939`（BUY BTC），
   两者 `reasoning_path` 都是 **4/8 段**，缺 `水位博弈 / 跨资产资金 / 反共识结论 / 逻辑自检`。
   3 笔实际亏损单（`11483 / 11555 / 11816`，合计 -2.56 USDT）**现在不 reject**，只剩缺证据类 warning。

### 口径修正记录（作废的旧结论）

早前"3 条 reject = 3 笔亏损、且不依赖盈亏"这一结论**作废**，根因是内核字段优先级 bug：

```python
# 错：短 reasoning 有内容，就盖掉了完整的结构化推理链
reasoning = decision.get("reasoning") or decision.get("reasoning_path") or ""
# 对：结构化字段优先
reasoning = decision.get("reasoning_path") or decision.get("reasoning") or ""
```

`dec-11483` 的 `reasoning` 只有 74 字、`reasoning_path` 1,175 字八段齐全，
旧写法判它"因果链断裂"（E014 severity 3）→ reject；修复后 E014 命中 6/6 → 2/6，reject 3 → 2。
回归测试 `audit/tests/test_kernel_fixes.py` 锁定两个方向：八段齐全**不得**报 E014，4/8 **必须**报。

## 已知边界（必须一起交付的诚实条款）

1. **E007/E016/E017/E018 依赖事后行情**，pretrade 模式下被排除。别把 full 模式的 reject 说成"进单前拦截"。
2. **E001a 是管道问题**：`hard_data_evidence` 的生产端（ai_worker 落库）不修，这条永远是 90%+ 噪音。
3. **n=2 不构成统计证据**。2 条 reject 只证明"结构性缺口能被抓住"，不是"命中率 100%"，
   更不能反推盈亏胜率。
4. **E018 曾是死码，已修**：内核 SQL 用 `ad.asset`，而 `ai_decisions` 只有 `target_asset` →
   `no such column: ad.asset` 被 `except OperationalError: return out` 静默吞掉。
   已改为 `ad.target_asset`，并让 SQL 失败**只提示一次**而不是永久静默。
   修完实测：SQL 能查到样本（3 条同类，1 胜 2 负），但 `win_rate=33.3% > 33%` 阈值，
   所以**当前数据下仍不触发**——是"活了但没响"，不是"还坏着"。
5. **数据会漂移，回放要标明时间点**。库是活的（engine 在写、还会清理数据），
   任何 baseline 都只对"当时那份数据"成立；报告里的 `source` 字段用于追溯。
6. **kline provider 是空接口**：内核当前不读 K 线，别声称"审计用到了行情上下文"。
7. **`check_sample_integrity` 依赖交易语义**：没有 `quick_sim_trades` 的客户，event 层会因缺 history 数据
   整层 not_applicable，而不是只跳过这一条。要彻底"去交易语义"，需要 W3 把该 checker 拆成可选子层。

---

## 与主仓库的关系

- **不改 `backend/`**：不做文件搬迁、不留 shim。`legacy.py` 通过 `sys.path` 引入内核，
  并在 `sys.modules` 里注入空壳 `db`，所以审计层运行期间原系统的 engine / api 完全不受影响。
- **重复代码为零**：33 码、修复表、7 个 checker 全部复用，改内核即改审计。
- **W2 已完成**：provider 四种实现 + 文件格式 + 导出命令 + 等价性验证（516/516）。
- **W3 已完成（shadow）**：审计接进决策落库路径。**注意口径（v483 修订）**：
  「100%（516/516）」说的是**离线回放**这 516 条历史决策全部可审，**不等于生产线上
  每条决策都被审过**。实测 390 条生产决策里 `audit_gate_log` 只有 50 条，原因有三：
  ① 技术直出（`[技术信号直出]`）与降级决策按设计不审，且不留痕 → 分母失真；
  ② drain / 技术直出两条落库路径此前根本没调门禁；
  ③ 门禁读决策用 `dict(row)`，调用方没设 `row_factory` 时直接 TypeError → 静默 skip。
  v483 已修三条路径 + 兼容 tuple 行 + 跳过时写 `audit_gate_log(verdict='skipped',
  mode='tech_direct'|'degraded')`。**覆盖率的正确口径是
  `审过 / (审过 + skipped)`，目标不是 100%，而是"没审的每一条都有登记、有理由"**。
  单条 ~3ms，判定与离线回放一致。详见 `W3-同步门禁.md`。
- **下一步**：把 `check_sample_integrity` 拆成可选子层（去交易语义）；再评估是否开 enforce。
