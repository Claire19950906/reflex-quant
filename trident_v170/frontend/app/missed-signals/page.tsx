'use client'

/**
 * app/missed-signals/page.tsx — 漏检信号专页 (v291+ 新增)
 *
 * 设计:
 *  - 聚焦三维度漏检: A 买卖信号 / B 信息方向 / C 技术错误
 *  - v291+ subcategory 细分: A1/A2/A3 / B1/B2/B3 / C1/C2/C3
 *  - 60s 自动轮询,与 /attribution 共享同一时点
 *  - 上方: MissedSignalsByDimBanner (顶部数字卡片)
 *  - 中间: MissedSignalsDimensionSummary (按子类的统计)
 *  - 下方: MissedSignalsPanel 完整明细
 *
 * 为什么独立成页:
 *  - /attribution#missed 是嵌入段, 滚动到才看到, 不够聚焦
 *  - 漏检 = 反思系统核心, 应该有独立入口 + 单独的"复盘时段"过滤器
 *  - 侧栏导航可直达 (复用 layout 的 nav)
 */

import { useEffect, useState } from 'react'
import { MissedSignalsPanel } from '@/components/v237/MissedSignalsPanel'
import { MissedSignalsByDimBanner } from '@/components/v237/MissedSignalsByDimBanner'
import { MissedSignalsDimensionSummary } from '@/components/v237/MissedSignalsDimensionSummary'
import { EventMarketAttribution } from '@/components/v291/event-market-attribution'

/** page 层统一轮询周期(ms): 与 /attribution 对齐 */
const PAGE_REFRESH_MS = 60_000

export default function MissedSignalsPage() {
  // v2026.10.15: 共享 60s tick
  const [pageTick, setPageTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => {
      setPageTick((t) => t + 1)
    }, PAGE_REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="flex flex-col gap-3 p-3">
      {/* 页面头: 标题 + 副标题 + 数字 banner */}
      <div className="flex flex-col gap-1 border-b border-border pb-2">
        <h1 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
          <span aria-hidden>🪞</span>
          漏检信号 · 三维度细分
        </h1>
        <p className="font-mono text-[11px] text-muted-foreground">
          v291+ 反思系统聚焦页 · A 买卖信号 / B 信息方向 / C 技术错误 · 每维度细分子类 (A1/A2/A3 · B1/B2/B3 · C1/C2/C3)
        </p>
      </div>

      {/* 顶部 banner: 三维度漏检总数 */}
      <MissedSignalsByDimBanner refreshMs={PAGE_REFRESH_MS} />

      {/* 中部: 子类统计 */}
      <MissedSignalsDimensionSummary refreshMs={PAGE_REFRESH_MS} />

      {/* 漏检明细 + 三维度分类 */}
      <MissedSignalsPanel refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} />

      {/* 下方: 事件-行情归因 (与漏检互补) */}
      <EventMarketAttribution refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} />
    </div>
  )
}
