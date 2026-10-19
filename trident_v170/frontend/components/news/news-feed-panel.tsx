// components/news/news-feed-panel.tsx - P0-3 news feed (2026-10-18)
// Data: bridge_server.py /api/news/recent (TECHFLOW + EASTMONEY RSS mirror)
// Auto-poll 60s, manual pull POST /api/news/pull-now

'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw, Loader2, Radio } from 'lucide-react'
import { apiGet, BRIDGE_BASE } from '@/lib/api'

type NewsItem = {
  id: number
  source: string
  headline: string
  body: string
  action: string
  score: number
  assets: string
  ts: number
  news_id: string
  fetched_at: number
}

type NewsRecent = {
  ok: boolean
  n: number
  by_source: Record<string, number>
  news: NewsItem[]
}

type PullResult = {
  ok: boolean
  total?: number
  inserted?: number
  skipped?: number
  sources?: Record<string, number>
  errors?: string[]
  elapsed_s?: number
}

const SOURCE_META: Record<string, { label: string; color: string; bg: string; border: string }> = {
  techflow: { label: 'TechFlow', color: 'text-cyan-400', bg: 'bg-cyan-500/10', border: 'border-cyan-500/40' },
  eastmoney: { label: 'DongFangCaiFu', color: 'text-amber-400', bg: 'bg-amber-500/10', border: 'border-amber-500/40' },
  blockbeats: { label: 'BlockBeats', color: 'text-purple-400', bg: 'bg-purple-500/10', border: 'border-purple-500/40' },
}

const POPULAR_ASSETS = ['BTC', 'ETH', 'SOL', 'WLD', 'MU', 'NVDA', 'TSLA', 'AAPL', 'MSFT', 'META', 'GOOG', 'AMZN']

function fmtTime(ts: number): string {
  const now = Date.now() / 1000
  const diff = now - ts
  if (diff < 60) return Math.round(diff) + 's ago'
  if (diff < 3600) return Math.round(diff / 60) + 'm ago'
  if (diff < 86400) return Math.round(diff / 3600) + 'h ago'
  const d = new Date(ts * 1000)
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function ActionBadge({ action }: { action: string }) {
  const cls = {
    BUY: 'border-long/40 bg-long/10 text-long',
    SELL: 'border-short/40 bg-short/10 text-short',
    HOLD: 'border-hold/40 bg-hold/10 text-hold',
  }[action] || 'border-border bg-secondary text-muted-foreground'
  return (
    <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-bold ${cls}`}>
      {action || 'INFO'}
    </span>
  )
}

function SentimentBadge({ score }: { score: number }) {
  if (!score || Math.abs(score) < 0.1) {
    return (
      <span className="shrink-0 rounded-md border border-border bg-secondary px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
        · 0.0
      </span>
    )
  }
  const strong = Math.abs(score) >= 0.7
  const cls = score > 0
    ? (strong
        ? 'border-emerald-500/60 bg-emerald-500/15 text-emerald-300'
        : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400')
    : (strong
        ? 'border-rose-500/60 bg-rose-500/15 text-rose-300'
        : 'border-rose-500/40 bg-rose-500/10 text-rose-400')
  const arrow = score > 0 ? '▲' : '▼'
  return (
    <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-bold ${cls}`}>
      {arrow} {score > 0 ? '+' : ''}{score.toFixed(2)}
    </span>
  )
}

function AssetBadge({ asset }: { asset: string }) {
  return (
    <span className="shrink-0 rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
      {asset}
    </span>
  )
}

export function NewsFeedPanel({ hours = 24, limit = 50 }: { hours?: number; limit?: number }) {
  const [data, setData] = useState<NewsRecent | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sourceFilter, setSourceFilter] = useState<string>('')
  const [assetFilter, setAssetFilter] = useState<string>('')
  const [pulling, setPulling] = useState(false)
  const [pullMsg, setPullMsg] = useState<string>('')

  const load = useCallback(async () => {
    setError(null)
    try {
      const params = new URLSearchParams()
      params.set('hours', String(hours))
      params.set('limit', String(limit))
      if (sourceFilter) params.set('source', sourceFilter)
      if (assetFilter) params.set('asset', assetFilter)
      const d = await apiGet<NewsRecent>('/news/recent?' + params.toString())
      if (d.ok) {
        setData(d)
      } else {
        setError('API returned ok=false')
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [hours, limit, sourceFilter, assetFilter])

  useEffect(() => {
    void load()
    const t = window.setInterval(load, 60000)
    return () => window.clearInterval(t)
  }, [load])

  const pullNow = useCallback(async () => {
    setPulling(true)
    setPullMsg('')
    try {
      const r = await fetch(BRIDGE_BASE + '/news/pull-now', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      const d: PullResult = await r.json()
      if (d.ok) {
        setPullMsg('OK ' + d.inserted + ' insert / ' + d.skipped + ' skip (' + (d.elapsed_s?.toFixed(1) || '?') + 's)')
        await load()
      } else {
        setPullMsg('X fail: ' + (d.errors?.join('; ') || 'unknown'))
      }
    } catch (e) {
      setPullMsg('X ' + String(e))
    } finally {
      setPulling(false)
      window.setTimeout(() => setPullMsg(''), 5000)
    }
  }, [load])

  const byAsset = useMemo(() => {
    const m: Record<string, number> = {}
    const items = data?.news || []
    items.forEach((n) => {
      if (n.assets) n.assets.split(',').forEach((a) => {
        const x = a.trim()
        if (x) m[x] = (m[x] || 0) + 1
      })
    })
    return Object.entries(m).sort((a, b) => b[1] - a[1]).slice(0, 12)
  }, [data])

  const sentimentStats = useMemo(() => {
    const items = data?.news || []
    const dist = { strongBull: 0, bull: 0, neutral: 0, bear: 0, strongBear: 0 }
    items.forEach((n) => {
      const s = n.score || 0
      if (s >= 0.7) dist.strongBull++
      else if (s >= 0.1) dist.bull++
      else if (s <= -0.7) dist.strongBear++
      else if (s <= -0.1) dist.bear++
      else dist.neutral++
    })
    const top = {
      bull: [...items].filter((n) => (n.score || 0) > 0).sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 3),
      bear: [...items].filter((n) => (n.score || 0) < 0).sort((a, b) => (a.score || 0) - (b.score || 0)).slice(0, 3),
    }
    return { dist, top }
  }, [data])

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Radio className="h-3.5 w-3.5 text-emerald-400" />
        <h2 className="font-mono text-sm uppercase tracking-widest text-muted-foreground">新闻流</h2>
        <span className="font-mono text-[10px] text-muted-foreground">
          最近 {hours}h · {data?.n ?? '—'} 条
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {pullMsg && (
            <span className="font-mono text-[10px] text-muted-foreground">{pullMsg}</span>
          )}
          <button
            onClick={pullNow}
            disabled={pulling}
            className="flex items-center gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 font-mono text-[10px] text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-50"
          >
            {pulling ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            立即拉取
          </button>
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-md border border-border bg-secondary px-2.5 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[10px] text-muted-foreground">来源:</span>
        {['', 'techflow', 'eastmoney'].map((s) => (
          <button
            key={s || 'all'}
            onClick={() => setSourceFilter(s)}
            className={`rounded-md border px-2 py-0.5 font-mono text-[10px] transition-colors ${
              sourceFilter === s
                ? 'border-primary bg-primary/10 text-primary'
                : 'border-border bg-secondary text-muted-foreground hover:text-foreground'
            }`}
          >
            {s ? SOURCE_META[s]?.label || s : '全部'}
          </button>
        ))}
        <span className="ml-2 font-mono text-[10px] text-muted-foreground">资产:</span>
        {['', ...POPULAR_ASSETS].map((a) => (
          <button
            key={a || 'any'}
            onClick={() => setAssetFilter(a)}
            className={`rounded-md border px-2 py-0.5 font-mono text-[10px] transition-colors ${
              assetFilter === a
                ? 'border-amber-500/50 bg-amber-500/10 text-amber-400'
                : 'border-border bg-secondary text-muted-foreground hover:text-foreground'
            }`}
          >
            {a || '任意'}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_240px]">
        <div className="thin-scroll max-h-[680px] overflow-y-auto rounded-lg border border-border bg-card">
          {loading && !data ? (
            <div className="flex h-32 items-center justify-center font-mono text-[11px] text-muted-foreground">
              <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />加载中…
            </div>
          ) : error ? (
            <div className="flex h-32 items-center justify-center font-mono text-[11px] text-short">
              {error}
            </div>
          ) : !data?.news.length ? (
            <div className="flex h-32 items-center justify-center font-mono text-[11px] text-muted-foreground">
              无匹配新闻
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {data.news.map((n) => {
                const sm = SOURCE_META[n.source] || { label: n.source, color: 'text-muted-foreground', bg: 'bg-secondary', border: 'border-border' }
                const assetList = n.assets ? n.assets.split(',').map((s) => s.trim()).filter(Boolean) : []
                return (
                  <li key={n.source + '-' + n.news_id} className="group p-3 transition-colors hover:bg-secondary/30">
                    <div className="mb-1.5 flex items-center gap-2">
                      <span className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-bold ${sm.border} ${sm.bg} ${sm.color}`}>
                        {sm.label}
                      </span>
                      <ActionBadge action={n.action} />
                      <SentimentBadge score={n.score} />
                      {assetList.slice(0, 4).map((a) => <AssetBadge key={a} asset={a} />)}
                      <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground">
                        {fmtTime(n.ts)}
                      </span>
                    </div>
                    <div className="line-clamp-2 font-mono text-[12px] leading-relaxed text-foreground/90 group-hover:text-foreground">
                      {n.headline}
                    </div>
                    {n.body && n.body !== n.headline && (
                      <div className="mt-1 line-clamp-2 font-mono text-[10.5px] leading-relaxed text-muted-foreground">
                        {n.body}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <aside className="flex flex-col gap-2">
          <div className="rounded-lg border border-border bg-card p-3">
            <h3 className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">by source</h3>
            {data?.by_source ? (
              <ul className="space-y-1">
                {Object.entries(data.by_source).map(([s, n]) => {
                  const sm = SOURCE_META[s] || { label: s, color: 'text-foreground', border: 'border-border', bg: 'bg-secondary' }
                  return (
                    <li key={s} className="flex items-center justify-between font-mono text-[11px]">
                      <span className={`rounded border px-1.5 py-0.5 text-[9px] ${sm.border} ${sm.bg} ${sm.color}`}>
                        {sm.label}
                      </span>
                      <span className="text-foreground/90">{n}</span>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <div className="font-mono text-[10px] text-muted-foreground">—</div>
            )}
          </div>
          <div className="rounded-lg border border-border bg-card p-3">
            <h3 className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">by asset (top 12)</h3>
            {byAsset.length ? (
              <ul className="space-y-0.5">
                {byAsset.map(([a, n]) => (
                  <li key={a} className="flex items-center justify-between font-mono text-[11px]">
                    <span className="text-foreground/90">{a}</span>
                    <span className="text-muted-foreground">{n}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="font-mono text-[10px] text-muted-foreground">— 无资产标签</div>
            )}
          </div>
          <div className="rounded-lg border border-border bg-card p-3">
            <h3 className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">sentiment 分布</h3>
            <ul className="space-y-1 font-mono text-[10px]">
              <li className="flex items-center justify-between">
                <span className="text-emerald-300">强多</span>
                <span className="text-foreground/90">{sentimentStats.dist.strongBull}</span>
              </li>
              <li className="flex items-center justify-between">
                <span className="text-emerald-400">看多</span>
                <span className="text-foreground/90">{sentimentStats.dist.bull}</span>
              </li>
              <li className="flex items-center justify-between">
                <span className="text-muted-foreground">中性</span>
                <span className="text-foreground/90">{sentimentStats.dist.neutral}</span>
              </li>
              <li className="flex items-center justify-between">
                <span className="text-rose-400">看空</span>
                <span className="text-foreground/90">{sentimentStats.dist.bear}</span>
              </li>
              <li className="flex items-center justify-between">
                <span className="text-rose-300">强空</span>
                <span className="text-foreground/90">{sentimentStats.dist.strongBear}</span>
              </li>
            </ul>
            {sentimentStats.top.bull.length > 0 && (
              <div className="mt-2 border-t border-border pt-2">
                <h4 className="mb-1 font-mono text-[9px] uppercase tracking-widest text-emerald-400">▲ top bullish</h4>
                <ul className="space-y-0.5 font-mono text-[10px]">
                  {sentimentStats.top.bull.map((n) => (
                    <li key={'b' + n.id} className="line-clamp-1 text-foreground/80" title={n.headline}>
                      <span className="text-emerald-400">+{(n.score || 0).toFixed(2)}</span> {n.headline}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {sentimentStats.top.bear.length > 0 && (
              <div className="mt-2 border-t border-border pt-2">
                <h4 className="mb-1 font-mono text-[9px] uppercase tracking-widest text-rose-400">▼ top bearish</h4>
                <ul className="space-y-0.5 font-mono text-[10px]">
                  {sentimentStats.top.bear.map((n) => (
                    <li key={'r' + n.id} className="line-clamp-1 text-foreground/80" title={n.headline}>
                      <span className="text-rose-400">{(n.score || 0).toFixed(2)}</span> {n.headline}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </aside>
      </div>
    </section>
  )
}
