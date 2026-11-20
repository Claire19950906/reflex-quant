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
