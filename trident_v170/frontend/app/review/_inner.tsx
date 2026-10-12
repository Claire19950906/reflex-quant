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

import { useMemo, type ComponentType, type ReactNode } from 'react'
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
import { SectionErrorBoundary, SectionHeading } from './_section'
import {
  REVIEW_SECTIONS,
  SECTION_MATRIX,
  selectReviewPanel,
  type ReviewPanelKey,
} from './_spec'

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
    <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4">
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
            {isAll && (
              <div
                role="tablist"
                aria-label="复盘模式"
                className="inline-flex self-start rounded-lg border border-border bg-secondary/50 p-0.5"
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={abTab === 'a'}
                  onClick={() => setAbTab('a')}
                  className={cn(
                    'rounded-md px-3 py-1.5 font-mono text-xs transition-all',
                    abTab === 'a'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  A · 历史真实交易复盘
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={abTab === 'b'}
                  onClick={() => setAbTab('b')}
                  className={cn(
                    'rounded-md px-3 py-1.5 font-mono text-xs transition-all',
                    abTab === 'b'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  B · 快速评测复盘
                </button>
              </div>
            )}
            {wrapWithErrorBoundary(
              REVIEW_SECTIONS.review.label,
              REVIEW_SECTIONS.review.errorBoundary,
              wrapWithLazy(
                REVIEW_SECTIONS.review.lazy,
                REVIEW_SECTIONS.review.minHeight,
                ReviewPanelComponent ? <ReviewPanelComponent period={period} /> : null,
              ),
            )}
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