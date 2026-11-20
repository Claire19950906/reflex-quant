'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v424 结构性优势体检 — 换掉的不是"再加信号"，而是"优势来源"。
// 检验对象：资金费率(funding) / 多空比(lsratio) / 持仓量(oi) 的分位极端。
// 三关缺一不可：扣费净收益 >0、t ≥ 2、时机置换 p < 0.05；再加一列无条件基线，
// 用来排除"这段行情本来就是单边，随便做空都赚"。

type Stat = {
  n: number
  avg: number | null
  win_rate: number | null
  profit_factor: number | null
  t_stat: number | null
  best: number | null
  worst: number | null
}

type Row = {
  asset: string
  horizon: string
  direction: string
  n: number
  cost_bps: number
  gross: Stat
  net: Stat
  baseline_avg: number | null
  excess_gross: number | null
  timing_p: number | null
  verdict: string
  has_edge: boolean
}

type Rep = {
  kind: string
  contrarian: boolean
  rows: Row[]
  has_any_edge: boolean
  headline: string
  window_days: number
  cached?: boolean
}

type CarryRow = {
  asset: string
  days: number
  points: number
  mean_per_settle_pct: number
  positive_share: number
  ann_pct_1x: number
  ann_pct_3x: number
  cost_bps: number
  breakeven_days: number | null
  verdict: string
}

type CarryRep = { rows: CarryRow[]; best: CarryRow | null; note: string }

const fmt = (v: number | null | undefined, d = 3) =>
  v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`
const pfmt = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : v < 0.001 ? '<0.001' : v.toFixed(3)

export default function EdgePage() {
  const [kind, setKind] = useState('funding')
  const [days, setDays] = useState(90)
  const [rep, setRep] = useState<Rep | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [carry, setCarry] = useState<CarryRep | null>(null)

  const load = useCallback((k: string, d: number) => {
    setBusy(true)
    fetch(`${API_BASE}/edge/scan?kind=${k}&days=${d}&horizons=8h,1d`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) { setRep(j.data); setErr('') }
        else setErr(j?.error || '读取失败')
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'))
      .finally(() => setBusy(false))
  }, [])

  useEffect(() => { load(kind, days) }, [kind, days, load])

  // 套息面板与上面的优势检验无关：它不吃方向，赚的是资金费率租金
  useEffect(() => {
    fetch(`${API_BASE}/edge/carry?windows=30,90,320`)
      .then((r) => r.json())
      .then((j) => { if (j?.ok) setCarry(j.data) })
      .catch(() => {})
  }, [])

  const kinds = [
    { id: 'funding', label: '资金费率' },
    { id: 'lsratio', label: '多空比' },
    { id: 'oi', label: '持仓量' },
  ]

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">结构性优势体检 · 资金费率/持仓/多空比</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            极端分位 → 反向方向 → 真实 K 线结算 · 三关全过才算优势：净&gt;0、t≥2、时机 p&lt;0.05 · 只读
          </p>
        </div>
        <div className="flex flex-wrap gap-1 font-mono text-[11px]">
          {kinds.map((k) => (
            <button
              key={k.id}
              onClick={() => setKind(k.id)}
              className={`rounded-sm border px-2 py-1 ${
                kind === k.id ? 'border-primary text-primary' : 'border-border text-muted-foreground'
              }`}
            >
              {k.label}
            </button>
          ))}
          {[30, 60, 90, 180].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`rounded-sm border px-2 py-1 ${
                days === d ? 'border-primary text-primary' : 'border-border text-muted-foreground'
              }`}
            >
              {d}天
            </button>
          ))}
        </div>
      </header>

      {err && <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">{err}</div>}

      {rep && (
        <>
          <div className="surface-card mb-3 border-l-2 border-amber-400/70 p-3 text-sm">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">结论</span>
            <div className="mt-1">{rep.headline}</div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              窗口 {rep.window_days} 天 · 方向{' '}
              {rep.contrarian ? '反向（拥挤就做反向）' : '顺势'} · 成本统一取 cost_model（BTC/ETH 30bps、SOL 32bps，含滑点）
              {rep.cached ? ' · 命中缓存' : ''}
            </div>
          </div>

          <div className="surface-card overflow-x-auto p-3">
            <table className="w-full font-mono text-[11px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border/60">
                  {['资产', '周期', '方向', '样本', '毛均值', '基线', '超额', '净均值', 't 值', '时机 p', '判定'].map((h, i) => (
                    <th key={h} className={`py-1.5 font-normal ${i >= 3 && i <= 8 ? 'text-right' : 'text-left'}`}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rep.rows.map((r, i) => (
                  <tr key={`${r.asset}-${r.horizon}-${r.direction}-${i}`} className="border-b border-border/30">
                    <td className="py-1.5 font-semibold">{r.asset}</td>
                    <td className="py-1.5">{r.horizon}</td>
                    <td className={`py-1.5 ${r.direction === 'BUY' ? 'text-long' : 'text-short'}`}>
                      {r.direction === 'BUY' ? '做多' : '做空'}
                    </td>
                    <td className="py-1.5 text-right">
                      {r.n}
                      {r.n < 30 && <span className="ml-1 text-amber-400/80">*</span>}
                    </td>
                    <td className={`py-1.5 text-right ${(r.gross.avg || 0) > 0 ? 'text-long' : 'text-short'}`}>
                      {fmt(r.gross.avg)}
                    </td>
                    <td className="py-1.5 text-right text-muted-foreground">{fmt(r.baseline_avg)}</td>
                    <td className={`py-1.5 text-right ${(r.excess_gross || 0) > 0 ? 'text-long' : 'text-short'}`}>
                      {fmt(r.excess_gross)}
                    </td>
                    <td className={`py-1.5 text-right ${(r.net.avg || 0) > 0 ? 'text-long' : 'text-short'}`}>
                      {fmt(r.net.avg)}
                    </td>
                    <td className="py-1.5 text-right">{r.net.t_stat ?? '—'}</td>
                    <td className={`py-1.5 text-right ${r.timing_p !== null && r.timing_p < 0.05 ? 'text-primary' : 'text-muted-foreground'}`}>
                      {pfmt(r.timing_p)}
                    </td>
                    <td className={`py-1.5 pl-3 ${r.has_edge ? 'text-long' : 'text-muted-foreground'}`}>
                      {r.has_edge ? '三关通过' : r.verdict}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">
              * 样本 &lt;30，只能算线索。基线 = 同期同方向"随便一个时点"的平均收益；超额 = 毛均值 − 基线，
              用来判断是真有时点信息，还是那段行情本来就是单边。时机 p = 把时点换成随机，能否复现（越小越好）。
            </p>
          </div>

          {carry && (
            <div className="surface-card mt-3 overflow-x-auto p-3">
              <div className="mb-2 flex items-baseline justify-between gap-3">
                <h2 className="font-mono text-xs font-semibold">资金费率套息（空永续 + 多现货对冲，收 8 小时租金）</h2>
                {carry.best && (
                  <span className="font-mono text-[11px] text-primary">
                    最好：{carry.best.asset} {carry.best.days}天 年化 {carry.best.ann_pct_1x}%（1x）
                  </span>
                )}
              </div>
              <table className="w-full font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    {['资产', '窗口', '点数', '均值/次', '正费率占比', '年化 1x', '年化 3x', '成本', '回本天数', '判定'].map((h, i) => (
                      <th key={h} className={`py-1.5 font-normal ${i >= 2 && i <= 8 ? 'text-right' : 'text-left'}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {carry.rows.map((r, i) => (
                    <tr key={`${r.asset}-${r.days}-${i}`} className="border-b border-border/30">
                      <td className="py-1.5 font-semibold">{r.asset}</td>
                      <td className="py-1.5">{r.days}天</td>
                      <td className="py-1.5 text-right">{r.points}</td>
                      <td className={`py-1.5 text-right ${r.mean_per_settle_pct > 0 ? 'text-long' : 'text-short'}`}>
                        {r.mean_per_settle_pct}%
                      </td>
                      <td className="py-1.5 text-right">{Math.round(r.positive_share * 100)}%</td>
                      <td className={`py-1.5 text-right ${r.ann_pct_1x > 0 ? 'text-long' : 'text-short'}`}>{r.ann_pct_1x}%</td>
                      <td className={`py-1.5 text-right ${r.ann_pct_3x > 0 ? 'text-long' : 'text-short'}`}>{r.ann_pct_3x}%</td>
                      <td className="py-1.5 text-right text-muted-foreground">{r.cost_bps}bps</td>
                      <td className="py-1.5 text-right">{r.breakeven_days ?? '—'}</td>
                      <td className="py-1.5 pl-3 text-muted-foreground">{r.verdict}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">{carry.note}</p>
            </div>
          )}

          <p className="mt-3 font-mono text-[11px] text-muted-foreground/70">
            三条硬规矩：样本不足不下结论、t&lt;2 不算显著、p≥0.05 不算择时。任何一条不过，都不该进实盘或占真钱。
          </p>
        </>
      )}

      {busy && <div className="mt-2 font-mono text-[11px] text-muted-foreground">跑置换检验中…</div>}
    </main>
  )
}
