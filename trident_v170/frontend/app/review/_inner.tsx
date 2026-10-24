'use client'

/**
 * app/review/_inner.tsx — /review 内容编排 (v302+ 重构 + v302+ P3 lazy chunk)
 *
 * 重构要点 (惊艳之处):
 *  - 渲染规则 = 查表 (SECTION_MATRIX[mode][key]) — 没有 (isJournal || isAll)
 *  - 复盘面板 = O(1) lookup (REVIEW_PANEL_SELECTOR[mode][ab])
 *  - period 引用稳定 (useMemo) — 切换 tab 时不会让所有 Panel 重渲染
 *  - 加新 section 只需改 _spec.ts — 本文件不动
 *  - 编译期类型保证:任何人漏声明 section 在某 mode 下的可见性,TS 立刻报错
 *  - v302+ P3: 复盘面板走 React.lazy() — 首屏 bundle 不再包含 ReviewPanel /
 *    ReviewHistoryPanel 两个大组件,experiences tab 用户首屏完全不下载
 */

import {
  Suspense,
  lazy,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type KeyboardEvent,
  type LazyExoticComponent,
  type ReactNode,
} from 'react'
// 静态 import(体积小,首屏必渲染或所有 tab 都要用):
import { FailureStatsPanel } from '@/components/replay/failure-stats-panel'
import { DataHealthPanel } from '@/components/replay/data-health-panel'
import { ReviewOverview } from '@/components/replay/review-overview'
import { AnalysisImprovePanel } from '@/components/replay/analysis-improve-panel'
import { DraftSummaryBanner } from '@/components/replay/reflection-errors/draft-summary-banner'
import { Expand, Minimize2, Copy, History, Keyboard } from 'lucide-react'
import { cn } from '@/lib/utils'
import { PeriodSelector, resolvePeriod, type PeriodRange } from '@/lib/data-channel-standards'
import { LazySection } from '@/lib/lazy-section'
import { useReviewUrlState } from '@/lib/use-review-url-state'
import { useReviewCollapse } from '@/lib/use-review-collapse'
import { useReviewShortcuts } from '@/lib/use-review-shortcuts'
import { formatViewSnapshotAsMarkdown } from '@/lib/review-view-snapshot'
import {
  PanelLoadingSkeleton,
  SectionErrorBoundary,
  SectionHeading,
  AbTablist,
  CollapsibleSection,
} from './_section'
import { SectionProgress } from './_section-progress'
import { HistoryDrawer } from './_history-drawer'
import { ShortcutsHelp } from './_shortcuts-help'
import {
  REVIEW_SECTIONS,
  SECTION_MATRIX,
  selectReviewPanel,
  type ReviewPanelKey,
} from './_spec'

// ====== v302+ P3+: 所有"重"面板都走 React.lazy() ======
// 收益:
//  - experiences tab 用户首屏根本不下载任何 review-*-panel chunk
//  - journal tab 用户也跳过 Attribution(虽然小,顺手)
//  - 即使是"当前 tab 必渲染"的 panel,lazy 也只是延后到本 tab 激活时下载,
//    不再绑死在首屏 bundle
//
// 为什么连 ReflectionErrorsPanel(641 行)/ StockReflectionPanel(501 行)
// 也要 lazy:这些 panel 自身就 100~640 行,体积大,experiences tab 跳过来
// 时根本不渲染。与"复盘面板"是一回事 — 在所有 tab 间按需加载。
const LAZY_PANELS = {
  // 复盘面板(带 period prop)
  ReviewPanel: lazy(() =>
    import('@/components/replay/review-panel').then((m) => ({ default: m.ReviewPanel })),
  ),
  ReviewHistoryPanel: lazy(() =>
    import('@/components/replay/review-history-panel').then((m) => ({
      default: m.ReviewHistoryPanel,
    })),
  ),
  // 诊断 / 推理 / 股票反思 — 体积大但不带 prop(或仅 initialCode)
  ReflectionErrorsPanel: lazy(() =>
    import('@/components/replay/reflection-errors-panel').then((m) => ({
      default: m.ReflectionErrorsPanel,
    })),
  ),
  AttributionReviewPanel: lazy(() =>
    import('@/components/replay/attribution-review-panel').then((m) => ({
      default: m.AttributionReviewPanel,
    })),
  ),
  ReasoningReviewPanel: lazy(() =>
    import('@/components/replay/reasoning-review-panel').then((m) => ({
      default: m.ReasoningReviewPanel,
    })),
  ),
  StockReflectionPanel: lazy(() =>
    import('@/components/replay/stock-reflection-panel').then((m) => ({
      default: m.StockReflectionPanel,
    })),
  ),
}
// 不加 satisfies:每个 panel 的 props 不同,无法用单个 ComponentType 收窄;
// 下面的 REVIEW_PANEL_COMPONENTS + 调用点(用泛型)各自负责 prop 类型。
type LazyPanelKey = keyof typeof LAZY_PANELS

/** 复盘面板的强类型注册表 — 单独抽出来是因为它需要带 period prop */
const REVIEW_PANEL_COMPONENTS: {
  readonly [K in ReviewPanelKey]: LazyExoticComponent<ComponentType<{ period: PeriodRange }>>
} = {
  ReviewPanel: LAZY_PANELS.ReviewPanel as LazyExoticComponent<
    ComponentType<{ period: PeriodRange }>
  >,
  ReviewHistoryPanel: LAZY_PANELS.ReviewHistoryPanel as LazyExoticComponent<
    ComponentType<{ period: PeriodRange }>
  >,
}

/** section 是否需要 ErrorBoundary 包裹 — 从 spec 派生 */
function wrapWithErrorBoundary(name: string, errorBoundary: boolean, content: ReactNode) {
  return errorBoundary ? <SectionErrorBoundary name={name}>{content}</SectionErrorBoundary> : content
}

/** v302+ P4: 每个 review section 的 SR 友好加载文案
 *  Screen reader 念 PanelLoadingSkeleton 时会读出 label,中文给用户
 *  上下文("载入推理审查…")比通用"载入中…"更清楚。
 *  按 section key 索引(不是 panel key — section spec 控制渲染策略)。
 *  保持单一事实源:任何加 section 都只改这里一行。 */
const SECTION_LABELS: Partial<Record<keyof typeof REVIEW_SECTIONS, string>> = {
  reflectionErrors: '载入诊断 · 反思错误…',
  attribution: '载入归因审查…',
  reasoning: '载入推理审查…',
  stockReflection: '载入股票反思…',
  review: '载入复盘面板…',
}

/** v302+ P4: review section 的渲染入口 — 集中 LazySection + Suspense +
 *  PanelLoadingSkeleton 三件套。调用者只关心"渲染哪个 panel 组件",
 *  不需要重复写 Suspense/label/minHeight 等 boilerplate。
 *
 *  用法:
 *    {renderReviewSection('reasoning', <LAZY_PANELS.ReasoningReviewPanel />)}
 *
 *  返回 null 当 content 为 null(用于 ReviewPanelComponent 查不到的情况)。
 */
function renderReviewSection<K extends keyof typeof REVIEW_SECTIONS>(
  specKey: K,
  content: ReactNode,
  labelOverride?: string,
) {
  if (content == null) return null
  const spec = REVIEW_SECTIONS[specKey]
  if (!spec) return content
  const label = labelOverride ?? SECTION_LABELS[specKey] ?? '载入中…'
  const inner = (
    <Suspense
      fallback={<PanelLoadingSkeleton minHeight={spec.minHeight} label={label} />}
    >
      {content}
    </Suspense>
  )
  return spec.lazy ? <LazySection minHeight={spec.minHeight}>{inner}</LazySection> : inner
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
  // v302+ P3+: useMemo 包住 — 避免每次渲染重新查 LAZY_PANELS
  // (虽然 lazy() 引用稳定,但用 useMemo 也防止下游 prop 比较失败时
  // 触发不必要的 re-render)
  const ReviewPanelComponent = useMemo(
    () => (reviewPanelKey ? REVIEW_PANEL_COMPONENTS[reviewPanelKey] : null),
    [reviewPanelKey],
  )

  // v302+ P3+: useDeferredValue 包 selectedErrorCode —
  // 用户从 FailureStatsPanel 点选错误码时,ReflectionErrorsPanel(641 行)
  // 会重新渲染。用 deferred value 让点击保持响应,ReflectionErrorsPanel
  // 在 React 调度空闲时再更新。initialCode 变化延迟可见但不影响输入。
  const deferredErrorCode = useDeferredValue(selectedErrorCode)

  // v302+ P3+: A/B tablist 键盘导航 — 索引 0 = A, 1 = B
  // tabRefs 用于 Arrow 切换时真正把焦点移到新 tab
  // (WAI-ARIA APG 要求焦点跟随选中,不能只改 tabIndex)
  // v302+ P4: tabRefs 仍由父组件管理(子组件不能直接 ref 父组件的 DOM),
  // 但键盘 handler 逻辑已下放到 <AbTablist> 内部的 tablistOnKeyDown。
  const tabRefs = useRef<[(HTMLButtonElement | null), (HTMLButtonElement | null)]>([null, null])

  // ====== v302+ P6: 折叠状态 / 抽屉 / 帮助弹窗 / 快捷键 集成 ======

  /** 折叠 / 展开状态(URL 持久化) */
  const collapse = useReviewCollapse()

  /** 复盘历史抽屉开关 */
  const [historyOpen, setHistoryOpen] = useState(false)

  /** 快捷键帮助弹窗开关 */
  const [helpOpen, setHelpOpen] = useState(false)

  /** 复制反馈: 0 = idle, >0 = 倒计时(ms),<=0 隐藏 */
  const [copyFlash, setCopyFlash] = useState(0)

  /** 复制当前视图为 Markdown(写到剪贴板 + flash 反馈 2s) */
  const handleCopyMarkdown = useCallback(() => {
    const sectionList = Object.entries(REVIEW_SECTIONS).map(([key, spec]) => ({
      id: spec.id,
      label: spec.label,
      visible: visibleSections[key as keyof typeof visibleSections] ?? false,
    }))
    const md = formatViewSnapshotAsMarkdown({
      urlState: { tab: viewMode, ab: abTab, err: selectedErrorCode, period: periodKey, customRange: customDateRange },
      totalSectionCount: sectionList.length,
      expandedSectionCount: sectionList.filter((s) => s.visible && !collapse.isCollapsed(s.id)).length,
      fullUrl: typeof window !== 'undefined' ? window.location.href : '',
      sections: sectionList,
    })
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      void navigator.clipboard.writeText(md).then(
        () => {
          setCopyFlash(2000)
          setTimeout(() => setCopyFlash(0), 2000)
        },
        () => {
          setCopyFlash(2000)
          setTimeout(() => setCopyFlash(0), 2000)
        },
      )
    }
  }, [
    viewMode,
    abTab,
    selectedErrorCode,
    periodKey,
    customDateRange,
    visibleSections,
    collapse,
  ])

  /** 切换全部展开/折叠 — 当前如果有任一折叠则全展开,否则全折叠 */
  const handleToggleExpandAll = useCallback(() => {
    const sectionIds = Object.values(REVIEW_SECTIONS).map((s) => s.id)
    const anyCollapsed = sectionIds.some((id) => collapse.isCollapsed(id))
    if (anyCollapsed) {
      collapse.expandAll()
    } else {
      collapse.collapseAll(sectionIds)
    }
  }, [collapse])

  /** 键盘快捷键: 跳到第 N 个 section(j/k 走 prev/next 分支) */
  const handleJumpToSection = useCallback(
    (idx: number) => {
      const sectionIds = Object.values(REVIEW_SECTIONS).map((s) => s.id)
      // 哨兵: -1 = 下一个, -2 = 上一个, -3 = 顶部, -4 = 底部
      if (idx === -1) idx = currentSectionIdx + 1
      else if (idx === -2) idx = currentSectionIdx - 1
      else if (idx === -3) idx = 0
      else if (idx === -4) idx = sectionIds.length - 1
      const targetId = sectionIds[idx]
      if (!targetId) return
      const el = typeof document !== 'undefined' ? document.getElementById(targetId) : null
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        el.focus({ preventScroll: true })
      }
    },
    // currentSectionIdx 通过 state 维护(下面 useEffect 写入)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )

  /** 跟踪当前在视口里的 section index(给 j/k 用) */
  const [currentSectionIdx, setCurrentSectionIdx] = useState(0)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof IntersectionObserver === 'undefined') return
    const sectionIds = Object.values(REVIEW_SECTIONS).map((s) => s.id)
    const els = sectionIds
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el != null)
    if (els.length === 0) return
    const observer = new IntersectionObserver(
      (entries) => {
        // 找最靠上的相交 section
        const intersecting = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (intersecting.length > 0) {
          const id = intersecting[0].target.id
          const idx = sectionIds.indexOf(id)
          if (idx >= 0) setCurrentSectionIdx(idx)
        }
      },
      { rootMargin: '-10% 0px -70% 0px', threshold: 0 },
    )
    for (const el of els) observer.observe(el)
    return () => observer.disconnect()
  }, [])

  /** 注册全局快捷键 */
  useReviewShortcuts({
    onJumpToSection: handleJumpToSection,
    sectionCount: Object.keys(REVIEW_SECTIONS).length,
    allExpanded: Object.values(REVIEW_SECTIONS).every((s) => !collapse.isCollapsed(s.id)),
    onToggleExpandAll: handleToggleExpandAll,
    onCopyMarkdown: handleCopyMarkdown,
    onShowHelp: () => setHelpOpen(true),
    onEscape: () => {
      if (helpOpen) setHelpOpen(false)
      else if (historyOpen) setHistoryOpen(false)
      else if (selectedErrorCode) setSelectedErrorCode(null)
    },
  })

  /** 用于 SectionProgress 的 section 列表(从 REVIEW_SECTIONS 派生) */
  const allSectionList = useMemo(
    () =>
      Object.values(REVIEW_SECTIONS).map((s) => ({
        id: s.id,
        label: s.label,
      })),
    [],
  )
  const expandedSectionCount = useMemo(
    () => allSectionList.filter((s) => !collapse.isCollapsed(s.id)).length,
    [allSectionList, collapse],
  )

  return (
    <main
      aria-labelledby="review-page-title"
      className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4"
    >
      {/* v302+ P3++: 页面级 <h1> — heading hierarchy 顶层
       * 屏幕阅读器从 <h1> 开始扫页面,缺了这个用户会迷茫。
       * 视觉上 sr-only(只对 SR 可见) — 标题已经在 period selector 旁边的
       * "复盘系统" 等段落里有视觉信息,不需要重复。
       * id="review-page-title" 配合 <main aria-labelledby> — WAI-ARIA 推荐
       * 实践:landmark 区域用 aria-labelledby 指向其可视/可读标题,SR 念到
       * <main> 时会先读"复盘系统 — /review 路由,landmark" 而不是含糊的
       * "main landmark"。 */}
      <h1 id="review-page-title" className="sr-only">
        复盘系统 — /review 路由
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

      {/* v302+ P6: 工具栏 — 折叠/复制/历史/快捷键 4 个按钮 */}
      <section
        aria-label="工具栏"
        className="flex flex-wrap items-center gap-1.5 rounded-md border border-border/60 bg-card/50 px-2 py-1.5"
      >
        <button
          type="button"
          onClick={handleToggleExpandAll}
          aria-label={
            Object.values(REVIEW_SECTIONS).every((s) => !collapse.isCollapsed(s.id))
              ? '折叠全部段位'
              : '展开全部段位'
          }
          className="inline-flex h-6 items-center gap-1 rounded border border-border bg-background px-2 font-mono text-[10px] text-foreground hover:bg-secondary"
        >
          {Object.values(REVIEW_SECTIONS).every((s) => !collapse.isCollapsed(s.id)) ? (
            <>
              <Minimize2 className="h-3 w-3" aria-hidden /> 折叠全部
            </>
          ) : (
            <>
              <Expand className="h-3 w-3" aria-hidden /> 展开全部
            </>
          )}
        </button>
        <button
          type="button"
          onClick={handleCopyMarkdown}
          aria-label="复制当前视图为 Markdown"
          className={cn(
            'inline-flex h-6 items-center gap-1 rounded border border-border bg-background px-2 font-mono text-[10px] text-foreground hover:bg-secondary',
            copyFlash > 0 && 'border-primary bg-primary/10 text-primary',
          )}
        >
          <Copy className="h-3 w-3" aria-hidden />
          {copyFlash > 0 ? '已复制' : '复制为 MD'}
        </button>
        <button
          type="button"
          onClick={() => setHistoryOpen(true)}
          aria-label="打开复盘历史抽屉"
          aria-expanded={historyOpen}
          className="inline-flex h-6 items-center gap-1 rounded border border-border bg-background px-2 font-mono text-[10px] text-foreground hover:bg-secondary"
        >
          <History className="h-3 w-3" aria-hidden /> 复盘历史
        </button>
        <button
          type="button"
          onClick={() => setHelpOpen(true)}
          aria-label="显示键盘快捷键帮助"
          className="inline-flex h-6 items-center gap-1 rounded border border-border bg-background px-2 font-mono text-[10px] text-foreground hover:bg-secondary"
        >
          <Keyboard className="h-3 w-3" aria-hidden /> 快捷键
        </button>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          按 <kbd className="rounded border border-border bg-secondary px-1">?</kbd> 唤出帮助
        </span>
      </section>

      {/* v302+ P6: 段位进度(sticky 顶部) */}
      <SectionProgress
        total={allSectionList.length}
        expanded={expandedSectionCount}
        sections={allSectionList}
      />

      <div className="flex min-w-0 flex-1 flex-col gap-3">
        {/* 草稿总览横幅 — 三种 tab 都展示 */}
        {visibleSections.draftSummary && <DraftSummaryBanner />}

        {/* 总览 */}
        <CollapsibleSection
          sectionId="overview"
          aria-label="总览"
          index={1}
          collapsed={collapse.isCollapsed('overview')}
          onToggle={() => collapse.toggle('overview')}
        >
          <ReviewOverview period={period} />
          {visibleSections.failureStats && (
            <FailureStatsPanel onSelectCode={(code) => setSelectedErrorCode(String(code))} />
          )}
        </CollapsibleSection>

        {/* 诊断 */}
        {(visibleSections.reflectionErrors || visibleSections.attribution) && (
          <CollapsibleSection
            sectionId="diagnose"
            aria-label="诊断"
            index={2}
            collapsed={collapse.isCollapsed('diagnose')}
            onToggle={() => collapse.toggle('diagnose')}
          >
            {visibleSections.reflectionErrors &&
              renderReviewSection(
                'reflectionErrors',
                <LAZY_PANELS.ReflectionErrorsPanel initialCode={deferredErrorCode} />,
              )}
            {visibleSections.attribution &&
              renderReviewSection('attribution', <LAZY_PANELS.AttributionReviewPanel />)}
          </CollapsibleSection>
        )}

        {/* 推理审查 */}
        {visibleSections.reasoning && (
          <CollapsibleSection
            sectionId={REVIEW_SECTIONS.reasoning.id}
            aria-label={REVIEW_SECTIONS.reasoning.label}
            index={3}
            collapsed={collapse.isCollapsed(REVIEW_SECTIONS.reasoning.id)}
            onToggle={() => collapse.toggle(REVIEW_SECTIONS.reasoning.id)}
          >
            {wrapWithErrorBoundary(
              REVIEW_SECTIONS.reasoning.label,
              REVIEW_SECTIONS.reasoning.errorBoundary,
              renderReviewSection('reasoning', <LAZY_PANELS.ReasoningReviewPanel />),
            )}
          </CollapsibleSection>
        )}

        {/* 股票反思 */}
        {visibleSections.stockReflection && (
          <CollapsibleSection
            sectionId={REVIEW_SECTIONS.stockReflection.id}
            aria-label={REVIEW_SECTIONS.stockReflection.label}
            index={4}
            collapsed={collapse.isCollapsed(REVIEW_SECTIONS.stockReflection.id)}
            onToggle={() => collapse.toggle(REVIEW_SECTIONS.stockReflection.id)}
          >
            {renderReviewSection('stockReflection', <LAZY_PANELS.StockReflectionPanel />)}
          </CollapsibleSection>
        )}

        {/* 数据健康 — 永远显示 */}
        <CollapsibleSection
          sectionId={REVIEW_SECTIONS.dataHealth.id}
          aria-label={REVIEW_SECTIONS.dataHealth.label}
          index={5}
          collapsed={collapse.isCollapsed(REVIEW_SECTIONS.dataHealth.id)}
          onToggle={() => collapse.toggle(REVIEW_SECTIONS.dataHealth.id)}
        >
          <DataHealthPanel />
        </CollapsibleSection>

        {/* 复盘 — A/B tab + lookup(O(1)) 选面板 */}
        {visibleSections.review && (
          <CollapsibleSection
            sectionId={REVIEW_SECTIONS.review.id}
            aria-label={REVIEW_SECTIONS.review.label}
            index={6}
            collapsed={collapse.isCollapsed(REVIEW_SECTIONS.review.id)}
            onToggle={() => collapse.toggle(REVIEW_SECTIONS.review.id)}
          >
            {/* v302+ P4: A/B tablist 提取为组件,自带 WAI-ARIA APG 1.2
             *  完整属性(aria-controls + Roving Tabindex + 键盘 .focus())。
             *  仅在 isAll 模式下显示(其它模式没有 tablist 可控)。 */}
            {isAll && (
              <AbTablist
                selected={abTab}
                onSelect={setAbTab}
                tabIds={['review-tab-a', 'review-tab-b']}
                panelId="review-tabpanel"
                tabRefs={tabRefs}
              />
            )}
            {wrapWithErrorBoundary(
              REVIEW_SECTIONS.review.label,
              REVIEW_SECTIONS.review.errorBoundary,
              // v302+ P4: 用 role="tabpanel" + aria-labelledby 链回当前 tab,
              // 配合上方 <AbTablist> 的 aria-controls 构成 WAI-ARIA APG 完整双向链。
              // 关键场景:SR 用户在 tab 间 Arrow 切换时,会同时听读 tab 状态和
              // 关联 panel 的内容(不再是孤立切换)。
              <div
                id="review-tabpanel"
                role={isAll ? 'tabpanel' : undefined}
                aria-labelledby={isAll ? `review-tab-${abTab}` : undefined}
              >
                {renderReviewSection(
                  'review',
                  ReviewPanelComponent ? <ReviewPanelComponent period={period} /> : null,
                )}
              </div>,
            )}
          </CollapsibleSection>
        )}

        {/* 改进 */}
        {visibleSections.improve && (
          <CollapsibleSection
            sectionId={REVIEW_SECTIONS.improve.id}
            aria-label={REVIEW_SECTIONS.improve.label}
            index={7}
            collapsed={collapse.isCollapsed(REVIEW_SECTIONS.improve.id)}
            onToggle={() => collapse.toggle(REVIEW_SECTIONS.improve.id)}
          >
            <SectionHeading icon="🛠" title={REVIEW_SECTIONS.improve.label} subtitle="因果图谱 / 因果链库 的审批与统计" />
            <AnalysisImprovePanel />
          </CollapsibleSection>
        )}
      </div>

      {/* v302+ P6: 抽屉 / 弹窗 集中渲染,放在 main 末尾(对 a11y 焦点顺序友好) */}
      <HistoryDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} />
      <ShortcutsHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
    </main>
  )
}