'use client'

import { useCallback, useEffect, useState } from 'react'
import { apiGet } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'

// FRED 官方宏观指标面板 — 数据来自 /api/macro/context（macro_snapshots 快照）

type MacroItem = {
  ts: number
  category: string
  source: string
  metric_key: string
  value: number | null
  unit: string
  status: string
  payload: {
    label?: string
    observation_date?: string
    note?: string
    [key: string]: unknown
  }
}

type MacroContextResponse =
  | { status: 'ok'; items: MacroItem[] }
  | { status: 'empty' | 'unavailable'; items?: never }

// 即时影响行情的指标（与后端 fred_feed 的 impact=high 对齐）
const HIGH_IMPACT_KEYS = new Set([
  'cpi_yoy', 'core_cpi_yoy', 'nonfarm_payrolls', 'unrate',
  'effr_fred', 'dgs10', 't10y2y', 'ppi_yoy',
])

function fmtValue(item: MacroItem): string {
  if (item.status !== 'ok' || item.value === null || item.value === undefined) {
    return 'N/A'
  }
  const unit = item.unit || ''
  if (unit === '%') return `${item.value.toFixed(2)}%`
  if (unit === '千人') return `${Math.round(item.value).toLocaleString()} 千人`
  return `${item.value}${unit}`
}

export function MacroPanel() {
  const [items, setItems] = useState<MacroItem[]>([])
  const [note, setNote] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiGet<MacroContextResponse>('/macro/context')
      if (data.status !== 'ok' || !data.items) {
        setItems([])
        setNote(data.status === 'empty' ? '暂无宏观数据，引擎运行后自动沉淀' : '宏观数据源不可用')
        return
      }
      const fred = data.items.filter((item) => item.category === 'fred')
      setItems(fred)
      setNote(null)
    } catch (cause) {
      setItems([])
      setNote(cause instanceof Error ? cause.message : '宏观接口不可用')
    } finally {
      setLoading(false)
    }
  }, [])

  // v302 P0+：手写 setInterval → usePollRefresh（统一 alive flag + cleanup）
  usePollRefresh(reload, 30_000)

  return (
    <div className="surface-card shrink-0 p-2.5">
      <div className="flex items-center justify-between">
        <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">FRED 官方宏观指标</h3>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={loading}
          className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground disabled:opacity-60"
        >
          {loading ? '刷新中…' : '刷新'}
        </button>
      </div>
      {note ? (
        <div className="mt-1.5 font-mono text-[11px] text-muted-foreground">{note}</div>
      ) : (
        <div className="thin-scroll mt-1.5 flex flex-wrap gap-1.5 overflow-x-auto">
          {items.map((item) => {
            const high = HIGH_IMPACT_KEYS.has(item.metric_key)
            const ok = item.status === 'ok' && item.value !== null
            return (
              <div
                key={item.metric_key}
                className={`rounded border px-2 py-1 font-mono text-[10px] ${
                  high
                    ? ok
                      ? 'border-primary/40 bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground'
                    : ok
                      ? 'border-border bg-secondary/40 text-foreground'
                      : 'border-border text-muted-foreground'
                }`}
              >
                <div className="flex items-center gap-1">
                  <span>{item.payload?.label ?? item.metric_key}</span>
                  {high && <span className="text-[9px] opacity-70">· 高影响</span>}
                </div>
                <div className="text-[11px]">
                  <b>{fmtValue(item)}</b>
                  <span className="ml-1 opacity-70">{item.payload?.observation_date ?? ''}</span>
                </div>
              </div>
            )
          })}
          {items.length === 0 && !note && (
            <div className="font-mono text-[11px] text-muted-foreground">加载中…</div>
          )}
        </div>
      )}
    </div>
  )
}
