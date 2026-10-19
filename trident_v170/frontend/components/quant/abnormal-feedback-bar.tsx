"use client"

import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

interface FeedbackStats {
  forward_precision: number
  reverse_recall: number
  missed_count: number
  pass_block_rate: number
  total_markers?: number
  lookback_bars?: number
  asset?: string
  interval?: string
  error?: string
}

/**
 * v221.5 异动检测反馈条 — 4 个量化指标的紧凑展示。
 *
 * 数据来源：`GET /api/abnormal/feedback-stats?asset=BTC&interval=10m`
 *
 * 设计原则：
 *   - read-only 展示（用户不需操作）
 *   - 4 个数字颜色编码：绿 ≥70% / 黄 50-70% / 红 <50%
 *   - 漏检数独立颜色（红 = 大）
 *   - 加载/错误态用极简占位符
 *
 * 用法：放在 chart 上方或 legend 旁作为「模型质量速览」。
 */
export function AbnormalFeedbackBar({
  asset = 'BTC',
  interval = '10m',
  className,
}: {
  asset?: string
  interval?: string
  className?: string
}) {
  const [stats, setStats] = useState<FeedbackStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setErr(null)
    fetch(
      `/api/abnormal/feedback-stats?asset=${encodeURIComponent(asset)}&interval=${interval}`,
      { cache: 'no-store' },
    )
      .then((r) => r.json())
      .then((d: FeedbackStats) => {
        if (cancelled) return
        setStats(d)
        if (d.error) setErr(d.error)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setErr(e instanceof Error ? e.message : 'fetch failed')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [asset, interval])

  const colorFor = (pct: number): string => {
    if (pct >= 70) return 'text-emerald-400'
    if (pct >= 50) return 'text-amber-400'
    return 'text-rose-400'
  }

  return (
    <div
      data-testid="abnormal-feedback-bar"
      className={cn(
        'flex items-center gap-4 rounded-md border border-border/50 bg-card/40 px-3 py-2 text-xs',
        className,
      )}
    >
      {loading ? (
        <span className="flex items-center gap-1 text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          加载反馈…
        </span>
      ) : err ? (
        <span className="text-rose-400" title={err}>
          反馈加载失败
        </span>
      ) : stats ? (
        <>
          <Metric
            label="前瞻命中"
            value={`${stats.forward_precision.toFixed(1)}%`}
            className={colorFor(stats.forward_precision)}
            tip="异动 markers 中，前向窗口内继续走出 ≥0.6% 行情的比例"
          />
          <Metric
            label="反转召回"
            value={`${stats.reverse_recall.toFixed(1)}%`}
            className={colorFor(stats.reverse_recall)}
            tip="近 lookback 内 |pct| ≥0.6% 的 bars 中被检测到的比例"
          />
          <Metric
            label="漏检数"
            value={`${stats.missed_count}`}
            className={stats.missed_count === 0 ? 'text-emerald-400' : 'text-rose-400'}
            tip="lookback 内 |pct| ≥0.6% 但未触发异动检测的 bars 数"
          />
          <Metric
            label="通过率"
            value={`${stats.pass_block_rate.toFixed(1)}%`}
            className={colorFor(stats.pass_block_rate)}
            tip="v221.6 计划区分 TP/FP/FN；当前与前瞻命中同义"
          />
          {stats.total_markers !== undefined && (
            <span className="ml-auto text-muted-foreground">
              {stats.total_markers} markers / {stats.lookback_bars} bars
            </span>
          )}
        </>
      ) : null}
    </div>
  )
}

function Metric({
  label,
  value,
  className,
  tip,
}: {
  label: string
  value: string
  className?: string
  tip?: string
}) {
  return (
    <div className="flex flex-col leading-tight" title={tip}>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className={cn('font-mono text-sm font-medium', className)}>{value}</span>
    </div>
  )
}