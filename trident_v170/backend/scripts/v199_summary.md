# v199 — SPA Apply 闭环 (P0 断链修复 #1/#2/#3)

## Goal
打通 `strategy_param_adjustments` (SPA) → `status='applied'` → `ai_decisions.applied_setting_changes` → LLM prompt 注入 的 4 处断链。

## 实施

### 1. Schema 变更 (`scripts/migrate_v199_spa_apply.py`)
- `ai_decisions` 加列: `applied_setting_changes TEXT DEFAULT ''`
- 幂等 (重跑跳过)

### 2. SPA Worker (`src_python/spa_apply_worker.py`)
- 阈值: sample_size ≥ 5 + avg_confidence ≥ 0.65 (对齐 v182 reflection)
- 白名单: `filter.*` / `analysis.*` / `sizing.*` / `score.*` 共 17 个 param_key
- 路径 (notes-only applied):
  - `UPDATE strategy_param_adjustments SET status='applied', applied_setting_change='[param_key] direction=X mag=Y (asset=Z, n=N, conf=C)'`
- 同步: 按 asset 维度回填到 `ai_decisions.applied_setting_changes` (最多 50 条)
- API: `--once` / `--dry-run` / `--daemon --interval=600`

### 3. Prompt 注入 (`engine/ai_worker.py`)
- 新函数 `_load_spa_applied_block_sync(asset, max_chars=600, ttl=60)`
- 数据源: `strategy_param_adjustments WHERE status='applied'`
- 匹配: asset 精确匹配优先 + NONE/* 兜底
- 格式: 
  ```
  # 策略调整已应用 (v199 SPA Applied Block)
    → asset=BTC 同 asset 0 条, 全局兜底 1 条
  [SPA1] [filter.asset_match.main_asset_missed] off mag=0.45 (n=31, conf=0.90)
  ```
- 注入位置: `_load_experience_block_sync` 之后, prompt 末尾追加

## 验证 (`scripts/verify_v199_spa_apply.py`)

| Check | 结果 |
|---|---|
| C1 migration 已应用 | ✓ ai_decisions.applied_setting_changes 列存在 |
| C2 worker 模块 | ✓ run_once/daemon_loop/KNOWN_FILTER_PARAM_KEYS |
| C3 pending 推进 | ✓ 0 条 pending 满足阈值但未推进, 1 条已 applied |
| C4 applied_setting_change 文本 | ✓ 已填 `[filter.asset_match.main_asset_missed] direction=off mag=0.45 (asset=NONE, n=31, conf=0.90)` |
| C5 ai_decisions 回填 | ✓ 50 条 ai_decisions.applied_setting_changes 非空 |
| C6 prompt 注入函数 | ✓ BTC/XAU block 148 chars 含 SPA Applied 标记 |

**v199 verify: 6/6 PASS**

接入 weekly verify (`verify_v190_weekly.py` L436-457 段) — PASS。

## 修复的断链 (按 audit_capabilities_report.md D 段)

| 断链 | 修复前 | 修复后 |
|---|---|---|
| **SPA.status 永久 pending** | 3/3 pending (sample≥5 也无法推进) | sample=31 的 id=6 已 applied |
| **SPA → ai_decisions.applied_setting_changes** | 不存在 | 50 条回填 |
| **ai_worker 不读 applied SPA** | 完全不读 | `_load_spa_applied_block_sync` 注入 prompt |

## 不在 v199 范围 (留给后续版本)
- SPA `param_key` 不在白名单的 (settings/strategies 子集): 走人工 endpoint `/api/paper/adjustments/{id}/approve`
- SPA id=7/8 (sample_size=1): 阈值不满足, 正确不推进
- ai_decisions.applied_setting_changes 自动清理 (settled=1 后保留, 不裁剪)
- 反思 daemon (truly 真跑 LLM): v199 后评估

## 启动 worker (production)
```bash
cd /Users/claire/.cline/data/workspaces/chat/trident_v170/backend/src_python
python3 spa_apply_worker.py --daemon --interval=600
# env: SPA_APPLY_INTERVAL_S (默认 600s = 10min)
```

启动后 api_server 不需要重启 — `_load_spa_applied_block_sync` 是 lazy 加载, 但 ai_worker 已修改需重启服务。