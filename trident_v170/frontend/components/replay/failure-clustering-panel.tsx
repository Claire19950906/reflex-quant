'use client'

/**
 * FailureClusteringPanel — 失败模式聚类面板 (v302+ P2 PR-17)。
 *
 * 解决"决策错误码"面板只能看到扁平计数的痛点:
 *  - 按 error_code 聚合(已 cluster)
 *  - 排序: count desc
 *  - 每行: code 徽章 + severity 颜色 + count + % + 趋势 + top assets + examples
 *  - 加载 / 错误 / 空 三态
 *
 * 视觉: 紧凑行布局,同 FailureStatsPanel 风格(圆角边框 / 10px 字体 / 等宽)
 *  trend ↑ 红色 / ↓ 绿色 / → 灰
 */

import { ChevronDown, ChevronUp, Minus, AlertTriangle, Loader2, Layers } from 'lucide-react'
import { useReflectionClustering, type FailurePattern } from '@/lib/hooks/use-reflection-clustering'
import { cn } from '@/lib/utils'

export interface FailureClusteringPanelProps {
  days?: number
  min_count?: number
  /** 渲染前 N 个模式(默认 5,后端已按 count desc 排序) */
  topN?: number
  className?: string
  /** 点击 example decision_id 时回调 */
  onSelectDecision?: (decisionId: number) => void
}

function severityTone(sev: number): string {
  if (sev >= 2.5) return 'bg-red-100 text-red-700 border-red-300'
  if (sev >= 1.5) return 'bg-amber-100 text-amber-700 border-amber-300'
  return 'bg-emerald-100 text-emerald-700 border-emerald-300'
}

function severityLabel(sev: number): string {
  if (sev >= 2.5) return 'CRIT'
  if (sev >= 1.5) return 'WARN'
  return 'INFO'
}

function trendDisplay(trend: 'up' | 'down' | 'flat', delta: number) {
  if (trend === 'up') {
    return {
      Icon: ChevronUp,
      label: delta >= 1 ? '↑ 新模式' : `↑ +${Math.round(delta * 100)}%`,
      cls: 'text-red-600 bg-red-50 border-red-200',
    }
  }
  if (trend === 'down') {
    return {
      Icon: ChevronDown,
      label: `↓ ${Math.round(delta * 100)}%`,
      cls: 'text-emerald-700 bg-emerald-50 border-emerald-200',
    }
  }
  return {
    Icon: Minus,
    label: '→ 平稳',
    cls: 'text-slate-600 bg-slate-50 border-slate-200',
  }
}

function PatternRow({
  p,
  onSelect,
}: {
  p: FailurePattern
  onSelect?: (id: number) => void
}) {
  const sev = severityTone(p.severity_avg)
  const trend = trendDisplay(p.trend, p.trend_delta)
  const TrendIcon = trend.Icon
  return (
    <li
      data-testid="clustering-row"
      data-code={p.code}
      className="rounded border border-border bg-background/40 px-2 py-1.5"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className={cn('rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold', sev)}>
          {p.code}
        </span>
        <span className="font-mono text-[9px] uppercase text-muted-foreground">
          {severityLabel(p.severity_avg)} · {p.severity_avg.toFixed(1)}
        </span>
        <span className="font-mono text-[11px] tabular-nums">
          <span className="font-bold">{p.count}</span>
          <span className="ml-1 text-muted-foreground">({(p.pct_of_total * 100).toFixed(1)}%)</span>
        </span>
        <span
          data-testid="trend-badge"
          className={cn('inline-flex items-center gap-0.5 rounded border px-1.5 py-0.5 font-mono text-[10px]', trend.cls)}
        >
          <TrendIcon className="h-3 w-3" />
          {trend.label}
        </span>
        <span className="font-mono text-[9px] text-muted-foreground">
          7d={p.last_7d} / prior={p.prior_7d}
        </span>
        {p.top_assets.length > 0 && (
          <span className="flex flex-wrap items-center gap-1">
            {p.top_assets.map(a => (
              <span
                key={a.asset}
                className="rounded border border-primary/30 bg-primary/5 px-1 py-0.5 font-mono text-[9px] text-primary"
              >
                {a.asset}×{a.count}
              </span>
            ))}
          </span>
        )}
      </div>
      {p.examples.length > 0 && (
        <ul className="mt-1 flex flex-wrap gap-1" data-testid="examples-list">
          {p.examples.map(ex => (
            <li key={ex.decision_id}>
              <button
                type="button"
                disabled={!onSelect}
                onClick={() => onSelect?.(ex.decision_id)}
                className={cn(
                  'rounded border px-1.5 py-0.5 font-mono text-[9px]',
                  onSelect
                    ? 'border-chart-2/40 bg-chart-2/10 text-chart-2 hover:bg-chart-2/20'
                    : 'border-border bg-muted text-muted-foreground cursor-default',
                )}
                title={ex.detail}
              >
                #{ex.decision_id} · {ex.detail.slice(0, 30)}
                {ex.detail.length > 30 ? '…' : ''}
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

export function FailureClusteringPanel({
  days = 30,
  min_count = 2,
  topN = 5,
  className,
  onSelectDecision,
}: FailureClusteringPanelProps) {
  const { data, loading, error, refresh } = useReflectionClustering({
    days, min_count, limit_per_group: 3,
  })
  const summary = `${data.days} 天 · ${data.total_errors} 条错误 · ${data.total_decisions} 笔决策 · ${data.patterns.length} 个聚类`
  const top = data.patterns.slice(0, topN)

  return (
    <section
      data-testid="failure-clustering-panel"
      className={cn('rounded-lg border border-border bg-card/50 p-2.5', className)}
    >
      <header className="flex flex-wrap items-center gap-2">
        <Layers className="h-3.5 w-3.5 text-primary" />
        <h3 className="font-mono text-xs font-bold uppercase tracking-wider">
          失败模式聚类
        </h3>
        <span className="font-mono text-[10px] text-muted-foreground">{summary}</span>
        {loading && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
        {error && (
          <span className="inline-flex items-center gap-1 font-mono text-[10px] text-red-600">
            <AlertTriangle className="h-3 w-3" /> {error}
          </span>
        )}
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="ml-auto rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground disabled:opacity-40"
        >
          刷新
        </button>
      </header>

      {top.length === 0 ? (
        <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">
          {error ? '加载失败,暂无数据' : '窗口内无 ≥ min_count 次的失败模式'}
        </p>
      ) : (
        <ol className="mt-1.5 space-y-1" data-testid="patterns-list">
          {top.map(p => (
            <PatternRow key={p.code} p={p} onSelect={onSelectDecision} />
          ))}
        </ol>
      )}

      {data.patterns.length > topN && (
        <p className="mt-1.5 font-mono text-[9px] text-muted-foreground/70">
          展示前 {topN} / 共 {data.patterns.length} 个聚类(后端已按 count 排序)
        </p>
      )}
    </section>
  )
}
