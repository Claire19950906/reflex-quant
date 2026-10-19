'use client'

import { useCallback, useEffect, useState } from 'react'
import { apiGet } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'

// BLS 官方宏观数据面板（CPI/非农/失业率）— 数据来自 /api/macro/context 中 category=bls 项
// BLS 是美国劳工统计局一手官方源，比 FRED 更接近发布时刻；面板每次拉取带 10min 缓存。

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
    observation_period?: string
    observation_date?: string
    note?: string
    [key: string]: unknown
  }
}

type MacroContextResponse =
  | { status: 'ok'; items: MacroItem[] }
  | { status: 'empty' | 'unavailable'; items?: never }

function fmtValue(item: MacroItem): string {
  if (item.status !== 'ok' || item.value === null || item.value === undefined) {
    return 'N/A'
  }
  const unit = item.unit || ''
  if (item.metric_key === 'cpi_yoy') return `${item.value >= 0 ? '+' : ''}${item.value.toFixed(2)}%`
  if (unit === '%') return `${item.value.toFixed(2)}%`
  if (unit === '千人') return `${Math.round(item.value).toLocaleString()} 千人`
  return `${item.value}${unit}`
}

function fmtPeriod(item: MacroItem): string {
  const p = item.payload?.observation_period
  if (!p) return item.payload?.observation_date ?? ''
  // 2026-M07 → 2026年7月
  const m = p.match(/^(\d{4})-M(\d{2})$/)
  if (m) return `${m[1]}年${Number(m[2])}月`
  return p
}

export function BlsMacroPanel() {
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
      const bls = data.items.filter((item) => item.category === 'bls')
      setItems(bls)
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
        <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">BLS 美国劳工统计局 · 官方一手数据</h3>
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
      ) : items.length > 0 ? (
        <div className="thin-scroll mt-1.5 flex flex-wrap gap-1.5 overflow-x-auto">
          {items.map((item) => {
            const ok = item.status === 'ok' && item.value !== null
            return (
              <div
                key={item.metric_key}
                className={`rounded border px-2 py-1 font-mono text-[10px] ${
                  ok ? 'border-primary/40 bg-primary/10 text-primary' : 'border-border text-muted-foreground'
                }`}
              >
                <div className="flex items-center gap-1">
                  <span>{item.payload?.label ?? item.metric_key}</span>
                  <span className="text-[9px] opacity-70">· 一手源</span>
                </div>
                <div className="text-[11px]">
                  <b>{fmtValue(item)}</b>
                  <span className="ml-1 opacity-70">{fmtPeriod(item)}</span>
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        <div className="mt-1.5 font-mono text-[11px] text-muted-foreground">
          未配置 BLS_API_KEY 或数据源暂不可用（配 key 后重启引擎生效）
        </div>
      )}
    </div>
  )
}
