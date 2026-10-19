'use client'

/**
 * v302+ P1 重构: 把 /review 页面 6 个 URL 状态键 (tab / ab / err / period / from / to)
 * 集中到一个 reducer + hook,关键收益:
 *  - 单次 router.replace 原子写多键 (修复原版 setCustomDateRange 3 次连续 replace 的
 *    中间态问题: 用户切到 custom 时如果 router.replace('from'…) 完成而 'period' 未写,
 *    URL 会短时间卡在 'from=X&to=Y&period=today' 不一致)
 *  - 解析 / 序列化逻辑纯函数化,便于单测 (readFromUrl / applyPatch)
 *  - 集中默认值与白名单 (tab / period 的合法值校验只在一处)
 *
 * 不上 useState/useReducer 内部状态:URL 是 source of truth,useReducer 内部
 * state 跟 URL 双轨会需要 useEffect 同步,徒增复杂度。这里"reducer"是
 * applyPatch 这个纯函数,dispatch = setter 包装 (router.replace 是副作用,
 * 由 React 事件回调触发,符合 useReducer 模型)。
 */

import { useCallback, useMemo } from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import type { PeriodKey } from './data-channel-standards'

export type ReviewTab = 'all' | 'experiences' | 'journal'
export type AbTab = 'a' | 'b'

export const DEFAULT_PERIOD: PeriodKey = 'today'
export const VALID_TABS: readonly ReviewTab[] = ['all', 'experiences', 'journal'] as const
export const VALID_PERIODS: readonly PeriodKey[] = ['today', '7d', '30d', 'custom'] as const

export type ReviewUrlState = {
  tab: ReviewTab
  ab: AbTab
  err: string | null
  period: PeriodKey
  /** 解析后的 from/to;period !== 'custom' 时为默认 7 天窗口 */
  customRange: { fromDate: string; toDate: string }
}

/** 单个 patch 操作:set(key, value) / unset(key) */
export type UrlPatch = Array<[string, string | null]>

/** 纯函数: 读 URLSearchParams → 解析为强类型 state */
export function readFromUrl(
  sp: URLSearchParams | null | undefined,
  defaultRange: { fromDate: string; toDate: string },
): ReviewUrlState {
  const tabRaw = (sp?.get('tab') ?? 'all').toLowerCase()
  const tab: ReviewTab = (VALID_TABS as readonly string[]).includes(tabRaw)
    ? (tabRaw as ReviewTab)
    : 'all'
  const ab: AbTab = sp?.get('ab') === 'b' ? 'b' : 'a'
  const err = sp?.get('err') ?? null
  const periodRaw = (sp?.get('period') ?? DEFAULT_PERIOD).toLowerCase()
  const period: PeriodKey = (VALID_PERIODS as readonly string[]).includes(periodRaw)
    ? (periodRaw as PeriodKey)
    : 'today'
  const from = sp?.get('from')
  const to = sp?.get('to')
  const customRange =
    period === 'custom' && from && to ? { fromDate: from, toDate: to } : defaultRange
  return { tab, ab, err, period, customRange }
}

/** 纯函数: (prevSearchParams, patch) → nextSearchParams
 *  patch = [['key', 'value'], ['key2', null]]  (null = 删除) */
export function applyPatch(
  sp: URLSearchParams | null | undefined,
  patch: UrlPatch,
): URLSearchParams {
  const next = new URLSearchParams(sp?.toString() ?? '')
  for (const [k, v] of patch) {
    if (v == null || v === '') next.delete(k)
    else next.set(k, v)
  }
  return next
}

/** 默认 custom range = 今天 + 7 天前(用户进入 /review 且未指定时显示的窗口) */
function defaultCustomRange(): { fromDate: string; toDate: string } {
  const today = new Date()
  const sevenAgo = new Date(today.getTime() - 7 * 24 * 3600 * 1000)
  const fmt = (d: Date) => {
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    return `${y}-${m}-${day}`
  }
  return { fromDate: fmt(sevenAgo), toDate: fmt(today) }
}

/** /review 专用 hook: 集中管理 6 个 URL 键 */
export function useReviewUrlState() {
  const sp = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()

  const defaultRange = useMemo(() => defaultCustomRange(), [])

  // 读: sp 变化触发重 parse
  const state = useMemo(() => readFromUrl(sp, defaultRange), [sp, defaultRange])

  // 写: 一次 router.replace 原子写多键
  const writeUrl = useCallback(
    (patch: UrlPatch) => {
      const next = applyPatch(sp, patch)
      const qs = next.toString()
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false })
    },
    [sp, router, pathname],
  )

  // 高阶 setter: 把语义化 next 值 → URL patch
  const setTab = useCallback(
    (next: ReviewTab) => writeUrl([['tab', next === 'all' ? null : next]]),
    [writeUrl],
  )
  const setAbTab = useCallback(
    (next: AbTab) => writeUrl([['ab', next === 'a' ? null : next]]),
    [writeUrl],
  )
  const setErrorCode = useCallback(
    (next: string | null) => writeUrl([['err', next]]),
    [writeUrl],
  )
  const setPeriod = useCallback(
    (next: PeriodKey) => writeUrl([['period', next === DEFAULT_PERIOD ? null : next]]),
    [writeUrl],
  )
  /** ⚡ 原子写 3 键: from + to + period=custom。修复原版 3 次 replace 中间态。 */
  const setCustomDateRange = useCallback(
    (next: { fromDate: string; toDate: string }) => {
      writeUrl([
        ['from', next.fromDate],
        ['to', next.toDate],
        ['period', 'custom'],
      ])
    },
    [writeUrl],
  )

  return useMemo(
    () => ({
      ...state,
      setTab,
      setAbTab,
      setErrorCode,
      setPeriod,
      setCustomDateRange,
    }),
    [state, setTab, setAbTab, setErrorCode, setPeriod, setCustomDateRange],
  )
}
