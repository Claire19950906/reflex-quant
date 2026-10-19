// components/quant/cot-panel.tsx - P0-3 续 CFTC COT (2026-10-18)
// 显示当前 selected asset 的 CFTC Commitments of Traders 周持仓数据
// Data: bridge_server.py /api/cot/recent?asset=X
//
// 关键信号:
//   - noncomm_net (机构净持仓) — 极端高位(>1.5σ) 后市反转 / 极端低位(<-1.5σ) 逼空
//   - spec_index (z-score, 52w window) — 标准化
//   - comm_net (套保者) — 通常与机构反向
//   - nonrept_net (散户) — 反向指标 (与价格同向 = 顶部信号)

'use client'

import { useCallback, useEffect, useState } from 'react'
import { Activity, ChevronDown, ChevronUp, Loader2, Users } from 'lucide-react'
import { apiGet } from '@/lib/api'

type CotHistoryRow = {
  report_date: string
  noncomm_net: number | null
  noncomm_long: number | null
  noncomm_short: number | null
  comm_net: number | null
  nonrept_long: number | null
  nonrept_short: number | null
  change_oi: number | null
  open_interest: number | null
  spec_index: number | null
  traders_total: number | null
  traders_noncomm_long: number | null
  traders_noncomm_short: number | null
}

type CotAsset = {
  asset: string
  contract_code: string
  contract_name: string
  n: number
  latest: CotHistoryRow
  history: CotHistoryRow[]
}

type CotRecent = {
  ok: boolean
  n: number
  assets: CotAsset[]
  weeks: number
}

function fmtInt(n: number | null | undefined, plus = false): string {
  if (n == null) return '—'
  const sign = plus && n > 0 ? '+' : ''
  if (Math.abs(n) >= 1000) return sign + (n / 1000).toFixed(2) + 'k'
  return sign + n.toString()
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return '—'
  return (n * 100).toFixed(1) + '%'
}

// ── Sparkline ────────────────────────────────────────
function Sparkline({ values, w = 180, h = 36 }: { values: (number | null)[]; w?: number; h?: number }) {
  if (!values || values.length < 2) {
    return <div className="text-[9px] text-muted-foreground">no data</div>
  }
  const vs = values.filter((v): v is number => v != null)
  if (vs.length < 2) return <div className="text-[9px] text-muted-foreground">—</div>
  const min = Math.min(...vs)
  const max = Math.max(...vs)
  const range = max - min || 1
  const pts = values.map((v, i) => {
    if (v == null) return null
    const x = (i / (values.length - 1)) * w
    const y = h - ((v - min) / range) * h
    return [x, y] as [number, number]
  })
  const validPts = pts.filter((p): p is [number, number] => p != null)
  const rev = validPts.slice().reverse()
  const path = rev.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')
  const zeroY = h - ((0 - min) / range) * h
  const color = vs[vs.length - 1] > vs[0] ? '#34d399' : (vs[vs.length - 1] < vs[0] ? '#fb7185' : '#94a3b8')
  return (
    <svg width={w} height={h} className="block">
      <line x1="0" y1={zeroY} x2={w} y2={zeroY} stroke="rgba(148,163,184,0.2)" strokeWidth="0.5" strokeDasharray="2 2" />
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  )
}

// ── Spec badge ───────────────────────────────────────
function SpecBadge({ z }: { z: number | null }) {
  if (z == null) return <span className="font-mono text-[10px] text-muted-foreground">—</span>
  let cls = 'text-slate-400 bg-slate-500/10 border-slate-500/30'
  let label = '中性'
  if (z > 1.0) { cls = 'text-emerald-300 bg-emerald-500/15 border-emerald-500/40'; label = '偏多' }
  if (z > 1.8) { cls = 'text-emerald-200 bg-emerald-500/25 border-emerald-400/60'; label = '极多' }
  if (z < -1.0) { cls = 'text-rose-300 bg-rose-500/15 border-rose-500/40'; label = '偏空' }
  if (z < -1.8) { cls = 'text-rose-200 bg-rose-500/25 border-rose-400/60'; label = '极空' }
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] ${cls}`}>
      {z > 0 ? <ChevronUp className="h-2.5 w-2.5" /> : (z < 0 ? <ChevronDown className="h-2.5 w-2.5" /> : null)}
      z={z.toFixed(2)} {label}
    </span>
  )
}


// ── Main panel ───────────────────────────────────────
export function COTPanel({ asset }: { asset: string | null }) {
  const [data, setData] = useState<CotRecent | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)

  const load = useCallback(async () => {
    if (!asset) { setData(null); return }
    setLoading(true); setErr(null)
    try {
      const d = await apiGet<CotRecent>(`/cot/recent?asset=${asset}&weeks=26`)
      if (d.ok) setData(d)
      else setErr('API ok=false')
    } catch (e) { setErr(String(e)) }
    finally { setLoading(false) }
  }, [asset])

  useEffect(() => {
    void load()
    if (!asset) return
    const t = window.setInterval(load, 6 * 60 * 60 * 1000)
    return () => window.clearInterval(t)
  }, [load, asset])

  if (!asset) {
    return (
      <div className="rounded-lg border border-slate-800/60 bg-slate-900/40 p-3 text-[11px] text-muted-foreground">
        <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-400">
          <Activity className="h-3 w-3" /> CFTC COT (机构持仓)
        </div>
        <div className="mt-1 text-[10px]">选择资产查看</div>
      </div>
    )
  }

  if (loading && !data) {
    return (
      <div className="rounded-lg border border-slate-800/60 bg-slate-900/40 p-3 text-[11px] text-muted-foreground">
        <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-400">
          <Activity className="h-3 w-3" /> CFTC COT
        </div>
        <div className="mt-1 flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" /> 拉取中...</div>
      </div>
    )
  }

  if (err) {
    return (
      <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-[11px] text-rose-300">
        COT 错误: {err}
      </div>
    )
  }

  const a = data?.assets?.[0]
  if (!a) {
    return (
      <div className="rounded-lg border border-slate-800/60 bg-slate-900/40 p-3 text-[11px] text-muted-foreground">
        <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-400">
          <Activity className="h-3 w-3" /> CFTC COT
        </div>
        <div className="mt-1 text-[10px]">{asset} 不在 COT 合约白名单</div>
        <div className="mt-0.5 text-[9px] text-muted-foreground">含: BTC/ETH/ES/NQ/GOLD/OIL/SILVER/NG/EUR/AUD/CAD/CORN/WHEAT</div>
      </div>
    )
  }

  const l = a.latest
  const sparkVals = a.history.map(h => h.noncomm_net).reverse()
  const weekChange = (l.change_oi ?? 0)
  const oiChangePct = (l.open_interest && l.change_oi) ? (l.change_oi / (l.open_interest - l.change_oi)) * 100 : null
  const nonrept_net = (l.nonrept_long ?? 0) - (l.nonrept_short ?? 0)

  return (
    <div className="rounded-lg border border-slate-800/60 bg-slate-900/40 p-2.5 text-[11px]">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-400">
          <Activity className="h-3 w-3" />
          CFTC COT · {a.contract_code}
        </div>
        <SpecBadge z={l.spec_index} />
      </div>
      <div className="mt-0.5 text-[9px] text-muted-foreground">{a.contract_name} · {l.report_date}</div>

      <div className="mt-2 flex items-center justify-between">
        <div>
          <div className="font-mono text-[9px] text-muted-foreground">机构净持仓</div>
          <div className={`font-mono text-base font-bold ${(l.noncomm_net ?? 0) > 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
            {fmtInt(l.noncomm_net, true)}
          </div>
        </div>
        <Sparkline values={sparkVals} />
      </div>

      <div className="mt-1.5 grid grid-cols-3 gap-1.5 text-[9px]">
        <div className="rounded border border-slate-800/40 bg-slate-950/40 px-1.5 py-1">
          <div className="text-muted-foreground">未平仓</div>
          <div className="font-mono text-foreground">{fmtInt(l.open_interest)}</div>
          <div className={`font-mono ${(weekChange > 0) ? 'text-emerald-400' : 'text-rose-400'}`}>
            {oiChangePct != null ? (oiChangePct > 0 ? '+' : '') + oiChangePct.toFixed(2) + '%' : '—'}
          </div>
        </div>
        <div className="rounded border border-slate-800/40 bg-slate-950/40 px-1.5 py-1">
          <div className="text-muted-foreground">多 / 空</div>
          <div className="font-mono text-emerald-300">{fmtInt(l.noncomm_long)}</div>
          <div className="font-mono text-rose-300">{fmtInt(l.noncomm_short)}</div>
        </div>
        <div className="rounded border border-slate-800/40 bg-slate-950/40 px-1.5 py-1">
          <div className="text-muted-foreground">套保 (反向)</div>
          <div className="font-mono text-foreground">{fmtInt(l.comm_net, true)}</div>
          <div className="text-muted-foreground">散户 {nonrept_net > 0 ? '+' : ''}{fmtInt(nonrept_net)}</div>
        </div>
      </div>

      <button
        type="button"
        onClick={() => setShowHistory(s => !s)}
        className="mt-1.5 w-full text-left text-[9px] text-muted-foreground hover:text-foreground"
      >
        {showHistory ? '▼ 隐藏历史' : '▶ 显示最近 26 周'}
      </button>
      {showHistory && (
        <div className="mt-1 max-h-32 overflow-y-auto rounded border border-slate-800/40 bg-slate-950/40">
          <table className="w-full text-[9px]">
            <thead className="sticky top-0 bg-slate-900/80 text-muted-foreground">
              <tr>
                <th className="px-1 py-0.5 text-left">周</th>
                <th className="px-1 py-0.5 text-right">机构净</th>
                <th className="px-1 py-0.5 text-right">OI</th>
                <th className="px-1 py-0.5 text-right">z</th>
              </tr>
            </thead>
            <tbody>
              {a.history.map((w) => (
                <tr key={w.report_date} className="border-t border-slate-800/30 font-mono">
                  <td className="px-1 py-0.5 text-foreground">{w.report_date.slice(5)}</td>
                  <td className={`px-1 py-0.5 text-right ${(w.noncomm_net ?? 0) > 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                    {fmtInt(w.noncomm_net, true)}
                  </td>
                  <td className="px-1 py-0.5 text-right text-muted-foreground">{fmtInt(w.open_interest)}</td>
                  <td className="px-1 py-0.5 text-right text-muted-foreground">{w.spec_index != null ? w.spec_index.toFixed(2) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-1 flex items-center gap-1 text-[8px] text-muted-foreground">
        <Users className="h-2.5 w-2.5" />
        {fmtInt(l.traders_total)} 报告人 · 长 {fmtInt(l.traders_noncomm_long)} / 短 {fmtInt(l.traders_noncomm_short)}
      </div>
    </div>
  )
}
