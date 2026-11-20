'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { DecisionQuickActions } from '@/components/quant/decision-quick-actions'
import { CapabilityPanel } from '@/components/quant/capability-panel'

// v380 反事实四条腿 — 单笔决策重放：实际 / 不进场 / 反向 / 延迟 / 半仓
// 目的：把「这笔为什么亏」拆成 方向 / 时点 / 仓位 三个可判定的问题

type Leg = {
  key: string
  label: string
  direction: number
  entry_ts: number | null
  entry_price: number | null
  exit_price: number | null
  horizon_minutes: number
  pnl_pct: number | null
  mfe_pct: number | null
  mae_pct: number | null
  note: string
}

type Replay = {
  ok: boolean
  reason?: string
  hint?: string
  decision_id?: number
  asset?: string
  event_type?: string
  timeframe?: string
  available_timeframes?: string[]
  action?: string
  is_hold?: boolean
  holding_horizon_minutes?: number
  stored?: { forward_pnl: number | null; mfe_pct: number | null; mae_pct: number | null }
  drift_pct?: number
  legs?: Leg[]
  best_leg?: string | null
  worst_leg?: string | null
  spread_pct?: number | null
}

type RecentItem = {
  id: number
  asset: string
  event_type?: string
  action: string
  entry_time: string
  pnl: number
  settled: number
}

const LEG_COLOR: Record<string, string> = {
  actual: 'bg-sky-500/70',
  skip: 'bg-slate-500/60',
  reverse: 'bg-fuchsia-500/70',
  delay: 'bg-amber-500/70',
  half: 'bg-emerald-500/70',
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return 'text-muted-foreground'
  if (v > 0) return 'text-emerald-500'
  if (v < 0) return 'text-red-500'
  return 'text-muted-foreground'
}

export default function CounterfactualPage() {
  const [recent, setRecent] = useState<RecentItem[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [replay, setReplay] = useState<Replay | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')

  const loadRecent = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/v1/counterfactual/recent?limit=40`)
      const json = await res.json()
      if (json?.ok) {
        setRecent(json.items || [])
        setSelected((cur) => cur ?? json.items?.[0]?.id ?? null)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '最近决策加载失败')
    }
  }, [])

  const loadDetail = useCallback(async (id: number) => {
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`${API_BASE}/v1/counterfactual/${id}`)
      const json = await res.json()
      setReplay(json)
      if (!json?.ok) setError(json?.hint || json?.reason || '该笔无法重放')
    } catch (e) {
      setError(e instanceof Error ? e.message : '重放失败')
      setReplay(null)
    } finally {
      setLoading(false)
    }
  }, [])

  // 支持从历史回溯页跳转：/counterfactual?id=11555 直接选中该笔
  useEffect(() => {
    const raw = new URLSearchParams(window.location.search).get('id')
    const n = raw ? Number(raw) : NaN
    if (Number.isFinite(n) && n > 0) setSelected(n)
  }, [])

  useEffect(() => { void loadRecent() }, [loadRecent])
  useEffect(() => { if (selected) void loadDetail(selected) }, [selected, loadDetail])

  const legs = replay?.legs ?? []
  const maxAbs = useMemo(
    () => Math.max(1, ...legs.map((l) => Math.abs(l.pnl_pct ?? 0))),
    [legs],
  )

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="font-mono text-sm font-semibold tracking-wider">反事实四条腿</h1>
          {selected ? (
            <button
              type="button"
              onClick={() => {
                const short = `${window.location.origin}/d/${selected}`
                void navigator.clipboard?.writeText(short)
                setCopied(short)
              }}
              className="rounded border border-border px-2 py-0.5 font-mono text-[11px] text-muted-foreground transition hover:border-primary hover:text-foreground"
              title="复制短链，粘到飞书/微信里可直接打开这一笔"
            >
              复制短链 /d/{selected}
            </button>
          ) : null}
          {copied && <span className="font-mono text-[11px] text-emerald-500">已复制</span>}
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          同一时间窗重放：实际 / 不进场 / 反向 / 延迟 / 半仓。方向错、时点错、仓位错，一眼分开。
        </p>
      </header>

      <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
        <aside className="surface-card max-h-[70vh] overflow-y-auto p-2 thin-scroll">
          <div className="px-2 py-1 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
            最近决策
          </div>
          {recent.map((r) => (
            <button
              key={r.id}
              type="button"
              onClick={() => setSelected(r.id)}
              className={`mb-1 flex w-full items-center gap-2 rounded px-2 py-1.5 text-left font-mono text-[11px] transition ${
                selected === r.id ? 'bg-secondary text-foreground' : 'text-muted-foreground hover:bg-secondary/60'
              }`}
            >
              <span className="w-10 shrink-0">#{r.id}</span>
              <span className="w-12 shrink-0">{r.asset}</span>
              <span className="w-12 shrink-0">{r.action}</span>
              <span className={`ml-auto ${pnlClass(r.pnl)}`}>{fmtPct(r.pnl)}</span>
            </button>
          ))}
          {!recent.length && <div className="px-2 py-3 text-xs text-muted-foreground">暂无可用决策</div>}
        </aside>

        <section className="space-y-3">
          {error && (
            <div className="surface-card border border-amber-500/30 p-3 text-xs text-amber-400">
              {error}
              {replay?.available_timeframes?.length ? `（可用周期：${replay.available_timeframes.join(' / ')}）` : ''}
            </div>
          )}

          {loading && <div className="surface-card p-6 text-xs text-muted-foreground">重放中…</div>}

          {!loading && replay?.ok && (
            <>
              <div className="surface-card flex flex-wrap items-center gap-x-4 gap-y-1 p-3 font-mono text-[11px]">
                <span className="text-foreground">#{replay.decision_id} · {replay.asset} · {replay.action}</span>
                <span className="text-muted-foreground">周期 {replay.timeframe}</span>
                <span className="text-muted-foreground">窗口 {replay.holding_horizon_minutes} 分钟</span>
                <span className="text-muted-foreground">
                  库内结算 {fmtPct(replay.stored?.forward_pnl ?? null)}
                </span>
                <span className={Math.abs(replay.drift_pct ?? 0) > 1 ? 'text-amber-400' : 'text-muted-foreground'}>
                  重算漂移 {fmtPct(replay.drift_pct ?? null)}
                </span>
                <span className="ml-auto text-muted-foreground">
                  最好 {replay.best_leg} · 最差 {replay.worst_leg} · 极差 {fmtPct(replay.spread_pct ?? null)}
                </span>
              </div>

              <div className="surface-card p-3">
                <DecisionQuickActions
                  decisionId={replay.decision_id ?? 0}
                  asset={replay.asset}
                  eventType={replay.event_type}
                />
                <div className="mt-2 font-mono text-[11px] text-muted-foreground">
                  静默会被写入口真实拦截；忽略把该笔移出统计；反手只记影子盈亏，不下真实单。
                </div>
              </div>

              <div className="surface-card overflow-x-auto p-3">
                <table className="w-full min-w-[720px] font-mono text-[11px]">
                  <thead className="text-muted-foreground">
                    <tr className="border-b border-border/60">
                      <th className="py-1.5 text-left">路径</th>
                      <th className="py-1.5 text-right">方向</th>
                      <th className="py-1.5 text-right">入场</th>
                      <th className="py-1.5 text-right">出场</th>
                      <th className="py-1.5 text-right">盈亏</th>
                      <th className="py-1.5 text-right">MFE</th>
                      <th className="py-1.5 text-right">MAE</th>
                      <th className="py-1.5 pl-3 text-left">说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {legs.map((l) => (
                      <tr key={l.key} className="border-b border-border/30">
                        <td className="py-1.5">
                          <span className="inline-flex items-center gap-1.5">
                            <span className={`inline-block h-1.5 w-4 rounded-sm ${LEG_COLOR[l.key] || 'bg-slate-500'}`} />
                            {l.label}
                          </span>
                        </td>
                        <td className="py-1.5 text-right">{l.direction > 0 ? '多' : l.direction < 0 ? '空' : '—'}</td>
                        <td className="py-1.5 text-right">{l.entry_price ?? '—'}</td>
                        <td className="py-1.5 text-right">{l.exit_price ?? '—'}</td>
                        <td className={`py-1.5 text-right ${pnlClass(l.pnl_pct)}`}>{fmtPct(l.pnl_pct)}</td>
                        <td className="py-1.5 text-right text-emerald-500/80">{fmtPct(l.mfe_pct)}</td>
                        <td className="py-1.5 text-right text-red-500/80">{fmtPct(l.mae_pct)}</td>
                        <td className="py-1.5 pl-3 text-muted-foreground">{l.note || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="surface-card p-3">
                <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                  盈亏对比（同尺度）
                </div>
                <div className="space-y-2">
                  {legs.map((l) => {
                    const v = l.pnl_pct ?? 0
                    const w = Math.min(100, (Math.abs(v) / maxAbs) * 100)
                    return (
                      <div key={l.key} className="flex items-center gap-2">
                        <span className="w-28 shrink-0 font-mono text-[11px] text-muted-foreground">{l.label}</span>
                        <div className="relative h-3 flex-1 overflow-hidden rounded bg-secondary">
                          <div
                            className={`absolute inset-y-0 ${v >= 0 ? 'left-1/2' : 'right-1/2'} ${v >= 0 ? 'bg-emerald-500/70' : 'bg-red-500/70'}`}
                            style={{ width: `${w / 2}%` }}
                          />
                          <div className="absolute inset-y-0 left-1/2 w-px bg-border" />
                        </div>
                        <span className={`w-16 shrink-0 text-right font-mono text-[11px] ${pnlClass(v)}`}>{fmtPct(v)}</span>
                      </div>
                    )
                  })}
                </div>
              </div>
            </>
          )}

          {!loading && !replay && !error && (
            <div className="surface-card p-6 text-xs text-muted-foreground">左侧选一笔决策开始重放</div>
          )}
        </section>
      </div>

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="假设单能力"
        hint="影子单 / 假设单 / 影子账本（只记不下单）"
        featureIds={['F15', 'F48']}
      />
      </section>
    </main>
  )
}
