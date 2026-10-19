'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { ChevronDown, ChevronUp, Clock, Globe } from 'lucide-react'

// v188+ 全球市场开闭市弹幕条 — WTI/XAU/BTC/美股 实时状态
// 默认展开;点击 chevron 可折叠为单行 (Globe + 时钟);折叠状态 localStorage 持久化
// 高度: 展开 24px / 折叠 24px(仅图标+时间)
type Market = {
  id: string
  name: string
  exchange: string
  tag: string
  locale_now: string
  status: 'open' | 'closed'
  sub_status: string
  minutes_to_next?: number | null
  next_event?: string
  active_sessions?: string[]
}

function fmtMins(m: number | null | undefined): string {
  if (m == null) return ''
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  const r = m % 60
  return r ? `${h}h${r}m` : `${h}h`
}

function colorByStatus(m: Market): string {
  if (m.status === 'closed') return 'bg-muted/40 text-muted-foreground border-border'
  if (m.sub_status === 'regular') return 'bg-long/15 text-long border-long/30'
  if (m.sub_status === 'premarket') return 'bg-yellow-500/15 text-yellow-600 border-yellow-500/30'
  if (m.sub_status === 'afterhours') return 'bg-blue-500/15 text-blue-500 border-blue-500/30'
  if (m.sub_status === 'always') return 'bg-primary/15 text-primary border-primary/30'
  return 'bg-long/10 text-long border-long/20'
}

function iconFor(m: Market): string {
  if (m.status === 'closed') return '⚫'
  if (m.sub_status === 'always') {
    const sess = m.active_sessions?.[0] || ''
    return sess === 'Asia' ? '🌏' : sess === 'Europe' ? '🌍' : sess === 'US' ? '🌎' : '🟢'
  }
  if (m.sub_status === 'regular') return '🟢'
  if (m.sub_status === 'premarket') return '🟡'
  if (m.sub_status === 'afterhours') return '🔵'
  return '🟢'
}

export function MarketHoursBar() {
  const [markets, setMarkets] = useState<Market[]>([])
  const [ts, setTs] = useState<number>(Date.now())
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<boolean>(true)
  // v189-8: mounted 标记 — hydration 后才挂载,避免时钟文本 SSR/CSR 不一致
  // 触发 React hydration error ("Text content did not match: 23:35:27 vs 23:35:29")
  // 导致 Next.js dev mode 弹 "1 error" 红 toast.
  const [mounted, setMounted] = useState<boolean>(false)

  // 读 localStorage 折叠状态（仅一次，避免水合不一致闪烁）
  useEffect(() => {
    try {
      const v = window.localStorage.getItem('trident.market_hours.expanded')
      if (v === '0') setExpanded(false)
      else if (v === '1') setExpanded(true)
    } catch {}
  }, [])

  const toggle = () => {
    setExpanded((prev) => {
      const next = !prev
      try { window.localStorage.setItem('trident.market_hours.expanded', next ? '1' : '0') } catch {}
      return next
    })
  }

  const load = () => {
    fetch(`${API_BASE}/v188/markets/status`, { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { ok: boolean; markets?: Market[]; error?: string }) => {
        if (d.ok && d.markets) {
          setMarkets(d.markets)
          setError(null)
        } else {
          setError(d.error || 'unknown')
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }

  useEffect(() => {
    load()
    const t1 = window.setInterval(load, 60_000)        // 60s 刷新
    const t2 = window.setInterval(() => setTs(Date.now()), 1000) // 1s 跳秒
    return () => {
      window.clearInterval(t1)
      window.clearInterval(t2)
    }
  }, [])

  // v189-8: hydration 后立即标记,允许客户端时钟渲染 (避免 SSR 渲染 23:35:27
  // 客户端渲染 23:35:29 触发 hydration mismatch)
  useEffect(() => {
    setMounted(true)
  }, [])

  if (error && markets.length === 0) {
    return (
      <div className="sticky top-16 z-10 flex h-6 items-center gap-1.5 overflow-hidden border-b border-border bg-card/60 px-3 font-mono text-[10px] text-muted-foreground">
        <Clock className="h-3 w-3" />
        <span>市场状态加载失败: {error}</span>
        <button
          type="button"
          onClick={toggle}
          aria-label={expanded ? '折叠市场状态条' : '展开市场状态条'}
          className="ml-auto inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted/40"
        >
          {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </button>
      </div>
    )
  }

  return (
    <div className="sticky top-16 z-10 flex h-6 items-center gap-1.5 overflow-hidden border-b border-border bg-card/60 px-3 font-mono text-[10px]">
      <button
        type="button"
        onClick={toggle}
        aria-label={expanded ? '折叠市场状态条' : '展开市场状态条'}
        title={expanded ? '收起市场状态条' : '展开市场状态条'}
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted/40"
      >
        {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>
      <Globe className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden="true" />
      {expanded && <span className="shrink-0 text-muted-foreground">全球市场</span>}
      {expanded && (
        <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto">
          {markets.map((m) => {
            const sub = m.sub_status === 'always'
              ? `24×7${m.active_sessions?.length ? ` · ${m.active_sessions.join('+')}` : ''}`
              : m.sub_status === 'regular' ? '盘中'
              : m.sub_status === 'premarket' ? '盘前'
              : m.sub_status === 'afterhours' ? '盘后'
              : m.sub_status === 'futures_day' ? '期货日盘'
              : m.sub_status === 'futures_overnight' ? '期货夜盘'
              : m.sub_status === 'futures_evening' ? '期货晚盘'
              : m.sub_status === 'weekend' ? '周末休'
              : m.sub_status === 'saturday' ? '周六休'
              : m.sub_status === 'sunday_before_open' ? '等待开盘'
              : m.sub_status === 'before_pre' ? '盘前等待'
              : m.sub_status === 'after_close' ? '盘后休'
              : m.sub_status
            const next = m.minutes_to_next != null && m.minutes_to_next > 0
              ? ` ▸ ${fmtMins(m.minutes_to_next)} 后${m.next_event || ''}`
              : ''
            return (
              <span
                key={m.id}
                className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 ${colorByStatus(m)}`}
                title={`${m.name} (${m.exchange}) · ${m.locale_now} · ${sub}${next}`}
              >
                <span className="text-[10px]">{iconFor(m)}</span>
                <span className="font-bold">{m.tag}</span>
                <span className="text-muted-foreground">{sub}</span>
                {next && <span className="text-muted-foreground">{next}</span>}
              </span>
            )
          })}
        </div>
      )}
      <span className="ml-auto shrink-0 tabular-nums text-muted-foreground" suppressHydrationWarning>
        {mounted ? new Date(ts).toLocaleTimeString('zh-CN', { hour12: false }) : ''}
      </span>
    </div>
  )
}