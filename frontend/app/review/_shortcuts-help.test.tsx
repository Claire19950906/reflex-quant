import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { ShortcutsHelp } from './_shortcuts-help'

afterEach(() => cleanup())

describe('ShortcutsHelp — 渲染', () => {
  it('open=false → 不渲染任何内容', () => {
    render(<ShortcutsHelp open={false} onClose={() => {}} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('open=true → 渲染 dialog', () => {
    render(<ShortcutsHelp open={true} onClose={() => {}} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('标题为"键盘快捷键"', () => {
    render(<ShortcutsHelp open={true} onClose={() => {}} />)
    expect(screen.getByRole('heading', { name: '键盘快捷键' })).toBeInTheDocument()
  })

  it('列出全部快捷键(j/k/g g/G/e/c/?/Esc)', () => {
    render(<ShortcutsHelp open={true} onClose={() => {}} />)
    expect(screen.getByText(/跳转到第 N 个段位/)).toBeInTheDocument()
    expect(screen.getByText(/下一个/)).toBeInTheDocument()
    expect(screen.getByText(/展开 \/ 折叠全部/)).toBeInTheDocument()
    expect(screen.getByText(/复制当前视图为 Markdown/)).toBeInTheDocument()
    expect(screen.getByText(/显示本帮助/)).toBeInTheDocument()
    expect(screen.getByText(/关闭弹窗/)).toBeInTheDocument()
  })

  it('a11y: aria-modal="true"', () => {
    render(<ShortcutsHelp open={true} onClose={() => {}} />)
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'true')
  })
})

describe('ShortcutsHelp — 交互', () => {
  it('点关闭按钮 → onClose', () => {
    const onClose = vi.fn()
    render(<ShortcutsHelp open={true} onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('按 Esc → onClose', () => {
    const onClose = vi.fn()
    render(<ShortcutsHelp open={true} onClose={onClose} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('open=false → 不响应 Esc', () => {
    const onClose = vi.fn()
    render(<ShortcutsHelp open={false} onClose={onClose} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })
})

afterEach(() => cleanup())
