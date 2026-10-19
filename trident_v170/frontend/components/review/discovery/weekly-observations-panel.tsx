'use client'

import { Calendar, ExternalLink, Radio, Loader2 } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { PushToCausalChainButton } from './push-to-causal-chain-button'
import { cn } from '@/lib/utils'

/* ════════════════════════════════════════════════════════════════════════
 * 📅 周度观察面板 — v2026-10-08 接通后端 /api/discovery/weekly-watch
 *
 * 数据源:
 *   - ICSA: FRED SERIES 自动 ingest (fred_feed.py:43)
 *   - Treasury auction: 暂未 ingest, 占位 status='pending'
 *
 * 视觉: 卡片栅格 + 数据源徽章 + 「每周节奏」一览表。
 * ════════════════════════════════════════════════════════════════════════ */

type WeeklyItem = {
  key: string
  title: string
  source: string
  release_day_cn: string
  meaning: string
  hint: string
  link?: string
  status: 'pending' | 'live' | 'error'
  reason?: string
  error?: string
  latest?: number
  previous?: number
  delta?: number
  ma4?: number | null
  ts?: number
  source_detail?: string
}

type WeeklyWatchResponse = {
  ok: boolean
  ts: number
  items: WeeklyItem[]
  note?: string
}

const ICONS: Record<string, string> = {
  icsa: '🧾',
  auction: '🏛️',
}

export function WeeklyObservationsPanel() {
  // v283+ · Task D · 6h 轮询(数据源 ICSA 是 FRED 周更) · AbortController 交给 usePoll
  const { data, loading, error: err } = usePoll<WeeklyItem[]>(
    '/api/discovery/weekly-watch',
    {
      intervalMs: 6 * 60 * 60 * 1000,
      transform: (raw) => {
        const d = raw as WeeklyWatchResponse | null | undefined
        if (!d || !d.ok) return null
        return d.items || []
      },
    },
  )
  // 失败时保证 data 至少是空数组(原本 setData([]) 语义)
  const items: WeeklyItem[] = data ?? []

  const liveCount = items.filter((d) => d.status === 'live').length
  const pendingCount = items.filter((d) => d.status === 'pending').length

  return (
    <div className="rounded border border-violet-500/40 bg-violet-500/5 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs">
        <div className="flex items-center gap-2">
          <Calendar className="h-4 w-4 text-violet-500" aria-hidden />
          <span className="font-bold uppercase tracking-widest text-violet-700 dark:text-violet-300">
            📅 周度观察 · 中间校验节奏
          </span>
          <span className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 text-[9px] font-bold text-violet-600 dark:text-violet-400">
            每周刷新 · 不与日/季度混用
          </span>
        </div>
        <div className="flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
          {loading && <Loader2 className="h-3 w-3 animate-spin text-violet-500" />}
          <Radio className="h-3 w-3 text-violet-500" />
          {err ? (
            <span className="text-rose-500">⚠ {err}</span>
          ) : (
            <span>🟢 live × {liveCount} · ⏳ pending × {pendingCount}</span>
          )}
        </div>
      </div>




      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
        {items.map((r) => (
          <div key={r.key} className="rounded border border-border bg-background/40 p-2.5 font-mono">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-1.5">
                <span className="text-base" aria-hidden>{ICONS[r.key] || '📊'}</span>
                <div>
                  <div className="text-xs font-bold">{r.title}</div>
                  <div className="text-[9px] text-muted-foreground">{r.release_day_cn}</div>
                </div>
              </div>
              <span className={cn(
                'rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider',
                r.status === 'live'
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                  : r.status === 'error'
                    ? 'border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400'
                    : 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
              )}>
                {r.status === 'live' ? '🟢 LIVE' : r.status === 'error' ? '❌ ERROR' : '⏳ 待接入'}
              </span>
            </div>

            {r.status === 'live' && r.latest !== undefined && (
              <div className="mt-1.5 grid grid-cols-3 gap-1.5 text-[10px]">
                <Stat label="最新" value={r.latest.toLocaleString()} />
                <Stat
                  label="Δ"
                  value={(r.delta ?? 0) > 0 ? `+${r.delta}` : `${r.delta}`}
                  tone={(r.delta ?? 0) > 0 ? 'up' : (r.delta ?? 0) < 0 ? 'down' : 'neutral'}
                />
                <Stat label="MA4" value={r.ma4 != null ? r.ma4.toLocaleString() : '—'} />
              </div>
            )}

            {(r.status === 'pending' || r.status === 'error') && (r.reason || r.error) && (
              <div className="mt-1.5 rounded border border-amber-500/20 bg-amber-500/5 p-1.5 text-[10px] text-amber-700 dark:text-amber-300">
                ⏳ {r.reason || r.error}
              </div>
            )}

            <div className="mt-1.5 text-[10px] text-foreground/80">
              <strong>作用:</strong> {r.meaning}
            </div>
            <div className="mt-1 text-[10px] text-muted-foreground">
              <strong>解读:</strong> {r.hint}
            </div>
            <div className="mt-1.5 flex items-center gap-2 text-[9px] text-muted-foreground">
              <span>来源:</span>
              <span className="rounded bg-secondary/40 px-1 py-0.5">{r.source}</span>
              {r.link && (
                <a
                  href={r.link}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-0.5 text-cyan-600 hover:underline dark:text-cyan-400"
                >
                  <ExternalLink className="h-2.5 w-2.5" />
                  原始链接
                </a>
              )}
              {/* v283+ · #23/#31 · 周度数据点 → 资产,投到因果链候选池 */}
              <span className="ml-auto">
                <PushToCausalChainButton origin="manual_from_weekly_obs"

                  cause={`weekly-obs · ${r.title}${r.latest != null ? ` = ${r.latest}` : ''}`}
                  effect={(r.meaning.split(/[→+]/)[0] || r.source || '宏观').trim()}
                  note={`r.status=${r.status} delta=${r.delta ?? 'n/a'} ma4=${r.ma4 ?? 'n/a'} source=${r.source} meaning=${r.meaning}`}
                  trigger_words={[r.key, r.title, r.source].join(',')}
                  weight={0.4}
                />
              </span>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-2 rounded border border-violet-500/20 bg-violet-500/5 p-1.5 text-[10px] text-muted-foreground">
        💡 周度数据是<strong className="text-foreground">「月-日-周-季度」校验链的中间层</strong>。
        月度数据发布前看周度(初请/拍卖)校验方向 — 例: 非农发布前一周,初请走高
        + 拍卖 tail → 即使非农超预期,长债/风险资产也可能反向定价。
      </div>
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'up' | 'down' | 'neutral' }) {
  const cls = tone === 'up'
    ? 'text-rose-500'
    : tone === 'down'
      ? 'text-emerald-500'
      : 'text-foreground'
  return (
    <div className="rounded border border-border bg-background/60 px-1.5 py-1">
      <div className="text-[8px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn('text-xs font-bold tabular-nums', cls)}>{value}</div>
    </div>
  )
}

