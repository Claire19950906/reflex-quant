'use client'

/**
 * v203+: 左栏新闻列表 —— 点选切右栏详情。
 * v210+: 交易信息行（入场价 / 当前价 / MFE / 未实现盈亏 / 状态）与主页 SignalLedger 共用组件，
 *         视觉上保持"一模一样"。宽度受限 340px → 紧凑 2 行布局（信号 + 入场→当前·MFE·PnL·状态）。
 */

import type { APListItem } from '@/lib/analysis-process-data'
import { fmtTs } from '@/lib/analysis-process-data'
import { SignalBadge } from '@/components/quant/signal-badge'
import { formatPrice } from '@/components/quant/price-ticker'
import type { Signal } from '@/lib/quant-data'
import { computeMFE, computePnL } from '@/lib/signal-utils'
import type { PriceMap } from '@/hooks/use-binance-prices'

// 复用主页 SignalLedger 的判定逻辑：商品用 USD、币用 USDT
function isCommodity(asset: string | null, cat: string | null): boolean {
  if (cat === 'GOLD' || cat === 'OIL') return true
  if (!asset) return false
  const a = asset.toUpperCase()
  return a === 'XAU' || a === 'GOLD' || a === 'WTI'
}

function resolvePair(it: APListItem): { pair: string; pairKey: string; asset: string | null } {
  const asset = it.latest_asset && it.latest_asset !== 'NONE' ? it.latest_asset : null
  const commodity = isCommodity(asset, it.latest_market_category)
  const suffix = commodity ? 'USD' : 'USDT'
  if (asset) return { pair: `${asset}/${suffix}`, pairKey: `${asset}USDT`, asset }
  return { pair: '—', pairKey: '', asset: null }
}

function TradeRow({
  it,
  livePrices,
}: {
  it: APListItem
  livePrices: PriceMap
}) {
  // 信号归一化（BUY/SELL/HOLD/FAILED）
  const rawAction = (it.latest_action || '').toUpperCase()
  const signal: Signal =
    rawAction === 'BUY' || rawAction === 'SELL' || rawAction === 'HOLD'
      ? (rawAction as Signal)
      : rawAction === 'FAILED'
        ? 'FAILED'
        : 'HOLD'

  // 是否实际下单（有 entry_price 即视为"已建仓"）
  const entry = typeof it.latest_entry_price === 'number' ? it.latest_entry_price : null
  const traded = entry !== null && entry > 0

  // 交易对 / 当前价（来自 useBinancePrices）
  const { pair, pairKey, asset } = resolvePair(it)
  const currentInfo = pairKey ? livePrices[pairKey] : undefined
  const current = currentInfo?.price ?? 0
  const hasCurrent = Boolean(currentInfo?.price)

  // MFE / PnL —— 完全复用主页 signal-utils
  const action = signal === 'FAILED' ? 'HOLD' : signal // FAILED 不参与 MFE/PNL 计算
  const mfeVal =
    action === 'BUY' || action === 'SELL'
      ? computeMFE(action, entry, it.latest_max_price, it.latest_min_price)
      : null
  const pnl =
    entry && hasCurrent && (action === 'BUY' || action === 'SELL')
      ? computePnL(action, entry, current)
      : null

  const note = it.latest_trade_gate_reason || it.latest_no_trade_reason || undefined
  const verdictText = String(it.latest_verdict || '')

  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[10px]">
      {/* 行 1：交易对 · SignalBadge —— 与主页 SignalLedger 完全对齐 */}
      <div className="flex w-full items-center gap-1.5">
        <span className="font-mono font-medium text-foreground">{pair}</span>
        <SignalBadge
          signal={signal}
          traded={signal === 'HOLD' || signal === 'FAILED' ? undefined : traded}
          note={note}
        />
      </div>
      {/* 行 2：入场价 → 当前价 · MFE · PnL · 状态 —— 字段与主页 SignalLedger 一一对齐 */}
      <div className="flex w-full flex-wrap items-baseline gap-x-2 gap-y-0.5 tabular-nums">
        <span className="text-muted-foreground">
          入场{' '}
          <span className="text-foreground/90">
            {entry ? `$${formatPrice(entry, asset ?? undefined)}` : '—'}
          </span>
        </span>
        <span className="text-muted-foreground/60">→</span>
        <span className="text-muted-foreground">
          现价{' '}
          <span className="text-foreground/90">
            {hasCurrent ? `$${formatPrice(current, asset ?? undefined)}` : '—'}
          </span>
        </span>
        <span className="text-long/80">
          MFE{' '}
          {mfeVal !== null
            // v210.1: mfeVal 已是百分数（如 0.16 = 0.16%），不要 ×100
            ? `${mfeVal >= 0 ? '+' : ''}${mfeVal.toFixed(2)}%`
            : '—'}
        </span>
        <span className={`font-semibold ${pnl?.positive ? 'text-long' : 'text-short'}`}>
          {pnl ? `${pnl.positive ? '+' : ''}${pnl.pct}%` : '—'}
        </span>
        <VerdictLabelMini verdict={verdictText} />
      </div>
    </div>
  )
}

function VerdictLabelMini({ verdict }: { verdict: string }) {
  const v = verdict?.toUpperCase()
  if (!v) return <span className="text-muted-foreground/40">—</span>
  if (v === 'WIN') return <span className="font-medium text-long">✅ WIN</span>
  if (v === 'LOSS') return <span className="font-medium text-short">❌ LOSS</span>
  return <span className="text-muted-foreground/50">➖ 无实质影响</span>
}

export function NewsList({
  items,
  selected,
  loading,
  onlyWithDecisions,
  onToggleOnlyWithDecisions,
  onSelect,
  onRefresh,
  livePrices = {} as PriceMap,
}: {
  items: APListItem[]
  selected: number | null
  loading: boolean
  onlyWithDecisions: boolean
  onToggleOnlyWithDecisions: (v: boolean) => void
  onSelect: (newsId: number) => void
  onRefresh: () => void
  /** 来自 useBinancePrices 的实时报价（按 "BTCUSDT" / "XAUUSDT" 等键索引） */
  livePrices?: PriceMap
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex items-baseline gap-2">
          <h2 className="font-mono text-[11px] font-bold uppercase tracking-wider text-primary">
            新闻列表
          </h2>
          <span className="font-mono text-[10px] text-muted-foreground">
            × {items.length}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex cursor-pointer items-center gap-1.5 font-mono text-[10px] text-muted-foreground">
            <input
              type="checkbox"
              checked={onlyWithDecisions}
              onChange={(e) => onToggleOnlyWithDecisions(e.target.checked)}
              className="h-3 w-3 accent-primary"
            />
            only_with_decisions
          </label>
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="rounded border border-border bg-secondary px-2 py-0.5 font-mono text-[10px] text-foreground hover:bg-secondary/80 disabled:opacity-50"
          >
            {loading ? '...' : '⟳'}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading && items.length === 0 ? (
          <div className="px-3 py-6 text-center font-mono text-[11px] text-muted-foreground">
            加载中...
          </div>
        ) : items.length === 0 ? (
          <div className="px-3 py-6 text-center font-mono text-[11px] text-muted-foreground">
            没有数据
          </div>
        ) : (
          <ul>
            {items.map((it) => {
              const isSelected = it.news_id === selected
              return (
                <li key={it.news_id}>
                  <button
                    type="button"
                    onClick={() => onSelect(it.news_id)}
                    className={`block w-full border-b border-border/60 px-3 py-2 text-left transition-colors ${
                      isSelected
                        ? 'border-l-2 border-l-primary bg-primary/5'
                        : 'border-l-2 border-l-transparent hover:bg-secondary/40'
                    }`}
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-mono text-[10px] text-muted-foreground">
                        #{it.news_id} · {fmtTs(it.ts)}
                      </span>
                      <span className="font-mono text-[9px] text-muted-foreground/70">
                        {it.source}
                      </span>
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-[12px] leading-snug text-foreground/90">
                      {it.content_preview || '—'}
                    </p>
                    <TradeRow it={it} livePrices={livePrices} />
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}