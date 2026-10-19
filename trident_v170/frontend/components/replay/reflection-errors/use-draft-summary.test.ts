/**
 * useDraftSummary hook 测试 —— 草稿总览(v302+ PR-15)。
 *
 * 覆盖:空 / 分组 / 排序 / stale / remove / clear / storage event。
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useDraftSummary } from './use-draft-summary'
import { saveDraft, emptyDraft, lsKey } from './draft-storage'

beforeEach(() => {
  localStorage.clear()
})

describe('useDraftSummary — empty', () => {
  it('LS 空 → total=0,各数组空', () => {
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.total).toBe(0)
    expect(result.current.byDecision).toEqual([])
    expect(result.current.stale).toEqual([])
    expect(result.current.multiErrorDecisions).toEqual([])
  })
})

describe('useDraftSummary — grouping', () => {
  it('多个 decision → 按 decisionId 分组', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'b' })
    saveDraft(3, 'E002', { ...emptyDraft(), text: 'c' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.total).toBe(3)
    expect(result.current.byDecision).toHaveLength(3)
    expect(result.current.byDecision.map((g) => g.decisionId).sort()).toEqual([1, 2, 3])
  })

  it('同 decision 多 error code → 一组多 entries', () => {
    saveDraft(7, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(7, 'E002', { ...emptyDraft(), text: 'b' })
    saveDraft(7, 'E003', { ...emptyDraft(), text: 'c' })
    const { result } = renderHook(() => useDraftSummary())
    const g = result.current.byDecision.find((x) => x.decisionId === 7)
    expect(g?.entries).toHaveLength(3)
  })

  it('multiErrorDecisions 仅含 >=2 entries 的组', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'b' })
    saveDraft(2, 'E002', { ...emptyDraft(), text: 'c' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.multiErrorDecisions).toHaveLength(1)
    expect(result.current.multiErrorDecisions[0].decisionId).toBe(2)
  })
})

describe('useDraftSummary — sorting', () => {
  it('有 savedAt 的组按 oldest 升序(老的在前)', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a', savedAt: '2024-01-10T00:00:00Z' })
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'b', savedAt: '2024-01-01T00:00:00Z' })
    saveDraft(3, 'E001', { ...emptyDraft(), text: 'c', savedAt: '2024-01-05T00:00:00Z' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.byDecision.map((g) => g.decisionId)).toEqual([2, 3, 1])
  })

  it('无 savedAt 的组沉底,按 decisionId 升序', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'b' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.byDecision.map((g) => g.decisionId)).toEqual([1, 2])
  })
})

describe('useDraftSummary — stale', () => {
  it('savedAt < 7 天前 → 进 stale', () => {
    const old = new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString()
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'old', savedAt: old })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.stale).toHaveLength(1)
    expect(result.current.stale[0].decisionId).toBe(1)
  })

  it('savedAt 3 天前 → 不算 stale', () => {
    const fresh = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString()
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'fresh', savedAt: fresh })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.stale).toHaveLength(0)
  })

  it('无 savedAt → 不算 stale', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'no-time' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.stale).toHaveLength(0)
  })
})

describe('useDraftSummary — remove', () => {
  it('remove(decisionId, errorCode) → LS 删除 + total 减 1', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(1, 'E002', { ...emptyDraft(), text: 'b' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.total).toBe(2)
    act(() => result.current.remove(1, 'E001'))
    expect(result.current.total).toBe(1)
    expect(localStorage.getItem(lsKey(1, 'E001'))).toBeNull()
    expect(localStorage.getItem(lsKey(1, 'E002'))).toBeTruthy()
  })
})

describe('useDraftSummary — clear', () => {
  it('clear() → 全删 + total=0', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(2, 'E002', { ...emptyDraft(), text: 'b' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.total).toBe(2)
    act(() => result.current.clear())
    expect(result.current.total).toBe(0)
    expect(result.current.byDecision).toEqual([])
  })

  it('LS 里其他非 reflection key → 不删', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    localStorage.setItem('other:key', 'preserve')
    const { result } = renderHook(() => useDraftSummary())
    act(() => result.current.clear())
    expect(localStorage.getItem('other:key')).toBe('preserve')
  })
})

describe('useDraftSummary — storage event', () => {
  it('window 派发 storage 事件(reflection key)→ refresh 生效', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.total).toBe(1)
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'b' })
    act(() => {
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: lsKey(2, 'E001'),
          newValue: localStorage.getItem(lsKey(2, 'E001')),
        }),
      )
    })
    expect(result.current.total).toBe(2)
  })

  it('storage 事件 key 非 reflection → 不刷新', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    const { result } = renderHook(() => useDraftSummary())
    expect(result.current.total).toBe(1)
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'other:key' }))
    })
    expect(result.current.total).toBe(1)
  })
})
