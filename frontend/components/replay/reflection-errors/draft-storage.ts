/**
 * 自由反思草稿的 localStorage 持久化层(v275+)。
 *
 * 抽出来的理由:之前 760 行的 reflection-errors-panel.tsx 把 LS 读写、组件渲染、
 * AI 追问、网络请求混在一个文件,任何改动都要在巨型 React 组件里跳来跳去。
 * 拆出来后:
 *   - lsKey / loadDraft / saveDraft 是纯函数,单测可覆盖(SSR / 解析失败 / 配额超限)
 *   - useReflectionDrafts hook 单独管"打开/更新/保存/追问"的状态机
 *   - 主组件只剩数据加载 + 渲染
 *
 * v302+ PR-15: 加 listAllDrafts / removeDraft — 给"草稿总览"用,
 *   让用户能看到 LS 里还剩哪些未提交草稿(尤其是多 decision / 多 error code 时)。
 */

import type { ReflectionDraft } from './use-reflection-drafts'

export const REFLECTION_DRAFT_LS_PREFIX = 'reflection-draft:'

/** localStorage key — key = decision_id:error_code */
export function lsKey(decisionId: number, errorCode: string): string {
  return `${REFLECTION_DRAFT_LS_PREFIX}${decisionId}:${errorCode}`
}

/** 解析 LS key 拿回 (decisionId, errorCode) 二元组 */
export function parseLsKey(
  key: string,
): { decisionId: number; errorCode: string } | null {
  if (!key.startsWith(REFLECTION_DRAFT_LS_PREFIX)) return null
  const rest = key.slice(REFLECTION_DRAFT_LS_PREFIX.length)
  const idx = rest.indexOf(':')
  if (idx <= 0) return null
  const decisionId = Number(rest.slice(0, idx))
  if (!Number.isFinite(decisionId) || !Number.isInteger(decisionId)) return null
  return { decisionId, errorCode: rest.slice(idx + 1) }
}

/**
 * 从 LS 读 — 失败 / 不存在 / 不是对象 一律返回 null,不抛。
 *
 * SSR 安全:typeof window === 'undefined' → null,Next build 不会爆。
 * 解析失败:JSON.parse 异常 → null,允许"返回崩态"但保留 UI 渲染。
 */
export function loadDraft(decisionId: number, errorCode: string): ReflectionDraft | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(lsKey(decisionId, errorCode))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return typeof parsed === 'object' && parsed ? (parsed as ReflectionDraft) : null
  } catch {
    return null
  }
}

/**
 * 持久化到 LS — 写入失败被吞,不抛。
 *
 * 注意:open 字段强制 false(刷新页面默认收起,避免视觉错位)。
 */
export function saveDraft(decisionId: number, errorCode: string, draft: ReflectionDraft): void {
  if (typeof window === 'undefined') return
  try {
    const persist: ReflectionDraft = { ...draft, open: false }
    window.localStorage.setItem(lsKey(decisionId, errorCode), JSON.stringify(persist))
  } catch {
    // quota exceeded / 隐私模式 / disabled storage → 静默吞
  }
}

/** v302+: 删除一个草稿(LS key) — 写失败被吞 */
export function removeDraft(decisionId: number, errorCode: string): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.removeItem(lsKey(decisionId, errorCode))
  } catch {
    // 隐私模式 / disabled storage → 静默吞
  }
}

/** v302+: 列出 LS 里所有 reflection 草稿 — 给"草稿总览"用。 */
export type DraftEntry = {
  decisionId: number
  errorCode: string
  draft: ReflectionDraft
}

/**
 * 扫描 localStorage,提取所有 reflection-draft:* 草稿。
 *
 * 设计要点:
 *  - SSR 安全:无 window → 空数组
 *  - 解析失败:key 跳过(不抛),保证坏数据不影响整体
 *  - 按 decisionId 升序,再按 errorCode 字典序 — UI 列表稳定
 *  - 过滤空草稿:text 空 + coreMarked=false + improvementText 空 + 无 aiQuestions
 *    (用户可能误点打开再关,会留下空 entry,不应该出现在总览里)
 */
export function listAllDrafts(): DraftEntry[] {
  if (typeof window === 'undefined') return []
  const out: DraftEntry[] = []
  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i)
      if (!key) continue
      const parsed = parseLsKey(key)
      if (!parsed) continue
      const raw = window.localStorage.getItem(key)
      if (!raw) continue
      let d: ReflectionDraft
      try {
        d = JSON.parse(raw) as ReflectionDraft
      } catch {
        continue
      }
      if (!d || typeof d !== 'object') continue
      // 过滤空草稿
      if (
        !d.text?.trim() &&
        !d.improvementText?.trim() &&
        !d.coreMarked &&
        (!d.aiQuestions || d.aiQuestions.length === 0)
      ) {
        continue
      }
      out.push({ decisionId: parsed.decisionId, errorCode: parsed.errorCode, draft: d })
    }
  } catch {
    // 读取整个 LS 失败(隐私模式 / quota 异常)→ 返回已收集的
  }
  out.sort((a, b) => {
    if (a.decisionId !== b.decisionId) return a.decisionId - b.decisionId
    return a.errorCode.localeCompare(b.errorCode)
  })
  return out
}

/** 默认空草稿(可由调用方覆盖任意字段) */
export function emptyDraft(): ReflectionDraft {
  return {
    open: true,
    text: '',
    coreMarked: false,
    improvementText: '',
    aiQuestions: [],
    loadingFollowups: false,
  }
}