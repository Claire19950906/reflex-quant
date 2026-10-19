'use client'

/**
 * _section-progress.tsx — 段位进度指示器(v302+ P6)
 *
 *  设计:
 *  - 顶部 sticky,小尺寸(40px 高),不抢主视线
 *  - 实时显示 "已展开 N / 总 M 段" + 当前位置
 *  - 位置用 IntersectionObserver 检测 — 哪个 section 在视口顶部最多,标为"当前"
 *  - 不抢 a11y 焦点;屏幕阅读器会用 aria-live="polite" 播报 "当前段: 复盘"
 *  - 与 CollapsibleSection 的 data-section-id 属性挂钩 — 不需要 prop 注入
 *
 *  为什么不用 scroll listener: IntersectionObserver 是浏览器原生 API,不会因为
 *  长内容滚动触发 60Hz 回调;浏览器在视口交叉时才通知,CPU 占用为 0。
 */

import { useEffect, useState } from 'react'
import { cn } from '@/lib/utils'

type SectionInfo = { id: string; label: string }

export function SectionProgress({
  total,
  expanded,
  sections,
  className,
}: {
  /** 全部 section 数(从 SECTION_MATRIX 派生) */
  total: number
  /** 已展开数 = total - collapsed.size */
  expanded: number
  /** 全部 section 的 (id, label) 列表,用于"当前位置"判定 */
  sections: readonly SectionInfo[]
  className?: string
}) {
  const [activeId, setActiveId] = useState<string | null>(sections[0]?.id ?? null)

  useEffect(() => {
    if (sections.length === 0) return
    if (typeof window === 'undefined' || typeof IntersectionObserver === 'undefined') return
    const elements = sections
      .map((s) => document.getElementById(s.id))
      .filter((el): el is HTMLElement => el != null)

    if (elements.length === 0) return

    // 用 rootMargin 把"激活线"放在视口顶部 20% 位置
    // 哪个 section 的中心最近激活线 → 视为当前
    const observer = new IntersectionObserver(
      (entries) => {
        // 收集所有相交的 section,选最靠上的
        const intersecting = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (intersecting.length > 0) {
          setActiveId(intersecting[0].target.id)
        }
      },
      {
        rootMargin: '-10% 0px -70% 0px',
        threshold: 0,
      },
    )

    for (const el of elements) observer.observe(el)
    return () => observer.disconnect()
  }, [sections])

  const activeIndex = sections.findIndex((s) => s.id === activeId)
  const activeSection = activeIndex >= 0 ? sections[activeIndex] : null

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic
      className={cn(
        'sticky top-0 z-20 -mx-3 flex items-center justify-between gap-2 border-b border-border/60 bg-background/80 px-3 py-1.5 font-mono text-[10px] backdrop-blur md:-mx-4 md:px-4',
        className,
      )}
      data-testid="section-progress"
    >
      <div className="flex items-center gap-3">
        <span className="text-muted-foreground">段位进度</span>
        <span className="rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-primary">
          {expanded} / {total} 展开
        </span>
        {activeSection && (
          <span className="text-muted-foreground">
            位置: <span className="text-foreground">{activeSection.label}</span>
            {activeIndex >= 0 && ` (${activeIndex + 1}/${sections.length})`}
          </span>
        )}
      </div>
      <div className="hidden h-1 w-32 overflow-hidden rounded-full bg-secondary md:block">
        <div
          className="h-full bg-primary transition-all"
          style={{ width: total > 0 ? `${(expanded / total) * 100}%` : '0%' }}
          aria-hidden
        />
      </div>
    </div>
  )
}
