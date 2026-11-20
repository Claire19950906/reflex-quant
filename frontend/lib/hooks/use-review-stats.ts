'use client'

/**
 * useReviewStats — 聚合 Review 数组的统计指标(v302+ P1)。
 *
 * 设计:
 *  - 纯函数 + useMemo,接受任意 Review[] 数组,返回统一结构
 *  - 不依赖 React 内部状态(不算严格 hook,但约定 lib/hooks 目录,便于测试)
 *  - 给 reasoning-review-panel / 其他面板复用"段缺失/6 维缺失/平均完备度"统计
 *  - 不在 hook 里 fetch 数据 — 数据拉取由 useApiPoll 负责,本 hook 只做聚合
 *
 * 返回结构刻意对齐原 reasoning-review-panel.tsx 的 stats 字段,
 *   保证内联使用和 hook 调用结果一致。
 */

import { useMemo } from 'react'
import type { Review } from '@/lib/types-reasoning-review'

export type ReviewStats = {
  avg: number
  n: number
  missingStep: number
  noCounter: number
  noMech: number
  noCross: number
  noAssum: number
  noHist: number
}

/** 空状态(无数据时回退,UI 不需要判空) */
export const EMPTY_REVIEW_STATS: ReviewStats = {
  avg: 0,
  n: 0,
  missingStep: 0,
  noCounter: 0,
  noMech: 0,
  noCross: 0,
  noAssum: 0,
  noHist: 0,
}

/** 聚合纯函数: 任意 Review[] → 统计指标。空数组返回 EMPTY_REVIEW_STATS。 */
export function aggregateReviewStats(items: ReadonlyArray<Review>): ReviewStats {
  if (items.length === 0) return EMPTY_REVIEW_STATS
  return {
    avg: items.reduce((s, x) => s + x.completion_score, 0) / items.length,
    n: items.length,
    missingStep: items.filter((x) => x.steps.missing.length > 0).length,
    noCounter: items.filter((x) => !x.counter_consensus.present).length,
    noMech: items.filter((x) => !x.causal_mechanism.present).length,
    noCross: items.filter((x) => !x.cross_asset_direction.present).length,
    noAssum: items.filter((x) => !x.assumption.present).length,
    noHist: items.filter((x) => !x.historical_pattern.present).length,
  }
}

/** Hook 形态: 内部 useMemo 缓存,reviews 引用不变不重算 */
export function useReviewStats(
  reviews: ReadonlyArray<Review> | undefined | null,
): ReviewStats {
  return useMemo(() => aggregateReviewStats(reviews ?? []), [reviews])
}
