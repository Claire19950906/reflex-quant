// v188 反思系统 API client (经验 CRUD + missed-signal + reflection stats)
import { apiGet, apiSend } from './api'

// ─── Types ────────────────────────────────────────────────────────────────

export type ExperienceItem = {
  id: number
  kind: string
  title: string
  content: string
  applicable_assets: string  // JSON array
  applicable_kinds: string   // JSON array
  applicable_actors: string  // JSON array
  weight: number
  priority: number
  source: string
  status: string             // active / archived / draft
  apply_count: number
  hit_count: number
  last_applied_at: string
  // v188 columns:
  applied_win_rate: number | null
  control_win_rate: number | null
  control_apply_count: number | null
  lift: number | null
  verified_count: number | null
  created_at: string
  updated_at: string
}

export type ExperienceStats = {
  ok: boolean
  experience: ExperienceItem
  applied: {
    n: number
    llm_invoked_n: number | null
    win_rate: number | null
    avg_pnl: number | null
    first_used: string | null
    last_used: string | null
  }
  control: {
    n: number
    win_rate: number | null
    avg_pnl: number | null
  }
  lift: number | null
  lift_pct: number
  days: number
}

export type ReflectionLayerStat = {
  layer: string
  n_total: number
  n_loss: number
  n_win: number
  loss_rate: number | null
}

export type ReflectionSignalStat = {
  layer: string
  sub_layer: string
  signal_name: string
  asset: string
  action: string
  n_total: number
  n_loss: number
  loss_rate: number | null
}

export type ReflectionStats = {
  days: number
  by_layer: ReflectionLayerStat[]
  by_signal: ReflectionSignalStat[]
  lift_top: Array<{
    id: number
    kind: string
    title: string
    lift: number
    applied_win_rate: number | null
    control_win_rate: number | null
    control_apply_count: number | null
  }>
  lift_bottom: Array<{
    id: number
    kind: string
    title: string
    lift: number
    applied_win_rate: number | null
    control_win_rate: number | null
    control_apply_count: number | null
  }>
  missed_signal_by_asset: Array<{ target_asset: string; n_missed: number }>
  missed_signal_total: number
}

export type MissedSignalStats = {
  asset: string
  days: number
  n_total_settled: number
  n_hold: number
  n_missed: number
  missed_rate: number | null
  missed_decisions: Array<{
    decision_id: number
    forward_pnl: number
    no_trade_reason: string
    created_at: string
  }>
}

// ─── API ──────────────────────────────────────────────────────────────────

export async function listExperiences(params: {
  kind?: string
  status?: string
  asset?: string
  actor?: string
  limit?: number
}): Promise<{ items: ExperienceItem[]; count: number }> {
  const q = new URLSearchParams()
  if (params.kind) q.set('kind', params.kind)
  if (params.status) q.set('status', params.status)
  if (params.asset) q.set('asset', params.asset)
  if (params.actor) q.set('actor', params.actor)
  if (params.limit) q.set('limit', String(params.limit))
  return apiGet(`/v186/experiences?${q.toString()}`)
}

export async function getExperienceStats(id: number, days = 30): Promise<ExperienceStats> {
  return apiGet(`/v188/experiences/${id}/stats?days=${days}`)
}

export async function addExperience(payload: {
  kind: string
  title: string
  content: string
  applicable_assets?: string[]
  applicable_kinds?: string[]
  applicable_actors?: string[]
  weight?: number
  priority?: number
  source?: string
}): Promise<{ ok: boolean; id?: number; error?: string }> {
  return apiSend('/v188/experiences/add', 'POST', payload)
}

export async function updateExperience(
  id: number, payload: Partial<{
    kind: string
    title: string
    content: string
    applicable_assets: string[]
    applicable_kinds: string[]
    applicable_actors: string[]
    weight: number
    priority: number
    status: 'active' | 'archived' | 'draft'
  }>,
): Promise<{ ok: boolean; id?: number; rows_affected?: number; error?: string }> {
  return apiSend(`/v188/experiences/${id}/update`, 'POST', payload)
}

export async function archiveExperience(
  id: number,
): Promise<{ ok: boolean; id?: number; rows_affected?: number; error?: string }> {
  return apiSend(`/v188/experiences/${id}/archive`, 'POST', {})
}

export async function triggerMissedSignalDetect(
  asset?: string, windowHours = 24, limit = 200,
): Promise<{ ok: boolean; stats?: { scanned: number; marked: number; details: unknown[] }; error?: string }> {
  const q = new URLSearchParams()
  if (asset) q.set('asset', asset)
  q.set('window_hours', String(windowHours))
  q.set('limit', String(limit))
  return apiSend(`/v188/missed-signal/detect?${q.toString()}`, 'POST', {})
}

export async function getMissedSignalStats(asset: string, days = 7): Promise<MissedSignalStats> {
  return apiGet(`/v188/missed-signal/${asset}?days=${days}`)
}

export async function getReflectionStats(days = 7): Promise<ReflectionStats> {
  return apiGet(`/v188/reflection/stats?days=${days}`)
}

export async function reproduceTradeReflection(
  reflectionId: number,
): Promise<{ ok: boolean; reflection_id?: number; llm_depth_reflection?: unknown; error?: string }> {
  return apiSend(`/v188/trade-reflections/${reflectionId}/reproduce`, 'POST', {})
}

export async function enrichPendingReflections(
  batchSize = 10,
): Promise<{ ok: boolean; processed?: number; error?: string }> {
  return apiSend(`/v188/trade-reflections/enrich-pending?batch_size=${batchSize}`, 'POST', {})
}