/**
 * useSegmentHistory — 段位历史快照 hook (v302+ P2 PR-16)。
 *
 * 测试目标:
 *  - 默认 7 天 / 200ms 间隔轮询,返回 SegmentHistory
 *  - 加载中 / 失败 → EMPTY_HISTORY fallback
 *  - days 钳位 [1, 30]
 *  - enabled=false → 不发请求
 *  - refresh 触发重拉
 *  - 拉到的数据 baseline / items 形状正确
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useSegmentHistory, EMPTY_HISTORY } from './use-segment-history'
import { FIXED_STAGE_KEYS } from '@/lib/segment-drift'

const apiGetMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiGet: (...args: unknown[]) => (apiGetMock as any)(...args),
}))

const sampleHistory = {
  days: 7,
  baseline: {
    总框架: 0.95,
    因素定位: 0.88,
    驱动力: 0.72,
    水位博弈: 0.65,
    跨资产资金: 0.45,
    反共识结论: 0.5,
    逻辑自检: 0.6,
    总结: 0.8,
  },
  items: [
    { date: '2026-10-06', count: 12, stages: {
      总框架: 1.0, 因素定位: 0.9, 驱动力: 0.7, 水位博弈: 0.6,
      跨资产资金: 0.4, 反共识结论: 0.5, 逻辑自检: 0.6, 总结: 0.8,
    }},
    { date: '2026-10-07', count: 8, stages: {
      总框架: 0.9, 因素定位: 0.85, 驱动力: 0.75, 水位博弈: 0.7,
      跨资产资金: 0.5, 反共识结论: 0.5, 逻辑自检: 0.6, 总结: 0.8,
    }},
  ],
}

describe('useSegmentHistory', () => {
  beforeEach(() => {
    apiGetMock.mockReset()
  })

  it('默认 7 天 → URL 含 days=7&limit=1000', async () => {
    apiGetMock.mockResolvedValue(sampleHistory)
    renderHook(() => useSegmentHistory({ intervalMs: 0 }))
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalled()
    })
    const url = apiGetMock.mock.calls[0][0]
    expect(url).toContain('days=7')
    expect(url).toContain('limit=1000')
  })

  it('days 传入 0 → 钳到 1', async () => {
    apiGetMock.mockResolvedValue(sampleHistory)
    renderHook(() => useSegmentHistory({ days: 0, intervalMs: 0 }))
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalled()
    })
    expect(apiGetMock.mock.calls[0][0]).toContain('days=1')
  })

  it('days 传入 100 → 钳到 30', async () => {
    apiGetMock.mockResolvedValue(sampleHistory)
    renderHook(() => useSegmentHistory({ days: 100, intervalMs: 0 }))
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalled()
    })
    expect(apiGetMock.mock.calls[0][0]).toContain('days=30')
  })

  it('days=14 → URL 含 days=14', async () => {
    apiGetMock.mockResolvedValue(sampleHistory)
    renderHook(() => useSegmentHistory({ days: 14, intervalMs: 0 }))
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalled()
    })
    expect(apiGetMock.mock.calls[0][0]).toContain('days=14')
  })

  it('拉取成功 → data 含 baseline + items', async () => {
    apiGetMock.mockResolvedValue(sampleHistory)
    const { result } = renderHook(() => useSegmentHistory({ intervalMs: 0 }))
    await waitFor(() => {
      expect(result.current.data.days).toBe(7)
    })
    expect(result.current.data.baseline['总框架']).toBe(0.95)
    expect(result.current.data.items).toHaveLength(2)
    expect(result.current.data.items[0].date).toBe('2026-10-06')
  })

  it('enabled=false → 不发请求,data 保持 EMPTY_HISTORY', () => {
    apiGetMock.mockResolvedValue(sampleHistory)
    const { result } = renderHook(() => useSegmentHistory({ enabled: false, intervalMs: 0 }))
    expect(apiGetMock).not.toHaveBeenCalled()
    expect(result.current.data).toBe(EMPTY_HISTORY)
    expect(result.current.data.items).toEqual([])
  })

  it('拉取失败 → data 仍为 EMPTY_HISTORY,error 写入', async () => {
    apiGetMock.mockRejectedValue(new Error('500'))
    const { result } = renderHook(() => useSegmentHistory({ intervalMs: 0 }))
    await waitFor(() => {
      expect(result.current.error).toBeTruthy()
    })
    expect(result.current.data).toBe(EMPTY_HISTORY)
    expect(result.current.error).toContain('500')
  })

  it('refresh() → 重新发请求', async () => {
    apiGetMock.mockResolvedValue(sampleHistory)
    const { result } = renderHook(() => useSegmentHistory({ intervalMs: 0 }))
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledTimes(1)
    })
    await act(async () => {
      result.current.refresh()
    })
    await waitFor(() => {
      expect(apiGetMock.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
  })
})

describe('EMPTY_HISTORY', () => {
  it('baseline 8 段全 0', () => {
    expect(Object.keys(EMPTY_HISTORY.baseline)).toHaveLength(8)
    for (const k of FIXED_STAGE_KEYS) {
      expect(EMPTY_HISTORY.baseline[k]).toBe(0)
    }
  })

  it('items 为空数组', () => {
    expect(EMPTY_HISTORY.items).toEqual([])
  })
})
