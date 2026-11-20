'use client'

import { useMemo } from 'react'
import { RefreshCw } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { cn } from '@/lib/utils'

/* ════════════════════════════════════════════════════════════════════════
 * 多空持仓比（Long/Short Ratio）· 真实交易所数据
 *
 * 数据来源:
 *   /api/crypto/lsr/latest  ←  bridge_server
 *   上游: Binance 合约 globalLongShortAccountRatio（全市场账户多空比）
 *         Bybit 合约 account-ratio（账户买卖比）
 *   写库: lsr_feed.py 每小时跑一次（com.trident.lsr-feed）
 *
 * 为什么不用 Coinglass:
 *   coinglass_lsr 需要付费 key，试用 key 从未成功，coinglass_lsr_samples
 *   自建表起就是 0 行 —— 面板长期只能显示缺失。这里换成免费且可达的真实源。
 *
 * 显示原则:
 *   只显示上游给出的比例；拿不到就显示"缺失"，不推算、不填充。
 * ════════════════════════════════════════════════════════════════════════ */

type LsrItem = {
  symbol: string
  source: string
  ts: number
  long_ratio: number | null
  short_ratio: number | null
  ls_ratio: number | null
}

type LsrResp = { ok: boolean; items: LsrItem[] }

const SOURCE_LABEL: Record<string, string> = {
  binance: 'Binance 账户比',
  bybit: 'Bybit 买卖比',
}

const SYMBOLS = ['BTC', 'ETH', 'SOL']

export function LsrPanel() {
  const poll = usePoll<LsrResp>('/crypto/lsr/latest?hours=6', {
    intervalMs: 5 * 60_000,
    transform: (raw) => raw as LsrResp,
  })

  const items = Array.isArray(poll.data?.items) ? (poll.data as LsrResp).items : []

  // 按 品种 → 源 组织,缺失就是 null(渲染成 "缺失")
  const table = useMemo(() => {
    const out: Record<string, Record<string, LsrItem | null>> = {}
    for (const s of SYMBOLS) {
      out[s] = { binance: null, bybit: null }
    }
    for (const it of items) {
      if (!out[it.symbol]) out[it.symbol] = {}
      out[it.symbol][it.source] = it
    }
    return out
  }, [items])

  const updatedAt = poll.lastFetchedAt ?? null
  const anyData = items.length > 0

  return (
    <div className="rounded border border-border bg-background/40 p-2.5">
      <div className="mb-2 flex items-center justify-between font-mono text-[11px] text-muted-foreground">
        <span>
          上游: Binance 合约 · Bybit 合约 · 每小时刷新
          {updatedAt ? ` · ${new Date(updatedAt).toLocaleTimeString('zh-CN', { hour12: false })}` : ''}
        </span>
        <button
          type="button"
          onClick={() => void poll.refresh()}
          className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 hover:bg-muted"
        >
          <RefreshCw className="h-3 w-3" />
          刷新
        </button>
      </div>

      {!anyData ? (
        <div className="rounded border border-dashed border-border p-3 font-mono text-[11px] text-muted-foreground">
          数据缺失（上游未返回）—— 不填占位数值
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {SYMBOLS.map((sym) => {
            const rows = table[sym] || {}
            return (
              <div key={sym} className="rounded border border-border bg-background/40 p-2.5">
                <div className="font-mono text-[11px] font-semibold uppercase tracking-wider text-foreground">
                  {sym}
                </div>
                <div className="mt-1 flex flex-col gap-1.5">
                  {Object.keys(rows).map((src) => {
                    const it = rows[src]
                    const ok = it && typeof it.long_ratio === 'number' && typeof it.short_ratio === 'number'
                    const longPct = ok ? (it!.long_ratio as number) * 100 : null
                    const shortPct = ok ? (it!.short_ratio as number) * 100 : null
                    const ls = ok ? it!.ls_ratio : null
                    return (
                      <div key={src} className="font-mono text-[11px] tabular-nums">
                        <div className="text-[11px] text-muted-foreground">
                          {SOURCE_LABEL[src] || src}
                        </div>
                        {ok ? (
                          <>
                            <div className="flex items-baseline gap-1.5">
                              <span className="text-emerald-600 dark:text-emerald-300">
                                多 {longPct!.toFixed(2)}%
                              </span>
                              <span className="text-rose-600 dark:text-rose-300">
                                空 {shortPct!.toFixed(2)}%
                              </span>
                            </div>
                            <div className="mt-0.5 flex items-center gap-1.5">
                              <span className="text-muted-foreground">比</span>
                              <span
                                className={cn(
                                  'font-bold',
                                  ls != null && ls >= 2
                                    ? 'text-emerald-600 dark:text-emerald-300'
                                    : 'text-foreground',
                                )}
                              >
                                {ls != null ? ls.toFixed(2) : '—'}
                              </span>
                            </div>
                          </>
                        ) : (
                          <div className="text-muted-foreground">缺失</div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
