'use client'

/**
 * v302+ P0 因果图谱反馈闭环面板
 * 需求 8 核心: "排除干扰 → 判断因果链对/错 → 纠正 → confidence 校准"
 *
 * 数据流:
 *   1. 拉取 /api/v291/causal-chains/feedback (auto_run=true)
 *   2. 渲染: 顶部汇总 + 各 chain 卡的 recent_outcomes 列表 + 校准差距
 *   3. 操作: 刷新, 切换时间窗口, 立即回填 N 天
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity, AlertTriangle, CheckCircle2, CircleDashed,
  GitBranch, History, RefreshCcw, TrendingDown, TrendingUp, XCircle,
} from 'lucide-react'
import { API_BASE } from '@/lib/api'
import { useToast } from '@/components/ui/toast'

type OutcomeStatus = 'HIT' | 'MISS' | 'NEUTRAL' | 'PENDING' | 'EXPIRED'

type RecentOutcome = {
  ts: number
  headline: string
  status: OutcomeStatus
  pct_5d: number | null
  direction_hit: boolean | null
  magnitude_hit: boolean | null
}

type ChainFeedback = {
  chain_id: string
  asset: string
  expected_direction: 'UP' | 'DOWN' | 'BOTH'
  magnitude_lo: number
  magnitude_hi: number
  stated_confidence: number
  lifetime_hit: number
  lifetime_miss: number
  window_days: number
  outcomes_n: number
  outcomes_hit: number
  outcomes_miss: number
  outcomes_neutral: number
  outcomes_pending: number
  real_hit_rate: number | null
  calibration_gap: number | null
  needs_recalibration: boolean
  recent_outcomes: RecentOutcome[]
}

type FeedbackResponse = {
  ok: boolean
  window_days: number
  chains: ChainFeedback[]
  summary: {
    n_chains: number
    n_chains_with_outcomes: number
    n_chains_needs_recalibration: number
    total_outcomes: number
    total_hit: number
    total_miss: number
    total_neutral: number
    total_pending: number
    overall_hit_rate: number | null
  }
  evaluator?: { auto_run: boolean; evaluated?: any }
  data_source?: string
}

function pctFmt(n: number | null | undefined, digits = 1) {
  if (n === null || n === undefined) return '—'
  const s = n >= 0 ? '+' : ''
  return `${s}${n.toFixed(digits)}%`
}

function tsFmt(ts: number | null | undefined) {
  if (!ts) return '—'
  return new Date(ts * 1000).toISOString().slice(0, 10)
}

const STATUS_META = {
  HIT: { label: '命中', cls: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300', icon: CheckCircle2 },
  MISS: { label: '错', cls: 'border-rose-500/50 bg-rose-500/10 text-rose-600 dark:text-rose-300', icon: XCircle },
  NEUTRAL: { label: '中性', cls: 'border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-300', icon: Activity },
  PENDING: { label: '待评估', cls: 'border-slate-400/50 bg-slate-400/10 text-slate-500 dark:text-slate-300', icon: CircleDashed },
  EXPIRED: { label: '数据缺', cls: 'border-slate-400/50 bg-slate-400/10 text-slate-400', icon: CircleDashed },
}

function dirArrow(dir: ChainFeedback['expected_direction']) {
  if (dir === 'UP') return <TrendingUp className="h-3.5 w-3.5 text-emerald-500" />
  if (dir === 'DOWN') return <TrendingDown className="h-3.5 w-3.5 text-rose-500" />
  return <Activity className="h-3.5 w-3.5 text-slate-400" />
}

export function CausalChainsFeedbackPanel() {
  const { toast } = useToast()
  const [data, setData] = useState<FeedbackResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [backfilling, setBackfilling] = useState(false)
  const [windowDays, setWindowDays] = useState(90)

  const load = useCallback(async (autoRun = true) => {
    try {
      setLoading(true)
      const r = await fetch(
        `${API_BASE}/v291/causal-chains/feedback?days=${windowDays}&auto_run=${autoRun}`,
        { cache: 'no-store' },
      )
      if (!r.ok) throw new Error(`feedback ${r.status}`)
      const json = (await r.json()) as FeedbackResponse
      setData(json)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [windowDays])

  useEffect(() => {
    void load(true)
  }, [load])

  const handleBackfill = useCallback(async () => {
    try {
      setBackfilling(true)
      const r = await fetch(`${API_BASE}/v291/causal-chains/feedback/backfill`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ days: windowDays }),
      })
      if (!r.ok) throw new Error(`backfill ${r.status}`)
      const json = await r.json()
      toast.success(`回填完成: 扫 ${json.chains_scanned} 链 → 写入 ${json.outcomes_inserted} 条新 outcome`)
      await load(true)
    } catch (cause) {
      toast.error(`回填失败: ${cause instanceof Error ? cause.message : String(cause)}`)
    } finally {
      setBackfilling(false)
    }
  }, [windowDays, load, toast])

  const summary = data?.summary
  const chains = useMemo(
    () => (data?.chains || []).slice().sort((a, b) => {
      if (a.needs_recalibration !== b.needs_recalibration) {
        return a.needs_recalibration ? -1 : 1
      }
      return b.outcomes_n - a.outcomes_n
    }),
    [data?.chains],
  )

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-4 p-3 md:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <History className="h-4 w-4 text-primary" />
            因果链反馈闭环
            {summary && summary.n_chains_needs_recalibration > 0 ? (
              <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 font-mono text-[10px] text-rose-600 dark:text-rose-300">
                <AlertTriangle className="h-3 w-3" />
                {summary.n_chains_needs_recalibration} 条需重新校准
              </span>
            ) : null}
          </h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
            每条因果链的"逐次事件"反馈：触发后 5 个交易日回查真实涨跌，判定 HIT/MISS/NEUTRAL，
            据此校准 stated_confidence。重新校准阈值：|calibration_gap| &gt; 0.15 且 n_eval &ge; 5。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            className="rounded-md border border-border bg-card px-2 py-1 font-mono text-xs"
            value={windowDays}
            onChange={(e) => setWindowDays(Number(e.target.value))}
            aria-label="时间窗口"
          >
            <option value={30}>30 天</option>
            <option value={90}>90 天</option>
            <option value={180}>180 天</option>
            <option value={365}>365 天</option>
          </select>
          <button
            type="button"
            onClick={() => void load(true)}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1 font-mono text-xs text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
            disabled={loading}
          >
            <RefreshCcw className={'h-3 w-3 ' + (loading ? 'animate-spin' : '')} />
            刷新
          </button>
          <button
            type="button"
            onClick={() => void handleBackfill()}
            className="inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1 font-mono text-xs text-primary transition-colors hover:bg-primary/20 disabled:opacity-50"
            disabled={backfilling}
          >
            <GitBranch className={'h-3 w-3 ' + (backfilling ? 'animate-pulse' : '')} />
            {backfilling ? '回填中…' : '立即回填'}
          </button>
        </div>
      </div>

      {error ? (
        <div className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-xs text-rose-600 dark:text-rose-300">
          加载失败: {error}
        </div>
      ) : null}

      {summary ? (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
          <SummaryCell label="已评估事件" value={String(summary.total_outcomes)} sub={`${windowDays}d 窗口`} tone="default" />
          <SummaryCell label="命中 HIT" value={String(summary.total_hit)} tone="good" />
          <SummaryCell label="错 MISS" value={String(summary.total_miss)} tone="bad" />
          <SummaryCell label="中性 NEUTRAL" value={String(summary.total_neutral)} tone="warn" />
          <SummaryCell label="待评估 PENDING" value={String(summary.total_pending)} tone="muted" />
          <SummaryCell
            label="总命中率"
            value={pctFmt((summary.overall_hit_rate ?? 0) * 100, 0)}
            sub={`${summary.n_chains_with_outcomes}/${summary.n_chains} 链有数据`}
            tone="default"
          />
        </div>
      ) : null}

      {loading && !data ? (
        <div className="p-6 text-center font-mono text-xs text-muted-foreground">加载中…</div>
      ) : chains.length === 0 ? (
        <div className="rounded-md border border-border bg-card p-6 text-center text-xs text-muted-foreground">暂无因果链</div>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {chains.map((ch) => <ChainFeedbackCard key={ch.chain_id} chain={ch} />)}
        </div>
      )}

      {data?.evaluator?.evaluated ? (
        <div className="font-mono text-[10px] text-muted-foreground">
          本次刷新已自动评估: {JSON.stringify(data.evaluator.evaluated).slice(0, 200)}
        </div>
      ) : null}
    </div>
  )
}

function SummaryCell({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone: 'default' | 'good' | 'bad' | 'warn' | 'muted'
}) {
  const toneCls = {
    default: 'border-border bg-card text-foreground',
    good: 'border-emerald-500/30 bg-emerald-500/5 text-emerald-700 dark:text-emerald-300',
    bad: 'border-rose-500/30 bg-rose-500/5 text-rose-700 dark:text-rose-300',
    warn: 'border-amber-500/30 bg-amber-500/5 text-amber-700 dark:text-amber-300',
    muted: 'border-slate-400/30 bg-slate-400/5 text-slate-500 dark:text-slate-400',
  }[tone]
  return (
    <div className={'rounded-md border p-2.5 ' + toneCls}>
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums">{value}</div>
      {sub ? <div className="font-mono text-[10px] text-muted-foreground">{sub}</div> : null}
    </div>
  )
}

function ChainFeedbackCard({ chain }: { chain: ChainFeedback }) {
  const [expanded, setExpanded] = useState(chain.outcomes_n > 0)
  const meta = chain.needs_recalibration
    ? {
        ring: 'ring-1 ring-rose-500/40',
        badge: (
          <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 font-mono text-[10px] text-rose-600 dark:text-rose-300">
            <AlertTriangle className="h-3 w-3" />
            需重新校准
          </span>
        ),
      }
    : { ring: '', badge: null }
  const gapInfo = (() => {
    if (chain.calibration_gap === null) {
      return <span className="font-mono text-[10px] text-muted-foreground">样本不足</span>
    }
    const g = chain.calibration_gap
    const real = chain.real_hit_rate ?? 0
    const sign = g >= 0 ? '高估' : '低估'
    return (
      <span className={'font-mono text-[10px] ' + (Math.abs(g) > 0.15 ? 'text-rose-500' : 'text-muted-foreground')}>
        stated={chain.stated_confidence.toFixed(2)} vs real={real.toFixed(2)} → {sign} {Math.abs(g).toFixed(2)}
      </span>
    )
  })()

  return (
    <div className={'rounded-md border border-border bg-card p-3 ' + meta.ring}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold">{chain.chain_id}</span>
            {dirArrow(chain.expected_direction)}
            <span className="rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
              {chain.asset}
            </span>
            <span className="font-mono text-[10px] text-muted-foreground">
              mag [{chain.magnitude_lo}%, {chain.magnitude_hi}%]
            </span>
            {meta.badge}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-3">
            <span className="font-mono text-[11px]">
              <span className="text-muted-foreground">stated_conf </span>
              <span className="font-semibold">{chain.stated_confidence.toFixed(2)}</span>
            </span>
            <span className="font-mono text-[11px]">
              <span className="text-muted-foreground">real_hit </span>
              <span className="font-semibold">
                {chain.real_hit_rate === null ? '—' : pctFmt(chain.real_hit_rate * 100, 0)}
              </span>
              <span className="ml-1 text-muted-foreground">
                ({chain.outcomes_hit}/{chain.outcomes_hit + chain.outcomes_miss + chain.outcomes_neutral})
              </span>
            </span>
            {gapInfo}
          </div>
        </div>
        <div className="flex items-center gap-1 font-mono text-[10px]">
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-600 dark:text-emerald-300">H {chain.outcomes_hit}</span>
          <span className="rounded bg-rose-500/15 px-1.5 py-0.5 text-rose-600 dark:text-rose-300">M {chain.outcomes_miss}</span>
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-600 dark:text-amber-300">N {chain.outcomes_neutral}</span>
          <span className="rounded bg-slate-400/15 px-1.5 py-0.5 text-slate-500 dark:text-slate-400">P {chain.outcomes_pending}</span>
        </div>
      </div>

      {chain.recent_outcomes.length > 0 ? (
        <button
          type="button"
          className="mt-2 w-full text-left font-mono text-[10px] text-muted-foreground hover:text-foreground"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? '▼' : '▶'} 最近 {chain.recent_outcomes.length} 次 outcome
        </button>
      ) : null}

      {expanded && chain.recent_outcomes.length > 0 ? (
        <ul className="mt-2 space-y-1.5">
          {chain.recent_outcomes.map((o: RecentOutcome, i) => {
            const m = STATUS_META[o.status] || STATUS_META.PENDING
            const Icon = m.icon
            return (
              <li key={i} className="flex flex-wrap items-center gap-2 rounded border border-border bg-background/40 p-1.5">
                <span className={'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ' + m.cls}>
                  <Icon className="h-3 w-3" />
                  {m.label}
                </span>
                <span className="font-mono text-[10px] text-muted-foreground">{tsFmt(o.ts)}</span>
                <span className="min-w-0 flex-1 truncate text-[11px]" title={o.headline}>
                  {o.headline || '—'}
                </span>
                <span
                  className={
                    'font-mono text-[10px] tabular-nums ' +
                    (o.pct_5d === null
                      ? 'text-muted-foreground'
                      : o.pct_5d > 0
                        ? 'text-emerald-600 dark:text-emerald-300'
                        : 'text-rose-600 dark:text-rose-300')
                  }
                >
                  5d {pctFmt(o.pct_5d)}
                </span>
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}



