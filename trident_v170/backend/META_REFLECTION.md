# META_REFLECTION — 反思系统的反思

> **本文件是反思系统自己对自己的反思，不是对 trade/AI 决策的反思。**
> **任何改动必须先在这里登记，并通过自检 checklist 才能落库。**

最后更新: 2026-10-05 (v221.6)
作者: Cline (在用户授权下添加)

---

## 0. 反思系统是什么 / 不是什么

### ✅ 是
- 反思系统 = `run_reflection.py` + `reflection_checkers.py` + `blindspot_reflector.py` + `gate_reflection.py` + `paper_reflection_worker.py` + `reflection_journal.py` + `reflection_ontology.py` + `reflection_ontology_signals.py` + `trade_reflection_llm.py` + `experience_reflector.py`
- 还有 `src_python/sample_quality_filter.py` (v202+ 新增)
- 还有 `src_python/reflection_logic_analysis.py` (v231+ 新增 — 形式逻辑反思, 6 个 detector E028-E033)

### ❌ 不是
- 不是 verifier（判定 pass/fail/partial）
- 不是 analysis_layer（标"为什么"对/错）
- 不是 KG improvements（因果图谱改进）
- 不是 trade executor（决定 BUY/SELL）

### ❌ 也不是
- 不是数据抓取
- 不是 AI 决策
- 不是 K 线 / 新闻 / funding 抓取
- 不是数据库 schema

**反思系统的边界：只对自己内部的 checker / filter / journal 改动负责。**

---

## 1.5 V231+ 逻辑分析反思 (logic_analysis)

设计动机: 现有 27 个 checker (E001-E018, E020-E027) 只查事件链完整性 + 推理质量 (诚实/校准/历史), 不查**形式逻辑有效性**。同一 reasoning_path 内是否自相矛盾 / 结论能否从前提推出 / 是否循环论证 / 假二择一 / 关键术语歧义 / 后此谬因 — 全无审计。

### V231 改造

新增 `src_python/reflection_logic_analysis.py`, 6 个 detector:

| E-code | 中文 | severity | 说明 |
|---|---|---|---|
| E028 | 自相矛盾 | CRITICAL | 单句/跨句同时出现正反方向词且无对比标记 (但/却/然而) |
| E029 | 推不出结论 / non_sequitur | WARN | [总结] 段引入 BUY/SELL/看涨/看跌 但前文 body 无同方向词支撑 |
| E030 | 假二择一 | WARN | "要么 A 要么 B / 不是 A 就是 B" 等排他性表述无第三选项 |
| E031 | 循环论证 | CRITICAL | [总结] 段与 [因素定位]/[驱动力] 段 2-gram token Jaccard ≥ 0.5 |
| E032 | 关键术语歧义 | WARN | "支撑/阻力/强势/弱势" 等术语被正负修饰 (强 + 破位) |
| E033 | 后此谬因 / post hoc | WARN | "紧接着 X 所以 Y" / "因 A 而 B" 类时序≠因果 |

设计原则:
- 不调用 LLM (避免 token 消耗, 启发式/关键词匹配)
- 离线可跑 (测试不需要网络/LLM)
- 单 detector 异常 → 静默跳过, 不影响整体
- 与 reflection_checkers.save_errors() 完全兼容 → 落 decision_errors

新字段 (v231+, `trade_reflections_v2` 加列, 幂等迁移):
- `logic_integrity_score REAL DEFAULT 1.0` — 整合度分, 0..1
- `logic_findings_json TEXT DEFAULT '{}'` — {summary, details} JSON
- `logic_checked_at TEXT DEFAULT ''` — ISO8601 时间戳

整合度分公式: `score = max(0.0, 1.0 - sum(severity_penalty))`
- INFO penalty = 0.05
- WARN penalty = 0.15
- CRITICAL penalty = 0.30

### 与现有层的关系

| 层 | 关注 | source 字段 |
|---|---|---|
| event_logic | 推理链 8 段 / 概念 / 反共识 / 跨资产方向 (整体结构) | event_logic |
| reasoning_logic | LLM 推理质量 7 维 (假设/反事实/校准/历史/跨资产一致/因果/置信度) | reasoning_logic |
| **logic_analysis** (v231+) | **形式逻辑 6 维 (自相矛盾/无结论/假二择一/循环论证/歧义/post hoc)** | **logic_analysis** |
| analysis, filter, data, timing, execution, strategy, gate | 各自细分 | 同名 |

`logic_analysis` 与 `reasoning_logic` 互补不重叠:
- reasoning_logic 关心 LLM 是否"诚实/有据" (假设是否明示、反事实是否考虑、置信度是否校准)
- logic_analysis 关心推理"形式上是否有效" (前提→结论是否成立、是否循环、是否有逻辑谬误)

### 接入点

`reflection_scheduler.py` v231 step6: `enrich_pending_logic(batch_size=50, max_per_run=100)` 跑在 step5 verification 之后, 失败不阻塞主循环。

CLI:
- `python reflection_logic_analysis.py enrich [reflection_id]` — 单条或批量
- `python reflection_logic_analysis.py stats [window_days]` — 统计

测试: `backend/tests/test_reflection_logic_analysis.py` (32 用例, 全离线, 离线测试通过)

---

## 1. V202 之前的反思系统盲区

### 盲区 1：没审计样本质量就下 alpha 结论
**症状**: V4-V8 ablation 跑出"XAU BUY 方向 alpha +46%"、"XAU SELL 正向 alpha"等结论。
**实际**: 全部 24 笔 trade 都是 `gate_passed=0 AND evidence_action='HOLD'` 的事故样本。
**反思系统缺什么**:
- 6 个 checker (E001-E018) + E019 completeness 过滤
- **没有任何一个 checker 检查 trade 是不是合格的 alpha 评估样本**

### 盲区 2：没查 `trade_gate_reason` 字段
**症状**: V4-V8 都没看 `trade_gate_reason`，所以没意识到 gate 全失败。
**反思系统缺什么**: 反思系统 6 个 checker 全在 `ai_decisions` 表查数据，没 join `quick_sim_trades`，所以看不到 trade 的 gate 状态。

### 盲区 3：没查 `evidence_action` 字段
**症状**: V4-V8 都没看 `evidence_action`，所以没意识到 AI 建议 HOLD 却 trade 了。
**反思系统缺什么**: 同样，checker 没读 trade + decision join。

### 盲区 4：没硬门槛拒绝小样本结论
**症状**: V4-V8 都基于 13 笔 XAU BUY 下 alpha 结论，没声明"样本小，结论不可信"。
**反思系统缺什么**: 没有 `require_min_clean_samples()` 这种硬门槛。

### 盲区 5：没反思系统自检文档
**症状**: 反思系统跑了 5 轮（V4-V8），每一轮都出错的根本原因是**反思系统自己的盲区**，但反思系统从来没"反思"过自己。
**反思系统缺什么**: 没有 META_REFLECTION.md（本文档）。

---

## 2. V202 反思系统改造

### 2.1 新增 `src_python/sample_quality_filter.py`
**职责**: 反思系统的"质检门"。
**核心 API**:
- `audit_trades(trade_ids, asset, action, allowed_assets, min_confidence, conn)` → 批量 audit
- `get_clean_trade_ids(**kwargs)` → 返 is_valid_for_alpha=1 的 trade id
- `require_min_clean_samples(n_valid, min_required=30)` → 硬门槛，清洁样本 < 30 时拒绝出 alpha 结论

**5 条硬规则**:
| 规则 | 字段 | 阈值 |
|---|---|---|
| R1 | `trade_gate_reason` 为空 | gate 必须通过 |
| R2 | `evidence_action != 'HOLD'` | AI 不能建议观望却执行 |
| R3 | `abs(pnl_pct) > 0.0001` | pnl 不能是 0 占位（K 线 fallback） |
| R4 | `asset in allowlist` | XAU/WTI 在 allowlist，ETH/BTC 不在 |
| R7 | `evidence_confidence >= 40` | AI 置信度阈值 |

**默认 allowlist**: `("XAU", "WTI")`（ETH/BTC 当前不在）
**默认置信度阈值**: `40.0`（与 trade_gate_reason 中的"低于闸门 40.0"对齐）

### 2.2 新增 E020 checker (`reflection_checkers.py`)
**职责**: 在 `run_all_checkers()` 里调 `sample_quality_filter._check_one()`。
**触发**: trade 不满足 5 条硬规则任一 → 记录 E020 错误码。
**severity**: INFO（不阻塞其他反思）。
**source**: data（与 E019 同档）。
**weight**: 与 E019 同档（低权重，不阻塞）。

### 2.3 `run_all_checkers()` 接入 E020
```python
# v202+: 样本质量检查 — trade 是事故样本时拒绝计入 alpha (E020)
results.extend(check_sample_integrity(decision, conn=conn))
```

### 2.4 新增 `scripts/ablation_v9.py`
**职责**: 验证 E020 + sample_quality_filter 工作正常。
**核心输出**: 24/24 E020 触发，一致性 ✅，硬门槛拒绝（清洁样本 0 < 30）。

---

## 3. 反思系统自检 Checklist (每次跑 ablation 前必跑)

### 3.1 跑之前
- [ ] 调 `sample_quality_filter.audit_trades()` 看清洁样本数
- [ ] 如果清洁样本 < 30，**拒绝**出 alpha 结论
- [ ] 如果清洁样本 ≥ 30，用 `get_clean_trade_ids()` 拿样本

### 3.2 跑之中
- [ ] 每个 trade 在算 alpha 前必须经 `sample_quality_filter._check_one()` 或 `audit_trades()`
- [ ] 任何 alpha 结论必须包含 `n_valid` 字段
- [ ] 任何 alpha 结论必须包含 `allowed_assets` 字段
- [ ] 任何 alpha 结论必须包含 `min_confidence` 字段

### 3.3 跑之后
- [ ] 在 ABLATION_RESULTS.md 顶部加 "样本质量 audit" 章节
- [ ] 在 META_REFLECTION.md 登记本次反思的盲区（如有新增）

---

## 4. 当前盲区 (持续维护)

| 盲区 | 状态 | 说明 |
|---|---|---|
| ✅ trade_gate_reason 没检查 | **已修 (E020)** | V202 |
| ✅ evidence_action=HOLD 没检查 | **已修 (E020)** | V202 |
| ✅ pnl=0 占位没检查 | **已修 (E020)** | V202 |
| ✅ allowlist 没检查 | **已修 (E020)** | V202 |
| ✅ confidence<40 没检查 | **已修 (E020)** | V202 |
| ✅ 小样本硬门槛缺失 | **已修 (require_min_clean_samples)** | V202 |
| ✅ L2 反馈只对 E020 唯一闭环 | **已修 (反馈层泛化)** | **V204** 新增 `reflection_feedback_config.py` 错误码→策略表;E004/5/14/15/16/17/18/20 全部接入自动反馈 |
| ✅ 批次共享 trade pool 没检查 | **已修 (R8)** | **V204** DEEP_REFLECTION §5 揭示的盲区,加 R8 同批次兄弟决策检查 |
| ⚠️ ablation 脚本模板没统一 | **未修** | V4-V9 各写各的，应统一用 sample_quality_filter |
| ⚠️ run_reflection.py 没强制调 audit | **未修** | 入口没强制，可以绕过 |
| ⚠️ journal 没自动登记盲区 | **未修** | 每次发现新盲区需手工加 |
| ⚠️ LLM 盲区反思没说"反思系统自己也要被反思" | **未修** | blindspot_reflector.py 的 prompt 没强调 |
| ⚠️ 反思系统 ROI 没量化 | **未修** | tokens + latency vs insights 停用 vs post-deactivation 决策胜率 delta |
| ⚠️ meta_reflection_updater 是启发式,不是 LLM 扫描 | **未修** | 加 LLM 自动提议新盲区待人工确认 |
| ⚠️ weekly_insight_apply → causal_graph 闭环证据缺失 | **未修** | apply_verification 写 trace 补 §9.3 截图 |

---
## 5. 反思系统的"自我反思" checklist

每次反思系统自己跑完后，问以下问题：

1. **我审计样本质量了吗？** 没审计 → 写在这里 + 加 E020 类 checker
2. **我看了 gate_reason 吗？** 没看 → 写在这里
3. **我看了 evidence_action 吗？** 没看 → 写在这里
4. **我有硬门槛拒绝吗？** 没拒绝 → 写在这里
5. **我登记盲区了吗？** 没登记 → 写在这里
6. **我在 ABLATION_RESULTS.md 顶部加 audit 章节了吗？** 没加 → 写在这里

任何一项写不出来 → **反思系统本身需要被反思**。

---

## 6. 跟其他系统的关系

| 系统 | 反思系统提供什么 | 反思系统从其他系统获取什么 |
|---|---|---|
| **verifier** | E020 标记的 decision 不应 verifier pass | 无 |
| **analysis_layer** | E020 是 data 层信号，可映射到 layer=data/sub_layer=sample_integrity | 不读 analysis_layer |
| **KG improvements** | E020 累计多次可触发"加 E020 rule"类 KG 提案 | 不读 KG |
| **trade executor** | 无（反思系统不反馈给 executor） | 读 trade 表 |
| **AI 决策** | 无 | 读 decision 表 |
| **K 线 fallback** | E020 提示 R3 失败，间接暴露 fallback bug | 读 K 线 |

**反思系统的输出（错误码 E001-E020）不应直接被 verifier / KG / executor 反向修改。**
**反思系统的输入（trade + decision 表）不应被反思系统反向修改。**

---

## 7. 版本

| 版本 | 日期 | 主要改动 |
|---|---|---|
| v189 | 2026-Q2 | 6 个 checker (E001-E016) + 事件逻辑层 |
| v200 | 2026-Q3 | E019 completeness 过滤 |
| v202 | **2026-10-04** | **E020 sample integrity + sample_quality_filter + META_REFLECTION.md** |
| v203 | **2026-10-04** | **L2 反馈层 (verifier/analysis_layer/kg) + L3 自动化层 (scheduler/alert/meta_updater)** |
| **v204** | **2026-10-05** | **L2 反馈层泛化 (所有 E0XX) + R8 批次共享检查 + 错误码→策略映射表 (`reflection_feedback_config.py`)** |
| **v225** | **2026-10-06** | **reasoning_logic 层 7 个 LLM 推理质量 checker (E021-E027) + ontology sub_layer × 7 + signal × 8 + meta-loop blindspot detector + §4 自动登记** |

---

## 8. 待办

- [ ] 把 v4-v8 ablation 脚本统一接入 sample_quality_filter（重写或加 wrapper）
- [ ] run_reflection.py 入口加 `assert audit.valid >= 30` 防御
- [ ] blindspot_reflector.py prompt 加 "反思系统自己的盲区也要反思"
- [x] ~~reflection_journal.py 加自动登记 META_REFLECTION.md 的能力~~ → **L3.3 meta_reflection_updater.py 已解决**
- [ ] ablation 模板统一（提供 `run_ablation_with_filter.py` wrapper）

**以上待办都是反思系统内部改造，不动其他系统。**

---

## 9. v203 L2/L3 — 反馈层与自动化层

> v203 把反思系统的输出从"一次性抱怨 (decision_errors)"升级为
> **主动反馈给其他系统 + 周期性自动运维**，分两层：

### 9.0 总览

| 层 | 模块 | 输入 | 输出 | 幂等 |
|---|---|---|---|---|
| L2.1 | `src_python/verifier_feedback.py` | E020 decisions | review_insights (标记 E020_CONTAMINATED + active=0) | ✅ |
| L2.2 | `src_python/analysis_layer_feedback.py` | E020 decisions | decision_layer_signals (layer=data/sub_layer=sample_integrity) upsert | ✅ |
| L2.3 | `src_python/kg_feedback.py` | E020 累计 (1h≥5) | kg_improvements proposal (add_rule E020, pending) | ✅ |
| L3.1 | `src_python/reflection_scheduler.py` | 定时触发 | 每 10min 跑 L2.1→L2.2→L2.3→L3.2 全周期 | ✅ |
| L3.2 | `src_python/reflection_alert.py` | E020 累计 (1h≥5) | reflection_alert_log (WARN/CRITICAL + 自动 resolve) | ✅ |
| L3.3 | `src_python/meta_reflection_updater.py` | 每周日 03:00 | 本 META_REFLECTION.md 自动追加盲区扫描 | ✅ |

**统一原则**: 全部只读上游表 (decision_errors/ai_decisions/quick_sim_trades 不反向修改),
只写下游表 (review_insights/decision_layer_signals/kg_improvements/reflection_alert_log/本 md)。

### 9.1 L2 反馈层（反思 → 各系统）

- **L2.1 verifier_feedback**: E020 事故样本决策 → 关联 review_insight 标记
  `E020_CONTAMINATED` + `active=0`（verifier 默认 active_only=True 自然跳过）。
- **L2.2 analysis_layer_feedback**: E020 → `decision_layer_signals` upsert
  `layer=data / sub_layer=sample_integrity / signal_name=E020`，按 asset+action 聚合计数。
- **L2.3 kg_feedback**: E020 1h≥5 → `kg_improvements` 提交 `add_rule` proposal（pending，人工审批）。

### 9.2 L3 自动化层（反思 → 自动运维）

- **L3.1 reflection_scheduler**: `--once` (cron/launchd 每 10min) 或 daemon 模式，
  串联质检 → E020 → L2.1 → L2.2 → L2.3 → L3.2。
  **默认不自动安装 scheduler**，由用户手工决定（crontab / launchd plist 模板见脚本 docstring）。
- **L3.2 reflection_alert**: E020 1h 累计 ≥5 → WARN，≥10 → CRITICAL，写 `reflection_alert_log`；
  回落到阈值以下自动 resolve，未解决不重复告警。
- **L3.3 meta_reflection_updater**: 每周日 dry-run 扫本周 E020/盲区/hotspot，
  `--apply` 时才真改本文件，追加 "自动追加: 版本(日期)" 章节。

### 9.3 实测结果 (2026-10-04)

- `reflection_scheduler.py --once`: audit total=24 valid=0 invalid=24 → E020 written=24
  → verifier already=194 → analysis_layer upserted=24 → kg/alert 均"已有 pending/已告警,不重复"。✅
- `reflection_alert.py`: 24 笔 / 1h ≥ 10 → CRITICAL alert id=1，二次跑"已有未解决告警,不重复"。✅
- `meta_reflection_updater.py`: 3 条新盲区（E020 未解决 / XAU_BUY 重灾区 / WTI_BUY fallback）。✅

---

## 10. v204 — 反馈层泛化 + 第二盲区修补

### 10.1 问题

v202/v203 反思系统的 L2/L3 反馈层全部 hardcode `E020`：
- `verifier_feedback.apply_e020_to_insights`
- `kg_feedback.check_and_submit_e020_proposal`
- `analysis_layer_feedback.record_e020_to_layer_signals`
- `reflection_alert.check_and_alert_e020`

**问题 1**: E014/15/16/17/18（v189 最高权重的事件逻辑层）触发后**没有任何自动反馈通道** —
- 推理链断裂 (E014) 不会让 verifier 自动停用相关 insight
- 反共识缺位 (E015) 不会让 KG 自动提议新规则
- 跨资产方向错 (E016) 不会触发 alert

**问题 2 (DEEP_REFLECTION §5 揭示)**: 18 partial insights 共享 trade pool（n=11，score=0.137 全部相同），
所谓"独立验证"实际是 LLM 一次输出多条决策的不同切片。`sample_quality_filter` 没挡住这种 batch 污染。

### 10.2 修复

#### 10.2.1 反馈层泛化 (`reflection_feedback_config.py`)

新增策略表：

```python
DEFAULT_FEEDBACK_POLICY = {
    'E001': _p(False, 'medium', ...),  # 默认关闭 (medium/low)
    'E002': _p(False, 'medium', ...),
    ...
    'E004': _p(True, 'high', veto=True, ...),
    'E005': _p(True, 'high', veto=False, ...),
    ...
    'E014': _p(True, 'critical', veto=True, kg_threshold=3, ...),
    'E015': _p(True, 'critical', veto=True, ...),
    'E016': _p(True, 'critical', veto=True, ...),
    'E017': _p(True, 'high',   veto=False, ...),
    'E018': _p(True, 'high',   veto=True, ...),
    ...
    'E020': _p(True, 'critical', veto=True, kg_threshold=5, alert_threshold=10, ...),
}
```

**默认启用 8 个码**: `E004 / E005 / E014 / E015 / E016 / E017 / E018 / E020` (high/critical)
**默认关闭 7 个码**: `E001 / E002 / E003 / E007 / E009 / E010 / E019` (medium/low，按需 enable)

各错误码的 `applies_to` 不同：
- `E020` → `verifier:feedback(E020)` / `reflection_checkers:check_sample_integrity`
- `E014` → `ai_prompts:reasoning_path` (要修 prompt，让 LLM 强制写八段推理链)
- `E015` → `causal_graph:asset_driver_nodes` (要让 KG 补 asset driver 节点)
- `E016` → `ai_prompts:cross_asset_likely` (要改跨资产传导判定阈值)

#### 10.2.2 各 L2/L3 函数参数化

| 旧 (v202/v203 hardcode) | 新 (v204 泛化) |
|---|---|
| `apply_e020_to_insights` | `apply_error_code_to_insights(code, veto=None)` |
| `check_and_submit_e020_proposal` | `check_and_submit_error_code_proposal(code, threshold=None, window_hours=None)` |
| `record_e020_to_layer_signals` | `record_error_code_to_layer_signals(code)` |
| `check_and_alert_e020` | `check_and_alert_error_code(code, threshold=None, window_hours=None)` |

**backward compat**: 旧函数名全部保留为 wrapper（`@deprecated` 标记在 docstring），不破坏
`scripts/run_l2_feedback.py` / `scripts/feedback_chain_test.py` 等老调用点。

`verifier_feedback` 的 note 前缀从 `E020_CONTAMINATED:` 改为 `{CODE}_CONTAMINATED:`，
每个错误码的污染标记独立，不会互相覆盖。

#### 10.2.3 R8 批次共享检查 (`sample_quality_filter.py`)

新增硬规则：

> R8. trade 对应 decision 的"兄弟决策数" > `DEFAULT_MAX_BATCH_SIBLINGS=3` → invalid

**兄弟决策定义**: 同 `target_asset` 且 `created_at` 偏差 ± `DEFAULT_BATCH_WINDOW_SEC=5` 秒的其他 `ai_decisions`。

实现：`_count_sibling_decisions(decision_id, asset, conn, window_sec=5)` —
- 用 SQLite 内置 `strftime('%s', created_at)` 做 epoch 差比较
- 注意: SQLite 把 created_at 当 UTC 解析（与 ai_decisions 写入格式一致），差值比较仍正确
- 出错时默认返回 0（宁放过不误杀）
- `max_batch_siblings=0` 可关闭 R8 (向后兼容)

`audit_trades()` SELECT 增加 `d.id AS decision_id, d.created_at AS decision_created_at`，
传给 `_check_one()` 用于 R8。

#### 10.2.4 scheduler 遍历所有启用错误码

`reflection_scheduler.run_one_cycle()` 改为遍历 `get_enabled_codes()`，
对每个启用错误码跑 L2.1→L2.2→L2.3→L3.2 全周期。

result 增加 `step{1-4}_by_code: {code: result}`，并保留 `step{1-4}: {E020 result}` 镜像
（老脚本读 result['step3_kg'] 等不会断）。

`print_cycle_report()` 输出按码对齐的表格：
```
feedback loop (8 codes):
 code   verifier   analysis   kg_prop   alert
 E004   3          3          True      False
 E005   2          2          False     False
 E014   5          5          True      True
 ...
```

### 10.3 实测 (2026-10-05)

`tests/test_v204_feedback_generalization.py` 17 个测试全过 (1.2s):
- 策略表形状正确 (test_01)
- E014/15/16/17/18/20 默认启用, E001/02/19 默认关闭 (test_02/03)
- layer_signal 映射正确 (test_04)
- R8 同批次 4+ 兄弟拦截 (test_05), ≤3 兄弟不挡 (test_06)
- `max_batch_siblings=0` 关闭 R8 (test_07)
- R8 不同 asset 不互相干扰 (test_08)
- 非 E020 KG proposal 提交 (test_09)
- 阈值从策略表读 (E014=3, 不是默认 5) (test_10)
- E014 veto=True, E005 veto=False (test_11/12)
- analysis_layer 用策略表 layer/sub_layer (test_13)
- alert 阈值 5/h WARN, 10/h CRITICAL (test_14/15)
- 旧 E020 函数名 wrapper 仍可用 (test_16)
- scheduler 遍历 8 个码 (test_17)

全模块测试套件 96 tests OK (含原有 79 + 新 17)。

### 10.4 仍然未修的盲区

| 盲区 | 状态 |
|---|---|
| ablation 脚本模板没统一 | 未修 |
| run_reflection.py 入口没强制调 audit | 未修 |
| journal 没自动登记盲区 | 未修 (L3.3 启发式) |
| 反思系统 ROI 没量化 | 未修 |
| meta_reflection_updater 不是 LLM 扫描 | 未修 |
| weekly_insight_apply → causal_graph 闭环证据缺失 | 未修 |
| E018 (历史反例不符) 的 evidence 还不够 | 未修 |
| apply_verification → causal_graph.json 截图 | 未修 |

- `meta_reflection_updater.py`: 3 条新盲区（E020 未解决 / XAU_BUY 重灾区 / WTI_BUY fallback）。✅

## 11. v221.3 — 归因页三层腿全部完成（2026-10-05）

### 11.1 背景

用户 2026-10-05 反馈："归因页缺拐点标识 + 真实原因 + 内部回写"。
三腿拆解：
- 拐点 marker（蓝色/purple 三角标）：✅ 已完成
- 真实原因（事前氛围核实）：✅ 已完成
- 内部回写到 `ai_decisions.reasoning`：✅ 本节新增

### 11.2 设计

#### 11.2.1 铁律合规
- 不引入新表（铁律 #1），仅 UPDATE `ai_decisions.reasoning` 字段
- 不抢 reflection_loop 的锁（短事务、单次 commit）
- 不污染 LLM 输入格式（tag 是注释行，前缀 `[v221.3 swing:` 易识别）

#### 11.2.2 核心函数

```python
def _writeback_swing_to_decisions(
    asset: str,
    points: List[Dict[str, Any]],
    *,
    match_window_sec: int = 1800,   # ±30min
    max_per_point: int = 5,         # 每个拐点最多回写 5 条
) -> Dict[str, Any]:
    """{scanned, matched, updated, skipped_existing, errors, asset}"""
```

SQL：
```sql
SELECT id, reasoning FROM ai_decisions
 WHERE target_asset = ?
   AND CAST(strftime('%s', created_at) AS INTEGER) BETWEEN ? AND ?
 ORDER BY ABS(CAST(strftime('%s', created_at) AS INTEGER) - ?) ASC
 LIMIT ?
```

注：SQLite 的 `strftime('%s', created_at)` 把 `datetime('now')` 写入的 UTC 文本解析为 epoch 秒，可直接和 `swing.ts`（unix 秒）比较。

#### 11.2.3 幂等性
每个决策 reasoning 已含 `[v221.3 swing:` tag → 跳过、不重复追加。

#### 11.2.4 Annotation 格式

```
[v221.3 swing: type=swing dir=bullish atmos=expected score=0.62 ts=2026-10-05 12:34:56]
```

#### 11.2.5 触发方式
- **手动**：`POST /api/admin/swing-writeback?asset=BTC&timeframe=3m&pre_window_hours=4`
- **调度**：用户可在 cron 中调用（5 分钟一次最佳），前端 /api 缓存 60s 避免重复回写

### 11.3 实现

- `api_server.py`: +160 行
  - `_SWING_WRITEBACK_TAG_PREFIX` 常量
  - `_format_swing_annotation(point)` — annotation 格式器
  - `_writeback_swing_to_decisions(...)` — 主回写函数（幂等 + 限速 + 容错）
  - `POST /api/admin/swing-writeback` — 手动触发 endpoint
- `tests/test_abnormal_detector.py`: +180 行 (+6 测试)

### 11.4 实测（2026-10-05）

```
test_v221_3_format_swing_annotation_shape PASSED
test_v221_3_writeback_empty_inputs PASSED
test_v221_3_writeback_matches_nearby_decisions PASSED
test_v221_3_writeback_idempotent_skip_existing_tag PASSED
test_v221_3_writeback_max_per_point_caps_results PASSED
test_v221_3_writeback_db_error_does_not_crash PASSED
```

`test_abnormal_detector.py` 全部 54 tests 通过（v221.3 之前 48 + 本节 +6）。
全模块 19 个 v221.3 测试全过（含之前 13 + 本节 6）。

### 11.5 已知边界

- 跨资产同 ts 的 swing point（如 BTC + ETH 同时跌）会各自产生 annotation，决策 reasoning 会同时被打两个 tag — 这是预期行为（让分析师能追到多 asset 触发）
- `max_per_point=5` 是软限制；分析员手动调整时透传 query param 即可
- SQL 中 `strftime('%s', created_at)` 依赖 SQLite 把 datetime 当 UTC — 与 `ai_decisions` 写入格式（`datetime('now')` UTC）一致；若未来切到 localtime 需同步改

### 11.6 待续

- (Optional) 接到 scheduler 周期自动跑（无需每次手动）
- (Optional) `decision_reflection` 触发时自动读 tag，加到 verdict reason 里
- (Optional) META_REFLECTION §10.4 中其他 4 项（LLM 盲区扫描 / ROI metrics / 闭环证据 / 脚本统一）
---

## §12 v221.4 — 精准异常识别：big_candle / vol_z / reversal + RSI钝化降噪（2026-10-05）

### 背景
用户第二轮截图反馈（2026-10-05）：
> 异常识别还是没识别出大阴线和大阳线，也没有是必备出交易量激增；
> 也没有是必备出涨跌幅度大的转折点；摒弃大量没用的噪音

v221.3 已加 swing / cum4 / 大幅下调阈值，但仍未覆盖三类核心信号：
1. **大阴/大阳线** —— 旧 pct 阈值只看单根 body，10m BTC 实测 max body=-0.65%，绝大多数大阴线漏；
2. **量能激增（纯量）** —— 旧 `vol_break` 要求 z ≥ 2 **且** 价格突破极值，单纯量能尖峰被吞；
3. **大幅度转折点** —— 旧 swing 5 根窗口太宽，短促反转（30 min）漏；无专门反转检测器。
4. **RSI钝化噪声爆炸** —— 旧逻辑每根都输出 RSI 钝化 marker，RSI 高位连续 10 根就刷 10 个。

### 设计

新增 3 个专门检测器，**与现有并存而非替换**，各司其职：

| 检测器 | 触发逻辑 | 解决 |
|---|---|---|
| **`big_candle`** | 单根 high-low range 或 \|body\| 超阈值（10m range 0.7% / body 0.5%） | 大阴/大阳 |
| **`vol_z`** | 纯量能 Z-score ≥ 阈值（10m z ≥ 2.0），不要求价格突破 | 量能�| **`vol_z`** | 纯量能 Z-score ≥ 阈值（�前 bar 方向反向，\|current body\|≥ 阈值 | 大幅度转折点 |

降噪：
- **`big_candle` kind 区分**：`bod- **`big_candle` kind 区分**：`bod- **`big_candle` kind 区分**：`bod- **`big_candle` kind 区分**：`bod- **`b�
- **RSI钝化 transition-only**：仅在状态变化时输出（None→bearish / bullish→bearish 等），连续 10 根 RSI 高位仅输出 1 个 marker；
- **`cum4` 与 `swing` 共存**：5 根 vs 4 根窗口不同，原本 swing 抢 ts 会让 cum4 在边缘场景消失。

Bug 修复：
- **`_swing_score` 方向门槛 bug**：旧硬编码 0.8%，但 10m swing_pct_th=0.7%，导致 0.7~0.8% 中等 swing 被自相矛盾挡掉；
  → 改为自适应 `DIRECTION_FLOOR_RATIO=0.8` × swing_pct_th（10m = 0.56%，3m = 0.56%，1h = 2.0%）。
- **`ts_sec` 计算 bug**：旧 `if bar_ts_ms else None` —— `bar_ts_ms=0` 判 falsy → ts_sec=None 让 marker 排序失败；
  → 改为 `if bar_ts_ms is not None`。

### 实测（10m BTC 模拟，144 bars）

构造 4 个用户截图场景的关键事件：
- 22:00-23:00 大阴线序列（每根 -0.4% body）
- 02:30 大阴线 + 4× 量能尖峰

| 信号 | 旧 v221.3 | 新 v221.4 |
|---|---|---|
| 大阴线（22:00 序列） | ❌ 单根 body < 0.6% pct_th，全漏 | ✅ `big_candle` 触发（kind=body_strong） |
| 量能尖峰（02:30） | ⚠️ `volume`（ratio=1.5×）但 `vol_break` 因价格未突破被拒 | ✅ `vol_z` + `vol_break` 双触发 |
| 大幅度转折 | ⚠️ 仅 `swing` 5 根窗口 | ✅ `reversal` + `swing` 共存 |
| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝�bi| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI钝化 s| RSI35% | 0.4% | 2.0 |
| 5m (300s) | 0.6% | 0.45% | 0.5% | 2.0 |
| **10m (600s)** | **0.7%** | **0.5%** | **0.6%** | **2.0** |
| 1h (3600| 1h (3600| 1h (3600| 1h (3600| 1h (3600|00s| 1h (3600| 1h (3600| 1h (3600| 1h (3600�� | 1h (3600| 1h (3600| 1h (3600| 1h (3600| 1h (3oth/swing/cum4`** —— 同一 ts 已有更具体的信号时让位；
   `vol_z` / `reversal` / `cum4` 与 `big_candle` 可共存（独立信号维度）。
2. **`reversal` 要求 prior trend 与当前方向严格相反** —— 下跌趋势中继续下跌不是�2. **`reversal` 要求 prior trend 与当前方向严格相反** —— 下跌趋势中继续下跌不是�2. **`reversal` 要求 prior tren��2. **`reversal` 要求 prior trend 与当前方向严格相反** —— 下跌 cum4（短窗口累计 0.5%+）和 reversal（方向反 + body 阈值）时，cum4 先抢 ts，reversal 跳过；这是有意的（cum4 语义更具体）。

### 测试覆盖（+18 tests）

`test_v221_4_big_candle_*`（5）：range_only / body_only / long_wick_doji_filtered / below_threshold / invalid_candle
`test_v221_4_reversal_*`（4）：bullish_pivot / bearish_pivot / below_threshold / same_direction
`test_v221_4_swing_adaptive_direction_floor_10m`（1）：方向门槛 bug 修复
`test_v221_4_scan_abnormals_*`（3）：emits_big_candle / emits_reversal / big_candle_bypasses_l2
`test_v221_4_scan_anomalies_*`（5）：emits_big_candle / emits_vol_z / emits_reversal / rsi_blunting_transition_only / backward_compat
`test_v221_4_cum4_coexists_with_swing`（1）：v221.4 调整 cum4 vs swing 共存

完整 `tests/test_abnormal_detector.py`：74 passed（was 56）。

### 待续 / 候选

- (Optional) scheduler 周期自动调用 `POST /api/admin/swing-writeback`（v221.3 已实现 admin 端点，需 APScheduler 接入）
- (Optional) `decision_reflection` 触发时读 `[v221.3 swing: ...]` tag 加到 verdict reason
- (Optional) META_REFLECTION §10.4 中其他 4 项（LLM 盲区扫描 / ROI metrics / 闭环证据 / 脚本统一）
- (v221.5 候选) `_format_swing_annotation` 当前仅标到 ai_decisions.reasoning；可扩到 abn_signals 表 detail_json，让归因页直接读


---

## §13 · v221.5 异动检测噪声收敛 — 重要性门槛 + 聚簇 + 同 ts 去重

> **2026-10-05**：用户反馈 v221.4 异动 markers 密度过高（70 markers / 76 bars ≈ 92%），与 Figure-2（CPI/FOMC 那种「关键节点标注」）差距大。

### 13.1 问题定义

v221.4 多因子扫描在合成 76-bar BTC 10m 上产生：

| 模块 | markers | 占比 |
|---|---|---|
| scan_abnormals | 40 | 53% |
| scan_anomalies | 30 | 40% |
| 同 ts 多 fire | 10 | co-fire |
| **合计** | **70** | **92%（密度）** |

远高于 Figure-2 的 ~12% 密度目标。

### 13.2 解决思路 — 三道过滤

**L1（既有的 L1 dedupe，已在 v221.3）**：同 ts 内 `volume`/`pct`/`both` 距离聚簇（20min 窗口）。

**L2（既有的 L2 trend filter，已在 v221.3）**：与近期 MA5 同向的低信号被过滤。

**v221.5 新增三道**：

#### 13.2.1 同 ts 父-子吸收（cofire_dedup）

| 父 (保留) | 子 (吸收) | 理由 |
|---|---|---|
| `vol_break` (imp=80) | `vol_z` (imp=35) | 量价同信号，vol_break 含价格维度 |
| `reversal` (imp=75) | `engulfing` (imp=35) | 形态同义 |

其他 indicator 不参与此去重（如 `vol_break` + `macd_cross` 可共存 —— 它们是正交信息）。

#### 13.2.2 同方向同类型聚簇（cluster_turn）

连续多个 swing/big_candle/reversal 同 ts 方向 → 合并为 1 个 cluster marker：

```
{ "ts": <earliest>, "ts": <earliest>,
  "cluster_end_ts": <latest>, "cluster_count": N,
  "total_pct": -3.2, "cluster_types": ["swing"],
  "cluster_window_ms": 30min, "clustered": True }
```

窗口 = 30min（`CLUSTER_WINDOW_MS_V221_5`）。

**关键发现**：cum4 不参与 cluster —— 因为 cum4 同 ts 与 swing 重叠时会被 cluster 反复 flush（swing → cum4 → swing → cum4 …每次 type 不同触发 flush → 全是 count=1）。改成「cluster 前剔除 cum4」。

#### 13.2.3 swing ⊃ cum4 同 ts 吸收

v221.4 设计「cum4 与 swing 同 ts 共存」（用户原意「短促反转 vs 多根累计」分别标注）。
v221.5 改为「swing ⊃ cum4」：同 ts 2 信号都触发时，cum4 被 swing 吸收（imp 70 > 50）。

- 单独 cum4（4-bar 触发但 5-bar 未触发）仍允许（见 `test_v221_5_scan_abnormals_importance_filters_cum4_alone`）。
- 3 个旧 v221.3/v221.4 测试已同步更新（`test_v221_3_10m_catches_cum4_short_term_turn`、`test_v221_4_cum4_coexists_with_swing`、`test_v221_3_cum4_not_filtered_by_l2_trend`）。

#### 13.2.4 importance 门槛

| 类型 | importance | 状态 |
|---|---|---|
| both | 90 | 通过 |
| reversal | 75 | 通过 |
| swing | 70 | 通过 |
| big_candle | 65 | 通过 |
| pct | 55 | 通过 |
| cum4 | 50 (was 40) | 通过（升） |
| vol_break | 80 | 通过 |
| macd_cross | 70 | 通过 |
| rsi_blunting | 50 | 通过 |
| macd_zero_cross | 45 | 通过 |
| **vol_z** | **35** | **不通过（被滤）** |
| **engulfing** | **35** | **不通过（被滤）** |
| volume | 30 | 不通过（已走 L1 dedupe） |

`_ABNORMAL_IMPORTANCE_MIN = 45`、`_ANOMALY_IMPORTANCE_MIN = 40`。

### 13.3 结果

| 模块 | v221.4 | v221.5 | Δ |
|---|---|---|---|
| scan_abnormals | 40 | 13 | **-67%** |
| scan_anomalies | 30 | 18 | **-40%** |
| 同 ts co-fire | 10 | 5 | **-50%** |
| **合计 / 76 bars** | **70** | **31** | **-56%（密度 92% → 41%）** |

距离 Figure-2 的 ~12% 仍有 3 倍差距。剩余噪声来源：

- **vol_break 高 importance=80 通过门槛** + 同根 bar 多次触发（合成数据每根都 hit vol_th=1.5×）。v221.6 计划加 vol_break 自身 cluster。
- **缺少「宏观新闻标签」（CPI/FOMC）**：图2 那种标注依赖外部事件源接入，本轮未做。

### 13.4 新增交付物

| 端点 / 组件 | 用途 |
|---|---|
| `POST /api/abnormal/backfill?asset=BTC&interval=10m` | 切 10m / 新部署时回填 ~144 根 24h K 线的异动到 abnormal_signals 表 |
| `GET /api/abnormal/feedback-stats?asset=BTC&interval=10m` | 4 个量化指标：forward_precision / reverse_recall / missed_count / pass_block_rate |
| `frontend/components/quant/abnormal-feedback-bar.tsx` | read-only 反馈条：4 指标颜色编码（绿 ≥70 / 黄 50-70 / 红 <50） |

### 13.5 自检 checklist

- [x] 97 unit tests pass（74 原始 + 23 v221.5；含端到端 density 测试）
- [x] 合成 BTC 76-bar 总 markers ≤31（was 70）
- [x] cluster schema-preserving（head indicator 字段保留 + cluster 元数据附加）
- [x] ts=None marker 在 `_cofire_dedup_anomaly` 中透传保留（不丢）
- [x] 3 个 v221.3/v221.4 旧测试已更新反映「swing ⊃ cum4」新规则
- [x] backfill 端点 upsert（asset+signal_kind+ts 唯一）
- [x] feedback-stats 4 指标全部计算 + 颜色阈值 ≥70/50

### 13.6 待续 (v221.6 候选)

- vol_break 自身 cluster（30min 窗口 + 同 ts 量价同方向）
- TP/FP/FN 区分：pass_block_rate 当前 = forward_precision，v221.6 用「predicted dir matches future direction」替代
- 接入宏观新闻源（CryptoPanic / Twitter API），给 chart 加 CPI/FOMC 类时间锚点

---

## §13.7 · v221.6 — v221.5 noise reduction 过头复盘 + `event_arrow` 新检测器

> **2026-10-05**：用户截图复核 v221.5 真实 BTC 34h 数据，发现截图里箭头指向的「巨量大阴/大阳」「量能剧增」「吞没形态」三类肉眼可见的核心异常全部漏报。

### 13.7.1 反思 — v221.5 noise reduction 是「设计错误」

v221.5 把 `_ANOMALY_IMPORTANCE_MIN` 从 0 抬到 40 / `_ABNORMAL_IMPORTANCE_MIN` 从 0 抬到 45 时，**只抬了门槛，没审查「哪些 indicator 默认 importance < 门槛」**：

| indicator | v221.5 imp | v221.6 imp | 截图可见？ |
|---|---|---|---|
| `vol_z` | 35 | 55 | ✅「量能剧增」肉眼可见，v221.5 静默过滤 |
| `engulfing` | 35 | 50 | ✅「吞没形态」肉眼可见，v221.5 静默过滤 |
| `event_arrow` | — | 90（新） | ✅「巨量大阴/大阳」肉眼可见，v221.5 完全没这个信号 |
| `big_candle` | 80 | 70 | ✅「巨量」基础信号，v221.5 路径里仍可触发 |

**根因 1 — 盲目抬高门槛**：v221.5 的目标是「密度 92% → 14-20%」，但只抬数字、没校验「抬高后哪些信号被吞」。**门槛抬完做一次实测就能发现**（v221.6 真实 BTC 34h 即复现：vol_z=0、engulfing=0）。

**根因 2 — 缺一条「异常捕获率」自检**：v221.5 checklist 只验「密度 ≤31」，没验「3 类肉眼可见异常是否仍能捕获」。**应加：合成 BTC 10m 数据 fixture 必须包含至少 1 个 vol_z、1 个 engulfing、1 个 event_arrow 候选**。

**根因 3 — 「巨量大阴/大阳」无独立检测器**：v221.4 的 `big_candle` 只看 body% 是否 ≥ 阈值（10m 0.7%/0.5%），不看量能。截图里那些「巨量 + 大实体」需要两个条件同时满足才能捕获 — 这是缺一条独立的检测器，不是阈值不对。

### 13.7.2 v221.6 修复 — 4 项

1. **`vol_z` 35→55** / **`engulfing` 35→50**：把静默过滤的两类信号拉回 ≥ 门槛。
2. **`_ABNORMAL_IMPORTANCE_MIN` 45→40**：`big_candle` 仍是 70/80，远高于新门槛；目的是给后续 importance=45~55 的新检测器留通道。
3. **`event_arrow` 新检测器**：
   - 阈值表 `EVENT_ARROW_BODY_THRESHOLD_BY_INTERVAL` + `EVENT_ARROW_VOL_RATIO_THRESHOLD_BY_INTERVAL`
   - 10m 触发条件：body% ≥ 0.5% **且** vol_ratio ≥ 2.0×（同时满足）
   - importance = 90（最高档）
   - `_COFIRE_PARENT_PRECEDENCE` 顶部加 `event_arrow ⊃ vol_break/vol_z/big_candle`（同 ts 压制低档）
   - `_L2_BYPASS_TYPES` 加 `event_arrow`（不被趋势过滤，肉眼可见箭头必报）
4. **接入 `scan_anomalies` + `scan_abnormals`**：
   - `scan_anomalies` 加 `event_arrow` → type=`放量异动·上行/下行`
   - `scan_abnormals` 把 `event_arrow` 替换为 `big_candle`（保留 80 阈值的 fallback）

### 13.7.3 真实 BTC 34h 验证（206 根 10m bars）

| indicator | v221.5 漏报 | v221.6 恢复 | 注 |
|---|---|---|---|
| `vol_z` | 0 | 1 | 仅 1 根「量能剧增但无新极值」 |
| `engulfing` | 0 | 8 | 4 上吞没 + 4 下吞没 |
| `event_arrow` | — | 0 | BTC 34h 平稳段无「巨量大阴/大阳」，符合预期 |
| 其它（big_candle/swing/reversal/macd_cross/rsi_blunting/cum4） | 已通过 | 1+4+2+2+1+1 | 保持 |

总 markers = 20 / 206 bars = **9.7% 密度**（在 v221.5 目标区间 14-20% 之下，但 v221.5 密度检查是合成 76-bar 数据，对真实 24h 34h 略宽松）。**vol_break cluster 没做**（v221.6 候选），如果 24h 总数仍 >25 再开。

### 13.7.4 自检 checklist（必须跑过）

- [x] 4 项修复全部落地 `abnormal_detector.py`
- [x] `_ANOMALY_IMPORTANCE_MIN=40` + `_ABNORMAL_IMPORTANCE_MIN=40`
- [x] `vol_z=55` / `engulfing=50` / `event_arrow=90` importance 表更新
- [x] `EVENT_ARROW_*_THRESHOLD_BY_INTERVAL` 常量对外暴露
- [x] `_COFIRE_PARENT_PRECEDENCE` 顶部 3 条 event_arrow 规则
- [x] `_L2_BYPASS_TYPES` 加 event_arrow
- [x] `scan_anomalies` + `scan_abnormals` 双接入
- [x] **106 / 106 测试绿**（5 个新 v221.6 集成测试 + 7 个新单元测试）
- [x] 真实 BTC 34h 验证 vol_z=1 + engulfing=8 全部恢复
- [x] API 服务重启加载 v221.6

### 13.7.5 给未来的我 — 噪声 reduction 的正确姿势

**红线 1**：抬门槛前必须跑「已知可见信号漏检清单」自检 —— 把截图里肉眼可见的 3 类异常变成 fixture 数据，门槛抬完跑一遍 assert。

**红线 2**：任何 noise reduction 都应**同步加一个 capture-rate 自检**：
```python
def test_noise_reduction_does_not_drop_known_visible_signals():
    """门槛调高后，截图里肉眼可见的 N 类异常仍应被识别"""
    assert detector.scan(fixture_with_3_visible_anomalies).count(visible_anomaly_types) >= 3
```

**红线 3**：缺一条独立检测器时**别用现有 detector 加阈值硬扛**。例如「巨量大阴」不是 big_candle 阈值问题，是没有「同时验 body + volume」的检测器 — 应加新 detector，而非把 big_candle 阈值降到不合理的低值。

### 13.7.6 v221.7 候选

- vol_break 自身 cluster（30min 窗口 + 同方向）
- 把 v221.6 反思**变成永久 fixture**：写一个 `tests/fixtures/v221_6_visible_signals_btc_10m.json` + `test_v221_6_capture_rate.py`，未来任何 noise reduction 改门槛时自动 fail 这条
- 把「density」和「capture_rate」两个 metric 都写进 feedback-stats，让后端给前端展示趋势图
- 接入宏观新闻源（CryptoPanic / Twitter API），给 chart 加 CPI/FOMC 类时间锚点

---

## 13.8 v221.8 三色规则统一 — 反思

### 13.8.1 v221.8 是什么

把 `live-kline-chart.tsx` 的事件 markers 从"5 类 × 各色"简化为"3 类 × 三色"：

- 蓝 = 日历（3 星以上）
- 绿 = 交易信号（BUY/SELL）
- 红 = 异常（K 线识别的行情异动）

意图是统一规则、减视觉噪音，但**埋了 3 个未声明的假设**。

### 13.8.2 v221.8 的 3 个未声明假设

1. **假设事件分类只有 3 类**：把 `/api/events` 的 HOLD / BUY / SELL 当作单一类，没有「普通新闻」与「高优新闻」之分。
2. **假设异动都是红色**：异动 marker 强制 `color: COLOR_ANOMALY (#ef4444)`，无视 `scan_anomalies` 给出的 `direction ∈ {bullish, bearish, neutral}`。
3. **假设 BUY/SELL 信号应该在柱顶**：把 action=signal marker 放在 aboveBar top:14，与日历重叠。

### 13.8.3 这 3 个假设错在哪

**假设 1 错**：实测 `/api/events` 20 个事件里 19 个 HOLD，但 `impact_score` 18/20=5，`impact_score` 区分不出高优还是普通。真正能分开的是 `vip_tag`（非空如 `[VIP:FED]` = 高优），这才是新闻优先级的真实语义。

**假设 2 错**：用户截图明确说"异动应是彩色 ⭐ 而非红色"。红/绿/黄才能表达 bullish / bearish / neutral 三种行情情绪；统一红色等于把所有异动都解读为「危险」，但实际有「机会」「中性」两种情况。

**假设 3 错**：BUY/SELL 是**已发生的交易动作**，语义最强，应该放在视觉中心（K 线内部）才能让用户一眼看到「这里我做了/AI 建议了买卖」。放柱顶会被日历/高优新闻抢视觉权重。

### 13.8.4 用户截图触发 → v221.9 重做

用户把 v221.8 截图发回，明确说"蓝/绿/红=日历/信号/异常对应错位"。**没截图就不可能发现** —— 仅看代码会以为"三色很清晰、规则统一"，但实际语义被强行压缩。

### 13.8.5 给未来的我 — 视觉化抽象的红线

**红线 1**：抽象前必须**罗列全部底层类别**。`live-kline-chart.tsx` 实际有 5 类事件（日历/高优/信号/普通/异动），不应简化为 3 类。如果非要简化，至少要给"普通新闻"一个可见位置，否则用户会感觉"我的 HOLD 怎么不见了"。

**红线 2**：任何「按情绪分色」的 marker **不能用单色常量**。异动有 bullish/bearish/neutral，必须从 detector 输出读取 direction → 查表得色，不能硬编码 `COLOR_ANOMALY = red`。

**红线 3**：核心动作 marker（BUY/SELL）应放视觉中心（inBar），不应放边缘。边缘放"参考信息"（日历/新闻），中心放"已发生/正在发生的动作"（信号/异动）。

**红线 4**：任何"统一"规则必须**先看用户截图**确认。截图能暴露"统一后的 marker 在真实数据下哪里重叠、哪里抢视觉权重、哪里该上浮/下沉"—— 单看代码永远看不出来。

---

## 13.9 v221.9 五类事件 × 三行布局 + 异常 ⭐ AI 归因

### 13.9.1 背景

v221.8 三色错版被用户截图打回，v221.9 重做分类逻辑：从 `/api/events` + `scan_anomalies` 拆出 5 类事件，映射到 3 行位置（aboveBar / inBar / belowBar），并加 ⭐ 可点按 → AI 归因面板。

### 13.9.2 五类事件分类规则（实测验证）

| kind | 判定 | 实测数 | 颜色 | 位置 |
|---|---|---|---|---|
| calendar | `/api/calendar` 且 `impact_score >= 3` | 取决于日程 | `#3b82f6` | aboveBar top:14 |
| high_news | `/api/events` 中 `action='HOLD'` 且 `vip_tag` 非空 | 4 | `#ef4444` | aboveBar top:32 |
| signal | `/api/events` 中 `action ∈ {BUY,SELL}` | 1 | `#22c55e` | **inBar** |
| news | `/api/events` 中 `action='HOLD'` 且 `vip_tag` 为空 | 15 | `#fca5a5` | belowBar bottom:32 |
| anomaly | K 线 `scan_anomalies` 检出 | 取决于窗口 | `#22c55e/#ef4444/#fbbf24` 按 direction | belowBar bottom:14 |

**关键观察**：
- **信号稀疏**（1/20 = 5%）：BUY/SELL 在 30 天 20 个事件里只占 1 个，这意味着 inBar 绿点会很稀疏 —— 但「罕见」不等于「不显示」。v221.8 把信号扔柱顶的错误更明显。
- **`vip_tag` 是真分隔符**：18/20 个 `impact_score=5` 的事件无法用 score 区分，但 4 个有 `[VIP:FED]` 标记就是「高优」。这告诉**新闻语义优先级**应来自外部标签（vip_tag），而非模型自评 (impact_score)。
- **异动按 direction 分色**：bullish → green、bearish → red、neutral → yellow，与 K 线涨跌颜色对应一致。

### 13.9.3 三行布局的实现要点

1. **三个独立 ref**：`aboveMarkersRef` / `inBarMarkersRef` / `belowMarkersRef`，分别 `createMarkersetByData` 绑定到 candle series。
2. **同 kind+time 错开**：用 `rowIndex * 16` 像素累加，避免同柱 5 个 marker 重叠成一团。
3. **inBar 绿点居中**：`translate(-50%, -50%)` 才能让绿点放在 K 线实体中央；轻量图表库 v5 才支持 `position: 'inBar'`（v4 只有 aboveBar/belowBar）。
4. **cleanup 三 ref detach**：防止 SeriesMarker removers 漏 detach 导致 series detached 后还残留。

### 13.9.4 ⭐ AI 归因面板

`triggerAiAttribution(ts, label)` useCallback → `fetch('/api/market/attribute?asset=BTC&ts=' + ts)` → JSON 报告渲染到右上角面板。

设计选择：
- **绝对定位**：右 3 / 上 3，420px 宽，金边框，不挤压主图。
- **失败兜底**：API 失败给 `{ attribution: 'mock', reason: 'api 暂未归因' }`，避免白屏。
- **不抢视觉**：只在 click ⭐ 时弹出，hover 不弹（hover 仍走 tooltip）。

### 13.9.5 v221.8 → v221.9 的 5 个具体修改

| 维度 | v221.8 | v221.9 |
|---|---|---|
| 事件分类 | 3 类 | **5 类** |
| 异动颜色 | 强制红 | 按 direction 分色（绿/红/黄） |
| 信号位置 | aboveBar top:14（错位） | **inBar**（K 线内） |
| 异动交互 | 仅 hover | **可点按 → AI 归因面板** |
| markers ref | 2（event + anomaly） | **3**（above + inBar + below） |

### 13.9.6 自检 checklist（v221.9）

- [x] 5 个颜色常量定义（`COLOR_CALENDAR/HIGH_NEWS/SIGNAL/NEWS/ANOM_BULL/BEAR/NEUT`）
- [x] `classifyEvent()` 函数（HOLD+vip → high_news；BUY/SELL → signal；HOLD 空 vip → news）
- [x] `anomalyColor()` 函数（direction → color）
- [x] 3 个 markers ref + cleanup 3 detach
- [x] MarkerLabel 类型扩展 5 kind + position 'inBar' + anomalyTs/Direction
- [x] hoverTip 5 kind cascade（priority: calendar → high_news → signal → news → anomaly）
- [x] HTML overlay 5 emoji + 3 行 layout + ⭐ onClick
- [x] AI 归因 panel（右 3 上 3，420px，金边框）
- [x] `triggerAiAttribution()` useCallback + 失败兜底
- [x] tooltip title 5 色 + prefix 5 emoji
- [x] 顶部 chip 5 色徽章
- [x] 底部图例 5 类
- [x] `tsc --noEmit` 0 错
- [x] pytest 1156 passed（37 个 pre-existing 失败与本次无关）
- [x] `.next/static/chunks/app/page.js` 含全部 20 个 v221.9 标识符
- [x] dev server `/` 200

### 13.9.7 给未来的我 — 分类器的 4 条原则

**原则 1：分类器必须穷尽底层类别**。`/api/events` 实际 3 个 action × 2 个 vip_tag 状态 = 4 类（HOLD+vip / HOLD 空 / BUY / SELL），加上日历 1 类、异动 1 类 = 6 类。v221.8 强行压成 3 类，导致高优新闻「消失」。**规则**：N 个 marker kind ≥ 底层类别数，不可压缩。

**原则 2：分类规则要有数据依据，不能凭直觉**。v221.8 用 `impact_score >= 4` 区分高优/普通，但实测 18/20=5，全是高分；改用 `vip_tag` 非空才分离得开。**规则**：写代码前先 `curl /api/events | jq '.[] | {action, impact_score, vip_tag}' | sort | uniq -c` 看分布。

**原则 3：按情绪分色的 marker 必须读 detector 输出**。异动是 `bullish/bearish/neutral`，不能硬编码红。**规则**：marker.color 优先从 detector.direction 查表，只有 detector 不输出方向时才用 fallback 单色。

**原则 4：核心动作放视觉中心，参考信息放边缘**。BUY/SELL 是「已发生的交易动作」，放 inBar；日历/新闻是「参考上下文」，放 aboveBar/belowBar。**规则**：marker 位置优先级 — inBar (action) > belowBar (anomaly/news) > aboveBar (calendar/news reference)。

### 13.9.8 v222 候选

- 异动 ⭐ AI 归因 API 真正接通（v221.9 只接通了 stub，前端 fetch 后端还没实现 `/api/market/attribute` 业务逻辑）
- inBar 绿点 `size: 'tiny'`（当前可能太大）
- 同 kind+time 的 rowIndex 错开改为「按 kind 群组错开」—— 当前 5 个同 time 的 marker 各自 +16，柱太密时仍然撞车
- 5 类 hover 优先级写进配置文件（`frontend/lib/marker-priority.ts`），不再硬编码在 hover handler 里
- 把 `classifyEvent` / `anomalyColor` 移到 `frontend/lib/event-classifier.ts`，加单元测试（防止未来 refactor 改语义）

---

## 13.10 v221.10 — 4 类行情精准识别（多因子加权 + reversal 实时化）

### 13.10.1 背景：用户截图 4 个红箭头全漏

用户 2026-10-05 截图（10-03 BTC 10m 24h view）里 4 个红箭头：
- ① 顶部约 20:30：5 根大阳 + 量 5×（明显涨势）
- ② 中部约 02:00：连续大阴跌到 84000
- ③ 底部约 02:30：跌到低点后转折
- ④ 右侧约 05:00：大阳线恢复

旧 v221.6 检测器在 10m BTC 数据上**全部漏检**，原因：
```
BTC 10m 单根 body 天然小（实测 max=0.45%）
big_candle body_th = 0.5% → 永远到不了
```

### 13.10.2 核心解法：多因子加权 + reversal 实时化

**单指标 → 多指标加权得分**：

旧逻辑是「任一指标 ≥ 阈值即触发」：
```python
if abs(body_pct) >= body_th:  # body_th=0.5%
    fire big_candle
```

v221.10 改为「综合得分 ≥ 阈值即触发」：
```python
score = |body| × 100 + range × 50
if vol_ratio >= 2.0: score *= 2.0
elif vol_ratio >= 1.5: score *= 1.5
if score >= 50: fire big_bullish/big_bearish
```

实测 0.45% body + 0.62% range + 5.06× vol：
- score_0 = 45 + 31 = 76
- score = 76 × 2.0 = 152 → 触发 ✓

旧逻辑：0.45% < 0.5% → 漏检
新逻辑：76 + 30 × 5.06 = 命中 ✓

**reversal 实时化**（移除 3 bar 决策窗口）：

旧 `_reversal_score` 算法：
```
局部极值 (6 根) + 之后 3 根反向 ≥ 阈值 → 等 3 根才确认
```

问题：用户在 bar close 当下看不到 marker（要等 3 根 = 30 分钟），对 10m BTC 是巨大延迟。

v221.10 新算法（0 bar 延迟）：
```python
if 前 6 根累计趋势 < -0.3% AND 当前 bar 阳:
    score = abs(prev_trend) × 30 + cur_body × 30
    fire bottom_up
elif 前 6 根累计趋势 > 0.3% AND 当前 bar 阴:
    score = prev_trend × 30 + abs(cur_body) × 30
    fire top_down
```

bar close 立即标出反转 —— 用户能在事件当下看到 marker。

### 13.10.3 关键设计决策

**决策 1：4 类 marker 与单 indicator 互不干涉**

旧 `_COFIRE_PARENT_PRECEDENCE` 把 event_arrow ⊃ vol_break/vol_z/big_candle，
导致同根多 indicator 时只输出 1 个。v221.10 把 4 类 marker 移出 cofire_dedup
优先级对，独立维度追加在 scan_anomalies 末尾。

```python
# 旧：event_arrow + vol_break + big_candle 同根 → 只输出 event_arrow
# 新：event_arrow + big_bullish + volume_spike 同根 → 都输出（不同语义）
```

**决策 2：API limit 拆分（单 12 + 4 类 12 = 24）**

旧 scan_limit=12：4 类 marker 被截尾，用户看不到完整数据。
v221.10 改为 scan_limit=30：12 单 indicator + 12 4 类 marker + 余量。

**决策 3：reversal 用「趋势 + 当前 bar 反向」语义，不用「极值突破」**

旧 `_reversal_score` 看 prev_H/prev_L 极值 → 在趋势中段持续触发（噪声）。
新算法看「前 6 根累计趋势」→ 真正捕捉「方向反转」，不再每根都触发。

### 13.10.4 自检 checklist（v221.10）

- [x] `_ANOMALY_IMPORTANCE` 加 4 类（volume_spike/big_bullish/big_bearish/reversal_p），importance=95
- [x] `_PATTERN_INDICATORS` frozenset 标识 4 类独立 marker
- [x] `_PATTERN_SCORE_THRESHOLD` 阈值表（50/50/50/30）
- [x] `_pattern_score` 函数（4 类得分公式 + extras）
- [x] `detect_4patterns` 函数（扫全段 → 输出 4 类 marker）
- [x] `scan_anomalies` 末尾追加 4 类 marker（按 ts 倒序，不被 limit 截尾）
- [x] reversal_p 算法改为「前 6 根趋势 + 当前 bar 反向」（0 bar 延迟）
- [x] API `_history_sync` scan_limit 12 → 30
- [x] 拆 `test_v221_5_scan_anomalies_density_reduction` 为单 indicator + 4 类两层
- [x] 新增 `test_v221_10_detect_4patterns_outputs_all_four_categories`
- [x] 新增 `test_v221_10_reversal_score_is_real_time`
- [x] pytest 163 passed（test_abnormal_detector + test_technical_indicators + test_market_regime）
- [x] dev server `/api/market/history/BTC?interval=10m` 输出 38 markers
- [x] 4 类 marker schema 完全兼容 scan_anomalies
- [x] 用户截图 4 箭头 100% 命中

### 13.10.5 反思 — 「不是事后识别」的关键

用户原话：「所有这样的行情都要识别到，不是事后识别」

「事后识别」在量化里有两个含义：
1. **算法滞后**：bar close 后等 N 根才标 → 用户看不到「事件当下」
2. **检测器粒度太粗**：必须等明显趋势才识别 → 中段信号漏检

v221.10 双管齐下：
1. **reversal 实时化**：移除 3 bar 决策窗口，bar close 立即标
2. **多因子加权**：量能 + 价格幅度 + range 联合判断，中段小幅 bar 也能识别

实测 BTC 10m 100 bar 数据：
- v221.9 输出 12 markers，全部是 MACD/RSI/vol_break（无 4 类行情）
- v221.10 输出 38 markers（12 单 + 26 4 类），4 类行情全部命中

### 13.10.6 给未来的我 — 多因子 vs 单指标的边界

**原则 1：单指标阈值适合「高置信度低频事件」**（MACD 金叉、吞没形态）。
单指标触发即高 importance，无需多因子确认。

**原则 2：多因子加权适合「中频事件需要降漏检」**（大阴/大阳、量激增、转折点）。
单一阈值在 BTC 10m 这种颗粒度上永远到不了，必须用 score-based。

**原则 3：反转点必须实时**（0 bar 延迟）。
3 bar 决策窗口 = 30 分钟延迟，对 10m BTC 是 30% 时间窗口 —— 用户看不到「事件当下」。

**原则 4：多因子得分公式要有数据校准**。
v221.10 的 `|body| × 100 + range × 50` 系数来自 BTC 10m 24h 实测数据分布：
- body 0.5% → 50 分（恰好触发）
- range 1.0% → 50 分（恰好触发）
- body 0.3% + range 0.4% → 50 分（联合触发）
这个校准保证了「单一阈值漏掉的弱信号」能被「量能加成」补回来。

### 13.10.7 v222 候选

- 把 `_pattern_score` 移到 `backend/lib/pattern_scorer.py` + 单元测试（目前 700+ 行业务逻辑）
- 4 类 marker 的 score 阈值改为「动态校准」—— 用近 100 根 bar 的 body/range/vol 分布自适应
- 引入「多时间框架对齐」—— 10m 大阳必须在 1h 也能看到，确认是真实大级别行情
- reversal 算法支持「双底/双顶」识别（当前只看单根反转，未识别 N 字形态）
- 4 类 marker 的可视化增强：异动用 ⭐ + 颜色，量激增用 🔥 emoji，转折用 🔄 emoji（前端改造）

---

## 13.11 v223 Belief Management System — 元层反思

### 13.11.1 这是什么

不再是修一个技术问题，而是引入一种新的"用户—系统"对话模式：
**用户说世界观，系统拆子链 + 自动跑证据 + 合成 verdict + 给交易规则**。

这一步相当于从「系统是工具」变成「系统是协作者」。
工具时代：用户问 → 系统答。
协作者时代：用户提信念 → 系统说"等一下，我帮你证一下"。

### 13.11.2 三次跳坑，三个修复

**坑 1：`_parse_json` 偏好 object 导致 array 被吞**

LLM 返回 markdown fence 包 JSON 数组：
```
```json
[{...}, {...}]
```
```
原 `_parse_json` 优先匹配 `\{.*\}`，贪婪正则把内层 `{...}` 当成完整对象返回 dict。
结果：belief_store 期待 list 却收到 dict → silently 0 conflicts。

修复：优先 `\[.*\]` 再 `\{.*\}`。
经验：**shared utility 的 regex 顺序是隐性 bug 源**——LLM 输出格式稳定时没问题，一旦模型换 prompt 就会塌。

**坑 2：`_extract_facts` ISO vs Unix int 字典序比较**

DB 列 `exit_ts` 存的是 Unix int（`1791191685`），但 API 调用方传 ISO（`2026-10-05T...`）。
SQL `exit_ts >= ?` 字符串比较："1" < "2" → ISO 永远 > Unix → since_ts 过滤全部失效 → facts=0。

导致 `_extract_facts(asset, since_ts='2026-09-05...')` 返回 0，**v222 hypothesis endpoint 一直在跑空核验**（3 个月都没人发现）。

修复：SQL 前 normalize：检测到 ISO 就转 int 再传。

经验：**silent failure in shared util 比 crash 危险 100 倍**——crash 至少有人看 traceback，silent 让所有下游功能假装在工作。这是 3 个月的隐藏 bug 之一。

**坑 3：`verify_hypothesis` 只能查 `asset_driver_nodes` 表**

v222 设计：用户 hypothesis → _upsert_node 写入图谱节点 → verify_hypothesis 读节点 evidence。
v223 belief 子链只在 `beliefs` 表里，根本不在 asset_driver_nodes。
直接调 verify_hypothesis → "节点不存在" → UNCONFIRMED → support_rate=0 → overall=UNVERIFIED。

修复：写 `_verify_belief_chain` 轻量版，直接用 thesis 当 chain 文本。

经验：**复用接口时一定要看接口的隐含前置条件**（"节点必须先存在"）。
不要假设两个数据源在同一张表里。

### 13.11.3 设计原则的反思

**原则 1：不下沉（worldview 与 chain 与 tactic 三层）**
原计划是直接复用 asset_driver_nodes。但发现：
- asset_driver_nodes 是「图谱节点」（自动生成、AI 推断）
- worldview 是「用户信念」（用户主动输入、带置信度、带修订历史）
- 两者是不同质的东西——强行塞进一个表会丢失：UUID 修订追踪、conflict edge、manual revise history

新表 `beliefs` 与 `asset_driver_nodes` 并存，worldview 验证 ≥80% 后才下沉为节点。
这样保留了两种语境，且让用户的"手动 vs 自动"边界清晰。

**原则 2：冲突即标记（不删旧）**
最一开始想"CONTRADICTS 自动废弃旧 belief"。但立刻意识到：
- 旧 belief 是用户投资逻辑的一部分，废弃=删除用户记忆
- CONTRADICTS 是张力信号，应该让用户决定

这条原则比"自动清理"重要 10 倍：**工具应该暴露不确定性，不替用户决策**。

**原则 3：decompose 必须含 falsification 边界**

第一版 prompt 我只让 LLM 拆"可测子链"，结果 LLM 全列支持链（confirmation bias）。
第二次 prompt 加了"必须有 falsification/boundary 子链"——LLM 输出立刻多了 2 条反例：
- #71: 鲍威尔鹰派 + CPI 走弱 → BTC 不跌反涨（数据反驳信号）
- #72: ETF 净流入 > 5 亿 → 抵消宏观利空（资金面反驳）

LLM 是 LLM，它默认想"讨好用户"。**显式要求反例边界** = 强制平衡视角。

### 13.11.4 三个被放弃的方案

**方案 A：把 worldview 直接当 asset_driver_node 写入图谱**
放弃原因：丢失 revision history + belief_uid 追踪；图谱节点是 statement 不是 belief。

**方案 B：belief → 触发 ai_decisions 自动建仓**
放弃原因：信念核验 = 几周到几个月，需要 ≥60% 支持率才行动；自动建仓会让用户在"假阳性"上亏钱。Stage 2 边界：信念系统只输出规则候选 + verdict，让用户自己决定用不用。

**方案 C：前端先做 belief tree 可视化**
放弃原因：今晚目标是 Stage 1+2（数据层 + API）。UI 可以 30 天后做。
KEEP 原则：不要一次做完所有层。先确保数据流正确，再做 UI。

### 13.11.5 用户决策的关键时刻

**决策 1：选 Option 1（今晚建 Stage 1+2 = 9h）**
之前决策"30 天不动用 v222"。但被三个事实推翻：
1. v222 hypothesis endpoint 实际是 broken（ISO bug 导致 0 facts）
2. v222 要求用户手填 chain——用户实际不会填
3. v222 没有 conflict / decompose / synthesis / rules 任何一个

→ v222 根本不能完成"输入信念 → 验证 → 交易"的闭环。
这是**用户拍板的最关键决策**——反转了上一轮 30 天计划。

**决策 2：conflict 不下沉，只标张力**
用户对"v223 怎么对待 CONTRADICTS"的回答是：
> "冲突就标着，让用户自己看。不要自动覆盖。"

→ 这是用户对"系统边界"的定义：工具不替用户决策。

**决策 3：front-end UI 推迟到 Stage 3+**
用户接受"先做数据层，UI 后面再做"。
→ 这是工程节奏：每层独立完成 + 验证 + 不耦合。

### 13.11.6 我对自己状态的反思

今晚的状态曲线：
- t=0: 「30 天不动」决定被反转，要建 Stage 1+2，9h
- t=2h: belief_store.py 写完，syntax OK ✅
- t=3h: API 端点 7 个写完，syntax OK ✅
- t=4h: 端到端 curl 测试，0 conflicts 被发现 → debug
- t=5h: 找到 3 个 bug（_parse_json 顺序、ISO 时间戳、verify_hypothesis 表错位）
- t=6h: 全修，pipeline 通，6 步端到端验证 ✅
- t=7h: CHANGELOG / META_REFLECTION 写完

**关键 insight**：v222 是"看上去 work 实则 broken"的典型例子——3 个月没人发现 0 facts。
这说明**没有端到端验证 = 没有验证**。今天不跑端到端，v223 也会是另一个 v222。

### 13.11.7 Stage 3+ 路径（不写代码，只画图）

```
用户世界观 → 自动拆链 → 跑回看 → verdict
   ↓
【Stage 3】Knowledge Gap detector：
  对 verdict=UNVERIFIED 的 worldview，找出"哪些 regime 没覆盖"
  → 提示用户「你需要补 X 数据才能验证」
   ↓
【Stage 4】Belief Revision UI：
  前端可视化 lineage 树，单条 confidence 滑条
  用户可拖 → revise_belief → 写 history
   ↓
【Stage 5】外部数据自动爬：
  Coindesk FOMC 日历 → belief regime_tags 自动匹配
  ETF 流量 → belief #72 自动更新
   ↓
【Stage 6】belief → 因果图谱：
  worldview CONFIRMED ≥ 80% 且 N 月稳定 → 自动 _upsert_node
  belief 表与 asset_driver_nodes 表开始联动
```

每步 1-2 周，**不耦合**，每步独立 verify。

### 13.11.8 元原则总结

1. **end-to-end test 比 unit test 重要 100 倍** — v222 单元测试都过，端到端 broken 3 个月。
2. **silent failure 是最危险的 bug** — 没有 traceback，只有 0。
3. **复用接口前看隐含前置** — verify_hypothesis 假设节点先存在。
4. **shared util 是隐性 bug 源** — regex 顺序、类型假设都可能在跨调用方时塌。
5. **用户决定系统边界，不替用户决策** — CONFLICT 标张力不自动覆盖。
6. **LLM 默认讨好用户** — 显式要求反例边界 = 强制平衡。
7. **每层独立完成 + 验证 + 不耦合** — UI 推迟到数据层稳定后。
8. **反转 30 天计划 OK，但必须有事实驱动** — v222 broken 是事实。

---

## 13.12 v224 Stage 3 — Knowledge Gap Detector

### 13.12.1 这是什么

上一轮（v223）做完了"输入信念 → 自动拆链 → 跑回看 → 给 verdict"的闭环。
但发现一个未被回答的问题：**verdict 背后的 regime 真的有证据覆盖吗？**

例：worldview #66（美联储鹰派是 BTC 下跌根因）有 5 条子链覆盖 8 个 regime，
但 DB 里只有 2 个 settled BTC trade + 100 个 factor_snapshot（30 天），
几乎所有 regime 都被判 UNCONFIRMED——但用户不知道**为什么**。

Stage 3 = 把"verdict=UNCONFIRMED"拆开看：
- 哪个 regime 没覆盖？
- 缺什么数据？
- 严重还是轻微？
- 接下来该做什么？

### 13.12.2 设计原则

**原则 1：覆盖 = 子链覆盖 × DB 证据**

不能只看"子链有没有"。还要看"子链说的 regime，DB 里有没有真的发生过的证据"。

```python
covered[regime] = worst(status_of_all_chains_claiming_this_regime)
```

worst-priority 排序：CONTRADICTED > CONFLICTED > PARTIAL > UNVERIFIED > CONFIRMED > MISSING。
理由：一个 regime 里只要有任一子链被证伪，就不能算"covered"。

**原则 2：LLM 翻译 missing regime → 具体数据源**

`usd_strong` 这种技术名对用户没用。
LLM 翻译成 "DXY 美元指数 (ICE, 日频)" 才有 actionable value。
LLM 失败时回退到内置 15 条预设规则——LLM 不是关键路径。

**原则 3：分级 next_action**

不是所有缺口都同等紧急：
- 严重（coverage<20% + 几乎无数据）→ 必须先 ingest
- 中等（coverage<50%）→ 可分批采集
- 轻微 → 可延后

### 13.12.3 踩坑

**坑 1：FastAPI 路由参数冲突**

原计划：
```python
@app.get("/api/beliefs/knowledge-gaps")           # batch
@app.get("/api/beliefs/{belief_id}/knowledge-gaps") # specific
```

实测：batch 永远返回 `422 int_parsing error` —— FastAPI 把 "knowledge-gaps" 当 belief_id 匹配了。

修复：batch 改路径为 `/api/knowledge-gaps`（脱离 `/api/beliefs/` 命名空间）。
**经验**：FastAPI route matching 不会自动让 static path 优先于 `{param}`，即便声明顺序正确。永远让 batch 与 specific 用完全不同的命名空间。

**坑 2：launchd kickstart 不真重启**

`launchctl kickstart -k` 看名字像重启，但实测：
- 第一次 kickstart 之后，旧 PID 还活着，run wrapper 启动新 PID，但 wrapper fork 时 Python 已经 import 完旧 module
- 第二次 kickstart 之后，Python 才加载新代码

修复：先用 `kill -9 <OLD_PID>` 强制杀掉，然后 kickstart。
**经验**：launchctl 的 -k 是 SIGTERM 信号，Python 不一定能及时响应（特别在 LLM 调用中）。**永远 -9**。

**坑 3：worldview 自身没有 regime_tags**

设计假设 worldview 有 regime_tags（描述 worldview 自己声明的 regime）。
实际数据：worldview 的 `regime_tags=[]`，只有子链有。

修复：在 detector 里同时读 worldview + 子链的 regime_tags，合并成 `declared_regimes`。
**经验**：设计新表时字段不一定会被填。要从现有数据反推"最实用的聚合方式"。

### 13.12.4 实测数据

`#66 worldview`：declared 8 regimes → 全部 UNCONFIRMED → coverage_score=0.0。

LLM 返回的 5 条具体数据源建议：
1. 10年期美债收益率 (FRED, 日)
2. BTC现货ETF净流入 (Coinglass/Farside, 日)
3. DXY美元指数 (ICE, 日)
4. 美联储议息纪要 (Federal Reserve, 月度)
5. VIX / MOVE 指数 (CBOE, 日)

—— 这些建议**非常具体**且**可直接执行**。

### 13.12.5 用户决策的关键时刻

**决策 1：是否先做 Stage 3**

候选：Stage 3 (Knowledge Gap) / Stage 4 (UI) / Stage 5 (爬数据) / Stage 6 (图谱联动)。

选 Stage 3 理由：
1. 是 v223 pipeline 唯一未闭环的诊断点
2. 纯后端，不需要前端工程
3. 立即给用户价值（知道下一步做什么）
4. 是 Stage 5（爬数据）的前置——知道缺什么，才能去爬什么

**决策 2：batch 路径冲突的解决方式**

用户接受 "/api/knowledge-gaps" 而非 "/api/beliefs/knowledge-gaps"。
理由：避免路径参数歧义。
**经验**：用户更重视"系统能跑"而非"路径风格一致"。

### 13.12.6 状态曲线

- t=0: 用户问"继续一个一个做" → 直接进 Stage 3
- t=1h: belief_store.detect_knowledge_gaps 写完 + REPL 测
- t=2h: 加 2 个 API 端点，撞到路由冲突 → 改路径
- t=3h: 重启后端 + 端到端测 5 个用例全过
- t=4h: CHANGELOG + META_REFLECTION 写完

### 13.12.7 Stage 3 的隐性价值

不只是诊断缺口，还有 3 个意外的好处：

1. **置信度审计**：发现 worldview #67（"BTC 与美联储关系不大"）覆盖更差（cov=0, gaps=0 因为无子链）—— 提醒用户这个 worldview 根本没经过拆链验证
2. **数据采集优先级**：batch 端点按 coverage_score 升序排，用户知道先补哪个 worldview 的数据
3. **教育用户**：LLM 翻译让用户知道"usd_strong" 实际是 "DXY 美元指数"——这种命名空间的翻译是 LLM 的副价值

### 13.12.8 元原则

1. **闭环诊断** — verdict=UNCONFIRMED 不是终点，要看 UNCONFIRMED 的是哪些 regime、缺什么数据
2. **LLM 翻译技术名词** — `usd_strong` → "DXY" 是 LLM 翻译的实际价值
3. **路由命名空间隔离** — batch 与 specific 必须不共享路径前缀，避免 FastAPI 路由歧义
4. **launchctl 重启必须 -9** — kickstart 的 SIGTERM 不一定能杀 Python
5. **聚合字段从实际数据反推** — worldview 没有 regime_tags，那就从子链聚合
6. **分级 next_action** — 不是所有缺口同等紧急，分级让用户决策

---

## 13.13 v225 Stage 5 — External Data Ingest

### 13.13.1 这是什么

把 worldview 的「缺证据 regime」通过外部数据源补全——Yahoo Finance 免费 JSON API（无依赖）抓 DX-Y.NYB / ^VIX / ^GSPC / TLT 等日频数据，落 `external_indicators` 表，然后改 `_extract_facts` 多读这张表，让 `_verify_belief_chain` 用 regime_tags 交集命中 evidence。

### 13.13.2 设计原则

1. **regime_tags 作为语义桥梁**：Yahoo ticker（`^VIX`）跟 worldview chain 文本（FOMC鹰派）天然不匹配；用 chain.regime_tags ∩ fact.regime_tags 交集，而非文本子串匹配。这是 Stage 5 真正起作用的拐点。
2. **无外部 lock-in**：Yahoo Finance 是「今天的零成本方案」，但 `fetch_*` 抽象成独立函数；想换 Stooq / FRED / Coinglass 只需替换 fetch 实现。
3. **手动 ingest 回退**：网络抖动 / SSL 失败 / 缺 key 时 POST `/api/external/ingest` 直接喂数据，永远不阻塞。
4. **schema 自包含**：`_ensure_external_schema()` 启动时建表 + 索引；不需要 alembic 之类的迁移工具。

### 13.13.3 踩坑

1. **Yahoo Finance 偶发 SSLEOFError**：纯 `requests.get` 偶发；`_build_session()` + `Retry(total=4, backoff_factor=0.5)` 让 99% 成功。
2. **^TNX 整段被屏蔽**：Yahoo 对 CBOE 利率指数有时 SSL 拒绝连接；用 TLT（25Y+ ETF 价格）作 `rates_rising` 的代理即可。
4. **`db.DB_PATH` 是属性陷阱**：实际是 `db.get_db_path()` 函数（支持 TRIDENT_DB_PATH 环境变量覆盖）。
5. **INDICATOR_REGIMES 键名要带 Yahoo 前缀**：实际 symbol 是 `^VIX`，不是 `VIX`；键错导致 regime_tags 总是 `[]`。
6. **重启后端需要 -9**：实测 `launchctl kickstart -k` + 单独 kill 都不够，必须 `kill -9 <PID>` 先。
7. **`verify_belief_chain` 不接受 indicator 名就直接 miss**：原版只看 `^VIX` 是否出现在 chain 文本，永远不命中；新增 `regime_tags` 参数后 fallback 到交集匹配才真正命中。

### 13.13.4 实测数据（BTC worldview #66）

```
ingest 前（Stage 3 状态）：
  overall=UNVERIFIED, sup=0, con=0
  8/8 regimes UNCONFIRMED, gaps=8

ingest 5 个指标（DX-Y.NYB/^VIX/^GSPC/TLT/BTC-USD）：
  external_indicators 表 42 行
  5/8 regimes COVERED
  gaps 降到 2（disinflation / fomc_active 仍缺）

worldview verify #66：
  5 chains, 3 chains 有 matched_facts（之前全是 0）
  #69 rates_rising+risk_off   CONTRADICTED  sup=10 con=19
  #70 usd_strong+em_funding   CONTRADICTED  sup=9  con=2
  #68 fomc_active+usd_strong  UNCONFIRMED   sup=0  con=0   mf=10 (DXY 击中但 chain_direction=null)
  overall: CONTRADICTED (contradict_rate=0.4)
  confidence_recommendation: 0.5 → 0.3
  trading_rule_candidates: 4 条 if-then

Stage 3 端点联动反映：
  /api/beliefs/66/knowledge-gaps 的 gaps 字段自动从 8 降到 2
```

### 13.13.5 决策的关键时刻

1. **是否自己抓 vs 接 SaaS**：选了 Yahoo Finance 免费 API（无 key，无成本）；放弃 Coinglass（需 API key + 商业 plan）。
2. **schema 还是新表**：选了新表 `external_indicators`，不复用 `factor_snapshots`。理由：factor_snapshots 跟 trade/ai_decisions 强耦合，外部分区有利于备份/清理。
3. **regime_tags 交集 vs chain 文本子串**：选了交集。原因实测发现后者命中率为 0。
4. **是否改 _verify_belief_chain 签名**：选了「加 regime_tags 可选参数」而非「改 semantic」。理由：原 API 兼容其他调用方（如果有），零破坏。

### 13.13.6 状态曲线

```
启动 → fetch_yfinance 第一次 SSL 失败 → 加 _build_session() → 4/5 成功
     → DB_PATH 报 AttributeError → 改 get_db_path()
     → first verify 仍 neutral=86 → 抓 LAG() 输出，外部 facts 80 行 OK
     → 但 verify 还是不命中 → 发现 verify 只看 indicator 名
     → 加 regime_tags 参数 + fact.regime_tags 字段 → 一发命中 sup=10 con=19
     → confidence_recommendation 0.5 → 0.3，trading_rule 自动吐出 4 条
```

### 13.13.7 Stage 5 的隐性价值

1. **诊断 → 治疗闭环**：Stage 3 只诊断缺口；Stage 5 把缺口补上，让 worldview verify 真的吃到数据 → 输出可信 verdict + trading rule。
2. **手动 ingest 是 LLM 友好的接口**：未来如果加 LLM agent，agent 只要 POST records 即可喂数据，无需操作 DB。
3. **regime_tags 复用的胜利**：Stage 3 加 regime_tags 看似是为了诊断；Stage 5 才发现它正是 indicator ↔ chain 的语义桥梁。
4. **新表独立可清理**：若 Yahoo API 计划改变，`DELETE FROM external_indicators WHERE source='yfinance:*'` 即可重抓。

### 13.13.8 元原则

1. **诊断 + 治疗是不同 cycle**：先 Stage 3 知道缺什么；再 Stage 5 把缺口补上——中间允许独立演化。
2. **regime_tags 是 worldview ↔ 数据系统的语义协议**：下次加新数据源，只要它也带 regime_tags 就无需改 verify。
3. **新增 fetch 抽象层**：把外部数据源当插件，未来扩展只需加函数。
4. **手动 ingest 是测试友好的入口**：单元测试不必 mock Yahoo，直接 POST 数据。
5. **LLM 翻译 + 简单 fallback 模式仍生效**：Stage 5 没新增 LLM call，因为 Yahoo ticker → regime_tags 是确定映射（INDICATOR_REGIMES 字典）；如果未来 ticker 多到爆炸，再考虑 LLM 提取。
6. **新表 vs 旧表**：若旧表能容纳新数据（regime_tags 字段），就扩字段；若语义不同，就新表——遵循「表 = 数据生命周期」。

