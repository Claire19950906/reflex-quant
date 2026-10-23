# v198 Summary — dim_event_type.direction_bias 覆盖补全

## 背景
v190/v193 填了大部分 `direction_bias`，但残留 3 条 bias 为空（覆盖率 24/27）：
- `cpi_release` (id=673, zh/en/cat 皆空)
- `market_event` (id=38, v176.5 LLM fallback 占位)
- `oil_inventory` (id=209, zh/en/cat 皆空 + bias 空)

## 数据调查
| code | event_atom | ai_decisions | 真实用途 | 策略 |
|---|---|---|---|---|
| `cpi_release` | 0 | 0 | **dead code** — 无任何代码引用 | 保留空 + 排除覆盖率 |
| `market_event` | 31 (全 unknown) | 49 | **v176.5 placeholder** — LLM 兜底中性 | 保留空 + 排除覆盖率 |
| `oil_inventory` | 0 | 0 | **event_struct 字典有 "原油库存" → oil_inventory** 映射，但 dim_event_type 行早期手插漏了 name | 补 name + 填 bias |

## 决策
**oil_inventory bias = `{"WTI": "neutral"}`**
- EIA 库存数据有增/减双方向（库存大减→bullish，大增→bearish）
- event_struct 字典规则不区分方向，无法抢答
- 走 neutral 兜底，让 ai_worker 基于实际方向独立判断（v199 follow-up 已留）

**market_event / cpi_release bias 保持 `{}`**
- market_event 是 v176.5 by-design 中性占位
- cpi_release 是 dead code（无任何引用），保留防外部兼容性破坏
- 两个都在 weekly verify 的覆盖率统计里显式排除

## 实施

### 1. `src_python/seed_event_dim.py` (L82-87)
```python
"oil_inventory": ("原油库存", "Crude Oil Inventory", "commodity", {
    # v198: 库存数据有增/减双方向（EIA 库存大减→WTI bullish / 大增→bearish）。
    # event_struct 字典规则不区分方向，因此 direction_bias 走 neutral 兜底。
    "WTI": "neutral",
}),
```

### 2. `scripts/migrate_v198_dim_bias.py` (new, 一次性运行)
- UPDATE oil_inventory 行：补 name_zh/en/category + bias
- market_event / cpi_release 保留空（no-op + 注释）
- 输出:
  ```
  [oil_inventory] UPDATE 行数 = 1
  [cpi_release] 当前 bias = {}（dead code，保持 {})
  [market_event] 当前 bias = {}（v176.5 placeholder，保持 {})
  ```

### 3. `scripts/verify_v198_dim_bias_completion.py` (new, **5/5 PASS**)
1. dim_event_type 全表 active=1 (27/27)
2. oil_inventory name + bias 完整
3. by-design/dead code 排除列表正确
4. 覆盖率 = 25/25 = 100% (排除后)
5. `dim_tables.direction_for("oil_inventory", "WTI") = "neutral"` 实测

### 4. `scripts/verify_v190_weekly.py` 改动
- **v193 #1 段** (L238-252): 排除 market_event/cpi_release，阈值 0.85 → 0.95
- **v198 段** (L389-430, new): 5 个 check 全过

## 结果
- **verify_v198_dim_bias_completion.py**: 5/5 PASS
- **verify_v190_weekly.py**: 21/21 PASS, 0 FAIL
- dim_event_type 覆盖率: 24/27 (88.9%) → **25/25 (100%, 排除后)**

## 排除集语义
```python
_EXCLUDED_FROM_DIM_BIAS = {"market_event", "cpi_release"}
```
写在 weekly verify 里统一管理，跨版本可追溯。

## 文件清单
| 路径 | 改动 |
|---|---|
| `backend/src_python/seed_event_dim.py` | L82-87 oil_inventory bias={WTI:neutral} |
| `backend/scripts/migrate_v198_dim_bias.py` | **new** 一次性 migration |
| `backend/scripts/verify_v198_dim_bias_completion.py` | **new** 5/5 PASS |
| `backend/scripts/verify_v190_weekly.py` | L238-252 (v193 #1 排除逻辑) + L389-430 (v198 段 new) |
| `backend/scripts/v198_summary.md` | **new** (本文件) |

## v199 候选
- ai_worker 信号链路读取 `direction_confidence` 动态调整 BUY/SELL 权重
  （v197 docs 已留为 follow-up）
- ai_worker 对 oil_inventory bias="neutral" 单独处理：根据实际库存变化方向
  （库存增 → WTI bearish / 库存减 → bullish）覆盖 neutral 默认