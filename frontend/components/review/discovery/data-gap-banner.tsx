'use client'

import { AlertOctagon, AlertTriangle, CheckCircle2, ChevronDown, ChevronUp } from 'lucide-react'
import { useState } from 'react'

import { usePoll } from '@/lib/hooks/use-poll'
import { cn } from '@/lib/utils'

/**
 * 段 0.5 · 数据缺口横幅（v497）
 *
 * 为什么单独做一条：发现页 8 个 pillar 里常有 6 个显示「暂无数据」，但原因完全不同 ——
 *   ① 真没数据（今天确实没异动）；
 *   ② 上游断了（黄金基本面 0 分量、DXY 49 天没更新、LSR 没有样本）；
 *   ③ 口径没接上（面板计数写死 0）。
 * 用户无法分辨，最后只能得出「这个系统很空」的错误结论。这条横幅把 ② 单独拎出来，
 * 红=影响决策可信度，黄=影响验证但还能用。
 *
 * 数据源：GET /api/health/data-gaps（后端逐条给数字和年龄，不在前端猜）
 * 口径变化：level=green 时只留一行细条，不占版面。
 */

type Gap = {
  key: string
  label: string
  level: 'red' | 'amber'
  detail: string
  value?: number | string | null
}

type GapResp = {
  ok: boolean
  level: 'red' | 'amber' | 'green'
  red: number
  amber: number
  gap_count: number
  gaps: Gap[]
  ts?: number
  error?: string
}

export function DataGapBanner() {
  const [expanded, setExpanded] = useState(false)

  // 5 分钟一次即可：这些缺口都是「小时级以上」的慢变量，轮询太勤只是白占请求。
  const poll = usePoll<GapResp>('/health/data-gaps', { intervalMs: 300_000 })
  const data = poll.data

  // 接口本身没接上时**不显示**横幅 —— 否则「接口没实现」会被误读成「系统有缺口」。
  if (!data || !data.ok || !data.gaps) return null

  const red = data.red ?? 0
  const amber = data.amber ?? 0
  const level = data.level

  if (level === 'green' || (!red && !amber)) {
    return (
      <div className="flex shrink-0 items-center gap-2 rounded border border-emerald-500/30 bg-emerald-500/5 px-3 py-1.5 font-mono text-[11px] text-emerald-300/80">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
        <span>数据自检通过：黄金基本面 / 宏观序列 / 多空持仓 / 异常信号 / 影子账本 / 背离样本 均正常</span>
      </div>
    )
  }

  const tone = red > 0
    ? 'border-rose-500/45 bg-rose-500/8 text-rose-200'
    : 'border-amber-500/45 bg-amber-500/8 text-amber-100'

  return (
    <div className={cn('min-h-[36px] shrink-0 overflow-visible rounded border px-3 py-2', tone)}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        {red > 0
          ? <AlertOctagon className="h-4 w-4 shrink-0" />
          : <AlertTriangle className="h-4 w-4 shrink-0" />}
        <span className="text-xs font-medium">
          数据缺口 {red + amber} 项
          <span className="ml-2 font-mono text-[11px] opacity-80">
            红 {red} · 黄 {amber}
          </span>
        </span>
        <span className="ml-1 hidden truncate font-mono text-[11px] opacity-70 sm:inline">
          {data.gaps.slice(0, 2).map((g) => g.label).join(' / ')}
          {data.gaps.length > 2 ? ' …' : ''}
        </span>
        <span className="ml-auto opacity-70">
          {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </span>
      </button>

      {expanded && (
        <ul className="mt-2 space-y-1 border-t border-current/20 pt-2 font-mono text-[11px] leading-relaxed">
          {data.gaps.map((g) => (
            <li key={g.key} className="flex items-start gap-2">
              <span
                className={cn(
                  'mt-[3px] inline-block h-1.5 w-1.5 shrink-0 rounded-full',
                  g.level === 'red' ? 'bg-rose-400' : 'bg-amber-400',
                )}
              />
              <span className="shrink-0 opacity-90">{g.label}</span>
              <span className="opacity-70">{g.detail}</span>
            </li>
          ))}
          {poll.lastFetchedAt && (
            <li className="pt-1 text-[10px] opacity-50">
              检查于 {new Date(poll.lastFetchedAt).toLocaleTimeString('zh-CN')} · 5 分钟刷新
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
