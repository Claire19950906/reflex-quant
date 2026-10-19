import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import type { Signal } from '@/lib/quant-data'
import { cn } from '@/lib/utils'

const config: Record<
  Signal,
  { label: string; className: string; Icon: typeof ArrowUpRight }
> = {
  BUY: {
    label: 'BUY',
    className: 'border-long/40 bg-long/10 text-long',
    Icon: ArrowUpRight,
  },
  SELL: {
    label: 'SELL',
    className: 'border-short/40 bg-short/10 text-short',
    Icon: ArrowDownRight,
  },
  HOLD: {
    label: 'HOLD',
    className: 'border-hold/40 bg-hold/10 text-hold',
    Icon: Minus,
  },
  FAILED: {
    label: '分析失败',
    className: 'border-short/40 bg-short/10 text-short',
    Icon: Minus,
  },
}

export function SignalBadge({
  signal,
  traded,
  note,
  className,
}: {
  signal: Signal
  /** BUY/SELL 是否已实际建仓（有入场价）。false = AI 方向但未下单 */
  traded?: boolean
  /** 未下单原因（tooltip 展示） */
  note?: string
  className?: string
}) {
  const { label, className: tone, Icon } = config[signal]
  const isDirection = signal === 'BUY' || signal === 'SELL'
  // 未下单：方向色保留但降低透明度 + 虚线边框 + "未下单"小字，一眼区分"AI 方向 vs 已成交"
  const notTraded = isDirection && traded === false
  return (
    <span
      title={
        notTraded
          ? `AI 方向 ${label}，但未实际下单${note ? `：${note}` : ''}（展开看交易决策）`
          : signal === 'HOLD' && note
            ? `AI 观望原因：${note}`
            : undefined
      }
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-xs font-bold tracking-wider tabular-nums',
        tone,
        notTraded && 'border-dashed opacity-70',
        className,
      )}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      {label}
      {notTraded && (
        <span className="rounded bg-card/80 px-0.5 font-mono text-[8px] font-normal tracking-normal text-muted-foreground">
          未下单
        </span>
      )}
    </span>
  )
}
