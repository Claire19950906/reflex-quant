'use client'

import { useEffect, useState } from 'react'
import { X, Bell, AlertTriangle, Network, Activity, History } from 'lucide-react'

/**
 * v276+: in-app 未读异动详情 modal。
 * 用户点 banner 红点 → 弹出 → 看每个 section 的真实 top N 异动 → 一键已读。
 */

const SECTION_META: Array<{
  key: string
  label: string
  icon: typeof Bell
  headerCls: string
  iconCls: string
  badgeCls: string
}> = [
  {
    key: 'fed_anomalies',
    label: 'Fed 利率异动',
    icon: AlertTriangle,
    headerCls: 'border-amber-500/40 bg-amber-500/10',
    iconCls: 'text-amber-500',
    badgeCls: 'border-amber-500/40 bg-amber-500/15 text-amber-600',
  },
  {
    key: 'cross_asset_high',
    label: '跨资产高严重度',
    icon: Network,
    headerCls: 'border-rose-500/40 bg-rose-500/10',
    iconCls: 'text-rose-500',
    badgeCls: 'border-rose-500/40 bg-rose-500/15 text-rose-600',
  },
  {
    key: 'cycle_signals',
    label: '周期反转信号',
    icon: Activity,
    headerCls: 'border-sky-500/40 bg-sky-500/10',
    iconCls: 'text-sky-500',
    badgeCls: 'border-sky-500/40 bg-sky-500/15 text-sky-600',
  },
  {
    key: 'fed_pre_event',
    label: '未来 24h Fed 事件',
    icon: Bell,
    headerCls: 'border-indigo-500/40 bg-indigo-500/10',
    iconCls: 'text-indigo-500',
    badgeCls: 'border-indigo-500/40 bg-indigo-500/15 text-indigo-600',
  },
  {
    key: 'missed_signals',
    label: 'AI 漏检信号',
    icon: History,
    headerCls: 'border-orange-500/40 bg-orange-500/10',
    iconCls: 'text-orange-500',
    badgeCls: 'border-orange-500/40 bg-orange-500/15 text-orange-600',
  },
]

type AlertItem = {
  id?: number; key?: string; ts: number
  asset?: string; severity?: number; signal_kind?: string; summary?: string
  direction?: string; prob?: number; phase?: string; days_to?: number | null; rsi?: number | null
  metric?: string; threshold_bp?: string | number; change_bp?: string | number; regime?: string
  event_time?: number; title?: string; impact?: string; region?: string
  symbol?: string; decision?: string; ai_score?: number | null
}

type AlertSection = {
  ok: boolean
  count?: number
  upcoming_24h?: number
  items: AlertItem[]
  error?: string
  last_read_at?: number
}

type DetailsPayload = {
  ok: boolean
  sections: Record<string, AlertSection>
  ts: number
}

function fmtTime(s: number | null | undefined): string {
  if (!s || s <= 0) return '—'
  try { return new Date(s * 1000).toLocaleString() }
  catch { return String(s) }
}

export function DiscoveryUnreadAlertsModal({
  open, onClose, onMarked,
}: {
  open: boolean
  onClose: () => void
  onMarked?: () => void
}) {
  const [data, setData] = useState<DetailsPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [marking, setMarking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let aborted = false
    setLoading(true)
    fetch('/api/discovery/unread-alerts/details?limit=8', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((d) => { if (!aborted) { setData(d); setError(null) } })
      .catch((e) => { if (!aborted) setError(String(e)) })
      .finally(() => { if (!aborted) setLoading(false) })
    return () => { aborted = true }
  }, [open])

  const markAll = async () => {
    setMarking(true)
    try {
      const r = await fetch('/api/discovery/mark-alerts-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section: 'all' }),
      })
      if (r.ok) onMarked?.()
      onClose()
    } finally {
      setMarking(false)
    }
  }

  if (!open) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="未读异动详情"
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 pt-12"
      onClick={onClose}
    >
      <div
        className="w-full max-w-4xl rounded-lg border border-violet-500/40 bg-background shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between gap-2 border-b border-border p-3">
          <div className="flex items-center gap-2">
            <Bell className="h-5 w-5 text-rose-500" aria-hidden />
            <h2 className="text-sm font-bold uppercase tracking-wide">未读异动详情</h2>
            <span className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 font-mono text-[9px] font-bold text-violet-600 dark:text-violet-400">
              v276 · 一键已读
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={markAll}
              disabled={marking}
              className="rounded border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 font-mono text-[10px] font-bold text-emerald-600 hover:bg-emerald-500/20 disabled:opacity-50 dark:text-emerald-400"
            >
              {marking ? '处理中…' : '✅ 全部标为已读'}
            </button>
            <button
              onClick={onClose}
              aria-label="关闭"
              className="rounded p-1 hover:bg-secondary"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="max-h-[70vh] overflow-y-auto p-3 font-mono">
          {loading && (
            <div className="py-8 text-center text-muted-foreground">加载中…</div>
          )}
          {error && (
            <div className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-rose-600">
              ⚠️ {error}
            </div>
          )}
          {data && SECTION_META.map(({ key, label, icon: Icon, headerCls, iconCls, badgeCls }) => {
            const sec = data.sections[key]
            if (!sec) return null
            const n = sec.count ?? sec.upcoming_24h ?? 0
            return (
              <section key={key} className="mb-4">
                <div className={`mb-1 flex items-center gap-2 rounded border p-1.5 ${headerCls}`}>
                  <Icon className={`h-4 w-4 ${iconCls}`} aria-hidden />
                  <strong className="text-[11px] uppercase tracking-wider text-foreground">{label}</strong>
                  <span className={`ml-auto rounded border px-1.5 py-0.5 text-[10px] font-bold ${badgeCls}`}>
                    {n}
                  </span>
                </div>
                {!sec.ok ? (
                  <div className="rounded border border-rose-500/30 bg-rose-500/5 p-2 text-rose-500">
                    ⚠️ {sec.error}
                  </div>
                ) : sec.items.length === 0 ? (
                  <div className="rounded border border-dashed border-border p-2 text-center text-[10px] italic text-muted-foreground">
                    无未读
                  </div>
                ) : (
                  <ul className="space-y-1">
                    {sec.items.map((it, idx) => (
                      <AlertRow key={`${key}-${it.id ?? idx}`} kind={key} item={it} />
                    ))}
                  </ul>
                )}
              </section>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function AlertRow({ kind, item }: { kind: string; item: AlertItem }) {
  if (kind === 'cycle_signals') {
    return (
      <li className="rounded border border-sky-500/30 bg-sky-500/5 p-1.5 text-[10px]">
        <span className="font-bold text-sky-600 dark:text-sky-400">{item.asset}</span>
        <span className="ml-1">
          {item.direction === 'up' ? '↗ up' : item.direction === 'down' ? '↘ down' : item.direction}
          {' · '}prob={item.prob} · phase={item.phase}
          {' · '}rsi={item.rsi ?? '?'}
          {' · '}days_to={item.days_to ?? '?'}
        </span>
        <span className="ml-2 text-[9px] text-muted-foreground">{fmtTime(item.ts)}</span>
      </li>
    )
  }
  if (kind === 'cross_asset_high') {
    return (
      <li className="rounded border border-rose-500/30 bg-rose-500/5 p-1.5 text-[10px]">
        <span className="font-bold text-rose-600 dark:text-rose-400">{item.asset}</span>
        <span className="ml-1 rounded border border-rose-500/40 bg-rose-500/15 px-1 text-rose-600 dark:text-rose-400">
          sev={item.severity}
        </span>
        <span className="ml-1 rounded border border-violet-500/30 bg-violet-500/10 px-1 text-violet-600 dark:text-violet-400">
          {item.signal_kind}
        </span>
        <span className="ml-2">{item.summary}</span>
        <span className="ml-2 text-[9px] text-muted-foreground">{fmtTime(item.ts)}</span>
      </li>
    )
  }
  if (kind === 'fed_anomalies') {
    return (
      <li className="rounded border border-amber-500/30 bg-amber-500/5 p-1.5 text-[10px]">
        <span className="font-bold text-amber-600 dark:text-amber-400">
          {item.asset} {item.metric}
        </span>
        <span className="ml-1">
          {item.direction} {item.change_bp}bp(阈值 {item.threshold_bp}bp)
        </span>
        <span className="ml-1 rounded border border-violet-500/30 bg-violet-500/10 px-1 text-violet-600 dark:text-violet-400">
          regime={item.regime}
        </span>
        <span className="ml-2 text-[9px] text-muted-foreground">{fmtTime(item.ts)}</span>
      </li>
    )
  }
  if (kind === 'fed_pre_event') {
    return (
      <li className="rounded border border-indigo-500/30 bg-indigo-500/5 p-1.5 text-[10px]">
        <span className="font-bold text-indigo-600 dark:text-indigo-400">{item.impact ?? '?'}</span>
        <span className="ml-1 rounded border border-border bg-background px-1 text-muted-foreground">
          {item.region}
        </span>
        <span className="ml-1">{item.title}</span>
        <span className="ml-2 text-[9px] text-muted-foreground">{fmtTime(item.event_time)}</span>
      </li>
    )
  }
  if (kind === 'missed_signals') {
    return (
      <li className="rounded border border-orange-500/30 bg-orange-500/5 p-1.5 text-[10px]">
        <span className="font-bold text-orange-600 dark:text-orange-400">{item.symbol}</span>
        <span className="ml-1 rounded border border-border bg-background px-1">{item.decision}</span>
        <span className="ml-1">score={item.ai_score ?? '?'}</span>
        <span className="ml-2 text-[9px] text-muted-foreground">{fmtTime(item.ts)}</span>
      </li>
    )
  }
  return null
}