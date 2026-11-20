'use client'

/**
 * FedWatchContext — fed-watch 数据共享层（v283 性能优化 · 解决 #2）
 *
 * 设计动机：
 *   之前 `fed-watch.tsx` 和 `fed-mailbox-panel.tsx` 各自 60s 轮询同一个接口
 *   `/api/discovery/fed-watch`，30s 内同接口被请求两次（浪费带宽 + 串联负载）。
 *
 *   用 React Context 抽出一份共享 state，两个 panel 都 `useFedWatch()` 读。
 *   Provider 放在 `/discover/page.tsx` 包住整个 DiscoverySection。
 *
 * 类型策略：
 *   不在 context 里 import FieldSchema 类型（避免循环依赖 + 类型耦合）。
 *   Provider 是泛型 `<T>`，使用方在 page.tsx 注入具体类型（FedWatchResponse）。
 *   useFedWatch<T>() 调用方声明类型，ctx 内部用 unknown 占位，运行时是同一份 state。
 */

import { createContext, useContext, type ReactNode } from 'react'
import { usePoll } from '@/lib/hooks/use-poll'

export interface FedWatchContextValue<T = unknown> {
  data: T | null
  loading: boolean
  error: string | null
  lastFetchedAt: number | null
  refresh: () => Promise<void>
}

const FedWatchContext = createContext<FedWatchContextValue<unknown> | null>(null)

export interface FedWatchProviderProps<T> {
  children: ReactNode
  /** 默认 transform：ok=false 视为「拉取失败」，让 hook 自动设 error */
  transform?: (raw: unknown) => T | null
  /** 覆盖默认 60s 轮询间隔 */
  intervalMs?: number
}

/**
 * 在 page.tsx 顶层用一次：<FedWatchProvider<MyType>>...</FedWatchProvider>
 * Provider 内部用 usePoll，60s 轮询；共享给所有子组件 useFedWatch() 读取。
 */
export function FedWatchProvider<T>({
  children,
  transform,
  intervalMs = 60_000,
}: FedWatchProviderProps<T>) {
  const fedWatch = usePoll<T>('/discovery/fed-watch', {
    intervalMs,
    transform,
  })
  return (
    <FedWatchContext.Provider value={fedWatch as FedWatchContextValue<unknown>}>
      {children}
    </FedWatchContext.Provider>
  )
}

/** 调用方声明类型；未在 Provider 内则报错，避免 silent fallthrough */
export function useFedWatch<T = unknown>(): FedWatchContextValue<T> {
  const ctx = useContext(FedWatchContext)
  if (!ctx) {
    throw new Error(
      'useFedWatch() must be used within <FedWatchProvider>. ' +
        '请在 /discover/page.tsx 外层包一次 Provider。',
    )
  }
  return ctx as FedWatchContextValue<T>
}