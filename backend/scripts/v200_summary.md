# v200 Summary — 反思 Filter 放宽 + LLM 反思占比修复

**日期**: 2026-09-28
**作者**: Cline (audit + fix cycle)
**状态**: ✅ 完成, **runtime verified** (+2 反思 LLM 写入, 9.5% → 12.2%)

---

## Audit 发现

### 起点 (v199 验证后反思 daemon "永远 processed=0")

**audit_capabilities_report.md (A 段)** 误判反思 daemon 三大原因:
1. daemon 没启动 → 错 (实际 300s/次跑, batch=5, 7 个 daemon 全在)
2. API key 失效 → 错 (其他 daemon 都跑, key 有效)
3. filter 太严 → 部分对, 但不是 pnl 阈值

### 真根因 (三层叠加)

通过 SQL JOIN 命中实测发现:

```
trade_reflections_v2:       73 条 (24h +13, 7d +73)
  decision_id 范围:         [1243, 1000247]
  
ai_decisions JOIN 命中:      13/73 (17.8%)
  (60 条历史反思的 decision_id 在 v158 cleanup 时已被删除, ai_decisions 当前 min=1757)

quick_sim_trades 命中:      15 条 trade, 仅 13 个不同 decision_id (subset of 上面 13 条)
```

v199 SQL filter 三层全 INNER JOIN, 等于把候选集缩到 13/73 = **17.8%**,
再叠加 verdict='LOSS' / depth≥3 / pnl<-0.5% / layer 守卫, 命中数 = **0 条**。

实际可填的反思:
- pnl<-0.5% (深度 8-9, error_layer='analysis') 的反思 = 8 条
- 其中 7 条**已被 admin reproduce 端点处理过** (1 月前手动触发, +5-7 分钟内近实时)
- 剩下 1 条 ref#93 — quick_sim JOIN 不上, daemon 跳过

---

## v200 A4/A5 修复

### A4: 取消 quick_sim JOIN

`enrich_pending_loop` 和 `enrich_one` 内部都去掉 `JOIN quick_sim_trades`。

**理由**: quick_sim_trades 表只覆盖 13 个决策 id, 历史反思大量缺失 verdict 字段。
直接读 `trade_reflections_v2.pnl_pct < -0.005` 判定 LOSS 更可靠 (pnl_pct 字段 73 条都有值)。

### A5: LEFT JOIN ai_decisions

`enrich_one` 内部把 `JOIN ai_decisions` 改成 `LEFT JOIN ai_decisions`。

**理由**: 60 条历史反思的 decision_id 不在 ai_decisions (v158 cleanup 副作用)。
LEFT JOIN 让 prompt 里 ai_decisions 字段为 NULL (用空字符串兜底), 历史反思仍能跑 LLM。

### SQL diff (after v200)

```sql
-- enrich_pending_loop
SELECT r.id
  FROM trade_reflections_v2 r
 WHERE r.llm_depth_reflection = ''
   AND r.depth_score >= 3
   AND r.pnl_pct < -0.005
   AND r.error_layer IN ('analysis', 'filter')
 ORDER BY r.id DESC LIMIT ?;

-- enrich_one
SELECT r.id, r.decision_id, r.asset, r.action, r.error_layer, ..., a.event_strength, ...
  FROM trade_reflections_v2 r
  LEFT JOIN ai_decisions a ON a.id = r.decision_id
 WHERE r.id = ?;
```

---

## 实测结果

### v200 A4 SQL filter 命中数

```
新 SQL:        depth≥3 AND pnl<-0.5% AND layer IN('analysis','filter') AND llm=''
  → 2 条: ref#93 (历史) + ref#133 (今日新增)
旧 SQL (v199): 三层 INNER JOIN + verdict='LOSS' + pnl<-0.5%
  → 1 条 (ref#133, 因为 ai_decisions 在 + quick_sim 在 + verdict=LOSS)
```

### 真实 daemon 跑 (batch=5)

```
跑前: total=74  with_llm=7  ratio=9.5%
跑后: total=74  with_llm=9  ratio=12.2%  (Δllm=+2)
```

成功给 ref#93 (ai_decisions 缺失, prompt 残缺但 LLM 仍输出) + ref#133 (完整 prompt) 写入 LLM 反思。

---

## Files

### Edited
- `backend/src_python/trade_reflection_llm.py`
  - L101-122 `enrich_one` 改 LEFT JOIN ai_decisions + 去 quick_sim JOIN + 去 verdict 守卫
  - L206-247 `enrich_pending_loop` 去 quick_sim JOIN + 新 error_layer 守卫
- `backend/scripts/audit_capabilities_report.md` (v199 audit 修订)
- `backend/scripts/verify_v200_reflection_filter.py` (new, 14/14 PASS)
- `backend/scripts/verify_v190_weekly.py` (寄生接入 v200 A5 check)

### Verified
- `verify_v200_reflection_filter.py` 14/14 PASS:
  - C1.1 SQL 不含 JOIN quick_sim_trades ✅
  - C1.2 SQL 不含 qs.verdict 条件 ✅
  - C1.3 SQL 含 error_layer IN 守卫 ✅
  - C1.4 SQL 含 pnl_pct < -0.005 阈值 ✅
  - C2.1 当前命中数 ≥ 1 (实测 2) ✅
  - C2.2 已写 LLM ≥ 7 ✅
  - C3 dry-run 不写库 ✅
  - C4 spy 调用 ids 满足新 filter ✅
  - C5 新 SQL 命中 > 旧 SQL 命中 (1→2) ✅
  - C6 LLM 反思格式兼容 (raw_response JSON wrap) ✅
  - C7.1 enrich_one LEFT JOIN ai_decisions ✅
  - C7.2 enrich_one 不含 quick_sim JOIN ✅
  - C7.3 enrich_one 不含 qs.verdict ✅

### Runtime verified
- 真跑 `enrich_pending_loop(batch=5)` → processed=2
- ref#93 + ref#133 都拿到 LLM 反思
- 全表 LLM 占比 9.5% → 12.2%

---

## 后续跟踪

1. **生产持续累积**: ref#133 证明 paper_reflection_worker 还在持续产生新反思,
   daemon 现在能自动消化 (v199 永远 processed=0)
2. **LLM 反思占比预期**: 数据增长 + daemon 主动抓取, 1-2 周后预计到 30-50%
3. **审计报告同步**: A 段评级已从 D → C+ (audit_capabilities_report.md 顶部流程图 + 表 + 雷达图)
4. **audit_capabilities_report.md 需更新**: 把 12.2% 写入 "学习能力" 段落 (当前是 9.6% / 20% 雷达)

---

## Tag
`v200`, `reflection-llm`, `filter-relaxation`, `audit-fix`