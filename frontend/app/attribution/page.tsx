'use client'

/**
 * app/attribution/page.tsx — 归因面板入口
 *
 * v170+ 改造:
 *   - 改用 useNewsImpact hook(取代手写 useEffect+fetchInternalNews+adapter)
 *   - VIX 默认 20(中性, regime=1.0)。待办: 后端宏观端点落地实时 VIX 后接入；
 *     实测 /api/macro/context 与 /api/macro/snapshot 当前均未返回可用 VIX，
 *     因此前端先保留兜底 20，取不到真值时不强行传空/NaN。
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

import { useEffect, useState, type ReactNode } from 'react'
import { MoversPanel } from '@/components/quant/movers-panel'
import { InputLayerPanel } from '@/components/quant/input-layer-panel'
import { CrossMarketStrip } from '@/components/quant/cross-market-strip'
import { CapabilityPanel } from '@/components/quant/capability-panel'
import { NewsImpactList } from '@/components/news/news-impact-list'
import { EntityImpactPanel } from '@/components/news/entity-impact-panel'
import { EventScatterPanel } from '@/components/attribution/event-scatter-panel'
import { EventMarketAttribution } from '@/components/v291/event-market-attribution'
import { PendingAttributionPanel } from '@/components/v291/pending-attribution-panel'
import { MarketRegimePanel } from '@/components/v291/market-regime-panel'
import { OrderbookDepthPanel } from '@/components/v291/orderbook-depth-panel'
import { FundingRatePanel } from '@/components/v291/funding-rate-panel'
import AbnormalAttributionPanel from '@/components/quant/abnormal-attribution-panel'
import { AttributionQualityPanel } from '@/components/gub/attribution-quality-panel'
import { useNewsImpact } from '@/lib/hooks/use-news-impact'
import type { MarketState } from '@/components/news/news-impact-weight'

/** 默认市场状态(regime=1.0 中性)。后端 VIX 落地后改为实时拉取；当前保留兜底。 */
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

  // 市场状态: 兜底中性 VIX=20; 待后端宏观端点落地实时 VIX 后在此拉取覆盖
  const marketState: MarketState = DEFAULT_MARKET_STATE

  // v337-UI: 空态必须给出"为什么空"，否则用户无法区分「系统坏了」和「今天确实没有方向」。
  //   实测: /api/events/today 今天返回 100 条事件，但全部 action=HOLD、score=0
  //   （AI 通道停摆），旧页面只写「暂无数据」，看起来像故障。
  const holdCount = items.filter((it) => {
    const a = (it as { action?: unknown }).action
    return typeof a === 'string' && a.toUpperCase() === 'HOLD'
  }).length
  const emptyReason: ReactNode = items.length === 0 ? (
    <>
      <p>· 后端该时段返回 <span className="text-foreground">0 条事件</span>：事件没有进入决策链。</p>
      <p>· 最常见原因：AI 通道停摆 —— 新闻照收，但走不到打分/决策，于是归因没有输入。</p>
      <p>· 下一步：去 <a className="underline hover:text-foreground" href="/sources">信息源</a> 看通道状态，或切到
        <a className="underline hover:text-foreground" href="/missed-signals"> 漏检信号 </a>看今天漏了什么。</p>
    </>
  ) : (
    <>
      <p>
        · 窗口内 <span className="text-foreground">{items.length}</span> 条事件里
        <span className="text-foreground"> {holdCount} </span>条是 HOLD（权重 0）——
        按影响力排序自然为空，<span className="text-foreground">不是没有新闻</span>。
      </p>
      <p>· HOLD 的含义：模型认为这条消息没有明确方向，或干脆没跑出结论（通道异常时全是 HOLD）。</p>
      <p>· 下一步：等通道恢复后这里会自动有内容；现在能看的是下方「逐笔归因」和「被否归因」。</p>
    </>
  )

  const highImpact = items.filter((it) => (it.weight ?? 0) >= 0.5).length
  // 全部 HOLD + 有事件 = 决策通道没产出（不是"正常"）
  const stalled = !loading && !error && items.length > 0 && highImpact === 0 && holdCount === items.length

  return (
    <div className="flex flex-col gap-3 p-3">
      <header className="mx-auto w-full max-w-[1600px] border-b border-border pb-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[11px] tracking-[0.18em] text-muted-foreground/60">
              TRIDENT / 03
            </p>
            <h1 className="mt-1 text-xl font-semibold tracking-tight text-foreground">行情归因</h1>
            <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted-foreground">
              哪条消息、何时生效、影响多久。事件落在 K 线上，点开看跨标的反应与证据。
            </p>
          </div>
          <div className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
            <span>
              {lastUpdated
                ? `更新 ${new Date(lastUpdated).toLocaleTimeString('zh-CN', { hour12: false })}`
                : loading
                  ? '加载中'
                  : '暂无更新'}
            </span>
            <button
              type="button"
              onClick={refresh}
              className="rounded border border-border bg-card px-2 py-1 text-foreground transition-colors hover:border-primary/50 hover:text-primary"
            >
              刷新
            </button>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div className="rounded border border-border bg-card px-3 py-2">
            <span className="block font-mono text-[11px] tracking-wider text-muted-foreground">窗口内事件</span>
            <b className="mt-0.5 block font-mono text-lg font-medium text-foreground">{items.length}</b>
          </div>
          <div className="rounded border border-border bg-card px-3 py-2">
            <span className="block font-mono text-[11px] tracking-wider text-muted-foreground">高影响</span>
            <b className="mt-0.5 block font-mono text-lg font-medium text-primary">{highImpact}</b>
          </div>
          <div className="rounded border border-border bg-card px-3 py-2">
            <span className="block font-mono text-[11px] tracking-wider text-muted-foreground">HOLD</span>
            <b className="mt-0.5 block font-mono text-lg font-medium text-muted-foreground">{holdCount}</b>
          </div>
          <div className="rounded border border-border bg-card px-3 py-2">
            <span className="block font-mono text-[11px] tracking-wider text-muted-foreground">数据状态</span>
            {/* v499: 100 条事件全 HOLD 时不能报"正常" —— 数据到了、决策通道没产出，
                用户会以为归因坏了。这里按"通道停摆"单独标黄。 */}
            <b
              className={`mt-0.5 block font-mono text-lg font-medium ${
                error ? 'text-short' : stalled ? 'text-amber-500' : 'text-long'
              }`}
              title={stalled ? '事件有数据，但 AI 决策通道没有产出方向（全部 HOLD）' : undefined}
            >
              {error ? '异常' : loading ? '加载中' : stalled ? '通道停摆' : '正常'}
            </b>
          </div>
        </div>
      </header>

      {/* v302+ P0 优化: 错误 + 时间戳移到顶部 — 之前夹在 NewsImpactList 和 v237 之间,
          用户得滚到中间才看到 API 失败, 且 retry 按钮只重拉 /events,
          跟下方 v237/v291/Movers 各自 fetch 独立, 语义上"全局顶部"更合理 */}
      {error && (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 rounded border border-short/40 bg-short/10 px-3 py-2 font-mono text-[11px] text-short"
        >
          {/* v345.9: 原来直接把 `/events error: {原始报错}` 打在页面上 —— 里面可能是
              http://127.0.0.1:8100/api/... 内部地址或 `500`，属于开发信息泄漏。
              现在只说「哪一块拉不到 + 下面还能看什么」。 */}
          <span>事件列表暂时拉不到，下方各组数据各自独立，仍可查看</span>
          <button
            type="button"
            onClick={refresh}
            className="rounded border border-short/40 bg-card px-2 py-0.5 text-[11px] hover:bg-short/20"
          >
            重试
          </button>
        </div>
      )}
      {lastUpdated !== null && !error && (
        <div className="px-1 text-right font-mono text-[11px] text-muted-foreground/60">
          最后更新 {new Date(lastUpdated).toLocaleTimeString('zh-CN', { hour12: false })}
        </div>
      )}
      {/* ─── ① 行情异动（页面入口：先看到"哪里在动"）─── */}
      <MoversPanel externalRefreshToken={pageTick} />

      {/* ─── ①.5 事件效力散点（v344）：哪类事件真的推动了价格 ─── */}
      <EventScatterPanel />

      {/* ─── ② 事件归因（本页核心：什么事件影响了什么资产、往哪个方向）───
          v339 P1: 原来是 12 个面板平铺，首屏全是卡片、没有层次。
          这里按「看归因的三个问题」重新分组，核心组默认展开。 */}
      <details open className="rounded-lg border border-border bg-card/40 px-3 py-2">
        <summary className="cursor-pointer select-none text-xs font-semibold text-foreground">
          事件归因 · 谁影响了谁（实体影响 / 新闻权重 / 事件-行情配对）
        </summary>
        <div className="mt-3 flex flex-col gap-3">
          <EntityImpactPanel
            items={items}
            marketState={marketState}
            loading={loading}
            topN={5}
            title="实体影响 Top5"
            emptyText="没有可聚合的实体影响"
            emptyReason={emptyReason}
          />
          <NewsImpactList
            items={items}
            marketState={marketState}
            loading={loading}
            title="新闻影响权重（按影响力排序）"
            emptyText="没有可排序的新闻权重"
            emptyReason={emptyReason}
          />
          <EventMarketAttribution refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} />
        </div>
      </details>

      {/* ─── ③ 漏检信号（v345.9：这里原来又完整渲染了一遍 MissedSignalsPanel，
              和 /missed-signals 专页是同一份数据同一个组件 —— 同一屏看两遍。
              现在只留一张入口卡，明细去专页看。id="missed" 保留，旧的 #missed 深链不失效。─── */}
      <a
        id="missed"
        href="/missed-signals"
        className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-card/40 px-3 py-2 text-xs hover:border-primary/50"
      >
        <span className="font-semibold text-foreground">漏检信号</span>
        <span className="text-muted-foreground">
          被规则筛掉的信号逐条明细在专页（含三档错误分类与逐条原因），这里不再重复展开
        </span>
        <span className="ml-auto font-mono text-[11px] text-primary">去专页 →</span>
      </a>

      {/* ─── ④ 复核与质量（默认折叠：这些是"数据可信吗"，不是"发生了什么"）─── */}
      <details className="rounded-lg border border-border bg-card/40 px-3 py-2">
        <summary className="cursor-pointer select-none text-xs font-medium text-foreground/80">
          复核与质量（待复核异动 / AI 归因明细 / 归因质量体检）
        </summary>
        <div className="mt-3 flex flex-col gap-3">
          {/* v334 (需求 #6/#7): 大异动（|涨跌|≥5%）没进主表时以前在页面上是"消失"的，
              用户只看到覆盖率不高，看不到"候选新闻已找到、卡在复核闸门"。 */}
          <PendingAttributionPanel refreshMs={PAGE_REFRESH_MS} externalRefreshToken={pageTick} />
          {/* AI 异动归因明细：AI worker 的结论 + 驱动 + 可执行结论 + 量能/形态上下文。 */}
          <AbnormalAttributionPanel refreshToken={pageTick} limit={20} />
          {/* v307 GUB: 归因质量体检 —— 真因果/推导/事后拟合的真实比例，
              v338 规则自检落地后真因果占比已从账面 100% 修正为 59.4%。 */}
          <AttributionQualityPanel />
        </div>
      </details>

      {/* ─── ⑤ 市场环境（默认折叠：判断"现在是什么市场情况、原因是什么"）─── */}
      <details className="rounded-lg border border-border bg-card/50 px-3 py-2">
        <summary className="cursor-pointer select-none text-xs font-medium text-foreground/80">
          市场环境（市场状态 / 挂单深度 / 资金费率 — 判断资金进出与杠杆水平）
        </summary>
        <div className="mt-3 flex flex-col gap-3">
          <MarketRegimePanel />
          {/* v478 · 跨市场读数：美股期货 / 美债 5Y10Y / 曲线价差 */}
          <CrossMarketStrip />
          <OrderbookDepthPanel />
          <FundingRatePanel />
        </div>
      </details>

      {/* v476 · 输入层体检：v459–v475 接进决策链的美债/估值/TIC/链上/分歧度，
          以前页面上只有结论。这里给原始读数与可信度，过期数据直接标「旧」。 */}
      <InputLayerPanel />

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="归因与研究能力"
        hint="影响回填 / 盈亏归因分解 / 跨资产传导链 / 异动快照回填"
        featureIds={['F01','F12','F13','F37']}
      />
      </section>
    </div>
  )
}