'use client'

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import type { PriceMap } from '@/hooks/use-binance-prices'
import { useBinancePrices } from '@/hooks/use-binance-prices'
import { cn } from '@/lib/utils'
import { EventBus } from '@/components/quant/event-bus'
import { AgentChat } from '@/components/quant/agent-chat'
import { LiveKlineChart } from '@/components/quant/live-kline-chart'
import { TechSignalsPanel } from '@/components/quant/tech-signals-panel'
import { SignalLedger } from '@/components/quant/signal-ledger'
import { StatCards } from '@/components/quant/stat-cards'
import { PaperEquityCard } from '@/components/quant/paper-equity-card'
import { AccountKpis } from '@/components/quant/account-kpis'
import { PositionsTable } from '@/components/quant/positions-table'
import { useReplayPositions } from '@/components/quant/use-replay-positions'
import { MarketTabs } from '@/components/quant/market-tabs'
import { PriceTicker } from '@/components/quant/price-ticker'
import { ReviewOverview } from '@/components/replay/review-overview'
import { ErrorTaxonomyPanel } from '@/components/analysis/error-taxonomy-panel'
import { CalendarHintBanner } from '@/components/quant/calendar-hint-banner'
import { TridentTime } from '@/components/quant/trident-time'
import { AbnormalFeedbackBar } from '@/components/quant/abnormal-feedback-bar'
import {
  PeriodSelector,
  resolvePeriod,
  type PeriodKey,
  type PeriodRange,
} from '@/lib/data-channel-standards'
import { MissedSignalsPanel } from '@/components/v237/MissedSignalsPanel'
import { ShadowLedgerPanel } from '@/components/v291/shadow-ledger-panel'
import { CapabilityPanel } from '@/components/quant/capability-panel'
import { DivergenceStrip } from '@/components/quant/divergence-strip'
import { MainViewsBand } from '@/components/quant/main-views-band'
import Link from 'next/link'

// P3-UI: 首屏之后的「诊断/复盘」面板默认折叠，只留标题行；
//   有需要再点开。把第一屏让给「正在发生什么 + 账户结论」。
const COLLAPSE_STORAGE_PREFIX = 'trident-collapse:'

function CollapsibleSection({
  title,
  hint,
  defaultOpen = false,
  children,
}: {
  title: string
  hint?: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  const storageKey = COLLAPSE_STORAGE_PREFIX + title
  const [open, setOpen] = useState(() => {
    if (typeof window === 'undefined') return defaultOpen
    try {
      const saved = window.localStorage.getItem(storageKey)
      return saved === null ? defaultOpen : saved === '1'
    } catch {
      return defaultOpen
    }
  })
  const toggle = () => {
    setOpen((v) => {
      const next = !v
      try {
        window.localStorage.setItem(storageKey, next ? '1' : '0')
      } catch {
        // 存储不可用（隐私模式等）时仅保留本次会话状态
      }
      return next
    })
  }
  return (
    <section className="col-span-1 lg:col-span-2">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center gap-2 border-b border-border bg-card/60 px-3 py-2 font-mono text-[12px] font-semibold uppercase tracking-wide text-foreground transition hover:text-primary"
      >
        <span aria-hidden>{open ? '▼' : '▶'}</span>
        <span>{title}</span>
        {hint && (
          <span className="font-mono text-[11px] font-normal normal-case text-muted-foreground">{hint}</span>
        )}
      </button>
      {open ? <div className="flex flex-col gap-3 pt-2">{children}</div> : null}
    </section>
  )
}

export function LiveDesk() {
  const [events, setEvents] = useState<ApiEvent[]>([])
  const [activeMarket, setActiveMarket] = useState<MarketCategory>('ALL')
  // P3-UI: 账户快照统一取数 —— 首页三份卡片共用一份 /replay/positions 轮询，
  //   不再各自 setInterval 打后端（原本 3 份并行轮询）。
  const { data: replaySnapshot, err: replayErr, loading: replayLoading } = useReplayPositions()
  // v282+: HONESTY — change24h nullable,前端按 '—' 显示而非假 0%
  const [xauPrice, setXauPrice] = useState<{ price: number; change24h: number | null } | null>(null)
  const [wtiPrice, setWtiPrice] = useState<{ price: number; change24h: number | null } | null>(null)
  const { prices, pricesByPair: binancePairs, connected: marketConnected, loaded: marketLoaded, ageSec: priceAgeSec } = useBinancePrices()
  // v310: binanceConnected 改为**派生值**直接算 —— 原先用 state + useEffect 同步,
  //   多一次渲染 + 一个「落后一拍」的中间态。现在不再有 setBinanceConnected。
  const binanceConnected = marketConnected || xauPrice !== null || wtiPrice !== null

  // v180 P0+：全局时段选择器(从 ReviewOverview 提到顶层)
  // 顶部卡片(7 张)+ StatCards(3 张)+ ReviewOverview + EventBus 事件流都按此窗口过滤。
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
  // v351-UI: 漏检反思默认收起 —— 它原本展开着占掉一整屏，把下面的主工作区
  //（EventBus + 行情 + 台账）全挤到首屏之外，第一眼只剩"反思材料"看不到"正在发生什么"。
  // 需要时点一下标题即可展开，信息一条没少。
  const [reflectOpen, setReflectOpen] = useState(false)
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

  // v310: 原先这里用 state + useEffect 同步 binanceConnected, 现已改为上方派生值
  //   (省一次渲染 + 省一个「落后一拍」的中间态)。

  return (
    // v286 — 加 items-start：EventBus 默认展开全部（不限高），左列自然撑高；
    //        右列保持自己的内容高度，不再被左列撑到千行面板那么高。
    <main className="mx-auto grid w-full min-h-0 max-w-[1600px] grid-cols-1 items-start gap-4 p-4 md:p-6 lg:grid-cols-[minmax(380px,1.35fr)_1fr]">
      {/* v180 P0+：全局时段选择器(从 ReviewOverview 提到顶层)
          影响 7 张顶部卡片 + StatCards 3 张今日卡 + 复盘概览 + EventBus 事件流
          PeriodSelector 组件本身由 /lib/data-channel-standards 提供,4 个固定时段
          + 自定义日期范围,UTC ↔ 北京时区自动换算 */}
      <section className="col-span-1 lg:col-span-2">
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border/60 bg-secondary/30 px-3 py-2">
          <span className="font-mono text-[11px] uppercase tracking-widest text-muted-foreground">
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
            className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary"
            title="切换后,7 张顶部卡片 + 3 张今日卡片 + 复盘概览 + 事件流列表都按此窗口过滤"
          >
            当前：{period.label}
          </span>
          {/* v345: 宏观日历提示原来单独占一整段（col-span-2），其实只有一行字 ——
              并进时段条这一行右侧，首屏少一段、信息不丢。 */}
          <div className="ml-auto min-w-0 max-w-[460px] flex-1">
            <CalendarHintBanner />
          </div>
        </div>
      </section>

      {/* v353-UI P1: 顶部结论区 —— 权益 / 已实现 / 胜率 / 活跃信号。
          原来这四组数字散在右栏 400px 里，字号被迫压到 10px，首屏也读不到。
          数据仍来自 /replay/positions，不新增接口。 */}
      <section className="col-span-1 lg:col-span-2">
        <AccountKpis events={events} snapshot={replaySnapshot} err={replayErr} />
      </section>

      {/* v286 — 去掉 overflow-hidden、保留 min-h-0：让 EventBus 在 showAll=true 时
              能撑出「全部事件」的自然高度，左列与右列各自自然高度，不再被 grid 默认 stretch 拽齐。
              flash 动画时通过 ring/shadow 自带，不依赖 overflow-hidden 兜底。 */}
      <div
        ref={eventBusAnchorRef}
        id="event-bus-today"
        className={cn(
          'min-h-0 rounded-lg transition-all duration-500',
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
          period={period}
        />
      </div>
      <div className="flex min-h-0 flex-col gap-4 overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <MarketTabs active={activeMarket} onChange={setActiveMarket} />
          <div className="flex items-center gap-3">
            <TridentTime compact />
            <PriceTicker prices={pricesForTicker} connected={binanceConnected} activeMarket={activeMarket} loaded={marketLoaded} ageSec={priceAgeSec} />
            <span className="font-mono text-[11px] text-muted-foreground">{events.length} 条事件</span>
          </div>
        </div>
        <PaperEquityCard hideValue snapshot={replaySnapshot} err={replayErr} loading={replayLoading} />
        <PositionsTable snapshot={replaySnapshot} err={replayErr} />
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
            {/* v457：技术信号面板 —— 后端在算、以前界面完全看不到 */}
            <TechSignalsPanel
              asset={activeMarket === 'CRYPTO' ? 'BTC' : activeMarket === 'GOLD' ? 'XAU' : 'WTI'}
              days={30}
              limit={6}
            />
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
      <section className="col-span-1 lg:col-span-2">
        <DivergenceStrip />
      </section>


      <section className="col-span-1 lg:col-span-2">
        <MainViewsBand />
      </section>

      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <section className="col-span-1 lg:col-span-2">
        <CapabilityPanel
          title="市场监控 · 异动与广度"
          hint="相对自身异动 / 成交突增 / 关键位突破 / 稳定币基差 / 宏观倒计时 / 市场宽度 / 解锁日历"
          featureIds={['F20', 'F21', 'F23', 'F28', 'F31', 'F34', 'F41', 'F42', 'F43']}
        />
      </section>

      {/* 反思系统同源战绩（A 真实持仓 / B 快速评测）— 与 /review 页数字完全一致；
          P3-UI 默认折叠，首屏不展开复盘战绩，需要时点标题打开。 */}
      <CollapsibleSection title="复盘战绩" hint="A 真实持仓 / B 快速评测 · 与反思系统一致">
        <ReviewOverview period={period} />
      </CollapsibleSection>

      {/* v344 — 三类错误仪表盘：买卖信号判断 / 信息分析方向 / 技术故障；
          P3-UI 默认折叠，属诊断内容，不占首屏。 */}
      <CollapsibleSection title="错误归因" hint="买卖判断 / 信息方向 / 技术故障">
        <ErrorTaxonomyPanel />
      </CollapsibleSection>


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
            <span className="font-mono text-[11px] font-normal normal-case text-muted-foreground">
              (A 买卖判断 / B 信息方向 / C 技术错误)
            </span>
          </button>
          <div className="flex items-center gap-2 font-mono text-[11px]">
            <Link
              href="/missed-signals"
              className="rounded border border-muted-foreground/30 bg-card px-2 py-0.5 text-foreground hover:bg-muted/40"
              prefetch={false}
            >
              打开专页 →
            </Link>
            <Link
              href="/missed-signals"
              className="text-muted-foreground underline hover:text-foreground"
              prefetch={false}
            >
              归因页锚点
            </Link>
          </div>
        </div>
        {reflectOpen && (
          <div className="flex flex-col gap-3 pt-2">
            {/* v344: 这一层原来还挂着「A/B/C 数字 banner + 三维度摘要」两张卡，
                与页面顶部的「三类错误仪表盘」讲的是同一件事（同样的 买卖/方向/技术 三个大类），
                已经重复 → 删掉，只保留仪表盘（摘要） + 下面的明细（MissedSignalsPanel）。 */}
            {/* v335 影子样本: 闸门没放行的单也在记账、到期结算 —— 回答"没样本怎么测" */}
            <ShadowLedgerPanel refreshMs={120_000} days={40} />
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

    </main>
  )
}
