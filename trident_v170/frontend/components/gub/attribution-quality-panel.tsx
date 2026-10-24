'use client'

/**
 * AttributionQualityPanel — 归因质量体检
 *
 * 核心价值: event_move_attribution 有 392 行, 但实测只有 65 行 (16.6%) 是真正的
 * 「新闻在前、行情在后」。其余是 spillover (同组资产互推)、macro_fallback (无新闻硬凑)、
 * retro (新闻滞后 100+ 小时的事后拟合)。
 *
 * 这个面板的作用 —— 在用户把归因当真相源之前, 先如实告诉他数据成色,
 * 避免伪因果被当成真关联固化进 causal_chains。
 */

import { useMemo, useState } from 'react'
import { useApiPoll } from '@/lib/hooks/use-api-poll'
import {
  attributionTier,
  chainOverconfidence,
  summarizeAttribution,
  type AttributionResponse,
  type ChainFeedbackResponse,
} from '@/lib/api-gub'
import { cn } from '@/lib/utils'

const POLL_MS = 120_000

type Tier = 'causal' | 'derived' | 'retro'

const TIER_META: Record<Tier, { label: string; hint: string; tone: string }> = {
  causal: {
    label: '真因果',
    hint: '新闻在前、行情在后，且确有新闻实体 —— 唯一可用于因果推断的一类',
    tone: 'text-long',
  },
  derived: {
    label: '推导',
    hint: 'spillover 同组资产互推 / macro_fallback 无新闻硬凑 —— 不是新闻直接驱动',
    tone: 'text-hold',
  },
  retro: {
    label: '事后拟合',
    hint: '新闻滞后行情 100+ 小时才被匹配上 —— 属于结果解释，不是预测',
    tone: 'text-short',
  },
}

export function AttributionQualityPanel({ className }: { className?: string } = {}) {
  const [filter, setFilter] = useState<Tier | 'all'>('all')

  const { data, error, loading } = useApiPoll<AttributionResponse>(
    '/v291/event-move-attribution?limit=200',
    { intervalMs: POLL_MS },
  )
  const { data: fb } = useApiPoll<ChainFeedbackResponse>('/v291/causal-chains/feedback?days=90', {
    intervalMs: POLL_MS,
  })

  const rows = useMemo(() => data?.items ?? [], [data])
  const summary = useMemo(() => summarizeAttribution(rows), [rows])
  const shown = useMemo(
    () => (filter === 'all' ? rows : rows.filter(r => attributionTier(r) === filter)),
    [rows, filter],
  )
  const overconfident = useMemo(
    () => (fb?.chains ?? []).filter(c => c.needs_recalibration || (chainOverconfidence(c) ?? 0) > 0.2),
    [fb],
  )

  return (
    <section className={cn('rounded-lg border border-border bg-card p-3', className)}>
      <header className="mb-2 flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">归因质量体检</h3>
        {data && (
          <span className="font-mono text-[10px] text-muted-foreground">
            抽样 {summary.total} / 全库 {data.rows_total}
          </span>
        )}
      </header>

      {error && (
        <div className="mb-2 rounded border border-border bg-muted/40 px-2 py-1.5 font-mono text-[10px] text-muted-foreground">
          {error}
        </div>
      )}

      {loading && !data && (
        <div className="py-3 text-center font-mono text-[10px] text-muted-foreground">加载中…</div>
      )}

      {data && summary.total > 0 && (
        <>
          <div className="mb-2">
            <div className="mb-1 flex items-baseline justify-between">
              <span className="text-[10px] text-muted-foreground">真因果占比</span>
              <span className={cn('font-mono text-sm font-semibold', summary.causalPct < 30 ? 'text-short' : 'text-long')}>
                {summary.causalPct.toFixed(1)}%
              </span>
            </div>
            <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
              <div className="bg-long" style={{ width: `${(summary.causal / summary.total) * 100}%` }} />
              <div className="bg-hold" style={{ width: `${(summary.derived / summary.total) * 100}%` }} />
              <div className="bg-short" style={{ width: `${(summary.retro / summary.total) * 100}%` }} />
            </div>
          </div>

          <div className="mb-2 flex flex-wrap gap-1.5">
            {(['all', 'causal', 'derived', 'retro'] as const).map(k => {
              const active = filter === k
              const n = k === 'all' ? summary.total
                : k === 'causal' ? summary.causal
                : k === 'derived' ? summary.derived
                : summary.retro
              return (
                <button
                  key={k}
                  onClick={() => setFilter(k)}
                  className={cn(
                    'rounded border px-2 py-0.5 font-mono text-[10px] transition-colors',
                    active ? 'border-primary bg-primary/10 text-foreground' : 'border-border text-muted-foreground hover:bg-muted/50',
                  )}
                >
                  {k === 'all' ? '全部' : TIER_META[k].label} {n}
                </button>
              )
            })}
          </div>

          {filter !== 'all' && (
            <p className="mb-1.5 text-[10px] leading-relaxed text-muted-foreground">
              {TIER_META[filter].hint}
            </p>
          )}
          {/* __DETAIL_LIST__ */}
          <ul className="max-h-64 space-y-1 overflow-y-auto">
            {shown.slice(0, 40).map(r => {
              const tier = attributionTier(r)
              return (
                <li key={r.id} className="rounded border border-border bg-background/40 px-2 py-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5">
                      <span className="font-mono text-[11px] font-semibold text-foreground">{r.asset}</span>
                      <span className="font-mono text-[10px] text-muted-foreground">{r.move_kind}</span>
                      <span className={cn('rounded border px-1 py-0.5 font-mono text-[9px]',
                        tier === 'causal' ? 'border-long/40 text-long'
                        : tier === 'retro' ? 'border-short/40 text-short'
                        : 'border-border text-hold')}>
                        {TIER_META[tier].label}
                      </span>
                    </span>
                    <span className={cn('font-mono text-[11px]', r.move_pct >= 0 ? 'text-long' : 'text-short')}>
                      {r.move_pct >= 0 ? '+' : ''}{r.move_pct.toFixed(2)}%
                    </span>
                  </div>
                  <div className="mt-0.5 font-mono text-[9px] text-muted-foreground">
                    lag {(r.time_lag_sec / 3600).toFixed(1)}h · score {r.algo_score.toFixed(3)} · {r.algo_reasons}
                  </div>
                </li>
              )
            })}
          </ul>
          {shown.length > 40 && (
            <p className="mt-1 text-center font-mono text-[9px] text-muted-foreground">
              仅显示前 40 条，共 {shown.length} 条
            </p>
          )}

          {overconfident.length > 0 && (
            <div className="mt-2 rounded border border-destructive/40 bg-destructive/5 p-2">
              <div className="mb-1 text-[10px] font-medium text-foreground">
                ⚠ {overconfident.length} 条因果链宣称置信度高于实测命中率
              </div>
              <ul className="space-y-0.5">
                {overconfident.slice(0, 5).map(c => (
                  <li key={c.chain_id} className="font-mono text-[9px] text-muted-foreground">
                    {c.chain_id} · 宣称 {c.stated_confidence.toFixed(2)} / 实测{' '}
                    {c.real_hit_rate != null ? `${(c.real_hit_rate * 100).toFixed(0)}%` : '样本不足'}
                    {c.outcomes_n < 3 && ' (n<3)'}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  )
}

export default AttributionQualityPanel