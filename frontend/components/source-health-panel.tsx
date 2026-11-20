'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v422 源健康面板 — 挂在 /sources 顶部。
//
// 为什么需要它：原来的三态（在线/报错/离线）分不清
//   「没配 key」/「三天前停更」/「能出但一半请求失败」，
// 于是"新闻变少了"只能靠感觉。这里按入库量算分，并把停更单独点名单。

type Item = {
  source: string
  label: string
  class: 'healthy' | 'flaky' | 'stalled' | 'dead' | 'not_enabled' | 'no_data'
  score: number
  n24: number
  n7: number
  fail_rate: number | null
  idle_h: number | null
  last_at: string
  last_error: string
  suggestion: string
}

type Rep = {
  source_total: number
  counts: Record<string, number>
  active_24h: number
  intake_24h: number
  concentration_top3: number | null
  stalled: { source: string; label: string; n7: number; idle_h: number | null }[]
  items: Item[]
  caveat: string
}

const CLS_META: Record<string, { label: string; cls: string }> = {
  healthy: { label: '正常', cls: 'border-emerald-500/50 text-long' },
  flaky: { label: '半残', cls: 'border-amber-400/50 text-amber-400' },
  stalled: { label: '停更', cls: 'border-amber-400/50 text-amber-400' },
  dead: { label: '已死', cls: 'border-red-500/50 text-short' },
  not_enabled: { label: '未启用', cls: 'border-slate-500/50 text-muted-foreground' },
  no_data: { label: '无数据', cls: 'border-slate-500/50 text-muted-foreground' },
}

export default function SourceHealthPanel() {
  const [rep, setRep] = useState<Rep | null>(null)
  const [err, setErr] = useState('')
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const load = () => {
      fetch(`${API_BASE}/sources/health?hours=24&days=7`)
        .then((r) => r.json())
        .then((j) => {
          if (j && Array.isArray(j.items)) { setRep(j); setErr('') }
          else setErr('读取失败')
        })
        .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'))
    }
    load()
    const t = window.setInterval(load, 60000)
    return () => window.clearInterval(t)
  }, [])

  if (err) {
    return <div className="surface-card mb-4 border border-amber-400/40 p-3 text-xs text-amber-400">
      源健康面板读取失败：{err}（下面那张三态清单不受影响）
    </div>
  }
  if (!rep) return null

  const c = rep.counts || {}
  const stalledTop = rep.stalled.slice(0, 5)

  return (
    <div className="surface-card mb-4 p-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
          源健康度（近 24h 入库口径）
        </div>
        <div className="font-mono text-[11px] text-muted-foreground">
          {rep.source_total} 源 · 活跃 {rep.active_24h} · 入库 {rep.intake_24h} 条 ·
          top3 占 {(rep.concentration_top3 === null ? '—' : `${(rep.concentration_top3 * 100).toFixed(0)}%`)}
        </div>
      </div>

      <div className="mb-2 flex flex-wrap gap-2 font-mono text-[11px]">
        {(['healthy', 'flaky', 'stalled', 'dead', 'no_data', 'not_enabled'] as const).map((k) => (
          <span key={k} className={`rounded-sm border px-1.5 py-0.5 ${CLS_META[k].cls}`}>
            {CLS_META[k].label} {c[k] ?? 0}
          </span>
        ))}
        <button
          onClick={() => setOpen((v) => !v)}
          className="rounded-sm border border-border px-1.5 py-0.5 text-muted-foreground"
        >
          {open ? '收起明细' : '看明细'}
        </button>
      </div>

      {stalledTop.length > 0 && (
        <div className="mb-2 font-mono text-[11px] text-amber-400">
          停更点名：
          {stalledTop.map((s) =>
            `${s.label}（近7天 ${s.n7} 条／${s.idle_h ?? '?'}h 没动）`).join('；')}
        </div>
      )}

      {open && (
        <div className="thin-scroll max-h-[320px] overflow-y-auto">
          <table className="w-full font-mono text-[11px]">
            <thead className="text-muted-foreground">
              <tr className="border-b border-border/60">
                {['源', '分类', '分', '24h', '7d', '失败率', '最后入库', '建议'].map((h, i) => (
                  <th key={h} className={`py-1.5 font-normal ${i <= 1 || i >= 6 ? 'text-left' : 'text-right'}`}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rep.items.map((it) => (
                <tr key={it.source} className="border-b border-border/30 align-top">
                  <td className="py-1.5">{it.label || it.source}</td>
                  <td className="py-1.5">
                    <span className={CLS_META[it.class]?.cls || ''}>{CLS_META[it.class]?.label || it.class}</span>
                  </td>
                  <td className="py-1.5 text-right">{it.score}</td>
                  <td className="py-1.5 text-right">{it.n24}</td>
                  <td className="py-1.5 text-right">{it.n7}</td>
                  <td className="py-1.5 text-right">{it.fail_rate === null ? '—' : `${(it.fail_rate * 100).toFixed(0)}%`}</td>
                  <td className="py-1.5 pl-3 text-left text-muted-foreground">{it.last_at || '—'}</td>
                  <td className="py-1.5 pl-3 text-left text-muted-foreground/80">{it.suggestion}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">{rep.caveat}</p>
    </div>
  )
}
