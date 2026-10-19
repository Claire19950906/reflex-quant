'use client'

/**
 * app/review/_inner.tsx — /review 内容编排 (v302+ 重构)
 *
 * 重构要点 (惊艳之处):
 *  - 渲染规则 = 查表 (SECTION_MATRIX[mode][key]) — 没有 (isJournal || isAll)
 *  - 复盘面板 = O(1) lookup (REVIEW_PANEL_SELECTOR[mode][ab])
 *  - period 引用稳定 (useMemo) — 切换 tab 时不会让所有 Panel 重渲染
 *  - 加新 section 只需改 _spec.ts — 本文件不动
 *  - 编译期类型保证:任何人漏声明 section 在某 mode 下的可见性,TS 立刻报错
 */

import { useCallback, useMemo, useRef, type ComponentType, type KeyboardEvent, type ReactNode } from 'react'
import { ReviewPanel } from '@/components/replay/review-panel'
import { ReviewHistoryPanel } from '@/components/replay/review-history-panel'
import { FailureStatsPanel } from '@/components/replay/failure-stats-panel'
import { AttributionReviewPanel } from '@/components/replay/attribution-review-panel'
import { ReflectionErrorsPanel } from '@/components/replay/reflection-errors-panel'
import { ReasoningReviewPanel } from '@/components/replay/reasoning-review-panel'
import { StockReflectionPanel } from '@/components/replay/stock-reflection-panel'
import { DataHealthPanel } from '@/components/replay/data-health-panel'
import { ReviewOverview } from '@/components/replay/review-overview'
import { AnalysisImprovePanel } from '@/components/replay/analysis-improve-panel'
import { DraftSummaryBanner } from '@/components/replay/reflection-errors/draft-summary-banner'
import { cn } from '@/lib/utils'
import { PeriodSelector, resolvePeriod, type PeriodRange } from '@/lib/data-channel-standards'
import { LazySection } from '@/lib/lazy-section'
import { useReviewUrlState } from '@/lib/use-review-url-state'
import type { AbTab } from '@/lib/use-review-url-state'
import { SectionErrorBoundary, SectionHeading } from './_section'
import {
  REVIEW_SECTIONS,
  SECTION_MATRIX,
  selectReviewPanel,
  type ReviewPanelKey,
} from './_spec'

// v302+ P4: WAI-ARIA APG 1.2 tab pattern — 锁定 id 命名(测试断言依赖)
//   - tab a/b:    review-tab-a / review-tab-b
//   - tabpanel:   review-tabpanel
// 改这些 id 会同时炸 4 条双向链测试
const REVIEW_TAB_IDS: Record<AbTab, string> = {
  a: 'review-tab-a',
  b: 'review-tab-b',
}
const REVIEW_TABPANEL_ID = 'review-tabpanel'

// tab 顺序(roving tabindex 用) — 单源,改顺序只需改这里
const AB_TABS: readonly AbTab[] = ['a', 'b'] as const
function nextTab(cur: AbTab, dir: 1 | -1): AbTab {
  const i = AB_TABS.indexOf(cur)
  const n = AB_TABS.length
  return AB_TABS[(i + dir + n) % n]!
}
function clampTab(cur: AbTab, target: AbTab): AbTab {
  return target
}

// 复盘面板组件注册表 — 把组件 import 与 spec key 绑定
const REVIEW_PANEL_COMPONENTS: {
  readonly [K in ReviewPanelKey]: ComponentType<{ period: PeriodRange }>
} = {
  ReviewPanel,
  ReviewHistoryPanel,
}

/** section 是否需要 ErrorBoundary 包裹 — 从 spec 派生 */
function wrapWithErrorBoundary(name: string, errorBoundary: boolean, content: ReactNode) {
  return errorBoundary ? <SectionErrorBoundary name={name}>{content}</SectionErrorBoundary> : content
}

/** section 是否需要 LazySection 包裹 — 从 spec 派生 */
function wrapWithLazy(
  lazy: boolean,
  minHeight: number | undefined,
  content: ReactNode,
) {
  return lazy ? <LazySection minHeight={minHeight!}>{content}</LazySection> : content
}

/**
 * v302+ P3+/P3++/P4: 完整 WAI-ARIA APG 1.2 tab pattern 实现
 *  - Roving tabindex(选中=0,其他=-1)— 让 Tab 进出 tablist 一次
 *  - ArrowLeft/Right 切换 + 末位循环
 *  - Home/End 跳到首/末
 *  - 切换时 .focus() 真正移动焦点(不止改 tabIndex)
 *  - 每个 tab 的 id + aria-controls 指向 panel,panel 的 aria-labelledby 指回选中 tab
 */
function ReviewModeTabs({
  abTab,
  onChange,
}: {
  abTab: AbTab
  onChange: (next: AbTab) => void
}) {
  const tabARef = useRef<HTMLButtonElement>(null)
  const tabBRef = useRef<HTMLButtonElement>(null)

  const focusTab = useCallback((t: AbTab) => {
    const ref = t === 'a' ? tabARef : tabBRef
    ref.current?.focus()
  }, [])

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLButtonElement>) => {
      let target: AbTab | null = null
      switch (e.key) {
        case 'ArrowRight':
          target = nextTab(abTab, 1)
          break
        case 'ArrowLeft':
          target = nextTab(abTab, -1)
          break
        case 'Home':
          target = AB_TABS[0]!
          break
        case 'End':
          target = AB_TABS[AB_TABS.length - 1]!
          break
        default:
          return
      }
      e.preventDefault()
      onChange(clampTab(abTab, target))
      // 把焦点真正挪到新 tab — 否则下次按 Tab 键会跳错位置
      focusTab(target)
    },
    [abTab, onChange, focusTab],
  )

  const tabClass = (active: boolean) =>
    cn(
      'rounded-md px-3 py-1.5 font-mono text-xs transition-all',
      active
        ? 'bg-background text-foreground shadow-sm'
        : 'text-muted-foreground hover:text-foreground',
    )

  return (
    <div
      role="tablist"
      aria-label="复盘模式"
      className="inline-flex self-start rounded-lg border border-border bg-secondary/50 p-0.5"
    >
      <button
        ref={tabARef}
        id={REVIEW_TAB_IDS.a}
        type="button"
        role="tab"
        aria-selected={abTab === 'a'}
        aria-controls={REVIEW_TABPANEL_ID}
        // Roving Tabindex: 只有"当前选中的 tab"才是 Tab 序列的一部分
        tabIndex={abTab === 'a' ? 0 : -1}
        onClick={() => onChange('a')}
        onKeyDown={handleKeyDown}
        className={tabClass(abTab === 'a')}
      >
        A · 历史真实交易复盘
      </button>
      <button
        ref={tabBRef}
        id={REVIEW_TAB_IDS.b}
        type="button"
        role="tab"
        aria-selected={abTab === 'b'}
        aria-controls={REVIEW_TABPANEL_ID}
        tabIndex={abTab === 'b' ? 0 : -1}
        onClick={() => onChange('b')}
        onKeyDown={handleKeyDown}
        className={tabClass(abTab === 'b')}
      >
        B · 快速评测复盘
      </button>
    </div>
  )
}

export function ReviewPageInner() {
  const urlState = useReviewUrlState()
  const {
    tab: viewMode,
    ab: abTab,
    err: selectedErrorCode,
    period: periodKey,
    customRange: customDateRange,
    setErrorCode: setSelectedErrorCode,
    setAbTab,
    setPeriod: setPeriodKey,
    setCustomDateRange,
  } = urlState

  // 阶段 C 核心:period 引用稳定 — 防止切换 tab 时让所有 Panel 收到"新 prop"
  const period: PeriodRange = useMemo(
    () => resolvePeriod(periodKey, customDateRange),
    [periodKey, customDateRange],
  )

  // 阶段 A 核心:单一查询取代 N 个布尔变量
  const visibleSections = SECTION_MATRIX[viewMode]
  const isAll = viewMode === 'all'

  // 阶段 A 核心:复盘面板选择 = O(1) lookup
  const reviewPanelKey = selectReviewPanel(viewMode, abTab)
  const ReviewPanelComponent = reviewPanelKey ? REVIEW_PANEL_COMPONENTS[reviewPanelKey] : null

  return (
    <main
      aria-labelledby="review-page-heading"
      className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4"
    >
      {/* v302+ P3++: 页面级 h1 — SR 从 h1 开始扫页面
          视觉隐藏但 screen reader 可见,正好配合 <main aria-labelledby> */}
      <h1 id="review-page-heading" className="sr-only">
        复盘 · Trident 量化平台诊断与交易复盘
      </h1>
      <section
        aria-label="全局时段选择"
        className="flex flex-wrap items-center gap-2 rounded-md border border-border/60 bg-secondary/30 px-3 py-2"
      >
        <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">📅 全局时段</span>
        <PeriodSelector
          value={periodKey}
          onChange={setPeriodKey}
          customDateRange={customDateRange}
          onCustomChange={setCustomDateRange}
          size="sm"
        />
        <span
          aria-live="polite"
          className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[9px] text-primary"
        >
          当前:{period.label}
        </span>
      </section>

      <div className="flex min-w-0 flex-1 flex-col gap-3">
        {/* 草稿总览横幅 — 三种 tab 都展示 */}
        {visibleSections.draftSummary && <DraftSummaryBanner />}

        {/* 总览 */}
        <section id="overview" aria-label="总览" className="scroll-mt-3">
          <ReviewOverview period={period} />
          {visibleSections.failureStats && (
            <FailureStatsPanel onSelectCode={(code) => setSelectedErrorCode(String(code))} />
          )}
        </section>

        {/* 诊断 */}
        {(visibleSections.reflectionErrors || visibleSections.attribution) && (
          <section id="diagnose" aria-label="诊断" className="scroll-mt-3">
            {visibleSections.reflectionErrors &&
              wrapWithLazy(
                REVIEW_SECTIONS.reflectionErrors.lazy,
                REVIEW_SECTIONS.reflectionErrors.minHeight,
                <ReflectionErrorsPanel initialCode={selectedErrorCode} />,
              )}
            {visibleSections.attribution &&
              wrapWithLazy(
                REVIEW_SECTIONS.attribution.lazy,
                REVIEW_SECTIONS.attribution.minHeight,
                <AttributionReviewPanel />,
              )}
          </section>
        )}

        {/* 推理审查 */}
        {visibleSections.reasoning && (
          <section
            id={REVIEW_SECTIONS.reasoning.id}
            aria-label={REVIEW_SECTIONS.reasoning.label}
            className="scroll-mt-3"
          >
            {wrapWithErrorBoundary(
              REVIEW_SECTIONS.reasoning.label,
              REVIEW_SECTIONS.reasoning.errorBoundary,
              wrapWithLazy(
                REVIEW_SECTIONS.reasoning.lazy,
                REVIEW_SECTIONS.reasoning.minHeight,
                <ReasoningReviewPanel />,
              ),
            )}
          </section>
        )}

        {/* 股票反思 */}
        {visibleSections.stockReflection && (
          <section
            id={REVIEW_SECTIONS.stockReflection.id}
            aria-label={REVIEW_SECTIONS.stockReflection.label}
            className="scroll-mt-3"
          >
            {wrapWithLazy(
              REVIEW_SECTIONS.stockReflection.lazy,
              REVIEW_SECTIONS.stockReflection.minHeight,
              <StockReflectionPanel />,
            )}
          </section>
        )}

        {/* 数据健康 — 永远显示 */}
        <section
          id={REVIEW_SECTIONS.dataHealth.id}
          aria-label={REVIEW_SECTIONS.dataHealth.label}
          className="scroll-mt-3"
        >
          <DataHealthPanel />
        </section>

        {/* 复盘 — A/B tab + lookup(O(1)) 选面板 */}
        {visibleSections.review && (
          <section
            id={REVIEW_SECTIONS.review.id}
            aria-label={REVIEW_SECTIONS.review.label}
            className="scroll-mt-3"
          >
            {isAll && <ReviewModeTabs abTab={abTab} onChange={setAbTab} />}
            {/* v302+ P4: tabpanel 容器
                - isAll 时:role=tabpanel + aria-labelledby 跟随选中 tab
                  (WAI-ARIA APG 1.2 tab pattern 双向链:tab.aria-controls → panel.id,
                   panel.aria-labelledby → selected tab.id)
                - journal 时:仅保留 id 锚点,不挂 role(没有 tab 可控,挂 role 反而
                  误导 SR 去找不存在的 tab) */}
            <div
              id={REVIEW_TABPANEL_ID}
              role={isAll ? 'tabpanel' : undefined}
              aria-labelledby={isAll ? REVIEW_TAB_IDS[abTab] : undefined}
            >
              {wrapWithErrorBoundary(
                REVIEW_SECTIONS.review.label,
                REVIEW_SECTIONS.review.errorBoundary,
                wrapWithLazy(
                  REVIEW_SECTIONS.review.lazy,
                  REVIEW_SECTIONS.review.minHeight,
                  ReviewPanelComponent ? <ReviewPanelComponent period={period} /> : null,
                ),
              )}
            </div>
          </section>
        )}

        {/* 改进 */}
        {visibleSections.improve && (
          <section
            id={REVIEW_SECTIONS.improve.id}
            aria-label={REVIEW_SECTIONS.improve.label}
            className="scroll-mt-3"
          >
            <SectionHeading icon="🛠" title={REVIEW_SECTIONS.improve.label} subtitle="因果图谱 / 因果链库 的审批与统计" />
            <AnalysisImprovePanel />
          </section>
        )}
      </div>
    </main>
  )
}