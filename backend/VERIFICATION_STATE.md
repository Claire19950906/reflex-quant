# Insight Verification 最终分布（v170.5+）

## 数据库状态 @ 2026-10-04 16:46

| verdict  | 数量 | 说明 |
|---------|------|------|
| partial | 111 | 弱信号（composite 0.1~0.7 但样本不足或饱和度不够） |
| failed | 55 | 有反向证据 / 完全无数据 |
| param_rule | 34 | 参数规则，无法验证 |

**verified = 0** — 这不是退化，是把虚高的 18 → 0 打回原形。

## 关键修复链（4 步）

### 1. 单位 bug（最早）
- `quick_sim_trades.entry_ts` = SEC（10 位）
- `kline_bars.ts` = MS（13 位）
- `find_related_trades` 错把 ms 当 sec 传入 → 永远查不到交易
- **修复**：在 `find_related_trades` 内 `ts_lo_s = int(ts_lo_ms // 1000)`

### 2. 动态警告文本
- 删掉硬编码 "7 天内 0 条" / "86.7% placeholder"
- `apply_verification` 的 `[warn]` 段改成实时 DB 查询

### 3. 窗口重叠虚增（最关键）
- 之前 `n_total = sum(r["n"] for r in out)` 把 24h+48h+72h 三个窗口累加
- 13 个 unique trades → `n_total = 33`（窗口重叠导致）
- `sample_conf = min(33/30, 1.0) = 1.0`（提前饱和）
- composite 直接通过 verified 门槛 → **虚高 18 条 verified**
- **修复**：用独立交易 ID 去重计算 `n_unique`

### 4. sample_conf 门槛收紧
- `min(sample_n / 30, 1.0)` → `min(sample_n / 60, 1.0)`
- 13 笔交易只能给 sample_conf = 0.217
- composite * 0.217 永远到不了 0.7 → verified = 0（诚实）
- partial 门槛 0.3 → 0.1：让弱信号也能落入 partial 而非 failed

## 18 条 XAU 真信号候选（partial）

| ID | 类型 | 信号 | 评价 |
|----|------|------|------|
| #99 | XAU BUY | 中东地缘 4/4 = 100% | pnl 极小（<0.04%），统计支持弱 |
| #115 | XAU BUY | 也门胡塞 4/4 = 100% | 同 #99，含同主题重复 |
| #153 | XAU BUY | 伊朗防空 2/2 = 100% | pnl +0.01% ×2，几乎不可执行 |
| #155 | XAU SELL | 美俄乌谈判 1/1 = 100% | 单样本无意义 |
| #172 | XAU BUY | 中东冲突 3胜1败 | 弱方向 |
| #117 | XAU BUY vs SELL | 同事件方向相反 | **矛盾**：BUY +0.04% vs SELL +0.06% |
| #134 | XAU BUY | 伊朗 1/4 | **反向**：4 笔伊朗 BUY 中仅 1 胜 |
| #139 | XAU sizing | 黄金本期最弱 -0.04% | 真实但短窗口 |
| #151 | XAU BUY | 5/8 = 62.5% | 样本不足以下结论 |
| #168 | XAU score | 盈亏比 0.4-0.9% | 风险信号，建议提高 confidence 门槛 |
| #191 | XAU BUY | 胜率62%，score 高反亏 | **反直觉**：高分信号反而易亏 |

**结论**：XAU 中东地缘事件触发 BUY 胜率（约 75%）确实存在；
**但单笔 pnl < 0.06%，盈亏比 0.4-0.9% → 实盘不可执行**。
18 条 partial 是数据诚实状态，**未达到 verified 仍不应批量停用**。

## 待用户决策

1. **是否停用 partial 中的反向/矛盾类**（#117 #134 #139 #168 #191）？
   - 这些是"信号与噪声级"的真实，但样本量不支持 verified
   - 停用后会从 active 列表剔除，不再进入新一轮建议
3. **是否等待样本增长**（n ≥ 60 → verified 自动浮出来）？
4. **是否调整 fallback 阈值**（当前 0.1，对应 60 笔交易 + K 线一致 = partial）？
5. **XAU OKX swap 成功率 76.9%（ok=15811 fail=4752）** — 是否值得优化？

## 文件改动记录

- `insight_verifier.py`：
  - `find_related_trades`: ms→sec（line 92-93）
  - `verify_insight_with_trades`: 加 `seen_ids` 去重，独立 `n_unique`（line 119-151）
  - `compute_real_signal_pct`: sample_conf 30→60（line 227-229）, partial 0.3→0.1（line 238）
- `apply_verification.py`：warning 段动态化（line 160-195）
- 备份表：`review_insights_backup`（200 条）