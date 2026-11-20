'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

/**
 * v476 · 生效门槛面板（挂在 /guards 开仓闸）
 *
 * v475 把"可信度高就抬高买入门槛"接进了决策，但 `buy_min_effective` 只活在
 * 决策返回值里 —— 关掉窗口就没了。复盘最常问的"这单为什么没买"，
 * 以前只能读 reasoning 文字猜。现在每次决策把三层门槛落一行：
 *   原始 buy_min → 分组校准 buy_min_calibrated → 扣掉可信度收紧量后的 buy_min_effective
 *
 * 只读 `decision_threshold_snapshot`，不重算、不反推历史。
 */

type Item = {
  decision_id: number
  ts: number
  asset: string
  action: string
  buy_min: number | null
  sell_min: number | null
  buy_min_calibrated: number | null
  sell_min_calibrated: number | null
  buy_min_effective: number | null
  sell_min_effective: number | null
  confidence: number | null
  confidence_penalty: number | null
  action_before_gate: string | null
  action_after_gate: string | null
  gate_changed: number
}

type Resp = {
  ok: boolean
  items?: Item[]
  stats?: { n?: number; gate_changed?: number; avg_penalty?: number | null }
  notes?: string[]
  detail?: string
}

function fmtTs(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function f(v: number | null, digits = 3): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits)
}

export function ThresholdPanel() {
  const [data, setData] = useState<Resp | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const ac = new AbortController()
    const timer = setTimeout(() => ac.abort(), 15_000)
    fetch(`${API_BASE}/decision-thresholds/recent?days=7&limit=30`, { signal: ac.signal, cache: 'no-store' })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return (await r.json()) as Resp
      })
      .then((d) => (d?.ok ? setData(d) : Promise.reject(new Error(d?.detail || 'ok=false'))))
      .catch((e: unknown) => {
        if (!ac.signal.aborted) setErr(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        clearTimeout(timer)
        if (!ac.signal.aborted) setLoading(false)
      })
    return () => {
      clearTimeout(timer)
      ac.abort()
    }
  }, [])

  const items = data?.items || []
  const stats = data?.stats || {}

  return (
    <section className="rounded-lg border border-border bg-card/40">
      <div className="flex flex-wrap items-baseline gap-x-3 border-b border-border/70 px-3 py-2">
        <h3 className="font-mono text-[12px] font-semibold text-foreground">生效门槛（最近 7 天决策）</h3>
        <span className="text-[11px] text-muted-foreground">
          原始 → 校准 → 扣可信度后生效；被压低导致动作改变的单独占一列
        </span>
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">
          {err ? <span className="text-short">接口未响应</span> : loading ? '载入中…' : `${stats.n ?? 0} 条`}
        </span>
      </div>

      {err ? (
        <div className="px-3 py-2 text-[11px] text-muted-foreground">
          门槛表当前不可读（{err}）。这里不编数据。
        </div>
      ) : items.length === 0 && !loading ? (
        <div className="px-3 py-2 text-[11px] text-muted-foreground">
          还没有落库的门槛记录 —— 门槛表从这次改动开始写，历史决策未留门槛，不反推。
          下一条决策进来后这里会出现第一行。
        </div>
      ) : (
        <>
          <div className="flex flex-wrap gap-x-4 gap-y-1 px-3 pt-2 font-mono text-[11px] text-muted-foreground">
            <span>
              决策 <span className="text-foreground">{stats.n ?? 0}</span>
            </span>
            <span>
              被门槛改变动作 <span className={stats.gate_changed ? 'text-short' : 'text-foreground'}>{stats.gate_changed ?? 0}</span>
            </span>
            <span>
              平均压低量 <span className="text-foreground">{stats.avg_penalty === null || stats.avg_penalty === undefined ? '—' : stats.avg_penalty.toFixed(3)}</span>
            </span>
          </div>
          <div className="overflow-x-auto px-3 pb-2 pt-1">
            <table className="w-full min-w-[760px] text-[11px]">
              <thead>
                <tr className="border-b border-border/60 text-left font-mono text-[10px] text-muted-foreground">
                  <th className="py-1 pr-2 font-normal">时间</th>
                  <th className="py-1 pr-2 font-normal">标的</th>
                  <th className="py-1 pr-2 font-normal">动作</th>
                  <th className="py-1 pr-2 text-right font-normal">买阈 原始</th>
                  <th className="py-1 pr-2 text-right font-normal">校准</th>
                  <th className="py-1 pr-2 text-right font-normal">生效</th>
                  <th className="py-1 pr-2 text-right font-normal">卖阈 生效</th>
                  <th className="py-1 pr-2 text-right font-normal">可信度</th>
                  <th className="py-1 pr-2 text-right font-normal">压低</th>
                  <th className="py-1 font-normal">门槛作用</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => (
                  <tr key={it.decision_id} className="border-b border-border/40">
                    <td className="py-1 pr-2 font-mono text-muted-foreground">{fmtTs(it.ts)}</td>
                    <td className="py-1 pr-2 font-medium text-foreground">{it.asset || '—'}</td>
                    <td className="py-1 pr-2 font-mono">{it.action || '—'}</td>
                    <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{f(it.buy_min)}</td>
                    <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{f(it.buy_min_calibrated)}</td>
                    <td className="py-1 pr-2 text-right font-mono text-foreground">{f(it.buy_min_effective)}</td>
                    <td className="py-1 pr-2 text-right font-mono">{f(it.sell_min_effective)}</td>
                    <td className="py-1 pr-2 text-right font-mono">{f(it.confidence, 2)}</td>
                    <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{f(it.confidence_penalty)}</td>
                    <td className="py-1 font-mono">
                      {it.gate_changed ? (
                        <span className="text-short">{it.action_before_gate} → {it.action_after_gate}（被压）</span>
                      ) : (
                        <span className="text-muted-foreground/70">未改变动作</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(data?.notes || []).length > 0 && (
            <div className="px-3 pb-2 font-mono text-[10px] text-muted-foreground/70">
              {(data?.notes || []).map((n) => (
                <div key={n}>· {n}</div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}
