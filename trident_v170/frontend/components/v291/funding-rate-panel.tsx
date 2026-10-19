'use client'

/**
 * v302+ P1-D 资金费率 (Funding Rate) 面板
 * 需求 14 扩展: 展示 BTC/ETH/SOL/BNB 永续合约资金费率 + 8 类衍生指标
 *
 * 数据流: /api/v291/funding-rate?symbol=XXX (60s 服务端缓存)
 *
 * 关键概念:
 *   - 资金费率 (funding rate): Binance 永续合约每 8h 结算一次
 *     rate > 0 ⇒ 多头支付空头 (贪婪, 杠杆堆积)
 *     rate < 0 ⇒ 空头支付多头 (恐慌, 空头挤压)
 *   - 年化: rate × 3 × 365
 *   - 信号分级: long_pay_extreme (rate ≥ 0.0003, 33% APR+) / neutral / short_pay_extreme
 */

import { useCallback, useEffect, useState } from 'react'
import {
  Activity, AlertTriangle, BarChart3,
  Percent, RefreshCcw, TrendingDown, TrendingUp, Zap,
} from 'lucide-react'
import { API_BASE } from '@/lib/api'
import { useToast } from '@/components/ui/toast'

type FundingHistoryItem = {
  ts: number
  rate: number
  mark_price: number | null
}

type FundingMetrics = {
  ok: boolean
  symbol: string
  ts: number
  mark_price: number | null
  n_periods: number
  current_rate: number
  avg_24h_rate: number
  avg_7d_rate: number
  avg_30d_rate: number
  stdev_30d_rate: number
  min_30d_rate: number
  max_30d_rate: number
  trend_24h_vs_7d: number
  annualized_current_pct: number
  annualized_24h_pct: number
  annualized_7d_pct: number
  annualized_30d_pct: number
  signal: string
  signal_label: string
  flip_to_negative_24h: boolean
  consecutive_sign_7d: string
  history: FundingHistoryItem[]
  cached?: boolean
  cache_age_s?: number
  fetch_error?: string
  error?: string
}

type AllSummary = {
  ok: boolean
  all_symbols: Record<string, Omit<FundingMetrics, 'history'>>
  n_extreme_long_pay: number
  n_extreme_short_pay: number
  extreme_long_pay_symbols: string[]
  extreme_short_pay_symbols: string[]
}

const SUPPORTED = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT'] as const

function fmtPct(n: number | null | undefined, digits = 2) {
  if (n === null || n === undefined) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

function fmtRate(n: number | null | undefined, digits = 6) {
  if (n === null || n === undefined) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}`
}

function fmtUsd(n: number | null | undefined) {
  if (n === null || n === undefined) return '—'
  return `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function tsFmt(ts: number) {
  return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC'
}

function signalColor(signal: string): { fg: string; bg: string; border: string } {
  if (signal === 'long_pay_extreme') return { fg: 'text-rose-500', bg: 'bg-rose-500/10', border: 'border-rose-500/40' }
  if (signal === 'long_pay') return { fg: 'text-rose-400', bg: 'bg-rose-500/5', border: 'border-rose-500/20' }
  if (signal === 'short_pay_extreme') return { fg: 'text-emerald-500', bg: 'bg-emerald-500/10', border: 'border-emerald-500/40' }
  if (signal === 'short_pay') return { fg: 'text-emerald-400', bg: 'bg-emerald-500/5', border: 'border-emerald-500/20' }
  return { fg: 'text-foreground', bg: 'bg-card', border: 'border-border' }
}

function SignalCard({ label, value, icon, color }: { label: string; value: string; icon: any; color: string }) {
  const Icon = icon
  return (
    <div className="rounded-md border border-border bg-card p-3">
      <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase text-muted-foreground">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      <div className={`mt-1 font-mono text-base font-semibold ${color}`}>{value}</div>
    </div>
  )
}

function RateRow({ item, idx }: { item: FundingHistoryItem; idx: number }) {
  const annPct = item.rate * 3 * 365 * 100
  const color = item.rate > 0 ? 'text-rose-400' : item.rate < 0 ? 'text-emerald-400' : 'text-muted-foreground'
  return (
    <div className={`grid grid-cols-12 gap-2 px-3 py-1 text-[11px] font-mono ${idx % 2 === 0 ? '' : 'bg-secondary/20'}`}>
      <div className="col-span-4 text-muted-foreground">{tsFmt(item.ts)}</div>
      <div className={`col-span-3 ${color}`}>{fmtRate(item.rate)}</div>
      <div className={`col-span-3 ${color}`}>{fmtPct(annPct, 2)}</div>
      <div className="col-span-2 text-right text-foreground/80">
        {item.mark_price ? fmtUsd(item.mark_price) : '—'}
      </div>
    </div>
  )
}

export function FundingRatePanel() {
  const { toast } = useToast()
  const [symbol, setSymbol] = useState<string>('BTCUSDT')
  const [data, setData] = useState<FundingMetrics | null>(null)
  const [summary, setSummary] = useState<AllSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (force = false) => {
    try {
      setLoading(true)
      const [r1, r2] = await Promise.all([
        fetch(`${API_BASE}/v291/funding-rate?symbol=${symbol}&refresh=${force}`, { cache: 'no-store' }),
        fetch(`${API_BASE}/v291/funding-rate?all_symbols=true&refresh=${force}`, { cache: 'no-store' }),
      ])
      if (!r1.ok) throw new Error(`funding-rate ${r1.status}`)
      const json: FundingMetrics = await r1.json()
      setData(json)
      setError(json.ok ? null : (json.error ?? 'fetch_failed'))
      if (r2.ok) {
        const sum: AllSummary = await r2.json()
        setSummary(sum)
      }
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }, [symbol])

  useEffect(() => {
    load(false)
    const t = setInterval(() => load(false), 60_000)
    return () => clearInterval(t)
  }, [load])

  const sig = data ? signalColor(data.signal) : signalColor('neutral')
  const curAnn = data?.annualized_current_pct ?? 0
  const annColor = curAnn >= 30 ? 'text-rose-500' : curAnn <= -10 ? 'text-emerald-500' : curAnn >= 10 ? 'text-rose-400' : curAnn <= -5 ? 'text-emerald-400' : 'text-foreground'

  const summaryRows = SUPPORTED.map(s => {
    const m = summary?.all_symbols?.[s]
    return { sym: s, m }
  })

  return (
    <div className="p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Percent className="h-4 w-4 text-amber-500" />
          <h2 className="font-mono text-sm font-semibold">Binance 永续合约资金费率</h2>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <span className="font-mono text-[10px] text-muted-foreground">交易对:</span>
          {SUPPORTED.map(s => (
            <button
              key={s}
              onClick={() => setSymbol(s)}
              className={`rounded-md border px-2 py-1 font-mono text-xs ${
                symbol === s
                  ? 'border-amber-500/60 bg-amber-500/10 text-amber-600'
                  : 'border-border bg-card text-muted-foreground hover:bg-secondary'
              }`}
            >
              {s}
            </button>
          ))}
          <button
            onClick={() => { load(true); toast.info('已强制刷新') }}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 font-mono text-xs hover:bg-secondary"
          >
            <RefreshCcw className="h-3 w-3" /> 强制刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 rounded-md border border-rose-500/50 bg-rose-500/10 p-3 text-xs text-rose-600">
          错误: {error} (如持续失败请检查 Binance fapi 连通性)
        </div>
      )}
      {loading && !data && (
        <div className="p-6 text-center font-mono text-xs text-muted-foreground">加载资金费率…</div>
      )}

      {summary && (
        <div className="mb-4 grid grid-cols-1 gap-2 md:grid-cols-3">
          <div className="rounded-md border border-border bg-card p-3">
            <div className="font-mono text-[10px] uppercase text-muted-foreground">全市场信号</div>
            <div className="mt-1 font-mono text-sm">
              <span className="text-rose-500">{summary.n_extreme_long_pay} 标的 long_pay</span>
              {' · '}
              <span className="text-emerald-500">{summary.n_extreme_short_pay} 标的 short_pay</span>
            </div>
          </div>
          {summary.extreme_long_pay_symbols.length > 0 && (
            <div className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3">
              <div className="font-mono text-[10px] uppercase text-rose-500">多头付费极端</div>
              <div className="mt-1 font-mono text-sm font-semibold text-rose-500">
                {summary.extreme_long_pay_symbols.join(' · ')}
              </div>
            </div>
          )}
          {summary.extreme_short_pay_symbols.length > 0 && (
            <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3">
              <div className="font-mono text-[10px] uppercase text-emerald-500">空头付费极端</div>
              <div className="mt-1 font-mono text-sm font-semibold text-emerald-500">
                {summary.extreme_short_pay_symbols.join(' · ')}
              </div>
            </div>
          )}
        </div>
      )}

      {data && data.ok && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-2 md:grid-cols-4">
            {summaryRows.map(({ sym, m }) => {
              if (!m) {
                return (
                  <div key={sym} className="rounded-md border border-border bg-card p-3">
                    <div className="font-mono text-[10px] uppercase text-muted-foreground">{sym}</div>
                    <div className="mt-1 font-mono text-sm text-muted-foreground">—</div>
                  </div>
                )
              }
              const sColor = signalColor(m.signal || 'neutral')
              return (
                <div
                  key={sym}
                  onClick={() => setSymbol(sym)}
                  className={`cursor-pointer rounded-md border ${sColor.border} ${sColor.bg} p-3 hover:opacity-80`}
                >
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[10px] uppercase text-muted-foreground">{sym}</div>
                    <div className={`font-mono text-[10px] uppercase ${sColor.fg}`}>{m.signal_label}</div>
                  </div>
                  <div className={`mt-1 font-mono text-sm font-semibold ${sColor.fg}`}>
                    {fmtPct(m.annualized_current_pct, 2)}
                  </div>
                  <div className="font-mono text-[10px] text-muted-foreground">
                    {fmtRate(m.current_rate)} / 8h
                  </div>
                </div>
              )
            })}
          </div>

          <div className="mb-4 grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-8">
            <SignalCard label="当前费率" value={fmtRate(data.current_rate)} icon={Activity} color={data.current_rate >= 0 ? 'text-rose-400' : 'text-emerald-400'} />
            <SignalCard label="年化 (当前)" value={fmtPct(data.annualized_current_pct, 2)} icon={Percent} color={annColor} />
            <SignalCard label="年化 24h" value={fmtPct(data.annualized_24h_pct, 2)} icon={data.annualized_24h_pct >= 0 ? TrendingUp : TrendingDown} color={data.annualized_24h_pct >= 0 ? 'text-rose-400' : 'text-emerald-400'} />
            <SignalCard label="年化 7d" value={fmtPct(data.annualized_7d_pct, 2)} icon={data.annualized_7d_pct >= 0 ? TrendingUp : TrendingDown} color={data.annualized_7d_pct >= 0 ? 'text-rose-400' : 'text-emerald-400'} />
            <SignalCard label="年化 30d" value={fmtPct(data.annualized_30d_pct, 2)} icon={data.annualized_30d_pct >= 0 ? TrendingUp : TrendingDown} color={data.annualized_30d_pct >= 0 ? 'text-rose-400' : 'text-emerald-400'} />
            <SignalCard label="24h vs 7d" value={`${data.trend_24h_vs_7d >= 0 ? '+' : ''}${(data.trend_24h_vs_7d * 10000).toFixed(2)} bps`} icon={data.trend_24h_vs_7d >= 0 ? TrendingUp : TrendingDown} color={data.trend_24h_vs_7d >= 0 ? 'text-rose-400' : 'text-emerald-400'} />
            <SignalCard label="30d 区间" value={`${(data.min_30d_rate * 10000).toFixed(2)} / ${(data.max_30d_rate * 10000).toFixed(2)}`} icon={BarChart3} color="text-foreground" />
            <SignalCard
              label={data.signal_label}
              value={data.flip_to_negative_24h ? '⚠ 翻负' : data.consecutive_sign_7d === 'all_long_pay' ? '7d 全正' : data.consecutive_sign_7d === 'all_short_pay' ? '7d 全负' : '震荡'}
              icon={data.signal.includes('extreme') ? AlertTriangle : Zap}
              color={sig.fg}
            />
          </div>

          <div className="rounded-md border border-border bg-card">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <h3 className="font-mono text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {symbol} · 最近 30 天 ({data.n_periods} 笔 / 8h 周期) — {data.cached ? `缓存 (${data.cache_age_s ?? 0}s 前)` : '实时拉取'}
              </h3>
              <div className="font-mono text-[10px] text-muted-foreground">
                mark price {data.mark_price ? fmtUsd(data.mark_price) : '—'} · 30d σ {fmtRate(data.stdev_30d_rate, 6)}
              </div>
            </div>
            <div className="grid grid-cols-12 gap-2 border-b border-border/40 bg-secondary/30 px-3 py-1.5 text-[10px] font-mono uppercase text-muted-foreground">
              <div className="col-span-4">时间 (UTC)</div>
              <div className="col-span-3">rate (8h)</div>
              <div className="col-span-3">年化 %</div>
              <div className="col-span-2 text-right">mark price</div>
            </div>
            <div className="max-h-[500px] overflow-y-auto">
              {data.history.slice().reverse().map((h, i) => (
                <RateRow key={h.ts} item={h} idx={i} />
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
