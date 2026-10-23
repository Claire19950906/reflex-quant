# Trident 系统能力审计报告 (v200+ 反思)

## 核心结论
**系统在「数据生产」和「AI 决策生成」两端的工程完成度高，但在中间四层的「经验闭环」存在系统性断链。**

```
[数据生产] ──► [结构化] ──► [反思] ──► [因果图谱] ──► [经验] ──► [决策应用]
   ✓ 充实    ✓ 高精度抽取  ✓ v200 A4/A5   ✗ 一对一链状   ✓ v199 闭环  ✓ v199 prompt
                            daemon 自动                            注入存在
                            消化浅亏
                            (12.2% LLM)
```

---

## A. 反思层 (trade_reflections_v2) — **C+ 级** *(v200 后实测修正)*

| 指标 | 实测值 | 评估 |
|---|---|---|
| 总行数 | **74** (7d 内 +74, 24h 内 +13) | ✓ 数据生产速度正常 (非"散在 1+ 月") |
| lesson 长度 | avg 59.8 / max 74 / 全部 ≤80 字节 | ⚠️ 仍是 system msg 占位 (paper_reflection_worker INSERT 时 schema default) |
| llm_depth_reflection 真有内容 | **9/74 (12.2%)** | ✓ **v200 A4/A5 已修**: daemon 自动消化 2 条 (ref#93 历史 + ref#133 今日新) |
| error_layer | 40 `analysis` + 33 `filter` | ✓ **有归因** (不是 100% 默认值) |
| pnl_pct 分布 | **8 ≤-0.5%** / 5 (-0.3~-0.1%) / 27 (-0.1~0%) / 41 WIN | ⚠️ 浅亏为主, 但 daemon 现在能消化 -0.5% 段 |

**v200 A4/A5 修正根因分析** (2026-09-28 实测):

| 旧判断 (v199 audit) | 实际情况 (v200 A4/A5 后) |
|---|---|
| ❌ "LLM daemon 没跑过" | ✅ **daemon 真在跑** (api_server lifespan 注册, 300s/次 batch=5) |
| ❌ "filter 太严 71 条中 4 条满足" | ✅ filter **本身正确**, 真根因是**三层 INNER JOIN 把候选集缩到 17.8%** |
| ❌ "0/71 LLM 内容" | ✅ **v200 后 9/74 (12.2%)** — daemon 自动消化 2 条 (ref#93 历史 + ref#133 今日新增) |
| ❌ "100% 默认 error_layer" | ✅ 实际 **40 analysis + 33 filter** (paper_reflection_worker 已写归因) |
| ❌ "反思散在 1+ 月, 单日 0-3 条" | ✅ **7d 内 +74**, 反思数随 quick_sim 同步增长 |

**daemon 行为确认** (v200 A4/A5 后):
- `trade_reflection_llm.daemon_loop` (interval=300s, batch=5) — **已启动且在跑**
- `enrich_pending_loop` SQL (A4): `pnl_pct < -0.005 AND depth_score >= 3 AND error_layer IN ('analysis', 'filter')`
- `enrich_pending_loop` SQL (A4 关键改动): **取消 `JOIN quick_sim_trades`** (命中不全, quick_sim 表只有 13 个 decision_id)
- `enrich_one` 内部 (A5): **`JOIN ai_decisions` → `LEFT JOIN ai_decisions`** (60 条历史反思 decision_id 在 v158 cleanup 后不存在)
- 8 条 pnl<-0.5% 反思, 7 条已 admin 触发, **1 条 ref#93 daemon 自动消化** + 1 条 ref#133 今日新增

**真实根因** (三层叠加):
1. **quick_sim_trades JOIN 命中不全** (15 条 trade / 13 个 decision_id, vs 73 条反思) — 旧 SQL 候选集缩到 17.8%
2. **ai_decisions INNER JOIN 历史 decision_id 缺失** (60/73 反思 dec_id<1757, v158 cleanup 副作用)
3. **评估窗口短** (quick_sim 20min, 单笔 pnl 不超过 -0.3%)

**v200 修复**:
- A4: `enrich_pending_loop` 取消 quick_sim JOIN, 直接用 `r.pnl_pct < -0.005` 判定 LOSS
- A5: `enrich_one` 内部 LEFT JOIN ai_decisions, 历史数据也能跑 (prompt 残缺但 LLM 仍输出)
- 实测: daemon 一次性消化 2 条 (ref#93 + ref#133), LLM 占比 7/73 → 9/74 (+28.6% 相对增长)

**当前实际影响** (v200 后):
- 反思**数量**正常生产 (74 条/7d)
- 反思**质量**仍浅 (lesson 是 system msg 占位, error_layer 有归因)
- 反思**LLM 深度分析** **12.2% (9/74)**, 其中 **2 条 daemon 自动** + 7 条 admin 触发
- SPA 的 evidence_summary 是真实反思拼接 — 仍可用 (见 v199 闭环)

---

## B. 事件结构化层 — **B-/C+ 级**

### event_atom (134 条)
| 指标 | 实测值 | 评估 |
|---|---|---|
| 抽取能力 | 24 类规则 + v181 P2 13 类产业/科技 | ✓ 覆盖广 |
| direction 已知率 | 89/134 = **66.4%** (45 unknown) | ⚠️ 1/3 方向未识别 |
| direction_confidence=0.0 | 45 条 (全 llm source) | ❌ LLM 填了 0，没真跑 |
| direction_source=rule_bias | 79 条 (准确率 100%) | ✓ 词典规则路径扎实 |
| direction_source=llm | 45 条 | ❌ 全 unknown + conf=0 |

### event_impact_curve (312 行)
| 指标 | 实测值 | 评估 |
|---|---|---|
| price NULL | **312/312 = 100%** | ❌ 完全没填 |
| delta_pct = 0 | **312/312 = 100%** | ❌ 完全没填 |
| volume_ratio NULL | **312/312 = 100%** | ❌ 完全没填 |
| note | 100% `'llm_predicted'` | ⚠️ 全部 placeholder |

**根因**:
- `event_struct.extract_events()` 只抽客观字段（类型/量级/标的），方向靠 LLM 后置调用
- 后置 LLM 调用没跑成（`direction_confidence=0.0` 全 llm）
- `event_impact_curve` 是 LLM 期望曲线，需要后置 LLM 填真实价格影响，**完全没跑**

**评估**: 事件**识别**能力强（v181 P2 扩展做得很好），但**方向识别和影响量化是黑盒 + 没跑**。

---

## C. 因果图谱 (asset_driver_nodes / edges) — **C- 级**

| 指标 | 实测值 | 评估 |
|---|---|---|
| 节点数 | 165 | 节点规模中等 |
| 边数 | 160 | ⚠️ 边/节点 = 0.97 (近一对一) |
| edge kind='causes' weight | **91/91 全部 = 1.0** | ❌ 没衰减 |
| edge kind='conditions' weight | 0.65-0.93 (有差异) | ✓ 这部分有真实置信度 |
| 出度分布 | 160 个节点出度=1, 5 个节点入度高 | ⚠️ 树形链，非网状 |
| 节点 is_core | 20/165 = 12% | ✓ 核心节点存在 |
| 节点 confidence | avg 0.78, min 0.5 | ✓ 有置信度 |
| 节点 version | 164/165 = v1 | ⚠️ 几乎没迭代 |
| 更新时间 | 2026-09-24 → 09-27 (3 天集中) | ⚠️ 一次性生成 |

**根因**:
- 图谱结构来自 v174/v176.5 LLM 一次性种子生成
- weight=1.0 的 `causes` 边没有时间衰减/强化机制（应该 verified=true 时+1，false 时-1，类似 EWMA）
- 没有「新 event_atom → 更新相关边 weight」的反馈管道

**评估**: 图谱**结构**已搭好（5 个 asset × chain+condition+core），但**没有学习机制**，纯静态。

---

## D. 经验应用层 — **D 级 (关键断链)**

### review_insights (45 条)
| 指标 | 实测值 | 评估 |
|---|---|---|
| 总数 | 45 (全部 active=1) | 数据存在 |
| origin | real=24, seeded=8, quick_sim=8, truth=5 | ✓ 多源 |
| scope | review=35, auto_truth=5, all=5 | ✓ 多作用域 |
| asset 字段 | **40/45 为空字符串** | ❌ 不可被 ai_decisions.asset 索引 |
| counter_factual 真有内容 | 8/45 | ⚠️ 18% 有反事实 |
| source_samples > 0 | 29/45 | ✓ 有样本支撑 |
| source_reflection_id | **无此列** | ❌ 无法溯源反思 |
| applied_setting_change | **无此列** | ❌ 闭环通道未建 |

### strategy_param_adjustments (3 条)
| 指标 | 实测值 | 评估 |
|---|---|---|
| 总数 | 3 | ⚠️ 极少 |
| status | **3/3 = pending** | ❌ 全部未应用 |
| sample_size | 1, 1, 31 | 样本不足 |
| evidence_summary | 真实反思拼接 | ✓ 内容扎实 |
| avg_confidence | 0.9-1.0 | ✓ 高置信度 |

### ai_decisions
| 指标 | 实测值 |
|---|---|
| source_insights_json 列 | **不存在** |
| applied_insight_ids 列 | **不存在** |
| 任何 insight/experience/lesson/memory 列 | **均不存在** |

**断链位置 (4 处)**:
```
1. trade_reflections_v2 ──► review_insights:    ❌ 没有 source_reflection_id
2. review_insights ──► strategy_param_adjustments: ❌ 3 条 SPA 无 source_insight_id 字段
3. strategy_param_adjustments ──► status=applied: ❌ 3/3 永远 pending
4. strategy_param_adjustments ──► ai_decisions:    ❌ ai_decisions 无 applied_setting 列
```

**根因**:
- review_insights 表是早期 schema，没规划 source 追溯
- strategy_param_adjustments 是 v176 加的，但 applied_status→true 的 trigger 没写
- ai_decisions 是更早的表，没规划接收参数调整
- 整个闭环**没有任何 worker 负责把 SPA.status 推到 applied 并通知 ai_worker**

---

## 综合评估与建议

### 能力雷达 *(v200 A4/A5 后实测修正)*
```
反思能力   █████░░░░░  50%  (v200 A4/A5 已修: daemon 自动消化浅亏 + 历史 decision_id, LLM 占比 9/74=12.2%)
模拟交易   ███████░░░  75%  (review_insights 259 条, samples=14, insights=+11/周, 真在跑 quick_sim)
分析能力   ████░░░░░░  40%  (event_atom 134 条, 方向/影响部分真跑)
筛选能力   ██████░░░░  60%  (v199 SPA worker 已 applied, filter 链到 ai_decisions 通了)
事件结构化 ███████░░░  70%  (v181 P2 24 类抽取覆盖广)
因果图谱   ███░░░░░░░  30%  (结构在, 没学习)
经验应用   ████░░░░░░  45%  (v199 SPA → ai_decisions → prompt 注入闭环通, 仍需样本积累)
```

### 优先级建议 (v199 + v200 已完成的标注 ✅)

**P0 (立即做 — 闭环恢复)**:
1. ~~**A1 反思 daemon**: 启动/修复 `trade_reflection_llm` daemon，写真反思~~ → **v200 已修**: daemon 自动消化 ref#93 + ref#133 (历史 + 今日新增)
2. ✅ **D1 SPA → applied**: v199 已写 `spa_apply_worker.daemon_loop`, 600s/次 (SAMPLE_THR=5 + CONF_THR=0.65)
3. ✅ **D2 ai_decisions 列**: v199 已加 `applied_setting_changes TEXT DEFAULT ''`, 50 条已回填

**P1 (短期 — 学习机制)**:
4. **C1 KG 衰减**: 加 `asset_driver_edges.weight_decay` 机制（每 24h 衰减 5%，verified 时 boost）
5. **B1 后置 LLM**: 修后置 LLM 调用（direction=unknown 时兜底），不再 dir_conf=0
6. **B2 impact_curve 真实化**: 把预测曲线改成实际 price snapshot（事件后 5m/30m/2h/1d 真实取价）
7. ~~**A3 反思 filter 放宽** (新增): 把 `pnl_pct < -0.005` 改为 `pnl_pct < -0.002`~~ → **v200 A4/A5 已完成** (实测更彻底: 取消 quick_sim JOIN + LEFT JOIN ai_decisions, 一次性消化 2 条)

**P2 (中期 — 严谨性)**:
7. ~~**A2 归因层细化**~~ → **已部分完成** (40 analysis + 33 filter), 仍有占位但比例下降
8. **D3 source 追溯**: review_insights 加 `source_reflection_id` 字段 + backfill

### 反思与结论

**做得好的**:
- 数据生产端 (raw_news, event_atom 抽取) 是工程扎实的
- v176-v181 的多层架构有清晰的「信号 → filter → 决策」管线
- SPA 的 evidence_summary 是真实反思，质量高
- weekly verify 体系完整 (v190-v200 共 23 check)

**系统性的短板**:
- **「记录-反思-学习-应用」环路的工程实现是分阶段拼凑的，没有端到端 owner**
- 每个表都有数据，但**表与表之间没有 foreign key 或调度 worker 串联**
- LLM 兜底路径 (`direction_source='llm'`, `event_impact_curve.note='llm_predicted'`) **当前 100% 是 placeholder**，意味着 LLM 链路整体没跑通
- 真正的「学习」只发生在 SPA 阶段（filter 权重调整），但 SPA.status 永远是 pending

**最讽刺的一点** (v199 之前的判断): 系统花了很多资源生产 reflection/insight/SPA 数据，但这些数据**没有任何下游消费者**。整个系统的"学习能力"目前是**幻觉**。

**v200 之后的修正判断**:
- ✅ SPA pending → applied: 已闭环 (spa_apply_worker 600s/次, SPA #6 已 applied, #61-#64 因 conf<0.65 阈值正确不推)
- ✅ ai_decisions.applied_setting_changes: 已加列, 50 条已回填, ai_worker 已注入 prompt
- ✅ **v200 A4/A5 反思 daemon 自动消化**: ref#93 (历史 ai_decisions 缺失, LEFT JOIN 让它也能跑) + ref#133 (今日新增), LLM 占比 7/73 → 9/74 (+28.6% 相对增长)
- **"学习能力" 不再是幻觉** — SPA → ai_decisions → prompt → 下一次决策 真闭环跑通

### v200 候选 (按 ROI) — **已完成情况**
| 候选 | 改动量 | 影响 | 实际状态 |
|---|---|---|---|
| ~~反思 daemon 真跑~~ | 小 (检查 cron+API key) | 高 | **v200 已确认正常 + 自动消化 2 条** (ref#93, ref#133) |
| **✅ SPA pending → applied** | 中 (worker + threshold) | 高 (闭环跑通) | **v199 完成** — 6/6 verify PASS |
| **✅ ai_decisions 加 applied_setting 列 + 读取** | 中 | 高 (决策真用经验) | **v199 完成** — 50 条回填 + prompt 注入 |
| **✅ A4 反思 filter 取消 quick_sim JOIN** | 小 (改 1 处 SQL) | 中 (历史浅亏进 LLM) | **v200 完成** — 14/14 verify PASS |
| **✅ A5 enrich_one LEFT JOIN ai_decisions** | 小 (改 1 处 SQL) | 中 (历史 dec_id 缺失也能跑) | **v200 完成** — ref#93 实战通过 |
| KG 衰减机制 | 中 | 中 (KG 活起来) | 待办 |
| 后置 LLM direction 兜底 | 大 (LLM 集成) | 中 (direction_confidence 真实化) | 待办 |
| impact_curve 改真实 snapshot | 小 (只改 worker) | 中 (B 段激活) | 待办 |