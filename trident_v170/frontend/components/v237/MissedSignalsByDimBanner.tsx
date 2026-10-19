'use client'
/**
 * components/v237/MissedSignalsByDimBanner.tsx — v302+ P0 精简版
 *
 * 专为非 attribution 页面 (如 analysis-process 顶部) 设计的紧凑 banner.
 * 复用 bridge_server.py /api/v237/missed-signals/by-dimension (critical_price_moves
 * 数据源, 真实 235 条),不依赖 trade_reflections_v2 上游.
 *
 * 响应 shape (来自 bridge_server.by_dimension):
 *   {
 *     ok: true,
 *     by_dimension: {
 *       A_signal:     { label, n, items, note },
 *       B_direction:  { label, n, items, note },
 *       C_technical:  { label, n, items, note },
 *     },
 *     totals: { A, B, C, sum },
 *     data_alignment: { warning } | null,
 *   }
 *
 * 失败独立(不抛错),数据为空时显示对齐提示,不影响父页面其他渲染.
 */

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { cn } from '@/lib/utils'
import { fetchWithFallback } from '@/lib/api'

type DimBlock = {
  label: string
  n: number
  items: Array<{ asset: string; ts: number; signal?: string; pattern?: string }>
  note?: string | null
}
type ByDimResp = {
  ok: boolean
  by_dimension?: { A_signal?: DimBlock; B_direction?: DimBlock; C_technical?: DimBlock }
  totals?: { A: number; B: number; C: number; sum: number }
  data_alignment?: { warning?: string } | null
  error?: string
}

const DIM_META: Record<'A' | 'B' | 'C', { key: string; label: string; color: string; sub: string }> = {
  A: { key: 'A_signal',    label: 'A · 买卖信号', color: 'text-long',       sub: '强信号无决策' },
  B: { key: 'B_direction', label: 'B · 方向',     color: 'text-short',      sub: '行情-利率冲突' },
  C: { key: 'C_technical', label: 'C · 技术',     color: 'text-foreground', sub: 'feed 异常' },
}

export function MissedSignalsByDimBanner({
  className,
  days = 2000,
  refreshMs = 60_000,
  variant = 'compact',
}: {
  className?: string
  days?: number
  refreshMs?: number
  variant?: 'compact' | 'inline'
}) {
  const [data, setData] = useState<ByDimResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const fetchIt = async () => {
      try {
        // v2026.10.19 P0 fix: 去掉 /api 前缀 — fetchWithFallback 内部 base 已经有 /api
        // (API_BASE='/api' 或 BRIDGE_BASE='http://127.0.0.1:8100/api'), 加了会拼成 /api/api/...
        // → 命中 bridge_server catch-all, 返 wrapper, 误报 BRIDGE_FALLBACK.
        // 真实端点 /api/v237/missed-signals/by-dimension 返 by_dimension + grand_total 516.
        const j: ByDimResp = await fetchWithFallback(
          `/v237/missed-signals/by-dimension?days=${days}`,
        )
        if (cancelled) return
        setData(j)
        if (j?.ok) setErr(null)
        else setErr(j?.error ?? 'unknown error')
      } catch (e) {
        if (cancelled) return
        setErr(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void fetchIt()
    const id = setInterval(fetchIt, refreshMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [days, refreshMs])

  if (loading && !data) {
    return (
      <div
        className={cn(
          'rounded border border-muted-foreground/30 bg-card px-3 py-2 font-mono text-[11px] text-muted-foreground',
          className,
        )}
      >
        三维度 (critical_price_moves) 加载中…
      </div>
    )
  }
  if (err && !data?.ok) {
    return (
      <div
        className={cn(
          'rounded border border-short/40 bg-short/10 px-3 py-2 font-mono text-[11px] text-short',
          className,
        )}
      >
        by-dimension 错误: {err}
      </div>
    )
  }

  const dims = data?.by_dimension ?? {}
  const totals = data?.totals
  const warning = data?.data_alignment?.warning
  const sum = totals?.sum ?? 0
  const isInline = variant === 'inline'

  return (
    <div
      className={cn(
        'rounded border bg-card',
        warning ? 'border-amber-500/40' : 'border-muted-foreground/30',
        isInline ? 'px-3 py-2' : 'p-3',
        className,
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            三维度漏检 (critical_price_moves)
          </span>
          <span className="font-mono text-[10px] text-muted-foreground/60">
            n={sum} · {days}d
          </span>
        </div>
        <Link
          href="/attribution#missed"
          className="font-mono text-[10px] text-muted-foreground underline hover:text-foreground"
        >
          详细 →
        </Link>
      </div>

      <div className={cn('mt-1.5 grid gap-1.5', isInline ? 'grid-cols-3' : 'grid-cols-1 sm:grid-cols-3')}>
        {(['A', 'B', 'C'] as const).map((k) => {
          const meta = DIM_META[k]
          const d = dims[meta.key as keyof typeof dims]
          const n = d?.n ?? 0
          const note = d?.note
          return (
            <div
              key={k}
              className={cn(
                'rounded border bg-muted/10 px-2 py-1',
                n > 0 ? 'border-muted-foreground/30' : 'border-muted-foreground/15 opacity-70',
              )}
            >
              <div className="flex items-baseline justify-between">
                <div className={cn('font-mono text-[10px] font-semibold', meta.color)}>
                  {meta.label}
                </div>
                <div className="font-mono text-[12px] font-bold text-foreground">{n}</div>
              </div>
              <div className="font-mono text-[9px] text-muted-foreground/70">
                {meta.sub}
                {note ? ` · ${note.slice(0, 28)}` : ''}
              </div>
            </div>
          )
        })}
      </div>

      {warning ? (
        <div className="mt-1.5 font-mono text-[9px] text-amber-600/90 dark:text-amber-400/90">
          ⚠ {warning}
        </div>
      ) : null}
    </div>
  )
}
