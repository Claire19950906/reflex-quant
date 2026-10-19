'use client'

import { FedWatchProvider } from '@/lib/contexts/fed-watch-context'
import { DiscoverySection } from '@/components/review/discovery/discovery-section'
import { MacroPanel } from '@/components/replay/macro-panel'
import { BlsMacroPanel } from '@/components/replay/bls-macro-panel'
import { Jin10CalendarPanel } from '@/components/replay/jin10-calendar-panel'
import { FlowsPanel } from '@/components/quant/flows-panel'
import { IntradayLiquidityPanel } from '@/components/review/discovery/intraday-liquidity-panel'
import { SmartMoneyPanel } from '@/components/review/discovery/smart-money-panel'
import { WeeklyObservationsPanel } from '@/components/review/discovery/weekly-observations-panel'
import { StorageIndustryPanel } from '@/components/review/discovery/storage-industry-panel'
import { DiscoverOverviewBar } from '@/components/review/discovery/discover-overview-bar'

/**
 * /discover 发现页(v275 起独立)
 *
 * 历史归属: 此前是 /review 反思系统的「段 0 · 发现/探索研究」(7 大支柱)。
 * 现已迁出并升级为独立板块, 同时整建制并入:
 *   · 原 /calendar 宏观日历板块(BLS 一手 + FRED 官方 + 金十财经)
 *   · 原 /flows 资金流动板块(币安/OKX/Bybit/Bitget 永续 + CFTC COT + SPDR GLD + EFFR + Fear&Greed)
 *
 * 共 12 大子段, anchor 一览(顶部 pillar-nav 可点击跳转):
 *   #disc-intraday · 日内流动性情绪(UST10Y/DXY/VIX/ES/NQ, 24h 跳动)
 *   #disc-smart-money · 聪明钱追踪(v285 新增:Hyperliquid 钱包 30s 轮询 + 浏览器通知)
 *   #disc-fed      · Fed 利率预期 + 传导
 *   #disc-fedwatch · Fed 全景监控(v276 新增:路径+FOMC+事件+相关性+自适应阈值)
 *   #disc-cross    · 跨资产同步异常
 *   #disc-asset    · 强信息驱动资产猎手
 *   #disc-event    · 事件驱动快速因果
 *   #disc-cycle    · 周期维度(阴阳共振)
 *   #disc-meta     · AI 元认知自检
 *   #disc-collab   · 多机协作·时区参考
 *   #disc-weekly   · 周度观察(ICSA/美债拍卖, 中间校验)
 *   #disc-storage  · 存储行业(海力士专用, 慢变量)
 *   #disc-macro    · 宏观日历(本轮并入, BLS/FRED/金十三大源)
 *   #disc-flows    · 资金流动(本轮并入, 跨市场公开源聚合)
 *
 * /calendar 与 /flows 入口已改为 redirect → /discover#disc-macro 与 /discover#disc-flows
 * 顶层导航仅保留一个 /discover 入口(替代原先的两个 tab)。
 */
export default function DiscoverPage() {
  return (
    <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4">
      {/* v302+ · L0 顶部概览条 — 4 张聚合卡 + 8 个 pillar 状态点 + 闭环 24h 统计。
          60s 轮询, 失败静默, 让用户一眼掌握全局不需要滚 14 个 section。 */}
      <DiscoverOverviewBar />

      {/* ────── 段 0 · 日内流动性情绪(顶部 banner · 开盘就能看) ────── */}
      <section id="disc-intraday" aria-label="日内流动性情绪" className="scroll-mt-3 flex flex-col gap-3">
        <div className="flex items-end gap-3 border-b border-border pb-1.5">
          <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
            <span aria-hidden>⚡</span>
            日内流动性情绪 · 实时跳动
          </h2>
          <p className="font-mono text-[11px] text-muted-foreground">
            今天这种大跌看的指标 · 美债 10Y/5Y + 美元 DXY + VIX + ES/NQ 期货 · 24h 实时,周末休市
          </p>
        </div>
        <IntradayLiquidityPanel />
      </section>

      {/* ────── 段 0.5 · 聪明钱追踪(v285+ · Hyperliquid 钱包 30s 轮询 + 浏览器提醒) ────── */}
      <section id="disc-smart-money" aria-label="聪明钱追踪" className="scroll-mt-3 flex flex-col gap-3">
        <div className="flex items-end gap-3 border-b border-border pb-1.5">
          <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
            <span aria-hidden>💎</span>
            聪明钱追踪 · 30s 实时
          </h2>
          <p className="font-mono text-[11px] text-muted-foreground">
            把 Hyperliquid 公开钱包加进 watchlist · 有人开 BTC 多 0.5x 立刻推浏览器通知
          </p>
        </div>
        <SmartMoneyPanel />
      </section>

      {/* 7 大探索研究支柱(原 DiscoverySection, 未改结构)
          v283+ · 用 FedWatchProvider 把 /api/discovery/fed-watch 的轮询
          从 2 次/分钟(fed-watch + fed-mailbox 各 60s) 合并为 1 次/分钟。
          解决 #2「双组件同接口重复请求」。 */}
      <FedWatchProvider>
        <DiscoverySection />
      </FedWatchProvider>

      {/* ────── 段 A · 周度观察(中间校验 · ICSA / 美债拍卖) ────── */}
      <section id="disc-weekly" aria-label="周度观察" className="scroll-mt-3 flex flex-col gap-3">
        <div className="flex items-end gap-3 border-b border-border pb-1.5">
          <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
            <span aria-hidden>📅</span>
            周度观察 · 中间校验节奏
          </h2>
          <p className="font-mono text-[11px] text-muted-foreground">
            初请失业金(每周四) + 美债拍卖(每周多档) — 月度数据前的方向校验,不与日/季度混用
          </p>
        </div>
        <WeeklyObservationsPanel />
      </section>

      {/* ────── 段 A2 · 存储行业(海力士专用 · 慢变量) ────── */}
      <section id="disc-storage" aria-label="存储行业" className="scroll-mt-3 flex flex-col gap-3">
        <div className="flex items-end gap-3 border-b border-border pb-1.5">
          <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
            <span aria-hidden>💾</span>
            存储行业 · 海力士专用 · 慢变量
          </h2>
          <p className="font-mono text-[11px] text-muted-foreground">
            DRAM/NAND 现货 + 合约价 + 库存 + 三厂 CapEx — 月/季度拐点,不被日内美债噪声带偏
          </p>
        </div>
        <StorageIndustryPanel />
      </section>

      {/* ────── 段 B · 宏观日历(原 /calendar 整建制并入) ────── */}
      <section id="disc-macro" aria-label="宏观日历" className="scroll-mt-3 flex flex-col gap-3">
        <div className="flex items-end gap-3 border-b border-border pb-1.5">
          <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
            <span aria-hidden>📅</span>
            宏观日历
          </h2>
          <p className="font-mono text-[11px] text-muted-foreground">
            BLS 美国劳工统计局(一手) + FRED 官方宏观指标 + 金十财经日历 — 任一数据公布自动生成事件进实时信号台
          </p>
        </div>
        <div className="surface-card shrink-0 p-2.5">
          <h3 className="font-mono text-[10px] uppercase tracking-widest text-primary">宏观日历板块 · 数据统筹</h3>
          <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
            本板块统筹展示宏观数据源:
            <b className="text-foreground">BLS 美国劳工统计局</b>(CPI/非农/失业率官方一手发布)
            + <b className="text-foreground">FRED 官方宏观指标</b>(美国 CPI/非农/利率/美债等官方统计)
            + <b className="text-foreground">金十财经日历</b>(本周全球财经数据, 含预测/前值/实际)。
            任一数据公布会自动生成事件进实时信号台, 并触发 AI 实时分析与模拟盘交易。
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[9px] text-muted-foreground/70">
            <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm border border-primary bg-primary/25" />高影响 · 已公布</span>
            <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm border border-primary/40 bg-primary/10" />高影响 · 未公布(即将公布)</span>
            <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm border border-border bg-secondary/40" />普通 · 已公布</span>
            <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm border border-border" />普通 · 未公布</span>
            <span>★ = 重要性</span>
          </div>
        </div>
        <BlsMacroPanel />
        <MacroPanel />
        <Jin10CalendarPanel />
      </section>

      {/* ────── 段 C · 资金流动(原 /flows 整建制并入) ────── */}
      <section id="disc-flows" aria-label="资金流动" className="scroll-mt-3 flex flex-col gap-3">
        <div className="flex items-end gap-3 border-b border-border pb-1.5">
          <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
            <span aria-hidden>💸</span>
            资金流动
          </h2>
          <p className="font-mono text-[11px] text-muted-foreground">
            跨市场公开源聚合 · 永续多空比 / 持仓量 / 资金费率 / 主动买卖比 / CFTC COT / SPDR GLD / EFFR / Fear&Greed
          </p>
        </div>
        <FlowsPanel />
      </section>
    </main>
  )
}
