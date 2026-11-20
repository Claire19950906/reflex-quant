// Crowd Pulse — 二级市场人群画像 / 权威度 / 月榜（v316）
//
// 数据全部来自真实抓取（百度热搜/同花顺/CoinGecko/Google News），
// 每条原始条目带 url 可回溯；解析失败由后端如实返回 errors，前端不编造。

import { apiGet } from './api'

// ─────────────────── v318 话题 × 观点 ───────────────────
//
// 与 ranking（指标表）分工：
//   ranking = 谁的热度/权威度最高
//   topics  = 人话：关注什么话题 + 持什么观点 + 具体说了什么（原话 + url）
export type CrowdTopicSort = 'heat' | 'authority' | 'momentum' | 'latest' | 'bullish'

export type CrowdTopicKind = '' | 'topic' | 'asset'

export type CrowdTopicItem = {
  title: string
  url: string
  source: string
  media_channel: string
  heat: number
  published_at: string
  collected_at: string
}

export type CrowdTopicSentiment = {
  bull: number
  bear: number
  neutral: number
  total: number
  stance: '看多主导' | '看空主导' | '多空分歧' | '无明确方向' | string
  bull_ratio: number
  bear_ratio: number
  net: number
}

export type CrowdTopic = {
  rank_no: number
  topic_key: string
  display_name: string
  topic_kind: string
  heat_total: number
  authority_score: number
  source_count: number
  days_active: number
  mentions: number
  momentum: number
  media_weight: number
  last_seen: string
  platforms: string[]
  sources: string[]
  platform_audiences: string[]
  markets: string[]
  asset_classes: string[]
  themes: string[]
  explain: string
  sentiment: CrowdTopicSentiment
  items: CrowdTopicItem[]
  item_total: number
}

export type CrowdTopicsResp = {
  month: string
  window: { start: string; end: string; days: number; label: string }
  sort: string
  kind: string
  count: number
  topics: CrowdTopic[]
  summary: {
    mentions: number
    topic_count: number
    platform_count: number
    platforms: string[]
    sentiment: CrowdTopicSentiment
    top_markets: { name: string; mentions: number }[]
    top_themes: { name: string; mentions: number }[]
    platform_mentions: { platform: string; audience: string; mentions: number }[]
  }
  note: string
}

/** 话题 × 观点（days>0 = 最近 N 天；kind: topic=在讨论什么 / asset=在买什么） */
export function fetchCrowdTopics(
  opts: {
    month?: string
    days?: number
    limit?: number
    sort?: CrowdTopicSort
    kind?: CrowdTopicKind
    item_limit?: number
  } = {},
): Promise<CrowdTopicsResp> {
  const q = new URLSearchParams()
  if (opts.month) q.set('month', opts.month)
  if (opts.days) q.set('days', String(opts.days))
  if (opts.limit) q.set('limit', String(opts.limit))
  if (opts.sort) q.set('sort', opts.sort)
  if (opts.kind) q.set('kind', opts.kind)
  if (opts.item_limit) q.set('item_limit', String(opts.item_limit))
  const qs = q.toString()
  return apiGet<CrowdTopicsResp>(`/crowd/topics${qs ? `?${qs}` : ''}`)
}

export type CrowdRankingSort = 'authority' | 'heat' | 'momentum'

export type CrowdRankingItem = {
  month: string
  topic_key: string
  display_name: string
  topic_kind: string
  rank_no: number
  authority_score: number
  heat_total: number
  source_count: number
  days_active: number
  momentum: number
  media_weight: number
  sources: string[]
  explain: string
  asset_class: string
  themes: string[]
  market: string
  ranked_by?: string
}

export type CrowdRankingResp = {
  month: string
  sort: string
  count: number
  ranking: CrowdRankingItem[]
}

export type CrowdSegment = {
  month: string
  dimension: string
  segment: string
  label: string
  item_count: number
  heat_total: number
  share: number
  rank_no: number
  sample_sources: string[]
}

export type CrowdAudienceResp = {
  month: string
  dimension_labels: Record<string, string>
  dimensions: Record<string, CrowdSegment[]>
}

export type CrowdSourceAuth = {
  month: string
  source: string
  media_channel: string
  items: number
  topics: number
  cross_verified: number
  authority_score: number
}

export type CrowdItem = {
  source: string
  item_kind: string
  title: string
  url: string
  media_channel: string
  heat: number
  rank_no: number
  published_at: string
  collected_at: string
  topic_key: string
}

/** 月度榜单（sort: authority 跨平台共识优先 / heat 各源最热 / momentum 正在升温） */
export function fetchCrowdRanking(
  opts: { month?: string; limit?: number; kind?: string; sort?: CrowdRankingSort } = {},
): Promise<CrowdRankingResp> {
  const q = new URLSearchParams()
  if (opts.month) q.set('month', opts.month)
  if (opts.limit) q.set('limit', String(opts.limit))
  if (opts.kind) q.set('kind', opts.kind)
  if (opts.sort) q.set('sort', opts.sort)
  const qs = q.toString()
  return apiGet<CrowdRankingResp>(`/crowd/ranking${qs ? `?${qs}` : ''}`)
}

/** 人群画像（9 个维度；不传 dimension 返回全部维度） */
export function fetchCrowdAudience(
  opts: { month?: string; dimension?: string } = {},
): Promise<CrowdAudienceResp> {
  const q = new URLSearchParams()
  if (opts.month) q.set('month', opts.month)
  if (opts.dimension) q.set('dimension', opts.dimension)
  const qs = q.toString()
  return apiGet<CrowdAudienceResp>(`/crowd/audience${qs ? `?${qs}` : ''}`)
}

/** 各数据源权威度 */
export function fetchCrowdSources(month?: string): Promise<{ month: string; count: number; sources: CrowdSourceAuth[] }> {
  const q = month ? `?month=${encodeURIComponent(month)}` : ''
  return apiGet(`/crowd/sources${q}`)
}

/** 最近采集到的原始条目（可逐条回溯 url） */
export function fetchCrowdItems(
  opts: { limit?: number; source?: string } = {},
): Promise<{ count: number; items: CrowdItem[] }> {
  const q = new URLSearchParams()
  if (opts.limit) q.set('limit', String(opts.limit))
  if (opts.source) q.set('source', opts.source)
  const qs = q.toString()
  return apiGet(`/crowd/items${qs ? `?${qs}` : ''}`)
}

// ─────────────────── v317 注意力领先（attention lead） ───────────────────
// 检验「跨平台注意力是否先于有意义的行情移动」。
// 关键：后端会同时给出**反向对照**（注意力之前价格是否已动）与
// **干净子集**命中率 —— 表面命中率会被「媒体追价格」严重虚高，
// 前端必须把 verdict 与 clean_hit_rate 显示出来，不能只显示 hit_rate。

export type CrowdAttentionPrice = {
  symbol: string
  base_close: number | null
  has_move: boolean
  move_ts: number
  move_at: string
  move_pct: number
  lead_minutes: number | null
  max_abs_move_pct: number
  bars: number
  timeframe: string
  horizon_minutes: number
  move_pct_threshold: number
}

export type CrowdAttentionReverse = {
  symbol: string
  prior_move: boolean
  prior_move_ts: number
  prior_move_at: string
  prior_move_pct: number
  reverse_lead_minutes: number | null
  bars: number
}

export type CrowdAttentionItem = {
  source: string
  title: string
  url: string
  media_channel: string
  published_at: string
  heat: number
}

export type CrowdAttentionEvent = {
  symbol: string
  attention_ts: number
  attention_at: string
  platform_count: number
  source_count: number
  item_count: number
  platform_list: string[]
  items: CrowdAttentionItem[]
  price: CrowdAttentionPrice
  reverse: CrowdAttentionReverse
  explain: string
}

export type CrowdAttentionSummary = {
  events_total: number
  events_with_price: number
  events_with_move: number
  hit_rate: number | null
  avg_lead_minutes: number | null
  median_lead_minutes: number | null
  lead_leads_count: number
  lead_lags_count: number
  events_with_prior_move: number
  prior_move_ratio: number | null
  reverse_median_lead_minutes: number | null
  clean_events: number
  clean_hit_rate: number | null
  clean_avg_lead_minutes: number | null
  verdict: 'attention_leads_price' | 'attention_lags_price' | 'inconclusive' | 'no_data' | string
  symbols: string[]
  generated_at: string
}

export type CrowdAttentionResp = {
  params: {
    lookback_hours: number
    min_platforms: number
    move_pct: number
    horizon_minutes: number
    timeframe: string
  }
  summary: CrowdAttentionSummary
  events: CrowdAttentionEvent[]
  note: string
  pending?: CrowdPendingCandidate[]
  pending_note?: string
}

export type CrowdPendingCandidate = {
  symbol: string
  attention_ts: number
  attention_at: string
  platform_count: number
  platform_list: string[]
  item_count: number
  items: CrowdAttentionItem[]
  max_abs_move_pct: number
  move_pct_threshold: number
  horizon_minutes: number
  system_signal: { ts: number; suggested_action: string; target_asset: string; created_at: string } | null
  already_acted: boolean
  explain: string
}

/** 注意力领先性检验（含反向对照证伪装置） */
export function fetchCrowdAttentionLead(
  opts: {
    lookback_hours?: number
    min_platforms?: number
    move_pct?: number
    horizon_minutes?: number
    timeframe?: string
    limit?: number
  } = {},
): Promise<CrowdAttentionResp> {
  const q = new URLSearchParams()
  if (opts.lookback_hours) q.set('lookback_hours', String(opts.lookback_hours))
  if (opts.min_platforms) q.set('min_platforms', String(opts.min_platforms))
  if (opts.move_pct) q.set('move_pct', String(opts.move_pct))
  if (opts.horizon_minutes) q.set('horizon_minutes', String(opts.horizon_minutes))
  if (opts.timeframe) q.set('timeframe', opts.timeframe)
  if (opts.limit) q.set('limit', String(opts.limit))
  const qs = q.toString()
  return apiGet<CrowdAttentionResp>(`/crowd/attention-lead${qs ? `?${qs}` : ''}`)
}

/** 先行漏检候选：注意力已起、价格未动、系统未出手 */
export function fetchCrowdPendingAttention(
  opts: {
    lookback_hours?: number
    min_platforms?: number
    move_pct?: number
    horizon_minutes?: number
    timeframe?: string
  } = {},
): Promise<CrowdAttentionResp> {
  const q = new URLSearchParams()
  if (opts.lookback_hours) q.set('lookback_hours', String(opts.lookback_hours))
  if (opts.min_platforms) q.set('min_platforms', String(opts.min_platforms))
  if (opts.move_pct) q.set('move_pct', String(opts.move_pct))
  if (opts.horizon_minutes) q.set('horizon_minutes', String(opts.horizon_minutes))
  if (opts.timeframe) q.set('timeframe', opts.timeframe)
  const qs = q.toString()
  return apiGet<CrowdAttentionResp>(`/crowd/pending-attention${qs ? `?${qs}` : ''}`)
}

/**
 * 漏检信号页专用：先行候选（注意力已起、价格未动、系统未出手）。
 *
 * 与既有 missed_signal 的分工（别混淆）：
 *   · 既有 missed_signal = 事后判定（价格异动了，当时系统给的是 HOLD）
 *   · 这里是事前候选（价格还没动，注意力已经出现，系统也还没出手）
 * 两者互补，不改写既有 missed_signal 字段的语义。
 */
export function fetchMissedSignalAttentionCandidates(
  opts: { lookback_hours?: number; horizon_minutes?: number } = {},
): Promise<{
  ok: boolean
  count: number
  candidates: CrowdPendingCandidate[]
  note: string
  summary: CrowdAttentionSummary
  error?: string
}> {
  const q = new URLSearchParams()
  if (opts.lookback_hours) q.set('lookback_hours', String(opts.lookback_hours))
  if (opts.horizon_minutes) q.set('horizon_minutes', String(opts.horizon_minutes))
  const qs = q.toString()
  return apiGet(`/missed-signal/attention-candidates${qs ? `?${qs}` : ''}`)
}
