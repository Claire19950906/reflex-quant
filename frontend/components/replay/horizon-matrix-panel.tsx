'use client'

/**
 * horizon-matrix-panel.tsx — 周期矩阵（v342）
 *
 * 为什么需要这个面板：
 *   主口径只结算 5 天，于是"这条判断到底擅长 1 小时还是 5 天"一直是盲区。
 *   后端已按 15m/1h/4h/1d/5d 分别记账（GET /api/horizons/stats），
 *   这里把它摊成一张表 —— 同一条判断在不同持有期上是赚还是亏。
 *
 * 三条口径（与后端一致，前端不再自己算）：
 *   1. 胜率分母只算已结算；no_price / not_executable 单列，不算判断错
 *   2. 样本 < 30 时显示 ⚠️：胜率区间很宽，别当结论
 *   3. 没有可结算样本的周期，「胜率」显示 —，不显示 0%
 */

import { useCallback, useEffect, useState } from 'react'
import { Loader2, RefreshCcw, Clock } from 'lucide-react'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/utils'

type HorizonRow = {
  horizon: string
  horizon_sec: number
  settled: number
  wins: number
  hit_rate: number | null
  avg_pnl_pct: number | null
  avg_outcome_pct: number | null
  no_price: number
  not_executable: number
  implausible: number
}

type StatsResp = {
  ok: boolean
  days: number
  asset: string | null
  horizons: HorizonRow[]
  status_totals?: Record<string, number>
  note?: string
}

const MIN_SAMPLE = 30
const PERIOD_OPTIONS = [7, 30, 90] as const

const HORIZON_LABEL: Record<string, string> = {
  '15m': '15 分钟',
  '1h': '1 小时',
  '4h': '4 小时',
  '1d': '1 天',
  '5d': '5 天（主口径）',
}

function fmtPct(v: number | null, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

export function HorizonMatrixPanel() {
  const [days, setDays] = useState<number>(30)
  const [data, setData] = useState<StatsResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (d: number) => {
    setLoading(true)
    setError(null)
    try {
      const resp = await apiGet<StatsResp>(`/horizons/stats?days=${d}`)
      setData(resp)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(days)
  }, [days, load])

  const rows = data?.horizons ?? []
  const anySmallSample = rows.some((r) => r.settled > 0 && r.settled < MIN_SAMPLE)
  const totalSettled = rows.reduce((n, r) => n + r.settled, 0)
  const best = rows
    .filter((r) => r.hit_rate !== null && r.settled > 0)
    .sort((a, b) => (b.hit_rate ?? 0) - (a.hit_rate ?? 0))[0]

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 font-mono text-xs text-muted-foreground">
          <Clock className="h-3.5 w-3.5" aria-hidden />
          <span>同一条判断在不同持有期上分别结算（15m / 1h / 4h / 1d / 5d）</span>
        </div>
        <div className="flex items-center gap-1">
          {PERIOD_OPTIONS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDays(d)}
              className={cn(
                'rounded border px-2 py-0.5 font-mono text-[11px] transition-colors',
                days === d
                  ? 'border-primary/60 bg-primary/10 text-primary'
                  : 'border-border text-muted-foreground hover:text-foreground',
              )}
              aria-pressed={days === d}
            >
              近 {d} 天
            </button>
          ))}
          <button
            type="button"
            onClick={() => void load(days)}
            className="ml-1 rounded border border-border p-1 text-muted-foreground hover:text-foreground"
            aria-label="刷新周期矩阵"
          >
            <RefreshCcw className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 py-6 font-mono text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          载入周期读数…
        </div>
      ) : error ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded border border-border bg-secondary/40 p-3 text-muted-foreground"
        >
          <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-hold/80" aria-hidden />
          <span className="min-w-0">
            <span className="text-xs">周期读数暂不可用</span>
            <span className="block truncate font-mono text-[11px] opacity-70">{error}</span>
          </span>
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded border border-dashed border-border p-4 font-mono text-xs text-muted-foreground">
          {data?.note ?? '暂无周期读数：等下一趟结算后回来。'}
          <div className="mt-1 opacity-70">
            说明：这里不算「判断错」，只说明多周期结算还没跑过。
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse font-mono text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="py-1.5 pr-3 text-left font-normal">持有期</th>
                <th className="py-1.5 pr-3 text-right font-normal">已结算</th>
                <th className="py-1.5 pr-3 text-right font-normal">胜</th>
                <th className="py-1.5 pr-3 text-right font-normal">胜率</th>
                <th className="py-1.5 pr-3 text-right font-normal">平均净收益</th>
                <th className="py-1.5 pr-3 text-right font-normal">平均原始</th>
                <th className="py-1.5 pr-3 text-right font-normal">无价</th>
                <th className="py-1.5 text-right font-normal">不可执行</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const small = r.settled > 0 && r.settled < MIN_SAMPLE
                const noSample = r.settled === 0
                return (
                  <tr
                    key={r.horizon}
                    className={cn(
                      'border-b border-border/60',
                      r.horizon === '5d' && 'bg-muted/20',
                    )}
                  >
                    <td className="py-1.5 pr-3">
                      {HORIZON_LABEL[r.horizon] ?? r.horizon}
                      {small && <span className="ml-1 text-amber-500">⚠️</span>}
                    </td>
                    <td className="py-1.5 pr-3 text-right">{r.settled}</td>
                    <td className="py-1.5 pr-3 text-right">{r.wins}</td>
                    <td
                      className={cn(
                        'py-1.5 pr-3 text-right',
                        r.hit_rate === null
                          ? 'text-muted-foreground'
                          : r.hit_rate >= 0.5
                            ? 'text-emerald-500'
                            : 'text-rose-500',
                      )}
                    >
                      {noSample || r.hit_rate === null ? '—' : `${(r.hit_rate * 100).toFixed(1)}%`}
                    </td>
                    <td
                      className={cn(
                        'py-1.5 pr-3 text-right',
                        r.avg_pnl_pct === null
                          ? 'text-muted-foreground'
                          : r.avg_pnl_pct > 0
                            ? 'text-emerald-500'
                            : 'text-rose-500',
                      )}
                    >
                      {fmtPct(r.avg_pnl_pct)}
                    </td>
                    <td className="py-1.5 pr-3 text-right text-muted-foreground">
                      {fmtPct(r.avg_outcome_pct)}
                    </td>
                    <td className="py-1.5 pr-3 text-right text-muted-foreground">
                      {r.no_price || 0}
                    </td>
                    <td className="py-1.5 text-right text-muted-foreground">
                      {r.not_executable || 0}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {!loading && !error && rows.length > 0 && (
        <div className="space-y-1 font-mono text-[11px] text-muted-foreground">
          <div>
            已结算合计 {totalSettled} 条
            {best && best.hit_rate !== null && (
              <>
                {' · '}当前最好：{best.horizon} 胜率 {(best.hit_rate * 100).toFixed(1)}%
                （{fmtPct(best.avg_pnl_pct)}）
              </>
            )}
          </div>
          <div>
            「无价」= 历史库没有那个时点的价；「不可执行」= T+1 品种在
            那个时点还不能平仓。两者都不计入胜率分母 —— 否则「没数据」会被
            算成「判断错」。
          </div>
          {anySmallSample && (
            <div className="text-amber-500">
              ⚠️ 有周期样本少于 {MIN_SAMPLE} 条，胜率区间很宽，先别当结论。
            </div>
          )}
        </div>
      )}
    </div>
  )
}
