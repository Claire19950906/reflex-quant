import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { aggregateReviewStats, useReviewStats, EMPTY_REVIEW_STATS } from './use-review-stats'
import type { Review } from '@/lib/types-reasoning-review'

function mkReview(over: Partial<Review> = {}): Review {
  return {
    decision_id: 1, asset: 'BTC', action: 'BUY', completion_score: 80,
    forward_pnl: null, is_correct: null, reasoning_length: 100,
    steps: { present: [], missing: [], pct: 0 },
    counter_consensus: { present: true, keywords_hit: [] },
    causal_mechanism: { present: true, keywords_hit: [] },
    factor_concepts: { count: 0, concepts: [] },
    cross_asset_direction: { present: true, target_asset: '', items: [], aligned_count: 0, conflict_count: 0, detail: '' },
    assumption: { present: true, keywords_hit: [] },
    historical_pattern: { present: true, keywords_hit: [] },
    calibration: { confidence: null, pnl_realized: null, is_correct: null },
    notes: [],
    ...over,
  }
}

describe('aggregateReviewStats — 纯函数', () => {
  it('空数组返回 EMPTY_REVIEW_STATS', () => {
    expect(aggregateReviewStats([])).toEqual(EMPTY_REVIEW_STATS)
  })

  it('单条: avg=80, n=1, missingStep=0', () => {
    const s = aggregateReviewStats([mkReview({ completion_score: 80 })])
    expect(s.avg).toBe(80)
    expect(s.n).toBe(1)
    expect(s.missingStep).toBe(0)
  })

  it('多条 avg 是算术平均', () => {
    const s = aggregateReviewStats([
      mkReview({ completion_score: 60 }),
      mkReview({ completion_score: 80 }),
      mkReview({ completion_score: 100 }),
    ])
    expect(s.avg).toBe(80)
    expect(s.n).toBe(3)
  })

  it('missingStep: missing.length > 0 计入', () => {
    const s = aggregateReviewStats([
      mkReview({ steps: { present: [], missing: ['总结'], pct: 87.5 } }),
      mkReview(),
      mkReview({ steps: { present: [], missing: ['跨资产资金', '总结'], pct: 75 } }),
    ])
    expect(s.missingStep).toBe(2)
  })

  it('6 维缺一: 5 个 noXxx 各加 1', () => {
    const s = aggregateReviewStats([
      mkReview({
        counter_consensus: { present: false, keywords_hit: [] },
        causal_mechanism: { present: false, keywords_hit: [] },
        cross_asset_direction: { present: false, target_asset: '', items: [], aligned_count: 0, conflict_count: 0, detail: '' },
        assumption: { present: false, keywords_hit: [] },
        historical_pattern: { present: false, keywords_hit: [] },
      }),
    ])
    expect(s.noCounter).toBe(1)
    expect(s.noMech).toBe(1)
    expect(s.noCross).toBe(1)
    expect(s.noAssum).toBe(1)
    expect(s.noHist).toBe(1)
  })

  it('全命中 6 维: 全 0', () => {
    const s = aggregateReviewStats([mkReview()])
    expect(s.noCounter).toBe(0)
    expect(s.noMech).toBe(0)
    expect(s.noCross).toBe(0)
    expect(s.noAssum).toBe(0)
    expect(s.noHist).toBe(0)
  })

  it('混合: 5 条里 2 条无 counter_consensus', () => {
    const s = aggregateReviewStats([
      mkReview({ counter_consensus: { present: false, keywords_hit: [] } }),
      mkReview(),
      mkReview({ counter_consensus: { present: false, keywords_hit: [] } }),
      mkReview(),
      mkReview(),
    ])
    expect(s.noCounter).toBe(2)
    expect(s.n).toBe(5)
  })

  it('avg 用浮点累加(100 条)不会丢精度', () => {
    const items = Array.from({ length: 100 }, () => mkReview({ completion_score: 77.7 }))
    const s = aggregateReviewStats(items)
    expect(s.avg).toBeCloseTo(77.7, 5)
    expect(s.n).toBe(100)
  })
})

describe('useReviewStats — hook 形态', () => {
  it('undefined reviews:返回 EMPTY_REVIEW_STATS', () => {
    const { result } = renderHook(() => useReviewStats(undefined))
    expect(result.current).toBe(EMPTY_REVIEW_STATS)
  })

  it('null reviews:返回 EMPTY_REVIEW_STATS', () => {
    const { result } = renderHook(() => useReviewStats(null))
    expect(result.current).toBe(EMPTY_REVIEW_STATS)
  })

  it('reviews 引用稳定 → 不重算', () => {
    const reviews = [mkReview({ completion_score: 90 })]
    const { result, rerender } = renderHook(() => useReviewStats(reviews))
    const first = result.current
    rerender()
    expect(result.current).toBe(first)
  })

  it('reviews 引用变 → 重算', () => {
    const r1 = [mkReview({ completion_score: 60 })]
    const r2 = [mkReview({ completion_score: 90 })]
    const { result, rerender } = renderHook(({ data }) => useReviewStats(data), {
      initialProps: { data: r1 },
    })
    const first = result.current
    rerender({ data: r2 })
    expect(result.current).not.toBe(first)
    expect(result.current.avg).toBe(90)
  })
})
