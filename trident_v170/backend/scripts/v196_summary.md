# v196: review_agent LLM 输出/解析 [XAU] asset 前缀

## 动机

之前的 review_agent 把 LLM 生成的所有经验规则都写成 `asset=''` (通则)，
但实际上 LLM 给出的很多规则会明显限定品种（如 "XAU 在美联储决议前 4h 胜率低"），
ai_worker 在调 load_recent_insights_block 时把这些规则当作通则注入到所有品种的 prompt，
导致：
- 黄金经验污染了 BTC 决策
- 加密经验污染了 XAU 决策
- 规则精度下降

v196 让 LLM 输出可选 `[XAU]/[BTC]/[ETH]/[WTI]/[SOL]/[*]` 资产前缀，save_insights 解析后写入 asset 字段，
供 ai_worker 按品种匹配注入（已有 v192 #2 的 load_recent_insights_block gate_target_asset 机制）。

## 改动

### 1. review_agent.py — `_generate_insights_llm` prompt

**位置**: `src_python/review_agent.py:2510-2546`

新增 v196 说明段（紧接 v191 `[param_key]` 规范后）：

```
v196 新增（可选）：如果规则明确只适用某品种，可在 param_key 之前加
[XAU]/[BTC]/[WTI]/[ETH]/[SOL]/[*] 资产前缀，系统会自动剥离并填入
asset 字段，供 ai_worker 决策时按品种匹配注入。
asset=通则（不限定品种）→ 用 [*] 或省略前缀。
示例：[XAU] [score.confidence_threshold] 黄金避险情绪时 BUY 信号置信度门槛上调。
```

### 2. review_agent.py — `_extract_from_quick_sim_llm` prompt

**位置**: `src_python/review_agent.py:2631, 2643-2644`

快速评测提炼路径同样支持：在示例 insights 字符串里加 `[XAU]` 前缀，
并在末尾加 "⑥v196：insights 若明确限定某品种，可在前面加 [XAU]/[BTC]/[WTI]/[ETH]/[SOL] 资产前缀"。

### 3. review_agent.py — 新增 `_extract_asset_prefix` + `save_insights` 改造

**位置**: `src_python/review_agent.py:2944-2980`

- 新增 `_KNOWN_ASSET_CODES` frozenset（含 XAU/BTC/ETH/WTI/SOL/CN_STOCK/US_STOCK/INDIVIDUAL_STOCK/SKHYNIX）
- 新增 `_extract_asset_prefix(text) -> (asset_code, rule)` 纯函数：
  - `[XAU] xxx` → `('XAU', 'xxx')` （剥离前缀）
  - `[*] xxx` → `('', 'xxx')` （显式通则，剥离）
  - `[OTHERS] xxx`（未识别）→ `('', '[OTHERS] xxx')` （保留原文向后兼容）
  - `[layer.sub.signal_name] xxx`（param_key 前缀）→ `('', 原文本)` （不剥离）
  - 无前缀 → `('', text)` （向后兼容）
- `save_insights` 改造：
  - 遍历 insights 时先 `_extract_asset_prefix(raw_rule)`
  - `INSERT INTO review_insights (rule, asset, scope, active, source_samples, origin)`
  - asset 写入识别后的 code，未识别→空字符串
- 顶部 `from typing import ... Tuple`

### 4. verify_v196_asset_prefix.py — 验证脚本

**位置**: `scripts/verify_v196_asset_prefix.py`

4 项验收：
1. `_extract_asset_prefix` 9 例解析测试（XAU/BTC/ETH/WTI/SOL/*/无前缀/OTHERS/param_key）
2. review_agent.py 含 v196 prompt + 函数 + 调用
3. `save_insights` 等价路径端到端：4 条测试数据 → asset 字段写入正确
4. 二次写入同样前缀 → asset 字段不变（幂等）

跑通：4/4 PASS

### 5. verify_v190_weekly.py — weekly 检查加 v196 段

**位置**: `scripts/verify_v190_weekly.py:292-337`

新增 `v196: review_agent LLM 输出/解析 [XAU] asset 前缀` 段：
- 5 个 check：两处 prompt 都含 v196、含 `_extract_asset_prefix` 函数、save_insights 调用、INSERT 含 asset 字段
- 统计 `review_insights` asset 分布（XAU/BTC/ETH/WTI/SOL/通则）

## 现状（验证后）

review_insights 表分布：
- 通则 (∅): 35 条
- XAU / BTC / ETH / WTI 各 1 条（structured_reflection 生成的覆盖度告警，rule 内 "Asset=XAU" 字面 + asset 字段同时写入）
- NONE: 1 条（v195 后已合并到通则语义）
- 合计 40 条

v196 注入后，下一次 weekly review 调用 LLM 时，新生成的规则若带 `[XAU]` 等前缀，
会自动写入 asset 字段，由 ai_worker 按品种匹配注入（v192 #2 机制）。

## 不变 / 向后兼容

- 旧规则（asset=''）继续按通则处理（v192 + v195 已支持）
- `_extract_asset_prefix` 对无前缀文本 / 未知前缀 / param_key 前缀都返回 `('', text)`，原文本不变
- save_insights 的 per-row commit / OperationalError 静默 skip 行为保留（v189-7 设计）
- LLM 在 prompt 里强调"可选" — 即使不输出前缀也完全正常

## 后续可做（未实施）

- 监控 v196 之后 LLM 输出的带前缀率（统计 weekly 新增 rule 中 `[XAU]` 占比）
- 写一份回溯脚本，给已有 35 条通则 rule 自动分类（基于关键词匹配 XAU/BTC/...）回填 asset
- 调小 `_INSIGHTS_CACHE_MIN_SAMPLES` 让 XAU/BTC/WTI 等低样本品种也能注入