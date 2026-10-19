'use client'

/**
 * useReflectionClustering — 拉失败模式聚类 (v302+ P2 PR-17)。
 *
 * 后端: GET /api/reflection/clustering?days=30&min_count=2&limit_per_group=3
 *   返回: { days, min_count, total_errors, total_decisions,
 *           patterns: [{code, count, pct_of_total, severity_avg,
 *                       trend, trend_delta, last_7d, prior_7d,
 *                       top_assets, examples}, ...] }
 *
 * 设计:
 *  - useApiPoll 包装,5 分钟轮询(聚类变化慢,不需要 30 秒)
 *  - days 钳 [1, 90]、min_count 钳 [1, ∞)、limit_per_group 钳 [1, 10]
 *  - enabled=false 时跳过
 *  - 失败 → EMPTY_CLUSTERING,UI 直接渲染空态
 *  - EMPTY_CLUSTERING 在 SSR/初次渲染时给个稳定骨架,避免 hydration mismatch
 */

import { useApiPoll } from '@/lib/hooks/use-api-poll'

export interface FailureAsset {
  asset: string
  count: number
}

export interface FailureExample {
  decision_id: number
  detail: string
  created_at: string
}

export interface FailurePattern {
  code: string
  count: number
  pct_of_total: number
  severity_avg: number
  trend: 'up' | 'down' | 'flat'
  trend_delta: number
  last_7d: number
  prior_7d: number
  top_assets: FailureAsset[]
  examples: FailureExample[]
}

export interface ReflectionClustering {
  days: number
  min_count: number
  total_errors: number
  total_decisions: number
  patterns: FailurePattern[]
}

export const EMPTY_CLUSTERING: ReflectionClustering = {
  days: 30,
  min_count: 2,
  total_errors: 0,
  total_decisions: 0,
  patterns: [],
}

export interface UseReflectionClusteringOptions {
  days?: number
  min_count?: number
  limit_per_group?: number
  enabled?: boolean
  intervalMs?: number
}

function clamp(v: number, lo: number, hi: number): number {
  if (!Number.isFinite(v)) return lo
  return Math.min(hi, Math.max(lo, Math.floor(v)))
}

export function useReflectionClustering(
  opts: UseReflectionClusteringOptions = {},
) {
  const days = clamp(opts.days ?? 30, 1, 90)
  const min_count = Math.max(1, Math.floor(opts.min_count ?? 2))
  const limit_per_group = clamp(opts.limit_per_group ?? 3, 1, 10)
  const path = `/reflection/clustering?days=${days}&min_count=${min_count}&limit_per_group=${limit_per_group}`
  const { data, loading, error, refresh } = useApiPoll<ReflectionClustering>(path, {
    intervalMs: opts.intervalMs ?? 300_000,
    immediate: true,
    enabled: opts.enabled !== false,
  })
  return {
    data: data ?? EMPTY_CLUSTERING,
    loading,
    error,
    refresh,
  }
}
