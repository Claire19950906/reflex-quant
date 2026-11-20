/**
 * useApiPoll 行为测试。
 *
 * 用 vi.mock 替换 @/lib/api 的 apiGet 以拦截网络,验证:
 *  - immediate / enabled / intervalMs / deps 行为
 *  - inFlight 防重复(in-flight 期间新一轮应跳过)
 *  - error / refresh / transform
 *  - 【v275+】AbortController: enabled / path / unmount 变化时取消进行中请求
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useApiPoll } from './use-api-poll'

const apiGetMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiGet: (...args: unknown[]) => (apiGetMock as any)(...args),
}))

describe('useApiPoll — error / refresh / transform', () => {
  beforeEach(() => {
    apiGetMock.mockReset()
  })

  it('失败 → state.error 写入,loading 归零', async () => {
    apiGetMock.mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useApiPoll('/x', { intervalMs: 0 }))
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.error).toBe('boom')
    expect(result.current.loading).toBe(false)
  })

  it('refresh() 主动触发重拉', async () => {
    apiGetMock.mockResolvedValue({ ok: 1 })
    const { result } = renderHook(() => useApiPoll('/x', { intervalMs: 0 }))
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    // 等第一次 immediate fetch 的 microtask 跑完,inFlight 归零
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    await act(async () => {
      await result.current.refresh()
    })
    expect(apiGetMock).toHaveBeenCalledTimes(2)
  })

  it('transform 返回 null → 当作失败(不抛错,error 也不写)', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    const { result } = renderHook(() =>
      useApiPoll<unknown>('/x', {
        intervalMs: 0,
        transform: (raw: any) => (raw.items.length === 0 ? null : raw.items),
      }),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.data).toBeNull()
    expect(result.current.error).toBeNull()
  })
})

describe('useApiPoll — AbortController 集成', () => {
  it('每次拉取都传一个新 AbortSignal', async () => {
    apiGetMock.mockReset()
    apiGetMock.mockImplementation(() => new Promise(() => {}))
    renderHook(() => useApiPoll('/x', { intervalMs: 0 }))
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    const signal = apiGetMock.mock.calls[0][1]?.signal
    expect(signal).toBeInstanceOf(AbortSignal)
  })

  it('enabled 由 true → false 时取消 in-flight', async () => {
    apiGetMock.mockReset()
    const seenSignals: AbortSignal[] = []
    apiGetMock.mockImplementation((_p: string, opts: any) => {
      if (opts?.signal) seenSignals.push(opts.signal)
      return new Promise(() => {})
    })

    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useApiPoll('/x', { intervalMs: 0, enabled }),
      { initialProps: { enabled: true } },
    )
    expect(seenSignals).toHaveLength(1)
    expect(seenSignals[0].aborted).toBe(false)

    await act(async () => {
      rerender({ enabled: false })
      await Promise.resolve()
    })
    expect(seenSignals[0].aborted).toBe(true)
  })

  it('path 改变 → 取消旧请求', async () => {
    apiGetMock.mockReset()
    const seenSignals: AbortSignal[] = []
    apiGetMock.mockImplementation((_p: string, opts: any) => {
      if (opts?.signal) seenSignals.push(opts.signal)
      return new Promise(() => {})
    })

    const { rerender } = renderHook(
      ({ p }: { p: string }) => useApiPoll(p, { intervalMs: 0 }),
      { initialProps: { p: '/a' } },
    )
    expect(seenSignals).toHaveLength(1)
    expect(seenSignals[0].aborted).toBe(false)

    await act(async () => {
      rerender({ p: '/b' })
      await Promise.resolve()
    })
    expect(seenSignals[0].aborted).toBe(true)
    expect(seenSignals[1]).not.toBe(seenSignals[0])
  })

  it('unmount → 取消 in-flight', async () => {
    apiGetMock.mockReset()
    const seenSignals: AbortSignal[] = []
    apiGetMock.mockImplementation((_p: string, opts: any) => {
      if (opts?.signal) seenSignals.push(opts.signal)
      return new Promise(() => {})
    })

    const { unmount } = renderHook(() => useApiPoll('/x', { intervalMs: 0 }))
    expect(seenSignals).toHaveLength(1)
    expect(seenSignals[0].aborted).toBe(false)

    unmount()
    expect(seenSignals[0].aborted).toBe(true)
  })

  it('AbortError 不被当作业务 error(降噪)', async () => {
    apiGetMock.mockReset()
    const abortErr = Object.assign(new Error('aborted'), { name: 'AbortError' })
    apiGetMock.mockRejectedValue(abortErr)
    const onError = vi.fn()
    const { result } = renderHook(() =>
      useApiPoll('/x', { intervalMs: 0, onError }),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.error).toBeNull()
    expect(onError).not.toHaveBeenCalled()
  })
})

describe('useApiPoll — 基础行为', () => {
  beforeEach(() => {
    apiGetMock.mockReset()
    apiGetMock.mockResolvedValue({ ok: 1 })
  })

  it('初次挂载立即拉取(immediate 默认 true)', async () => {
    const { result } = renderHook(() => useApiPoll('/x', { intervalMs: 0 }))
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    expect(apiGetMock).toHaveBeenCalledWith(
      '/x',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.data).toEqual({ ok: 1 })
  })

  it('immediate=false 时不立即拉', () => {
    renderHook(() => useApiPoll('/x', { intervalMs: 0, immediate: false }))
    expect(apiGetMock).not.toHaveBeenCalled()
  })

  it('enabled=false 时跳过立即拉与轮询', () => {
    renderHook(() => useApiPoll('/x', { intervalMs: 5_000, enabled: false }))
    expect(apiGetMock).not.toHaveBeenCalled()
  })

  it('lastFetchedAt 在成功拉取后被设置', async () => {
    const { result } = renderHook(() => useApiPoll('/x', { intervalMs: 0 }))
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(typeof result.current.lastFetchedAt).toBe('number')
    expect(result.current.lastFetchedAt).toBeGreaterThan(0)
  })
})

describe('useApiPoll — 轮询与 inFlight', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    apiGetMock.mockReset()
    apiGetMock.mockResolvedValue({ ok: 1 })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('intervalMs > 0 时按 interval 重复拉取', async () => {
    renderHook(() => useApiPoll('/x', { intervalMs: 5_000 }))
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    // 先让第一次 in-flight fetch 完成（inFlight 释放后定时器触发的新一轮才会真正发出）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(apiGetMock).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(apiGetMock).toHaveBeenCalledTimes(3)
  })

  it('deps 变化时立即重拉', async () => {
    const { rerender } = renderHook(
      ({ p }: { p: string }) =>
        useApiPoll('/api', { intervalMs: 0, deps: [p] }),
      { initialProps: { p: 'a' } },
    )
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    await act(async () => {
      rerender({ p: 'b' })
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(apiGetMock).toHaveBeenCalledTimes(2)
  })

  it('inFlight 防止重复发起(高并发 interval)', async () => {
    let resolveFn!: (v: unknown) => void
    apiGetMock.mockReset()
    apiGetMock.mockImplementation(
      () => new Promise((r) => { resolveFn = r }),
    )

    renderHook(() => useApiPoll('/x', { intervalMs: 100 }))
    expect(apiGetMock).toHaveBeenCalledTimes(1)

    await act(async () => {
      vi.advanceTimersByTime(100)
      await Promise.resolve()
    })
    expect(apiGetMock).toHaveBeenCalledTimes(1) // 上一轮未结束 → 跳过

    await act(async () => {
      vi.advanceTimersByTime(100)
      await Promise.resolve()
    })
    expect(apiGetMock).toHaveBeenCalledTimes(1) // 仍跳过

    await act(async () => {
      resolveFn({ ok: 1 })
      await Promise.resolve()
      await Promise.resolve()
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(apiGetMock).toHaveBeenCalledTimes(2)
  })
})
