'use client'

import { useMemo, useState } from 'react'
import { TrendingUp, TrendingDown, AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { cn } from '@/lib/utils'

/* ════════════════════════════════════════════════════════════════════════
 * 🌐 跨资产全网涨跌排行(v227 P1-3 · 接入后端 /discovery/cross-asset-rankings)
 *
 * 后端按时间窗聚合 external_indicators,返回:
 *   gainers 涨幅前列 / losers 跌幅前列 / crashes 严重下跌(≤ 阈值) /
 *   category_breakdown 分品类平均涨跌。
 * 失败走空状态,不造数。
 * ════════════════════════════════════════════════════════════════════════ */

type RankItem = {
  symbol: string
  category: string
  latest: number
  earliest: number
  change_pct: number
  ts: number
  is_crash: boolean
}

type RankResp = {
  ok: boolean
  timeframe: string
  total_count: number
  gainers: RankItem[]
  losers: RankItem[]
  crashes: RankItem[]
  crash_threshold_pct: number
  category_breakdown: Record<string, { count: number; avg_change_pct: number }>
  method: string
  note?: string
}

const TIMEFRAMES = ['1h', '24h', '7d', '30d'] as const

const CATEGORY_LABEL: Record<string, string> = {
  memory_chip: '存储芯片',
  altcoin: '加密',
  safe_metal: '贵金属',
  energy: '能源',
  bond: '债券',
  fx: '外汇',
  other: '其它',
}

function pctCls(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'text-muted-foreground'
  return v > 0 ? 'text-emerald-600 dark:text-emerald-400' : v < 0 ? 'text-rose-600 dark:text-rose-400' : 'text-foreground'
}

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

export function CrossAssetRankingsPanel() {
  const [tf, setTf] = useState<(typeof TIMEFRAMES)[number]>('24h')

  const { data, loading, refresh } = usePoll<RankResp>(
    `/discovery/cross-asset-rankings?timeframe=${tf}&limit=15`,
    {
      intervalMs: 10 * 60_000,
      deps: [tf],
      transform: (raw) => {
        const d = raw as RankResp | null
        if (!d || typeof d === 'object' && 'ok' in d && (d as RankResp).ok === false) return null
        return d as RankResp
      },
    },
  )

  const gainers = useMemo(() => data?.gainers ?? [], [data])
  const losers = useMemo(() => data?.losers ?? [], [data])
  const crashes = useMemo(() => data?.crashes ?? [], [data])
  const breakdown = Object.entries(data?.category_breakdown ?? {})

  return (
    <div className="rounded border border-rose-500/30 bg-white p-3 text-sm dark:bg-slate-900">
      <div className="flex flex-wrap items-center gap-2 font-mono text-xs">
        <TrendingUp className="h-4 w-4 text-rose-500" aria-hidden />
        <span className="font-semibold">🌐 跨资产涨跌排行</span>
        <div className="ml-auto flex flex-wrap items-center gap-1">
          {TIMEFRAMES.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTf(t)}
              className={cn(
                'rounded border px-1.5 py-0.5 text-[11px] font-mono',
                tf === t
                  ? 'border-primary bg-primary/10 text-primary'
                  : 'border-border bg-secondary/30 text-muted-foreground hover:text-foreground',
              )}
            >
              {t}
            </button>
          ))}
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-0.5 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-secondary/50 disabled:opacity-40"
          >
            <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
          </button>
        </div>
      </div>

      {data?.note && (
        <p className="mt-1.5 text-[11px] text-muted-foreground">{data.note}</p>
      )}

      {/* 分品类平均涨跌 */}
      {breakdown.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {breakdown.map(([cat, stat]) => (
            <span key={cat} className="rounded border border-border bg-secondary/20 px-1.5 py-0.5 font-mono text-[11px]">
              {CATEGORY_LABEL[cat] ?? cat} ×{stat.count}
              <span className={cn('ml-1', pctCls(stat.avg_change_pct))}>
                {fmtPct(stat.avg_change_pct)}
              </span>
            </span>
          ))}
        </div>
      )}

      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
        <RankColumn
          title="📈 涨幅前列"
          icon={<TrendingUp className="h-3 w-3 text-emerald-500" />}
          rows={gainers}
          empty="暂无上涨标的"
        />
        <RankColumn
          title="📉 跌幅前列"
          icon={<TrendingDown className="h-3 w-3 text-rose-500" />}
          rows={losers}
          empty="暂无下跌标的"
        />
        <RankColumn
          title={`🚨 严重下跌 ≤ ${data?.crash_threshold_pct ?? -10}%`}
          icon={<AlertTriangle className="h-3 w-3 text-amber-500" />}
          rows={crashes}
          empty="无严重下跌"
          crash
        />
      </div>

      {loading && !data && (
        <div className="mt-2 flex items-center gap-1 text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 扫描中…
        </div>
      )}
    </div>
  )
}

function RankColumn({
  title,
  icon,
  rows,
  empty,
  crash = false,
}: {
  title: string
  icon: React.ReactNode
  rows: RankItem[]
  empty: string
  crash?: boolean
}) {
  return (
    <div className="rounded border border-border bg-secondary/10 p-2">
      <div className="flex items-center gap-1 font-mono text-[11px] font-bold text-foreground">
        {icon}
        {title}
        <span className="ml-auto text-muted-foreground/70">×{rows.length}</span>
      </div>
      <div className="mt-1 space-y-0.5">
        {rows.length === 0 && <div className="text-[11px] text-muted-foreground">{empty}</div>}
        {rows.slice(0, 10).map((r) => (
          <div key={r.symbol} className="flex items-center gap-1.5 font-mono text-[11px]">
            <span className="font-bold">{r.symbol}</span>
            <span className="text-muted-foreground/70">{CATEGORY_LABEL[r.category] ?? r.category}</span>
            <span className={cn('ml-auto tabular-nums', pctCls(r.change_pct), crash && 'font-bold')}>
              {fmtPct(r.change_pct)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
