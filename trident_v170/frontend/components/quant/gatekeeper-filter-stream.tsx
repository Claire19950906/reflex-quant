'use client'

import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, ShieldCheck, ShieldX, Clock } from 'lucide-react'
import { API_BASE } from '@/lib/api'
import { cn } from '@/lib/utils'

type FilterItem = {
  id: number
  source: string
  content_preview: string
  timestamp: string
  relevance_score: number
  status: string
}

type FilterStream = {
  passed: FilterItem[]
  filtered: FilterItem[]
  counts: {
    passed_today: number
    filtered_today: number
    passed_total: number
    filtered_total: number
  }
  today: string
  generated_at: string
}

// 紧凑显示：源标签 + 内容预览 + 时间 + 分数
const FilterRow = memo(function FilterRow({ item, kind }: { item: FilterItem; kind: 'passed' | 'filtered' }) {
  const ts = (item.timestamp || '').slice(11, 16) // HH:MM
  const isPassed = kind === 'passed'
  return (
    <li
      className={cn(
        'flex items-start gap-1.5 px-2 py-1 text-[10px] leading-snug',
        'border-b border-border/40 last:border-b-0',
      )}
      title={`[${item.source}] ${item.content_preview}\nrelevance=${item.relevance_score} status=${item.status}`}
    >
      <span
        className={cn(
          'mt-0.5 shrink-0 rounded px-1 py-px font-mono text-[8px]',
          isPassed ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400',
        )}
      >
        {item.source.slice(0, 4)}
      </span>
      <span className="line-clamp-2 flex-1 text-muted-foreground/80">{item.content_preview}</span>
      <span className="mt-0.5 shrink-0 font-mono text-[9px] tabular-nums text-muted-foreground/60">
        {ts || '--:--'}
      </span>
    </li>
  )
})

function shortTime(value?: string): string {
  if (!value) return ''
  return value.slice(11, 19) // HH:MM:SS
}

export function GatekeeperFilterStream() {
  const [data, setData] = useState<FilterStream | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<boolean>(true)
  const lastFetch = useRef<number>(0)

  // 读 localStorage 折叠状态
  useEffect(() => {
    try {
      const v = window.localStorage.getItem('trident.gate_stream.expanded')
      if (v === '0') setExpanded(false)
      else if (v === '1') setExpanded(true)
    } catch {}
  }, [])

  const toggle = () => {
    setExpanded((prev) => {
      const next = !prev
      try { window.localStorage.setItem('trident.gate_stream.expanded', next ? '1' : '0') } catch {}
      return next
    })
  }

  const fetchData = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/events/filter-stream?limit=20`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const j = (await r.json()) as Partial<FilterStream>
      // shape 防御: 后端字段缺失 / 旧版本 / 错误页 HTML 解析时回退到空 shape
      setData({
        passed: Array.isArray(j.passed) ? j.passed : [],
        filtered: Array.isArray(j.filtered) ? j.filtered : [],
        counts: j.counts ?? { passed_today: 0, filtered_today: 0, passed_total: 0, filtered_total: 0 },
        today: j.today ?? '',
        generated_at: j.generated_at ?? '',
      })
      setErr(null)
      lastFetch.current = Date.now()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'fetch failed')
      // 不要 setData(null) — 保留上次成功的 UI, 只把错误标出来
    }
  }, [])

  useEffect(() => {
    void fetchData()
    const t = setInterval(fetchData, 15_000)
    return () => clearInterval(t)
  }, [fetchData])

  const counts = data?.counts
  const ageSec = data ? Math.max(0, Math.round((Date.now() - lastFetch.current) / 1000)) : null

  return (
    <div
      className="border-b border-border bg-secondary/30 px-3 py-2"
      aria-label="门卫筛选双流"
    >
      {/* 双列标题：左 通过 / 右 筛掉 + 折叠按钮 + 15s 轮询时间 */}
      <div className="flex items-center justify-between gap-3 text-[10px]">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={toggle}
            aria-label={expanded ? '折叠门卫筛选流' : '展开门卫筛选流'}
            title={expanded ? '收起门卫筛选流' : '展开门卫筛选流'}
            className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted/40"
          >
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
          <div className="flex items-center gap-1.5">
            <ShieldCheck className="h-3 w-3 text-emerald-400" aria-hidden="true" />
            <span className="font-mono uppercase tracking-wider text-emerald-400">通过</span>
            <span className="font-mono tabular-nums text-muted-foreground">
              今日 {counts?.passed_today ?? 0}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <ShieldX className="h-3 w-3 text-rose-400" aria-hidden="true" />
            <span className="font-mono uppercase tracking-wider text-rose-400">筛掉</span>
            <span className="font-mono tabular-nums text-muted-foreground">
              今日 {counts?.filtered_today ?? 0}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-1 font-mono text-[9px] text-muted-foreground/60">
          <Clock className="h-2.5 w-2.5" aria-hidden="true" />
          <span>
            15s 轮询{ageSec !== null ? ` · ${ageSec}s` : ''}
            {err ? ` · ${err}` : ''}
          </span>
        </div>
      </div>
      {/* 双列流：左侧通过（绿色）/ 右侧筛掉（红色），各自 max-h 120px 滚动 — 可折叠 */}
      {expanded && (
        <div className="mt-1.5 grid grid-cols-2 gap-2">
          <div className="rounded border border-emerald-500/20 bg-emerald-500/[0.03]">
            <ol className="max-h-[120px] overflow-y-auto thin-scroll">
              {(data?.passed ?? []).slice(0, 12).map((it) => (
                <FilterRow key={`p-${it.id}`} item={it} kind="passed" />
              ))}
              {(data?.passed?.length ?? 0) === 0 && (
                <li className="px-2 py-1 text-[10px] text-muted-foreground/60">暂无</li>
              )}
            </ol>
          </div>
          <div className="rounded border border-rose-500/20 bg-rose-500/[0.03]">
            <ol className="max-h-[120px] overflow-y-auto thin-scroll">
              {(data?.filtered ?? []).slice(0, 12).map((it) => (
                <FilterRow key={`f-${it.id}`} item={it} kind="filtered" />
              ))}
              {(data?.filtered?.length ?? 0) === 0 && (
                <li className="px-2 py-1 text-[10px] text-muted-foreground/60">暂无</li>
              )}
            </ol>
          </div>
        </div>
      )}
    </div>
  )
}