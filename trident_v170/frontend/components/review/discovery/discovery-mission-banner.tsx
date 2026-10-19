'use client'

import { Telescope, Sparkles, Network, Globe2, History, Radar, Users, AlertTriangle, Beaker, Bell } from 'lucide-react'
import { useState } from 'react'
import { DiscoveryUnreadAlertsModal } from './discovery-unread-alerts-modal'
import { usePoll } from '@/lib/hooks/use-poll'

/**
 * 段 0 · 任务说明横幅
 * 把用户原话翻译成系统任务 — 让用户一眼看到本板块的边界
 * v275: 宏观日历 + 资金流动整建制并入, 共 9 大子段
 * v276: 加 in-app 实时异动红点(每 60s 拉一次 /api/discovery/unread-alerts)
 * v276+: 点红点弹详情 modal + 一键已读
 * v283: 改用 usePoll hook(替代手写 useState/useEffect/setInterval + abort flag)
 */

type UnreadAlerts = {
  ok: boolean
  total_unread?: number
  sections?: {
    fed_anomalies?: { ok: boolean; count?: number }
    cross_asset_high?: { ok: boolean; count?: number }
    cycle_signals?: { ok: boolean; count?: number }
    missed_signals?: { ok: boolean; count?: number }
    fed_pre_event?: { ok: boolean; upcoming_24h?: number }
  }
  error?: string
}

export function DiscoveryMissionBanner() {
  const [modalOpen, setModalOpen] = useState(false)

  // v283: 用统一 hook 替代 12 行手写轮询模板
  //   - 自动 abort + unmount safety + clearInterval + 错误处理
  //   - 后端若 ok=false 仍要展示空状态,所以 transform 不过滤掉 (只看 data 是否为 null)
  const alertsPoll = usePoll<UnreadAlerts>('/discovery/unread-alerts', {
    intervalMs: 60_000,
  })
  const alerts: UnreadAlerts = alertsPoll.data ?? { ok: false, error: alertsPoll.error ?? undefined }

  const s = alerts.sections || {}
  const fedAnomalies = s.fed_anomalies?.count ?? 0
  const crossAsset = s.cross_asset_high?.count ?? 0
  const cycleSig = s.cycle_signals?.count ?? 0
  const missed = s.missed_signals?.count ?? 0
  const total = alerts.total_unread ?? (fedAnomalies + crossAsset + cycleSig + missed)
  const upcoming = s.fed_pre_event?.upcoming_24h ?? 0

  return (
    <div className="rounded border border-violet-500/40 bg-violet-500/5 p-3">
      <div className="flex items-center gap-2 font-mono text-xs">
        <Telescope className="h-4 w-4 text-violet-500" aria-hidden />
        <span className="font-bold uppercase tracking-widest text-violet-600 dark:text-violet-400">
          发现 · 探索研究 / 宏观数据统筹
        </span>
        <span className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 text-[9px] font-bold text-violet-600 dark:text-violet-400">
          DISCOVERY · 不下单
        </span>
        <span className="rounded border border-indigo-500/30 bg-indigo-500/10 px-1.5 py-0.5 text-[9px] font-bold text-indigo-600 dark:text-indigo-400">
          9 大子段
        </span>

        {/* v276: in-app 实时异动红点(可点击打开 modal) */}
        {alerts.ok && total > 0 ? (
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="ml-auto inline-flex items-center gap-1 rounded-full border border-rose-500/50 bg-rose-500/15 px-2 py-0.5 text-[10px] font-bold text-rose-600 hover:bg-rose-500/25 dark:text-rose-400"
            title={`Fed 异动 ${fedAnomalies} / 跨资产高危 ${crossAsset} / 周期信号 ${cycleSig} / 漏检 ${missed} · 点击查看详情`}
          >
            <Bell className="h-3 w-3 animate-pulse" />
            <span>未读 {total}</span>
          </button>
        ) : alerts.ok && upcoming > 0 ? (
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="ml-auto inline-flex items-center gap-1 rounded-full border border-sky-500/50 bg-sky-500/15 px-2 py-0.5 text-[10px] font-bold text-sky-600 hover:bg-sky-500/25 dark:text-sky-400"
            title={`未来 24h 重大 Fed 事件 ${upcoming} 个 · 点击查看详情`}
          >
            <Bell className="h-3 w-3" />
            <span>未来 24h {upcoming} 个 Fed 事件</span>
          </button>
        ) : alerts.error ? (
          <span className="ml-auto inline-flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[9px] text-amber-600 dark:text-amber-400">
            ⚠️ 异动计数暂不可用
          </span>
        ) : null}
      </div>

      <p className="mt-2 text-[11px] leading-5 text-muted-foreground">
        <strong className="text-foreground">板块定位</strong>:
        寻找铃兰总结规律的前置 — 主动探索「还没有进因果图谱/经验库」的潜在规律。
        不只是「看信号、看反思」,而是「找下一条规律」「找下一个机会」
        「补选优思维框架的死角和不足」。
        <span className="ml-1 text-foreground/70">
          v275 起宏观日历(BLS/FRED/金十) 与 资金流动 也并入本板块,共 9 大子段。
        </span>
        <span className="ml-1 text-amber-600 dark:text-amber-400">
          v276 起每日 07:00 自动推送「昨日发现规律汇总」+ Fed 事件 30min/24h 事前提醒。
        </span>
        {alerts.ok && (total > 0 || upcoming > 0) ? (
          <span className="ml-1 text-rose-600 dark:text-rose-400">
            · 当前 <strong>{total}</strong> 条未读异动
            {upcoming > 0 ? ` + 未来 24h ${upcoming} 个 Fed 事件待提醒` : ''}。
          </span>
        ) : null}
      </p>

      <ul className="mt-2 grid grid-cols-1 gap-1.5 text-[10px] text-muted-foreground md:grid-cols-2">
        <li className="flex items-start gap-1">
          <Sparkles className="mt-0.5 h-3 w-3 shrink-0 text-amber-500" />
          <span><strong className="text-foreground">新规律发现</strong>:从跨资产同步、事件冲击、周期共振中挖掘尚未沉淀的因果链</span>
        </li>
        <li className="flex items-start gap-1">
          <Radar className="mt-0.5 h-3 w-3 shrink-0 text-sky" />
          <span><strong className="text-foreground">新投资机会</strong>:强信息驱动资产(海力士/山寨币/超级低估)的实时雷达</span>
        </li>
        <li className="flex items-start gap-1">
          <Network className="mt-0.5 h-3 w-3 shrink-0 text-fuchsia-500" />
          <span><strong className="text-foreground">思维框架补完</strong>:用 AI 元认知测试暴露因果图谱的盲信风险</span>
        </li>
        <li className="flex items-start gap-1">
          <Globe2 className="mt-0.5 h-3 w-3 shrink-0 text-emerald-500" />
          <span><strong className="text-foreground">多视角·多周期·多市场</strong>:不同时区主力盘 + 大中小周期 + 跨资产同步</span>
        </li>
      </ul>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[9px] text-muted-foreground/70">
        <span className="flex items-center gap-1"><History className="h-2.5 w-2.5" />本段为研究前置,不直接进模拟盘</span>
        <span className="flex items-center gap-1"><Users className="h-2.5 w-2.5" />多机 git 协作见 #disc-collab</span>
        <span className="flex items-center gap-1"><Beaker className="h-2.5 w-2.5" />假设需要先质询才能入图谱</span>
        <span className="flex items-center gap-1 text-rose-500"><AlertTriangle className="h-2.5 w-2.5" />警惕先设立预期再做决策的确认偏差</span>
      </div>

      {/* v276+: 详情 modal — 点红点弹出 */}
      <DiscoveryUnreadAlertsModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onMarked={async () => {
          // 立即重新拉一次 banner 计数,避免 60s 延迟
          await alertsPoll.refresh()
        }}
      />
    </div>
  )
}
