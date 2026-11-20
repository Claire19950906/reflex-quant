import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { fireEvent } from '@testing-library/dom'
import { useReviewShortcuts } from './use-review-shortcuts'

afterEach(() => {
  vi.restoreAllMocks()
})

function mount(
  opts: Parameters<typeof useReviewShortcuts>[0],
) {
  return renderHook(
    ({ o }: { o: Parameters<typeof useReviewShortcuts>[0] }) =>
      useReviewShortcuts(o),
    { initialProps: { o: opts } },
  )
}

const base = {
  onJumpToSection: () => {},
  sectionCount: 5,
  allExpanded: true,
  onToggleExpandAll: () => {},
  onCopyMarkdown: () => {},
  onShowHelp: () => {},
  onEscape: () => {},
}

describe('useReviewShortcuts — 数字键 1-9', () => {
  it('1 调 onJumpToSection(0)', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection })
    fireEvent.keyDown(document, { key: '1' })
    expect(onJumpToSection).toHaveBeenCalledWith(0)
  })

  it('9 调 onJumpToSection(8)', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection, sectionCount: 9 })
    fireEvent.keyDown(document, { key: '9' })
    expect(onJumpToSection).toHaveBeenCalledWith(8)
  })

  it('按 9 但 sectionCount=3 → 不响应', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection, sectionCount: 3 })
    fireEvent.keyDown(document, { key: '9' })
    expect(onJumpToSection).not.toHaveBeenCalled()
  })
})

describe('useReviewShortcuts — j/k/G', () => {
  it('j → onJumpToSection(-1) 哨兵"下一个"', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection })
    fireEvent.keyDown(document, { key: 'j' })
    expect(onJumpToSection).toHaveBeenCalledWith(-1)
  })

  it('k → onJumpToSection(-2) 哨兵"上一个"', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection })
    fireEvent.keyDown(document, { key: 'k' })
    expect(onJumpToSection).toHaveBeenCalledWith(-2)
  })

  it('G (shift+g) → onJumpToSection(-4) 哨兵"底部"', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection })
    fireEvent.keyDown(document, { key: 'G', shiftKey: true })
    expect(onJumpToSection).toHaveBeenCalledWith(-4)
  })
})

describe('useReviewShortcuts — 其它单键', () => {
  it('e → onToggleExpandAll', () => {
    const onToggleExpandAll = vi.fn()
    mount({ ...base, onToggleExpandAll })
    fireEvent.keyDown(document, { key: 'e' })
    expect(onToggleExpandAll).toHaveBeenCalledTimes(1)
  })

  it('c → onCopyMarkdown', () => {
    const onCopyMarkdown = vi.fn()
    mount({ ...base, onCopyMarkdown })
    fireEvent.keyDown(document, { key: 'c' })
    expect(onCopyMarkdown).toHaveBeenCalledTimes(1)
  })

  it('? → onShowHelp', () => {
    const onShowHelp = vi.fn()
    mount({ ...base, onShowHelp })
    fireEvent.keyDown(document, { key: '?' })
    expect(onShowHelp).toHaveBeenCalledTimes(1)
  })

  it('Escape → onEscape', () => {
    const onEscape = vi.fn()
    mount({ ...base, onEscape })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onEscape).toHaveBeenCalledTimes(1)
  })
})

describe('useReviewShortcuts — 守卫', () => {
  it('在 input 上按键 → 不响应', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection })
    const input = document.createElement('input')
    document.body.appendChild(input)
    fireEvent.keyDown(input, { key: '1' })
    expect(onJumpToSection).not.toHaveBeenCalled()
    document.body.removeChild(input)
  })

  it('在 textarea 上按键 → 不响应', () => {
    const onCopyMarkdown = vi.fn()
    mount({ ...base, onCopyMarkdown })
    const ta = document.createElement('textarea')
    document.body.appendChild(ta)
    fireEvent.keyDown(ta, { key: 'c' })
    expect(onCopyMarkdown).not.toHaveBeenCalled()
    document.body.removeChild(ta)
  })

  it('按住 Ctrl → 不响应(防与浏览器冲突)', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection })
    fireEvent.keyDown(document, { key: '1', ctrlKey: true })
    expect(onJumpToSection).not.toHaveBeenCalled()
  })

  it('按住 Meta(Cmd) → 不响应', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection })
    fireEvent.keyDown(document, { key: '1', metaKey: true })
    expect(onJumpToSection).not.toHaveBeenCalled()
  })

  it('enabled=false → 不挂 listener', () => {
    const onJumpToSection = vi.fn()
    mount({ ...base, onJumpToSection, enabled: false })
    fireEvent.keyDown(document, { key: '1' })
    expect(onJumpToSection).not.toHaveBeenCalled()
  })
})

describe('useReviewShortcuts — 返回值', () => {
  it('暴露 allExpanded', () => {
    const { result } = renderHook(() => useReviewShortcuts({ ...base, allExpanded: false }))
    expect(result.current.allExpanded).toBe(false)
  })
})


