'use client'

import { useMemo } from 'react'
import { Gem, Loader2, RefreshCw, ShieldCheck, ShieldAlert, HelpCircle } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { PushToCausalChainButton } from './push-to-causal-chain-button'
import { cn } from '@/lib/utils'

/* ════════════════════════════════════════════════════════════════════════
 * 💎 低估/超跌检测(v227 P1-2 · 接入后端 /discovery/undervalued-detector)
 *
 * 后端启发式:7d + 30d 跌幅 + 蓄力相位 + 反转概率 + 新闻热度,综合分 0-100;
 * 财务闸门:有财报且基本面恶化 → 分数压到 35 以下,明确标「便宜有原因」;
 * 无财报 → 标「基本面未核验」,不推断低估。
 * ════════════════════════════════════════════════════════════════════════ */

type UnderItem = {
  symbol: string
  category: string
  change_7d_pct: number
  change_30d_pct: number
  phase: string
  reversal_prob: number | null
  reversal_dir: string | null
  news_count_24h: number
  news_surge_pct: number
  fundamental_verified: boolean
  fundamental_ok: boolean | null
  fundamental_note: string
  undervalued_score: number
  reasons: string[]
  score_capped_by_fundamentals: boolean
  hint: string
}

type UnderResp = {
  ok: boolean
  ts: number
  items: UnderItem[]
  method: string
  note?: string
}

const CATEGORY_LABEL: Record<string, string> = {
  memory_chip: '存储芯片',
  altcoin: '加密',
  safe_metal: '贵金属',
  other: '其它',
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

function GateBadge({ item }: { item: UnderItem }) {
  if (!item.fundamental_verified) {
    return (
      <span className="inline-flex items-center gap-0.5 rounded border border-border bg-secondary/30 px-1 py-0.5 text-[10px] text-muted-foreground">
        <HelpCircle className="h-3 w-3" /> 未核验
      </span>
    )
  }
  if (item.fundamental_ok) {
    return (
      <span className="inline-flex items-center gap-0.5 rounded border border-emerald-500/40 bg-emerald-500/10 px-1 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
        <ShieldCheck className="h-3 w-3" /> 财务未恶化
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-0.5 rounded border border-rose-500/40 bg-rose-500/10 px-1 py-0.5 text-[10px] text-rose-600 dark:text-rose-400">
      <ShieldAlert className="h-3 w-3" /> 基本面恶化
    </span>
  )
}

export function UndervaluedDetectorPanel() {
  const { data, loading, refresh } = usePoll<UnderResp>('/discovery/undervalued-detector', {
    intervalMs: 30 * 60_000,
    transform: (raw) => {
      const d = raw as UnderResp | null
      if (!d || typeof d === 'object' && 'ok' in d && (d as UnderResp).ok === false) return null
      return d as UnderResp
    },
  })

  const items = useMemo(() => data?.items ?? [], [data])

  return (
    <div className="rounded border border-emerald-500/40 bg-emerald-500/5 p-3">
      <div className="flex items-center gap-2 font-mono text-xs">
        <Gem className="h-4 w-4 text-emerald-500" aria-hidden />
        <span className="font-bold uppercase tracking-widest text-emerald-700 dark:text-emerald-300">
          💎 超跌 / 低估检测
        </span>
        <span className="rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] font-bold text-emerald-600 dark:text-emerald-400">
          技术超跌 + 财务闸门
        </span>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="ml-auto inline-flex items-center gap-0.5 rounded border border-border px-1.5 py-0.5 text-[11px] hover:bg-secondary/50 disabled:opacity-40"
        >
          <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
        </button>
      </div>

      {data?.note && <p className="mt-1.5 text-[11px] text-muted-foreground">{data.note}</p>}

      <div className="mt-2 grid grid-cols-1 gap-1.5 md:grid-cols-2">
        {!items.length && (
          <div className="col-span-2 text-xs text-muted-foreground">
            {loading ? '扫描中…' : '暂无超跌标的'}
          </div>
        )}
        {items.slice(0, 12).map((it) => (
          <div key={it.symbol} className="rounded border border-border bg-background/40 p-2">
            <div className="flex items-center gap-1.5">
              <strong className="font-mono text-[11px]">{it.symbol}</strong>
              <span className="text-[11px] text-muted-foreground">{CATEGORY_LABEL[it.category] ?? it.category}</span>
              <GateBadge item={it} />
              <span className="ml-auto font-mono text-[11px] font-bold">
                {Number.isFinite(it.undervalued_score) ? it.undervalued_score.toFixed(0) : '—'}/100
              </span>
            </div>
            <div className="mt-1 grid grid-cols-3 gap-1 font-mono text-[11px] text-muted-foreground">
              <span>7d <span className={cn('text-rose-500', it.change_7d_pct >= 0 && 'text-emerald-500')}>
                {fmtPct(it.change_7d_pct)}
              </span></span>
              <span>30d <span className={cn('text-rose-500', it.change_30d_pct >= 0 && 'text-emerald-500')}>
                {fmtPct(it.change_30d_pct)}
              </span></span>
              <span>新闻 24h ×{it.news_count_24h}</span>
            </div>
            {it.hint && <div className="mt-1 text-[11px] text-foreground/80">💡 {it.hint}</div>}
            {it.reasons.length > 0 && (
              <div className="mt-1 text-[10px] text-muted-foreground/80">
                {it.reasons.slice(0, 3).join(' · ')}
              </div>
            )}
            <div className="mt-1.5">
              <PushToCausalChainButton
                origin="manual_from_undervalued_detector"
                cause={`超跌检测 · ${CATEGORY_LABEL[it.category] ?? it.category} score=${Number.isFinite(it.undervalued_score) ? it.undervalued_score.toFixed(0) : '—'}`}
                effect={it.symbol}
                note={`7d=${it.change_7d_pct}% 30d=${it.change_30d_pct}% phase=${it.phase} reversal_prob=${it.reversal_prob ?? 'n/a'} verified=${it.fundamental_verified} ok=${it.fundamental_ok ?? 'n/a'}`}
                trigger_words={[it.symbol, it.category, 'undervalued'].join(',')}
                weight={0.5}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
