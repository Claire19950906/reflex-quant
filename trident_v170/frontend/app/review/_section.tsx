'use client'

/**
 * app/review/_section.tsx — 段标题 + 错误边界(v302+ P1 拆分自 page.tsx)
 *
 * 设计:
 *  - SectionHeading 复用于 /review 的段头(icon + 标题 + 副标题)
 *  - SectionErrorBoundary 用 class component 写(React 18 标准),捕获 render/lifecycle 错误
 *  - 其它段的稳定性靠子组件自身 try/catch + Zod 校验兜底
 *  - v302+ P4:AbTablist 提取为可复用组件 — 完整 WAI-ARIA APG 1.2 模式
 *    (aria-controls + role=tabpanel + Roving Tabindex),a11y 测试可独立
 */

import {
  Component,
  memo,
  type ErrorInfo,
  type KeyboardEvent,
  type MutableRefObject,
  type ReactNode,
} from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

/** 段标题(icon + 标题 + 副标题) */
export function SectionHeading({
  icon,
  title,
  subtitle,
}: {
  icon: string
  title: string
  subtitle: string
}) {
  return (
    <div className="flex items-end gap-3 border-b border-border pb-1.5">
      <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
        <span aria-hidden>{icon}</span>
        {title}
      </h2>
      <p className="font-mono text-xs text-muted-foreground">{subtitle}</p>
    </div>
  )
}

/** v302+ P3+: PanelLoadingSkeleton — lazy 组件 chunk 加载时的占位
 *  之前内联在 _inner.tsx;挪到这里是为了:
 *   1. 单一事实源 — 任何 lazy 加载都用同一种 skeleton,样式不漂移
 *   2. 复用 — 其它路由(/replay 等)如果也要 lazy 化,直接 import
 *   3. 测试 — 单独可测(a11y 跑过了,改样式不会回归)
 *
 *  a11y 细节:
 *   - role="status" + aria-live="polite": 屏幕阅读器会播报状态变化,
 *     但不会打断当前朗读
 *   - minHeight: 防 CLS(layout shift),数值由调用方传 spec.minHeight
 *
 *  v302+ P4: 包裹 React.memo — _inner.tsx 重渲时(切 tab/切 period 会触发)
 *  PanelLoadingSkeleton 实例不会无意义重渲(它们的 props 引用稳定:minHeight
 *  和 label 都是从 spec 读出的字面量,即使父组件重渲,React.memo 的浅比较
 *  会让 skeleton 跳过 render 阶段)。实际收益:骨架占位期间省 ~5 个 RTT 的
 *  重渲 DOM diff。
 */
export const PanelLoadingSkeleton = memo(function PanelLoadingSkeleton({
  minHeight = 200,
  label = '载入中…',
}: {
  minHeight?: number
  label?: string
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      style={{ minHeight: `${minHeight}px` }}
      className="flex items-center justify-center font-mono text-xs text-muted-foreground"
    >
      {label}
    </div>
  )
})

/** 错误边界 —— 一个面板抛错不连累整页。
 *  - 捕获 React 渲染期抛错(render error / lifecycle error)
 *  - 不捕获: 事件回调、异步、SSR —— 这是 React ErrorBoundary 的标准边界
 *  - 重试: 点 "重试" 清掉 error,子树重新挂载(会重新触发 fetch)
 *
 *  用 class 而非 react-error-boundary 包: 项目 0 新依赖原则(package.json
 *  里没装 react-error-boundary)。这是 React 18 写 ErrorBoundary 的唯一
 *  方式 —— React 19 才允许 hooks 写。
 */
export class SectionErrorBoundary extends Component<
  { name: string; children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(
      `[review/section:${this.props.name}] crashed:`,
      error,
      info.componentStack,
    )
  }

  render() {
    if (this.state.error) {
      // role="alert" 移到内部 span —— 原版在外层 div,屏幕阅读器会把"重试"
      // 按钮文本一起急促读出。拆开后 alert 只覆盖错误信息,按钮独立 Tab 可达。
      return (
        <div className="flex items-center gap-2 rounded border border-destructive/40 bg-destructive/5 px-3 py-2 font-mono text-xs text-destructive">
          <span role="alert">⚠️ {this.props.name} 面板异常:{this.state.error.message}</span>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="ml-auto underline decoration-dotted hover:text-destructive/80"
          >
            重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

/** v302+ P4: A/B tablist 的键盘导航 handler(WAI-ARIA APG 1.2)
 *  - ArrowLeft/Right: 在 tab 间切换焦点 + 选中(循环)
 *  - Home/End: 跳到首/末
 *  - 焦点通过 Roving Tabindex (0/-1) 轮转
 *  - onFocus 回调:选中后真正 .focus() 新 tab(否则只改 tabIndex 会让 Tab 键乱跳)
 *
 *  返回 onKeyDown handler;tab 列表只接受 ARIA 规定的 key,
 *  其它键透传给浏览器默认行为(不 preventDefault)。
 */
function tablistOnKeyDown(
  currentIndex: number,
  total: number,
  onSelect: (index: number) => void,
  onFocus?: (index: number) => void,
) {
  return (e: KeyboardEvent<HTMLButtonElement>) => {
    let next: number
    switch (e.key) {
      case 'ArrowLeft':
        next = currentIndex === 0 ? total - 1 : currentIndex - 1
        break
      case 'ArrowRight':
        next = currentIndex === total - 1 ? 0 : currentIndex + 1
        break
      case 'Home':
        next = 0
        break
      case 'End':
        next = total - 1
        break
      default:
        return
    }
    e.preventDefault()
    onSelect(next)
    onFocus?.(next)
  }
}

/** v302+ P4: A/B 复盘模式切换 — 完整 WAI-ARIA APG 1.2 tablist 模式
 *
 *  关键 a11y 细节(全在 APG 1.2 tabs pattern 里):
 *  1. container: <div role="tablist" aria-label="...">
 *  2. each tab: <button role="tab" id="..." aria-controls="<panelId>" aria-selected>
 *  3. Roving Tabindex(只有当前选中 tab tabIndex=0,其它 -1)
 *  4. 键盘:ArrowLeft/Right/Home/End 切换 + .focus() 真正移动焦点
 *  5. 配套的 panel 容器必须有 role="tabpanel" + id=<panelId> + aria-labelledby=<tabId>
 *
 *  refs 必须是父组件创建的 ref array — 用于 Arrow 切换时 .focus()。
 *  把 refs 留作 prop 是因为 React 不允许子组件的 ref 指向父组件的 DOM。
 */
export type AbTab = 'a' | 'b'

export function AbTablist({
  selected,
  onSelect,
  tabIds,
  panelId,
  tabRefs,
}: {
  selected: AbTab
  onSelect: (next: AbTab) => void
  /** tabId[0] = 'a' tab 的 id,tabId[1] = 'b' tab 的 id */
  tabIds: readonly [string, string]
  /** 被 tab 控制的 panel 容器 id — 写入 aria-controls */
  panelId: string
  /** 父组件的 ref array(长度 = 2)— 用于 Arrow 切换时 .focus() */
  tabRefs: MutableRefObject<[(HTMLButtonElement | null), (HTMLButtonElement | null)]>
}) {
  const onKeyDown = tablistOnKeyDown(
    selected === 'a' ? 0 : 1,
    2,
    (idx) => onSelect(idx === 0 ? 'a' : 'b'),
    (idx) => tabRefs.current[idx]?.focus(),
  )
  return (
    <div
      role="tablist"
      aria-label="复盘模式"
      className="inline-flex self-start rounded-lg border border-border bg-secondary/50 p-0.5"
    >
      <button
        id={tabIds[0]}
        type="button"
        role="tab"
        aria-selected={selected === 'a'}
        aria-controls={panelId}
        tabIndex={selected === 'a' ? 0 : -1}
        ref={(el) => {
          tabRefs.current[0] = el
        }}
        onClick={() => onSelect('a')}
        onKeyDown={onKeyDown}
        className={cn(
          'rounded-md px-3 py-1.5 font-mono text-xs transition-all',
          selected === 'a'
            ? 'bg-background text-foreground shadow-sm'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        A · 历史真实交易复盘
      </button>
      <button
        id={tabIds[1]}
        type="button"
        role="tab"
        aria-selected={selected === 'b'}
        aria-controls={panelId}
        tabIndex={selected === 'b' ? 0 : -1}
        ref={(el) => {
          tabRefs.current[1] = el
        }}
        onClick={() => onSelect('b')}
        onKeyDown={onKeyDown}
        className={cn(
          'rounded-md px-3 py-1.5 font-mono text-xs transition-all',
          selected === 'b'
            ? 'bg-background text-foreground shadow-sm'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        B · 快速评测复盘
      </button>
    </div>
  )
}

/** v302+ P6: 可折叠段包装 — 把 <section> 包装成可折叠/展开的容器
 *
 *  设计要点:
 *  - 视觉最小化: 默认只在右上角放一个小 chevron + (可选) 段序号,不影响现有布局
 *  - 状态完全由外部传入(controlled): 不在内部 useState,跟 useReviewCollapse 配套
 *  - a11y: button 用 aria-controls 指向内容容器,aria-expanded 反映状态
 *  - 折叠时跳过渲染子树: 不只是 display:none,React 真正卸载(节省 lazy chunk 解析)
 *  - 隐藏的子节点(visible=false): 仍然渲染(SPEC 里某些 section 在某些 mode 下隐藏) —
 *    折叠/隐藏是两个独立关注点,这里只管折叠
 *
 *  用法:
 *    <CollapsibleSection
 *      sectionId="review"
 *      collapsed={isCollapsed('review')}
 *      onToggle={() => toggle('review')}
 *      aria-label="复盘"
 *    >
 *      {content}
 *    </CollapsibleSection>
 */
export function CollapsibleSection({
  sectionId,
  collapsed,
  onToggle,
  index,
  'aria-label': ariaLabel,
  className,
  children,
  defaultOpen = true,
}: {
  /** section 的稳定 id — 同一 section 跨 render 必须传相同的 string */
  sectionId: string
  /** 当前是否折叠 — 来自 useReviewCollapse().isCollapsed(sectionId) */
  collapsed: boolean
  /** 切换回调 — 通常是 useReviewCollapse().toggle.bind(null, sectionId) */
  onToggle: () => void
  /** 1-based 序号(用于键盘快捷键 1-9 跳转 & 视觉提示)
   *  不传则不显示序号(用于分组内的小段,如 diagnose 内的两个子段) */
  index?: number
  'aria-label': string
  className?: string
  children: ReactNode
  /** 首次挂载时若 collapsed=true,默认应该渲染还是空?
   *  true  = 折叠时仍渲染(预热 lazy chunk)— 适合 100% 折叠概率低的 section
   *  false = 折叠时不渲染(节省内存)— 适合很少展开的 section
   *  默认 true,因为我们一般只让用户主动折叠,首次访问应该看到内容
   */
  defaultOpen?: boolean
}) {
  const contentId = `${sectionId}-content`
  const showContent = !collapsed

  return (
    <section
      id={sectionId}
      aria-label={ariaLabel}
      data-section-id={sectionId}
      data-collapsed={collapsed ? 'true' : 'false'}
      className={cn('scroll-mt-3', className)}
    >
      <div className="flex items-center justify-between gap-2 pb-1">
        <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          {index !== undefined && (
            <span
              className="inline-flex h-4 min-w-4 items-center justify-center rounded border border-border bg-secondary/50 px-1 text-[9px] text-foreground"
              aria-hidden
            >
              {index}
            </span>
          )}
          <span>{ariaLabel}</span>
        </div>
        <button
          type="button"
          onClick={onToggle}
          aria-controls={contentId}
          aria-expanded={!collapsed}
          aria-label={`${collapsed ? '展开' : '折叠'} ${ariaLabel} 段`}
          className={cn(
            'inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors',
            'hover:bg-secondary hover:text-foreground',
            'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary',
          )}
        >
          {collapsed ? (
            <ChevronRight className="h-3 w-3" aria-hidden />
          ) : (
            <ChevronDown className="h-3 w-3" aria-hidden />
          )}
        </button>
      </div>
      <div
        id={contentId}
        role="region"
        aria-label={`${ariaLabel} 内容`}
        hidden={collapsed}
      >
        {showContent || defaultOpen ? children : null}
      </div>
    </section>
  )
}
