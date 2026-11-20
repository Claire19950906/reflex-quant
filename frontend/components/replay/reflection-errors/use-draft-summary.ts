'use client'

/**
 * useDraftSummary — LS 里所有未提交反思草稿的总览(v302+ PR-15)。
 *
 * 设计动机:之前 useReflectionDrafts 只看"当前打开的会话",用户 LS 里
 * 残留多少草稿是黑盒。这个 hook 扫描所有 reflection-draft:* key,返回
 * 按 decisionId 分组的统计 + stale(>7天) + multiErrorDecisions(同决策
 * 多个 error code),给"草稿总览 banner"用。
 *
 * 关键设计:
 *  - useMemo([bump]) 缓存 listAllDrafts 结果,refresh() 触发重算
 *  - 监听 window 'storage' 事件(其他 tab / 同一 tab 改 LS 都触发)
 *  - stale = savedAt 早于 7 天前的草稿;无 savedAt 不算 stale
 *    (空草稿过滤已由 listAllDrafts 内部处理)
 *  - multiErrorDecisions = byDecision 里 entries.length >= 2 的子集
 *  - remove / clear 调底层 removeDraft + refresh,UI 立即反映
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  listAllDrafts,
  removeDraft,
  type DraftEntry,
  REFLECTION_DRAFT_LS_PREFIX,
} from './draft-storage'

/** 一个 decisionId 下所有草稿的聚合 */
export interface DraftGroupByDecision {
  decisionId: number
  entries: DraftEntry[]
  /** 最早 savedAt(有的话);无任何 savedAt → undefined */
  oldestSavedAt?: string
}

export interface DraftSummary {
  total: number
  /** 按 decisionId 分组,按 oldestSavedAt 升序(老的在前) */
  byDecision: DraftGroupByDecision[]
  /** >7天没更新的草稿(基于 savedAt) */
  stale: DraftEntry[]
  /** 同 decisionId 多个 error code 的组(>=2) */
  multiErrorDecisions: DraftGroupByDecision[]
  /** 强制重扫 */
  refresh: () => void
  /** 删除一条草稿 + 重扫 */
  remove: (decisionId: number, errorCode: string) => void
  /** 清空所有草稿 + 重扫 */
  clear: () => void
}

const STALE_DAYS = 7
const STALE_MS = STALE_DAYS * 24 * 60 * 60 * 1000

/** 工具:取一组里最早 savedAt(lex 比较 ISO8601 即可) */
function pickOldest(entries: DraftEntry[]): string | undefined {
  let oldest: string | undefined
  for (const e of entries) {
    if (!e.draft.savedAt) continue
    if (!oldest || e.draft.savedAt < oldest) oldest = e.draft.savedAt
  }
  return oldest
}

export function useDraftSummary(): DraftSummary {
  const [bump, setBump] = useState(0)
  const refresh = useCallback(() => setBump((b) => b + 1), [])

  // 跨 tab / 同 tab 跨组件的 LS 改动 → refresh
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onStorage = (e: StorageEvent) => {
      // 任意 reflection-draft:* 变化触发(也容忍 key=null 表示清空)
      if (!e.key || e.key.startsWith(REFLECTION_DRAFT_LS_PREFIX)) {
        refresh()
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [refresh])

  const all = useMemo(() => listAllDrafts(), [bump])

  const byDecision: DraftGroupByDecision[] = useMemo(() => {
    const map = new Map<number, DraftEntry[]>()
    for (const e of all) {
      const arr = map.get(e.decisionId) ?? []
      arr.push(e)
      map.set(e.decisionId, arr)
    }
    const groups: DraftGroupByDecision[] = []
    for (const [decisionId, entries] of map.entries()) {
      groups.push({ decisionId, entries, oldestSavedAt: pickOldest(entries) })
    }
    // 排序:有 savedAt 的在前(老 → 新),无 savedAt 的沉底按 decisionId
    groups.sort((a, b) => {
      if (a.oldestSavedAt && b.oldestSavedAt) {
        return a.oldestSavedAt.localeCompare(b.oldestSavedAt)
      }
      if (a.oldestSavedAt) return -1
      if (b.oldestSavedAt) return 1
      return a.decisionId - b.decisionId
    })
    return groups
  }, [all])

  const stale: DraftEntry[] = useMemo(() => {
    const cutoff = Date.now() - STALE_MS
    return all.filter((e) => {
      if (!e.draft.savedAt) return false
      const t = new Date(e.draft.savedAt).getTime()
      return Number.isFinite(t) && t < cutoff
    })
  }, [all])

  const multiErrorDecisions = useMemo(
    () => byDecision.filter((g) => g.entries.length >= 2),
    [byDecision],
  )

  const remove = useCallback(
    (decisionId: number, errorCode: string) => {
      removeDraft(decisionId, errorCode)
      refresh()
    },
    [refresh],
  )

  const clear = useCallback(() => {
    for (const e of all) removeDraft(e.decisionId, e.errorCode)
    refresh()
  }, [all, refresh])

  return {
    total: all.length,
    byDecision,
    stale,
    multiErrorDecisions,
    refresh,
    remove,
    clear,
  }
}
