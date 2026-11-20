'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v385 记忆检索 — 把「语义记忆层」摊开给人看：哪些历史教训会被注入 prompt
// 与 /api/memory/search 同一套索引，所以这里看到的召回就是模型看到的

type Item = {
  doc_id: string
  source: string
  asset: string
  event_type: string
  ts: string
  similarity: number | null
  outcome: number | null
  text: string
}

type Stats = { ok: boolean; docs: number; vocab: number; by_source: Record<string, number> }

export default function MemoryPage() {
  const [q, setQ] = useState('非农超预期 黄金下跌')
  const [asset, setAsset] = useState('XAU')
  const [k, setK] = useState(5)
  const [items, setItems] = useState<Item[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadStats = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/v1/memory/stats`)
      setStats(await r.json())
    } catch { /* 统计失败不影响检索 */ }
  }, [])

  const search = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const r = await fetch(
        `${API_BASE}/v1/memory/search?q=${encodeURIComponent(q)}&asset=${encodeURIComponent(asset)}&k=${k}`,
      )
      const j = await r.json()
      if (!j?.ok) setError(j?.error || '检索失败')
      setItems(j?.items || [])
      if (j?.docs) setStats((s) => (s ? { ...s, docs: j.docs } : s))
    } catch (e) {
      setError(e instanceof Error ? e.message : '检索失败')
    } finally {
      setLoading(false)
    }
  }, [q, asset, k])

  useEffect(() => { void loadStats(); void search() }, [loadStats, search])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4">
        <h1 className="font-mono text-sm font-semibold tracking-wider">记忆检索</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          按相似度召回历史反思（同品种 / 同事件加权），召回结果就是分析时注入 prompt 的那几条。
        </p>
      </header>

      {stats?.ok && (
        <div className="mb-3 flex flex-wrap gap-1.5 font-mono text-[11px] text-muted-foreground">
          <span className="rounded bg-secondary px-2 py-0.5">记忆 {stats.docs} 条</span>
          <span className="rounded bg-secondary px-2 py-0.5">词表 {stats.vocab}</span>
          {Object.entries(stats.by_source || {}).map(([src, n]) => (
            <span key={src} className="rounded bg-secondary px-2 py-0.5">{src} {n}</span>
          ))}
        </div>
      )}

      <div className="surface-card p-3">
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void search() }}
            placeholder="当前新闻标题 / 正文，或想找的教训"
            className="w-full flex-1 rounded border border-border bg-background px-3 py-2 font-mono text-xs outline-none focus:border-primary"
          />
          <input
            value={asset}
            onChange={(e) => setAsset(e.target.value.toUpperCase())}
            placeholder="品种（可空）"
            className="w-full rounded border border-border bg-background px-3 py-2 font-mono text-xs outline-none focus:border-primary sm:w-28"
          />
          <select
            value={k}
            onChange={(e) => setK(Number(e.target.value))}
            className="rounded border border-border bg-background px-2 py-2 font-mono text-xs outline-none"
          >
            {[3, 5, 8, 10].map((n) => <option key={n} value={n}>top {n}</option>)}
          </select>
          <button
            type="button"
            onClick={() => void search()}
            disabled={loading}
            className="rounded bg-primary px-4 py-2 font-mono text-xs text-primary-foreground disabled:opacity-50"
          >
            {loading ? '检索中…' : '检索'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-3 surface-card flex items-start gap-2 p-3 text-muted-foreground">
          <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-hold/80" aria-hidden />
          <span className="min-w-0">
            <span className="text-xs">记忆检索暂不可用</span>
            <span className="block truncate font-mono text-[11px] opacity-70">{error}</span>
          </span>
        </div>
      )}

      <div className="mt-3 space-y-2">
        {items.map((it, i) => (
          <div key={`${it.doc_id}-${i}`} className="surface-card p-3">
            <div className="flex flex-wrap items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
              <span className="rounded bg-primary/15 px-1.5 py-0.5 text-foreground">M{i + 1}</span>
              <span>相似度 {Number(it.similarity ?? 0).toFixed(2)}</span>
              <span>· {it.asset || '任意品种'}</span>
              {it.event_type && <span>· {it.event_type}</span>}
              <span>· {it.source}</span>
              <span>· {String(it.ts || '').slice(0, 10)}</span>
              {it.outcome !== null && it.outcome !== undefined && (
                <span className={Number(it.outcome) > 0 ? 'text-emerald-500' : 'text-red-500'}>
                  · 结果 {Number(it.outcome) > 0 ? '+' : ''}{Number(it.outcome).toFixed(2)}%
                </span>
              )}
              <span className="ml-auto text-muted-foreground/60">{it.doc_id}</span>
            </div>
            <p className="mt-1.5 text-xs leading-relaxed">{it.text}</p>
          </div>
        ))}
        {!items.length && !loading && (
          <div className="surface-card p-6 text-xs text-muted-foreground">
            没有召回：语料里暂时没有相近的反思（查询词全是新术语时会退化为同品种近期教训）。
          </div>
        )}
      </div>
    </main>
  )
}
