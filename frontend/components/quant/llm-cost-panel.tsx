'use client'

import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'

/**
 * v479 · Token 消耗明细（挂在 /connectivity）
 *
 * 现有 `BackendCapabilityPanel domain="llm-stats"` 只用了 `/llm-stats/summary`，
 * 回答"今天烧了多少"，回答不了"是谁烧的、什么时候烧的"。
 * 这里补两个一直没上页面的接口：
 *   - `/llm-stats/by-call-site?days=7`：按调用点拆 token（哪个环节最贵）
 *   - `/llm-stats/timeline?days=7`：按小时分桶（什么时段在跑批）
 *
 * 口径：只展示后端给的数，不估算、不按"字符数≈token"折算。
 * 某个点失败就那一块显示「无数据」，不拿 0 顶。
 */

type Summary = {
  window_days?: number
  totals?: { tokens_in?: number; tokens_out?: number; calls?: number }
}

type CallSite = { call_site: string; calls: number; tokens_in: number; tokens_out: number }
type Bucket = { bucket_ts: number; calls: number; tokens_in: number; tokens_out: number; ok_calls?: number }
type Timeline = { bucket_seconds?: number; buckets?: Bucket[] }

function fmtTok(n: number | undefined | null): string {
  if (n == null) return '—'
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return String(n)
}

export function LlmCostPanel() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [sites, setSites] = useState<CallSite[] | null>(null)
  const [timeline, setTimeline] = useState<Timeline | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const ac = new AbortController()
    setLoading(true)
    setErr(null)
    const get = async (p: string) => {
      const r = await fetch(`${API_BASE}${p}`, { signal: ac.signal, cache: 'no-store' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }
    Promise.allSettled([
      get('/llm-stats/summary'),
      get('/llm-stats/by-call-site?days=7'),
      get('/llm-stats/timeline?days=7'),
    ])
      .then(([s, c, t]) => {
        setSummary(s.status === 'fulfilled' ? s.value : null)
        setSites(c.status === 'fulfilled' ? c.value : null)
        setTimeline(t.status === 'fulfilled' ? t.value : null)
        if ([s, c, t].every((r) => r.status === 'rejected')) {
          setErr('接口未响应')
        }
      })
      .finally(() => {
        if (!ac.signal.aborted) setLoading(false)
      })
    return () => ac.abort()
  }, [open])

  const rows = useMemo(() => {
    if (!sites) return []
    const list = [...sites]
      .map((s) => ({ ...s, total: (s.tokens_in || 0) + (s.tokens_out || 0) }))
      .sort((a, b) => b.total - a.total)
    const sum = list.reduce((acc, s) => acc + s.total, 0)
    return list.slice(0, 10).map((s) => ({ ...s, share: sum > 0 ? s.total / sum : 0 }))
  }, [sites])

  // 7 天按小时分桶 → 合成 24 个"日均小时"柱，避免 168 根挤在一起
  const hourly = useMemo(() => {
    const buckets = timeline?.buckets || []
    if (buckets.length === 0) return []
    const acc = Array.from({ length: 24 }, () => 0)
    const cnt = Array.from({ length: 24 }, () => 0)
    buckets.forEach((b) => {
      const h = new Date(b.bucket_ts * 1000).getHours()
      acc[h] += (b.tokens_in || 0) + (b.tokens_out || 0)
      cnt[h] += 1
    })
    return acc.map((v, i) => (cnt[i] ? v / cnt[i] : 0))
  }, [timeline])

  const maxHour = Math.max(...hourly, 1)

  return (
    <section className="rounded-lg border border-border bg-card/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/70 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-2 font-mono text-[12px] font-semibold text-foreground hover:text-primary"
        >
          <span aria-hidden>{open ? '▼' : '▶'}</span>
          <span>Token 消耗明细</span>
        </button>
        <span className="text-[11px] text-muted-foreground">
          按调用点拆 7 天消耗 + 按小时看批处理时段
        </span>
        {summary?.totals && (
          <span className="ml-auto font-mono text-[11px] text-muted-foreground">
            近 {(summary.window_days ?? 1).toFixed(0)} 天：
            <span className="text-foreground"> {fmtTok(summary.totals.tokens_in)} </span>入 /
            <span className="text-foreground"> {fmtTok(summary.totals.tokens_out)} </span>出 /
            <span className="text-foreground"> {summary.totals.calls ?? 0} </span>次
          </span>
        )}
      </div>

      {open && (
        <div className="px-3 py-2">
          {err ? (
            <div className="text-[11px] text-muted-foreground">{err}；不显示占位数据。</div>
          ) : loading ? (
            <div className="text-[11px] text-muted-foreground">载入中…</div>
          ) : (
            <>
              <div className="mb-1 font-mono text-[11px] font-semibold text-foreground">按调用点（7 天）</div>
              {rows.length === 0 ? (
                <div className="text-[11px] text-muted-foreground">无数据。</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[520px] text-[11px]">
                    <thead>
                      <tr className="border-b border-border/60 text-left font-mono text-[10px] text-muted-foreground">
                        <th className="py-1 pr-2 font-normal">调用点</th>
                        <th className="py-1 pr-2 text-right font-normal">调用</th>
                        <th className="py-1 pr-2 text-right font-normal">入</th>
                        <th className="py-1 pr-2 text-right font-normal">出</th>
                        <th className="py-1 pl-2 font-normal">占比</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((s) => (
                        <tr key={s.call_site} className="border-b border-border/40">
                          <td className="py-1 pr-2 font-mono text-foreground/80">{s.call_site}</td>
                          <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{s.calls}</td>
                          <td className="py-1 pr-2 text-right font-mono">{fmtTok(s.tokens_in)}</td>
                          <td className="py-1 pr-2 text-right font-mono">{fmtTok(s.tokens_out)}</td>
                          <td className="py-1 pl-2">
                            <div className="flex items-center gap-1">
                              <div className="h-1.5 w-24 rounded-sm bg-muted/40">
                                <div className="h-1.5 rounded-sm bg-primary/70" style={{ width: `${Math.round(s.share * 100)}%` }} />
                              </div>
                              <span className="font-mono text-[10px] text-muted-foreground">
                                {(s.share * 100).toFixed(1)}%
                              </span>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="mt-3 mb-1 font-mono text-[11px] font-semibold text-foreground">一天里什么时候在烧（7 天平均）</div>
              {hourly.length === 0 ? (
                <div className="text-[11px] text-muted-foreground">无数据。</div>
              ) : (
                <div className="flex items-end gap-[3px]">
                  {hourly.map((v, h) => (
                    <div key={h} className="flex flex-1 flex-col items-center gap-1">
                      <div
                        className="w-full rounded-t-sm bg-primary/50"
                        style={{ height: `${Math.max(2, Math.round((v / maxHour) * 40))}px` }}
                        title={`${h}:00 · ${fmtTok(v)} tokens`}
                      />
                      {h % 6 === 0 && (
                        <span className="font-mono text-[9px] text-muted-foreground/70">{h}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  )
}
