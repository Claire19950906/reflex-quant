'use client'

/**
 * useSegmentDrift — 把 reviews 数组 → 段位漂移检测结果 (v302+ P1)。
 *
 * 设计:
 *  - 内部就是 3 行: presenceFromReviews + detectSegmentDrift + DEFAULT_BASELINE
 *  - 抽成 hook 主要是: 1) useMemo 缓存避免每次渲染重算; 2) 给其他面板同款接口
 *  - 返回永远有效的 DriftResult(空数据返回 EMPTY_DRIFT,UI 直接 <SegmentDriftBadge /> 即可)
 *
 * 不上 rolling baseline: 静态 DEFAULT_BASELINE 已能反映"模型新偏差 vs 6 个月前基线";
 *   滚动需要历史 N>当前批 的数据接口,目前 /reasoning/review/list 一次只返当前批。
 *   后续可加 /reasoning/review/history 端点后再上 detectDriftWithRollingBaseline 变体。
 */

import { useMemo } from 'react'
import {
  detectSegmentDrift,
  presenceFromReviews,
  DEFAULT_BASELINE,
  EMPTY_DRIFT,
  type DriftResult,
} from '@/lib/segment-drift'

export function useSegmentDrift<
  R extends { steps: { missing: string[] } },
>(reviews: ReadonlyArray<R> | undefined | null): DriftResult {
  return useMemo<DriftResult>(() => {
    if (!reviews || reviews.length === 0) return EMPTY_DRIFT
    const presence = presenceFromReviews(reviews)
    return detectSegmentDrift(presence, DEFAULT_BASELINE)
  }, [reviews])
}

/**
 * useSegmentHistory — 拉 N 天段位快照,用于 8xN 热力图 (v302+ P2 PR-16)。
 *
 * 后端: GET /api/reasoning/review/segment-history?days=7&limit=1000
 *   返回: { days, baseline: {stage: rate}, items: [{date, count, stages: {stage: rate}}] }
 *
 * 设计:
 *  - 与 useApiPoll 同款: refresh() 由父组件控制(失败不爆 UI)
 *  - 失败/加载中返回 EMPTY_HISTORY(同 EMPTY_DRIFT 风格)
 *  - polling 周期 5 分钟(快照变化比单决策慢,不需要更频繁)
 *  - baseline 用于前端计算 delta 着色,不放进 hook(纯数据)
 */

import { useApiPoll } from '@/lib/hooks/use-api-poll'
import { FIXED_STAGE_KEYS, type StageKey } from '@/lib/segment-drift'

export type SegmentHistoryDay = {
  /** ISO 日期 'YYYY-MM-DD' */
  date: string
  /** 当天决策数(0 时 stages 全 0,UI 仍占位) */
  count: number
  /** 每段命中率 [0, 1] */
  stages: Record<StageKey, number>
}

export type SegmentHistory = {
  days: number
  baseline: Record<StageKey, number>
  items: SegmentHistoryDay[]
}

export const EMPTY_HISTORY: SegmentHistory = {
  days: 0,
  baseline: Object.fromEntries(FIXED_STAGE_KEYS.map((k) => [k, 0])) as Record<StageKey, number>,
  items: [],
}

export type UseSegmentHistoryOpts = {
  /** 1~30, 默认 7 */
  days?: number
  /** 是否启用轮询,默认 true(快照变化慢) */
  enabled?: boolean
  /** 轮询间隔 ms, 默认 5 分钟 */
  intervalMs?: number
}

export function useSegmentHistory(opts: UseSegmentHistoryOpts = {}): {
  data: SegmentHistory
  loading: boolean
  error: string | null
  refresh: () => void
} {
  const { days = 7, enabled = true, intervalMs = 5 * 60 * 1000 } = opts
  // days 上限 30, 与后端对齐
  const safeDays = Math.max(1, Math.min(30, Math.floor(days)))

  const { data, loading, error, refresh } = useApiPoll<SegmentHistory>(
    `/api/reasoning/review/segment-history?days=${safeDays}&limit=1000`,
    { intervalMs, enabled },
  )

  return {
    data: data ?? EMPTY_HISTORY,
    loading,
    error,
    refresh,
  }
}
