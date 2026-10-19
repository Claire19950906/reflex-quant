'use client'

import { useEffect, useState } from 'react'
import { API_BASE, fetchWithFallback } from '@/lib/api'
import { Loader2 } from 'lucide-react'

// ── 异动识别（K 线异动列表）──────────────────────────────────────
// /api/abnormal-signals?signal_kind=multi_factor

type AbnormalSignal = {
  id: number
  ts: number
  asset: string
  signal_kind: string
  direction: string
  anchor_ts: number | null
  detail: {
    // 后端 factors 是 string 列表（因子标签）；其它字段都是字符串/数值
    factors?: string[]
    rsi?: number | null
    rsi_divergence?: string | null
    volume_divergence?: string | null
  }
  hint: string
  status: string
  created_at: string
}

const SH_OFFSET_MS = 8 * 3600 * 1000
function fmtTs(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000 + SH_OFFSET_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
}

export function AnomalyTypePanel() {
  const [signals, setSignals] = useState<AbnormalSignal[]>([])
  const [sigLoading, setSigLoading] = useState(true)
  const [sigErr, setSigErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => {
      // 8000 一直 500, fetchWithFallback 会自动 fallback 到 8100 bridge
      fetchWithFallback<{ signals?: AbnormalSignal[] }>(
        '/abnormal-signals?signal_kind=multi_factor&status=all&limit=12'
      )
        .then((d) => {
          if (!alive) return
          setSignals(Array.isArray(d?.signals) ? d.signals : [])
          setSigErr(null)
        })
        .catch((e) => { if (alive) setSigErr(String(e?.message || e)) })
        .finally(() => { if (alive) setSigLoading(false) })
    }
    void load()
    const t = window.setInterval(load, 30_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  return (
    <div className="rounded border border-border bg-card p-3">
      {/* K 线异动列表 */}
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
          📈 K 线异动识别（multi_factor）
        </h3>
        <span className="font-mono text-[10px] text-muted-foreground/70">
          多因子 + RSI 背离 + 量价背离
        </span>
      </div>
      {sigLoading ? (
        <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />加载异动签名…
        </div>
      ) : sigErr ? (
        <div className="rounded border border-short/30 bg-short/10 p-2 text-xs text-short">
          异动接口异常：{sigErr}
        </div>
      ) : signals.length === 0 ? (
        <div className="rounded border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
          最近没有多因子异动签名（阈值未命中 或 检测器尚未启动）
        </div>
      ) : (
        <ul className="space-y-1.5">
          {signals.slice(0, 8).map((s) => {
            const up = (s.direction || '').toLowerCase() === 'bullish' || (s.direction || '').toUpperCase() === 'BUY'
            const dn = (s.direction || '').toLowerCase() === 'bearish' || (s.direction || '').toUpperCase() === 'SELL'
            const dirColor = up ? 'text-long' : dn ? 'text-short' : 'text-muted-foreground'
            const dirBg = up ? 'bg-long/15' : dn ? 'bg-short/15' : 'bg-muted/40'
            const factors = s.detail?.factors ?? []
            return (
              <li key={s.id} className="rounded border border-border bg-card/40 px-2 py-1.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-[11px] font-semibold text-foreground">{s.asset}</span>
                  <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${dirColor} ${dirBg}`}>
                    {s.direction || 'unknown'}
                  </span>
                  <span className="font-mono text-[10px] text-muted-foreground">{fmtTs(s.ts)}</span>
                  <span className={`ml-auto rounded px-1 py-0.5 font-mono text-[9px] ${s.status === 'open' ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300' : 'bg-muted/40 text-muted-foreground'}`}>
                    {s.status}
                  </span>
                </div>
                {factors.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1 font-mono text-[10px] text-muted-foreground/85">
                    {factors.map((f, i) => (
                      <span key={i} className="rounded bg-primary/15 px-1 py-0.5 text-primary">
                        {f}
                      </span>
                    ))}
                  </div>
                )}
                {s.detail?.rsi_divergence && (
                  <div className="mt-1 font-mono text-[10px] text-amber-600 dark:text-amber-300">
                    ⚠ RSI {s.detail.rsi != null ? `(${s.detail.rsi.toFixed(1)}) ` : ''}{s.detail.rsi_divergence}
                    {s.detail.volume_divergence ? ` · 量价 ${s.detail.volume_divergence}` : ''}
                  </div>
                )}
                {s.hint && (
                  <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground/70" title={s.hint}>
                    💡 {s.hint}
                  </p>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}