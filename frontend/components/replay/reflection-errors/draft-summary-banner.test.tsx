/**
 * DraftSummaryBanner 组件测试 —— 草稿总览横幅(v302+ PR-15)。
 *
 * 覆盖:
 *  - 空 LS → 不渲染
 *  - 有草稿 → 显示总数 + 决策数
 *  - stale 草稿 → badge-stale 显示数量
 *  - 多错误码 → badge-multi-error 显示
 *  - 折叠 / 展开切换
 *  - 展开后 → 显示每行 + 文字预览
 *  - 删除单条 → 行消失
 *  - 清空全部 → 二次确认
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen, within, fireEvent, cleanup } from '@testing-library/react'
import { DraftSummaryBanner } from './draft-summary-banner'
import { saveDraft, emptyDraft, lsKey } from './draft-storage'

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
})

describe('DraftSummaryBanner — 渲染与否', () => {
  it('LS 空 → 不渲染 banner', () => {
    const { container } = render(<DraftSummaryBanner />)
    expect(container.querySelector('[data-testid="draft-summary-banner"]')).toBeNull()
  })

  it('1 个草稿 → 显示总数 + 1 决策', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    render(<DraftSummaryBanner />)
    const banner = screen.getByTestId('draft-summary-banner')
    expect(within(banner).getByText('1 个未提交反思草稿')).toBeTruthy()
    expect(within(banner).getByText('(1 决策)')).toBeTruthy()
  })

  it('3 个草稿 2 决策 → 显示 3 + 2 决策', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(1, 'E002', { ...emptyDraft(), text: 'b' })
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'c' })
    render(<DraftSummaryBanner />)
    const banner = screen.getByTestId('draft-summary-banner')
    expect(within(banner).getByText('3 个未提交反思草稿')).toBeTruthy()
    expect(within(banner).getByText('(2 决策)')).toBeTruthy()
  })
})

describe('DraftSummaryBanner — badge', () => {
  it('stale(>7天)草稿 → badge-stale 显示', () => {
    const old = new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString()
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'old', savedAt: old })
    render(<DraftSummaryBanner />)
    const banner = screen.getByTestId('draft-summary-banner')
    expect(within(banner).getByTestId('badge-stale')).toBeTruthy()
    expect(within(banner).getByTestId('badge-stale').textContent).toContain('1 个')
  })

  it('多错误码决策 → badge-multi-error 显示', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(1, 'E002', { ...emptyDraft(), text: 'b' })
    saveDraft(1, 'E003', { ...emptyDraft(), text: 'c' })
    render(<DraftSummaryBanner />)
    const banner = screen.getByTestId('draft-summary-banner')
    expect(within(banner).getByTestId('badge-multi-error')).toBeTruthy()
  })

  it('新鲜草稿 → 不显示 badge-stale', () => {
    const fresh = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString()
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'fresh', savedAt: fresh })
    render(<DraftSummaryBanner />)
    expect(screen.queryByTestId('badge-stale')).toBeNull()
  })
})


describe('DraftSummaryBanner — 折叠 / 展开', () => {
  it('默认折叠 → draft-list 不在 DOM', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    render(<DraftSummaryBanner />)
    expect(screen.queryByTestId('draft-list')).toBeNull()
  })

  it('点标题 → 展开 draft-list', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'hello world' })
    render(<DraftSummaryBanner />)
    const banner = screen.getByTestId('draft-summary-banner')
    const toggle = within(banner).getByRole('button', { name: /1 个未提交/ })
    fireEvent.click(toggle)
    expect(screen.getByTestId('draft-list')).toBeTruthy()
  })

  it('展开后显示每个决策行 + 文字预览', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: '我应该先看总框架' })
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'AAPL 该减仓' })
    render(<DraftSummaryBanner />)
    fireEvent.click(screen.getByRole('button', { name: /2 个未提交/ }))
    expect(screen.getByTestId('draft-row-1')).toBeTruthy()
    expect(screen.getByTestId('draft-row-2')).toBeTruthy()
    // 文字预览 — 每个决策行内各自的"错误码: 预览文字"
    expect(within(screen.getByTestId('draft-row-1')).getByText('E001:')).toBeTruthy()
    expect(within(screen.getByTestId('draft-row-1')).getByText(/我应该先看总框架/)).toBeTruthy()
    expect(within(screen.getByTestId('draft-row-2')).getByText('E001:')).toBeTruthy()
  })
})

describe('DraftSummaryBanner — 删除单条', () => {
  it('点 × → LS 删除', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(1, 'E002', { ...emptyDraft(), text: 'b' })
    render(<DraftSummaryBanner />)
    fireEvent.click(screen.getByRole('button', { name: /2 个未提交/ }))
    const del = screen.getByLabelText('删除 E001 草稿')
    fireEvent.click(del)
    expect(localStorage.getItem(lsKey(1, 'E001'))).toBeNull()
    expect(localStorage.getItem(lsKey(1, 'E002'))).toBeTruthy()
  })
})

describe('DraftSummaryBanner — 清空全部', () => {
  it('点"清空全部" → 出现二次确认', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    render(<DraftSummaryBanner />)
    fireEvent.click(screen.getByRole('button', { name: '清空全部' }))
    expect(screen.getByText('确认清空全部?')).toBeTruthy()
  })

  it('点"是" → 全删 + banner 消失', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'b' })
    const { container } = render(<DraftSummaryBanner />)
    fireEvent.click(screen.getByRole('button', { name: '清空全部' }))
    fireEvent.click(screen.getByRole('button', { name: '是' }))
    expect(container.querySelector('[data-testid="draft-summary-banner"]')).toBeNull()
  })

  it('点"否" → 草稿保留', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'a' })
    render(<DraftSummaryBanner />)
    fireEvent.click(screen.getByRole('button', { name: '清空全部' }))
    fireEvent.click(screen.getByRole('button', { name: '否' }))
    expect(localStorage.getItem(lsKey(1, 'E001'))).toBeTruthy()
  })
})
