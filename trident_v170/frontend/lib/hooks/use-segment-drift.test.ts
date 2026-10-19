import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useSegmentDrift } from './use-segment-drift'
import { EMPTY_DRIFT, FIXED_STAGE_KEYS } from '@/lib/segment-drift'

describe('useSegmentDrift — 行为契约', () => {
  it('undefined:返回 EMPTY_DRIFT', () => {
    const { result } = renderHook(() => useSegmentDrift(undefined))
    expect(result.current).toBe(EMPTY_DRIFT)
  })

  it('null:返回 EMPTY_DRIFT', () => {
    const { result } = renderHook(() => useSegmentDrift(null))
    expect(result.current).toBe(EMPTY_DRIFT)
  })

  it('空数组:返回 EMPTY_DRIFT', () => {
    const { result } = renderHook(() => useSegmentDrift([]))
    expect(result.current).toBe(EMPTY_DRIFT)
  })

  it('有效 reviews:返回非空 DriftResult', () => {
    const reviews = [
      { steps: { missing: [] as string[] } },
      { steps: { missing: ['跨资产资金'] as string[] } },
      { steps: { missing: [] as string[] } },
    ]
    const { result } = renderHook(() => useSegmentDrift(reviews))
    expect(result.current.total).toBe(3)
    // current 8 段都有
    for (const k of FIXED_STAGE_KEYS) {
      expect(typeof result.current.current[k]).toBe('number')
    }
  })

  it('依赖稳定性:同 reviews ref 多次渲染不重算', () => {
    const reviews = [{ steps: { missing: ['总结'] as string[] } }]
    const { result, rerender } = renderHook(() => useSegmentDrift(reviews))
    const first = result.current
    rerender()
    expect(result.current).toBe(first)  // useMemo 命中
  })

  it('reviews ref 变 → 重算', () => {
    const r1 = [{ steps: { missing: [] as string[] } }]
    const r2 = [{ steps: { missing: ['总结'] as string[] } }]
    const { result, rerender } = renderHook(({ data }) => useSegmentDrift(data), {
      initialProps: { data: r1 },
    })
    const first = result.current
    rerender({ data: r2 })
    expect(result.current).not.toBe(first)
  })

  it('极端:100% 缺"总结" → alert severity = alert', () => {
    const reviews = Array.from({ length: 50 }, () => ({
      steps: { missing: ['总结'] as string[] },
    }))
    const { result } = renderHook(() => useSegmentDrift(reviews))
    const zj = result.current.alerts.find((a) => a.stage === '总结')
    expect(zj?.severity).toBe('alert')
  })

  it('足够样本稳定 → 无 alert (仅可能 warn)', () => {
    // 100% 命中除"跨资产资金" 外所有段 → 跨资产 0 vs 10% baseline = -10pp < 15pp 不告警
    const reviews = Array.from({ length: 100 }, () => ({
      steps: { missing: ['跨资产资金'] as string[] },
    }))
    const { result } = renderHook(() => useSegmentDrift(reviews))
    // 跨资产资金 0 vs 0.10 = -0.10 (no alert)
    // 其他段 100% vs baseline,正常情况下不告警 (但小 delta 可能 warn)
    const kua = result.current.alerts.find((a) => a.stage === '跨资产资金')
    expect(kua).toBeUndefined()
  })
})
