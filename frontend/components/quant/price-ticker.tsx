'use client'

// v282+: HONESTY 边界 — 绝不允许把 null 折叠成 0.00% / 假价格。
// 详见 lib/honesty.ts NEVER_FABRICATE_INVARIANT。
import {
  NEVER_FABRICATE_INVARIANT,
  UNAVAILABLE_PLACEHOLDER,
  isRealNumber,
  formatPriceOrDash,
} from '@/lib/honesty'
import { cn } from '@/lib/utils'
import type { MarketCategory } from '@/lib/quant-data'
import type { PriceMap } from '@/hooks/use-binance-prices'

// 引用即承认红线;不删除
void NEVER_FABRICATE_INVARIANT
void UNAVAILABLE_PLACEHOLDER
void isRealNumber
void formatPriceOrDash

type TickerSymbol = {
  asset: string
  label: string
  /** Which markets this symbol should appear in */
  showIn: MarketCategory[]
}

const SYMBOLS: TickerSymbol[] = [
  { asset: 'BTC',  label: 'BTC',  showIn: ['ALL', 'CRYPTO', 'MACRO'] },
  { asset: 'ETH',  label: 'ETH',  showIn: ['ALL', 'CRYPTO', 'MACRO'] },
  { asset: 'SOL',  label: 'SOL',  showIn: ['ALL', 'CRYPTO']           },
  { asset: 'XAU',  label: 'XAU',  showIn: ['ALL', 'GOLD', 'MACRO']    },
  { asset: 'WTI',  label: 'WTI',  showIn: ['ALL', 'OIL', 'MACRO']     },
]

type Props = {
  prices: PriceMap
  connected: boolean
  activeMarket: MarketCategory
  /**
   * v310 P0 fix: 是否已完成过一次成功拉取。
   * - false → 首次加载中 → 显示「加载中」
   * - true  → 已加载过但该品种后端没给 → 显示「—」(诚实: 真的没数据, 不是还在加载)
   *
   * 旧实现只有 connected 一个**全局**位, 于是「这个品种后端根本没提供」
   * 和「还在加载」两种情况都渲染成「加载中」—— XAU/WTI 只走 SSE price_update,
   * /market/prices 里根本没有这两个品种, 结果它们永远停在「加载中」。
   */
  loaded?: boolean
  /** 报价距上次成功拉取的秒数；> STALE_SEC 判为陈旧，提示用户而非装作实时 */
  ageSec?: number | null
}

const STALE_SEC = 60

/**
 * Format an asset price for display — 2‑decimal institutional standard.
 *
 * - sub‑$1 tokens: 4 decimal places (e.g. $0.5218)
 * - everything else: 2 decimal places (e.g. $64,180.50 / $4,113.16 / $148.22)
 */
export function formatPrice(p: number, _asset?: string): string {
  if (p < 1) return p.toFixed(4)
  return p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function PriceTicker({ prices, connected, activeMarket, loaded = true, ageSec = null }: Props) {
  const visible = SYMBOLS.filter((s) => s.showIn.includes(activeMarket))
  // v310: 未完成首次拉取 → 「加载中」; 已完成但该品种无数据 → 「—」
  const pending = !loaded
  const stale = loaded && connected && ageSec != null && ageSec > STALE_SEC
  const staleLabel = ageSec != null && ageSec > 3600 ? `${Math.floor(ageSec / 3600)}h` : ageSec != null ? `${Math.floor(ageSec / 60)}m` : ''

  return (
    <div className="inline-flex items-center gap-0.5 rounded-lg border border-border bg-secondary/50 p-0.5">
      {visible.map((s) => {
        const info = prices[s.asset]
        // v252.2: change24h nullable — 后端拿不到 prev_close 时返回 null,
        // 显示 "—" 而非假 "+0.00%"。
        const chg = info?.change24h ?? null
        const up = chg == null ? null : chg >= 0

        return (
          <div
            key={s.asset}
            className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5"
          >
            <span className="font-mono text-[11px] font-semibold text-foreground">
              {s.label}
            </span>
            {info ? (
              <>
                <span className="font-mono text-[11px] tabular-nums text-foreground/90">
                  ${formatPrice(info.price, s.asset)}
                </span>
                <span
                  className={cn(
                    'font-mono text-[11px] tabular-nums',
                    up == null
                      ? 'text-muted-foreground'
                      : up
                      ? 'text-long'
                      : 'text-short',
                  )}
                  title={chg == null ? 'prev_close 不可用 — 不显示涨跌' : undefined}
                >
                  {chg == null ? '—' : `${up ? '+' : ''}${chg.toFixed(2)}%`}
                </span>
              </>
            ) : (
              <span
                className="font-mono text-[11px] text-muted-foreground"
                title={
                  pending
                    ? '行情加载中…'
                    : connected
                      ? '该品种暂无行情数据(后端未提供)'
                      : '行情源断开'
                }
              >
                {pending ? '加载中' : connected ? '—' : '断开'}
              </span>
            )}
          </div>
        )
      })}
      <div
        className={cn(
          'mx-1 h-2 w-2 rounded-full',
          stale ? 'bg-amber-500' : connected ? 'bg-long' : 'bg-muted-foreground',
        )}
        title={stale ? `报价已陈旧 ${staleLabel} 未更新（行情源可能停更）` : connected ? '多源行情已连接' : '多源行情断开'}
      />
      {stale && staleLabel && (
        <span className="font-mono text-[11px] text-amber-500/90" title="报价陈旧：距上次更新已超过 60 秒">
          陈旧 {staleLabel}
        </span>
      )}
    </div>
  )
}
