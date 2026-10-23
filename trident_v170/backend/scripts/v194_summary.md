# v194 — extract_and_persist 默认资产兜底

## 目标
消除 v193 #2 的事后修补：让 `event_atom.extract_and_persist` 在 news 没有明确 `target_assets` 时**自动按 event_type 填默认资产**（不再需要 backfill 脚本）。

## 修改

### 1. `event_atom.py` — `DEFAULT_ASSET_FOR_EVENT_TYPE` 字典
```python
DEFAULT_ASSET_FOR_EVENT_TYPE: Dict[str, str] = {
    "geopolitical_conflict": "XAU",   # 地缘冲突 → 黄金避险
    "geopolitical_easing": "XAU",     # 地缘缓和 → 黄金避险缓解
    "opec_cut": "WTI",                # OPEC 减产 → 原油
    "opec_increase": "WTI",           # OPEC 增产 → 原油
    "central_bank_gold": "XAU",       # 央行购金 → 黄金
    "oil_inventory": "WTI",           # 原油库存 → 原油
    "natural_disaster": "WTI",        # 自然灾害 → 原油
    "trade_tariff": "XAU",            # 关税 → 黄金
}
```

### 2. `extract_and_persist` — fallback 逻辑
```python
target_assets = atom.get("target_assets") or []
asset_code = target_assets[0] if target_assets else None
asset_source = "rule_bias"
if not asset_code and code in DEFAULT_ASSET_FOR_EVENT_TYPE:
    asset_code = DEFAULT_ASSET_FOR_EVENT_TYPE[code]
    asset_source = "rule_bias_default_asset"
# ...
direction_source=asset_source,  # 区分 news 明确 vs fallback
```

## 验收 (`verify_v194_default_asset.py`)
- ✅ DEFAULT_ASSET_FOR_EVENT_TYPE 含 8 个映射
- ✅ extract_and_persist 含 fallback 逻辑 + 动态 source
- ✅ v193 #2 backfill 数据 8 条 `rule_bias_default_asset`
- ✅ fallback 数据 bias 一致率 **100%** (geopol_conflict/XAU: bullish/bullish n=8)
- ✅ 当前 `_EVENT_RULES` 已配 target_assets → fallback 是预防性代码
- **5/5 PASS**

### 集成到 weekly verify
- 加 4 项 v194 checks → **30/30 PASS** (21 base + 3 v192 + 4 v194 + 2 v193)

## 设计取舍
| 选项 | 优 | 劣 |
|---|---|---|
| **v194 选** — 软兜底（仅当 asset=NULL） | 不破坏现有 news 明确指定的语义 | 需要在字典维护映射 |
| 硬填默认（忽略 news target_assets） | 无需 fallback 分支 | 覆盖真实 news 信息（如 "OPEC 减产打击 WTI 之外还连带 DXY"） |
| 不填 (status quo) | 简单 | 需要 backfill 事后修补（v193 #2） |

→ 选软兜底：保留 news 优先级，仅在缺失时兜底。

## 数据流对比
**修复前**：
```
news "中东紧张升级" → extract_events(target_assets=[XAU,WTI]) → asset=XAU ✓
news "OPEC 减产" (asset=[]) → asset=NULL → direction=unknown ✗
                                          ↓ 需 backfill_v193b 事后修补
```

**修复后**：
```
news "中东紧张升级" → extract_events(target_assets=[XAU,WTI]) → asset=XAU ✓ (news 优先)
news "OPEC 减产" (target_assets=[]) → DEFAULT_ASSET_FOR_EVENT_TYPE[opec_cut]=WTI → asset=WTI ✓
                                     direction_source=rule_bias_default_asset
```

## direction_source 字段语义
| 值 | 含义 |
|---|---|
| `rule_bias` | news 明确指定 asset，规则 bias 给出方向 |
| `rule_bias_default_asset` | news 没指定 asset，按 event_type 兜底填默认资产 + 规则 bias |
| `llm` / `ai` | LLM 自行推断（外部） |
| `platform_published_at` | 平台时间源标记（非 direction 来源） |

## 已知限制
- `_EVENT_RULES` 当前都配了 target_assets，fallback 是**预防性代码**（暂未实际触发）
- 兜底后的 direction_confidence 仍 = 0.7（与 news 明确路径相同）— 未区分 "确定度"，未来可降权 (如 0.4)
- 仅 8 个 event_type 在字典里 — 新增 event_type 不会自动兜底，需手动加映射

## 后续
- v195 (可选): weekly_insight_apply INSERT 时填 asset = matched_news.gate_main_asset，让 review_insights 不全是通则