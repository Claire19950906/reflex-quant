'use client'

/**
 * PendingAttributionPanel — 归因页「待复核区」入口 (v334)
 *
 * 背景（需求 #6/#7）：
 *   |涨跌| >= 5% 的大异动必须过 AI 复核才允许进归因主表。AI 通道离线时，
 *   这批行会停在 event_move_attribution_pending —— 候选新闻其实已经找到了，
 *   只是卡在复核闸门前。以前这批数据只在库里，页面上完全看不到，
 *   用户看到的是"大行情没有归因"，并不知道系统已经找到了线索。
 *
 * 这个面板做三件事：
 *   1. 把待复核条数显式亮出来（尤其"大幅异动"有几条），不再假装覆盖率就是全部真相；
 *   2. 每条给出候选新闻标题 + 证据分，让用户能人工判断；
 *   3. 给出 why_pending —— 是"没 AI 通道"还是"证据分不够"，两种完全不同的问题。
 *
 * 数据流: /api/event-move-attribution-pending?limit=50 (bridge 8100)
 */

import { useCallback, useEffect, useState } from 'react'
import { AlarmClock, AlertTriangle, ArrowDown, ArrowUp, Inbox } from 'lucide-react'
import { fetchWithFallback } from '@/lib/api'

type PendingRow = {
  id: number
  move_id: number
  asset: string
  move_pct: number
  move_kind: string
  duration_min: number
  move_ts: number
  algo_score: number
  algo_reasons: string
  time_lag_sec: number
  ev_source: string
  ev_action: string
  ev_news_id: number | null
  headline: string | null
  news_source: string | null
  why_pending: string
}

type PendingResp = {
  ok: boolean
  n: number
  pending_total: number
  pending_big_moves: number
  items: PendingRow[]
  note?: string
}

function fmtTs(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toISOString().slice(5, 16).replace('T', ' ')
}

function fmtLag(sec: number): string {
  const h = sec / 3600
  if (Math.abs(h) < 1) return `${Math.round(sec / 60)}m`
  return `${h > 0 ? '+' : ''}${h.toFixed(1)}h`
}

export function PendingAttributionPanel({
  refreshMs = 60_000,
  externalRefreshToken,
}: {
  refreshMs?: number
  externalRefreshToken?: number
}) {
  const [data, setData] = useState<PendingResp | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetchWithFallback<PendingResp>(
        '/api/event-move-attribution-pending?limit=50',
      )
      if (!r?.ok) throw new Error('pending endpoint not ok')
      setData(r)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchData()
  }, [fetchData])

  useEffect(() => {
    if (!refreshMs) return
    const id = setInterval(() => void fetchData(), refreshMs)
    return () => clearInterval(id)
  }, [fetchData, refreshMs])

  useEffect(() => {
    if (externalRefreshToken) void fetchData()
  }, [externalRefreshToken, fetchData])

  const items = data?.items ?? []
  const total = data?.pending_total ?? 0
  const bigCount = data?.pending_big_moves ?? 0

  return (
    <section className="rounded-lg border border-border bg-card/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left"
      >
        <span className="flex items-center gap-2 text-xs font-medium text-foreground/80">
          <AlarmClock className="h-3.5 w-3.5 text-amber-500" />
          待复核区
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[11px] text-amber-600 dark:text-amber-400">
            {total} 条
          </span>
          {bigCount > 0 && (
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              其中 |涨跌| ≥ 5% {bigCount} 条
            </span>
          )}
          {loading && <span className="font-mono text-[11px] text-muted-foreground">刷新中…</span>}
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">{open ? '收起' : '展开'}</span>
      </button>

      {open && (
        <div className="border-t border-border/60 px-3 py-2">
          {error && (
            <div className="mb-2 flex items-center gap-1 rounded border border-short/40 bg-short/10 px-2 py-1 font-mono text-[11px] text-short">
              <AlertTriangle className="h-3 w-3" /> {error}
            </div>
          )}

          {items.length === 0 && !error ? (
            <div className="flex items-center gap-2 py-3 font-mono text-[11px] text-muted-foreground">
              <Inbox className="h-3.5 w-3.5" /> 暂无待复核行（大异动都已被解释或已过复核）
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              {items.map((it) => {
                const up = (it.move_pct ?? 0) >= 0
                return (
                  <div
                    key={it.id}
                    className="rounded border border-border/60 bg-background/40 px-2 py-1.5"
                  >
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
                      <span className="text-muted-foreground">{fmtTs(it.move_ts)}</span>
                      <span className="font-semibold text-foreground">{it.asset}</span>
                      <span
                        className={`flex items-center gap-0.5 ${up ? 'text-long' : 'text-short'}`}
                      >
                        {up ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />}
                        {up ? '+' : ''}
                        {Number(it.move_pct ?? 0).toFixed(2)}%
                      </span>
                      <span className="rounded bg-muted px-1 text-[11px] text-muted-foreground">
                        {it.move_kind}
                      </span>
                      <span className="text-muted-foreground">证据 {Number(it.algo_score ?? 0).toFixed(2)}</span>
                      <span className="text-muted-foreground">滞后 {fmtLag(it.time_lag_sec || 0)}</span>
                    </div>
                    <div className="mt-1 line-clamp-2 text-[11px] leading-snug text-foreground/80">
                      {it.headline?.trim() || '（只匹配到正文/标签，无标题可展示）'}
                    </div>
                    <div className="mt-0.5 font-mono text-[11px] text-amber-600 dark:text-amber-400">
                      {it.why_pending}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {data?.note && (
            <p className="mt-2 font-mono text-[11px] leading-relaxed text-muted-foreground/70">
              {data.note}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
