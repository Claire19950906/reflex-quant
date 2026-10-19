'use client'

/**
 * usePoll — 统一的轮询 hook（v283 性能优化基础 · 发现页 12 panel 共用）
 *
 * 设计动机：发现页 12 个子组件每个都重复写了：
 *   useState + useEffect + useCallback + setInterval + reload + abort flag
 * 普遍存在 3 类问题：
 *   ❶ 没有 abort flag，组件 unmount 后还 setState（React 18 warning）
 *   ❷ cycle-radar.tsx:93 用 clearTimeout 清理 setInterval（语义错）
 *   ❸ 上一轮请求未取消，新一轮已开始，可能导致 setState 顺序错乱
 *
 * 本 hook 一次性解决以上全部问题，统一 12 个 panel 的轮询模式。
 *
 * 用法：
 *   const { data, loading, error, lastFetchedAt, refresh } = usePoll<MyType>(
 *     '/discovery/cycle-scan',
 *     { intervalMs: 600_000 },
 *   )
 *
 *   // 自定义 transform（后端返回 ok=false 时过滤）
 *     usePoll<MyType>('/x', {
 *       intervalMs: 60_000,
 *       transform: (raw) => (raw && raw.ok) ? raw : null,
 *     })
 *
 *   // 多字段依赖（外部参数变了，重拉）
 *     usePoll('/x', { intervalMs: 5000, deps: [paramA, paramB] })
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE } from '@/lib/api'

export interface UsePollOptions<T> {
  /** 轮询间隔（ms）；设为 0 表示只拉一次不轮询 */
  intervalMs: number
  /**
   * 自定义 transform：把原始 JSON 转成 T | null。
   * 返回 null 表示「本次拉取当作失败/无数据」（不抛错，error 也不设置）。
   * 默认：如果 raw 有 ok 字段则取 raw，否则 raw 本身。
   */
  transform?: (raw: unknown) => T | null
  /** 是否在 mount 后立即拉一次（默认 true；intervalMs=0 时只拉一次） */
  immediate?: boolean
  /**
   * 依赖项：变化时立即重拉（类似 useEffect dep array）
   * 典型用法：把 URL query 参数放进来。
   */
  deps?: ReadonlyArray<unknown>
  /** fetch 额外参数（method / headers / body） */
  fetchInit?: Omit<RequestInit, 'signal' | 'cache'>
  /** cache 模式，默认 `no-store`（发现页全要实时） */
  cache?: RequestCache
  /**
   * v283+ · ETag 协商。`true` 时：
   *   - 首次响应记下 ETag
   *   - 后续请求自动带 `If-None-Match`
   *   - 服务端返回 304 时直接复用旧 data（不发 setLoading）
   * 省 99% 带宽(只有 id+updated_at 哈希 16 字节,响应体空)。
   */
  useETag?: boolean
  /**
   * 当 raw 失败时是否调用 onError；不传则把错误信息写到内部 state。
   * 注意：fetch 失败 / transform 返回 null 都视为「失败」。
   */
  onError?: (msg: string) => void
  /**
   * v283+ · 是否启用。`false` 时既不立即拉也不轮询（但仍订阅 deps，
   * 一旦变 true 就立即拉）。典型用法：scout 主源失败时才启用 fallback 源。
   */
  enabled?: boolean
}

export interface UsePollResult<T> {
  data: T | null
  loading: boolean
  error: string | null
  /** 最后一次成功拉取的时间戳（ms，Date.now()） */
  lastFetchedAt: number | null
  /** 主动触发重拉（不等待下次 interval） */
  refresh: () => Promise<void>
  /** v283+ · 当前 ETag (若 useETag=true 且服务端返回了);304 时不变 */
  etag: string | null
}

export function usePoll<T = unknown>(
  path: string,
  opts: UsePollOptions<T>,
): UsePollResult<T> {
  const {
    intervalMs,
    transform,
    immediate = true,
    deps = [],
    fetchInit,
    cache = 'no-store',
    useETag = false,  // v283+ · ETag 协商(true 时自动带 If-None-Match)
    onError,
    enabled = true,
  } = opts

  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState<boolean>(immediate)
  const [error, setError] = useState<string | null>(null)
  const [lastFetchedAt, setLastFetchedAt] = useState<number | null>(null)

  // mounted ref：组件 unmount 后 setState 自然无效，避免 React warning
  const mountedRef = useRef(true)
  // 当前进行中的 controller：新一轮开始前 abort 上一轮
  const ctrlRef = useRef<AbortController | null>(null)
  // v283+ · ETag 状态(由 useETag 启用,服务端返回时设置,304 时不变)
  const etagRef = useRef<string | null>(null)

  // 保持最新的回调（onError / transform），避免每次 rebuild 导致 effect 重启
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError
  const transformRef = useRef(transform)
  transformRef.current = transform

  const doFetch = useCallback(async () => {
    // 取消上一轮仍在跑的请求
    if (ctrlRef.current) {
      try { ctrlRef.current.abort() } catch { /* noop */ }
    }
    const ctrl = new AbortController()
    ctrlRef.current = ctrl

    setLoading(true)
    setError(null)
    try {
      const url = path.startsWith('http') ? path : `${API_BASE}${path}`
      const fetchHeaders: Record<string, string> = {}
      if (useETag && etagRef.current) {
        fetchHeaders['If-None-Match'] = etagRef.current
      }
      const r = await fetch(url, {
        ...fetchInit,
        cache,
        signal: ctrl.signal,
        headers: { ...fetchInit?.headers, ...fetchHeaders },
      })
      // v283+ · 304 Not Modified:服务端说内容没变,复用旧 data(零带宽)
      if (useETag && r.status === 304) {
        if (mountedRef.current) {
          setLastFetchedAt(Date.now())
          setLoading(false)
        }
        return
      }
      // v283+ · 记录本次响应的 ETag(供下次带 If-None-Match)
      const respEtag = useETag ? r.headers.get('ETag') : null
      if (respEtag && respEtag.trim() && respEtag.trim() !== etagRef.current) {
        etagRef.current = respEtag.trim()
      }
      if (!r.ok) {
        const msg = `${path} HTTP ${r.status}`
        if (mountedRef.current) {
          setError(msg)
          setLoading(false)
        }
        onErrorRef.current?.(msg)
        return
      }
      const raw = await r.json()
      // transform：用户自定义 OR 默认按 ok 字段
      const next = transformRef.current
        ? transformRef.current(raw)
        : ((raw as { ok?: boolean })?.ok === false ? null : (raw as T))
      if (!mountedRef.current) return
      if (next === null || next === undefined) {
        // transform 主动判定为失败
        const msg = `${path} returned null/ok=false`
        setError(msg)
        setLoading(false)
        onErrorRef.current?.(msg)
        return
      }
      setData(next)
      setLastFetchedAt(Date.now())
      setError(null)
      setLoading(false)
    } catch (cause) {
      // AbortError 不算错
      if (cause instanceof DOMException && cause.name === 'AbortError') return
      const msg = cause instanceof Error ? cause.message : String(cause)
      if (mountedRef.current) {
        setError(msg)
        setLoading(false)
      }
      onErrorRef.current?.(msg)
    }
  }, [path, fetchInit, cache, useETag]) // v283+ · useETag 进 deps(虽然读 ref,但 useCallback 仍需重 build)

  useEffect(() => {
    mountedRef.current = true
    if (!enabled) {
      // 不拉取也不轮询;但保留 cleanup(unmount 时清空状态)
      return () => {
        mountedRef.current = false
        if (ctrlRef.current) {
          try { ctrlRef.current.abort() } catch { /* noop */ }
        }
      }
    }
    if (immediate) {
      void doFetch()
    }
    if (intervalMs > 0) {
      const id = window.setInterval(() => { void doFetch() }, intervalMs)
      return () => {
        mountedRef.current = false
        window.clearInterval(id)
        if (ctrlRef.current) {
          try { ctrlRef.current.abort() } catch { /* noop */ }
        }
      }
    }
    // intervalMs = 0 → 只拉一次
    return () => {
      mountedRef.current = false
      if (ctrlRef.current) {
        try { ctrlRef.current.abort() } catch { /* noop */ }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, doFetch, enabled, ...deps])

  return { data, loading, error, lastFetchedAt, refresh: doFetch, etag: etagRef.current }
}