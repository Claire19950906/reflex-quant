'use client'

import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'

// ── 资产涨跌热力图（卡片版） ──────────────────────────────────────
// 数据源：复用 /api/market/movers。
// 形态：treemap 风格矩形网格 —— 每个资产一个矩形，面积∝|涨跌%|，
// 颜色 = 方向（涨绿跌红）+ 强度（透明度∝|涨跌%|）。
// 比起 asset-leaderboard 的条形图，矩形布局更易一眼看出"谁动得最猛"。

type MoverItem = {
  asset: string
  change24h: number | null
  source?: string
}

import { ASSET_META } from '@/lib/asset-meta'

type Props = { onSelectAsset?: (asset: string) => void }

export function AssetHeatmap({ onSelectAsset }: Props) {
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
          setItems(Array.isArray(d.items) ? d.items : [])
          setErr(null)
        })
        .catch((e) => { if (alive) setErr(String(e?.message || e)) })
        .finally(() => { if (alive) setLoading(false) })
    }
    void load()
    const t = window.setInterval(load, 30_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  const totalAbs = useMemo(() => {
    let s = 0
    // EXEMPT-HONESTY: 内部求和比例尺,null 当 0 用于归一化,非显示给用户
    for (const it of items) s += Math.abs(it.change24h ?? 0)
    return s || 1
  }, [items])

  if (loading) return <div className="rounded border border-border bg-card p-4 text-xs text-muted-foreground">加载资产热力图…</div>
  if (err) return <div className="rounded border border-short/30 bg-short/10 p-4 text-xs text-short">热力图接口异常：{err}</div>
  if (!items.length) return <div className="rounded border border-border bg-card p-4 text-xs text-muted-foreground">暂无资产数据</div>

  // 按涨跌绝对值降序
  const sorted = [...items].sort(
    // EXEMPT-HONESTY: 排序键,null→0 是中性,排序后正常展示真值或"—"
    (a, b) => Math.abs(b.change24h ?? 0) - Math.abs(a.change24h ?? 0),
  )

  return (
    <div className="rounded border border-border bg-card p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
          🟩🟥 资产涨跌热力图（24h）
        </h3>
        <span className="font-mono text-[10px] text-muted-foreground/70">
          面积∝|涨跌%| · 颜色=方向 · 点按联动 K 线
        </span>
      </div>
      <div className="grid grid-cols-2 gap-1.5 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
        {sorted.map((it) => {
          const chg = it.change24h
          const up = chg == null ? null : chg >= 0
          const ratio = Math.abs(chg ?? 0) / totalAbs
          const intensity = Math.max(40, Math.min(95, 40 + ratio * 55))
          const bg = chg == null
            ? 'bg-muted/60'
            : up
            ? `bg-long/${intensity.toFixed(0)} hover:bg-long`
            : `bg-short/${intensity.toFixed(0)} hover:bg-short`
          const weight = 0.5 + ratio * 1.5
          return (
            <button
              key={it.asset}
              type="button"
              onClick={() => onSelectAsset?.(it.asset)}
              className={`relative flex flex-col items-start justify-between rounded p-2.5 text-left transition-all ${bg}`}
              style={{ minHeight: `${60 * weight}px` }}
            >
              <div>
                <div className="font-mono text-sm font-bold leading-tight text-foreground">{it.asset}</div>
                <div className="font-mono text-[10px] text-muted-foreground/80">{ASSET_META[it.asset] ?? ''}</div>
              </div>
              <div className="mt-1 font-mono text-base font-semibold tabular-nums leading-none text-foreground">
                {chg == null ? '—' : `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`}
              </div>
              <div className="absolute right-1.5 top-1.5 font-mono text-[9px] uppercase tracking-wider text-foreground/60">
                {up == null ? '·' : up ? '▲' : '▼'}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}