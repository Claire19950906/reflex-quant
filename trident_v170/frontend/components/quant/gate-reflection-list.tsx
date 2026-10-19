'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

type GateNews = {
  news_id: number
  content: string
  verdict: string
  strength: number
}

type GateForwardItem = {
  decision_id: number
  news_id: number
  verdict: string
  strength: number
  core_impact: string
  main_asset: string
  kind: string
  action: string
  settled: number
  correct: boolean | null
  forward_pnl: number | null
  mfe_pct: number | null
  content: string
}

type GateReverseItem = {
  ts: number
  pct_change: number | null
  vol_ratio: number | null
  trigger_type: string
  total_news: number
  covered: boolean
  miss: boolean
  passed_count: number
  excluded_count: number
  passed_news: GateNews[]
  excluded_news: GateNews[]
}

type GateReflectionResp = {
  asset: string
  days: number
  summary: {
    n_judged: number
    n_decided: number
    n_win: number
    n_loss: number
    precision: number | null
    pass_winrate: number | null
    n_abnormal: number
    n_covered: number
    n_miss: number
    recall: number | null
  }
  forward: GateForwardItem[]
  reverse: GateReverseItem[]
}

const VERDICT_CLS: Record<string, string> = {
  可交易: 'text-long bg-long/10 border-long/30',
  只能看: 'text-amber-500 bg-amber-500/10 border-amber-500/30',
  排除: 'text-muted-foreground bg-muted/40 border-border',
  无信号: 'text-muted-foreground bg-muted/40 border-border',
}

const SH_OFFSET_MS = 8 * 3600 * 1000

function fmtTs(ts: number): string {
  const d = new Date(ts * 1000 + SH_OFFSET_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
}

function pct(v: number | null): string {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

// v283+：判决分布（A）+ 操作偏差（B）—— 按 verdict / 按 action 切片，
// 解决原面板"单一 precision 数字分不清哪类判决在赚/亏"的盲点。
const PASS_VERDICTS = new Set(['可交易', '只能看'])

type VerdictBucket = {
  label: string
  count: number
  wins: number
  losses: number
  unsettled: number
  winrate: number | null // null = 0 settled
  totalPnl: number | null // 仅放行类（可交易/只能看）有意义
  isPass: boolean
}

type ActionBucket = {
  count: number
  wins: number
  losses: number
  unsettled: number
  winrate: number | null
}

function buildVerdictBuckets(forward: GateForwardItem[]): VerdictBucket[] {
  const labels = ['可交易', '只能看', '排除', '无信号']
  return labels.map((label) => {
    const items = forward.filter((f) => f.verdict === label)
    const wins = items.filter((f) => f.correct === true).length
    const losses = items.filter((f) => f.correct === false).length
    const unsettled = items.filter((f) => f.correct == null).length
    const settled = wins + losses
    const winrate = settled > 0 ? (wins / settled) * 100 : null
    const totalPnl = PASS_VERDICTS.has(label)
      ? items.reduce((sum, f) => sum + (f.forward_pnl ?? 0), 0)
      : null
    return {
      label,
      count: items.length,
      wins,
      losses,
      unsettled,
      winrate,
      totalPnl,
      isPass: PASS_VERDICTS.has(label),
    }
  })
}

function buildActionBuckets(forward: GateForwardItem[]) {
  const mk = (act: 'BUY' | 'SELL' | 'HOLD'): ActionBucket => {
    const items = forward.filter((f) => f.action === act)
    const wins = items.filter((f) => f.correct === true).length
    const losses = items.filter((f) => f.correct === false).length
    const unsettled = items.filter((f) => f.correct == null).length
    const settled = wins + losses
    return {
      count: items.length,
      wins,
      losses,
      unsettled,
      winrate: settled > 0 ? (wins / settled) * 100 : null,
    }
  }
  return { BUY: mk('BUY'), SELL: mk('SELL'), HOLD: mk('HOLD') }
}

export default function GateReflectionList({ asset }: { asset: string | null }) {
  const [data, setData] = useState<GateReflectionResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState<'forward' | 'reverse'>('forward')

  useEffect(() => {
    if (!asset) {
      setData(null)
      return
    }
    let alive = true
    setLoading(true)
    fetch(`${API_BASE}/market/gate-reflection?asset=${asset}&days=3`, { cache: 'no-store' })
      .then((r: Response) => r.json())
      .then((d: GateReflectionResp) => {
        if (alive) setData(d)
      })
      .catch(() => {
        if (alive) setData(null)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [asset])

  if (!asset) {
    return (
      <div className="mt-4 rounded border border-border bg-card/40 px-3 py-4 text-center font-mono text-xs text-muted-foreground">
        在上方选择品种后，展示该品种的门卫筛选复盘（正向判断兑现 / 反向异动漏检）
      </div>
    )
  }

  const s = data?.summary ?? null
  const forward = data?.forward ?? []
  const reverse = data?.reverse ?? []

  return (
    <div className="mt-4 rounded border border-border bg-card/40">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
        <h4 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
          门卫筛选复盘 · {asset}（近 {data?.days ?? 3} 天）
        </h4>
        <div className="flex items-center gap-0.5 rounded border border-border bg-card p-0.5">
          {(
            [
              ['forward', '正向 · 判断兑现'],
              ['reverse', '反向 · 异动漏检'],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              onClick={() => setTab(k)}
              className={`rounded px-2 py-0.5 font-mono text-[10px] ${
                tab === k ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-secondary'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {s && (
        <div className="grid grid-cols-2 gap-2 px-3 py-2 md:grid-cols-4">
          <div className="rounded bg-card px-2 py-1.5">
            <div className="font-mono text-[10px] text-muted-foreground">正向准确率 precision</div>
            <div className="font-mono text-base text-foreground">{s.precision == null ? '—' : `${s.precision.toFixed(0)}%`}</div>
            <div className="font-mono text-[10px] text-muted-foreground/70">
              判 {s.n_win} 对 / {s.n_loss} 错（有结果 {s.n_decided}）
            </div>
          </div>
          <div className="rounded bg-card px-2 py-1.5">
            <div className="font-mono text-[10px] text-muted-foreground">反向召回 recall</div>
            <div className="font-mono text-base text-foreground">{s.recall == null ? '—' : `${s.recall.toFixed(0)}%`}</div>
            <div className="font-mono text-[10px] text-muted-foreground/70">异动 {s.n_abnormal} 点 / 覆盖 {s.n_covered}</div>
          </div>
          <div className="rounded bg-card px-2 py-1.5">
            <div className="font-mono text-[10px] text-muted-foreground">异动漏检数</div>
            <div className={`font-mono text-base ${s.n_miss > 0 ? 'text-short' : 'text-foreground'}`}>{s.n_miss}</div>
            <div className="font-mono text-[10px] text-muted-foreground/70">行情异常但无门卫放行新闻</div>
          </div>
          <div className="rounded bg-card px-2 py-1.5">
            <div className="font-mono text-[10px] text-muted-foreground">放行后胜率</div>
            <div className="font-mono text-base text-foreground">{s.pass_winrate == null ? '—' : `${s.pass_winrate.toFixed(0)}%`}</div>
            <div className="font-mono text-[10px] text-muted-foreground/70">门卫放行决策 {s.n_judged} 条</div>
          </div>
        </div>
      )}

      {forward.length > 0 && (
        <div className="border-t border-border px-3 py-2">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            判决分布 · 按 verdict
          </div>
          <div className="grid grid-cols-2 gap-1.5 md:grid-cols-4">
            {buildVerdictBuckets(forward).map((b) => (
              <VerdictBucketCard key={b.label} bucket={b} />
            ))}
          </div>
          <ActionBiasStrip forward={forward} />
        </div>
      )}
{loading && !data && (
        <div className="px-3 py-4 text-center font-mono text-xs text-muted-foreground">门卫复盘加载中…</div>
      )}

      {!loading && data && tab === 'forward' && (
        <div className="px-3 pb-3">
          {forward.length === 0 ? (
            <div className="py-4 text-center font-mono text-xs text-muted-foreground">
              暂无该品种的门卫放行决策（可能尚未产生可交易/只能看的新闻，或数据待回填）
            </div>
          ) : (
            <ul className="space-y-1.5">
              {forward.map((f) => (
                <GateForwardRow key={f.decision_id} item={f} />
              ))}
            </ul>
          )}
        </div>
      )}

      {!loading && data && tab === 'reverse' && (
        <div className="px-3 pb-3">
          {reverse.length === 0 ? (
            <div className="py-4 text-center font-mono text-xs text-muted-foreground">
              该品种近期无 3m 异动点（或 K 线数据不可用），暂无法反向核验门卫召回
            </div>
          ) : (
            <ul className="space-y-1.5">
              {reverse.map((r) => (
                <GateReverseRow key={r.ts} item={r} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
function GateForwardRow({ item }: { item: GateForwardItem }) {
  const vc = VERDICT_CLS[item.verdict] ?? 'text-muted-foreground bg-muted/40 border-border'
  const correctCls =
    item.correct === true ? 'text-long' : item.correct === false ? 'text-short' : 'text-muted-foreground'
  const correctLabel = item.correct === true ? '✓ 判对' : item.correct === false ? '✗ 判错' : '· 未结算'

  return (
    <li className="rounded-lg border border-border bg-card px-3 py-2">
      <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${vc}`}>{item.verdict}</span>
        <span className="font-mono text-[10px] text-muted-foreground">强度 {item.strength}</span>
        {item.core_impact && (
          <span className="truncate font-mono text-[10px] text-foreground/80" title={item.core_impact}>
            {item.core_impact}
          </span>
        )}
        <span className="ml-auto font-mono text-[10px] uppercase text-muted-foreground">{item.action}</span>
      </div>
      <p className="line-clamp-2 text-[12px] leading-5 text-foreground/85">{item.content}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px]">
        <span className={correctCls}>{correctLabel}</span>
        <span className="text-muted-foreground">
          盈亏 <span className={item.forward_pnl == null ? '' : item.forward_pnl >= 0 ? 'text-long' : 'text-short'}>{pct(item.forward_pnl)}</span>
        </span>
        {item.mfe_pct != null && <span className="text-muted-foreground">最大有利 {pct(item.mfe_pct)}</span>}
        {item.kind && <span className="text-muted-foreground/70">{item.kind}</span>}
      </div>
    </li>
  )
}

function GateReverseRow({ item }: { item: GateReverseItem }) {
  // v282+: HONESTY — pct_change nullable,不用 ?? 0 假装有数
  const up = item.pct_change != null && item.pct_change >= 0
  const dotColor = item.pct_change == null ? '#f59e0b' : up ? '#22c55e' : '#ef4444'
  const trig = item.trigger_type === 'both' ? '量价异动' : item.trigger_type === 'volume' ? '量能异动' : '涨跌异动'

  return (
    <li className={`rounded-lg border px-3 py-2 ${item.miss ? 'border-short/40 bg-short/5' : 'border-border bg-card'}`}>
      <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px]">
        <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: dotColor }} />
        <span className="text-foreground/80">{fmtTs(item.ts)}</span>
        <span className="text-muted-foreground">
          {item.pct_change != null ? `单根${item.pct_change >= 0 ? '+' : ''}${item.pct_change.toFixed(2)}%` : ''}
          {item.vol_ratio != null ? ` · 量比 ${item.vol_ratio.toFixed(2)}×` : ''} · {trig}
        </span>
        <span
          className={`ml-auto rounded border px-1.5 py-0.5 text-[10px] ${
            item.miss
              ? 'border-short/40 bg-short/10 text-short'
              : 'border-long/40 bg-long/10 text-long'
          }`}
        >
          {item.miss ? '⚠ 疑似漏检' : '✓ 已抓到'}
        </span>
      </div>

      {item.passed_news.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {item.passed_news.slice(0, 3).map((n) => (
            <div key={n.news_id} className="flex items-start gap-2 text-[11px] leading-relaxed text-foreground/85">
              <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-long" />
              <span className="min-w-0 flex-1">{n.content}</span>
              <span className="shrink-0 font-mono text-[10px] text-muted-foreground">强度 {n.strength}</span>
            </div>
          ))}
        </div>
      )}
      {item.miss && item.total_news === 0 && (
        <div className="mt-1 font-mono text-[10px] text-muted-foreground/80">
          该异动点 ±30min 内无任何该品种放行新闻（门卫未放行 → 需核验是否漏掉重要消息）
        </div>
      )}
      {item.miss && item.excluded_news.length > 0 && (
        <div className="mt-1 font-mono text-[10px] text-muted-foreground/80">
          窗口内 {item.total_news} 条均被门卫「排除/只能看」未放行，疑似漏检
        </div>
      )}
    </li>
  )
}

// ---------- v283+：判决分布卡片（A） ----------
function VerdictBucketCard({ bucket }: { bucket: VerdictBucket }) {
  const cls = VERDICT_CLS[bucket.label] ?? 'text-muted-foreground bg-muted/40 border-border'
  const textCls = cls.split(' ').find((c) => c.startsWith('text-')) ?? 'text-muted-foreground'
  const bgCls = cls.split(' ').find((c) => c.startsWith('bg-')) ?? 'bg-muted/40'
  const borderCls = cls.split(' ').find((c) => c.startsWith('border-')) ?? 'border-border'

  const max = Math.max(bucket.wins, bucket.losses, bucket.unsettled, 1)
  const wPct = (bucket.wins / max) * 100
  const lPct = (bucket.losses / max) * 100
  const uPct = (bucket.unsettled / max) * 100

  return (
    <div className={`rounded border ${borderCls} ${bgCls} px-2 py-1.5`}>
      <div className="flex items-center justify-between">
        <span className={`font-mono text-[11px] font-semibold ${textCls}`}>{bucket.label}</span>
        <span className="font-mono text-[10px] text-muted-foreground">{bucket.count} 条</span>
      </div>
      {bucket.count > 0 && (
        <>
          <div className="mt-1 flex h-1.5 gap-0.5 overflow-hidden rounded-sm">
            <div style={{ width: `${wPct}%`, backgroundColor: 'var(--long)', opacity: 0.75 }} />
            <div style={{ width: `${lPct}%`, backgroundColor: 'var(--short)', opacity: 0.75 }} />
            <div style={{ width: `${uPct}%`, backgroundColor: 'var(--muted-foreground, #9ca3af)', opacity: 0.45 }} />
          </div>
          <div className="mt-1 font-mono text-[10px] leading-tight text-muted-foreground">
            {bucket.isPass ? (
              <PassBucketStats bucket={bucket} />
            ) : (
              <NonPassBucketStats bucket={bucket} />
            )}
          </div>
        </>
      )}
    </div>
  )
}

function PassBucketStats({ bucket }: { bucket: VerdictBucket }) {
  const wr = bucket.winrate
  const pnl = bucket.totalPnl
  return (
    <>
      {wr != null && (
        <span className={wr >= 50 ? 'text-long' : 'text-short'}>胜率 {wr.toFixed(0)}%</span>
      )}
      {wr != null && ' · '}
      <span
        className={
          pnl == null
            ? ''
            : pnl > 0
            ? 'text-long'
            : pnl < 0
            ? 'text-short'
            : ''
        }
      >
        PnL{' '}
        {pnl == null
          ? '—'
          : `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`}
      </span>
    </>
  )
}

function NonPassBucketStats({ bucket }: { bucket: VerdictBucket }) {
  return (
    <>
      <span className="text-long">✓{bucket.wins}</span>
      <span> · </span>
      <span className={bucket.losses > 0 ? 'text-short' : 'text-muted-foreground'}>✗{bucket.losses}</span>
      <span> · </span>
      <span className="text-muted-foreground/70">未结{bucket.unsettled}</span>
    </>
  )
}

// ---------- v283+：操作偏差条（B） ----------
function ActionBiasStrip({ forward }: { forward: GateForwardItem[] }) {
  const buckets = buildActionBuckets(forward)
  const total = forward.length
  if (total === 0) return null

  const Item = ({
    label,
    bucket,
    colorCls,
  }: {
    label: string
    bucket: ActionBucket
    colorCls: string
  }) => {
    if (bucket.count === 0) return null
    const pct = ((bucket.count / total) * 100).toFixed(0)
    return (
      <div className="flex items-center gap-1.5">
        <span className={`font-mono text-[10px] font-bold ${colorCls}`}>{label}</span>
        <span className="font-mono text-[11px] text-foreground">{pct}%</span>
        <span className="font-mono text-[10px] text-muted-foreground/70">({bucket.count}条)</span>
        {bucket.winrate != null && (
          <span
            className={`font-mono text-[10px] ${bucket.winrate >= 50 ? 'text-long' : 'text-short'}`}
          >
            胜率 {bucket.winrate.toFixed(0)}%
          </span>
        )}
      </div>
    )
  }

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 rounded border border-border bg-card px-2 py-1.5">
      <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
        ⚖ 操作偏差
      </span>
      <Item label="BUY" bucket={buckets.BUY} colorCls="text-long" />
      <Item label="SELL" bucket={buckets.SELL} colorCls="text-short" />
      {buckets.HOLD.count > 0 && <Item label="HOLD" bucket={buckets.HOLD} colorCls="text-muted-foreground" />}
    </div>
  )
}