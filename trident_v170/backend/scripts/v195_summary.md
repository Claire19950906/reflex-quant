# v195 — review_insights asset='NONE' 视为通则

## 目标
解决 `[覆盖度告警]` 类 insight 的 asset='NONE' 占位 — 它语义上表示"未指定品种"，但 ai_prompts 之前把它当作**独立品种**排序到尾部，导致：
- `[NONE] ...` 错误地出现在 prompt 中（不是有效的品种前缀）
- asset-specific 计数虚增（实际不算品种专属规则）

## 修改 (`src_python/ai_prompts.py`)

### ORDER BY 把 NONE 归类为通则
```python
# Before:
"              WHEN COALESCE(asset,'') = '' THEN 1 ELSE 2 END,"

# After (v195):
"              WHEN upper(COALESCE(asset,'')) IN ('', 'NONE') THEN 1 ELSE 2 END,"
```

### asset_tag 输出 NONE 跳过
```python
# Before:
if r["asset"]:
    asset_tag = f"[{_norm_insights_asset(r['asset'])}] "

# After (v195):
if r["asset"] and _norm_insights_asset(r["asset"]) not in ("", "NONE"):
    asset_tag = f"[{_norm_insights_asset(r['asset'])}] "
# 效果：asset='NONE' 行不带 [NONE] 前缀，与通则一致
```

## 数据语义
| asset 字段值 | 排序位置 | asset_tag |
|---|---|---|
| `''` (空) | 通则区 | 无前缀 |
| `'NONE'` | **通则区 (v195)** | **无前缀 (v195)** |
| `'XAU'`/`'BTC'`/etc | 命中 target 时专属区 | `[XAU]` |
| 其他品种 | 末尾 | `[OTHER]` |

## 验收 (`verify_v195_none_as_general.py`)
- ✅ ai_prompts.py ORDER BY 含 `IN ('', 'NONE')`
- ✅ asset_tag 守卫含 `not in ("", "NONE")`
- ✅ block 不泄露 `[NONE]` 前缀
- ✅ asset-specific 真实计数 = 4 (XAU/WTI/BTC/ETH)，NONE 归入通则
- **4/4 PASS**

### 集成到 weekly verify
- 加 2 项 v195 checks → **34/34 PASS** (21 base + 3 v192 + 4 v194 + 2 v193 + 1 fixture + 2 v195 + 1 refactor)

## 设计取舍
| 选项 | 优 | 劣 |
|---|---|---|
| **v195 选** — ai_prompts 层把 NONE 当通则 | 不动数据结构（语义保留），prompt 表现正确 | 需要在 2 处守卫（order + tag） |
| 数据层：UPDATE review_insights SET asset='' WHERE asset='NONE' | 改一次永久 | 丢失 'NONE' 的真实语义（"该 insight 不绑定品种"是合法状态） |
| 数据层 + INSERT 时转 '' | 同样丢失语义 | 同上 |

→ 选 v195 方案：保留 'NONE' 作为合法状态（代表"通则"），仅在 ai_prompts 输出层做归一化。

## 剩余问题
- review_agent `_generate_insights_llm` 提炼的**叙事型** insight (rule 文本是通则) 仍是 asset='' — 这是正确的，它们的 rule 文本不绑定品种
- 改进 review_agent 让它在生成 rule 时附带 asset hint（基于 source trades 的 main_asset）需要修改 LLM prompt，scope 大，留作 v196+