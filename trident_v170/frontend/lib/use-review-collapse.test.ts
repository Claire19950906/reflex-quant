/**
 * use-review-collapse.test.ts — 折叠 hook 的纯函数 + hook 行为测试
 *
 * 测试策略:
 *  - readCollapsedFromUrl / collapseToUrlPatch 是纯函数 → 100% 覆盖边界
 *  - useReviewCollapse 行为: 通过 mock next/navigation,验证 router.replace 调用
 *  - 关键不变量: 折叠 set 为空时,URL 必须删掉 collapse 键(而不是写成 collapse=)
 */

import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  COLLAPSE_PARAM,
  collapseToUrlPatch,
  readCollapsedFromUrl,
  useReviewCollapse,
} from './use-review-collapse'

// ====== 1. 纯函数 readCollapsedFromUrl ======

describe('readCollapsedFromUrl', () => {
  it('空 URL → 空 Set', () => {
    expect(readCollapsedFromUrl(new URLSearchParams()).size).toBe(0)
  })

  it('null/undefined → 空 Set(SSR 兜底)', () => {
    expect(readCollapsedFromUrl(null).size).toBe(0)
    expect(readCollapsedFromUrl(undefined).size).toBe(0)
  })

  it('单个 id', () => {
    const sp = new URLSearchParams(`${COLLAPSE_PARAM}=review`)
    expect(Array.from(readCollapsedFromUrl(sp))).toEqual(['review'])
  })

  it('逗号分隔多 id', () => {
    const sp = new URLSearchParams(`${COLLAPSE_PARAM}=review,reasoning,dataHealth`)
    const out = readCollapsedFromUrl(sp)
    expect(out.size).toBe(3)
    expect(out.has('review')).toBe(true)
    expect(out.has('reasoning')).toBe(true)
    expect(out.has('dataHealth')).toBe(true)
  })

  it('空白 token 过滤(防止 ",," 产生空字符串 id)', () => {
    const sp = new URLSearchParams(`${COLLAPSE_PARAM}=a,,b,`)
    const out = readCollapsedFromUrl(sp)
    expect(Array.from(out).sort()).toEqual(['a', 'b'])
  })

  it('其他参数不影响(只读 collapse 键)', () => {
    const sp = new URLSearchParams('tab=journal&collapse=review&err=E042')
    const out = readCollapsedFromUrl(sp)
    expect(Array.from(out)).toEqual(['review'])
  })
})

// ====== 2. 纯函数 collapseToUrlPatch ======

describe('collapseToUrlPatch', () => {
  it('空 Set → 删除 collapse 键(null 而非空串)', () => {
    const patch = collapseToUrlPatch(new Set())
    expect(patch).toEqual([['collapse', null]])
  })

  it('单 id → 序列化为单个 patch', () => {
    const patch = collapseToUrlPatch(new Set(['review']))
    expect(patch).toEqual([['collapse', 'review']])
  })

  it('多 id → 用逗号拼起来', () => {
    const patch = collapseToUrlPatch(new Set(['a', 'b', 'c']))
    expect(patch).toEqual([['collapse', 'a,b,c']])
  })
})

// ====== 3. useReviewCollapse hook 行为 ======

// mock next/navigation
const mockReplace = vi.fn()
const mockSp = { get: vi.fn(), toString: vi.fn(() => '') } as unknown as URLSearchParams
const mockPathname = '/review'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: mockReplace }),
  useSearchParams: () => mockSp,
  usePathname: () => mockPathname,
}))

function makeSpGetter(map: Record<string, string>): URLSearchParams {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(map)) sp.set(k, v)
  return sp
}

describe('useReviewCollapse', () => {
  it('初始空 → isCollapsed 全 false', () => {
    ;(mockSp as any).get = vi.fn(() => null)
    ;(mockSp as any).toString = vi.fn(() => '')
    const { result } = renderHook(() => useReviewCollapse())
    expect(result.current.collapsed.size).toBe(0)
    expect(result.current.isCollapsed('review')).toBe(false)
  })

  it('toggle 单一 id → URL 写入该 id', () => {
    ;(mockSp as any).get = vi.fn((k: string) => (k === COLLAPSE_PARAM ? null : null))
    ;(mockSp as any).toString = vi.fn(() => '')
    const { result } = renderHook(() => useReviewCollapse())
    act(() => result.current.toggle('review'))
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining(`${COLLAPSE_PARAM}=review`),
      expect.objectContaining({ scroll: false }),
    )
  })

  it('toggle 同一个 id 两次 → URL 清空 collapse 键', () => {
    ;(mockSp as any).toString = vi.fn(() => `${COLLAPSE_PARAM}=review`)
    ;(mockSp as any).get = vi.fn((k: string) => (k === COLLAPSE_PARAM ? 'review' : null))
    const { result } = renderHook(() => useReviewCollapse())
    act(() => result.current.toggle('review'))
    // 第二次 toggle 后,set 应为空,URL 应不含 collapse
    const lastCall = mockReplace.mock.calls[mockReplace.mock.calls.length - 1]
    expect(lastCall[0]).not.toContain(COLLAPSE_PARAM)
  })

  it('expandAll → URL 不含 collapse 键', () => {
    ;(mockSp as any).toString = vi.fn(() => `${COLLAPSE_PARAM}=a,b,c`)
    ;(mockSp as any).get = vi.fn((k: string) => (k === COLLAPSE_PARAM ? 'a,b,c' : null))
    const { result } = renderHook(() => useReviewCollapse())
    act(() => result.current.expandAll())
    const lastCall = mockReplace.mock.calls[mockReplace.mock.calls.length - 1]
    expect(lastCall[0]).not.toContain(COLLAPSE_PARAM)
  })

  it('collapseAll → URL 写入所有 id', () => {
    ;(mockSp as any).toString = vi.fn(() => '')
    ;(mockSp as any).get = vi.fn(() => null)
    const { result } = renderHook(() => useReviewCollapse())
    act(() => result.current.collapseAll(['a', 'b', 'c']))
    const lastCall = mockReplace.mock.calls[mockReplace.mock.calls.length - 1]
    // URLSearchParams 把 ',' 编码成 %2C — 解码后断言
    const decoded = decodeURIComponent(lastCall[0])
    expect(decoded).toContain(`${COLLAPSE_PARAM}=a,b,c`)
  })
})
