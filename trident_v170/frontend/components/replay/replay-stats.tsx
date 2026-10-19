'use client'

import { useEffect, useState } from 'react'
import type { ReplayStats } from '@/lib/replay-data'
import { fetchReplayStats, fmtPct } from '@/lib/replay-data'

export function ReplayStats({ refreshKey }: { refreshKey: number }) {
  const [stats, setStats] = useState<ReplayStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchReplayStats()
      .then((s) => { if (!cancelled) { setStats(s); setError(null) } })
      .catch((e) => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [refreshKey])

  if (error) {
    return (
      <div className="rounded-md border border-short/40 bg-short/10 p-3 text-xs text-short">
        复盘统计加载失败：{error}
      </div>
    )
  }

  if (!stats) {
    return (
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-lg border border-border bg-secondary/50" />
        ))}
      </div>
    )
  }

  const { overall, by_asset } = stats
  const noData = (overall.total || 0) === 0

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-mono text-xs uppercase tracking-widest text-muted-foreground">
          模拟盘 · 全部交易统计
        </h3>
        <span className="rounded border border-hold/50 bg-hold/10 px-2 py-0.5 font-mono text-[10px] text-hold">
          PAPER TRADING
        </span>
      </div>
      <div className="mt-0.5 font-mono text-[9px] leading-tight text-muted-foreground/70">
        闸门通过后的真实模拟持仓（建仓/胜率/盈亏幅度）= 模拟盘真实战绩，与上面快速评测的口径不同
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="自动建仓总数"
          value={overall.total || 0}
          sub={`跟踪 ${overall.tracking || 0} · 已结算 ${overall.settled || 0}`}
          note="闸门通过后真实建仓的总笔数：跟踪=未结算持仓，已结算=已平仓"
        />
        <StatCard
          label="胜率"
          value={noData ? '—' : `${((overall.winrate || 0) * 100).toFixed(1)}%`}
          sub={noData ? '等待当前策略建仓' : `W${overall.wins || 0} / L${overall.losses || 0} / HOLD${overall.holds || 0} · 胜率=W/(W+L)`}
          note="已结算持仓中 WIN 的占比（W=盈利，L=亏损，HOLD=持平），不含跟踪中的持仓"
          tone={overall.winrate >= 0.5 ? 'good' : overall.winrate >= 0.3 ? 'warn' : 'bad'}
        />
        <StatCard
          label="平均 forward_pnl"
          value={noData ? '—' : fmtPct(overall.avg_pnl)}
          sub={`最佳 ${fmtPct(overall.best_trade)} · 最差 ${fmtPct(overall.worst_trade)}`}
          note="已结算持仓的平均收益率（forward_pnl 已按分级止盈加权），正=整体盈利"
          tone={(overall.avg_pnl || 0) >= 0 ? 'good' : 'bad'}
        />
        <StatCard
          label="平均 MFE / MAE"
          value={noData ? '—' : `${fmtPct(overall.avg_mfe)} / ${fmtPct(overall.avg_mae)}`}
          sub="最大有利/不利幅度"
          note="MFE=持仓过程浮盈峰值，MAE=浮亏峰值；衡量持仓过程质量，非最终盈亏"
        />
      </div>

      {by_asset.length > 0 && (
        <div className="rounded-md border border-border bg-card p-3">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              按品种分布
            </div>
            <div className="font-mono text-[9px] text-muted-foreground/70">各品种笔数 · 胜率% · 平均盈亏</div>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            {by_asset.map((row) => {
              const decided = (row.wins || 0) + (row.losses || 0)
              const wr = decided ? (row.wins || 0) / decided : 0
              return (
                <div
                  key={row.asset}
                  className="flex items-center justify-between rounded-lg border border-border bg-secondary/30 px-3 py-2"
                >
                  <div>
                    <div className="font-mono text-xs font-semibold text-foreground">{row.asset}</div>
                    <div className="font-mono text-[10px] text-muted-foreground">{row.total} 笔</div>
                  </div>
                  <div className="text-right">
                    <div
                      className={`font-mono text-sm font-semibold ${
                        wr >= 0.5 ? 'text-long' : wr >= 0.3 ? 'text-hold' : 'text-short'
                      }`}
                    >
                      {(wr * 100).toFixed(0)}%
                    </div>
                    <div className={`font-mono text-[10px] ${(row.avg_pnl || 0) >= 0 ? 'text-long/80' : 'text-short/80'}`}>
                      {fmtPct(row.avg_pnl)}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function StatCard({
  label,
  value,
  sub,
  note,
  tone,
}: {
  label: string
  value: string | number
  sub?: string
  note?: string
  tone?: 'good' | 'bad' | 'warn'
}) {
  const toneCls =
    tone === 'good'
      ? 'text-long'
      : tone === 'bad'
      ? 'text-short'
      : tone === 'warn'
      ? 'text-hold'
      : 'text-foreground'
  return (
    <div className="surface-card p-2.5">
      <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={`mt-0.5 font-mono text-lg font-bold ${toneCls}`}>{value}</div>
      {sub && <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">{sub}</div>}
      {note && <div className="mt-0.5 font-mono text-[9px] leading-tight text-muted-foreground/70">{note}</div>}
    </div>
  )
}
