'use client'
/**
 * components/v237/MissedSignalsByDimBanner.tsx — v302+ P0 精简版
 *
 * 专为非 attribution 页面 (如 analysis-process 顶部) 设计的紧凑 banner.
 * 复用 bridge_server.py /api/missed-signals/by-dimension (数据源
 * critical_price_moves, 条数由后端实时统计), 不依赖 trade_reflections_v2 上游.
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
  // v308 P1-fix3: 原来默认 2000 天, 而同页的 MissedSignalsDimensionSummary
  // 硬编码 90 天 → 同一页两张"三维度"卡片显示不同窗口的条数, 互相矛盾
  // (2000d 那张因为后端 LIMIT 200 恒显示 200, 看起来还像"卡住了")。
  // 统一为 90 天, 与 Summary 一致; 需要别的窗口由调用方显式传 days。
  days = 90,
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
        // 真实端点 /api/missed-signals/by-dimension 返 by_dimension + grand_total 516.
        const j: ByDimResp = await fetchWithFallback(
          `/missed-signals/by-dimension?days=${days}`,
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
          'inline-flex w-fit items-center gap-1 rounded border border-muted-foreground/25 bg-card/60 px-2 py-0.5 font-mono text-[11px] text-muted-foreground',
          className,
        )}
      >
        三维度漏检数据加载中
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
            三维度漏检
          </span>
          <span className="font-mono text-[11px] text-muted-foreground/60">
            n={sum} · {days}d
          </span>
        </div>
        <Link
          href="/missed-signals"
          className="font-mono text-[11px] text-muted-foreground underline hover:text-foreground"
        >
          详细 →
        </Link>
      </div>

      {/* 结论先行：三张卡要读完才知道最该修哪一类，这里直接给判断。
          只用接口已有字段（totals + 各维度 note），不引入新数据。 */}
      {sum > 0 ? (() => {
        const entries = (['A', 'B', 'C'] as const)
          .map((k) => ({ k, n: dims[DIM_META[k].key as keyof typeof dims]?.n ?? 0 }))
          .sort((a, b) => b.n - a.n)
        const top = entries[0]
        const pct = sum > 0 ? Math.round((top.n / sum) * 100) : 0
        const topNote = dims[DIM_META[top.k].key as keyof typeof dims]?.note
        const zeroDims = entries.filter((e) => e.n === 0).map((e) => e.k)
        return (
          <p className="mt-1 font-mono text-[11px] leading-relaxed text-muted-foreground">
            <span className={cn('font-semibold', DIM_META[top.k].color)}>{DIM_META[top.k].label}</span>
            {' '}占 {pct}%（{top.n}/{sum}）
            {topNote ? `，主因：${topNote.slice(0, 40)}` : ''}
            {zeroDims.length > 0 ? ` · ${zeroDims.join('/')} 维度为 0，暂不失真` : ''}
          </p>
        )
      })() : null}

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
                <div className={cn('font-mono text-[11px] font-semibold', meta.color)}>
                  {meta.label}
                </div>
                <div className="font-mono text-[12px] font-bold text-foreground">{n}</div>
              </div>
              <div className="font-mono text-[11px] text-muted-foreground/70">
                {n === 0 ? `${days}d 内该维度无漏检` : meta.sub}
                {note ? ` · ${note.slice(0, 28)}` : ''}
              </div>
            </div>
          )
        })}
      </div>

      {warning ? (
        <div className="mt-1.5 font-mono text-[11px] text-amber-600/90 dark:text-amber-400/90">
          ⚠ {warning}
        </div>
      ) : null}
    </div>
  )
}
