'use client'

/**
 * components/quant/abnormal-attribution-panel.tsx
 *
 * AI 异动归因面板 (需求 #1 / #6): 事件 + 形态 + 量能 → 可执行结论。
 *
 * 为什么需要这个面板:
 *   AI worker 一直在跑 (abnormal_attributions 1.8 万行), 但归因页上**看不到**它:
 *   · 之前只有 /api/abnormal-signals 给的是信号列表, 没有 AI 的归因结论;
 *   · 归因结论里的 actionable 97% 是"观望", 等于没给可执行结论;
 *   · 量能/形态上下文 (kline_bars / tech_signals) 从来没跟归因结论放在一起。
 *
 * 本面板直接消费 /api/abnormal-attribution:
 *   结论 summary + 驱动 drivers + 可执行结论 action_text + 量能/形态上下文。
 */

import { useCallback, useEffect, useState } from 'react'
import { API_BASE, fetchWithFallback } from '@/lib/api'
import { AlertTriangle, ChevronDown, ChevronRight, Loader2, Sparkles } from 'lucide-react'

type Driver = {
  cause?: string
  source?: string
  confidence?: string
  speculative?: boolean
}

type AttributionItem = {
  id: number
  signal_id: number | null
  asset: string
  signal_kind: string
  anchor_ts: number
  summary: string
  drivers: Driver[]
  cross_asset_likely: string[]
  actionable: string
  is_actionable: number
  action_text: string
  action_source: string
  severity_hint: number
  source_model?: string
  used_news_n?: number
  // 量能 / 形态 上下文
  volume_ratio?: number | null
  kline_pattern?: string
  kline_close?: number | null
  tech_pattern?: string
  tech_score?: number | null
  tech_vol_ratio?: number | null
  daily_pattern?: string
  daily_volume_ratio?: number | null
}

type Resp = {
  ok: boolean
  n: number
  n_actionable?: number
  items?: AttributionItem[]
  error?: string
}

const SEVERITY_COLOR: Record<number, string> = {
  1: 'text-slate-400',
  2: 'text-amber-400',
  3: 'text-red-500',
}

export default function AbnormalAttributionPanel({
  refreshToken = 0,
  limit = 20,
}: {
  refreshToken?: number
  limit?: number
}) {
  const [data, setData] = useState<Resp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [open, setOpen] = useState<number | null>(null)
  const [onlyActionable, setOnlyActionable] = useState(false)

  const load = useCallback(async () => {
    try {
      setErr(null)
      const url = `${API_BASE}/abnormal-attribution?limit=${limit}${
        onlyActionable ? '&only_actionable=true' : ''
      }`
      const res = await fetchWithFallback(url)
      const json = (await res.json()) as Resp
      setData(json)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [limit, onlyActionable])

  useEffect(() => {
    void load()
  }, [load, refreshToken])

  const items = data?.items ?? []

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
      <header className="mb-2 flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-200">
          <Sparkles className="h-4 w-4 text-cyan-400" />
          AI 异动归因 (事件 + 形态 + 量能)
        </h3>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <label className="flex cursor-pointer items-center gap-1">
            <input
              type="checkbox"
              checked={onlyActionable}
              onChange={(e) => setOnlyActionable(e.target.checked)}
            />
            只看可执行结论
          </label>
          <span>共 {data?.n ?? 0} 条 · 可执行 {data?.n_actionable ?? 0}</span>
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
        </div>
      </header>

      {err ? (
        <p className="flex items-center gap-1 text-xs text-red-400">
          <AlertTriangle className="h-3.5 w-3.5" /> {err}
        </p>
      ) : null}

      {!loading && items.length === 0 ? (
        <p className="text-xs text-slate-500">暂无 AI 归因记录</p>
      ) : null}

      <ul className="flex flex-col gap-1.5">
        {items.map((it) => {
          const isOpen = open === it.id
          const sev = it.severity_hint ?? 1
          return (
            <li key={it.id} className="rounded border border-slate-800/80 bg-slate-900/40">
              <button
                type="button"
                className="flex w-full items-start gap-2 px-2 py-1.5 text-left"
                onClick={() => setOpen(isOpen ? null : it.id)}
              >
                {isOpen ? (
                  <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" />
                ) : (
                  <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" />
                )}
                <span className="w-14 shrink-0 font-mono text-xs text-cyan-300">
                  {it.asset}
                </span>
                <span className="flex-1 text-xs text-slate-300">{it.summary}</span>
                {it.is_actionable ? (
                  <span className="shrink-0 rounded bg-emerald-900/60 px-1.5 py-0.5 text-[11px] text-emerald-300">
                    可执行
                  </span>
                ) : (
                  <span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-[11px] text-slate-400">
                    观望
                  </span>
                )}
                <span className={`shrink-0 text-[11px] ${SEVERITY_COLOR[sev] ?? 'text-slate-400'}`}>
                  强度{sev}
                </span>
              </button>
              {isOpen ? (
                <AbnormalAttributionDetail item={it} />
              ) : null}
            </li>
          )
        })}
      </ul>
    </section>
  )
}


function AbnormalAttributionDetail({ item: it }: { item: AttributionItem }) {
  const volBits: string[] = []
  if (it.kline_pattern) volBits.push(`形态 ${it.kline_pattern}`)
  else if (it.tech_pattern) volBits.push(`技术形态 ${it.tech_pattern}`)
  else if (it.daily_pattern) volBits.push(`日线 ${it.daily_pattern}`)
  if (it.volume_ratio != null) volBits.push(`量比 ${it.volume_ratio}`)
  if (it.tech_vol_ratio != null) volBits.push(`技术量比 ${it.tech_vol_ratio}`)
  if (it.daily_volume_ratio != null) volBits.push(`日线量比 ${it.daily_volume_ratio}`)
  const tsText = it.anchor_ts
    ? new Date(it.anchor_ts * 1000).toISOString().slice(0, 19).replace('T', ' ')
    : '—'
  return (
    <div className="flex flex-col gap-2 border-t border-slate-800/80 px-3 py-2 text-xs">
      <div>
        <span className="text-slate-500">可执行结论: </span>
        <span className="text-emerald-300">{it.action_text || it.actionable || '—'}</span>
        {it.action_source === 'derived_from_drivers' ? (
          <span className="ml-1 text-[11px] text-slate-500">(由驱动推导)</span>
        ) : null}
      </div>
      <div>
        <span className="text-slate-500">时间: </span>
        <span className="text-slate-300">{tsText}</span>
        {it.source_model ? <span className="ml-2 text-slate-500">模型: {it.source_model}</span> : null}
        {typeof it.used_news_n === 'number' ? (
          <span className="ml-2 text-slate-500">引用新闻: {it.used_news_n}</span>
        ) : null}
      </div>
      <div>
        <span className="text-slate-500">量能/形态: </span>
        <span className="text-slate-300">{volBits.length ? volBits.join(' · ') : '—'}</span>
      </div>
      {it.cross_asset_likely?.length ? (
        <div>
          <span className="text-slate-500">跨品种: </span>
          <span className="text-slate-300">{it.cross_asset_likely.join(', ')}</span>
        </div>
      ) : null}
      <div>
        <span className="text-slate-500">驱动: </span>
        <ul className="mt-0.5 flex flex-col gap-0.5">
          {(it.drivers ?? []).map((d, i) => (
            <li key={i} className="text-slate-300">
              · {d.cause}
              <span className="ml-1 text-[11px] text-slate-500">
                [{d.source || '未知来源'} / {d.confidence || '?'}
                {d.speculative ? ' / 推测' : ''}]
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
