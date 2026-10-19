'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import type { PriceMap } from '@/hooks/use-binance-prices'
import { useBinancePrices } from '@/hooks/use-binance-prices'
import { cn } from '@/lib/utils'
import { EventBus } from '@/components/quant/event-bus'
import { AgentChat } from '@/components/quant/agent-chat'
import { LiveKlineChart } from '@/components/quant/live-kline-chart'
import { SignalLedger } from '@/components/quant/signal-ledger'
import { StatCards } from '@/components/quant/stat-cards'
import { MarketTabs } from '@/components/quant/market-tabs'
import { PriceTicker } from '@/components/quant/price-ticker'
import { ReviewOverview } from '@/components/replay/review-overview'
import { CalendarHintBanner } from '@/components/quant/calendar-hint-banner'
import { TridentTime } from '@/components/quant/trident-time'
import { AbnormalFeedbackBar } from '@/components/quant/abnormal-feedback-bar'
import {
  PeriodSelector,
  resolvePeriod,
  type PeriodKey,
  type PeriodRange,
} from '@/lib/data-channel-standards'
import { MissedSignalsDimensionSummary } from '@/components/v237/MissedSignalsDimensionSummary'
import { MissedSignalsByDimBanner } from '@/components/v237/MissedSignalsByDimBanner'
import { MissedSignalsPanel } from '@/components/v237/MissedSignalsPanel'
import Link from 'next/link'

export function LiveDesk() {
  const [events, setEvents] = useState<ApiEvent[]>([])
  const [activeMarket, setActiveMarket] = useState<MarketCategory>('ALL')
  const [binanceConnected, setBinanceConnected] = useState(false)
  // v282+: HONESTY — change24h nullable,前端按 '—' 显示而非假 0%
  const [xauPrice, setXauPrice] = useState<{ price: number; change24h: number | null } | null>(null)
  const [wtiPrice, setWtiPrice] = useState<{ price: number; change24h: number | null } | null>(null)
  const { prices, pricesByPair: binancePairs, connected: marketConnected } = useBinancePrices()

  // v180 P0+：全局时段选择器(从 ReviewOverview 提到顶层)
  // 所有顶部卡片(7 张)+ StatCards(3 张)+ EventBus 都按此窗口过滤
  const [periodKey, setPeriodKey] = useState<PeriodKey>('today')
  const [customDateRange, setCustomDateRange] = useState<{ fromDate: string; toDate: string }>(
    () => {
      const today = new Date()
      const sevenAgo = new Date(today.getTime() - 7 * 24 * 3600 * 1000)
      const fmt = (d: Date) => {
        const y = d.getFullYear()
        const m = String(d.getMonth() + 1).padStart(2, '0')
        const day = String(d.getDate()).padStart(2, '0')
        return `${y}-${m}-${day}`
      }
      return { fromDate: fmt(sevenAgo), toDate: fmt(today) }
    },
  )
  const period: PeriodRange = resolvePeriod(periodKey, customDateRange)

  // v180 P0+：「今日信号」卡片点击 → 滚动到 EventBus（id anchor）并短暂闪烁高亮。
  // 同时把 activeMarket 切到 ALL（让 EventBus 不被市场过滤掉今日所有信号）。
  // v188 P0+：sticky toggle —— 点一次开启，再点一次关闭；不自动消失。
  const eventBusAnchorRef = useRef<HTMLDivElement | null>(null)
  const [eventBusFlash, setEventBusFlash] = useState(false)
  const [eventBusTodayOnly, setEventBusTodayOnly] = useState(false)
  // v302+ P0+: 实时信号台统一 60s tick — 推给反思三件套 (Banner/Summary/Panel), 避免错峰打后端
  const [reflectTick, setReflectTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setReflectTick((t) => t + 1), 60_000)
    return () => clearInterval(id)
  }, [])
  // v302+ P0+: 默认展开反思 section; 用户可手动折叠
  const [reflectOpen, setReflectOpen] = useState(true)
  const handleSignalsCardClick = useCallback(() => {
    setActiveMarket('ALL')
    // sticky toggle —— 点一次开启，再点一次关闭
    setEventBusTodayOnly((prev) => {
      const next = !prev
      if (next && eventBusAnchorRef.current) {
        const rect = eventBusAnchorRef.current.getBoundingClientRect()
        const targetY = window.scrollY + rect.top - 80
        window.scrollTo({ top: targetY, behavior: 'smooth' })
      }
      // 只在开启时闪烁一下
      if (next) {
        setEventBusFlash(true)
        window.setTimeout(() => setEventBusFlash(false), 1500)
      }
      return next
    })
  }, [])

  const pricesByPair: PriceMap = useMemo(() => {
    const merged = { ...binancePairs }
    if (xauPrice) {
      merged['XAUUSDT'] = xauPrice
      merged['GOLDUSDT'] = xauPrice
    }
    if (wtiPrice) merged['WTIUSDT'] = wtiPrice
    return merged
  }, [binancePairs, xauPrice, wtiPrice])

  const pricesForTicker = useMemo(() => {
    const merged = { ...prices }
    if (xauPrice) merged['XAU'] = xauPrice
    if (wtiPrice) merged['WTI'] = wtiPrice
    return merged
  }, [prices, xauPrice, wtiPrice])

  const handlePriceUpdate = useCallback((asset: string, price: number, change24h?: number | null) => {
    // v282+: HONESTY — 透传 null,不 fallback 成 0 让 UI 显示假 0%
    const change = change24h ?? null
    if (asset === 'XAU') setXauPrice({ price, change24h: change })
    else if (asset === 'WTI') setWtiPrice({ price, change24h: change })
  }, [])

  useEffect(() => {
    setBinanceConnected(marketConnected || xauPrice !== null || wtiPrice !== null)
  }, [marketConnected, xauPrice, wtiPrice])

  return (
    // v286 — 加 items-start：EventBus 默认展开全部（不限高），左列自然撑高；
    //        右列保持自己的内容高度，不再被左列撑到千行面板那么高。
    <main className="mx-auto grid w-full min-h-0 max-w-[1600px] grid-cols-1 items-start gap-4 p-4 md:p-6 lg:grid-cols-[minmax(380px,1.35fr)_1fr]">
      {/* v180 P0+：全局时段选择器(从 ReviewOverview 提到顶层)
          影响所有 7 张顶部卡片 + StatCards 3 张今日卡 + EventBus 事件流
          PeriodSelector 组件本身由 /lib/data-channel-standards 提供,4 个固定时段
          + 自定义日期范围,UTC ↔ 北京时区自动换算 */}
      <section className="col-span-1 lg:col-span-2">
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border/60 bg-secondary/30 px-3 py-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            📅 全局时段
          </span>
          <PeriodSelector
            value={periodKey}
            onChange={setPeriodKey}
            customDateRange={customDateRange}
            onCustomChange={setCustomDateRange}
            size="sm"
          />
          <span
            className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[9px] text-primary"
            title="切换后,7 张顶部卡片 + 3 张今日卡片 + EventBus 事件流全部按此窗口过滤"
          >
            影响全部 7+3 张卡片 · 当前:{period.label}
          </span>
        </div>
      </section>

      {/* 反思系统同源战绩（A 真实持仓 / B 快速评测）— 与 /review 页数字完全一致 */}
      <section className="col-span-1 lg:col-span-2">
        <ReviewOverview period={period} />
      </section>

      {/* 重要宏观数据日历提示（与 ai_worker 注入到 prompt 的同一份事件） */}
      <section className="col-span-1 lg:col-span-2">
        <CalendarHintBanner />
      </section>

      {/* v302+ P0+: 实时信号台「错过信号反思」三件套 — Banner(数字) + Summary(三维度细分) + Panel(完整明细)
         需求 7: 分析页（实时信号台）需要识别信号 → 进入反思系统，并显示完整错过信号界面
         三维度: A 买卖信号判断 (long) / B 信息分析方向 (short) / C 技术错误 (中性)
         可手动折叠, 默认展开, 60s 自动同步, 跳转到 /missed-signals 专页看全部. */}
      <section className="col-span-1 lg:col-span-2">
        <div className="flex items-center justify-between gap-2 border-b border-border bg-card/60 px-3 py-2">
          <button
            type="button"
            onClick={() => setReflectOpen((v) => !v)}
            aria-expanded={reflectOpen}
            className="flex items-center gap-2 font-mono text-[12px] font-semibold uppercase tracking-wide text-foreground transition hover:text-primary"
          >
            <span aria-hidden>{reflectOpen ? '▼' : '▶'}</span>
            <span aria-hidden>🪞</span>
            <span>错过信号反思 · 实时信号台</span>
            <span className="font-mono text-[9px] font-normal normal-case text-muted-foreground">
              (A 买卖判断 / B 信息方向 / C 技术错误)
            </span>
          </button>
          <div className="flex items-center gap-2 font-mono text-[10px]">
            <Link
              href="/missed-signals"
              className="rounded border border-muted-foreground/30 bg-card px-2 py-0.5 text-foreground hover:bg-muted/40"
              prefetch={false}
            >
              打开专页 →
            </Link>
            <Link
              href="/attribution#missed"
              className="text-muted-foreground underline hover:text-foreground"
              prefetch={false}
            >
              归因页锚点
            </Link>
          </div>
        </div>
        {reflectOpen && (
          <div className="flex flex-col gap-3 pt-2">
            {/* 顶部数字 banner: A/B/C 各自的总数 + 备注 — 自带 setInterval 60s */}
            <MissedSignalsByDimBanner refreshMs={60_000} variant="inline" />
            {/* 三维度细分: A 买卖信号判断 / B 信息分析方向 / C 技术错误 (full 含子分类) — 自带 setInterval 60s */}
            <MissedSignalsDimensionSummary variant="full" refreshMs={60_000} />
            {/* 完整漏检明细: hideDimensionSummary=true 避免重复上方的 Summary */}
            <MissedSignalsPanel
              id="desk-missed"
              refreshMs={60_000}
              externalRefreshToken={reflectTick}
              hideDimensionSummary
            />
          </div>
        )}
      </section>

      {/* v286 — 去掉 overflow-hidden、保留 min-h-0：让 EventBus 在 showAll=true 时
              能撑出「全部事件」的自然高度，左列与右列各自自然高度，不再被 grid 默认 stretch 拽齐。
              flash 动画时通过 ring/shadow 自带，不依赖 overflow-hidden 兜底。 */}
      <div
        ref={eventBusAnchorRef}
        id="event-bus-today"
        className={cn(
          'order-2 min-h-0 rounded-lg transition-all duration-500 lg:order-1',
          eventBusFlash && 'ring-4 ring-long/70 ring-offset-2 ring-offset-background shadow-xl shadow-long/30 animate-pulse',
        )}
      >
        {eventBusTodayOnly && (
          <div className="mb-2 flex items-center justify-between gap-2 rounded border border-long/50 bg-long/10 px-3 py-2 font-mono text-[11px] text-long">
            <span>📍 已过滤为「今日信号」（今日 + DONE + 非 HOLD/FAILED）—— 再点卡片关闭</span>
            <button
              type="button"
              onClick={() => setEventBusTodayOnly(false)}
              className="rounded px-1.5 py-0.5 hover:bg-long/20"
              title="关闭今日信号过滤"
            >
              ✕
            </button>
          </div>
        )}
        <EventBus
          activeMarket={activeMarket}
          onMarketChange={setActiveMarket}
          onEventsUpdated={setEvents}
          todayOnly={eventBusTodayOnly}
          onPriceUpdate={handlePriceUpdate}
        />
      </div>
      <div className="order-1 flex min-h-0 flex-col gap-4 overflow-hidden lg:order-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <MarketTabs active={activeMarket} onChange={setActiveMarket} />
          <div className="flex items-center gap-3">
            <TridentTime compact />
            <PriceTicker prices={pricesForTicker} connected={binanceConnected} activeMarket={activeMarket} />
            <span className="font-mono text-[10px] text-muted-foreground">{events.length} 条事件</span>
          </div>
        </div>
        <StatCards
          events={events}
          activeMarket={activeMarket}
          onSignalsCardClick={handleSignalsCardClick}
          signalsFilterActive={eventBusTodayOnly}
          period={period}
        />
        {activeMarket === 'CRYPTO' || activeMarket === 'GOLD' || activeMarket === 'OIL' ? (
          <>
            <AbnormalFeedbackBar
              asset={
                activeMarket === 'CRYPTO' ? 'BTC'
                  : activeMarket === 'GOLD' ? 'XAU'
                    : 'WTI'
              }
              interval="10m"
            />
            <div key={activeMarket} className="h-[350px] shrink-0 md:h-[420px]">
              <LiveKlineChart market={activeMarket} events={events} />
            </div>
          </>
        ) : (
          <div key={activeMarket} className="min-h-0 flex-1">
            <AgentChat events={events} activeMarket={activeMarket} />
          </div>
        )}
        {activeMarket === 'CRYPTO' || activeMarket === 'GOLD' || activeMarket === 'OIL' ? (
          <SignalLedger
            events={events}
            activeMarket={activeMarket}
            livePrices={pricesByPair}
          />
        ) : null}
      </div>
    </main>
  )
}
