'use client'

/**
 * SegmentHistoryHeatmap — 8 段 × N 天 段位一致性热力图 (v302+ P2 PR-16)。
 *
 * 设计动机:
 *   FailureStatsPanel 的 SegmentDriftBadge 只显示"当前 vs 静态基线",
 *   没法看"是突然漂移还是长期疲软"。8 段 × 7 天热力图直接补这个维度。
 *
 * 视觉:
 *   - 8 行(段名,固定顺序),N 列(日期,左老右新)
 *   - 每格颜色 = (今天 hit_rate - baseline) 的偏离度
 *     · delta >= 0.15  → 绿(超越基线)
 *     · delta in [-0.15, 0.15) → 中性灰(±15pp 容忍带)
 *     · delta < -0.15 → 红(低于基线,饱和度随严重度递增)
 *   - 同时显示文字 +pp / -pp(色盲友好)
 *   - 悬浮 tooltip 显示当天 count
 *
 * 边界:
 *   - items 为空 → 不渲染(直接 return null)
 *   - count = 0 当天 → 全 8 段 灰色斜纹背景 + '—'
 *
 * 性能:
 *   - 8 × 30 = 240 cell,纯 CSS Grid,无 SVG 重排
 *   - useMemo 算 colorClass,避免每渲染重算
 */

import { useMemo } from 'react'
import { cn } from '@/lib/utils'
import { FIXED_STAGE_KEYS, type StageKey } from '@/lib/segment-drift'
import { useSegmentHistory, type SegmentHistoryDay } from '@/lib/hooks/use-segment-history'
import { Loader2 } from 'lucide-react'

export type SegmentHistoryHeatmapProps = {
  /** 1~30,默认 7 */
  days?: number
  className?: string
}

const STAGE_LABELS: Record<StageKey, string> = {
  总框架: '总框架',
  因素定位: '因素定位',
  驱动力: '驱动力',
  水位博弈: '水位博弈',
  跨资产资金: '跨资产资金',
  反共识结论: '反共识结论',
  逻辑自检: '逻辑自检',
  总结: '总结',
}

const STAGE_KEYS: ReadonlyArray<StageKey> = FIXED_STAGE_KEYS

// 配色阈值: 偏离基线 ±0.15 = 容忍带(中性),外侧按严重度递增
const WARN_THRESHOLD = 0.15

/** 偏离度 → 颜色 class */
function deltaClass(delta: number, count: number): string {
  if (count === 0) return 'bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-600'
  if (delta >= WARN_THRESHOLD) {
    return delta >= WARN_THRESHOLD * 2
      ? 'bg-green-200 text-green-900 dark:bg-green-800/60 dark:text-green-100'
      : 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200'
  }
  if (delta < -WARN_THRESHOLD) {
    if (delta <= -WARN_THRESHOLD * 2) {
      return 'bg-red-200 text-red-900 dark:bg-red-800/60 dark:text-red-100'
    }
    return 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200'
  }
  return 'bg-gray-50 text-gray-700 dark:bg-gray-800/40 dark:text-gray-300'
}

/** 格式偏离度文字,如 "+12pp" / "-30pp" */
function formatDelta(delta: number): string {
  if (Math.abs(delta) < 0.005) return '±0'
  const pp = Math.round(Math.abs(delta) * 100)
  return delta > 0 ? `+${pp}pp` : `-${pp}pp`
}

export function SegmentHistoryHeatmap({
  days = 7,
  className,
}: SegmentHistoryHeatmapProps) {
  const { data, loading, error } = useSegmentHistory({ days, intervalMs: 5 * 60 * 1000 })

  const { baseline, items } = data

  // 用 useMemo 算每格的 (delta, count, colorClass, label) — 8*N 一次性算完
  const cells = useMemo(() => {
    return STAGE_KEYS.map((stage) => ({
      stage,
      cells: items.map((day: SegmentHistoryDay) => {
        const rate = day.stages[stage] ?? 0
        const base = baseline[stage] ?? 0
        const delta = rate - base
        return {
          date: day.date,
          count: day.count,
          rate,
          delta,
          className: deltaClass(delta, day.count),
          label: day.count === 0 ? '—' : formatDelta(delta),
        }
      }),
    }))
  }, [items, baseline])

  if (loading && items.length === 0) {
    return (
      <div data-testid="segment-history-heatmap" className={cn('flex items-center gap-2 text-sm text-muted-foreground', className)}>
        <Loader2 className="h-4 w-4 animate-spin" />
        加载段位快照…
      </div>
    )
  }

  if (error && items.length === 0) {
    return (
      <div data-testid="segment-history-heatmap" className={cn('text-sm text-red-600', className)}>
        段位快照加载失败
      </div>
    )
  }

  if (items.length === 0) {
    return null
  }

  return (
    <div
      data-testid="segment-history-heatmap"
      className={cn('flex flex-col gap-2', className)}
    >
      <div className="text-xs font-medium text-muted-foreground">
        段位一致性快照(过去 {days} 天)
      </div>
      <div
        className="grid gap-0.5 text-[10px]"
        style={{
          gridTemplateColumns: `minmax(60px,auto) repeat(${items.length}, minmax(36px,1fr))`,
        }}
      >
        {/* header: 日期 */}
        <div />
        {items.map((it) => (
          <div key={it.date} className="text-center text-muted-foreground" title={it.date}>
            {it.date.slice(5)}  {/* MM-DD */}
          </div>
        ))}
        {/* body: 每行 = 1 个段位 */}
        {cells.map((row) => (
          <FragmentRow
            key={row.stage}
            stage={row.stage}
            cells={row.cells}
          />
        ))}
      </div>
      <div className="text-[10px] text-muted-foreground/70">
        颜色 = 当日命中率 − 时段基线(±15pp 内中性;绿=超越基线,红=落后)
        {baseline['总框架'] !== undefined && (
          <span className="ml-2">基线样本: {items.reduce((s, it) => s + it.count, 0)} 条 / {items.length} 天</span>
        )}
      </div>
    </div>
  )
}

function FragmentRow({
  stage,
  cells,
}: {
  stage: StageKey
  cells: Array<{ date: string; count: number; rate: number; delta: number; className: string; label: string }>
}) {
  return (
    <>
      <div className="flex items-center pr-1 text-right text-muted-foreground">
        {STAGE_LABELS[stage]}
      </div>
      {cells.map((c) => (
        <div
          key={`${stage}-${c.date}`}
          data-testid={`heatmap-cell-${stage}-${c.date}`}
          data-stage={stage}
          data-date={c.date}
          data-delta={c.delta.toFixed(3)}
          data-count={c.count}
          title={`${stage} · ${c.date}\n命中率 ${(c.rate * 100).toFixed(0)}% · 当日 ${c.count} 条\n偏离基线 ${c.label}`}
          className={cn(
            'flex items-center justify-center rounded-sm py-1 font-mono',
            c.className,
          )}
        >
          {c.label}
        </div>
      ))}
    </>
  )
}
