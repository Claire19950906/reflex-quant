# v192 — 实战经验规则闭环注入

## 目标
让 review_agent 产出的实战经验规则 (review_insights) **按品种注入 AI 决策 prompt**，形成闭环：
review_agent (复盘) → strategy_param_adjustments (写入) + review_insights (提炼规则) → ai_worker (注入 prompt)

## v192 #1 — `ai_prompts.load_recent_insights_block` (资产相关)
- `ai_prompts.py::load_recent_insights_block(target_asset, ttl=5, max_items=8)`
- 排序：① 命中 target_asset 优先 ② 通则 (asset='') 次之 ③ source_samples DESC ④ id DESC
- 5s TTL bucketed cache（按 (target_asset, max_items) 分桶）
- counter_factual "反例" 标注
- **4/4 PASS** (`verify_v193_event_atom_direction.py`)

## v192 #2 — `ai_worker.py` 接入 asset-specific 注入

### 修改
- `engine/ai_worker.py::_call_llm_sync` 在原 `insights_section` 后追加 per-asset block
- 仅在 `gate_target_asset` 非空时调 `load_recent_insights_block(target_asset=gate_target_asset)`
- 异常 → 静默零注入（不影响主流程）
- 日志标签 `[INSIGHTS-ASSET] {asset} 注入 ({n} chars)`

### 验收 (`verify_v192_2_insights_integration.py`)
- ✅ `load_recent_insights_block(XAU)` 返回 839 字 block
- ✅ `ai_worker.py` 源码含 `load_recent_insights_block` / `INSIGHTS-ASSET` / `gate_target_asset` 守卫
- ✅ XAU 专属规则 (@#0) 优先于通则 (@#1)
- ✅ 5s TTL 缓存命中 (block == block2)
- ✅ XAU vs BTC 桶独立 (839 vs 830 chars)
- **5/5 PASS**

### 集成到 weekly verify (`verify_v190_weekly.py`)
- 加 3 项 v192 #2 checks → **26/26 PASS** (21 base + 3 v192 + 2 v193)

## 数据流
```
review_agent._generate_insights_llm (kind=1, n>=5)
   ↓ INSERT
review_insights (rule, asset, scope, source_samples, counter_factual)
   ↓ 
weekly_insight_apply.py (周一扫 + 模糊关键词 → param_key 写入 strategy_param_adjustments)
   ↓
[ai_worker 决策时] ai_prompts.load_recent_insights_block(target_asset)
   ↓ 按 asset 匹配 + 反例标注
   ↓ 拼进 prompt 尾部 ("═══ 近期实战经验规则 ═══")
   ↓ LLM 决策时优先参考
```

## 闭环指标
| 步骤 | 指标 |
|---|---|
| review_agent 产出 | kind=1 insight 27 条 (n>=5) |
| weekly_insight_apply 写入 | strategy_param_adjustments 3 条 (param_key 从模糊关键词解析) |
| ai_prompts 注入 | 8 条/asset (max_items=8) |
| ai_worker 实际注入 | 26/26 weekly verify PASS |

## 已知限制
- review_insights 只有 1 条 asset='XAU'，35 条 asset=NULL（通则）
- weekly_insight_apply 当前不会推断 matched news 的 target_asset → insight asset 字段都空
- **未来改进**：weekly_insight_apply INSERT 时填 asset = matched_news.gate_main_asset (增加 asset-specific insight 比例)