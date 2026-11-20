'use client'

import { useEffect, useState } from 'react'

import { FedWatchProvider } from '@/lib/contexts/fed-watch-context'
import {
  DiscoveryAssetGroup,
  DiscoveryFedGroup,
  DiscoverySystemGroup,
} from '@/components/review/discovery/discovery-section'
import { DiscoveryMissionBanner } from '@/components/review/discovery/discovery-mission-banner'
import { DataGapBanner } from '@/components/review/discovery/data-gap-banner'
import { MacroPanel } from '@/components/replay/macro-panel'
import { BlsMacroPanel } from '@/components/replay/bls-macro-panel'
import { Jin10CalendarPanel } from '@/components/replay/jin10-calendar-panel'
import { FlowsPanel } from '@/components/quant/flows-panel'
import { IntradayLiquidityPanel } from '@/components/review/discovery/intraday-liquidity-panel'
import { LsrPanel } from '@/components/review/discovery/lsr-panel'
import { SmartMoneyPanel } from '@/components/review/discovery/smart-money-panel'
import { InstitutionalPanel } from '@/components/review/discovery/institutional-panel'
import { WeeklyObservationsPanel } from '@/components/review/discovery/weekly-observations-panel'
import { StorageIndustryPanel } from '@/components/review/discovery/storage-industry-panel'
import { DiscoverOverviewBar } from '@/components/review/discovery/discover-overview-bar'
import { LazySection } from '@/lib/lazy-section'

/**
 * /discover 发现页
 *
 * v345.9 · 信息架构重构（用户反馈：没必要的挤在一处、重复的、没用的说明文字都删掉）
 *
 * 旧结构 = 14 个面板平铺，每段一句 20~40 字的长副标题，顶部还有一整块任务说明，
 * 底部再补一段「板块定位」。结果说明文字比数据多，用户在滚屏找数据。
 *
 * 新结构 = 5 组，按「数据变化的快慢」排，快的在上、慢的在下：
 *   ① 情绪与仓位   日内流动性 · 多空持仓比 · 聪明钱
 *   ② 资产与事件   资产猎手 · 事件因果 · 跨资产 · 周期
 *   ③ Fed 与宏观    Fed 一块（预期+传导+全景+邮箱）· 宏观日历 · 周度观察
 *   ④ 资金与行业   资金流动 · 存储行业
 *   ⑤ 系统自检     AI 元认知 · 多机协作
 *
 * 同时删除：每段的长副标题、宏观日历里的整段「数据统筹」说明、底部「板块定位」代码路径说明。
 * 锚点 id（disc-intraday / disc-fed / ...）全部保留，顶部概览条的跳转继续可用。
 */
export default function DiscoverPage() {
  // 这一页是 14 个实时面板的拼装，每个面板首帧都会算「当前时间/实时状态」类文本，
  // 而 /discover 是构建期静态预渲染 —— 服务端那份文字被冻结在构建那一刻，
  // 浏览器 hydration 再算一次就对不上 → React #425，整页掉进错误边界。
  // 修法：首帧只出骨架，mounted 之后再挂真面板。
  const [mounted, setMounted] = useState(false)
  useEffect(() => setMounted(true), [])

  if (!mounted) {
    return (
      <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 p-3 md:p-4">
        <div className="h-16 animate-pulse rounded-lg border border-border bg-card/60" />
        <div className="h-40 animate-pulse rounded-lg border border-border bg-card/40" />
        <div className="h-64 animate-pulse rounded-lg border border-border bg-card/30" />
        <p className="font-mono text-[11px] text-muted-foreground">发现页加载中…</p>
      </main>
    )
  }

  return (
    <FedWatchProvider>
      <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-3 md:p-4">
        {/* L0 概览条 —— 4 张聚合卡 + 状态点 + 24h 闭环统计，60s 刷新 */}
        <DiscoverOverviewBar />

        {/* 一行边界说明 + 未读异动红点（原任务说明横幅，已从 4 段压成 1 行） */}
        <DiscoveryMissionBanner />

        {/* v497 · 数据缺口横幅：把「上游断了」和「今天真的没数据」分开，红/黄两档 */}
        <DataGapBanner />

        {/* ═══ ① 情绪与仓位（分钟级，开盘就能看） ═══ */}
        <Group
          title="情绪与仓位"
          hint="分钟级 · 盘中实时"
          ids={['disc-intraday', 'disc-lsr', 'disc-smart-money']}
        >
          <Sub id="disc-intraday" icon="⚡" title="日内流动性情绪" tag="24h">
            <IntradayLiquidityPanel />
          </Sub>
          <Sub id="disc-lsr" icon="⚖️" title="多空持仓比" tag="1h">
            <LsrPanel />
          </Sub>
          <Sub id="disc-smart-money" icon="💎" title="聪明钱追踪" tag="30s">
            <SmartMoneyPanel />
          </Sub>
        </Group>

        {/* v498 · 知名投资人 13F（季度级，SEC 公开申报；与链上聪明钱区分） */}
        <Group title="知名投资人持仓" hint="季度级 · SEC 13F-HR" ids={['disc-13f']}>
          <Sub id="disc-13f" icon="🏛" title="13F 机构持仓" tag="季度">
            <InstitutionalPanel />
          </Sub>
        </Group>

        {/* ═══ ② 资产与事件（日级，找机会） ═══ */}
        <Group title="资产与事件" hint="日级 · 找下一次机会" ids={['disc-asset', 'disc-event', 'disc-cross', 'disc-undervalued', 'disc-cycle', 'disc-crowd']}>
          <DiscoveryAssetGroup />
        </Group>

        {/* ═══ ③ Fed 与宏观节奏（政策与日历） ═══ */}
        {/* v384: ③④⑤ 改为进视口再挂载 —— 首屏只挂 ①②，少 10+ 个并发请求，
            首屏从 ~40s 拉到秒级；滚到时再挂，锚点跳转同样会触发（IO 跟随滚动）。 */}
        <LazySection rootMargin="600px 0px" minHeight={360}>
        <Group title="Fed 与宏观节奏" hint="政策 · 日历 · 月度/周度校验" ids={['disc-fed', 'disc-macro', 'disc-weekly']}>
          <DiscoveryFedGroup />

          <Sub id="disc-macro" icon="📅" title="宏观日历" tag="BLS · FRED · 金十">
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground/70">
              <span className="flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-sm border border-primary bg-primary/25" />高影响·已公布
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-sm border border-primary/40 bg-primary/10" />高影响·待公布
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-sm border border-border bg-secondary/40" />普通·已公布
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-sm border border-border" />普通·待公布
              </span>
              <span>★ 重要性</span>
            </div>
            <BlsMacroPanel />
            <MacroPanel />
            <Jin10CalendarPanel />
          </Sub>

          <Sub id="disc-weekly" icon="🗓" title="周度观察" tag="每周四 · 美债拍卖">
            <WeeklyObservationsPanel />
          </Sub>
        </Group>
        </LazySection>

        {/* ═══ ④ 资金与行业（慢变量） ═══ */}
        <LazySection rootMargin="600px 0px" minHeight={320}>
        <Group title="资金与行业" hint="慢变量 · 结构拐点" ids={['disc-flows', 'disc-storage']}>
          <Sub id="disc-flows" icon="💸" title="资金流动" tag="跨市场公开源">
            <FlowsPanel />
          </Sub>
          <Sub id="disc-storage" icon="💾" title="存储行业" tag="月/季度">
            <StorageIndustryPanel />
          </Sub>
        </Group>
        </LazySection>

        {/* ═══ ⑤ 系统自检（低频） ═══ */}
        <LazySection rootMargin="600px 0px" minHeight={240}>
        <Group title="系统自检" hint="低频 · 入库前" ids={['disc-meta', 'disc-collab']}>
          <DiscoverySystemGroup />
        </Group>
        </LazySection>
      </main>
    </FedWatchProvider>
  )
}

/** 分组外壳：左侧竖条 + 组名 + 一句极短口径，避免每段各写一长句。 */
function Group({
  title,
  hint,
  ids,
  children,
}: {
  title: string
  hint: string
  ids: string[]
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-card/20 p-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border pb-2">
        <h2 className="font-mono text-sm font-bold tracking-wide text-foreground">{title}</h2>
        <p className="font-mono text-[11px] text-muted-foreground">{hint}</p>
        <div className="ml-auto flex flex-wrap gap-1">
          {ids.map((id) => (
            <a
              key={id}
              href={`#${id}`}
              className="rounded border border-border bg-secondary/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
            >
              #{id.replace('disc-', '')}
            </a>
          ))}
        </div>
      </div>
      {children}
    </div>
  )
}

/** 子段：只有图标 + 名称 + 频率标签，不再挂长副标题。 */
function Sub({
  id,
  icon,
  title,
  tag,
  children,
}: {
  id: string
  icon: string
  title: string
  tag?: string
  children: React.ReactNode
}) {
  return (
    <section id={id} aria-label={title} className="flex scroll-mt-3 flex-col gap-2">
      <div className="flex items-center gap-2">
        <span aria-hidden className="text-sm">{icon}</span>
        <h3 className="font-mono text-[13px] font-bold text-foreground">{title}</h3>
        {tag ? (
          <span className="rounded border border-border bg-secondary/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {tag}
          </span>
        ) : null}
      </div>
      {children}
    </section>
  )
}
