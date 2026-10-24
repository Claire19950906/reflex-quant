import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { ReactNode } from 'react'

/**
 * ReviewPageInner 编排测试 (v302+ P1 重构补测)。
 *
 * 关注点:
 *  - tab=experiences 不渲染「推理审查」/「股票反思」section(修复空 LazySection 占位 500px)
 *  - tab=journal 不显示 A/B tab 切换器
 *  - tab=all 渲染所有 section + A/B tab
 *  - A/B tab 用 role=tab/aria-selected 正确传达选中态
 *  - "改进"section 副标题不再有重复"因果图谱"
 */

vi.mock('@/components/replay/review-panel', () => ({
  ReviewPanel: () => <div data-testid="mock-review-panel" />,
}))
vi.mock('@/components/replay/review-history-panel', () => ({
  ReviewHistoryPanel: () => <div data-testid="mock-review-history-panel" />,
}))
vi.mock('@/components/replay/failure-stats-panel', () => ({
  FailureStatsPanel: () => null,
}))
vi.mock('@/components/replay/attribution-review-panel', () => ({
  AttributionReviewPanel: () => <div data-testid="mock-attribution" />,
}))
vi.mock('@/components/replay/reflection-errors-panel', () => ({
  ReflectionErrorsPanel: () => <div data-testid="mock-reflection-errors" />,
}))
vi.mock('@/components/replay/reasoning-review-panel', () => ({
  ReasoningReviewPanel: () => <div data-testid="mock-reasoning" />,
}))
vi.mock('@/components/replay/stock-reflection-panel', () => ({
  StockReflectionPanel: () => <div data-testid="mock-stock-reflection" />,
}))
vi.mock('@/components/replay/data-health-panel', () => ({
  DataHealthPanel: () => null,
}))
vi.mock('@/components/replay/review-overview', () => ({
  ReviewOverview: () => null,
}))
vi.mock('@/components/replay/analysis-improve-panel', () => ({
  AnalysisImprovePanel: () => <div data-testid="mock-analysis-improve" />,
}))
vi.mock('@/components/replay/reflection-errors/draft-summary-banner', () => ({
  DraftSummaryBanner: () => null,
}))
vi.mock('@/lib/data-channel-standards', () => ({
  PeriodSelector: () => <div data-testid="mock-period-selector" />,
  resolvePeriod: (key: string) => ({
    key,
    label: key,
    hours: 0,
    fromTs: null,
    toTs: null,
  }),
}))
vi.mock('@/lib/lazy-section', () => ({
  LazySection: ({ children }: { children: ReactNode }) => <>{children}</>,
}))
vi.mock('@/lib/use-review-url-state', () => ({
  useReviewUrlState: vi.fn(),
}))
vi.mock('@/lib/use-review-collapse', () => ({
  useReviewCollapse: () => ({
    collapsed: new Set<string>(),
    isCollapsed: () => false,
    toggle: () => {},
    expandAll: () => {},
    collapseAll: () => {},
  }),
}))
vi.mock('@/lib/use-review-shortcuts', () => ({
  useReviewShortcuts: () => ({ allExpanded: true }),
}))

import { useReviewUrlState } from '@/lib/use-review-url-state'
import { ReviewPageInner } from './_inner'

afterEach(() => cleanup())

function mockUrlState(overrides: Record<string, unknown> = {}) {
  vi.mocked(useReviewUrlState).mockReturnValue({
    tab: 'all',
    ab: 'a',
    err: null,
    period: 'today',
    customRange: { fromDate: '2024-01-01', toDate: '2024-01-07' },
    setTab: vi.fn(),
    setAbTab: vi.fn(),
    setErrorCode: vi.fn(),
    setPeriod: vi.fn(),
    setCustomDateRange: vi.fn(),
    ...overrides,
  } as never)
}

describe('ReviewPageInner — tab 编排', () => {
  it('tab=all → 渲染诊断 / 推理 / 股票反思 / 复盘(A/B) / 改进 section', () => {
    mockUrlState({ tab: 'all' })
    render(<ReviewPageInner />)
    expect(screen.getByLabelText('诊断')).toBeInTheDocument()
    expect(screen.getByLabelText('推理审查')).toBeInTheDocument()
    expect(screen.getByLabelText('股票反思')).toBeInTheDocument()
    expect(screen.getByLabelText('复盘')).toBeInTheDocument()
    expect(screen.getByLabelText('改进')).toBeInTheDocument()
  })

  it('tab=journal → 渲染诊断 / 推理 / 股票反思 / 复盘,不显示 A/B tab', () => {
    mockUrlState({ tab: 'journal' })
    render(<ReviewPageInner />)
    expect(screen.getByLabelText('诊断')).toBeInTheDocument()
    expect(screen.getByLabelText('推理审查')).toBeInTheDocument()
    expect(screen.getByLabelText('股票反思')).toBeInTheDocument()
    expect(screen.getByLabelText('复盘')).toBeInTheDocument()
    // journal 模式不显示 A/B tab 切换器(原代码也只在 isAll 时渲染)
    expect(screen.queryByRole('tablist', { name: '复盘模式' })).not.toBeInTheDocument()
  })

  it('tab=experiences → 不渲染「推理审查」/「股票反思」section(修复 #2 空占位 bug)', () => {
    mockUrlState({ tab: 'experiences' })
    render(<ReviewPageInner />)
    // 诊断 section 在 experiences 模式下仍在(展示 AttributionReviewPanel)
    expect(screen.getByLabelText('诊断')).toBeInTheDocument()
    // 关键修复:experiences tab 不再挂载空 LazySection 占位 500px
    expect(screen.queryByLabelText('推理审查')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('股票反思')).not.toBeInTheDocument()
  })
})

describe('ReviewPageInner — A/B tab 切换', () => {
  it('tab=all + ab=a → A tab 处于 aria-selected=true', () => {
    mockUrlState({ tab: 'all', ab: 'a' })
    render(<ReviewPageInner />)
    const aTab = screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })
    const bTab = screen.getByRole('tab', { name: /B · 快速评测复盘/ })
    expect(aTab).toHaveAttribute('aria-selected', 'true')
    expect(bTab).toHaveAttribute('aria-selected', 'false')
  })

  it('tab=all + ab=b → B tab 处于 aria-selected=true', () => {
    mockUrlState({ tab: 'all', ab: 'b' })
    render(<ReviewPageInner />)
    const aTab = screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })
    const bTab = screen.getByRole('tab', { name: /B · 快速评测复盘/ })
    expect(aTab).toHaveAttribute('aria-selected', 'false')
    expect(bTab).toHaveAttribute('aria-selected', 'true')
  })

  it('A/B tablist 含 aria-label="复盘模式"', () => {
    mockUrlState({ tab: 'all' })
    render(<ReviewPageInner />)
    expect(screen.getByRole('tablist', { name: '复盘模式' })).toBeInTheDocument()
  })
})

describe('ReviewPageInner — 改进 section 文案', () => {
  it('副标题不含重复 "因果图谱"(修复 #1 文案错字)', () => {
    mockUrlState({ tab: 'all' })
    render(<ReviewPageInner />)
    const heading = screen.getByRole('heading', { name: /改进/ })
    const section = heading.closest('section')
    expect(section).toBeInTheDocument()
    const subtitle = section?.querySelector('p')
    expect(subtitle?.textContent).not.toMatch(/因果图谱因果图谱/)
    expect(subtitle?.textContent).toMatch(/因果链库/)
  })
})

describe('ReviewPageInner — 全局时段 ARIA', () => {
  it('全局时段 section 含 aria-label="全局时段选择"', () => {
    mockUrlState({ tab: 'all' })
    render(<ReviewPageInner />)
    expect(screen.getByLabelText('全局时段选择')).toBeInTheDocument()
  })

  it('"当前:" 文本节点含 aria-live="polite"', () => {
    mockUrlState({ tab: 'all', period: '7d' })
    render(<ReviewPageInner />)
    // mockUrlState 的 period='7d' 但 resolvePeriod 是 mock,label=key='7d'
    const liveNode = screen.getByText(/当前:/)
    expect(liveNode).toHaveAttribute('aria-live', 'polite')
  })
})
