// ─── GUB 能力层 API 封装 ─────────────────────────────────────────────────────
//
// 背景 (2026-10-21 审计):
//   backend/src_python/bridge_server.py 暴露 64 个端点, 但 lib/api.ts 的
//   FALLBACK_PATHS 只接了 39 个 —— 25 个端点后端已经在算真实数据,
//   前端一个地方都没调用。本文件把「后端有货、前端没接」的端点补齐。
//
//   设计约定 (对齐 lib/api.ts 现有风格):
//   · 全部走 apiGet —— 自带 no-store + 桥接 fallback + BRIDGE_FALLBACK throw
//   · 类型严格对齐后端 JSON, 不做「any 大杂烩」
//   · 查询参数用 URLSearchParams 拼, 不手写字符串插值
//
// 端点分为四组 (见各节注释):
//   A. 技术信号与形态   /tech/signals /tech/structure /tech/anomaly*
//   B. 市场基本面       /v291/market-regime /v291/funding-rate /v291/orderbook-depth
//   C. 归因与因果准确性 /v291/event-move-attribution /v189/causal/accuracy-stats
//                        /review/causal-graph /v291/causal-chains/feedback
//
// 写操作端点 (score-batch / score-single / evaluate-now / feedback-backfill /
// event-move-attribution-rebuild) 统一放底部 MANUAL_* 一节, 不自动调用 ——
// 它们会改库或触发重算, 必须由用户在 UI 上显式点击。

import { apiGet, apiSend } from './api'

// ─── A. 技术信号与形态 ───────────────────────────────────────────────────────

/** 单条技术信号的融合构成 (后端 tech_detector.py 的 signals 数组) */
export type TechSignalComponent = {
  k: string
  v: number | Record<string, unknown>
  /** 该分项在 score 里的权重 */
  w: number
}

export type TechSignal = {
  ts: number
  close: number
  /** 触发形态: reversal / support / resistance / breakout / volume ... */
  trigger_type: string
  /** 融合评分。注意: 历史上曾恒为 0.95 (无区分度), 已如实透传不做美化 */
  score: number
  /** 是否用了未来数据 (reversal 类历史上恒为 1 = 存在泄漏, 前端据此打标) */
  uses_future: number | boolean
  pct_1d: number
  pct_3d: number
  vol_ratio: number
  support_level: number | null
  signals: TechSignalComponent[]
  /** 同时命中的形态类型 (如 ['reversal','support']) */
  merged_types: string[]
  bars_available: number
  data_max_ts: number
}

export type TechSignalsResponse = {
  ok: boolean
  asset: string
  signals: TechSignal[]
  [k: string]: unknown
}

/** K 线形态识别结果 (后端 price_structure.py), name_zh 已是中文 */
export type TechStructure = {
  ts: number
  /** 英文形态名: doji / double_top / bullish_engulfing ... */
  name: string
  /** 中文形态名: 十字星 / 双顶 / 看涨吞没 ... */
  name_zh: string
  open: number
  high: number
  low: number
  close: number
  height_pct: number
  info: Record<string, number | string>
  uses_future: boolean
}

export type TechStructureResponse = {
  ok: boolean
  asset: string
  structures: TechStructure[]
  [k: string]: unknown
}

/** 历史异动记录 (anomaly_history 表) —— 含形态/RSI/MACD 证据链 */
export type AnomalyRecord = {
  ts: number
  kind: string
  close: number
  pattern: string
  /** 中文证据列表: ['价格形态','RSI极值反转'] */
  evidence: string[]
  rsi: number
  macd_hist: number
  vol_ratio: number
  score: number
  scanned_at: number
}

export type AnomalyHistoryResponse = {
  ok: boolean
  asset: string
  interval: string
  anomalies: AnomalyRecord[]
  [k: string]: unknown
}

/** 实时异动扫描 —— 噪声已被过滤, 返回的是「通过样本质量门」的异动 */
export type AnomalyScanResponse = {
  ok: boolean
  asset: string
  interval: string
  anomalies: AnomalyRecord[]
  n: number
  bars_used: number
  raw_patterns: number
  noise_filtered: number
  density_pct: number
  thresholds: {
    rsi_os: number
    rsi_ob: number
    vol_gate: number
    adaptive: boolean
  }
  persisted: number
  data_source: string
  data_max_ts: number
}

/** 行情数据是否新鲜 —— 面板顶部显示, 避免拿过期数据下结论 */
export type HealthResponse = {
  ok: boolean
  status?: string
  assets?: number
  news?: number
  max_ts?: number
  [k: string]: unknown
}

// 后端 /api/tech/signals|structure 的形参是 symbol / days，不是 asset / limit。
// 原先这里发 asset+limit，后端全部忽略 → 永远返回默认 symbol=MU、days=90 的数据，
// 换资产/换回看天数都无效。这里对齐后端契约。
export function fetchTechSignals(asset: string, days?: number): Promise<TechSignalsResponse> {
  const q = new URLSearchParams({ symbol: asset })
  if (days != null) q.set('days', String(days))
  return apiGet<TechSignalsResponse>(`/tech/signals?${q}`)
}

export function fetchTechStructure(asset: string, days?: number): Promise<TechStructureResponse> {
  const q = new URLSearchParams({ symbol: asset })
  if (days != null) q.set('days', String(days))
  return apiGet<TechStructureResponse>(`/tech/structure?${q}`)
}

export function fetchAnomalyHistory(asset: string, interval?: string, limit?: number): Promise<AnomalyHistoryResponse> {
  const q = new URLSearchParams({ asset })
  if (interval) q.set('interval', interval)
  if (limit != null) q.set('limit', String(limit))
  return apiGet<AnomalyHistoryResponse>(`/tech/anomaly-history?${q}`)
}

export function fetchAnomalyScan(asset: string, interval?: string): Promise<AnomalyScanResponse> {
  const q = new URLSearchParams({ asset })
  if (interval) q.set('interval', interval)
  return apiGet<AnomalyScanResponse>(`/tech/anomaly?${q}`)
}

// ─── B. 市场基本面 ──────────────────────────────────────────────────────────

/**
 * COT 仓位状态 —— 对应需求 14「识别市场基本面」。
 *
 * 后端从 cftc_cot_data (2028 行) 按大型投机商净持仓的历史分位推出:
 *   large_spec_long  大型投机商净多头 → 集体看多 / 潜在逼空
 *   large_spec_short 大型投机商净空头 → 集体撤资 / 踩踏风险
 *   spec_extreme     净持仓打到历史极值 (percentile 0 或 100)
 * intensity: extreme / strong / moderate / mild
 *
 * ⚠️ 注意: asset 列表来自 COT 品种表 (EUR/NG/NQ/AUD...),
 *    与 13 资产交易清单不重叠, 是「跨资产外部资金背景」而非持仓标的本身。
 */
/** 后端 v291_market_regime 支持的 regime 枚举 (与 components/v291/market-regime-panel.tsx 保持一致) */
export type RegimeKind =
  | 'leverage_buildup'
  | 'deleveraging'
  | 'large_spec_long'
  | 'large_spec_short'
  | 'commercial_hedging'
  | 'risk_off_rotation'
  | 'spec_extreme'

export type MarketRegime = {
  /** spec_extreme / 跨资产类 regime 可能不带单一 asset (而是 assets_crypto/assets_safe) */
  asset?: string
  regime: RegimeKind
  report_date: string
  /** 未平仓量周变化 % */
  oi_chg_pct?: number
  /** 大型投机商净持仓占未平仓量百分比 */
  spec_net_pct?: number
  spec_delta_pct?: number
  comm_delta_pct?: number
  comm_now_pct?: number
  /** 历史分位 0~100, 0=史上最空 */
  percentile?: number
  direction?: 'increasing' | 'decreasing'
  /** 仅 spec_extreme 类型带 */
  side?: 'long' | 'short'
  crypto_oi_chg_pct?: number
  safe_oi_chg_pct?: number
  assets_crypto?: string[]
  assets_safe?: string[]
  intensity: 'strong' | 'moderate' | 'extreme'
  score: number
}

export type RegimeAssetSummary = {
  report_date: string
  open_interest: number
  oi_chg_pct: number
  spec_net_pct: number
  spec_delta_pct: number
}

export type MarketRegimeResponse = {
  ok: boolean
  n_regimes: number
  n_assets: number
  regime_counts: Record<string, number>
  regimes: MarketRegime[]
  by_asset: Record<string, RegimeAssetSummary>
  since_date: string
}

export type FundingRatePoint = {
  ts: number
  rate: number
  [k: string]: unknown
}

export type FundingRateResponse = {
  ok: boolean
  symbol: string
  rates: FundingRatePoint[]
  [k: string]: unknown
}

export type OrderBookDepthResponse = {
  ok: boolean
  symbol: string
  bids: number[][]
  asks: number[][]
  [k: string]: unknown
}

export function fetchMarketRegime(days?: number): Promise<MarketRegimeResponse> {
  const q = new URLSearchParams()
  if (days != null) q.set('days', String(days))
  const suffix = q.toString() ? `?${q}` : ''
  return apiGet<MarketRegimeResponse>(`/v291/market-regime${suffix}`)
}

export function fetchFundingRate(symbol?: string): Promise<FundingRateResponse> {
  const q = new URLSearchParams()
  if (symbol) q.set('symbol', symbol)
  const suffix = q.toString() ? `?${q}` : ''
  return apiGet<FundingRateResponse>(`/v291/funding-rate${suffix}`)
}

export function fetchOrderbookDepth(symbol?: string, refresh?: boolean): Promise<OrderBookDepthResponse> {
  const q = new URLSearchParams()
  if (symbol) q.set('symbol', symbol)
  if (refresh) q.set('refresh', 'true')
  const suffix = q.toString() ? `?${q}` : ''
  return apiGet<OrderBookDepthResponse>(`/v291/orderbook-depth${suffix}`)
}

export function fetchHealth(): Promise<HealthResponse> {
  return apiGet<HealthResponse>('/health')
}

// ─── C. 归因与因果准确性 ────────────────────────────────────────────────────

/**
 * 事件→行情 归因明细 (event_move_attribution 表 392 行)。
 *
 * ⚠️ 数据质量警告 (2026-10-21 实测, 前端必须如实呈现, 不要粉饰):
 *   · 277/392 (70.6%) match=spillover     —— 同组资产互推, 不是新闻驱动
 *   · 82/392  (20.9%) match=macro_fallback —— 无新闻时按涨幅硬凑
 *   · 36/392  (9.2%)  lag 为负 + 'retro'  —— 新闻滞后行情 100+ 小时, 属事后拟合
 *   · 仅 65/392 (16.6%) 是真正的「新闻在前、行情在后」
 *
 * 因此 isCausalMatch / isRetroFit 是本文件的核心判定 ——
 * 引用该数据的面板应按这两位过滤, 否则会把伪因果当真相源。
 */
export type AttributionRow = {
  id: number
  move_id: number
  news_event_id: number
  asset: string
  /** 新闻 ts - 行情 ts。正 = 新闻在前(因果); 负 = 行情在前(事后拟合) */
  time_lag_sec: number
  algo_score: number
  /** 匹配说明, 形如 'match=macro; lag=-4176s; retro' */
  algo_reasons: string
  move_ts: number
  move_pct: number
  move_kind: string
  duration_min: number
  ev_source: string
  ev_action: string
  ev_ts: number
  ev_news_id: number | null
}

export type AttributionResponse = {
  ok: boolean
  n: number
  rows_total: number
  items: AttributionRow[]
  [k: string]: unknown
}

export type CausalChainAccuracy = {
  chain_id: string
  asset: string
  trigger_pattern: string
  n_steps: number
  total_lag_min: number
  confidence: number
  hit_count: number
  miss_count: number
  /** 无样本时后端返回 null —— 不是 0, 前端勿写成 '0%' */
  hit_rate: number | null
  source: string
  last_verified?: string | number | null
  [k: string]: unknown
}

export type CausalAccuracyResponse = {
  ok: boolean
  n_chains: number
  assets: string[]
  trigger_patterns: string[]
  chains: CausalChainAccuracy[]
  /** 空对象 —— 因果图谱的 origin 归因能力尚未落地 */
  origin_breakdown_chain: Record<string, number>
  origin_breakdown_truth: Record<string, number>
  trust_breakdown: { trusted: number; pending: number }
  generated_at: number
  _aliased_from?: string
  [k: string]: unknown
}

export type CausalGraphNode = {
  id: number
  label: string
  type: string
  ts: number
  algo_score?: number
}

export type CausalGraphEdge = {
  from: number
  to: number
  label?: string
  weight?: number
  [k: string]: unknown
}

export type CausalGraphResponse = {
  ok: boolean
  asset: string
  hours: number
  nodes: CausalGraphNode[]
  edges: CausalGraphEdge[]
  [k: string]: unknown
}

/** 因果链反馈闭环 —— 标出哪些链的实际命中率与宣称置信度不符 */
export type ChainFeedback = {
  chain_id: string
  asset: string
  expected_direction: string
  magnitude_lo: number
  magnitude_hi: number
  stated_confidence: number
  lifetime_hit: number
  lifetime_miss: number
  window_days: number
  outcomes_n: number
  outcomes_hit: number
  outcomes_miss: number
  outcomes_neutral: number
  outcomes_pending: number
  real_hit_rate: number | null
  /** real_hit_rate - stated_confidence, null = 样本不足 */
  calibration_gap: number | null
  /** true = 宣称置信度与实测显著背离, 应重新校准 */
  needs_recalibration: boolean
  [k: string]: unknown
}

export type ChainFeedbackResponse = {
  ok: boolean
  window_days: number
  chains: ChainFeedback[]
  [k: string]: unknown
}

export function fetchAttribution(params?: {
  asset?: string
  limit?: number
}): Promise<AttributionResponse> {
  const q = new URLSearchParams()
  if (params?.asset) q.set('asset', params.asset)
  if (params?.limit != null) q.set('limit', String(params.limit))
  const suffix = q.toString() ? `?${q}` : ''
  return apiGet<AttributionResponse>(`/v291/event-move-attribution${suffix}`)
}

export function fetchCausalAccuracy(): Promise<CausalAccuracyResponse> {
  return apiGet<CausalAccuracyResponse>('/v189/causal/accuracy-stats')
}

export function fetchCausalGraph(params?: { asset?: string; hours?: number }): Promise<CausalGraphResponse> {
  const q = new URLSearchParams()
  if (params?.asset) q.set('asset', params.asset)
  if (params?.hours != null) q.set('hours', String(params.hours))
  const suffix = q.toString() ? `?${q}` : ''
  return apiGet<CausalGraphResponse>(`/review/causal-graph${suffix}`)
}

export function fetchChainFeedback(days?: number): Promise<ChainFeedbackResponse> {
  const q = new URLSearchParams()
  if (days != null) q.set('days', String(days))
  const suffix = q.toString() ? `?${q}` : ''
  return apiGet<ChainFeedbackResponse>(`/v291/causal-chains/feedback${suffix}`)
}

// ─── 判定辅助: 让前端如实反映数据质量, 而不是把伪因果当真 ─────────────────

/**
 * 这条归因是否「新闻在前、行情在后」的真正因果匹配。
 *
 * 判据 (对应后端 calc_event_move_attribution.py 的 match 类型):
 *   · ev_news_id 非空 且 time_lag_sec >= 0 → 真因果
 *   · time_lag_sec < 0 → retro, 事后拟合, 排除
 *   · match=spillover → 同组资产互推, 非新闻驱动, 排除
 *   · match=macro_fallback → 无新闻硬凑, 排除
 */
export function isCausalMatch(row: AttributionRow): boolean {
  if (row.ev_news_id == null) return false
  if (row.time_lag_sec < 0) return false
  if (row.algo_reasons.includes('spillover')) return false
  if (row.algo_reasons.includes('macro_fallback')) return false
  return true
}

/** 是否为事后拟合 (新闻滞后于行情) —— 调用方应显式提示, 不要静默展示 */
export function isRetroFit(row: AttributionRow): boolean {
  return row.time_lag_sec < 0 || row.algo_reasons.includes('retro')
}

/** 归因质量分层: causal 真因果 / retro 事后拟合 / derived 推导(非新闻直接驱动) */
export type AttributionTier = 'causal' | 'retro' | 'derived'
export function attributionTier(row: AttributionRow): AttributionTier {
  if (isRetroFit(row)) return 'retro'
  if (isCausalMatch(row)) return 'causal'
  return 'derived'
}

/**
 * 因果链的「过度自信」幅度 = 宣称置信度 - 实测命中率。
 * 样本 < 3 时返回 null (样本不足, 不能下结论)。
 */
export function chainOverconfidence(c: ChainFeedback): number | null {
  if (c.outcomes_n < 3 || c.real_hit_rate == null) return null
  return c.stated_confidence - c.real_hit_rate
}

/**
 * 归因质量汇总 —— 给面板顶部一行摘要用, 让用户先看到真实成色
 * (而不是打开页面误以为 392 条都是真因果)。
 */
export function summarizeAttribution(rows: AttributionRow[]): {
  total: number
  causal: number
  retro: number
  derived: number
  causalPct: number
} {
  let causal = 0
  let retro = 0
  for (const r of rows) {
    const t = attributionTier(r)
    if (t === 'causal') causal++
    else if (t === 'retro') retro++
  }
  const total = rows.length
  return {
    total,
    causal,
    retro,
    derived: total - causal - retro,
    causalPct: total ? (causal / total) * 100 : 0,
  }
}

// ─── MANUAL: 写操作 ─────────────────────────────────────────────────────────
//
// 以下端点会改库或触发重算, 一律不自动调用, 必须由用户在 UI 上显式点击。

/** 立即跑一轮因果链评分 (写 causal_chain_outcomes) */
export function scoreChainsBatch(body?: unknown): Promise<unknown> {
  return apiSend('/v291/causal-chains/score-batch', 'POST', body ?? {})
}

/** 单条决策的因果链 hook 验证 */
export function scoreChainSingle(body?: unknown): Promise<unknown> {
  return apiSend('/v291/causal-chains/score-single', 'POST', body ?? {})
}

/** 立即评估因果链 outcome */
export function evaluateChainsNow(body?: unknown): Promise<unknown> {
  return apiSend('/v291/causal-chains/evaluate-now', 'POST', body ?? {})
}

/** 回填反馈闭环历史 */
export function backfillChainFeedback(body?: unknown): Promise<unknown> {
  return apiSend('/v291/causal-chains/feedback/backfill', 'POST', body ?? {})
}

/** 重建事件归因表 (会覆盖 event_move_attribution) */
export function rebuildAttribution(body?: unknown): Promise<unknown> {
  return apiSend('/v291/event-move-attribution/rebuild', 'POST', body ?? {})
}