'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { LedgerReconcileCard } from '@/components/profit/ledger-reconcile'

// v423 能不能赚钱复检器 — 同一批历史信号，换持有期、换成本档，净期望是正是负。
// 结论全部由后端算（routers/profit.py → cost_horizon_eval.py），页面只做展示，
// 不重算、不改口径。

type Stat = {
  n: number
  avg: number | null
  median: number | null
  win_rate: number | null
  profit_factor: number | null
  t_stat: number | null
  best: number | null
  worst: number | null
}

type Cell = {
  horizon: string
  tf: string
  cost_bps: number
  cost_pct: number
  gross: Stat
  net: Stat
  avg_abs_move: number
  cost_to_move: number | null
  verdict: string
  profitable: boolean
}

type Rep = {
  entries: number
  skipped: Record<string, number>
  cells: Cell[]
  by_asset: { asset: string; horizon: string; cost_bps: number; net: Stat; verdict: string }[]
  best_cell: Cell | null
  missing_bars: string[]
  window_days: number
  assets: string[]
  headline: string
  note: string
}

const fmt = (v: number | null | undefined, d = 3) =>
  v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`

export default function ProfitPage() {
  const [days, setDays] = useState(60)
  const [rep, setRep] = useState<Rep | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback((d: number) => {
    setBusy(true)
    fetch(`${API_BASE}/profit/scan?days=${d}`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) { setRep(j.data); setErr('') }
        else setErr(j?.error || '读取失败')
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'))
      .finally(() => setBusy(false))
  }, [])

  useEffect(() => { load(days) }, [days, load])

  const horizons = rep ? Array.from(new Set(rep.cells.map((c) => c.horizon))) : []
  const costs = rep ? Array.from(new Set(rep.cells.map((c) => c.cost_bps))).sort((a, b) => b - a) : []
  const cellAt = (h: string, bps: number) => rep?.cells.find((c) => c.horizon === h && c.cost_bps === bps)

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">能不能赚钱 · 成本×持有期复检</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            同一批历史信号，用真实 K 线在不同持有期重结算，再叠加不同成本档 · 只读
          </p>
        </div>
        <div className="flex gap-1 font-mono text-[11px]">
          {[30, 60, 120, 180].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`rounded-sm border px-2 py-1 ${
                days === d ? 'border-primary text-primary' : 'border-border text-muted-foreground'
              }`}
            >
              近 {d} 天
            </button>
          ))}
        </div>
      </header>

      {err && <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">{err}</div>}

      <LedgerReconcileCard />

      {rep && (
        <>
          <div className="surface-card mb-3 border-l-2 border-amber-400/70 p-3 text-sm">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">结论</span>
            <div className="mt-1">{rep.headline}</div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              样本 {rep.entries} 笔 · 窗口 {rep.window_days} 天 · 跳过（持有期未走完/空洞）：
              {Object.entries(rep.skipped || {}).map(([k, v]) => `${k} ${v}`).join('，') || '无'}
            </div>
          </div>

          <div className="surface-card mb-3 overflow-x-auto p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              净期望矩阵（每笔，扣成本后）
            </div>
            <table className="w-full font-mono text-[11px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border/60">
                  <th className="py-1.5 text-left font-normal">持有期</th>
                  {costs.map((bps) => (
                    <th key={bps} className="py-1.5 text-right font-normal">{bps} bps</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {horizons.map((h) => (
                  <tr key={h} className="border-b border-border/30">
                    <td className="py-1.5 font-semibold">{h}</td>
                    {costs.map((bps) => {
                      const c = cellAt(h, bps)
                      const v = c?.net.avg
                      return (
                        <td
                          key={bps}
                          className={`py-1.5 text-right ${
                            v === null || v === undefined
                              ? 'text-muted-foreground'
                              : v > 0
                              ? 'text-long'
                              : 'text-short'
                          }`}
                          title={c ? `n=${c.net.n} 胜率=${c.net.win_rate} t=${c.net.t_stat}` : ''}
                        >
                          {fmt(v)}
                          {c && (
                            <span className="ml-1 text-muted-foreground/70">
                              n={c.net.n}
                            </span>
                          )}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">
              绿=扣费后每笔为正（不代表显著）· 红=为负。鼠标悬停看样本数/胜率/t 值。
              成本档：30bps 现货吃单双边 · 10/5bps 合约与做市 · 2bps 理想返佣 · 0bps 只用来验证信号本身有没有优势。
            </p>
          </div>

          <div className="surface-card mb-3 p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              信号本身有没有优势（0bps 档，剔除成本看毛收益）
            </div>
            <table className="w-full font-mono text-[11px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border/60">
                  {['持有期', '毛均值', '胜率', '盈亏比', 't 值', '平均绝对波动', '成本/波动', '判定'].map((h, i) => (
                    <th key={h} className={`py-1.5 font-normal ${i === 0 || i >= 5 ? 'text-left' : 'text-right'}`}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {horizons.map((h) => {
                  const c = cellAt(h, 0)
                  if (!c) return null
                  return (
                    <tr key={h} className="border-b border-border/30">
                      <td className="py-1.5 font-semibold">{h}</td>
                      <td className={`py-1.5 text-right ${(c.gross.avg || 0) > 0 ? 'text-long' : 'text-short'}`}>
                        {fmt(c.gross.avg)}
                      </td>
                      <td className="py-1.5 text-right">
                        {c.gross.win_rate === null ? '—' : `${(c.gross.win_rate * 100).toFixed(0)}%`}
                      </td>
                      <td className="py-1.5 text-right">{c.gross.profit_factor ?? '—'}</td>
                      <td className="py-1.5 text-right">{c.gross.t_stat ?? '—'}</td>
                      <td className="py-1.5 pl-3 text-left">{c.avg_abs_move}%</td>
                      <td className="py-1.5 pl-3 text-left">{c.cost_to_move ?? '—'}</td>
                      <td className="py-1.5 pl-3 text-left text-muted-foreground">{c.verdict}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {rep.by_asset.length > 0 && rep.best_cell && (
            <div className="surface-card p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                分品种（在最优格子 {rep.best_cell.horizon} / {rep.best_cell.cost_bps}bps 上展开）
              </div>
              <table className="w-full font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    {['品种', '样本', '净均值', '胜率', 't 值', '判定'].map((h, i) => (
                      <th key={h} className={`py-1.5 font-normal ${i === 0 || i === 5 ? 'text-left' : 'text-right'}`}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rep.by_asset.map((a) => (
                    <tr key={a.asset} className="border-b border-border/30">
                      <td className="py-1.5 font-semibold">{a.asset}</td>
                      <td className="py-1.5 text-right">{a.net.n}</td>
                      <td className={`py-1.5 text-right ${(a.net.avg || 0) > 0 ? 'text-long' : 'text-short'}`}>
                        {fmt(a.net.avg)}
                      </td>
                      <td className="py-1.5 text-right">
                        {a.net.win_rate === null ? '—' : `${(a.net.win_rate * 100).toFixed(0)}%`}
                      </td>
                      <td className="py-1.5 text-right">{a.net.t_stat ?? '—'}</td>
                      <td className="py-1.5 pl-3 text-left text-muted-foreground">{a.verdict}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="mt-3 font-mono text-[11px] text-muted-foreground/70">{rep.note}</p>
          {rep.missing_bars.length > 0 && (
            <p className="mt-1 font-mono text-[11px] text-amber-400">
              缺 K 线、无法复检的品种：{rep.missing_bars.join('、')}（这些格子的结论不覆盖它们）
            </p>
          )}
          <p className="mt-1 font-mono text-[11px] text-muted-foreground/70">
            t 值 ≥2 才算统计显著。本页故意不把"净均值为正"说成"能赚钱"——2030 笔里所有正格子都 t&lt;2。
          </p>
        </>
      )}

      {busy && <div className="mt-2 font-mono text-[11px] text-muted-foreground">结算中…</div>}
    </main>
  )
}
