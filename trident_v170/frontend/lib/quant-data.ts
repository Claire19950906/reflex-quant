export type Signal = 'BUY' | 'SELL' | 'HOLD' | 'FAILED'

export type AnalysisStatus = 'PENDING' | 'PROCESSING' | 'DONE' | 'FAILED'

// v212 fix: 'OTHER' 被物理删除。
// 旧 OTHER 兜底 → 改成 'AGGREGATE'，语义是"已通过筛选、有市场影响，但未命中任何特定品种"。
// 筛层过不去的"无品类"新闻早在 gatekeeper 就被丢了；跑到这里的"聚合"事件是稀有但合法的状态。
export type MarketCategory = 'CRYPTO' | 'GOLD' | 'OIL' | 'MACRO' | 'STOCK' | 'HYNIX' | 'AGGREGATE' | 'ALL'

export type ApiEvent = {
  id: number
  news_id: number
  decision_id: number | null
  analysis_status: AnalysisStatus
  timestamp: string
  // 平台声称的发布时间（后端 _format_time 后；存量/无时间源为空串）
  published_at?: string
  // 全网原始溯源时间：同内容指纹在全网最早被采集的时刻（后端 _format_time 后；无则空串）
  first_seen_ts?: string
  // 全网溯源：AI 自由回答的自然语言文本（"全网最早发布时间+来源"）。
  // 无确切信息为 "无法确认" 或空串。
  net_origin_info?: string
  // 全网溯源：AI 直接给出的结构化最早时间（精确 "YYYY-MM-DD HH:MM:SS" 或 "AMBIGUOUS"；
  // 5 层校验不过/无信息则为空串）。有值才显示，且必须搭配 cross_net_source 展示来源。
  cross_net_first_seen_ts?: string
  // 全网溯源：cross_net_first_seen_ts 的依据来源（"XX社首发"/"候选1:…/候选2:…"；无则空串）。
  cross_net_source?: string
  ai_time: string
  source: string
  news_text: string
  action: Signal
  score: number
  reason: string
  market_category: MarketCategory
  target_asset: string
  parent_id: number | null
  child_count: number
  reasoning_path: string
  // 品种相关推理逻辑：AI 逐品种解释"为什么这条新闻关联该品种"（仅主行有值）
  asset_reasoning?: string
  vip_tag: string
  entry_price: number | null
  exit_price: number | null
  partial_exit_price?: number | null
  partial_exit_ratio?: number
  partial_pnl_pct?: number
  heat_score?: number | null
  impact_score?: number | null
  asset_impact?: number | null
  // 需求5 新增子模块输出：关注度得分(0-100) / 弹性窗参数(JSON串) / 原始数据戳 / 硬数据截图
  attention_score?: number | null
  elasticity_window?: string
  data_anchor_ts?: string
  hard_data_evidence?: string
  max_price: number | null
  min_price: number | null
  max_price_time: number
  min_price_time: number
  entry_time: string
  is_correct: string
  settled: number
  doubao_action: Signal
  doubao_reasoning: string
  cluster_size: number
  no_trade_reason?: string
  trade_gate_reason?: string
  direct_catalyst?: number | boolean
  event_strength?: string
  market_confirmation?: string
  invalidation_condition?: string
  expected_horizon?: string
  peer_reviews?: PeerReview[]
  server_emitted_at_ms?: number
  // 多品种：同 news 的副品种决策（reasoning 前缀 [副品种 标记，各自独立信号）
  sibling_decisions?: SiblingDecision[]
  // ── v172 增量：事件结构化输出层 ──
  /** 规则确定性抽出的事件原子列表 JSON（event_struct.extract_events） */
  event_struct?: string
  /** LLM 调用前拼好的 6 字段 JSON payload（meta/events/snapshot/relations/baseline/text_summary） */
  structured_input?: string
  /** LLM 输出 reasoning 与 structured_input 的反向引用比对（verdict/mentioned/missing/fabricated/check_at） */
  structured_check?: string
  /** v174 强制字段 — 从 dim_event_type 25 个 code 中选 1 */
  event_type_code?: string
  /** v174 强制字段 — reasoning 引用的快照距事件时间分钟差，文本："5"/"30"/"N/A" */
  snapshot_proximity?: string
}

// ─── v172 事件结构化输出层类型 ────────────────────────────────────

/** 规则确定性抽出的事件原子（对应 event_atom 表 + dim_event_type + dim_asset join） */
export type EventAtom = {
  id: number
  raw_news_id: number
  event_type_id: number
  event_type_code: string
  event_type_zh: string
  event_category: string
  asset_id: number | null
  asset_code: string
  event_time: number
  event_time_iso?: string
  event_time_confidence: number | null
  event_time_source: string
  magnitude_value: number | null
  magnitude_unit: string
  actor: string
  direction: string
  direction_confidence: number | null
  direction_source: string
  evidence_count: number | null
  parser_version: string
  cluster_id: number | null
  reasoning: string
  created_at: string
}

/** 候选时间（event_time_candidates 表） */
export type EventTimeCandidate = {
  id: number
  candidate_ts: number
  candidate_ts_iso?: string
  tz: string
  source: 'platform_published_at' | 'ai_inferred' | 'cross_net' | 'human_annotated' | 'other' | string
  confidence: number | null
  reasoning: string
  is_primary: boolean
  parser_version: string
  created_at: string
}

/** LLM 输入的 6 字段 payload（structured_input JSON 解析后） */
export type StructuredInput = {
  meta?: Record<string, unknown>
  events?: unknown[]
  snapshot?: Record<string, unknown>
  relations?: unknown[]
  baseline?: Record<string, unknown>
  text_summary?: string
  /** 后端解析失败的 raw JSON 透传 */
  _raw?: string
  _parse_error?: boolean
}

/** LLM reasoning 对 structured_input 的反向引用比对（structured_check JSON 解析后） */
export type StructuredCheck = {
  verdict?: 'ok' | 'fabricated' | 'partial' | 'missing' | string
  mentioned?: string[]
  missing?: string[]
  fabricated?: string[]
  check_at?: number | string
  reason?: string
  _raw?: string
  _parse_error?: boolean
}

/** 影响曲线单点（event_impact_curve 表） */
export type ImpactCurvePoint = {
  event_atom_id: number
  asset_id: number | null
  asset_code: string
  window_label: '5m' | '30m' | '2h' | '1d' | 'extended'
  delta_pct: number | null
  price: number | null
  volume_ratio: number | null
  sample_ts: string
  note: string
  event_type_code?: string
}

/** 影响曲线按窗口聚合 */
export type ImpactCurveAggregated = Partial<Record<'5m' | '30m' | '2h' | '1d', {
  asset_code: string
  price: number | null
  delta_pct: number | null
  volume_ratio: number | null
  sample_ts: number
  sample_ts_iso?: string
  note: string
}>>

/** 单新闻结构化查询返回 */
export type NewsStructuredResponse = {
  news_id: number
  decision_id: number | null
  created_at: string
  structured_input: StructuredInput | null
  structured_check: StructuredCheck | null
  event_type_code: string
  snapshot_proximity: string
  verdict: string | null
}

/** 维度表 - 资产 */
export type DimAsset = {
  id: number
  code: string
  name: string
  kind: 'crypto' | 'commodity' | 'equity_index' | 'fx' | 'bond' | 'other'
  venue: string
  active: number
}

/** 维度表 - 事件类型 */
export type DimEventType = {
  id: number
  code: string
  name_zh: string
  name_en: string
  category: 'macro' | 'commodity' | 'crypto' | 'geopolitics' | 'other'
  direction_bias: Record<string, string>
  active: number
}

/** event_atom 摘要 */
export type EventAtomSummary = {
  by_type_code: Record<string, number>
  by_asset_code: Record<string, number>
  by_category: Record<string, number>
  window_days: number
  since_ts: number
}

/** 每日影响曲线统计 */
export type EventImpactStat = {
  date_local: string
  impact_curve_filled: number | null
  event_atoms_total: number | null
  fallback_used: number | null
  avg_window_minutes: number | null
  clock_jumps: number | null
  date_epoch_start: number | null
  created_at: number | null
}

// 多品种副品种：主品种外的受影响品种独立信号（同一条新闻）
export type SiblingDecision = {
  decision_id: number | null
  action: Signal
  score: number
  target_asset: string
  market_category?: string
  reason?: string
  no_trade_reason?: string
  trade_gate_reason?: string
  evidence_confidence?: number | null
  entry_price?: number | null
  exit_price?: number | null
  max_price?: number | null
  min_price?: number | null
  settled?: number
  is_correct?: string
  forward_pnl?: number | null
}

// 多模型交叉验证：主模型外的验证模型独立分析结果（含完整六段推理）
export type PeerReview = {
  model_label?: string
  model_id?: string
  action?: string
  score?: number | null
  reasoning?: string
  reasoning_path?: string
  expected_horizon?: string
  event_strength?: string
  market_confirmation?: string
  direct_catalyst?: number | boolean | null
  heat_score?: number | null
  impact_score?: number | null
  asset_impact?: number | null
}

export type SentimentPoint = {
  time: string
  score: number
}

// v212 fix: 'Other' 桶被移除。聚合事件（极少出现）单独占一栏，并显式说明它的语义，
// 避免和"漏过筛层的垃圾箱"混淆。
// v?? 海力士 cap：单独一类（与 SKHYNIX 关联；韩股海力士 / 000660.KS / 美国海力士 / Micron 全部归此），
//              不再混入 STOCK 大类，避免中文圈用户在「股票」里漏看海力士。
export const MARKET_TABS: { key: MarketCategory; label: string }[] = [
  { key: 'ALL',       label: 'All' },
  { key: 'CRYPTO',    label: 'Crypto' },
  { key: 'GOLD',      label: 'Gold' },
  { key: 'OIL',       label: 'Oil' },
  { key: 'STOCK',     label: 'Stocks' },
  { key: 'HYNIX',     label: '海力士' },
  { key: 'MACRO',     label: 'Macro' },
  { key: 'AGGREGATE', label: '聚合事件' },
]
