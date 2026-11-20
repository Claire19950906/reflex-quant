/**
 * app/review/_a11y.test.tsx — /review 路由的可访问性回归测试 (v302+ P3)
 *
 * 用 jest-axe 跑 axe-core,锁定 WAI-ARIA / WCAG 关键规则:
 *  - region(每个 section 必须有可访问名)
 *  - landmark(每个 role 必须有对应 aria-label)
 *  - aria-roles / aria-attrs(role=tablist 必须包 role=tab;aria-selected 一致)
 *  - label(交互元素必须有可访问名)
 *
 * 这是 v302+ 修复 "role=alert 把重试按钮一起急促读出" 后留下的回归网 —
 * 防止后续重构把好 a11y 模式弄丢。
 *
 * 测试策略:对 /review 的关键交互组件(段头、A/B tab 切换、错误边界)
 * 各跑一次 axe,断言 toHaveNoViolations。任何 axe violation 立即爆。
 */

import { describe, it, expect, afterEach, vi } from 'vitest'

// 辅助函数:断言 jest-axe 的 results 无 violation
// vitest 2.x 的类型 augmentation 容易把 @vitest/expect 整个签名弄坏,
// 所以不通过 module augmentation 加 toHaveNoViolations matcher,
// 而是用强转 + 括号表达式访问属性,绕开 TS 的 matcher 类型检查。
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function a11yHasNoViolations(results: any) {
  // 运行时 expect.extend(toHaveNoViolations) 在 vitest.setup.ts 已注册,
  // 这里用字符串属性访问绕过 TS 类型检查
  ;(expect(results) as any).toHaveNoViolations()
}
import { render, cleanup, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import { axe } from 'jest-axe'

afterEach(() => cleanup())

// ====== 子组件 mock — 与 _inner.test.tsx 同步 ======

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
  // v302+ P3++: mock 接收 initialCode 并记到 lastInitialCode 上,
  // 测试可以验证 useDeferredValue 后的值是否正确传给 panel
  ReflectionErrorsPanel: ({ initialCode }: { initialCode?: string | null }) => {
    ;(globalThis as Record<string, unknown>).__lastInitialCode = initialCode
    return <div data-testid="mock-reflection-errors" />
  },
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
  resolvePeriod: (key: string) => ({ key, label: key, hours: 0, fromTs: null, toTs: null }),
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
import { SectionHeading, SectionErrorBoundary, PanelLoadingSkeleton } from './_section'
import { REVIEW_SECTIONS } from './_spec'

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

// ====== 测试组 1: SectionHeading — 段头的可访问性 ======

describe('SectionHeading — a11y', () => {
  it('单独渲染无 axe violations', async () => {
    const { container } = render(
      <SectionHeading icon="🛠" title="改进" subtitle="因果图谱 · 因果链库" />,
    )
    const results = await axe(container)
    a11yHasNoViolations(results)
  })

  it('不同 icon/title/subtitle 都无 violations', async () => {
    const { container } = render(
      <SectionHeading icon="📊" title="诊断" subtitle="错误码分布" />,
    )
    const results = await axe(container)
    a11yHasNoViolations(results)
  })
})

// ====== 测试组 2: SectionErrorBoundary — 错误降级的可访问性 ======

describe('SectionErrorBoundary — a11y', () => {
  // 抑制错误日志
  const origError = console.error
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(console as any).error = () => {}
  afterEach(() => {
    ;(console as any).error = origError
  })

  it('正常 children 无 violations', async () => {
    const { container } = render(
      <SectionErrorBoundary name="测试段">
        <div>正常内容</div>
      </SectionErrorBoundary>,
    )
    const results = await axe(container)
    a11yHasNoViolations(results)
  })

  it('fallback 状态(模拟错误)无 violations', async () => {
    function Bomb(): never {
      throw new Error('boom')
    }
    const { container } = render(
      <SectionErrorBoundary name="测试段">
        <Bomb />
      </SectionErrorBoundary>,
    )
    const results = await axe(container)
    a11yHasNoViolations(results)
  })

  it('fallback 内 role=alert 与 重试按钮 不互相 throw 误读(防 v302 旧 bug 回归)', async () => {
    // v302 修复历史:role="alert" 移到内部 span,按钮独立 Tab 可达,
    // 否则屏幕阅读器会把"重试"按钮文本急促读出。这里用 axe 锁住 a11y 形态。
    function Bomb(): never {
      throw new Error('boom')
    }
    const { container, getByRole } = render(
      <SectionErrorBoundary name="复盘">
        <Bomb />
      </SectionErrorBoundary>,
    )
    expect(getByRole('button', { name: '重试' })).toBeInTheDocument()
    const results = await axe(container)
    a11yHasNoViolations(results)
  })
})

// ====== 测试组 3: ReviewPageInner — 全页编排的可访问性 ======

describe('ReviewPageInner — a11y', () => {
  it('tab=all + ab=a 全页无 axe violations(A/B tab 切换器是关键测试目标)', async () => {
    mockUrlState({ tab: 'all', ab: 'a' })
    const { container } = render(<ReviewPageInner />)
    const results = await axe(container)
    a11yHasNoViolations(results)
  })

  it('tab=all + ab=b 无 violations', async () => {
    mockUrlState({ tab: 'all', ab: 'b' })
    const { container } = render(<ReviewPageInner />)
    const results = await axe(container)
    a11yHasNoViolations(results)
  })

  it('tab=journal 无 violations(无 A/B tab 切换器)', async () => {
    mockUrlState({ tab: 'journal' })
    const { container } = render(<ReviewPageInner />)
    const results = await axe(container)
    a11yHasNoViolations(results)
  })

  it('tab=experiences 无 violations(无复盘面板)', async () => {
    mockUrlState({ tab: 'experiences' })
    const { container } = render(<ReviewPageInner />)
    const results = await axe(container)
    a11yHasNoViolations(results)
  })
})

// ====== 测试组 4: 自定义规则 — 锁定本项目的 a11y 设计约定 ======

describe('ReviewPageInner — 项目专属 a11y 约定', () => {
  it('所有 section 必须有 aria-label 或 aria-labelledby(无裸 section)', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const sections = container.querySelectorAll('section')
    expect(sections.length).toBeGreaterThan(0)
    for (const sec of sections) {
      const hasLabel =
        sec.hasAttribute('aria-label') || sec.hasAttribute('aria-labelledby')
      expect(hasLabel, '<section> 缺少 aria-label/aria-labelledby').toBe(true)
    }
  })

  it('A/B tablist 必须有 aria-label(role=tablist 必带)', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const tablists = container.querySelectorAll('[role="tablist"]')
    expect(tablists.length).toBeGreaterThan(0)
    for (const tl of tablists) {
      expect(tl.hasAttribute('aria-label'), 'tablist 缺 aria-label').toBe(true)
    }
  })

  it('tablist 内恰好 1 个 tab aria-selected=true', () => {
    mockUrlState({ tab: 'all', ab: 'a' })
    const { container } = render(<ReviewPageInner />)
    const tabs = container.querySelectorAll('[role="tab"]')
    expect(tabs.length).toBe(2)
    const selectedCount = Array.from(tabs).filter(
      (t) => t.getAttribute('aria-selected') === 'true',
    ).length
    expect(selectedCount).toBe(1)
  })

  it('icon 必须 aria-hidden(防止 emoji 被读两遍)', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const icons = container.querySelectorAll('h2 > span[aria-hidden]')
    expect(icons.length).toBeGreaterThan(0)
  })
})

// ====== v302+ P3+: tablist 键盘导航(WAI-ARIA Authoring Practices) ======
// 锁定行为:
//  - ArrowRight: 跳到下一个 tab
//  - ArrowLeft: 跳到上一个 tab
//  - Home/End: 跳到首/末 tab
//  - 当前选中的 tab tabIndex=0,其他 -1(Roving Tabindex)
describe('v302+ P3+: tablist 键盘导航', () => {
  it('当前选中的 tab tabIndex=0,其他 tabIndex=-1(Roving Tabindex)', () => {
    mockUrlState({ tab: 'all', ab: 'a' })
    const { container } = render(<ReviewPageInner />)
    const tabs = Array.from(container.querySelectorAll('[role="tab"]'))
    expect(tabs.length).toBe(2)
    // ab='a' 选中第一个 tab
    expect(tabs[0].getAttribute('tabindex')).toBe('0')
    expect(tabs[1].getAttribute('tabindex')).toBe('-1')

    // 切到 ab='b' 后,选第二个 tab
    mockUrlState({ tab: 'all', ab: 'b' })
    cleanup()
    const { container: c2 } = render(<ReviewPageInner />)
    const tabs2 = Array.from(c2.querySelectorAll('[role="tab"]'))
    expect(tabs2[0].getAttribute('tabindex')).toBe('-1')
    expect(tabs2[1].getAttribute('tabindex')).toBe('0')
  })

  it('ArrowRight 把焦点 + 选中挪到下一个 tab', () => {
    const setAbTab = vi.fn()
    mockUrlState({ tab: 'all', ab: 'a', setAbTab })
    const { container } = render(<ReviewPageInner />)
    const firstTab = container.querySelectorAll('[role="tab"]')[0] as HTMLElement
    fireEvent.keyDown(firstTab, { key: 'ArrowRight' })
    expect(setAbTab).toHaveBeenCalledWith('b')
  })

  it('ArrowLeft 把焦点 + 选中挪到上一个 tab', () => {
    const setAbTab = vi.fn()
    mockUrlState({ tab: 'all', ab: 'b', setAbTab })
    const { container } = render(<ReviewPageInner />)
    const secondTab = container.querySelectorAll('[role="tab"]')[1] as HTMLElement
    fireEvent.keyDown(secondTab, { key: 'ArrowLeft' })
    expect(setAbTab).toHaveBeenCalledWith('a')
  })

  it('Home 跳到第一个 tab', () => {
    const setAbTab = vi.fn()
    mockUrlState({ tab: 'all', ab: 'b', setAbTab })
    const { container } = render(<ReviewPageInner />)
    const secondTab = container.querySelectorAll('[role="tab"]')[1] as HTMLElement
    fireEvent.keyDown(secondTab, { key: 'Home' })
    expect(setAbTab).toHaveBeenCalledWith('a')
  })

  it('End 跳到最后一个 tab', () => {
    const setAbTab = vi.fn()
    mockUrlState({ tab: 'all', ab: 'a', setAbTab })
    const { container } = render(<ReviewPageInner />)
    const firstTab = container.querySelectorAll('[role="tab"]')[0] as HTMLElement
    fireEvent.keyDown(firstTab, { key: 'End' })
    expect(setAbTab).toHaveBeenCalledWith('b')
  })

  it('无关键(如 Enter / Space)不触发 setAbTab', () => {
    const setAbTab = vi.fn()
    mockUrlState({ tab: 'all', ab: 'a', setAbTab })
    const { container } = render(<ReviewPageInner />)
    const firstTab = container.querySelectorAll('[role="tab"]')[0] as HTMLElement
    fireEvent.keyDown(firstTab, { key: 'Enter' })
    fireEvent.keyDown(firstTab, { key: ' ' })
    fireEvent.keyDown(firstTab, { key: 'Tab' })
    expect(setAbTab).not.toHaveBeenCalled()
  })

  it('ArrowRight 在最后一个 tab 上循环到第一个', () => {
    const setAbTab = vi.fn()
    mockUrlState({ tab: 'all', ab: 'b', setAbTab })
    const { container } = render(<ReviewPageInner />)
    const secondTab = container.querySelectorAll('[role="tab"]')[1] as HTMLElement
    fireEvent.keyDown(secondTab, { key: 'ArrowRight' })
    expect(setAbTab).toHaveBeenCalledWith('a')
  })
})

// ====== v302+ P3+: PanelLoadingSkeleton a11y ======
// 锁定 lazy 占位的可访问名 + 状态角色。
describe('v302+ P3+: PanelLoadingSkeleton a11y', () => {
  it('默认状态:role=status + aria-live=polite + 可见 label', async () => {
    const { container, getByRole } = render(<PanelLoadingSkeleton />)
    const status = getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toBe('载入中…')
    a11yHasNoViolations(await axe(container))
  })

  it('自定义 label 正确渲染到 aria-live 区域', () => {
    const { getByRole } = render(<PanelLoadingSkeleton label="载入复盘面板…" />)
    const status = getByRole('status')
    expect(status.textContent).toBe('载入复盘面板…')
  })

  it('minHeight 锁高(防 CLS)— 渲染到 inline style', () => {
    const { getByRole } = render(<PanelLoadingSkeleton minHeight={420} />)
    const status = getByRole('status') as HTMLElement
    expect(status.style.minHeight).toBe('420px')
  })
})

// ====== v302+ P3++: tablist 焦点管理(真实 a11y bug 修复) ======
// WAI-ARIA APG 要求:
//  - Arrow 切换时,焦点必须真正移动到新 tab(.focus())
//  - 不能只改 tabIndex — 否则用户按 Tab 键会跳错位置
describe('v302+ P3++: tablist 焦点管理', () => {
  it('ArrowRight 真正把焦点移动到下一个 tab', () => {
    mockUrlState({ tab: 'all', ab: 'a' })
    const { container } = render(<ReviewPageInner />)
    const tabs = container.querySelectorAll('[role="tab"]')
    const firstTab = tabs[0] as HTMLElement
    const secondTab = tabs[1] as HTMLElement
    // 初始焦点放到第一个 tab
    firstTab.focus()
    expect(document.activeElement).toBe(firstTab)
    // ArrowRight 后,焦点必须移到第二个
    fireEvent.keyDown(firstTab, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(secondTab)
  })

  it('ArrowLeft 真正把焦点移动到上一个 tab', () => {
    mockUrlState({ tab: 'all', ab: 'b' })
    const { container } = render(<ReviewPageInner />)
    const tabs = container.querySelectorAll('[role="tab"]')
    const secondTab = tabs[1] as HTMLElement
    const firstTab = tabs[0] as HTMLElement
    secondTab.focus()
    expect(document.activeElement).toBe(secondTab)
    fireEvent.keyDown(secondTab, { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(firstTab)
  })

  it('Home 把焦点移到第一个 tab', () => {
    mockUrlState({ tab: 'all', ab: 'b' })
    const { container } = render(<ReviewPageInner />)
    const tabs = container.querySelectorAll('[role="tab"]')
    const secondTab = tabs[1] as HTMLElement
    const firstTab = tabs[0] as HTMLElement
    secondTab.focus()
    fireEvent.keyDown(secondTab, { key: 'Home' })
    expect(document.activeElement).toBe(firstTab)
  })

  it('End 把焦点移到最后一个 tab', () => {
    mockUrlState({ tab: 'all', ab: 'a' })
    const { container } = render(<ReviewPageInner />)
    const tabs = container.querySelectorAll('[role="tab"]')
    const firstTab = tabs[0] as HTMLElement
    const secondTab = tabs[1] as HTMLElement
    firstTab.focus()
    fireEvent.keyDown(firstTab, { key: 'End' })
    expect(document.activeElement).toBe(secondTab)
  })

  it('ArrowRight 在末位 tab 上循环回第一个', () => {
    mockUrlState({ tab: 'all', ab: 'b' })
    const { container } = render(<ReviewPageInner />)
    const tabs = container.querySelectorAll('[role="tab"]')
    const secondTab = tabs[1] as HTMLElement
    const firstTab = tabs[0] as HTMLElement
    secondTab.focus()
    fireEvent.keyDown(secondTab, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(firstTab)
  })
})

// ====== v302+ P3++: useDeferredValue 流向 ======
// Opt 8:selectedErrorCode 被 useDeferredValue 包了,
// 然后传给 ReflectionErrorsPanel.initialCode。这里验证值正确流动。
describe('v302+ P3++: useDeferredValue 流向 ReflectionErrorsPanel', () => {
  it('URL err=null 时 panel 收到 null', () => {
    mockUrlState({ tab: 'all', err: null })
    render(<ReviewPageInner />)
    expect((globalThis as Record<string, unknown>).__lastInitialCode).toBeNull()
  })

  it('URL err=ABC123 时 panel 收到 ABC123', () => {
    mockUrlState({ tab: 'all', err: 'ABC123' })
    render(<ReviewPageInner />)
    expect((globalThis as Record<string, unknown>).__lastInitialCode).toBe('ABC123')
  })

  it('experiences tab 不渲染 ReflectionErrorsPanel(不应触发 mock)', () => {
    // 清空标记
    ;(globalThis as Record<string, unknown>).__lastInitialCode = 'SENTINEL'
    mockUrlState({ tab: 'experiences', err: 'WONT_PASS' })
    render(<ReviewPageInner />)
    // experiences tab 没有 visibleSections.reflectionErrors,
    // panel 不应该被渲染,mock 不会被调用,标记保持原值
    expect((globalThis as Record<string, unknown>).__lastInitialCode).toBe('SENTINEL')
  })
})

// ====== v302+ P3++: 页面级 h1 ======
// 屏幕阅读器从 h1 开始扫页面,缺了 user 会迷茫。
describe('v302+ P3++: 页面级 h1 (sr-only)', () => {
  it('所有 tab 都至少有 1 个 h1(sr-only,不可见)', () => {
    for (const tab of ['all', 'journal', 'experiences'] as const) {
      mockUrlState({ tab })
      const { container } = render(<ReviewPageInner />)
      const h1s = container.querySelectorAll('h1')
      expect(h1s.length, `${tab} 缺 h1`).toBeGreaterThanOrEqual(1)
    }
  })

  it('h1 用 sr-only class(对 SR 可见,视觉隐藏)', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const h1 = container.querySelector('h1') as HTMLElement
    expect(h1.className).toContain('sr-only')
  })
})

// ====== v302+ P4: <main> aria-labelledby 链接 h1 ======
// WAI-ARIA APG 最佳实践:landmark 区域 (main / nav / aside 等) 用
// aria-labelledby 指向其标题,SR 念到 landmark 时会先念标题,而不是
// 只说"main region"。这条测试锁 id 配对 — id 改了就立刻爆。
describe('v302+ P4: <main> aria-labelledby 链接 h1', () => {
  it('<main> 有 aria-labelledby 属性', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const main = container.querySelector('main') as HTMLElement
    expect(main.getAttribute('aria-labelledby')).toBeTruthy()
  })

  it('aria-labelledby 指向真实存在的 h1 id', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const main = container.querySelector('main') as HTMLElement
    const labelledById = main.getAttribute('aria-labelledby')!
    const targetEl = container.querySelector(`#${labelledById}`)
    expect(targetEl).not.toBeNull()
    expect(targetEl?.tagName.toLowerCase()).toBe('h1')
  })

  it('h1 id 与 aria-labelledby 值完全一致(防漂移)', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const h1 = container.querySelector('h1') as HTMLElement
    const main = container.querySelector('main') as HTMLElement
    expect(h1.id).toBe(main.getAttribute('aria-labelledby'))
  })
})

// ====== v302+ P3++: /review 路由级 metadata ======
// Next.js 会在 <head> 里放这个 title,浏览器标签页 / 书签 / SR 首次进入都用它。
describe('v302+ P3++: /review 路由级 metadata', () => {
  it('layout.tsx 暴露了非空 title 覆盖根 layout', async () => {
    const { metadata } = await import('./layout')
    expect(metadata.title).toBeTruthy()
    expect(typeof metadata.title).toBe('string')
  })

  it('title 不是空字符串,且与根 layout 的通用 title 不同', async () => {
    const { metadata } = await import('./layout')
    expect(metadata.title).not.toBe('')
    // 应该比根 layout 更有针对性 ('Trident Quant Platform' 是个通用 title)
    expect(metadata.title).not.toBe('Trident Quant Platform')
  })
})

// ====== v302+ P4: REVIEW_SECTIONS ↔ DOM 同步(防 spec 漂移) ======
// _inner.tsx 渲染时硬编码 `id={REVIEW_SECTIONS.X.id}` /
// `aria-label={REVIEW_SECTIONS.X.label}`。这两条测试锁了 spec 和 DOM
// 必须一致 — 任何人改 spec 的 id 但忘了改 JSX,或反之,都会立刻爆。
describe('v302+ P4: REVIEW_SECTIONS ↔ DOM 同步', () => {
  it('all tab:dataHealth section 的 id/aria-label 与 spec 完全一致', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const section = container.querySelector(
      `#${REVIEW_SECTIONS.dataHealth.id}`,
    ) as HTMLElement
    expect(section).not.toBeNull()
    expect(section.getAttribute('aria-label')).toBe(REVIEW_SECTIONS.dataHealth.label)
  })

  it('all tab:reasoning section 的 id/aria-label 与 spec 完全一致', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const section = container.querySelector(
      `#${REVIEW_SECTIONS.reasoning.id}`,
    ) as HTMLElement
    expect(section).not.toBeNull()
    expect(section.getAttribute('aria-label')).toBe(REVIEW_SECTIONS.reasoning.label)
  })

  it('all tab:stockReflection section 的 id/aria-label 与 spec 完全一致', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const section = container.querySelector(
      `#${REVIEW_SECTIONS.stockReflection.id}`,
    ) as HTMLElement
    expect(section).not.toBeNull()
    expect(section.getAttribute('aria-label')).toBe(REVIEW_SECTIONS.stockReflection.label)
  })

  it('all tab:review section 的 id/aria-label 与 spec 完全一致', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const section = container.querySelector(
      `#${REVIEW_SECTIONS.review.id}`,
    ) as HTMLElement
    expect(section).not.toBeNull()
    expect(section.getAttribute('aria-label')).toBe(REVIEW_SECTIONS.review.label)
  })

  it('每个内容 section 都用 id 锚点(scroll-mt-3 暗示可滚动定位)', () => {
    // scroll-mt-3 暗示 section 是可滚动目标,意味着需要 id 才能定位。
    // 锁:可见的内容 section 必须有 id 属性。
    // 例外:第一个 section 是"全局时段选择"工具栏(无 scroll-mt-3,纯 UI),
    // 它不需要 anchor id(没人会 #period-selector 跳到那里)。
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const sections = Array.from(
      container.querySelectorAll('main section'),
    ) as HTMLElement[]
    const contentSections = sections.filter((s) => s.className.includes('scroll-mt-3'))
    expect(contentSections.length, '至少应有内容 section').toBeGreaterThan(0)
    for (const s of contentSections) {
      expect(s.id, `section[aria-label="${s.getAttribute('aria-label')}"] 缺 id`).toBeTruthy()
    }
  })
})
describe('v302+ P4: A/B tablist ↔ panel 完整 WAI-ARIA APG 1.2 双向链', () => {
  // WAI-ARIA APG 1.2 tab pattern 要求:
  //   1. tablist + tabs (我们已有)
  //   2. 每个 tab 有 aria-controls 指向 panel
  //   3. panel 有 id 匹配,role=tabpanel,aria-labelledby 指回当前选中的 tab
  //   4. 切 tab 时 aria-labelledby 跟随更新
  // 关键场景:SR 用户在 tab 间 Arrow 切换时,会同时听读 tab 状态 +
  // 关联 panel 的可访问名,而不是孤立切换。

  function getReviewSection(container: HTMLElement) {
    return container.querySelector(
      `#${REVIEW_SECTIONS.review.id}`,
    ) as HTMLElement
  }

  it('tablist 存在 + 2 个 tab 都有 aria-controls 指向 review-tabpanel', () => {
    mockUrlState({ tab: 'all' })
    const { container } = render(<ReviewPageInner />)
    const reviewSection = getReviewSection(container)
    const tablist = reviewSection.querySelector('[role="tablist"]') as HTMLElement
    expect(tablist).not.toBeNull()
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]')) as HTMLElement[]
    expect(tabs).toHaveLength(2)
    for (const t of tabs) {
      expect(t.getAttribute('aria-controls')).toBe('review-tabpanel')
    }
  })

  it('panel 容器有 id="review-tabpanel" + role=tabpanel + aria-labelledby 指向当前 tab', () => {
    mockUrlState({ tab: 'all', ab: 'a' })
    const { container } = render(<ReviewPageInner />)
    const panel = container.querySelector('#review-tabpanel') as HTMLElement
    expect(panel).not.toBeNull()
    expect(panel.getAttribute('role')).toBe('tabpanel')
    // 当前选中 a → aria-labelledby 指向 review-tab-a
    expect(panel.getAttribute('aria-labelledby')).toBe('review-tab-a')
  })

  it('切到 b:aria-labelledby 跟着指向 review-tab-b (跟随选中 tab)', () => {
    mockUrlState({ tab: 'all', ab: 'b' })
    const { container } = render(<ReviewPageInner />)
    const panel = container.querySelector('#review-tabpanel') as HTMLElement
    expect(panel.getAttribute('aria-labelledby')).toBe('review-tab-b')
  })

  it('双向链完整性:tab.aria-controls === panel.id 且 panel.aria-labelledby 指向选中的 tab.id', () => {
    // 端到端 a11y 守卫:任何人改坏 tabIds/panelId/aria-labelledby 都会爆
    for (const ab of ['a', 'b'] as const) {
      mockUrlState({ tab: 'all', ab })
      const { container } = render(<ReviewPageInner />)
      const reviewSection = getReviewSection(container)
      const tablist = reviewSection.querySelector('[role="tablist"]') as HTMLElement
      const selectedTab = tablist.querySelector(
        '[role="tab"][aria-selected="true"]',
      ) as HTMLElement
      const panel = container.querySelector('#review-tabpanel') as HTMLElement
      expect(panel.id).toBe('review-tabpanel')
      expect(selectedTab.getAttribute('aria-controls')).toBe(panel.id)
      expect(panel.getAttribute('aria-labelledby')).toBe(selectedTab.id)
    }
  })

  it('journal tab:没有 tablist,panel 也没有 role=tabpanel(无 tab 可控)', () => {
    // journal 模式不显示 tablist — panel 此时不受任何 tab 控制,
    // 不应有 role=tabpanel(否则 SR 会找"无主的"tab,反而困惑)。
    mockUrlState({ tab: 'journal' })
    const { container } = render(<ReviewPageInner />)
    const reviewSection = getReviewSection(container)
    expect(reviewSection.querySelector('[role="tablist"]')).toBeNull()
    // panel 容器 id 仍存在(供其他用途),但 role 应该是 undefined
    const panel = container.querySelector('#review-tabpanel') as HTMLElement | null
    if (panel) {
      // 如果 panel 存在,role 必须不存在(不能让 SR 期待一个不存在的 tab)
      expect(panel.getAttribute('role')).toBeNull()
      expect(panel.getAttribute('aria-labelledby')).toBeNull()
    }
  })
})
