import { API_BASE } from '@/lib/api'

// Replay data types — simulated/paper-trading view of EXECUTED signals

export type SimSignal = {
  id: number
  news_id: number
  news_time: string
  source: string
  news_text: string
  asset: string
  action: 'BUY' | 'SELL' | 'HOLD'
  score: number
  reasoning: string | null
  reasoning_path: string | null
  market_category: string
  event_strength: string | null
  direct_catalyst: number | null
  prediction_type: string | null
  market_confirmation: string | null
  event_phase: string | null
  expected_horizon: string | null
  invalidation_condition: string | null
  decision_context: string | null
  entry_price: number | null
  exit_price: number | null
  partial_exit_price: number | null
  partial_exit_ratio: number | null
  partial_pnl_pct: number | null
  heat_score: number | null
  impact_score: number | null
  asset_impact: number | null
  max_price: number | null
  min_price: number | null
  max_price_time: number | null
  min_price_time: number | null
  entry_time: string | null
  is_correct: 'WIN' | 'LOSS' | 'HOLD' | 'BREAKEVEN' | '' | null
  settled: number
  mfe_pct: number | null
  mae_pct: number | null
  forward_pnl: number | null
  mfe_time_mins: number | null
  extra_models_consensus: Record<string, unknown> | string | null
  doubao_action: 'BUY' | 'SELL' | 'HOLD' | null
  doubao_reasoning: string | null
  no_trade_reason?: string | null
  trade_gate_reason?: string | null
  evidence_confidence?: number | null
  evidence_action?: string | null
  peer_reviews?: Array<{
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
  }> | null
  cluster_size: number | null
  timeframe_match: string | null
  created_at: string | null
  strategy_id: number | null
  strategy_version_id: number | null
  strategy_name: string | null
  strategy_version: number | null
  strategy_params: Record<string, number | string | boolean> | null
  current_price?: number | null
  current_pnl_pct?: number | null
  current_pnl_usdt?: number | null
  live_mfe_pct?: number
  live_mae_pct?: number
  pricing_status?: 'LIVE' | 'UNAVAILABLE'
  paper_status?: 'TRACKING' | 'SETTLED'
}

export type PaperTrack = 'crypto' | 'gold' | 'oil' | 'stocks'

export type PaperTradingSettings = {
  is_running: boolean
  tracks: PaperTrack[]
  gate_enabled?: boolean
  news_trade_max_age_seconds?: number
  tp_enabled?: boolean
  tp1_mult?: number
  tp2_mult?: number
  tp1_ratio?: number
  tp2_ratio?: number
  buy_min_score?: number
  sell_min_score?: number
  market_structure_enabled?: boolean
  started_at: string | null
  active_run_id: number | null
  active_run: null | {
    id: number
    strategy_id: number
    strategy_version_id: number
    strategy_name: string
    strategy_version: number
    agent_version: string
    model_id: string
    spec_version: string
    activation_reason: string
    status: 'RUNNING' | 'STOPPED'
  }
  updated_at: string | null
}

export type QuickSimProfitability = {
  settled: number
  wins: number
  losses: number
  breakeven: number
  winrate_pct: number | null
  avg_pnl_pct: number | null
  total_pnl_pct: number
  total_pnl_usdt: number
  return_on_notional_pct: number | null
  profitable: boolean | null
}

export type QuickSimEvaluation = {
  enabled: boolean
  horizon_minutes: number
  notional_usdt: number
  open_trades: number
  overall: QuickSimProfitability
  gate_passed: QuickSimProfitability
  gate_rejected: QuickSimProfitability
  by_asset: Array<QuickSimProfitability & { asset: string }>
  by_model: Array<QuickSimProfitability & { model: string }>
  open: Array<{
    id: number
    decision_id: number
    asset: string
    action: 'BUY' | 'SELL'
    entry_price: number
    entry_ts: number
    horizon_minutes: number
    gate_passed: number
    gate_reason: string
  }>
  recent: Array<{
    id: number
    decision_id: number
    asset: string
    action: 'BUY' | 'SELL'
    entry_price: number
    exit_price: number | null
    pnl_pct: number | null
    pnl_usdt: number | null
    verdict: string
    gate_passed: number
    gate_reason: string
    entry_ts: number
    exit_ts: number | null
    settled: number
  }>
  method: string
}

export type ReplayPositions = {
  current_strategy: {
    id: number
    name: string
    active_version: {
      id: number
      version: number
      params: Record<string, number | string | boolean>
    }
  }
  trading_settings: PaperTradingSettings
  news_settings?: NewsSourceSettings
  pairs: Array<{
    asset: string
    symbol: string
    track: PaperTrack
    price: number | null
    change24h: number | null
    source: string
    source_count: number
    status: 'LIVE' | 'UNAVAILABLE'
  }>
  market_status: 'ok' | 'partial' | 'unavailable'
  market_sources: Record<string, { status: string; assets: string[]; error: string }>
  strategy_feedback: {
    stage: 'STOPPED' | 'WAITING_NEWS' | 'SIGNAL_REJECTED' | 'POSITION_OPEN'
    message: string
    evaluated: number
    passed: number
    rejected: number
    latest: Array<{
      id: number
      target_asset: string
      suggested_action: string
      evidence_confidence: number | null
      evidence_action: string
      trade_gate_reason: string
      entry_price: number | null
      created_at: string
    }>
  }
  llm_performance: {
    model_id: string
    settled: number
    wins: number
    losses: number
    holds: number
    accuracy_pct: number | null
    avg_pnl_pct: number | null
    total_pnl_pct: number
    profitable: boolean | null
    verdict: string
    method: string
    all_models?: {
      settled: number
      wins: number
      losses: number
      holds: number
      accuracy_pct: number | null
      avg_pnl_pct: number | null
      total_pnl_pct: number
      note: string
    }
  }
  quick_sim: QuickSimEvaluation
  positions: SimSignal[]
  account: {
    initial_equity_usdt: number
    realized_pnl_usdt: number
    unrealized_pnl_usdt: number | null
    current_equity_usdt: number | null
    used_margin_usdt: number
    available_equity_usdt: number | null
    unpriced_positions: number
  }
  updated_at_ms: number
  pricing_note: string
}

export type ReplayKlineBar = {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type ReplayKlineMarker = {
  time: number
  position: 'aboveBar' | 'belowBar' | 'inBar'
  color: string
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square'
  text: string
  kind: 'entry' | 'exit'
}

export type ReplayKlineResponse = {
  signal_id: number
  asset: string
  action?: 'BUY' | 'SELL' | 'HOLD'
  entry_price: number
  entry_time: number
  exit_price?: number | null
  stop_loss?: number | null
  take_profit?: number | null
  trailing_callback_rate?: number
  invalidation_condition?: string
  settled?: number
  is_paper_trading: boolean
  data_source?: 'real' | 'simulated'
  market_type?: 'binance_perp' | 'simulated'   // 币安永续真实行情 / 模拟反推
  markers?: ReplayKlineMarker[]
  klines: ReplayKlineBar[]
}

export type NewsSourceKey = 'financialjuice' | 'tree_news' | 'techflow' | 'eastmoney' | 'blockbeats' | 'jin10' | 'fred' | 'finnhub' | 'feishu_jin10' | 'reuters' | 'bls' | 'yahoo_finance' | 'marketwatch' | 'fxstreet'

export type NewsSourceSettings = {
  enabled: boolean
  daily_target: number
  today_count: number
  remaining: number
  sources: Record<NewsSourceKey, boolean>
}

export type ReplayStats = {
  overall: {
    total: number
    tracking: number
    settled: number
    wins: number
    losses: number
    holds: number
    avg_pnl: number | null
    avg_mfe: number | null
    avg_mae: number | null
    best_trade: number | null
    worst_trade: number | null
    winrate: number
  }
  strategy_id: number
  strategy_version_id: number
  by_asset: Array<{ asset: string; total: number; wins: number; losses: number; avg_pnl: number | null }>
  by_action: Array<{ action: string; total: number; wins: number; avg_pnl: number | null }>
  is_paper_trading: boolean
}

export async function fetchReplaySignals(params?: {
  asset?: string
  action?: string
  settled?: number
  limit?: number
}): Promise<SimSignal[]> {
  const sp = new URLSearchParams()
  if (params?.asset) sp.set('asset', params.asset)
  if (params?.action) sp.set('action', params.action)
  if (params?.settled !== undefined) sp.set('settled', String(params.settled))
  if (params?.limit) sp.set('limit', String(params.limit))
  const url = `${API_BASE}/replay/signals?${sp.toString()}`
  const r = await fetch(url)
  if (!r.ok) throw new Error(`fetchReplaySignals ${r.status}`)
  return (await r.json()) as SimSignal[]
}

export async function fetchReplayPositions(): Promise<ReplayPositions> {
  const r = await fetch(`${API_BASE}/replay/positions?limit=50`, { cache: 'no-store' })
  if (!r.ok) throw new Error(`fetchReplayPositions ${r.status}`)
  return (await r.json()) as ReplayPositions
}

function asTradingSettings(payload: Record<string, unknown>): PaperTradingSettings {
  return {
    is_running: Boolean(payload.is_running),
    tracks: Array.isArray(payload.tracks) ? payload.tracks as PaperTrack[] : [],
    gate_enabled: payload.gate_enabled === undefined ? undefined : Boolean(payload.gate_enabled),
    news_trade_max_age_seconds:
      typeof payload.news_trade_max_age_seconds === 'number' ? payload.news_trade_max_age_seconds : undefined,
    tp_enabled: payload.tp_enabled === undefined ? undefined : Boolean(payload.tp_enabled),
    tp1_mult: typeof payload.tp1_mult === 'number' ? payload.tp1_mult : undefined,
    tp2_mult: typeof payload.tp2_mult === 'number' ? payload.tp2_mult : undefined,
    tp1_ratio: typeof payload.tp1_ratio === 'number' ? payload.tp1_ratio : undefined,
    tp2_ratio: typeof payload.tp2_ratio === 'number' ? payload.tp2_ratio : undefined,
    buy_min_score: typeof payload.buy_min_score === 'number' ? payload.buy_min_score : undefined,
    sell_min_score: typeof payload.sell_min_score === 'number' ? payload.sell_min_score : undefined,
    market_structure_enabled:
      payload.market_structure_enabled === undefined ? undefined : Boolean(payload.market_structure_enabled),
    started_at: (payload.started_at as string | null) ?? null,
    active_run_id: (payload.active_run_id as number | null) ?? null,
    active_run: (payload.active_run as PaperTradingSettings['active_run']) ?? null,
    updated_at: (payload.updated_at as string | null) ?? null,
  }
}

function asNewsSettings(payload: Record<string, unknown> | null | undefined): NewsSourceSettings | null {
  if (!payload) return null
  const nested = payload.news_settings
  const source = nested && typeof nested === 'object' ? nested as Record<string, unknown> : payload
  if (typeof source.enabled !== 'boolean' || !source.sources || typeof source.sources !== 'object') {
    return null
  }
  return source as unknown as NewsSourceSettings
}

export async function updatePaperTrading(settings: Pick<PaperTradingSettings, 'is_running' | 'tracks'> & {
  gate_enabled?: boolean
  news_trade_max_age_seconds?: number
  tp_enabled?: boolean
  tp1_mult?: number
  tp2_mult?: number
  tp1_ratio?: number
  tp2_ratio?: number
  buy_min_score?: number
  sell_min_score?: number
  market_structure_enabled?: boolean
}): Promise<PaperTradingSettings> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 15_000)
  try {
    const r = await fetch(`${API_BASE}/replay/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
      signal: controller.signal,
    })
    if (!r.ok) throw new Error((await r.text()) || `updatePaperTrading ${r.status}`)
    const payload = await r.json() as Record<string, unknown>
    const saved = asTradingSettings(payload)
    if (settings.gate_enabled !== undefined && saved.gate_enabled === undefined) {
      saved.gate_enabled = settings.gate_enabled
    }
    return saved
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      throw new Error('请求超时（15 秒未响应），请检查后端 API 是否正常运行')
    }
    throw cause
  } finally {
    window.clearTimeout(timer)
  }
}

export type ReplayResetResult = {
  ok: boolean
  message: string
  backup: {
    json: string
    excel: string
    traded_decisions: number
    quick_sim_trades: number
    paper_trading_runs: number
  }
  cleared: {
    signals: number
    quick_sim_trades: number
    paper_trading_runs: number
  }
}

export async function resetPaperTrading(): Promise<ReplayResetResult> {
  const r = await fetch(`${API_BASE}/replay/reset`, { method: 'POST' })
  if (!r.ok) throw new Error((await r.text()) || `resetPaperTrading ${r.status}`)
  return (await r.json()) as ReplayResetResult
}

/** 把后端返回的备份完整路径转成可下载链接（GET /api/backups/{文件名}）。 */
export function backupDownloadUrl(fullPath: string): string {
  const name = fullPath.split(/[\\/]/).pop() ?? ''
  return `${API_BASE}/backups/${encodeURIComponent(name)}`
}

export async function fetchReplayStats(strategyId?: number, versionId?: number): Promise<ReplayStats> {
  const sp = new URLSearchParams()
  if (strategyId) sp.set('strategy_id', String(strategyId))
  if (versionId) sp.set('version_id', String(versionId))
  const r = await fetch(`${API_BASE}/replay/stats?${sp.toString()}`, { cache: 'no-store' })
  if (!r.ok) throw new Error(`fetchReplayStats ${r.status}`)
  return (await r.json()) as ReplayStats
}

const DEFAULT_NEWS_SETTINGS: NewsSourceSettings = {
  enabled: true,
  daily_target: 300,
  today_count: 0,
  remaining: 300,
  sources: {
    financialjuice: true,
    tree_news: true,
    techflow: true,
    eastmoney: true,
    blockbeats: true,
    jin10: true,
    fred: true,
    finnhub: true,
    feishu_jin10: true,
    reuters: true,
    bls: true,
    yahoo_finance: true,
    marketwatch: true,
    fxstreet: true,
  },
}

export async function fetchNewsSourceSettings(): Promise<NewsSourceSettings> {
  const replay = await fetch(`${API_BASE}/replay/settings`, { cache: 'no-store' })
  if (replay.ok) {
    const mapped = asNewsSettings(await replay.json() as Record<string, unknown>)
    if (mapped) return mapped
  }
  return DEFAULT_NEWS_SETTINGS
}

export async function updateNewsSourceSettings(body: {
  enabled?: boolean
  daily_target?: number
  sources?: Partial<Record<NewsSourceKey, boolean>>
}): Promise<NewsSourceSettings> {
  const r = await fetch(`${API_BASE}/replay/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      news_enabled: body.enabled,
      news_daily_target: body.daily_target,
      news_sources: body.sources,
    }),
  })
  if (!r.ok) throw new Error((await r.text()) || `updateNewsSourceSettings ${r.status}`)
  return asNewsSettings(await r.json() as Record<string, unknown>) ?? DEFAULT_NEWS_SETTINGS
}

export async function fetchReplaySignalKline(
  signalId: number,
  interval: '1m' | '15m' | '1h' | '4h' | '1d' = '1h'
): Promise<ReplayKlineResponse> {
  const r = await fetch(`${API_BASE}/replay/signal/${signalId}/kline?interval=${interval}`)
  if (!r.ok) throw new Error(`fetchReplaySignalKline ${r.status}`)
  return (await r.json()) as ReplayKlineResponse
}

/** 轻量实时价+成交量：原地更新最后一根 K 线 bar（加密走 Binance，高频安全）。 */
export async function fetchLivePrice(
  asset: string,
  interval: '1m' | '15m' | '1h' | '4h' | '1d' = '1h'
): Promise<{ price: number | null; volume: number | null }> {
  try {
    const r = await fetch(`${API_BASE}/replay/kline/live?asset=${encodeURIComponent(asset)}&interval=${interval}`, { cache: 'no-store' })
    if (!r.ok) return { price: null, volume: null }
    return (await r.json()) as { price: number | null; volume: number | null }
  } catch {
    return { price: null, volume: null }
  }
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}%`
}

export function fmtPrice(v: number | null | undefined, asset = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const upper = asset.toUpperCase()
  const decimals = upper === 'BTC' || upper === 'ETH' ? 2 : upper === 'XAU' || upper === 'GOLD' ? 2 : 4
  return v.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function actionColor(action: string): string {
  switch (action.toUpperCase()) {
    case 'BUY':
      return 'text-long border-long/40 bg-long/10'
    case 'SELL':
      return 'text-short border-short/40 bg-short/10'
    case 'FAILED':
      return 'text-short border-short/50 bg-short/15'
    default:
      return 'text-muted-foreground border-border bg-secondary'
  }
}

export function verdictColor(verdict: string | null | undefined): string {
  switch ((verdict || '').toUpperCase()) {
    case 'WIN':
      return 'text-long border-long/50 bg-long/15'
    case 'LOSS':
      return 'text-short border-short/50 bg-short/15'
    case 'HOLD':
    case 'BREAKEVEN':
      return 'text-hold border-hold/50 bg-hold/15'
    default:
      return 'text-muted-foreground border-border bg-secondary'
  }
}
