'use client'

/**
 * app/attribution/page.tsx — 归因面板入口
 *
 * v170+ 改造:
 *   - 改用 useNewsImpact hook(取代手写 useEffect+fetchInternalNews+adapter)
 *   - VIX 默认 20(典型低波)— TODO: 接 cross-asset-anomaly-panel 的实时 VIX
 *
 * v2026.10.15 改造:
 *   - 增加 pageRefreshTick 共享 60s tick, 通过 externalRefreshToken 推给 4 个 panel,
 *     避免每个 panel 各自 30/60/120s setInterval 错峰打后端, 让集中 60s 时点拉取.
 *   - panel 内部 setInterval 保留(双保险), 但额外响应 page 推过来的 token.
 *
 * 为什么这样写:
 *   - useNewsImpact 统一处理 loading / error / abort / 轮询 — page 只需组合
 *   - NewsImpactList 是纯展示组件,数据单向流入
 *   - 默认 VIX=20 让组件能立即渲染(regime=1.0 中性),运营人员能先看到版式
 *   - 失败态(网络错/空数据)统一走 NewsImpactList 内部 EmptyState,不污染外层
 */

import { useEffect, useState } from 'react'
import { MoversPanel } from '@/components/quant/movers-panel'
import { NewsImpactList } from '@/components/news/news-impact-list'
import { EntityImpactPanel } from '@/components/news/entity-impact-panel'
import { MissedSignalsPanel } from '@/components/v237/MissedSignalsPanel'
import { EventMarketAttribution } from '@/components/v291/event-market-attribution'
import { useNewsImpact } from '@/lib/hooks/use-news-impact'
import type { MarketState } from '@/components/news/news-impact-weight'

/** 默认市场状态(regime=1.0 中性,等接入实时 VIX 后替换) */
const DEFAULT_MARKET_STATE: MarketState = { vix: 20 }

/** page 层统一轮询周期(ms): 4 个 panel 全部对齐到同一时点拉取 */
const PAGE_REFRESH_MS = 60_000

export default function AttributionPage() {
  // 拉取 + 适配(60s 自动轮询,卸载时 abort)
  const { items, loading, error, lastUpdated, refresh } = useNewsImpact({
    refreshMs: PAGE_REFRESH_MS,
  })

  // v2026.10.15: 共享 60s tick, 通过 externalRefreshToken 推给 4 个 panel
  // 0 表示初始态(各 panel 走各自的 fetchOnMount), > 0 表示"被 page 触发了一次"
  const [pageTick, setPageTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => {
      setPageTick((t) => t + 1)
    }, PAGE_REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  // 市场状态(目前硬编码,将来接 cross-asset-anomaly-panel)
  const marketState: MarketState = DEFAULT_MARKET_STATE

  return (
    <div className="flex flex-col gap-3 p-3">
      {/* v302+ P0 优化: 错误 + 时间戳移到顶部 — 之前夹在 NewsImpactList 和 v237 之间,
          用户得滚到中间才看到 API 失败, 且 retry 按钮只重拉 /events,
          跟下方 v237/v291/Movers 各自 fetch 独立, 语义上"全局顶部"更合理 */}
      {error && (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 rounded border border-short/40 bg-short/10 px-3 py-2 font-mono text-[11px] text-short"
        >
          <span>/events error: {error} (下方 3 个 panel 的数据来自各自端点, 仍可能正常)</span>
          <button
            type="button"
            onClick={refresh}
            className="rounded border border-short/40 bg-card px-2 py-0.5 text-[10px] hover:bg-short/20"
          >
            Retry
          </button>
        </div>
      )}
      {lastUpdated !== null && !error && (
        <div className="px-1 text-right font-mono text-[9px] text-muted-foreground/60">
          Updated {new Date(lastUpdated).toISOString().slice(11, 19)} UTC · auto-refresh {Math.round(PAGE_REFRESH_MS/1000)}s (统一)
        </div>
      )}
      <EntityImpactPanel
        items={items}
        marketState={marketState}
        loading={loading}
        topN={5}
        title="Top Entity Impact (Attribution)"
      />
      <NewsImpactList
        items={items}
        marketState={marketState}
        loading={loading}
        title="News Impact Weights (Attribution)"
      />
      {/* v237: 漏检反思区 — 在 NewsImpact 之下, Movers 之上.
         v302+ P0: id="missed" 供 deep-link /attribution#missed (LiveDesk 顶部 strip 跳转) */}
      <MissedSignalsPanel id="missed" refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} />
      {/* v291: 事件-行情归因 — 在漏检反思之下, Movers 之上 (algo-first) */}
      <EventMarketAttribution refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} />
      <MoversPanel externalRefreshToken={pageTick} />
    </div>
  )
}