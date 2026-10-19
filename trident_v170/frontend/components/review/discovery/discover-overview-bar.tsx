'use client'

import { useCallback, useState } from 'react'
import { Activity, AlertTriangle, GitBranch, Sparkles, Loader2 } from 'lucide-react'
import { API_BASE, apiGet } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'
import { cn } from '@/lib/utils'

type Pillar = {
  key: string
  label: string
  last_update: string | null
  count_24h: number
  status: 'fresh' | 'stale' | 'empty'
}

type LoopStats = {
  scanned_24h: number
  into_chains_24h: number
  into_graph_24h: number
  last_synced_at: string | null
}

type Summary = {
  ok: boolean
  period: string
  generated_at: string
  cards: {
    scanner_hits: { total: number; top_3: Array<{ asset?: string; count?: number }> }
    event_alerts: { total: number; top_3: Array<{ asset?: string; action?: string; score?: number }> }
    cycle_signals: { total_24h: number; current_phase: string; top_3: Array<{ asset?: string; direction?: string; score?: number }> }
    causal_pending: { pending: number; approved: number; applied_today: number; suggested_action: string }
  }
  pillars: Pillar[]
  loop_stats: LoopStats
}

const EMPTY_SUMMARY: Summary = {
  ok: false,
  period: '24h',
  generated_at: '',
  cards: {
    scanner_hits: { total: 0, top_3: [] },
    event_alerts: { total: 0, top_3: [] },
    cycle_signals: { total_24h: 0, current_phase: '', top_3: [] },
    causal_pending: { pending: 0, approved: 0, applied_today: 0, suggested_action: '暂无待审批' },
  },
  pillars: [],
  loop_stats: { scanned_24h: 0, into_chains_24h: 0, into_graph_24h: 0, last_synced_at: null },
}

function cardCls(tone?: 'ok' | 'warn' | 'neutral'): string {
  if (tone === 'warn') return 'border-amber-500/40 bg-amber-500/5'
  if (tone === 'neutral') return 'border-border bg-secondary/20'
  return 'border-primary/30 bg-primary/5'
}

function dotCls(s: Pillar['status']): string {
  if (s === 'fresh') return 'bg-emerald-500'
  if (s === 'stale') return 'bg-amber-500'
  return 'bg-border'
}

export function DiscoverOverviewBar() {
  const [summary, setSummary] = useState<Summary>(EMPTY_SUMMARY)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(async () => {
    try {
      // lib/api.ts 统一拦截 _bridge_fallback 抛 BRIDGE_FALLBACK, catch 后保持默认
      const data = await apiGet<Summary>('/discovery/summary?period=24h')
      setSummary(data)
    } catch {
      /* 端点未实现/网络错 — 保持 EMPTY_SUMMARY 默认 */
    } finally {
      setLoading(false)
    }
  }, [])

  usePollRefresh(reload, 60_000)

  const sc = summary.cards.scanner_hits
  const ea = summary.cards.event_alerts
  const cy = summary.cards.cycle_signals
  const cp = summary.cards.causal_pending
  const ls = summary.loop_stats

  return (
    <section aria-label="discover 顶部概览" className="rounded border border-primary/40 bg-primary/5 p-2.5">
      <header className="mb-1.5 flex items-center gap-2 font-mono">
        <Sparkles className="h-3.5 w-3.5 text-primary" />
        <h3 className="text-[11px] font-bold uppercase tracking-widest text-foreground">发现页概览 · 24h 闭环</h3>
        {loading && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
        <span className="ml-auto text-[10px] text-muted-foreground">
          {summary.generated_at ? '更新于 ' + summary.generated_at.slice(11, 19) : '加载中…'}
        </span>
      </header>

      <div className="grid grid-cols-2 gap-1.5 md:grid-cols-4">
        <div className={cn('rounded border px-2.5 py-1.5 font-mono', cardCls())}>
          <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            <Activity className="h-3 w-3" />
            资产猎手命中
          </div>
          <div className="mt-0.5 text-base font-bold text-foreground">{sc.total ?? 0}</div>
          <div className="mt-0.5 space-y-0.5 text-[10px] text-muted-foreground">
            {sc.top_3 && sc.top_3.length > 0 ? (
              sc.top_3.map((t, i) => (
                <div key={i} className="truncate">
                  · {t.asset || '?'} <span className="text-muted-foreground/70">×{t.count ?? ''}</span>
                </div>
              ))
            ) : (
              <div>暂无材质新闻</div>
            )}
          </div>
        </div>

        <div className={cn('rounded border px-2.5 py-1.5 font-mono', cardCls())}>
          <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            <AlertTriangle className="h-3 w-3" />
            事件雷达告警
          </div>
          <div className="mt-0.5 text-base font-bold text-foreground">{ea.total ?? 0}</div>
          <div className="mt-0.5 space-y-0.5 text-[10px] text-muted-foreground">
            {ea.top_3 && ea.top_3.length > 0 ? (
              ea.top_3.map((t, i) => (
                <div key={i} className="truncate">
                  · {t.asset || '?'} {t.action === 'BUY' ? '↑' : t.action === 'SELL' ? '↓' : '·'}
                  <span className="ml-1 text-muted-foreground/70">{((t.score ?? 0) * 100).toFixed(0)}%</span>
                </div>
              ))
            ) : (
              <div>暂无高分事件</div>
            )}
          </div>
        </div>
      </div>
<div className="grid grid-cols-2 gap-1.5 md:grid-cols-4">
        <div className={cn('rounded border px-2.5 py-1.5 font-mono', cardCls(cy.current_phase === 'markdown' ? 'warn' : 'ok'))}>
          <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            <GitBranch className="h-3 w-3" />
            周期相位
          </div>
          <div
            className={cn(
              'mt-0.5 text-base font-bold',
              cy.current_phase === 'markdown' ? 'text-amber-600 dark:text-amber-400' : 'text-foreground',
            )}
          >
            {cy.current_phase || '—'}
          </div>
          <div className="mt-0.5 space-y-0.5 text-[10px] text-muted-foreground">
            <div>24h 命中: {cy.total_24h ?? 0}</div>
            {cy.top_3 && cy.top_3.length > 0 && (
              <div className="truncate">
                · {cy.top_3[0].asset} {cy.top_3[0].direction === 'down' ? '↓' : '↑'}
                <span className="text-muted-foreground/70"> {((cy.top_3[0].score ?? 0) * 100).toFixed(0)}%</span>
              </div>
            )}
          </div>
        </div>

        <div className={cn('rounded border px-2.5 py-1.5 font-mono', cardCls((cp.pending ?? 0) > 0 ? 'warn' : 'ok'))}>
          <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            <Sparkles className="h-3 w-3" />
            因果待审批
          </div>
          <div className="mt-0.5 text-base font-bold text-foreground">{cp.pending ?? 0}</div>
          <div className="mt-0.5 space-y-0.5 text-[10px] text-muted-foreground">
            <div>今日已应用: {cp.applied_today ?? 0}</div>
            <div className="truncate">{cp.suggested_action}</div>
          </div>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-primary/20 font-mono text-[10px] text-muted-foreground">
        <span>闭环 24h:</span>
        <span>扫描 <b className="text-foreground">{ls.scanned_24h}</b></span>
        <span>→ 入链 <b className="text-foreground">{ls.into_chains_24h}</b></span>
        <span>→ 入图 <b className="text-foreground">{ls.into_graph_24h}</b></span>
        {ls.last_synced_at && (
          <span className="ml-auto">最近一次图同步: {ls.last_synced_at}</span>
        )}
      </div>

      {summary.pillars.length > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-muted-foreground">
          {summary.pillars.map((p) => (
            <span key={p.key} className="flex items-center gap-1">
              <span className={cn('inline-block h-1.5 w-1.5 rounded-full', dotCls(p.status))} aria-hidden />
              {p.label} <span className="text-muted-foreground/70">{p.count_24h}</span>
            </span>
          ))}
        </div>
      )}
    </section>
  )
}