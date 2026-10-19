// components/quant/mover-news-card.tsx - P0-3 mover news (2026-10-18)
// 显示当前 selected asset 的最近新闻, 含 sentiment.
// Data: bridge_server.py /api/news/recent?asset=X

'use client'

import { useCallback, useEffect, useState } from 'react'
import { Loader2, Newspaper } from 'lucide-react'
import { apiGet } from '@/lib/api'

type NewsItem = {
  id: number
  source: string
  headline: string
  body: string
  score: number
  ts: number
}

type NewsRecent = {
  ok: boolean
  n: number
  news: NewsItem[]
}

const SOURCE_LABEL: Record<string, { label: string; cls: string }> = {
  techflow: { label: 'TF', cls: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-400' },
  eastmoney: { label: 'EM', cls: 'border-amber-500/40 bg-amber-500/10 text-amber-400' },
}

function fmtTime(ts: number): string {
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return Math.round(diff) + 's'
  if (diff < 3600) return Math.round(diff / 60) + 'm'
  if (diff < 86400) return Math.round(diff / 3600) + 'h'
  return Math.round(diff / 86400) + 'd'
}

function SentChip({ score }: { score: number }) {
  const s = score || 0
  if (Math.abs(s) < 0.1) {
    return <span className="font-mono text-[9px] text-muted-foreground">·0</span>
  }
  const cls = s > 0
    ? (s >= 0.7 ? 'text-emerald-300' : 'text-emerald-400')
    : (s <= -0.7 ? 'text-rose-300' : 'text-rose-400')
  const arrow = s > 0 ? '▲' : '▼'
  return <span className={`font-mono text-[9px] font-bold ${cls}`}>{arrow}{s.toFixed(2)}</span>
}

export function MoverNewsCard({ asset }: { asset: string | null }) {
  const [data, setData] = useState<NewsRecent | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!asset) {
      setData(null)
      return
    }
    setLoading(true)
    setErr(null)
    try {
      const d = await apiGet<NewsRecent>(`/news/recent?asset=${asset}&hours=72&limit=5`)
      if (d.ok) setData(d)
      else setErr('API ok=false')
    } catch (e) {
      setErr(String(e))
    } finally {
      setLoading(false)
    }
  }, [asset])

  useEffect(() => {
    void load()
    if (!asset) return
    const t = window.setInterval(load, 90000)  // 1.5min
    return () => window.clearInterval(t)
  }, [load, asset])

  if (!asset) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-card/40 p-3 font-mono text-[10px] text-muted-foreground">
        <div className="mb-1 flex items-center gap-1.5">
          <Newspaper className="h-3 w-3" />
          <span className="uppercase tracking-widest">📰 相关新闻</span>
        </div>
        点左侧任一资产,显示最近 72h 新闻 + sentiment
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="mb-2 flex items-center gap-1.5">
        <Newspaper className="h-3 w-3 text-amber-400" />
        <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          📰 相关新闻
        </h3>
        <span className="font-mono text-[10px] text-foreground/80">{asset}</span>
        {data && (
          <span className="ml-auto font-mono text-[9px] text-muted-foreground">
            72h · {data.n}
          </span>
        )}
      </div>
      {loading && !data ? (
        <div className="flex h-16 items-center justify-center font-mono text-[10px] text-muted-foreground">
          <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />加载中…
        </div>
      ) : err ? (
        <div className="font-mono text-[10px] text-rose-400">err: {err}</div>
      ) : !data || data.n === 0 ? (
        <div className="font-mono text-[10px] text-muted-foreground">
          72h 内无匹配新闻 (asset={asset})
        </div>
      ) : (
        <ul className="space-y-1.5">
          {data.news.map((n) => {
            const sm = SOURCE_LABEL[n.source] || { label: n.source.slice(0, 2).toUpperCase(), cls: 'border-border bg-secondary text-muted-foreground' }
            return (
              <li key={n.source + '-' + n.id} className="group">
                <div className="mb-0.5 flex items-center gap-1.5">
                  <span className={`shrink-0 rounded border px-1 py-0 font-mono text-[8px] font-bold ${sm.cls}`}>
                    {sm.label}
                  </span>
                  <SentChip score={n.score} />
                  <span className="ml-auto shrink-0 font-mono text-[9px] text-muted-foreground">
                    {fmtTime(n.ts)}
                  </span>
                </div>
                <div className="line-clamp-2 font-mono text-[10.5px] leading-snug text-foreground/90 group-hover:text-foreground" title={n.headline}>
                  {n.headline}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
