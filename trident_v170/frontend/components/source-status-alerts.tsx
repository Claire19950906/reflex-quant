'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE, fetchWithFallback } from '@/lib/api'

// 信息源异常 toast（全局常驻，不影响 ai_worker / 任何模型推理）
//
// 行为约定：
//   - 每 30s 拉 /api/sources/alerts —— 后端只列"持续异常"（连续失败 ≥2 或 stale ≥15min）
//   - 每次新出现的异常 source 弹一个固定 toast，**必须用户手动点"我知道了"才消失**
//   - 已点掉的 alert 写入 localStorage（24h 缓存），避免刷新后旧异常反复弹
//   - 同一条 alert 用 `${source_id}#${last_fail_ts}` 做唯一 key（fail_ts 变化 = 新一轮失败 = 再弹一次）
//   - 多个 toast 垂直堆叠在右上角，**纯前端组件，对后端 / ai_worker / 模型推理 0 影响**
//   - 后端 API 失败也不会弹（避免自身卡顿引发级联）

type AlertItem = {
  source_id: string
  category: string
  label: string
  severity: 'error' | 'stale'
  consecutive_failures: number
  last_fail_ts: number | null
  secs_since_fail: number | null
  last_error: string
}

const SEEN_KEY = 'trident_source_alerts_seen_v1'
const SEEN_TTL_S = 86400

function loadSeen(): Record<string, number> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(SEEN_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    const cutoff = Date.now() / 1000 - SEEN_TTL_S
    const out: Record<string, number> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof v === 'number' && v > cutoff) out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

function saveSeen(seen: Record<string, number>) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(SEEN_KEY, JSON.stringify(seen))
  } catch {
    /* quota exceeded 等忽略 */
  }
}

function alertKey(it: AlertItem): string {
  return `${it.source_id}#${Math.round(it.last_fail_ts ?? 0)}`
}

function fmtClock(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtAgo(secs: number | null): string {
  if (secs == null) return ''
  if (secs < 60) return `${Math.round(secs)}秒前`
  if (secs < 3600) return `${Math.round(secs / 60)}分钟前`
  return `${(secs / 3600).toFixed(1)}小时前`
}

const CATEGORY_COLOR: Record<string, string> = {
  行情: 'border-l-blue-500',
  宏观: 'border-l-violet-500',
  新闻: 'border-l-emerald-500',
  引擎: 'border-l-rose-500',
}

const SEVERITY_STYLE: Record<string, { badge: string; ring: string; icon: string }> = {
  error: { badge: 'bg-red-500/15 text-red-600 dark:text-red-400', ring: 'ring-red-500/30', icon: '⚠' },
  stale: { badge: 'bg-amber-500/15 text-amber-600 dark:text-amber-400', ring: 'ring-amber-500/30', icon: '⏱' },
}

export function SourceStatusAlerts() {
  const [alerts, setAlerts] = useState<AlertItem[]>([])
  const [dismissed, setDismissed] = useState<Record<string, number>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  useEffect(() => {
    setDismissed(loadSeen())
  }, [])

  const fetchAlerts = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/sources/alerts`, { cache: 'no-store' })
      let d: any
      if (!r.ok) {
        // 8000 一直 500,fallback 到 8100 bridge
        d = await fetchWithFallback<{ items?: AlertItem[]; alerts?: AlertItem[] }>('/sources/alerts')
      } else {
        d = await r.json()
      }
      const d2 = d as { items?: AlertItem[]; alerts?: AlertItem[] }
      // 8000 用 items, 8100 bridge 用 alerts
      const items = Array.isArray(d2.items) ? d2.items : (Array.isArray(d2.alerts) ? d2.alerts : [])
      setAlerts(items)
    } catch {
      /* silent */
    }
  }, [])

  useEffect(() => {
    void fetchAlerts()
    const t = window.setInterval(fetchAlerts, 30_000)
    return () => window.clearInterval(t)
  }, [fetchAlerts])

  const dismiss = useCallback((key: string) => {
    setDismissed((prev) => {
      const next = { ...prev, [key]: Date.now() / 1000 }
      saveSeen(next)
      return next
    })
  }, [])

  const toggle = useCallback((key: string) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))
  }, [])

  const visibleAlerts = alerts.filter((it) => {
    const k = alertKey(it)
    const ack = dismissed[k]
    if (!ack) return true
    return (it.last_fail_ts ?? 0) > ack
  })

  if (visibleAlerts.length === 0) return null

  return (
    <div
      className="pointer-events-none fixed right-3 top-16 z-[200] flex w-[380px] max-w-[calc(100vw-1.5rem)] flex-col gap-2"
      data-testid="source-alerts-container"
    >
      {visibleAlerts.map((it) => {
        const k = alertKey(it)
        const sev = SEVERITY_STYLE[it.severity] ?? SEVERITY_STYLE.error
        const catColor = CATEGORY_COLOR[it.category] ?? 'border-l-slate-500'
        const isOpen = !!expanded[k]
        return (
          <div
            key={k}
            className={`pointer-events-auto surface-card border-l-4 ${catColor} ring-1 ${sev.ring}`}
            data-testid="source-alert"
          >
            <button
              type="button"
              onClick={() => toggle(k)}
              className="flex w-full items-start gap-2 px-3 py-2 text-left"
            >
              <span
                className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${sev.badge}`}
              >
                {sev.icon}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="truncate font-mono text-xs font-semibold">
                    {it.label || it.source_id}
                  </span>
                  <span className={`shrink-0 rounded px-1 py-0.5 font-mono text-[9px] ${sev.badge}`}>
                    {it.severity === 'error'
                      ? `连续失败 ${it.consecutive_failures} 次`
                      : '离线/过期'}
                  </span>
                </div>
                <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                  {it.category} · 失败于 {fmtClock(it.last_fail_ts)} · {fmtAgo(it.secs_since_fail)}
                </div>
                {isOpen && it.last_error && (
                  <div className="mt-1.5 break-words rounded bg-red-500/5 px-2 py-1 font-mono text-[10px] text-red-700 dark:text-red-400">
                    {it.last_error}
                  </div>
                )}
              </div>
              <span className="ml-1 shrink-0 text-[10px] text-muted-foreground">
                {isOpen ? '▾' : '▸'}
              </span>
            </button>
            <div className="flex items-center justify-between gap-2 border-t border-border/50 px-3 py-1.5">
              <a
                href="/sources"
                target="_self"
                className="font-mono text-[10px] text-muted-foreground underline-offset-2 hover:underline"
              >
                查看全部信息源 →
              </a>
              <button
                type="button"
                onClick={() => dismiss(k)}
                className="rounded bg-emerald-600 px-2 py-0.5 font-mono text-[10px] font-medium text-white hover:bg-emerald-700"
                data-testid="dismiss-alert"
              >
                ✅ 我知道了
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}

export default SourceStatusAlerts