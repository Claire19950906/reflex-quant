'use client'

import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { ASSET_META } from '@/lib/asset-meta'
import { Loader2 } from 'lucide-react'

// ── 事件热力图（asset × hour） ────────────────────────────────────
// 数据源：GET /api/events/heatmap?hours=24&limit_assets=10
// 形态：网格 — 行=资产（按事件量排序），列=小时（最近 24h）。
// 单元格颜色深度∝事件数；hover 显示 BUY/SELL 计数 + 平均 score。

type HeatCell = {
  ts: number
  count: number
  score_sum: number
  abs_score_sum: number
  buy: number
  sell: number
}
type HeatResp = {
  hours: number
  bucket_count: number
  buckets: string[]
  assets: string[]
  cells: Record<string, HeatCell[]>
  max_count: number
  max_score: number
  since_ts: number
  now_ts: number
  generated_at: string
}

type Props = { hours?: number; limit_assets?: number }

export function EventHeatmap({ hours = 24, limit_assets = 10 }: Props) {
  const [data, setData] = useState<HeatResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [hover, setHover] = useState<{ asset: string; idx: number; cell: HeatCell } | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => {
      fetch(`${API_BASE}/events/heatmap?hours=${hours}&limit_assets=${limit_assets}`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d: HeatResp) => { if (alive) { setData(d); setErr(null) } })
        .catch((e) => { if (alive) setErr(String(e?.message || e)) })
        .finally(() => { if (alive) setLoading(false) })
    }
    void load()
    const t = window.setInterval(load, 60_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [hours, limit_assets])

  const columnTotals = useMemo(() => {
    if (!data) return [] as number[]
    return Array.from({ length: data.bucket_count }, (_, idx) => {
      let s = 0
      for (const a of data.assets) s += data.cells[a]?.[idx]?.count ?? 0
      return s
    })
  }, [data])

  if (loading) {
    return (
      <div className="flex h-32 items-center justify-center rounded border border-border bg-card text-xs text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />加载事件热力图…
      </div>
    )
  }
  if (err) {
    return (
      <div className="rounded border border-short/30 bg-short/10 p-3 text-xs text-short">
        事件热力图异常：{err}
      </div>
    )
  }
  if (!data || data.assets.length === 0) {
    return (
      <div className="rounded border border-border bg-card p-4 text-xs text-muted-foreground">
        最近 {hours}h 暂无事件（可能被 L1 筛子全部没晒进来，或 watcher 还没启动）
      </div>
    )
  }

  const colLabels = data.buckets.map((b) => b.split(' ')[1] ?? b)
  const showFull = data.bucket_count > 12
  return (
    <div className="rounded border border-border bg-card p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
          🕒 事件热力图（asset × hour · 最近 {data.hours}h）
        </h3>
        <span className="font-mono text-[10px] text-muted-foreground/70">
          {data.assets.length} 资产 × {data.bucket_count} 时段 · 颜色深度∝事件数
        </span>
      </div>
      <div className="overflow-x-auto">
        <div className="min-w-[640px]">
          <div className="flex items-end gap-0.5 pl-[68px]">
            {data.buckets.map((b, i) => (
              <div key={i} className="flex-1 text-center font-mono text-[8px] text-muted-foreground/70">
                {showFull && (i === 0 || i === data.bucket_count - 1 || i % 6 === 0)
                  ? b.split(' ')[0].slice(-5) + ' ' + colLabels[i]
                  : colLabels[i]}
              </div>
            ))}
          </div>
          {data.assets.map((asset) => (
            <div key={asset} className="mt-1 flex items-center gap-0.5">
              <div className="w-[64px] shrink-0 pr-2 text-right">
                <div className="font-mono text-[11px] font-semibold leading-tight text-foreground">{asset}</div>
                <div className="truncate font-mono text-[9px] leading-tight text-muted-foreground/70">
                  {ASSET_META[asset] ?? ''}
                </div>
              </div>
              {(data.cells[asset] ?? []).map((cell, idx) => {
                const intensity = data.max_count > 0 ? Math.min(1, cell.count / data.max_count) : 0
                const bg = cell.count === 0
                  ? 'bg-muted/15'
                  : cell.buy > cell.sell
                  ? `bg-long/${(40 + intensity * 55).toFixed(0)}`
                  : cell.sell > cell.buy
                  ? `bg-short/${(40 + intensity * 55).toFixed(0)}`
                  : `bg-primary/${(40 + intensity * 55).toFixed(0)}`
                const title = cell.count === 0
                  ? `${asset} ${colLabels[idx]} · 无事件`
                  : `${asset} ${colLabels[idx]} · ${cell.count} 条 (BUY ${cell.buy} / SELL ${cell.sell}) · |Σscore|=${cell.abs_score_sum.toFixed(2)}`
                return (
                  <div
                    key={idx}
                    title={title}
                    onMouseEnter={() => setHover({ asset, idx, cell })}
                    onMouseLeave={() => setHover(null)}
                    className={`h-5 flex-1 cursor-default rounded-sm ${bg} transition-all hover:ring-1 hover:ring-primary`}
                  />
                )
              })}
            </div>
          ))}
          <div className="mt-2 flex items-center gap-0.5 border-t border-dashed border-border pt-1 pl-[68px]">
            {columnTotals.map((t, i) => (
              <div
                key={i}
                className={`flex-1 text-center font-mono text-[9px] ${t > 0 ? 'text-foreground/80' : 'text-muted-foreground/40'}`}
                title={`${colLabels[i]} 合计 ${t} 条事件`}
              >
                {t > 0 ? t : ''}
              </div>
            ))}
          </div>
          <div className="mt-0.5 pl-[68px] text-right font-mono text-[9px] text-muted-foreground/60">
            ↓ 每列合计（小时总事件量）
          </div>
        </div>
      </div>
      {hover && (
        <div className="mt-2 rounded border border-border bg-background/80 px-2 py-1 font-mono text-[10px] text-foreground/90">
          <span className="font-semibold">{hover.asset}</span> ·{' '}
          {data.buckets[hover.idx]} ·{' '}
          <span className="text-long">{hover.cell.buy} BUY</span> /{' '}
          <span className="text-short">{hover.cell.sell} SELL</span> ·{' '}
          {hover.cell.count} 条 · |Σscore|={hover.cell.abs_score_sum.toFixed(2)}
        </div>
      )}
    </div>
  )
}