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
import { MissedSignalsDimensionSummary } from '@/components/v237/MissedSignalsDimensionSummary'
import { EventMarketAttribution } from '@/components/v291/event-market-attribution'
import { MissedSignalsAttentionCandidates } from '@/components/v317/missed-signals-attention-candidates'
import { BackendCapabilityPanel } from '@/components/quant/backend-capability-panel'

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
          A 买卖信号 / B 信息方向 / C 技术错误，每类再细分小项（A1/A2/A3 · B1/B2/B3 · C1/C2/C3）
        </p>
      </div>

      {/* v361 合并：原来这里是两块 —— 上面 ByDimBanner（漏检条数 n=36）、
          下面 DimensionSummary（反思样本 n=362），两个口径并排放在同一页，
          读者会以为数字对不上。现在只留 DimensionSummary，它一张卡里同时给出
          「漏检 X · 反思 Y · 命中 Z%」，并在表头合计漏检条数。 */}
      <MissedSignalsDimensionSummary refreshMs={PAGE_REFRESH_MS} />

      {/* v317: 注意力先行候选 —— 事前视角的漏检（注意力已起、价格未动、系统未出手）。
          与下方 MissedSignalsPanel 的事后 missed_signal 互补，不重复、不改写。 */}
      <MissedSignalsAttentionCandidates
        refreshMs={PAGE_REFRESH_MS}
        externalRefreshToken={pageTick}
      />

      {/* 漏检明细 + 三维度分类
          v310 P0 fix: 传 hideDimensionSummary —— 面板内部本来就会再渲染一份
          MissedSignalsDimensionSummary, 而本页 L56 已经单独渲染过一次,
          不关掉就会**重复渲染两遍**「三维度」区块(页面顶部可见两行
          "加载三维度数据 (trade_reflections_v2)…")。首页 LiveDesk 早就传了这个 prop。 */}
      <MissedSignalsPanel refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} hideDimensionSummary />

      {/* 下方: 事件-行情归因 (与漏检互补) */}
      <EventMarketAttribution refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} />

      {/* v2026.11.02 后端能力归位: 信号权重。
          /api/signal-weights/stats 后端一直在算(signal_weight_history 43107 行 +
          signalweightcron 每 6 分钟跑), 但界面上完全没有入口 —— 而「这条信号为什么被
          压权」正是漏检归因的核心。默认折叠, 展开才取数(该端点在写锁竞争下首调
          实测 39s, 随首屏加载会拖死整页)。 */}
      <BackendCapabilityPanel domain="signal-weights" />
    </div>
  )
}
