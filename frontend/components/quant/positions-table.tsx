'use client'

import { useMemo } from 'react'
import Link from 'next/link'
import { Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ReplayPositions } from '@/lib/replay-data'

// v353-UI P1: 首页「当前持仓」表。
//
// 为什么加：交易台最该回答的三个问题之一就是"我手上有什么"，
// 而在此之前首页完全没有持仓视图（只有一张账户总额卡）。
//
// 口径：/replay/positions 里 settled != 1 的记录；浮盈亏直接用后端算好的
// current_pnl_usdt / current_pnl_pct，前端不自己乘价格，避免与模拟盘页对不上。
// 没有持仓时给明确空态，不留空表头。

function fmt(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function fmtTime(at: string | null | undefined) {
  if (!at) return '—'
  const m = at.match(/(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/)
  return m ? `${m[1]}-${m[2]} ${m[3]}:${m[4]}` : at
}

export function PositionsTable({
  snapshot,
  err = false,
}: {
  snapshot: ReplayPositions | null
  err?: boolean
}) {
  const data = snapshot

  const rows = useMemo(
    () => (data ? data.positions.filter((p) => Number(p.settled) !== 1).slice(0, 8) : []),
    [data],
  )

  return (
    <section className="surface-card overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <Layers className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
        <h2 className="text-[13px] font-semibold">当前持仓</h2>
        <span className="font-mono text-[11px] text-muted-foreground">
          {rows.length > 0 ? `${rows.length} 笔` : '空仓'}
        </span>
        <Link
          href="/replay"
          prefetch={false}
          className="ml-auto text-[11px] text-primary hover:underline"
        >
          去模拟盘 →
        </Link>
      </div>

      {rows.length === 0 ? (
        <div className="px-4 py-6 text-center text-[11px] leading-relaxed text-muted-foreground">
          {err
            ? '模拟盘接口暂不可用，持仓保留上一次快照。'
            : '当前没有未平仓的持仓 —— 模拟盘开仓后，这里会逐笔显示入场价、现价与浮盈亏。'}
        </div>
      ) : (
        <table className="w-full">
          <thead>
            <tr className="border-b border-border bg-secondary/30 text-left text-[11px] text-muted-foreground">
              <th className="px-4 py-1.5 font-medium">品种</th>
              <th className="px-2 py-1.5 font-medium">方向</th>
              <th className="px-2 py-1.5 text-right font-medium">入场</th>
              <th className="px-2 py-1.5 text-right font-medium">现价</th>
              <th className="px-4 py-1.5 text-right font-medium">浮盈亏</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const pnl = p.current_pnl_pct
              const usdt = p.current_pnl_usdt
              const up = (pnl ?? 0) >= 0
              return (
                <tr key={p.id} className="border-b border-border/60 last:border-0">
                  <td className="px-4 py-2 text-[12px] font-medium">{p.asset}</td>
                  <td className="px-2 py-2">
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 font-mono text-[11px]',
                        p.action === 'BUY' ? 'bg-long/10 text-long' : 'bg-short/10 text-short',
                      )}
                    >
                      {p.action === 'BUY' ? '多' : '空'}
                    </span>
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-[11px] text-muted-foreground">
                    {fmt(p.entry_price)}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-[11px]">
                    {fmt(p.current_price)}
                  </td>
                  <td
                    className={cn(
                      'px-4 py-2 text-right font-mono text-[11px]',
                      usdt == null ? 'text-muted-foreground' : up ? 'text-long' : 'text-short',
                    )}
                  >
                    {usdt == null ? '—' : `${up ? '+' : ''}${fmt(usdt)}`}
                    {pnl != null && (
                      <span className="ml-1 text-[11px] opacity-70">
                        {up ? '+' : ''}
                        {pnl.toFixed(2)}%
                      </span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {rows.length > 0 && (
        <div className="flex items-center gap-3 border-t border-border bg-secondary/20 px-4 py-1.5 font-mono text-[11px] text-muted-foreground">
          <span>保证金占用 {fmt(data?.account.used_margin_usdt)}</span>
          <span>可用 {fmt(data?.account.available_equity_usdt)}</span>
          <span className="ml-auto">入场时间 {fmtTime(rows[0].entry_time)}</span>
        </div>
      )}
    </section>
  )
}
