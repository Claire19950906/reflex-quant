/**
 * useReflectionDrafts hook 测试 —— 反思草稿状态机(v275+)。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

const apiSendMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiSend: (...args: unknown[]) => (apiSendMock as any)(...args),
}))

import { useReflectionDrafts, draftKey } from './use-reflection-drafts'
import { loadDraft, lsKey } from './draft-storage'

describe('useReflectionDrafts — open / close / update', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('openReflection 创建空草稿', () => {
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k]).toBeTruthy()
    expect(result.current.reflections[k].open).toBe(true)
    expect(result.current.reflections[k].text).toBe('')
  })

  it('openReflection 已 LS 持久化 → 复用已有 text', () => {
    localStorage.setItem(
      lsKey(2, 'E002'),
      JSON.stringify({
        open: false,
        text: '上次写到一半',
        coreMarked: false,
        improvementText: '',
        aiQuestions: [],
        loadingFollowups: false,
      }),
    )
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(2, 'E002'))
    const k = draftKey(2, 'E002')
    expect(result.current.reflections[k].text).toBe('上次写到一半')
    expect(result.current.reflections[k].open).toBe(true)
  })

  it('closeReflection 含内容 → saveDraft 持久化', () => {
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    act(() =>
      result.current.updateDraft(1, 'E001', { text: '有内容' }),
    )
    act(() => result.current.closeReflection(1, 'E001'))
    expect(loadDraft(1, 'E001')).toBeTruthy()
    expect(loadDraft(1, 'E001')?.text).toBe('有内容')
  })

  it('closeReflection 完全空 → 不持久化', () => {
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    act(() => result.current.closeReflection(1, 'E001'))
    expect(loadDraft(1, 'E001')).toBeNull()
  })

  it('updateDraft merge patch(只改 text 不影响 aiQuestions)', () => {
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    act(() =>
      result.current.updateDraft(1, 'E001', {
        aiQuestions: ['q1', 'q2'],
      }),
    )
    act(() =>
      result.current.updateDraft(1, 'E001', { text: 'hello' }),
    )
    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].text).toBe('hello')
    expect(result.current.reflections[k].aiQuestions).toEqual(['q1', 'q2'])
  })

  it('saveReflection 写 savedAt ISO 字符串 + 持久化', () => {
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    act(() =>
      result.current.updateDraft(1, 'E001', { text: '要保存' }),
    )
    act(() => result.current.saveReflection(1, 'E001'))
    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].savedAt).toBeTruthy()
    expect(() =>
      new Date(result.current.reflections[k].savedAt!).toISOString(),
    ).not.toThrow()
    const persisted = loadDraft(1, 'E001')
    expect(persisted?.text).toBe('要保存')
  })

  it('insertQuestion 在 text 末尾追加【问】q', () => {
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    act(() => result.current.updateDraft(1, 'E001', { text: 'A' }))
    act(() => result.current.insertQuestion(1, 'E001', 'B 是什么?'))
    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].text).toBe('A\n【问】B 是什么?')
  })

  it('insertQuestion 无 text → 直接【问】q', () => {
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    act(() => result.current.insertQuestion(1, 'E001', 'first'))
    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].text).toBe('【问】first')
  })
})

describe('useReflectionDrafts — requestFollowups', () => {
  beforeEach(() => {
    apiSendMock.mockReset()
    localStorage.clear()
  })

  it('成功 → aiQuestions 填充 + 传 AbortSignal', async () => {
    apiSendMock.mockResolvedValue({ questions: ['q1', 'q2'] })
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))
    act(() => result.current.updateDraft(1, 'E001', { text: 'reflect' }))

    await act(async () => {
      await result.current.requestFollowups(1, 'E001')
    })

    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].aiQuestions).toEqual(['q1', 'q2'])
    expect(result.current.reflections[k].loadingFollowups).toBe(false)
    expect(apiSendMock).toHaveBeenCalledWith(
      '/reflection/suggest-followups',
      'POST',
      expect.objectContaining({
        decision_id: 1,
        error_code: 'E001',
        current_reflection: 'reflect',
        n_suggest: 3,
      }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('失败 → lastError 写入,loading 归零', async () => {
    apiSendMock.mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))

    await act(async () => {
      await result.current.requestFollowups(1, 'E001')
    })

    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].lastError).toBe('boom')
    expect(result.current.reflections[k].loadingFollowups).toBe(false)
  })

  it('重复调用 → 上一轮被 abort,第二次结果胜出', async () => {
    let resolveFirst!: (v: unknown) => void
    apiSendMock.mockImplementation(
      () => new Promise((r) => { resolveFirst = r }),
    )
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))

    let firstPromise: Promise<void>
    act(() => {
      firstPromise = result.current.requestFollowups(1, 'E001')
    })
    expect(apiSendMock).toHaveBeenCalledTimes(1)
    const firstSignal = apiSendMock.mock.calls[0][3]?.signal
    expect(firstSignal.aborted).toBe(false)

    apiSendMock.mockResolvedValue({ questions: ['q2'] })
    let secondPromise: Promise<void>
    act(() => {
      secondPromise = result.current.requestFollowups(1, 'E001')
    })
    expect(apiSendMock).toHaveBeenCalledTimes(2)
    expect(firstSignal.aborted).toBe(true)

    resolveFirst({ questions: ['q1'] })
    await act(async () => {
      await firstPromise
      await secondPromise
    })
    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].aiQuestions).toEqual(['q2'])
  })

  it('AbortError 不写 lastError', async () => {
    const abortErr = Object.assign(new Error('aborted'), { name: 'AbortError' })
    apiSendMock.mockRejectedValue(abortErr)
    const { result } = renderHook(() => useReflectionDrafts())
    act(() => result.current.openReflection(1, 'E001'))

    await act(async () => {
      await result.current.requestFollowups(1, 'E001')
    })

    const k = draftKey(1, 'E001')
    expect(result.current.reflections[k].lastError).toBeFalsy()
  })
})