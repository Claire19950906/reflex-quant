// v170 P0+：review-history-panel 共享类型定义
//
// 抽出来减少 review-history-panel.tsx 的行数（981 → ~700+）
// 主文件保留主组件，子组件（SignalGroup / TradeStatusBadge / TradeSparkline）
// 在 review-history-parts.tsx 复用本类型。

/** 参考信号：从快速评测已结算交易提炼的统计规律（AI 分析新新闻时参考） */
export type SignalItem = {
  label: string
  total: number
  wins: number
  losses: number
  winrate: number | null
  total_pnl: number
  summary: string
}

export type ReviewSignals = {
  overall: Omit<SignalItem, 'label'> & { total: number }
  assets: SignalItem[]
  directions: SignalItem[]
  phases: SignalItem[]
  strengths: SignalItem[]
  intervals: SignalItem[]
}

export type NewsTrade = {
  news_id: number
  source: string
  content: string
  news_time: string | null
  trade_count: number
  wins: number
  losses: number
  winrate: number | null
  total_pnl: number
  latest_exit_ts: number
  has_review: boolean
}

export type TradeRow = {
  id: number
  decision_id: number
  asset: string
  action: string
  score: number | null
  evidence_confidence: number | null
  gate_passed: number | null
  gate_reason: string
  entry_price: number | null
  exit_price: number | null
  entry_ts: number | null
  exit_ts: number | null
  pnl_pct: number | null
  pnl_usdt: number | null
  is_correct: string
  verdict: string
  reasoning: string
  expected_horizon: string
  event_phase: string
  event_strength: string
  interval_minutes: number | null
  /** v180 P0+：名义持仓（USDT），来自 ai_decisions.notional_usdt，可空 */
  notional_usdt?: number | null
  /** v180 P0+：待判定原因（统一口径 §5）；空串 = 已判定 */
  pending_reason?: string
}

export type ReviewSummary = {
  verdict?: string
  key_insight?: string
  detail?: string
  rule_check?: string
  failures?: Array<{ decision_id: number; reason_name: string; reason_text: string; confidence: number }>
  successes?: Array<{ decision_id: number; reason_name: string; reason_text: string; confidence: number }>
  aggregate?: { total: number; decided: number; wins: number; losses: number; winrate: number | null; total_pnl: number }
}

export type CrossSourceTrace = {
  cluster_size: number
  sources: Array<{
    id: number
    source: string
    first_seen_ts: string
    delay_seconds: number
    is_earliest: boolean
    is_current: boolean
    snippet: string
  }>
  primary_source: string | null
  primary_origin_info: string | null
}

export type NewsTradeDetail = {
  news_id: number
  source: string
  content: string
  news_time: string | null
  trades: TradeRow[]
  aggregate: { total: number; decided: number; wins: number; losses: number; winrate: number | null; total_pnl: number }
  review: ReviewSummary | null
  review_generated_at: string | null
  /** v188 P0+：跨源溯源（同 fingerprint 的全网首次采集时间序列） */
  cross_trace?: CrossSourceTrace | null
}
