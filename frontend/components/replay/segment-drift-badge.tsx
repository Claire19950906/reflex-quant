/**
 * SegmentDriftBadge — 段位漂移告警的 UI 展示(v302+ P1)。
 *
 * 用途: 在 /review 任何面板顶部显示"8 段命中率与历史基线的偏离度",
 *       红色 = 严重漂移(±30pp),黄色 = 一般漂移(±15pp),绿色 = 稳定。
 *
 * 设计:
 *  - 纯展示组件: 接收已经算好的 DriftResult,不再做计算
 *  - 默认前 3 条最严重告警,完整列表在 title 属性(hover 可见)
 *  - max=0 时显示"段位稳定"绿勾
 *  - 用 cn() 控制颜色,与项目其他徽标一致
 */

import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { DriftResult } from '@/lib/segment-drift'

export type SegmentDriftBadgeProps = {
  result: DriftResult
  /** 显示最多 N 条(默认 3) */
  max?: number
  /** 紧凑模式:只显示"段位漂移 N" 不展开段名 */
  compact?: boolean
  className?: string
}

export function SegmentDriftBadge({
  result,
  max = 3,
  compact = false,
  className,
}: SegmentDriftBadgeProps) {
  const alerts = result.alerts.slice(0, max)
  if (alerts.length === 0) {
    return (
      <span
        className={cn('inline-flex items-center gap-1', className)}
        title="8 段命中率与历史基线一致"
      >
        <CheckCircle2 className="h-3 w-3 text-green-500" />
        段位稳定
      </span>
    )
  }
  const alertCount = result.alerts.filter((a) => a.severity === 'alert').length
  const warnCount = result.alerts.filter((a) => a.severity === 'warn').length
  const titleText = result.alerts
    .map((a) => `${a.stage} ${a.delta >= 0 ? '+' : ''}${Math.round(a.delta * 100)}pp`)
    .join(' / ')
  return (
    <span
      className={cn('inline-flex items-center gap-1', className)}
      title={titleText}
    >
      <AlertTriangle
        className={cn(
          'h-3 w-3',
          alertCount > 0 ? 'text-red-500' : 'text-yellow-500',
        )}
      />
      段位漂移
      {alertCount > 0 && (
        <span className="font-bold text-red-500">{alertCount}</span>
      )}
      {warnCount > 0 && (
        <span className="font-bold text-yellow-600 dark:text-yellow-300">
          {warnCount}
        </span>
      )}
      {!compact && (
        <>
          <span className="text-muted-foreground/60">前 {alerts.length}:</span>
          {alerts.map((a) => (
            <span
              key={a.stage}
              className={cn(
                'rounded px-1',
                a.severity === 'alert'
                  ? 'bg-red-500/15 text-red-500'
                  : 'bg-yellow-500/15 text-yellow-700 dark:text-yellow-300',
              )}
            >
              {`${a.stage} ${a.delta >= 0 ? '+' : ''}${Math.round(a.delta * 100)}pp`}
            </span>
          ))}
        </>
      )}
    </span>
  )
}
