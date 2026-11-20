import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { SectionProgress } from './_section-progress'

afterEach(() => cleanup())

afterEach(() => cleanup())

const SECTIONS = [
  { id: 'diagnose', label: '诊断' },
  { id: 'review', label: '复盘' },
  { id: 'improve', label: '改进' },
]

describe('SectionProgress — 基础渲染', () => {
  it('渲染 "展开 N / 总 M"', () => {
    render(<SectionProgress total={5} expanded={3} sections={SECTIONS} />)
    expect(screen.getByText('3 / 5 展开')).toBeInTheDocument()
  })

  it('渲染 "段位进度" 标签', () => {
    render(<SectionProgress total={3} expanded={3} sections={SECTIONS} />)
    expect(screen.getByText('段位进度')).toBeInTheDocument()
  })

  it('默认显示第一个 section 作为当前位置', () => {
    const { container } = render(<SectionProgress total={3} expanded={3} sections={SECTIONS} />)
    expect(container.textContent).toContain('诊断')
    expect(container.textContent).toContain('(1/3)')
  })

  it('空 sections → 不渲染位置', () => {
    const { container } = render(<SectionProgress total={0} expanded={0} sections={[]} />)
    expect(container.textContent).not.toContain('(1/')
  })

  it('data-testid="section-progress" 供 e2e 锁定', () => {
    const { container } = render(<SectionProgress total={3} expanded={3} sections={SECTIONS} />)
    expect(container.querySelector('[data-testid="section-progress"]')).toBeInTheDocument()
  })

  it('role="status" + aria-live="polite"(a11y)', () => {
    const { container } = render(<SectionProgress total={3} expanded={3} sections={SECTIONS} />)
    const el = container.querySelector('[data-testid="section-progress"]')
    expect(el).toHaveAttribute('role', 'status')
    expect(el).toHaveAttribute('aria-live', 'polite')
  })
})
