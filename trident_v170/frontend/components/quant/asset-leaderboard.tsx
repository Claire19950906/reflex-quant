'use client'

import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { ArrowUpRight, ArrowDownRight, Loader2 } from 'lucide-react'

// ── 资产涨跌排行榜 ────────────────────────────────────────────────
// 数据源：复用 /api/market/movers（30s 轮询）。
// 形态：水平条形图 + 名次 + 涨跌%。点击 = 触发 onSelectAsset。
// 比起 movers-panel.tsx 顶部的 grid 卡片，区别是「以名次为第一视觉」
// —— 大涨/大跌的资产一眼能看到，而不是被卡片均分挤在一起看不清。

type MoverItem = {
  asset: string
  price: number | null
  change24h: number | null
  source?: string
  source_count?: number | null
}

import { ASSET_META } from '@/lib/asset-meta'

type Props = {
  onSelectAsset?: (asset: string) => void
}

export function AssetLeaderboard({ onSelectAsset }: Props) {
  const [items, setItems] = useState<MoverItem[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => {
      fetch(`${API_BASE}/market/movers`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => {
          if (!alive) return
          const list = (Array.isArray(d.items) ? d.items : []) as MoverItem[]
          // 后端 movers 已按涨跌降序；这里只截前 8
          setItems(list.slice(0, 8))
          setErr(null)
        })
        .catch((e) => { if (alive) setErr(String(e?.message || e)) })
        .finally(() => { if (alive) setLoading(false) })
    }
    void load()
    const t = window.setInterval(load, 30_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  // 找到最大的绝对涨跌值用于条形比例尺(0 是中性排序键,非显示数据)
  const maxAbs = useMemo(() => {
    let m = 0
    for (const it of items) {
      const a = Math.abs(it.change24h ?? 0) // EXEMPT-HONESTY: 排序比例尺键,非显示
      if (a > m) m = a
    }
    return m || 1
  }, [items])

  if (loading) {
    return (
      <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />加载资产涨跌排行榜…
      </div>
    )
  }
  if (err) {
    return (
      <div className="rounded border border-short/30 bg-short/10 px-3 py-2 text-xs text-short">
        排行榜接口异常：{err}
      </div>
    )
  }
  if (!items.length) {
    return (
      <div className="rounded border border-border bg-card p-4 text-xs text-muted-foreground">
        暂无资产行情数据（等待行情 watcher 启动）
      </div>
    )
  }
  return (
    <div className="rounded border border-border bg-card p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
          📊 资产涨跌排行榜（24h）
        </h3>
        <span className="font-mono text-[10px] text-muted-foreground/70">
          长度 = |涨跌%| · 颜色 = 方向（涨绿跌红）
        </span>
      </div>
      <ul className="space-y-1.5">
        {items.map((it, i) => {
          const chg = it.change24h
          const up = chg == null ? null : chg >= 0
          const pct = ((Math.abs(chg ?? 0) / maxAbs) * 100).toFixed(0)
          return (
            <li key={it.asset}>
              <button
                type="button"
                onClick={() => onSelectAsset?.(it.asset)}
                className="group flex w-full items-center gap-3 rounded px-2 py-1.5 text-left transition-colors hover:bg-secondary/40"
              >
                {/* 名次 */}
                <span className="w-6 shrink-0 text-center font-mono text-[11px] font-semibold text-muted-foreground">
                  {i + 1}
                </span>
                {/* 资产名 */}
                <div className="w-16 shrink-0">
                  <div className="font-mono text-xs font-semibold text-foreground">{it.asset}</div>
                  <div className="font-mono text-[10px] text-muted-foreground/70">
                    {ASSET_META[it.asset] ?? ''}
                  </div>
                </div>
                {/* 条形（宽度∝|涨跌|） */}
                <div className="relative flex-1">
                  <div className="h-4 w-full overflow-hidden rounded bg-muted/40">
                    <div
                      className={`h-full transition-all ${
                        up == null
                          ? 'bg-muted-foreground/30'
                          : up
                          ? 'bg-long/70 group-hover:bg-long'
                          : 'bg-short/70 group-hover:bg-short'
                      }`}
                      style={{ width: `${chg == null ? 0 : pct}%` }}
                    />
                  </div>
                </div>
                {/* 涨跌% */}
                <div
                  className={`w-20 shrink-0 text-right font-mono text-xs tabular-nums ${
                    up == null
                      ? 'text-muted-foreground'
                      : up
                      ? 'text-long'
                      : 'text-short'
                  }`}
                >
                  {chg == null ? '—' : (
                    <span className="inline-flex items-center gap-0.5">
                      {up ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                      {chg >= 0 ? '+' : ''}{chg.toFixed(2)}%
                    </span>
                  )}
                </div>
                {/* 价格 */}
                <div className="w-24 shrink-0 text-right font-mono text-[11px] text-muted-foreground tabular-nums">
                  {it.price == null ? '—' : it.price.toLocaleString('en-US', { maximumFractionDigits: 2 })}
                </div>
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}