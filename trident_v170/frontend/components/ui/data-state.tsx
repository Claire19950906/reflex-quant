'use client'

/**
 * DataState — 统一 loading / error / empty / success 四态容器（v170 P0+）
 *
 * 设计动机：review 页 9 个面板各自写：
 *   {loading && <Loader2 />}
 *   {error && <div className="text-destructive">{error}</div>}
 *   {data && <Content/>}
 * 共 9 套写法 + 9 种 spinner 风格 + 9 种错误。
 *
 * 统一后：
 *   <DataState loading={loading} error={error} empty={!data}>
 *     <Content data={data}/>
 *   </DataState>
 *
 * 兼容性：error 存在时 children 不渲染（避免 NPE）。
 * empty 时 children 渲染但传 data=null（允许组件做空态分支）。
 *
 * 状态优先级：error > loading > empty > children
 */

import { ReactNode } from 'react'
import { Loader2, AlertTriangle, Inbox } from 'lucide-react'
import { cn } from '@/lib/utils'

export type DataStateProps<T> = {
  /** 是否加载中（首次拉取） */
  loading?: boolean
  /** 错误信息（字符串或 Error） */
  error?: string | Error | null
  /** 是否为空（已加载但没有数据） */
  empty?: boolean
  /** 实际数据（用于 empty 判断与子组件渲染） */
  data?: T | null
  /** 自定义 loading 文案 */
  loadingLabel?: string
  /** 自定义 error 文案前缀 */
  errorPrefix?: string
  /** 自定义 empty 文案 */
  emptyLabel?: string
  /** 子组件 */
  children?: ReactNode | ((data: T) => ReactNode)
  /** 自定义类名（追加到根容器） */
  className?: string
  /** 紧凑模式（高度小、无 padding） */
  compact?: boolean
}

export function DataState<T>(props: DataStateProps<T>) {
  const {
    loading,
    error,
    empty,
    data,
    loadingLabel = '载入中…',
    errorPrefix = '⚠️',
    emptyLabel = '暂无数据',
    children,
    className,
    compact = false,
  } = props

  // ① error 态最优先 — children 不渲染
  if (error) {
    const msg = error instanceof Error ? error.message : String(error)
    return (
      <div
        role="alert"
        className={cn(
          'flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/5 font-mono text-xs text-destructive',
          compact ? 'px-2 py-1' : 'px-3 py-2',
          className,
        )}
      >
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className="truncate">{errorPrefix} {msg}</span>
      </div>
    )
  }

  // ② loading 态
  if (loading) {
    return (
      <div
        role="status"
        aria-live="polite"
        className={cn(
          'flex items-center gap-2 font-mono text-xs text-muted-foreground',
          compact ? 'px-2 py-1' : 'px-3 py-2',
          className,
        )}
      >
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
        <span>{loadingLabel}</span>
      </div>
    )
  }

  // ③ empty 态
  if (empty) {
    return (
      <div
        className={cn(
          'flex items-center gap-2 rounded-md border border-dashed border-border bg-secondary/20 font-mono text-xs text-muted-foreground',
          compact ? 'px-2 py-1' : 'px-3 py-2',
          className,
        )}
      >
        <Inbox className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>{emptyLabel}</span>
      </div>
    )
  }

  // ④ success 态 — 渲染 children
  if (typeof children === 'function') {
    // children 是函数：传 data 进去（注意 data 可能为 null/empty 时已被 ③ 截掉）
    return <>{children(data as T)}</>
  }
  return <>{children}</>
}
