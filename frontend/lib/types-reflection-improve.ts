// v170 P0+：reflection-improve-panel 共享类型定义
//
// 从 reflection-improve-panel.tsx 抽出来供 reflection-improve-parts.tsx 复用，
// 减少主文件行数（668 → ~170），主组件保留编排 + 数据拉取 + KPI 列表。
//
// 约束（务必遵守）：
//   - 任何改 BuySellKpi / StrategyReflection 的 PR，必须同步 paper-trading 后端 schema。

/** /paper/adjustments 单条 */
export type Adjustment = {
  id: number
  asset: string
  action: string
  param_key: string
  direction: string
  magnitude: number
  sample_size: number
  avg_confidence: number | null
  evidence_summary: string
  first_suggested_at: string | null
  last_suggested_at: string | null
  status: string
  note: string
}

/** 时延分箱 — 入场距新闻发布时间 */
export type Bucket = Record<'<60s' | '60-300s' | '300-900s' | '900s+', number>

/** 即时涨跌后的胜率（KPI buy_pattern / sell_pattern） */
export type Pattern = {
  win_rate_after_up: number
  win_rate_after_down: number
  total_after_up: number
  total_after_down: number
  advice: string
}

/** 入场时延 vs 胜/负 触发模式（KPI win_trigger_pattern / loss_trigger_pattern） */
export type TriggerPattern = {
  buckets: Bucket
  median_delay_sec: number | null
  best_delay_sec: number | null
}

/** 单资产 × 动作的汇总统计 */
export type AssetActionStats = {
  asset: string
  action: string
  count: number
  win_count: number
  loss_count: number
  win_rate: number
  avg_pnl_pct: number
  median_pnl_pct: number
  best_pnl_pct: number
  worst_pnl_pct: number
  avg_mfe_pct: number | null
  avg_mae_pct: number | null
  avg_hold_minutes: number
  avg_score: number | null
  score_p20?: number
  score_p80?: number
}

/** /paper/buy-sell-kpi 响应 */
export type BuySellKpi = {
  days: number
  asset_filter: string | null
  total_trades: number
  overall: AssetActionStats | { count: number }
  per_asset_action: AssetActionStats[]
  by_event_strength: (AssetActionStats & { event_strength: string })[]
  buy_pattern: Pattern
  sell_pattern: Pattern
  win_trigger_pattern: TriggerPattern
  loss_trigger_pattern: TriggerPattern
  generated_at: string
}

/** v201 #1: 策略级反思的某条模式 */
export type StrategyPattern = {
  pattern: string
  supporting_data: string
  severity: 'low' | 'medium' | 'high'
}

/** v201 #1: 策略级反思的某条推荐参数调整 */
export type StrategyRecommendation = {
  type: 'param'
  key: string
  direction: 'up' | 'down' | 'on' | 'off'
  magnitude: number
  confidence: number
  rationale: string
}

/** /paper/strategy-reflections 单条 */
export type StrategyReflection = {
  id: number
  period_start: string
  period_end: string
  period_days: number
  sample_size: number
  llm_summary: string
  strategy_patterns: StrategyPattern[]
  recommendations: StrategyRecommendation[]
  affect_dimension: string
  evidence_summary: string
  overall_confidence: number
  status: 'pending' | 'approved' | 'rejected' | 'applied' | 'expired'
  admin_note: string
  created_at: string
  approved_at: string | null
  applied_strategy_version_id: number | null
}