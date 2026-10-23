# v197 — event_atom 方向置信度区分 news 明确 vs fallback

**日期**: 2026-09-28
**类别**: insight pipeline / 数据质量分层
**影响范围**: event_atom.direction_confidence, downstream ai_worker / signal 链路

---

## 目标

把 `direction_confidence` 从单一固定值改成「按数据来源分层」：

- **news 明确路径** (`direction_source='rule_bias'`): `0.7` — news 直接提到资产 + dim_event_type 方向 bias
- **fallback 路径** (`direction_source='rule_bias_default_asset'`): `0.4` — news 没提资产，靠 event_type 兜底推断

下游 ai_worker / signal 链路可读 `direction_confidence` 在 BUY/SELL 决策时降权
fallback 信号（避免把低置信方向当成高分信号使用）。

---

## 改动

### 1. `src_python/event_atom.py`

**新增常量** (~line 130):

```python
# v197: fallback 路径的 direction_confidence 调低到 0.4（vs news 明确 0.7）。
# 理由：news 没提具体资产、靠 event_type 兜底推断时，方向信号置信度应低于
#       「news 直接点名 XAU + 规则推断方向」的情形。下游 ai_worker 可读此值
#       在 BUY/SELL 决策时降权（避免把低置信方向当成高分信号使用）。
FALLBACK_DIR_CONFIDENCE: float = 0.4
```

**分支逻辑** (~line 470-485):

```python
asset_source = "rule_bias"
if not asset_code and code in DEFAULT_ASSET_FOR_EVENT_TYPE:
    asset_code = DEFAULT_ASSET_FOR_EVENT_TYPE[code]
    asset_source = "rule_bias_default_asset"
direction = "unknown"
d_conf = 0.0
if asset_code:
    bias_cn = dim_tables.direction_for(code, asset_code, connection=connection)
    if bias_cn:
        direction = direction_cn_to_db(bias_cn)
        # v197: 区分 news 明确 (0.7) vs fallback (0.4)
        # fallback = 资产是按 event_type 兜底推断而非 news 显式提到
        if asset_source == "rule_bias_default_asset":
            d_conf = FALLBACK_DIR_CONFIDENCE  # 0.4
        else:
            d_conf = float(direction_confidence)  # 0.7
```

---

## 验证

### `scripts/verify_v197_dir_confidence_fallback.py` — 5/5 PASS

策略：用 monkeypatch 替换 `event_struct.extract_events`，构造两种 target_assets：
- `target_assets=[]` → 触发 fallback (`asset_code` 由 `DEFAULT_ASSET_FOR_EVENT_TYPE` 兜底)
- `target_assets=['XAU']` → news 明确路径

避开 LLM/正则词典不可控的现实约束，直接验证 d_conf 分支逻辑。

| 检查 | 期望 | 实际 |
|---|---|---|
| FALLBACK_DIR_CONFIDENCE == 0.4 | 0.4 | 0.4 ✅ |
| DEFAULT_ASSET_FOR_EVENT_TYPE 含关键 fallback key | 8 keys | 8 keys ✅ |
| fallback 路径 d_conf=0.4 source=rule_bias_default_asset | 0.4 | 0.4 ✅ |
| news 明确路径 d_conf=0.7 source=rule_bias | 0.7 | 0.7 ✅ |
| 清理测试 atom+raw | del > 0 | del=2 ✅ |

### `scripts/verify_v190_weekly.py` — 21/21 PASS (新增 v197 段 6/6)

```
rule_bias_default_asset d_conf 分布: {0.4: 2, 0.5: 8}
rule_bias             d_conf 分布: {0.7: 75}
```

历史 d_conf=0.5 是 v194→v196 之间某版逻辑残留（v197 之前），v197 之后新插的
都应该是 0.4/0.7。**不强制要求历史数据迁移**（迁移脚本不在本次范围）。

---

## 不在本次范围

- 历史 `direction_confidence=0.5` 数据迁移（10 条 `rule_bias_default_asset`）
- ai_worker 信号链路对 `direction_confidence` 权重的动态调整（v198+ 考虑）
- dim_event_type 剩余 3 条 `direction_bias` 缺失的补全（v190 backlog）

---

## 与 v196 区别

| | v196 | v197 |
|---|---|---|
| 作用对象 | `review_insights.asset` | `event_atom.direction_confidence` |
| 区分字段 | `[XAU]/[BTC]` 前缀 | `direction_source` |
| 目的 | 让 review_agent LLM 输出按品种归属 | 让方向置信度按数据来源分层 |
| 验证手段 | 静态字符串匹配 + DB 分布 | monkeypatch extract_events + DB 分布 |