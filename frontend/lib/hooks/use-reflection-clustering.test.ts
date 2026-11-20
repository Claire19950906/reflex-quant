/**
 * useReflectionClustering — 失败模式聚类 hook (v302+ P2 PR-17)。
 * 覆盖: URL三参数/三个钳位/默认值/enabled/失败fallback/成功/refresh/intervalMs/EMPTY。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import {
  useReflectionClustering,
  EMPTY_CLUSTERING,
  type ReflectionClustering,
} from './use-reflection-clustering'

const apiGetMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiGet: (...args: unknown[]) => (apiGetMock as any)(...args),
}))

const sampleClustering: ReflectionClustering = {
  days: 30,
  min_count: 2,
  total_errors: 100,
  total_decisions: 45,
  patterns: [
    {
      code: 'E014', count: 25, pct_of_total: 0.25, severity_avg: 2.4,
      trend: 'up', trend_delta: 0.5, last_7d: 15, prior_7d: 10,
      top_assets: [{ asset: 'BTC', count: 8 }, { asset: 'ETH', count: 5 }],
      examples: [
        { decision_id: 100, detail: '推理链断裂', created_at: '2026-01-09 10:00:00' },
      ],
    },
  ],
}

describe('useReflectionClustering', () => {
  beforeEach(() => {
    apiGetMock.mockReset()
    apiGetMock.mockResolvedValue(sampleClustering)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // 1. 默认 30/2/3 → URL 含三参数
  it('builds URL with default days=30 min_count=2 limit_per_group=3', async () => {
    const { result } = renderHook(() => useReflectionClustering())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    const url = apiGetMock.mock.calls[0][0] as string
    expect(url).toContain('days=30')
    expect(url).toContain('min_count=2')
    expect(url).toContain('limit_per_group=3')
    expect(url.startsWith('/reflection/clustering?')).toBe(true)
  })

  // 2. days 钳位到 [1, 90]
  it('clamps days to [1, 90]', async () => {
    const { result: r1 } = renderHook(() => useReflectionClustering({ days: 0 }))
    await waitFor(() => expect(r1.current.loading).toBe(false))
    expect((apiGetMock.mock.calls[0][0] as string)).toContain('days=1')

    apiGetMock.mockClear()
    const { result: r2 } = renderHook(() => useReflectionClustering({ days: 500 }))
    await waitFor(() => expect(r2.current.loading).toBe(false))
    expect((apiGetMock.mock.calls[0][0] as string)).toContain('days=90')

    apiGetMock.mockClear()
    const { result: r3 } = renderHook(() => useReflectionClustering({ days: 14 }))
    await waitFor(() => expect(r3.current.loading).toBe(false))
    expect((apiGetMock.mock.calls[0][0] as string)).toContain('days=14')
  })

  // 3. min_count 钳位 [1, ∞)
  it('clamps min_count to >= 1', async () => {
    const { result } = renderHook(() => useReflectionClustering({ min_count: 0 }))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect((apiGetMock.mock.calls[0][0] as string)).toContain('min_count=1')

    apiGetMock.mockClear()
    const { result: r2 } = renderHook(() => useReflectionClustering({ min_count: 5 }))
    await waitFor(() => expect(r2.current.loading).toBe(false))
    expect((apiGetMock.mock.calls[0][0] as string)).toContain('min_count=5')
  })

  // 4. limit_per_group 钳位 [1, 10]
  it('clamps limit_per_group to [1, 10]', async () => {
    const { result: r1 } = renderHook(() => useReflectionClustering({ limit_per_group: 0 }))
    await waitFor(() => expect(r1.current.loading).toBe(false))
    expect((apiGetMock.mock.calls[0][0] as string)).toContain('limit_per_group=1')

    apiGetMock.mockClear()
    const { result: r2 } = renderHook(() => useReflectionClustering({ limit_per_group: 999 }))
    await waitFor(() => expect(r2.current.loading).toBe(false))
    expect((apiGetMock.mock.calls[0][0] as string)).toContain('limit_per_group=10')
  })

  // 5. 成功 → 返回 data
  it('returns data on success', async () => {
    const { result } = renderHook(() => useReflectionClustering())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data.patterns).toHaveLength(1)
    expect(result.current.data.patterns[0].code).toBe('E014')
    expect(result.current.data.total_errors).toBe(100)
  })

  // 6. 失败 → EMPTY_CLUSTERING fallback
  it('falls back to EMPTY_CLUSTERING on error', async () => {
    apiGetMock.mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useReflectionClustering())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data).toEqual(EMPTY_CLUSTERING)
    expect(result.current.error).toBeTruthy()
  })

  // 7. enabled=false → 不发请求
  it('skips request when enabled=false', async () => {
    const { result } = renderHook(() => useReflectionClustering({ enabled: false }))
    await new Promise(r => setTimeout(r, 30))
    expect(apiGetMock).not.toHaveBeenCalled()
    expect(result.current.data).toEqual(EMPTY_CLUSTERING)
  })

  // 8. refresh 触发再请求
  it('refresh triggers refetch', async () => {
    const { result } = renderHook(() => useReflectionClustering())
    await waitFor(() => expect(result.current.loading).toBe(false))
    const initialCalls = apiGetMock.mock.calls.length
    await act(async () => { await result.current.refresh() })
    expect(apiGetMock.mock.calls.length).toBeGreaterThan(initialCalls)
  })

  // 9. intervalMs 透传 — 不抛错即可
  it('passes intervalMs to useApiPoll', async () => {
    const { result } = renderHook(() => useReflectionClustering({ intervalMs: 60_000 }))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data.patterns).toHaveLength(1)
  })

  // 10. EMPTY_CLUSTERING 形状
  it('EMPTY_CLUSTERING has stable shape', () => {
    expect(EMPTY_CLUSTERING).toEqual({
      days: 30, min_count: 2, total_errors: 0, total_decisions: 0, patterns: [],
    })
  })

  // 11. 拉到的 data 形状正确 (top_assets / examples / trend 字段)
  it('exposes full pattern shape (trend / top_assets / examples)', async () => {
    const { result } = renderHook(() => useReflectionClustering())
    await waitFor(() => expect(result.current.loading).toBe(false))
    const p = result.current.data.patterns[0]
    expect(p.trend).toBe('up')
    expect(p.top_assets[0].asset).toBe('BTC')
    expect(p.examples[0].decision_id).toBe(100)
    expect(p.last_7d).toBe(15)
    expect(p.prior_7d).toBe(10)
  })
})
