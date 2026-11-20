'use client'

import { Telescope, Bell } from 'lucide-react'
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
        <span className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 text-[11px] font-bold text-violet-600 dark:text-violet-400">
          DISCOVERY · 不下单
        </span>
        <span className="rounded border border-border bg-secondary/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          5 组
        </span>

        {/* v276: in-app 实时异动红点(可点击打开 modal) */}
        {alerts.ok && total > 0 ? (
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="ml-auto inline-flex items-center gap-1 rounded-full border border-rose-500/50 bg-rose-500/15 px-2 py-0.5 text-[11px] font-bold text-rose-600 hover:bg-rose-500/25 dark:text-rose-400"
            title={`Fed 异动 ${fedAnomalies} / 跨资产高危 ${crossAsset} / 周期信号 ${cycleSig} / 漏检 ${missed} · 点击查看详情`}
          >
            <Bell className="h-3 w-3 animate-pulse" />
            <span>未读 {total}</span>
          </button>
        ) : alerts.ok && upcoming > 0 ? (
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="ml-auto inline-flex items-center gap-1 rounded-full border border-sky-500/50 bg-sky-500/15 px-2 py-0.5 text-[11px] font-bold text-sky-600 hover:bg-sky-500/25 dark:text-sky-400"
            title={`未来 24h 重大 Fed 事件 ${upcoming} 个 · 点击查看详情`}
          >
            <Bell className="h-3 w-3" />
            <span>未来 24h {upcoming} 个 Fed 事件</span>
          </button>
        ) : alerts.error ? (
          <span className="ml-auto inline-flex items-center gap-1 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
            ⚠️ 异动计数暂不可用
          </span>
        ) : null}
      </div>

      <p className="mt-1.5 text-[11px] leading-5 text-muted-foreground">
        只探索、不下单 · 结论先进因果图谱，审批后才进分析链路
      </p>

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
