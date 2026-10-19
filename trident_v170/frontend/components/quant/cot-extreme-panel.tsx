// cot-extreme-panel.tsx - COT extreme signals (cross-asset, contrarian)
// Data: /api/cot/heatmap + /api/abnormal-signals?signal_kind=cot_extreme
'use client'

import { useEffect, useState } from 'react'
import { Activity, AlertTriangle, ChevronRight, Loader2, TrendingDown, TrendingUp } from 'lucide-react'
import { fetchWithFallback } from '@/lib/api'

const fmtZ = (z: number) => (z > 0 ? `+${z.toFixed(2)}` : z.toFixed(2))
const fmtNet = (n: number) => {
  const sign = n > 0 ? '+' : n < 0 ? 'â' : ''
  return sign + Math.abs(n).toLocaleString()
}
const fmtTs = (ts: number) => (ts ? new Date(ts * 1000).toISOString().slice(0, 10) : 'â')

export function CotExtremePanel() {
  const [signals, setSignals] = useState<any[]>([])
  const [heat, setHeat] = useState<any | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [emit, setEmit] = useState<{ n: number; ts: number } | null>(null)

  const load = async () => {
    try {
      const [sigResp, heatResp] = await Promise.all([
        fetchWithFallback<{ signals?: any[] }>('/abnormal-signals?signal_kind=cot_extreme&status=all&limit=20'),
        fetchWithFallback<any>('/cot/heatmap?weeks=4'),
      ])
      const list = Array.isArray(sigResp?.signals) ? sigResp.signals : []
      list.sort((a: any, b: any) => Math.abs(b.detail?.spec_index ?? 0) - Math.abs(a.detail?.spec_index ?? 0))
      setSignals(list)
      setHeat(heatResp)
      setErr(null)
    } catch (e: any) {
      setErr(String(e?.message || e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const t = window.setInterval(load, 30_000)
    return () => window.clearInterval(t)
  }, [])

  const onEmit = async () => {
    const r = await fetchWithFallback<{ ok: boolean; n_total: number }>('/cot/emit-now')
    if (r?.ok) { setEmit({ n: r.n_total, ts: Date.now() }); void load() }
  }
  const onPull = async () => {
    const r = await fetchWithFallback<{ ok: boolean; n_total?: number }>('/cot/pull-now')
    if (r?.ok) { setEmit({ n: r.n_total ?? 0, ts: Date.now() }); void load() }
  }

  return (
    <div className="rounded border border-border bg-card p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="flex items-center gap-1 font-mono text-xs uppercase tracking-wider text-muted-foreground">
          <Activity className="h-3.5 w-3.5" />COT Extreme (Contrarian)
        </h3>
        <div className="flex items-center gap-1">
          {heat?.summary && <span className="font-mono text-[9.5px] text-muted-foreground/70">{heat.summary.n_extreme}/{heat.summary.n_assets}</span>}
          <button type="button" onClick={onEmit} className="rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] hover:bg-amber-500/10">emit</button>
          <button type="button" onClick={onPull} className="rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] hover:bg-blue-500/10">pull</button>
        </div>
      </div>

      {loading ? <div className="flex h-20 items-center justify-center text-xs text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" />loading...</div>
       : err ? <div className="rounded border border-rose-700/30 bg-rose-500/10 p-2 text-xs text-rose-300"><AlertTriangle className="mr-1 inline h-3 w-3" />COT API error: {err}</div>
       : signals.length === 0 ? <div className="rounded border border-border bg-muted/30 p-3 text-xs text-muted-foreground">No COT extreme signals (|z| &lt; {heat?.thresholds?.extreme ?? 1.5})</div>
       : <ul className="space-y-1.5">
           {signals.map((s: any) => {
             const z = s.detail?.spec_index ?? 0
             const net = s.detail?.noncomm_net ?? 0
             const isBull = s.direction === 'bullish'
             return (
               <li key={s.id} className={`rounded border px-2 py-1.5 ${isBull ? "border-emerald-700/30 bg-emerald-500/5" : "border-rose-700/30 bg-rose-500/5"}`}>
                 <div className="flex w-full flex-wrap items-center gap-x-2 gap-y-1">
                   <span className="font-mono text-[11px] font-semibold text-foreground">{s.asset}</span>
                   <span className={`font-mono text-[11px] font-bold ${isBull ? "text-emerald-300" : "text-rose-300"}`}>z={fmtZ(z)}</span>
                   <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${isBull ? "border-emerald-700/40 text-emerald-300" : "border-rose-700/40 text-rose-300"}`}>
                     {isBull ? "Contrarian BUY" : "Contrarian SELL"}
                   </span>
                   <span className="ml-auto font-mono text-[10px] text-muted-foreground">{fmtTs(s.ts)}</span>
                 </div>
                 <div className="mt-0.5 pl-1 font-mono text-[10px] text-muted-foreground/80">
                   spec_net <span className="text-foreground/90">{fmtNet(net)}</span>
                 </div>
                 <div className="mt-1 truncate font-mono text-[10px] text-muted-foreground/80" title={s.hint}>
                   <ChevronRight className="mr-0.5 inline h-2.5 w-2.5" />{s.hint}
                 </div>
               </li>
             )
           })}
         </ul>}
    </div>
  )
}


export default CotExtremePanel
