// v203+: 分析过程 API + 类型 —— 后端 /api/analysis-process/* 的前端包装

import { apiGet } from './api'

// ─────── 类型 ───────

export type APGate = {
  main_asset: string | null
  kind: string | null
  timing: string | null
  strength: number | null
  verdict: string | null
  core_impact: string | null
  assets: string[] | string | null
  is_material: number | null
  // v181: 1-100 影响力打分（<20 一律排除,可交易候选 >=20）
  impact_1to100?: number | null
  // v181: 新闻发布时间距 now 的小时数(用于 12h 过期门可视化)
  news_age_hours?: number | null
  no_trade_reason?: string | null
  trade_gate_reason?: string | null
  [k: string]: unknown
}

export type APRawNews = Record<string, unknown> & {
  id: number
  source: string
  content: string
  timestamp?: number | string | null
  ts?: number | string | null
  status?: string | null
  published_at?: number | string | null
  gate_main_asset?: string | null
}

export type APDecision = Record<string, unknown> & {
  id: number
  news_id: number
  target_asset?: string | null
  market_category?: string | null
  suggested_action?: string | null
  sentiment_score?: number | null
  expected_horizon?: string | null
  forward_pnl?: number | null
  is_correct?: string | null
  settled?: number | null
  agent_model_id?: string | null
}

export type APReasoning = {
  decision_id: number
  reasoning: string
  reasoning_path_raw: string
  reasoning_segments: Array<{ key: string; value: string }>
  factor_ranking: unknown[]
  elasticity_window: Record<string, unknown>
  asset_reasoning: string
  verification: unknown
  hard_data_evidence: string
  invalidation_condition: string
  is_correct: string
  forward_pnl: number | null
  expected_horizon: string
}

export type APLlmCallMeta = {
  agent_model_id: string
  evidence_confidence: number | null
  evidence_action: string
  market_confirmation: string
  event_strength: string
  impact_magnitude: number | null
  attention_score: number | null
  event_phase: string
  timeframe_match: string
  trade_gate_reason: string
  no_trade_reason: string
}

export type APQuickSim = Record<string, unknown> & {
  id: number
  decision_id: number
  news_id: number
  asset: string
  action: string
  expected_horizon?: string | null
  entry_price?: number | null
  exit_price?: number | null
  pnl_pct?: number | null
  forward_pnl?: number | null
  is_correct?: string | null
  mfe?: number | null
  mae?: number | null
}

export type APTradeReflection = Record<string, unknown> & {
  id: number
  decision_id: number
  reflection?: string | null
  reflected_at?: number | string | null
  attribution_code?: number | null
  attribution_text?: string | null
}

export type APStructuredReflection = Record<string, unknown> & {
  id: number
  decision_id: number
  result_type?: string | null
  root_cause?: string | null
  lessons?: string | null
}

export type APAttributionReview = Record<string, unknown> & {
  id: number
  ref_id?: number | null
  trade_id?: number | null
  code?: number | null
  text?: string | null
  human_status?: string | null
}

export type APReviewInsight = Record<string, unknown> & {
  id: number
  rule: string
  asset: string | null
  scope?: string | null
  active?: number | null
  source_samples?: number | null
  origin?: string | null
  kind?: string | null
  counter_factual?: string | null
}

export type APExperienceEntry = Record<string, unknown> & {
  id: number
  kind: string
  title: string
  content: string
  applicable_assets?: string | null
  applicable_kinds?: string | null
  priority?: number | null
  weight?: number | null
  status?: string | null
  source_insight_id?: number | null
}

export type APCausalChain = Record<string, unknown> & {
  id: number
  name: string
  cause?: string | null
  effect?: string | null
  trigger_words?: string | null
  mechanism?: string | null
  precondition?: string | null
  invalidation?: string | null
  weight?: number | null
  enabled?: number | null
}

export type APNewsTradeReview = Record<string, unknown> & {
  news_id: number
  summary?: string | null
  summary_parsed?: Record<string, unknown>
}

export type APLifecycle = {
  news_id: number
  news: APRawNews | null
  gate: APGate | null
  decisions: APDecision[]
  primary_decision_id: number | null
  reasoning: APReasoning | null
  llm_call_meta: APLlmCallMeta | null
  quick_sim: APQuickSim[]
  paper_run: Record<string, unknown>[]
  trade_reflections: APTradeReflection[]
  structured_reflections: APStructuredReflection[]
  attribution_reviews: APAttributionReview[]
  news_trade_review: APNewsTradeReview | null
  review_insights: APReviewInsight[]
  experience_entries: APExperienceEntry[]
  causal_chain_links: APCausalChain[]
}

export type APListItem = {
  news_id: number
  ts: number | string | null
  source: string
  status: string
  content_preview: string
  gate_main_asset: string | null
  gate_kind: string | null
  gate_strength: number | null
  gate_verdict: string | null
  // v181: 影响力打分 / 时效
  gate_impact_1to100?: number | null
  news_age_hours?: number | null
  published_at: number | string | null
  decisions_total: number
  decisions_settled: number
  decisions_win: number
  latest_decision_id: number | null
  latest_action: string | null
  latest_asset: string | null
  latest_pnl: number | null
  latest_verdict: string | null
  latest_settled: boolean
  latest_market_category: string | null
  latest_entry_price: number | null
  latest_exit_price: number | null
  latest_max_price: number | null
  latest_min_price: number | null
  latest_entry_time: string | null
  latest_trade_gate_reason: string | null
  latest_no_trade_reason: string | null
}

// ─────── API ───────

export type RecentListResponse = { items: APListItem[]; count: number; limit: number }

export function fetchRecentAnalysisList(
  limit = 20,
  onlyWithDecisions = false,
): Promise<RecentListResponse> {
  const qs = new URLSearchParams()
  qs.set('limit', String(limit))
  if (onlyWithDecisions) qs.set('only_with_decisions', 'true')
  return apiGet<RecentListResponse>(`/analysis-process/recent?${qs.toString()}`)
}

export function fetchLifecycle(newsId: number): Promise<APLifecycle> {
  return apiGet<APLifecycle>(`/analysis-process/news/${newsId}`)
}

export function fetchHealthcheck(): Promise<{ ok: boolean; module: string; version: string }> {
  return apiGet<{ ok: boolean; module: string; version: string }>(
    `/analysis-process/healthcheck`,
  )
}

// ─────── UI 辅助 ───────

export function actionColor(action?: string | null): string {
  const a = (action || '').toUpperCase()
  if (a === 'BUY') return 'text-buy'
  if (a === 'SELL') return 'text-sell'
  return 'text-hold'
}

export function verdictColor(verdict?: string | null): string {
  const v = (verdict || '').toUpperCase()
  if (v === 'WIN' || v === 'CORRECT' || v === '1' || v === 'TRUE') return 'text-buy'
  if (v === 'LOSS' || v === 'INCORRECT' || v === '0' || v === 'FALSE') return 'text-sell'
  return 'text-muted-foreground'
}

export function fmtPct(p?: number | null, digits = 2): string {
  if (p === null || p === undefined) return '—'
  // DB 存的就是百分数（如 0.1625 = 0.1625%），不要再 ×100
  return `${p.toFixed(digits)}%`
}

export function fmtPctSigned(p?: number | null, digits = 2): string {
  if (p === null || p === undefined) return '—'
  const sign = p > 0 ? '+' : ''
  // DB 存的就是百分数（如 0.1625 = 0.1625%），不要再 ×100
  return `${sign}${p.toFixed(digits)}%`
}

export function fmtTs(ts?: number | string | null): string {
  if (ts === null || ts === undefined) return '—'
  let d: Date | null = null
  if (typeof ts === 'number') {
    d = new Date(ts < 1e12 ? ts * 1000 : ts)
  } else if (typeof ts === 'string') {
    if (/^\d+$/.test(ts)) {
      d = new Date(Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts))
    } else {
      d = new Date(ts)
    }
  }
  if (!d || isNaN(d.getTime())) return String(ts)
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  const ss = String(d.getUTCSeconds()).padStart(2, '0')
  const mo = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  return `${d.getUTCFullYear()}-${mo}-${dd} ${hh}:${mm}:${ss}`
}

export function sourceLabel(source?: string | null): string {
  const s = (source || '').toLowerCase()
  if (s.includes('jin10') || s.includes('金十')) return '金十'
  if (s.includes('eastmoney') || s.includes('东财')) return '东财'
  if (s.includes('techflow') || s.includes('深潮')) return '深潮'
  if (s.includes('blockbeats') || s.includes('律动')) return '律动'
  if (s.includes('financialjuice')) return 'FinancialJuice'
  if (s.includes('tree') || s.includes('telegram')) return 'TreeNews'
  if (s.includes('fred')) return 'FRED'
  if (s.includes('reuters')) return 'Reuters'
  if (s.includes('blockmedia')) return 'BlockMedia'
  return (source || '').slice(0, 12) || '—'
}