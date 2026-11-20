'use client'

/**
 * useReflectionDrafts — 反思草稿状态机(v275+)。
 *
 * 把"打开 / 关闭 / 更新 / 保存 / 追问 / 插入问句"这 6 个动作抽成单独 hook,
 * 让主面板只剩数据加载 + 渲染。AI 追问走 /reflection/suggest-followups。
 *
 * 关键设计:
 *   - reflections 是 [decision_id:error, ReflectionDraft] 映射;同一条决策
 *     可能有多个错误码,各自独立草稿(key = decisionId:errorCode)
 *   - 关闭时若已写文字/改进/勾选 → 自动 saveDraft(保留);否则丢弃
 *   - requestFollowups 用 AbortController —— v275+ 接 useApiPoll 同款取消语义
 *     防止用户连点"让 AI 追问"造成多次请求竞态
 *   - lastError:状态字符串(供 UI 红字提示),不为 null 表示最近一次请求失败
 */

import { useCallback, useRef, useState } from 'react'
import { apiSend } from '@/lib/api'
import { emptyDraft, loadDraft, saveDraft, lsKey } from './draft-storage'

export interface ReflectionDraft {
  open: boolean
  text: string
  coreMarked: boolean
  improvementText: string
  aiQuestions: string[]
  loadingFollowups: boolean
  savedAt?: string
  lastError?: string
}

export function draftKey(decisionId: number, errorCode: string): string {
  return `${decisionId}:${errorCode}`
}

export interface UseReflectionDraftsResult {
  reflections: Record<string, ReflectionDraft>
  openReflection: (decisionId: number, errorCode: string) => void
  closeReflection: (decisionId: number, errorCode: string) => void
  updateDraft: (
    decisionId: number,
    errorCode: string,
    patch: Partial<ReflectionDraft>,
  ) => void
  saveReflection: (decisionId: number, errorCode: string) => void
  requestFollowups: (decisionId: number, errorCode: string) => Promise<void>
  insertQuestion: (decisionId: number, errorCode: string, q: string) => void
}

export function useReflectionDrafts(): UseReflectionDraftsResult {
  const [reflections, setReflections] = useState<Record<string, ReflectionDraft>>({})
  const followupAbortRef = useRef<AbortController | null>(null)
  // 保持最新 reflections 快照,避免 stale closure (requestFollowups 需要读 cur.text)
  const reflectionsRef = useRef<Record<string, ReflectionDraft>>({})
  reflectionsRef.current = reflections

  const openReflection = useCallback((decisionId: number, errorCode: string) => {
    const k = draftKey(decisionId, errorCode)
    setReflections((prev) => {
      const existing = prev[k] || loadDraft(decisionId, errorCode) || emptyDraft()
      return { ...prev, [k]: { ...existing, open: true } }
    })
  }, [])

  const closeReflection = useCallback((decisionId: number, errorCode: string) => {
    const k = draftKey(decisionId, errorCode)
    setReflections((prev) => {
      const cur = prev[k]
      if (!cur) return prev
      const next: ReflectionDraft = { ...cur, open: false }
      if (cur.text || cur.improvementText || cur.coreMarked) {
        saveDraft(decisionId, errorCode, next)
      }
      return { ...prev, [k]: next }
    })
  }, [])

  const updateDraft = useCallback(
    (
      decisionId: number,
      errorCode: string,
      patch: Partial<ReflectionDraft>,
    ) => {
      const k = draftKey(decisionId, errorCode)
      setReflections((prev) => {
        const cur = prev[k] || emptyDraft()
        return { ...prev, [k]: { ...cur, ...patch } }
      })
    },
    [],
  )

  const saveReflection = useCallback(
    (decisionId: number, errorCode: string) => {
      const k = draftKey(decisionId, errorCode)
      setReflections((prev) => {
        const cur = prev[k]
        if (!cur) return prev
        const stamped: ReflectionDraft = {
          ...cur,
          savedAt: new Date().toISOString(),
          lastError: '',
        }
        saveDraft(decisionId, errorCode, stamped)
        return { ...prev, [k]: stamped }
      })
    },
    [],
  )

  const requestFollowups = useCallback(
    async (decisionId: number, errorCode: string) => {
      const k = draftKey(decisionId, errorCode)
      const cur = reflectionsRef.current[k]
      if (!cur) return

      updateDraft(decisionId, errorCode, {
        loadingFollowups: true,
        lastError: '',
      })

      // 取消上一轮未结束的追问(避免重复返回覆盖 aiQuestions 顺序)
      if (followupAbortRef.current) followupAbortRef.current.abort()
      const controller = new AbortController()
      followupAbortRef.current = controller

      try {
        const data = await apiSend<{ questions?: string[] }>(
          '/reflection/suggest-followups',
          'POST',
          {
            decision_id: decisionId,
            error_code: errorCode,
            current_reflection: cur.text || '',
            n_suggest: 3,
          },
          { signal: controller.signal },
        )
        if (controller.signal.aborted) return
        updateDraft(decisionId, errorCode, {
          aiQuestions: data.questions || [],
          loadingFollowups: false,
        })
      } catch (e: any) {
        if (e?.name === 'AbortError') return
        updateDraft(decisionId, errorCode, {
          loadingFollowups: false,
          lastError: e?.message || 'failed',
        })
      } finally {
        if (followupAbortRef.current === controller) {
          followupAbortRef.current = null
        }
      }
    },
    [updateDraft],
  )

  const insertQuestion = useCallback(
    (decisionId: number, errorCode: string, q: string) => {
      const k = draftKey(decisionId, errorCode)
      setReflections((prev) => {
        const cur = prev[k]
        if (!cur) return prev
        const prefix = cur.text ? (cur.text.endsWith('\n') ? cur.text : cur.text + '\n') : ''
        const addition = prefix + '【问】' + q
        return { ...prev, [k]: { ...cur, text: addition } }
      })
    },
    [],
  )

  return {
    reflections,
    openReflection,
    closeReflection,
    updateDraft,
    saveReflection,
    requestFollowups,
    insertQuestion,
  }
}

/** 工具:同 debug 已 commit,导出给旧 import 兼容(避免循环依赖) */
export { lsKey }