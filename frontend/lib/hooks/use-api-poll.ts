'use client'

/**
 * useApiPoll — 统一的「轮询 + apiGet 错误处理」hook（v170 P0+）
 *
 * 设计动机：review/replay 路由下 9+ 个面板普遍写：
 *   useEffect(() => { fetch(path, { cache: 'no-store' }).then(r => r.ok ? r.json() : ...) })
 *   + window.setInterval(reload, 10000)
 * 复制粘贴 9 套，且普遍缺：abort flag / 错误状态 / ETag / enabled 开关。
 *
 * 抽出来后：
 *   const { data, loading, error, refresh } = useApiPoll<MyType>(
 *     '/review/reports',
 *     { intervalMs: 10_000, deps: [period] },
 *   )
 *
 * 与 usePoll 关系：useApiPoll 是 usePoll 的 apiGet 化姊妹 hook，
 *   解决 usePoll 内部硬编码 fetch 的问题 —— 让所有轮询统一走 apiGet，
 *   错误处理与 9 个面板一致（throw new Error(path status)）。
 *
 * 【v275+】AbortController 升级：
 *   - 每次 fetch 创建新 AbortController,挂到 abortRef
 *   - enabled → false / path 改变 / deps 改变 / unmount → 自动 abort 旧请求
 *   - AbortError 静默吞掉(不写入 state.error,不算业务错误)
 *   - API 完全兼容 v170,既有 9 个面板无需改动
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet } from '@/lib/api'

export interface UseApiPollOptions<T> {
  /** 轮询间隔（ms）；0 表示只拉一次不轮询 */
  intervalMs: number
  /** mount 后是否立即拉一次（默认 true） */
  immediate?: boolean
  /** 依赖项变化时立即重拉 */
  deps?: ReadonlyArray<unknown>
  /** 错误回调（默认把错误写到内部 state.error） */
  onError?: (msg: string) => void
  /** 启用开关；false 时跳过立即拉与轮询（但仍订阅 deps） */
  enabled?: boolean
  /**
   * 可选 transform：把 apiGet 返回的 T 转成 U。
   * 典型用法：raw 是 wrapper {items, count}，转成 items 数组。
   * 返回 null 表示当作失败（不抛错，error 也不设置）。
   */
  transform?: (data: T) => T | null
}

export interface UseApiPollResult<T> {
  data: T | null
  loading: boolean
  error: string | null
  /** 最后一次成功拉取的时间戳（ms，Date.now()） */
  lastFetchedAt: number | null
  /** 主动触发重拉（不等待下次 interval） */
  refresh: () => Promise<void>
}

/**
 * useApiPoll —— 统一「轮询 + apiGet」hook。
 *
 * 设计要点：
 *   - fetch 替换为 apiGet：错误抛出与 9 个面板一致
 *   - mounted flag：组件 unmount 后 setState 自然无效
 *   - inFlight flag：新一轮开始前跳过上一轮（避免 setState 顺序错乱）
 *   - deps：依赖项变化立即重拉（类似 useEffect dep array）
 *   - enabled：暂停轮询而不卸载（如 panel 关闭时不重拉）
 *   - 【v275+】AbortController：每请求自带 signal,enabled/deps/path 改变时取消旧请求
 */
export function useApiPoll<T = unknown>(
  path: string,
  opts: UseApiPollOptions<T>,
): UseApiPollResult<T> {
  const {
    intervalMs,
    immediate = true,
    deps = [],
    onError,
    enabled = true,
    transform,
  } = opts

  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState<boolean>(immediate && enabled)
  const [error, setError] = useState<string | null>(null)
  const [lastFetchedAt, setLastFetchedAt] = useState<number | null>(null)

  const mountedRef = useRef(true)
  const inFlightRef = useRef(false)
  // v275+: 持有当前请求的 AbortController,enabled/path/deps 变化或 unmount 时取消
  const abortRef = useRef<AbortController | null>(null)
  const transformRef = useRef(transform)
  transformRef.current = transform
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError

  const doFetch = useCallback(async () => {
    if (inFlightRef.current) return  // 上一轮未结束，跳过避免顺序错乱
    inFlightRef.current = true
    setLoading(true)
    setError(null)
    // v275+: 新 AbortController,确保可被 cancelInFlight 取消
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const raw = await apiGet<T>(path, { signal: controller.signal })
      if (!mountedRef.current) return
      if (controller.signal.aborted) return  // 二次保险
      const result = transformRef.current ? transformRef.current(raw) : raw
      if (result == null) return  // transform 返回 null 当作无数据
      setData(result)
      setLastFetchedAt(Date.now())
    } catch (e) {
      if (!mountedRef.current) return
      // v275+: AbortError 是"我们主动取消",不算业务错误 → 静默吞掉
      if (e instanceof Error && e.name === 'AbortError') return
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      onErrorRef.current?.(msg)
    } finally {
      inFlightRef.current = false
      if (abortRef.current === controller) {
        abortRef.current = null
      }
      if (mountedRef.current) setLoading(false)
    }
  }, [path])

  // v275+: 取消 in-flight 工具函数 — 复用控制器,避免每处重复写 abort 调用
  const cancelInFlight = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
  }, [])

  // mount 标志 — unmount 时取消飞行中请求(防止 React 18 setState-on-unmounted warning)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      cancelInFlight()
    }
  }, [cancelInFlight])

  // 立即拉 + 依赖变化时重拉 + enabled 切换 (false 时取消)
  useEffect(() => {
    if (!enabled) {
      cancelInFlight()
      setLoading(false)
      return
    }
    if (immediate) void doFetch()
    return () => {
      cancelInFlight()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doFetch, enabled, immediate, ...deps])

  // 定时轮询
  useEffect(() => {
    if (!enabled || intervalMs <= 0) return
    const timer = window.setInterval(() => void doFetch(), intervalMs)
    return () => window.clearInterval(timer)
  }, [doFetch, intervalMs, enabled])

  return { data, loading, error, lastFetchedAt, refresh: doFetch }
}