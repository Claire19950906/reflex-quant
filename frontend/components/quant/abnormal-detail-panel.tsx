'use client'

import { useEffect, useMemo, useState } from 'react'
import { API_BASE, fetchWithFallback } from '@/lib/api'
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Loader2,
  Sparkles,
  History,
  BookOpen,
  Users,
} from 'lucide-react'

// ── Phase 2 异常签名 · 详情面板 ────────────────────────────────────────
// 列出来自 /api/abnormal-signals（multi_factor）的事件；点行展开抽屉，
// 抽屉内：左 = AI 多因子归因（Step 2 产物），右 = 相似历史事件（Step 3 产物）。
// 两个子数据互不耦合，独立 loading / error；attribution 为 null 时按"pending"提示。
//
// 不改后端；只消费 /api/abnormal-signals/{id}/attribution 和 /similar。

type SignalDirection = 'bullish' | 'bearish' | string

type AbnormalSignal = {
  id: number
  ts: number
  asset: string
  signal_kind: string
  direction: SignalDirection
  anchor_ts: number | null
  detail: {
    factors?: string[]
    rsi?: number | null
    rsi_divergence?: string | null
    volume_divergence?: string | null
  }
  hint: string
  status: string
  created_at: string
}

type AttributionDriver = {
  cause: string
  source?: string
  confidence?: 'high' | 'medium' | 'low' | string
  speculative?: boolean
}

type Attribution = {
  summary?: string
  drivers?: AttributionDriver[]
  cross_asset_likely?: string[]
  actionable?: string
  severity_hint?: number
  model?: string
  source_model?: string
  used_news_n?: number
  created_at?: string
} | null

// v317: 跨平台注意力证据 —— 独立于 AI 归因的一条客观事实来源。
// 来自真实抓取的 crowd_source_items（带真实发布时间戳），不是 LLM 推断。
type CrowdAttentionEvidence = {
  symbol: string
  attention_ts: number
  attention_at: string
  lead_minutes: number
  platform_count: number
  platform_list: string[]
  item_count: number
  items?: Array<{
    title?: string
    source?: string
    url?: string
    media_channel?: string
    published_at?: string
    heat?: number
  }>
  kind?: string
  explain?: string
}

type AttributionResp = {
  signal_id: number
  asset: string
  signal_kind: string
  status: string
  attribution?: Attribution
  crowd_attention?: CrowdAttentionEvidence | null
  error?: string
}

type SimilarResult = {
  signal_id: number
  ts: number
  asset: string
  direction: SignalDirection
  factors?: string[]
  similarity_score: number
  time_delta_h: number
  hint?: string
  attribution?: {
    summary?: string
    actionable?: string
    severity_hint?: number
  } | null
}

type SimilarResp = {
  current: { signal_id: number; ts: number; asset: string; direction: string; factors?: string[] } | null
  since_ts: number
  results: SimilarResult[]
  summary: string
  fallback?: boolean
  error?: string
}

const SH_OFFSET_MS = 8 * 3600 * 1000

function fmtTs(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000 + SH_OFFSET_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
}

const SEV_META: Record<number, { label: string; cls: string }> = {
  1: { label: '轻度', cls: 'bg-muted/40 text-muted-foreground border-border' },
  2: { label: '中度', cls: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40' },
  3: { label: '重度', cls: 'bg-short/15 text-short border-short/40' },
}

const DIR_META: Record<string, { label: string; cls: string }> = {
  bullish: { label: '看涨', cls: 'text-long bg-long/15 border-long/30' },
  bearish: { label: '看跌', cls: 'text-short bg-short/15 border-short/30' },
  // 中性 / 空方向：multi_factor 在 bull/bear 平票时 direction=None → 后端落库为 ''
  neutral: { label: '方向未明', cls: 'text-hold bg-hold/15 border-hold/30' },
  '': { label: '方向未明', cls: 'text-hold bg-hold/15 border-hold/30' },
}

// 需求：归因页"量能异动"改用背离类型（顶背离/底背离）显式渲染，
// 并提供事件简写 + 悬停 tooltip 全称。后端 detail 已有 rsi_divergence / volume_divergence
// 但前端之前没消费，仅展示裸 factors 列表。
const DIV_LABEL = {
  rsi_top:        { label: 'RSI 顶背离', cls: 'text-short bg-short/15 border-short/40', short: 'RSI顶' },
  rsi_bottom:     { label: 'RSI 底背离', cls: 'text-long bg-long/15 border-long/40',   short: 'RSI底' },
  vol_top:        { label: '量价顶背离', cls: 'text-short bg-short/15 border-short/40', short: '量价顶' },
  vol_bottom:     { label: '量价底背离', cls: 'text-long bg-long/15 border-long/40',   short: '量价底' },
} as const

type DivKey = keyof typeof DIV_LABEL

function divergenceFromDetail(detail: AbnormalSignal['detail'] | undefined): { key: DivKey; meta: typeof DIV_LABEL[DivKey] } | null {
  if (!detail) return null
  const rsi = (detail.rsi_divergence || '').toLowerCase()
  const vol = (detail.volume_divergence || '').toLowerCase()
  // RSI 优先（更灵敏）；若 RSI 已给出方向就用 RSI，否则用 量价
  if (rsi === 'bearish') return { key: 'rsi_top', meta: DIV_LABEL.rsi_top }
  if (rsi === 'bullish') return { key: 'rsi_bottom', meta: DIV_LABEL.rsi_bottom }
  if (vol === 'bearish') return { key: 'vol_top', meta: DIV_LABEL.vol_top }
  if (vol === 'bullish') return { key: 'vol_bottom', meta: DIV_LABEL.vol_bottom }
  return null
}

// 把 factors 列表压成一段"事件简写"（≤ 16 字）+ 全名用于 tooltip
function summarizeFactors(factors: string[]): { short: string; full: string } {
  if (!factors.length) return { short: '—', full: '' }
  // 找第一个背离 / 异动标签作为简写
  const first = factors[0] || ''
  const short = first.length > 12 ? first.slice(0, 12) + '…' : first
  const full = factors.join(' · ')
  return { short, full }
}

function dirMeta(d: SignalDirection | null | undefined): { label: string; cls: string } {
  const key = (d || '').toLowerCase()
  return (
    DIR_META[key] ?? {
      label: d || '方向未明',
      cls: 'text-muted-foreground bg-muted/40 border-border',
    }
  )
}

function scoreColor(s: number): string {
  if (s >= 0.9) return 'text-long'
  if (s >= 0.7) return 'text-amber-600 dark:text-amber-300'
  return 'text-muted-foreground'
}

// ── AttributionCard 子组件 ────────────────────────────────────────────
export function AttributionCard({ signalId }: { signalId: number }) {
  const [data, setData] = useState<AttributionResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const ctrl = new AbortController()
    setLoading(true)
    setErr(null)
    // 主服务不可用, 走 fetchWithFallback 到桥接
    // bridge catch-all → {ok:true,_bridge_fallback:true} → 转成 "signal_id 不存在" 空态
    fetchWithFallback<AttributionResp & { _bridge_fallback?: boolean }>(
      `/abnormal-signals/${signalId}/attribution`,
    )
      .then((d) => {
        if (!alive) return
        if (d?._bridge_fallback) {
          setData({
            signal_id: signalId, asset: '', signal_kind: 'multi_factor', status: 'unknown',
            error: 'bridge_fallback',
          } as AttributionResp)
          return
        }
        setData(d as AttributionResp)
      })
      .catch((e) => {
        if (!alive || e?.name === 'AbortError') return
        setErr(String(e?.message || e))
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false; ctrl.abort() }
  }, [signalId])

  if (loading) {
    return (
      <div className="flex h-20 items-center justify-center text-xs text-muted-foreground">
        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />正在拉取 AI 归因…
      </div>
    )
  }
  if (err) {
    return (
      <div className="rounded border border-short/30 bg-short/10 p-2 text-xs text-short">
        <AlertCircle className="mr-1 inline h-3 w-3" />归因接口异常：{err}
      </div>
    )
  }
  if (!data) return null

  // 后端对不存在的 signal 返回 200 + {error: "not found"}；区分 "未生成归因 (pending)" 与 "信号不存在 (not found)"
  if (data.error) {
    return (
      <div className="rounded border border-short/30 bg-short/10 p-2.5 text-xs text-short">
        <AlertCircle className="mr-1 inline h-3 w-3" />
        <span className="font-mono">信号不存在</span>
        <span className="ml-1.5 text-foreground/80">（signal_id={data.signal_id}；可能已被清理）</span>
      </div>
    )
  }

  const a = data.attribution
  if (!a) {
    // v317: AI 归因 pending 时，注意力证据仍要独立展示 —— 两条证据互不依赖，
    // 不能因为 LLM 还没跑完就把真实抓到的跨平台注意力藏起来。
    return (
      <div className="space-y-2.5">
        <div className="rounded border border-amber-500/30 bg-amber-500/5 p-2.5 text-xs text-amber-700 dark:text-amber-300">
          <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />
          <span className="font-mono">pending</span>
          <span className="ml-1.5 text-foreground/80">归因生成中（通常 30-90s 内完成，可稍后再开）</span>
        </div>
        {data.crowd_attention && <CrowdAttentionCard ev={data.crowd_attention} />}
      </div>
    )
  }

  const drivers = (a.drivers ?? []).slice().sort((x, y) => {
    // 排序：conf=high 优先，其次 medium，低推测在后；稳定顺序由数组下标兜底
    const order = { high: 0, medium: 1, low: 2 } as Record<string, number>
    const xo = order[(x.confidence ?? 'medium').toLowerCase()] ?? 1
    const yo = order[(y.confidence ?? 'medium').toLowerCase()] ?? 1
    return xo - yo
  })
  const sev = SEV_META[a.severity_hint ?? 1] ?? SEV_META[1]
  const actionable = (a.actionable ?? '观望').toLowerCase()

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
        <span className={`rounded border px-1.5 py-0.5 ${sev.cls}`}>严重度 {a.severity_hint ?? 1} · {sev.label}</span>
        <span
          className={`rounded border px-1.5 py-0.5 ${
            actionable === 'yes'
              ? 'bg-long/15 text-long border-long/40'
              : actionable === 'no'
                ? 'bg-muted/40 text-muted-foreground border-border'
                : 'bg-hold/15 text-hold border-hold/40'
          }`}
        >
          actionable: {a.actionable ?? '观望'}
        </span>
        {(a.source_model || a.model) && (
          <span className="rounded bg-muted/40 px-1.5 py-0.5 text-muted-foreground">
            {a.source_model || a.model}
            {typeof a.used_news_n === 'number' && <span className="ml-1 text-muted-foreground/70">· {a.used_news_n} news</span>}
          </span>
        )}
        {a.created_at && <span className="ml-auto text-muted-foreground/70">{a.created_at}</span>}
      </div>

      {a.summary && (
        <p className="rounded border border-primary/30 bg-primary/5 px-2.5 py-2 text-[12px] leading-relaxed text-foreground/90">
          {a.summary}
        </p>
      )}

      {drivers.length > 0 && (
        <div>
          <div className="mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">主驱动（LLM 推理 · 最多 3 条）</div>
          <ul className="space-y-1">
            {drivers.map((d, i) => {
              const conf = (d.confidence ?? 'medium').toLowerCase()
              const confCls =
                conf === 'high'
                  ? 'bg-long/15 text-long border-long/40'
                  : conf === 'low'
                    ? 'bg-muted/40 text-muted-foreground border-border'
                    : 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40'
              return (
                <li key={i} className="rounded border border-border bg-card/50 px-2 py-1.5 text-[11px]">
                  <div className="flex flex-wrap items-baseline gap-1.5">
                    <span className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded bg-primary/15 font-mono text-[11px] font-semibold text-primary">
                      {i + 1}
                    </span>
                    <span className={`shrink-0 rounded border px-1 py-0.5 font-mono text-[11px] ${confCls}`}>
                      {conf}
                    </span>
                    {d.speculative && (
                      <span className="shrink-0 rounded border border-amber-500/40 bg-amber-500/10 px-1 py-0.5 font-mono text-[11px] text-amber-700 dark:text-amber-300">
                        推测
                      </span>
                    )}
                    {d.source && (
                      <span className="ml-auto shrink-0 rounded bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                        {d.source}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 leading-relaxed text-foreground/85">{d.cause}</p>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {Array.isArray(a.cross_asset_likely) && a.cross_asset_likely.length > 0 && (
        <div>
          <div className="mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">跨资产可能驱动</div>
          <ul className="flex flex-wrap gap-1.5">
            {a.cross_asset_likely.map((c, i) => (
              <li key={i} className="rounded border border-border bg-muted/30 px-2 py-1 font-mono text-[11px] text-foreground/85">
                {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* v317: 跨平台注意力证据。与上面的 AI 归因是两回事 ——
          这里只陈述"异动之前确实有多平台提及"这一事实，不参与也不改写 AI 结论。 */}
      {data.crowd_attention && <CrowdAttentionCard ev={data.crowd_attention} />}
    </div>
  )
}

/** v317: 跨平台注意力证据卡片
 *
 *  诚实性要点：单条事件里"异动前有注意力"**不等于**"注意力是原因"。
 *  全局样本验证的结论是 attention_lags_price（多数情况是媒体追价格），
 *  所以这里必须带上这条背景，不能让单点证据被读成因果证明。
 */
function CrowdAttentionCard({ ev }: { ev: CrowdAttentionEvidence }) {
  const platforms = ev.platform_list ?? []
  return (
    <div className="rounded border border-primary/30 bg-primary/5 p-2.5">
      <div className="flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
        <Users className="h-3 w-3 text-primary" />
        <span className="font-semibold text-foreground/90">跨平台注意力（v317）</span>
        <span className="rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-primary">
          领先 {ev.lead_minutes} 分钟
        </span>
        <span className="rounded bg-muted/40 px-1.5 py-0.5 text-muted-foreground">
          {ev.platform_count} 个平台 · {ev.item_count} 条
        </span>
        <span className="ml-auto text-muted-foreground/70">{ev.attention_at}</span>
      </div>

      {ev.explain && (
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-foreground/85">{ev.explain}</p>
      )}

      {platforms.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {platforms.map((p) => (
            <span key={p} className="rounded border border-border bg-card/60 px-1.5 py-0.5 font-mono text-[11px] text-foreground/80">
              {p}
            </span>
          ))}
        </div>
      )}

      {Array.isArray(ev.items) && ev.items.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {ev.items.map((it, i) => (
            <li key={i} className="rounded border border-border bg-card/50 px-2 py-1 text-[11px]">
              <div className="flex flex-wrap items-baseline gap-1.5">
                {it.source && (
                  <span className="shrink-0 rounded bg-muted/40 px-1 py-0.5 font-mono text-[11px] text-muted-foreground">
                    {it.source}
                  </span>
                )}
                {it.published_at && (
                  <span className="font-mono text-[11px] text-muted-foreground/70">{it.published_at}</span>
                )}
              </div>
              {it.url ? (
                <a
                  href={it.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-0.5 block truncate text-foreground/85 underline decoration-muted-foreground/40 underline-offset-2 hover:decoration-primary"
                >
                  {it.title || it.url}
                </a>
              ) : (
                <span className="mt-0.5 block truncate text-foreground/85">{it.title}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="mt-1.5 border-t border-primary/20 pt-1.5 text-[11px] leading-relaxed text-muted-foreground">
        这是一条独立证据（真实抓取、带发布时间戳），不参与也不改写上面的 AI 归因。
        <span className="text-amber-600 dark:text-amber-400">
          注意：单次「异动前有注意力」不等于注意力是原因
        </span>
        —— 全局样本检验的结论是注意力多在价格之后（attention_lags_price），
        请把本卡片当作「当时确实有人在讨论」的事实，而不是因果证明。
      </div>
    </div>
  )
}
// ── SimilarList 子组件 ────────────────────────────────────────────────
function SimilarList({ signalId }: { signalId: number }) {
  const [data, setData] = useState<SimilarResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const ctrl = new AbortController()
    setLoading(true)
    setErr(null)
    // 主服务不可用, 走 fetchWithFallback 到桥接
    // bridge catch-all 返回 {ok:true,_bridge_fallback:true,...} → 转成 "信号不存在" 空态
    fetchWithFallback<SimilarResp & { _bridge_fallback?: boolean }>(
      `/abnormal-signals/${signalId}/similar?since=30d&max_results=5`,
    )
      .then((d) => {
        if (!alive) return
        if (d?._bridge_fallback) {
          // bridge 未实现该端点 → 明确提示, 不要伪装成"近 30d 无相似事件"
          setData({ current: null, since_ts: 0, results: [], summary: '', error: 'bridge_fallback' } as SimilarResp)
          return
        }
        setData(d as SimilarResp)
      })
      .catch((e) => {
        if (!alive || e?.name === 'AbortError') return
        setErr(String(e?.message || e))
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false; ctrl.abort() }
  }, [signalId])

  if (loading) {
    return (
      <div className="flex h-20 items-center justify-center text-xs text-muted-foreground">
        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />正在检索相似历史…
      </div>
    )
  }
  if (err) {
    return (
      <div className="rounded border border-short/30 bg-short/10 p-2 text-xs text-short">
        <AlertCircle className="mr-1 inline h-3 w-3" />相似检索异常：{err}
      </div>
    )
  }
  if (!data) return null

  // 信号不存在时后端返回 current=null + results=[] + error=...；
  // 不能误显示成"近 30d 没有..."。
  if (data.error || data.current === null) {
    return (
      <div className="rounded border border-short/30 bg-short/10 p-2.5 text-xs text-short">
        <AlertCircle className="mr-1 inline h-3 w-3" />
        <span className="font-mono">信号不存在</span>
        <span className="ml-1.5 text-foreground/80">（无法检索相似历史）</span>
      </div>
    )
  }

  const results = data.results ?? []
  const summary = data.summary ?? ''
  const fallback = !!data.fallback

  if (results.length === 0) {
    return (
      <div className="rounded border border-border bg-muted/30 p-2.5 text-xs text-muted-foreground">
        近 30d 没有同品种/同方向且因子重叠的历史事件
        {fallback && <span className="ml-1 font-mono">(归因表缺失，降级展示)</span>}
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-muted-foreground">
        <BookOpen className="h-3 w-3" />
        <span className="text-foreground/80">{summary}</span>
        {fallback && <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-700 dark:text-amber-300">降级</span>}
      </div>
      <ul className="space-y-1">
        {results.map((r, i) => {
          const dir = dirMeta(r.direction)
          const attrSummary = r.attribution?.summary ?? ''
          const sev = r.attribution?.severity_hint != null ? SEV_META[r.attribution.severity_hint] : null
          return (
            <li
              key={`${r.signal_id}-${i}`}
              className="rounded border border-border bg-card/40 px-2.5 py-1.5"
            >
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px]">
                <span className="font-semibold text-foreground/90">#{r.signal_id}</span>
                <span className="font-mono text-foreground/85">{r.asset}</span>
                <span className={`rounded border px-1 py-0.5 text-[11px] ${dir.cls}`}>{dir.label}</span>
                <span className="text-muted-foreground">{fmtTs(r.ts)}</span>
                <span className="text-muted-foreground/70">Δ {r.time_delta_h >= 0 ? '−' : '+'}{Math.abs(r.time_delta_h).toFixed(1)}h</span>
                <span className={`ml-auto font-semibold tabular-nums ${scoreColor(r.similarity_score)}`}>
                  {(r.similarity_score * 100).toFixed(1)}%
                </span>
              </div>
              {attrSummary && (
                <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-foreground/80" title={attrSummary}>
                  {attrSummary}
                </p>
              )}
              {sev && (
                <span className={`mt-1 inline-block rounded border px-1.5 py-0.5 font-mono text-[11px] ${sev.cls}`}>
                  {sev.label}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ── 详情抽屉 ──────────────────────────────────────────────────────────
function AbnormalDetailDrawer({ signal, refreshKey }: { signal: AbnormalSignal; refreshKey: number }) {
  const factors = signal.detail?.factors ?? []
  const div = divergenceFromDetail(signal.detail)
  // refreshKey 跟随主面板 30s 刷新递增：强制子组件 unmount/remount，重新拉取数据，
  // 避免抽屉里看到 stale 数据（attribution 可能刚生成完）。
  return (
    <div className="border-t border-border bg-background/40 px-3 py-3">
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="rounded border border-primary/30 bg-primary/5 p-2.5">
          <div className="mb-2 flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-primary">
            <Sparkles className="h-3 w-3" />AI 归因（v182.3）
          </div>
          <AttributionCard key={`attr-${signal.id}-${refreshKey}`} signalId={signal.id} />
        </div>
        <div className="rounded border border-blue-500/30 bg-blue-500/5 p-2.5">
          <div className="mb-2 flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-blue-700 dark:text-blue-300">
            <History className="h-3 w-3" />相似历史（KB · 近 30d）
          </div>
          <SimilarList key={`sim-${signal.id}-${refreshKey}`} signalId={signal.id} />
        </div>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5 font-mono text-[11px] text-muted-foreground/85">
        {div && (
          <span
            className={`rounded border px-1.5 py-0.5 font-semibold ${div.meta.cls}`}
            title={div.key.startsWith('rsi_') ? `RSI 背离方向：${signal.detail?.rsi_divergence}` : `量价 背离方向：${signal.detail?.volume_divergence || ''}`}
          >
            {div.meta.label}
          </span>
        )}
        <span className="shrink-0">因子标签：</span>
        {factors.map((f) => (
          // factor 是字符串，用 f 作 key 避免数组下标导致的 unmount/remount
          <span key={f} className="rounded bg-primary/15 px-1.5 py-0.5 text-primary">
            {f}
          </span>
        ))}
      </div>
    </div>
  )
}

// ── 主面板 ────────────────────────────────────────────────────────────
export function AbnormalDetailPanel() {
  const [signals, setSignals] = useState<AbnormalSignal[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  // 每次列表刷新成功就 +1；传给 drawer 强制 AttributionCard/SimilarList 重新拉取
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    let alive = true
    let ctrl: AbortController | null = null
    const load = () => {
      // 取消上一轮未完成的请求，避免旧响应覆盖新数据
      if (ctrl) ctrl.abort()
      ctrl = new AbortController()
      // 直接走 fetchWithFallback (内部先试主服务再退桥接)
      fetchWithFallback<{ signals?: AbnormalSignal[] }>(
        '/abnormal-signals?signal_kind=multi_factor&status=all&limit=12',
      )
        .then((d) => {
          if (!alive) return
          const list = Array.isArray(d.signals) ? (d.signals as AbnormalSignal[]) : []
          setSignals(list)
          setExpandedId((cur) => (cur != null && !list.find((s) => s.id === cur) ? null : cur))
          setErr(null)
          setRefreshKey((k) => k + 1)
        })
        .catch((e) => {
          if (!alive || e?.name === 'AbortError') return
          setErr(String(e?.message || e))
        })
        .finally(() => { if (alive) setLoading(false) })
    }
    void load()
    const t = window.setInterval(load, 30_000)
    return () => { alive = false; window.clearInterval(t); if (ctrl) ctrl.abort() }
  }, [])

  const sorted = useMemo(() => {
    return [...signals].sort((a, b) => (b.ts || 0) - (a.ts || 0))
  }, [signals])

  return (
    <div className="rounded border border-border bg-card p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
          🧬 异常签名详情 · AI 归因 + 相似历史（multi_factor）
        </h3>
        <span className="font-mono text-[11px] text-muted-foreground/70">
          自动 30s 刷新 · 点行展开
        </span>
      </div>

      {loading ? (
        <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />加载异常签名…
        </div>
      ) : err ? (
        <div className="rounded border border-short/30 bg-short/10 p-2 text-xs text-short">
          <AlertCircle className="mr-1 inline h-3 w-3" />异动接口异常：{err}
        </div>
      ) : sorted.length === 0 ? (
        <div className="rounded border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
          最近没有 multi_factor 异常签名（阈值未命中 或 检测器尚未启动）
        </div>
      ) : (
        <ul className="space-y-1.5">
          {sorted.slice(0, 10).map((s) => {
            const dir = dirMeta(s.direction)
            const factors = s.detail?.factors ?? []
            const div = divergenceFromDetail(s.detail)
            const { short: eventShort, full: eventFull } = summarizeFactors(factors)
            const isOpen = expandedId === s.id
            return (
              <li key={s.id} className="rounded border border-border bg-card/40">
                <button
                  type="button"
                  onClick={() => setExpandedId(isOpen ? null : s.id)}
                  aria-expanded={isOpen}
                  className="flex w-full flex-col items-stretch gap-1 px-2.5 py-1.5 text-left transition-colors hover:bg-card"
                >
                  {/* 第一行：信号 ID / 品种 / 方向 / 时间 + 事件简写（hover 看全称）+ 状态 */}
                  <div className="flex w-full flex-wrap items-center gap-x-2 gap-y-1">
                    {isOpen
                      ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
                    <span className="font-mono text-[11px] font-semibold text-foreground/90">#{s.id}</span>
                    <span className="font-mono text-[11px] font-semibold text-foreground">{s.asset}</span>
                    <span className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${dir.cls}`}>
                      {dir.label}
                    </span>
                    <span className="font-mono text-[11px] text-muted-foreground">{fmtTs(s.ts)}</span>
                    {eventFull && (
                      <span
                        className="max-w-[180px] truncate rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-foreground/90"
                        title={`事件全称：${eventFull}${s.hint ? `\n\n${s.hint}` : ''}`}
                      >
                        ⚡ {eventShort}
                      </span>
                    )}
                    <span
                      className={`ml-auto rounded px-1.5 py-0.5 font-mono text-[11px] ${
                        s.status === 'open'
                          ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
                          : 'bg-muted/40 text-muted-foreground'
                      }`}
                    >
                      {s.status}
                    </span>
                  </div>
                  {/* 第二行：背离类型标签（顶/底背离）—— 这是用户主要痛点，原来只显示"量能异动" */}
                  <div className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 pl-5">
                    {div ? (
                      <span
                        className={`rounded border px-1.5 py-0.5 font-mono text-[11px] font-semibold ${div.meta.cls}`}
                        title={
                          (s.detail?.rsi_divergence && div.key.startsWith('rsi_'))
                            ? `RSI 背离方向：${s.detail.rsi_divergence}`
                            : `量价 背离方向：${s.detail?.volume_divergence || ''}`
                        }
                      >
                        {div.meta.label}
                      </span>
                    ) : (
                      <span className="rounded border border-border bg-muted/30 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                        无背离
                      </span>
                    )}
                    {factors.length > 0 && (
                      <span
                        className="truncate font-mono text-[11px] text-muted-foreground/70"
                        title={factors.join(' · ')}
                      >
                        {factors.slice(0, 3).join(' · ')}{factors.length > 3 ? ` · +${factors.length - 3}` : ''}
                      </span>
                    )}
                  </div>
                </button>
                {isOpen && <AbnormalDetailDrawer signal={s} refreshKey={refreshKey} />}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export default AbnormalDetailPanel
