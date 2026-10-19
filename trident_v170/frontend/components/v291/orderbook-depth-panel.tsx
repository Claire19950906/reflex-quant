'use client'

/**
 * v302+ P1-C 挂单量 (Order Book Depth) 面板
 * 需求 14 扩展: 实时展示 BTC/ETH 挂单量 + 7 类指标
 *
 * 数据流: /api/v291/orderbook-depth?symbol=XXX (60s 服务端缓存)
 */

import { useCallback, useEffect, useState } from 'react'
import {
  Activity, AlertTriangle, ArrowDown, ArrowUp, BarChart3,
  RefreshCcw, TrendingDown, TrendingUp, Wallet,
} from 'lucide-react'
import { API_BASE } from '@/lib/api'
import { useToast } from '@/components/ui/toast'

type OrderbookMetrics = {
  ok: boolean
  symbol: string
  ts: number
  best_bid: number
  best_ask: number
  mid_price: number
  spread_bps: number
  total_bid_qty: number
  total_ask_qty: number
  total_bid_usd: number
  total_ask_usd: number
  bid_ask_ratio: number
  large_bid_qty: number
  large_ask_qty: number
  large_bid_usd: number
  large_ask_usd: number
  large_order_imbalance: number
  n_large_bids: number
  n_large_asks: number
  near_50bps_bid_usd: number
  near_50bps_ask_usd: number
  near_50bps_bid_pct: number
  near_50bps_ask_pct: number
  n_levels: number
  cached?: boolean
  cache_age_s?: number
  fetch_error?: string
  error?: string
}

function fmtUsd(n: number | null | undefined, digits = 0) {
  if (n === null || n === undefined) return '—'
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`
  return `$${n.toFixed(digits)}`
}

function fmtQty(n: number | null | undefined) {
  if (n === null || n === undefined) return '—'
  if (n >= 1000) return n.toFixed(0)
  return n.toFixed(3)
}

function tsFmt(ts: number) {
  return new Date(ts * 1000).toISOString().slice(11, 19) + ' UTC'
}

function Signal({ label, value, icon, color }: { label: string; value: string; icon: any; color: string }) {
  const Icon = icon
  return (
    <div className="rounded-md border border-border bg-card p-3">
      <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase text-muted-foreground">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      <div className={`mt-1 font-mono text-lg font-semibold ${color}`}>{value}</div>
    </div>
  )
}

function DetailRow({ label, left, right, muted = false }: { label: string; left: string; right: string; muted?: boolean }) {
  return (
    <div className={`grid grid-cols-3 gap-3 px-3 py-2 text-xs ${muted ? 'text-muted-foreground' : ''}`}>
      <div className="font-mono text-emerald-500/90">{left}</div>
      <div className="text-center font-mono text-[10px] text-muted-foreground">{label}</div>
      <div className="text-right font-mono text-rose-500/90">{right}</div>
    </div>
  )
}

export function OrderbookDepthPanel() {
  const { toast } = useToast()
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [data, setData] = useState<OrderbookMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (force = false) => {
    try {
      setLoading(true)
      const r = await fetch(`${API_BASE}/v291/orderbook-depth?symbol=${symbol}&refresh=${force}`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`orderbook-depth ${r.status}`)
      const json: OrderbookMetrics = await r.json()
      setData(json)
      setError(json.ok ? null : (json.error ?? 'fetch_failed'))
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }, [symbol])

  useEffect(() => { load(false) }, [load])

  const barColor = !data?.ok ? 'text-muted-foreground' :
    data.bid_ask_ratio >= 1.5 ? 'text-emerald-500' :
    data.bid_ask_ratio >= 1.0 ? 'text-emerald-400' :
    data.bid_ask_ratio <= 0.7 ? 'text-rose-500' : 'text-rose-400'
  const imbalanceColor = !data?.ok ? 'text-muted-foreground' :
    data.large_order_imbalance >= 0.2 ? 'text-emerald-500' :
    data.large_order_imbalance <= -0.2 ? 'text-rose-500' : 'text-muted-foreground'
  const spreadColor = !data?.ok ? 'text-muted-foreground' :
    data.spread_bps > 5 ? 'text-rose-500' :
    data.spread_bps > 1 ? 'text-amber-500' : 'text-emerald-500'
  const nearColor = !data?.ok ? 'text-muted-foreground' :
    data.near_50bps_bid_pct >= 55 ? 'text-emerald-500' :
    data.near_50bps_bid_pct <= 45 ? 'text-rose-500' : 'text-muted-foreground'

  return (
    <div className="mx-auto w-full max-w-[1600px] p-3 md:p-6">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold tracking-tight">实时挂单量</h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
            从 Binance 公共 API 拉取 BTC/ETH 永续深度, 计算 7 类挂单量指标
            (买卖力量比 / 大单不平衡 / 价差 / 近端流动性).
            数据源: 前 100 档挂单, 服务端 60s 缓存.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={symbol}
            onChange={e => setSymbol(e.target.value)}
            className="rounded-md border border-border bg-card px-2 py-1 font-mono text-xs"
          >
            <option value="BTCUSDT">BTC/USDT</option>
            <option value="ETHUSDT">ETH/USDT</option>
          </select>
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
          错误: {error} (如持续失败请检查 Binance API 连通性)
        </div>
      )}
      {loading && !data && (
        <div className="p-6 text-center font-mono text-xs text-muted-foreground">加载挂单量…</div>
      )}

      {data && data.ok && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-8">
            <Signal label="中间价" value={`$${data.mid_price.toLocaleString(undefined, { maximumFractionDigits: 2 })}`} icon={Activity} color="text-foreground" />
            <Signal label="价差 (bps)" value={data.spread_bps.toFixed(3)} icon={BarChart3} color={spreadColor} />
            <Signal label="买/卖 量比" value={data.bid_ask_ratio.toFixed(3)} icon={data.bid_ask_ratio >= 1 ? TrendingUp : TrendingDown} color={barColor} />
            <Signal label="大单不平衡" value={`${data.large_order_imbalance >= 0 ? '+' : ''}${(data.large_order_imbalance * 100).toFixed(1)}%`} icon={data.large_order_imbalance >= 0 ? TrendingUp : TrendingDown} color={imbalanceColor} />
            <Signal label="买盘 USD" value={fmtUsd(data.total_bid_usd)} icon={Wallet} color="text-emerald-500" />
            <Signal label="卖盘 USD" value={fmtUsd(data.total_ask_usd)} icon={Wallet} color="text-rose-500" />
            <Signal label="近端买盘占比" value={`${data.near_50bps_bid_pct.toFixed(1)}%`} icon={data.near_50bps_bid_pct >= 50 ? ArrowUp : ArrowDown} color={nearColor} />
            <Signal label="大单 (买/卖)" value={`${data.n_large_bids} / ${data.n_large_asks}`} icon={AlertTriangle} color="text-foreground" />
          </div>

          <div className="rounded-md border border-border bg-card">
            <div className="border-b border-border px-3 py-2">
              <h3 className="font-mono text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {symbol} · 100 档深度 — {data.cached ? `缓存 (${data.cache_age_s ?? 0}s 前)` : '实时拉取'}
              </h3>
            </div>
            <div className="divide-y divide-border/40">
              <div className="grid grid-cols-3 gap-3 px-3 py-2 text-xs">
                <div className="text-emerald-500 font-semibold">买盘 (Bids)</div>
                <div className="text-center text-muted-foreground text-[10px]">指标</div>
                <div className="text-right text-rose-500 font-semibold">卖盘 (Asks)</div>
              </div>
              <DetailRow label="挂单量" left={fmtQty(data.total_bid_qty)} right={fmtQty(data.total_ask_qty)} />
              <DetailRow label="USD 价值" left={fmtUsd(data.total_bid_usd)} right={fmtUsd(data.total_ask_usd)} />
              <DetailRow label="最佳价" left={data.best_bid.toString()} right={data.best_ask.toString()} />
              <DetailRow label="大单 (>$50K)" left={`${data.n_large_bids} 单 · ${fmtUsd(data.large_bid_usd)}`} right={`${data.n_large_asks} 单 · ${fmtUsd(data.large_ask_usd)}`} />
              <DetailRow label="近端 ±50bps USD" left={fmtUsd(data.near_50bps_bid_usd)} right={fmtUsd(data.near_50bps_ask_usd)} />
              <DetailRow label="档位数" left={`${data.n_levels}`} right={`${data.n_levels}`} muted />
            </div>
            <div className="border-t border-border px-3 py-2 text-[10px] text-muted-foreground">
              ts {tsFmt(data.ts)} · 大单阈值 $50K · 近端窗口 ±50bps
            </div>
          </div>
        </>
      )}
    </div>
  )
}

