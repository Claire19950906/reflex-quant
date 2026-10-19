// lib/hooks/use-news-impact.ts — v302 stub → 真实实现 (2026-10-18 改造)
//
// 原 HEAD 文件是 v302 stub（仅返回 items=[] / loading=false / error=null），
// 真实"轮询 /events + adapter"逻辑从未存在过仓库里（无 git 历史）。
// 本次改造按 stub 顶部"真实 useNewsImpact 行为（用户原版）"注释还原：
//  - 每 refreshMs 轮询 /api/events
//  - 用 fetchInternalNews (lib/news-data.ts 现成) + 内联 adapter 适配 NewsImpactItem
//  - abort-like on unmount (用 cancelled flag + mountedRef 阻止 stale setState)
//  - 失败时 error 字段供 page top alert 用
//
// 改动记录（2026-10-18）：
//  - 把"不做事"的 useEffect 替换为真实 fetch 逻辑
//  - 引入 fetchInternalNews (lib/news-data.ts 现成) + InternalNews 类型
//  - 新增 adapter: InternalNews[] → NewsImpactItem[]
//  - 用 cancelled flag + mountedRef 做卸载清理（fetchInternalNews 不传 AbortSignal）
//  - refresh callback 触发一次立即重拉（保留 page 顶部 Retry 按钮语义）
//
// 不动：
//  - UseNewsImpactOptions / UseNewsImpactResult 类型签名（callers 不变）
//  - 默认参数 (refreshMs=60_000, enabled=true)
//  - app/attribution/page.tsx 调用方

'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchInternalNews, type InternalNews } from '@/lib/news-data'
import type { NewsImpactItem } from '@/components/news/news-impact-weight'

export type UseNewsImpactOptions = {
  refreshMs?: number
  enabled?: boolean
}

export type UseNewsImpactResult = {
  items: NewsImpactItem[]
  loading: boolean
  error: string | null
  lastUpdated: number | null
  refresh: () => void
}

/**
 * adapter: InternalNews → NewsImpactItem
 *  - weight = |score| (score ∈ [-1, 1])
 *  - sentiment = action (BUY=+1, SELL=-1, HOLD=0)
 *  - title = news_text 前 80 字
 *  - asset 字段 InternalNews 不带 → undefined
 *    (entity 聚合由展示组件 entity-impact-panel 处理)
 *  - raw 字段保留整条 InternalNews，方便下游展示时取 source / timestamp 等
 */
function adapt(rows: InternalNews[]): NewsImpactItem[] {
  return rows.map((it) => ({
    id: it.id,
    asset: undefined,
    title: it.news_text.slice(0, 80),
    weight: Math.abs(it.score),
    sentiment: it.action === 'BUY' ? 1 : it.action === 'SELL' ? -1 : 0,
    created_at: it.timestamp,
    source: it.source,
    action: it.action,
    raw: it,
  }))
}

export function useNewsImpact(opts: UseNewsImpactOptions = {}): UseNewsImpactResult {
  const { refreshMs = 60_000, enabled = true } = opts
  const [items, setItems] = useState<NewsImpactItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [tick, setTick] = useState(0)
  // mounted 标志: 防止 unmount 后 setState 触发 React 18 警告
  const mountedRef = useRef(false)

  // refresh 回调：page 顶部 Retry 按钮 / 外部触发一次立即重拉
  const refresh = useCallback(() => {
    setTick((t) => t + 1)
  }, [])

  // mounted 生命周期
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // 主 effect: tick 变化触发一次拉取
  // 用 cancelled flag 在 effect 清理时阻止 stale setState (cleanup-on-next-render)
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    setLoading(true)
    fetchInternalNews()
      .then((rows) => {
        if (cancelled || !mountedRef.current) return
        setItems(adapt(rows))
        setError(null)
        setLastUpdated(Date.now())
      })
      .catch((e) => {
        if (cancelled || !mountedRef.current) return
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (cancelled || !mountedRef.current) return
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // tick 进 deps：refresh() 调用让 tick++，触发下一次拉取
    // refreshMs 进 deps：外部调整间隔时立即重新建立
  }, [enabled, refreshMs, tick])

  // 周期轮询：每 refreshMs ms 让 tick++, 主 effect 再触发拉取
  useEffect(() => {
    if (!enabled) return
    const id = setInterval(() => {
      setTick((t) => t + 1)
    }, refreshMs)
    return () => clearInterval(id)
  }, [enabled, refreshMs])

  return { items, loading, error, lastUpdated, refresh }
}
