# 实验：主通道短输出 42% —— 先分辨"截断"还是"本来就短"

日期：2026-11-20　状态：待执行（不改任何生产配置）

## 现象

`analysis_yield --days 7` 的通道质量表：

| 通道 | 调用 | 短输出 | 空输出 | 平均输出 | 判定 |
|---|---|---|---|---|---|
| minimax/MiniMax-M3（账户1） | 2852 | 41.6% | 31.2% | 742 tok | 不建议用于主分析 |
| minimax2/MiniMax-M3（账户2） | 433 | 14.7% | 13.9% | 1063 tok | 正常 |

同一模型、同一 prompt，账户1 的短输出是账户2 的 2.8 倍。7 天 17.9M 输入 token 里有相当一部分
换回了半截 JSON。

## 先纠正一个我自己的误判

我一开始以为账户1 被 `config.py` 的 `max_tokens=2500` 卡住。查了调用链后确认：

- 正式分析路径的 payload 是 `**extra_body` 后面再写 `"max_tokens": int(os.getenv("MINIMAX_MAX_TOKENS", "4000"))`，
  **extra_body 里的值会被覆盖**；
- 两个账户走的是同一段代码、同一个 4000 上限。

所以"账户1 卡 2500、账户2 有 6000"不成立，**改 max_tokens 大概率治不了这个 42%**。
先别动它。

## 已用现有数据排除"截断"（2026-11-20 20:0x）

`ai_call_stats` 7 天，`purpose='main'`：

| 通道 | 调用 | 输出 ≥3900 token（近顶） | <500 token | 平均 | 最大 |
|---|---|---|---|---|---|
| minimax/（账户1） | 2873 | **1** | 1192 | 741 | 4000 |
| minimax2/（账户2） | 433 | 0 | 64 | 1055 | 2652 |

账户1 平均 741 就停了，**2873 次里只有 1 次接近 4000 上限** —— 这不是截断，
是模型在账户1 上直接给了"压缩版"回答。所以抬 `max_tokens` 没有用（下面 A/B 方案作废）。
真正要选的是：把账户1 降权（让账户2 多承担），还是查账户1 的额度/端点差异。

## 真正要分辨的两件事

短输出有两种，处理方式完全相反：

1. **截断**：`finish_reason=length`、输出停在半个 JSON、`tokens_out` 贴近 4000。
   → 该抬上限/抬超时，或缩短 prompt。
2. **本来就短**：`finish_reason=stop`、JSON 合法但只有两三段、`tokens_out` 700 左右。
   → 抬上限没用，是模型在账户1 的端点上"偷懒"，要查端点差异或降权。

现在 `ai_call_stats` 只有 `tokens_out`，没有 `finish_reason`，只能猜。**这就是缺口。**

## 处置方案（二选一，都需要你点头）

### A. 补一个字段，先看清楚（推荐，零风险）

给 `ai_call_stats` 加 `finish_reason TEXT`，在 `chat()` 记一次响应时写入。
跑 24 小时后：

```sql
-- 账户1 的短输出里，截断占多少
SELECT finish_reason, COUNT(*) n,
       ROUND(AVG(tokens_out)) avg_out,
       SUM(tokens_out>=3900) near_cap
FROM ai_call_stats
WHERE ts >= strftime('%s','now') - 86400
  AND purpose='main' AND model_id LIKE 'minimax/%'
GROUP BY 1;
```

判据：`near_cap` 占比 ≥ 60% → 是截断，抬 `MINIMAX_MAX_TOKENS` 到 6000 并同步抬超时；
`near_cap` ≤ 20% 且 `finish_reason=stop` 为主 → 是端点偷懒，改的是账户1 的权重（2 → 1），
让账户2 承担更多流量（需同时观察门卫是否被挤压）。

### B. 抬 max_tokens（已作废）

数据已证明不是截断（近顶 1/2873），抬到 6000 只会让偶发长回答更慢，不会减少短输出。
**不做。**

### C. 账户1 降权（下一步的可选项）

`MINIMAX_MAIN_WEIGHT 2→1` + `MINIMAX2_WEIGHT 1→2`，让产出完整的账户2 承担 2/3 流量。
代价：账户2 与门卫共用，门卫可能被挤压（有 fail-open + 冻结回放兜底，属延迟不丢数据）。
判据：24 小时后主通道短输出应降到 ~15%，同时盯 gate 的 402/空输出是否上升。
这条路要先确认账户2 的配额余量，**不建议不看余量就切**。

## 不建议做的事

- 不要为了让 42% 好看去下调 `analysis_yield` 的短输出阈值 —— 那是把尺子改短。
- 不要在没分辨清楚前同时改 max_tokens 和通道权重 —— 两个变量一起动，事后无法归因。
