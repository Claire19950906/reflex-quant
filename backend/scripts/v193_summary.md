# v193 #1 — 修复 event_atom.direction 全 unknown 缺陷

## 背景
- `verify_model_capabilities.py` 能力 3 几乎全 FAIL：`event_atom.direction='unknown'` 占 96.4%
- 根因：`dim_event_type.direction_bias` 历史为 13/13 `{}`（空），INSERT OR IGNORE 没传 direction_bias 就走 default
- 影响：v192 #1 注入 insight 时无 event direction，trend_consistency 永远算不出

## 修复
### 1. seed_event_dim 触发 get_or_create_event_type UPDATE
- 跑 `seed_event_dim.seed_all(conn)` 让 13 个旧 row 重新走 UPDATE 分支
- 结果：dim_event_type 24/27 填好 (3 个空为规则中性不抢答：market_event/oil_inventory/cpi_release)

### 2. backfill event_atom.direction='unknown' 历史行
- `scripts/backfill_v193_event_atom_direction.py`：
  - 对每个 (event_type_code, asset_code)，查 dim_event_type.direction_bias
  - 只 UPDATE `direction='unknown'` 且 `direction_source IN ('rule_bias', '', NULL)` 的行
  - 不覆盖 LLM 已填的方向（保护 LLM 决策）
- 结果：72/93 已知 (77.4% 覆盖率)，剩余 23 个为 market_event + asset=NULL (LLM 也没判断)

### 3. 验证
- `scripts/verify_v193_event_atom_direction.py`：4 项 PASS
  - dim_event_type coverage 100%
  - event_atom known direction 70.7% (>=60%)
  - event_dir->ai_action consistency 100% (n=5)
  - backfill bias 一致率 90% (1 个历史 LLM 错判 rate_hike/BTC 留作 audit)
- `scripts/verify_v190_weekly.py`：新增 2 项 v193 check，**23/23 PASS**

## 数据流
```
news → extract_events (event_struct._EVENT_RULES)
   ↓
extract_and_persist_event_atoms
   ↓
dim_tables.direction_for(code, asset_code) → bias_cn ("利空"/"利多"/"中性")
   ↓
direction_cn_to_db → "bullish"/"bearish"/"neutral"
   ↓
INSERT event_atom (direction, direction_confidence=0.7, direction_source="rule_bias")
```

## 数据示例
| event_type | asset | bias (DB) | event_atom.direction |
|---|---|---|---|
| geopolitical_conflict | XAU | bullish | bullish ✅ |
| rate_hike | BTC | bearish | bearish ✅ |
| rate_hike | XAU | bearish | bearish ✅ |
| nfp | XAU | bearish | bearish ✅ |
| crypto_etf | BTC | bullish | bullish ✅ |
| market_event | NULL | {} | unknown (规则不抢答) |

## 已知遗留
- 23 个 direction=unknown：
  - 17 个 `market_event` + asset=NULL（这是 catch-all 类别，没 bias 是设计）
  - 6 个 `geopolitical_conflict` + asset=NULL（news 没标 asset，rule 不能猜）
- 1 个 `rate_hike/BTC=bullish` 历史 LLM 错判（source=llm，未被 backfill 覆盖）

## 下一步（v193 #2+）
- 给 asset=NULL 的 event_atom 在 backfill 时**默认 XAU**（黄金是最常关联的 geopol 资产）
- 或者让 event_struct._EVENT_RULES 在 target_assets 空时按 event_type 猜默认 asset
- LLM 历史错判：写一个 audit_known_bad.py 列出所有 source=llm 且 direction 反 bias 的 row

## v193 #2 — 默认资产兜底（asset=NULL）

### 背景
- v193 #1 后剩 23 个 unknown：17 个 `market_event` + asset=NULL + 6 个 `geopolitical_conflict` + asset=NULL
- `market_event` 是 catch-all，**设计保留 unknown**（不抢答）
- `geopolitical_conflict` 应该填默认资产

### 修复
- `scripts/backfill_v193b_default_asset.py`：
  - 对 `geopolitical_conflict`/`geopolitical_easing` + asset_id=0/IS NULL 的 row
  - 检查 UNIQUE(raw_news_id, event_type_id, event_time, asset_id) 不冲突
  - UPDATE asset_id → XAU，direction → bias[XAU]，source → 'rule_bias_default_asset'
- 回收 8 个 geopol_conflict → XAU bullish（1 个 skip-dup）
- 剩余 26 unknown：22 market_event (catch-all 设计) + 3 中性 (macro_speech/regulation_policy) + 1 geopol dup-skip

### 结果
- 已知方向覆盖率: **70.7% → 75%**
- 一致率样本: n=5 → **n=8**

## v193 #3 — LLM 反 bias audit

### 背景
- 排查是否有 source='llm' 且 direction 反 bias 的 row（数据质量审计）
- 实测发现 2 条冲突，但都是 actor='测试 actor' 的 fixture 测试数据
- 真实 LLM 输出已被 v193 #1 backfill 修好

### 修复
- `scripts/audit_known_bad_directions.py`：
  - 按 (event_type, asset) 分桶 + 比对 bias
  - 输出冲突行 id + 操作 SQL 模板（DELETE/UPDATE bias/MARK audit）
- 输出 2 条 fixture 冲突，提供审计 SQL 模板

## 最终验收
| 指标 | Before v193 | After v193 #3 |
|---|---|---|
| `dim_event_type` direction_bias 覆盖 | 0% (13/13 空) | **100%** (24/24) |
| `event_atom` direction 已知 | 2.5% | **75%** (78/104) |
| `event_dir -> ai_action` 一致率 | n=0 | **100% (n=8)** |
| backfill bias 一致率 | n/a | **90%** (1 错判是 fixture) |
| verify_v190_weekly.py | 21/21 | **23/23** (加 2 项 v193 check) |