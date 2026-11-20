'use client'

import { usePoll } from '@/lib/hooks/use-poll'
import { cn } from '@/lib/utils'

/**
 * 段 · 指标记分牌（v497）
 *
 * 为什么单独做：原来的台账统计把看涨/看跌背离混在一起，看到的是"命中 47.8%、
 * 平均 +0.3%"，结论会是"勉强能用"。按方向拆开后才发现 —— 看跌 +1.9%、看涨 −1.06%，
 * 实际是**一半赚钱一半送钱**。混着看等于把无效信号的平均值算进了有效信号的收益里。
 *
 * 数据源：GET /api/divergence/scoreboard（后端按 方向/形态/等级 分桶，带样本量判定）
 * 判定口径写在界面上了，避免"3 笔就敢说 100% 命中"这种自欺。
 */

type DirRow = {
  direction: 'bullish' | 'bearish' | string
  n: number
  wins: number
  hit_rate: number | null
  avg_pnl: number | null
  avg_mfe: number | null
  avg_mae: number | null
  verdict: string
}

type CtxRow = {
  context: string
  label: string
  n: number
  wins: number
  hit_rate: number | null
  avg_pnl: number | null
  verdict: string
}

type ScoreResp = {
  ok: boolean
  total: number
  settled: number
  open: number
  by_direction: DirRow[]
  by_context?: CtxRow[]
  conclusion: string
  disclaimer: string
}

const VERDICT_CLASS: Record<string, string> = {
  可用: 'text-long',
  '降权/停用': 'text-short',
  避开: 'text-short font-medium',
  观察中: 'text-amber-400',
  样本不足: 'text-muted-foreground',
}

function signed(v: number | null | undefined) {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

export function DivergenceScoreboard({ className }: { className?: string }) {
  const poll = usePoll<ScoreResp>('/divergence/scoreboard', { intervalMs: 300_000 })
  const data = poll.data

  // 接口没接上就不显示：否则"接口没实现"会被误读成"没有样本"
  if (!data || !data.ok || !data.by_direction?.length) return null

  return (
    <div className={cn('mb-3 rounded-sm border border-border p-2.5 font-mono text-[11px]', className)}>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-muted-foreground">
          指标记分牌 · 结算 {data.settled} 笔 / 待结算 {data.open} 笔
        </span>
        <span className="text-muted-foreground/70">10 根 horizon · 已扣 8bp</span>
      </div>

      <table className="w-full table-fixed border-collapse">
        <colgroup>
          <col style={{ width: '18%' }} />
          <col style={{ width: '12%' }} />
          <col style={{ width: '16%' }} />
          <col style={{ width: '18%' }} />
          <col style={{ width: '18%' }} />
          <col style={{ width: '18%' }} />
        </colgroup>
        <thead>
          <tr className="border-b border-border text-muted-foreground">
            <th className="py-1 text-left font-normal">方向</th>
            <th className="py-1 text-right font-normal">样本</th>
            <th className="py-1 text-right font-normal">命中率</th>
            <th className="py-1 text-right font-normal">净收益</th>
            <th className="py-1 text-right font-normal">有利/不利</th>
            <th className="py-1 text-right font-normal">判定</th>
          </tr>
        </thead>
        <tbody>
          {data.by_direction.map((d) => (
            <tr key={d.direction} className="border-b border-border/50">
              <td className="py-1 text-foreground">{d.direction === 'bullish' ? '看涨背离' : '看跌背离'}</td>
              <td className="py-1 text-right text-muted-foreground">{d.n}</td>
              <td className="py-1 text-right text-foreground">{d.hit_rate == null ? '—' : `${d.hit_rate}%`}</td>
              <td className={cn('py-1 text-right', (d.avg_pnl ?? 0) >= 0 ? 'text-long' : 'text-short')}>
                {signed(d.avg_pnl)}
              </td>
              <td className="py-1 text-right text-muted-foreground">
                {signed(d.avg_mfe)} / {signed(d.avg_mae == null ? null : -d.avg_mae)}
              </td>
              <td className={cn('py-1 text-right', VERDICT_CLASS[d.verdict] || 'text-muted-foreground')}>{d.verdict}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {(data.by_context || []).filter((c) => c.n >= 10).length > 0 ? (
        <table className="mt-2 w-full table-fixed border-collapse">
          <colgroup>
            <col style={{ width: '40%' }} />
            <col style={{ width: '12%' }} />
            <col style={{ width: '16%' }} />
            <col style={{ width: '16%' }} />
            <col style={{ width: '16%' }} />
          </colgroup>
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="py-1 text-left font-normal">组合（形态·趋势）</th>
              <th className="py-1 text-right font-normal">样本</th>
              <th className="py-1 text-right font-normal">命中率</th>
              <th className="py-1 text-right font-normal">净收益</th>
              <th className="py-1 text-right font-normal">判定</th>
            </tr>
          </thead>
          <tbody>
            {(data.by_context || [])
              .filter((c) => c.n >= 10)
              .map((c) => (
                <tr key={c.context} className="border-b border-border/50">
                  <td className="py-1 text-foreground">{c.label}</td>
                  <td className="py-1 text-right text-muted-foreground">{c.n}</td>
                  <td className="py-1 text-right text-foreground">
                    {c.hit_rate == null ? '—' : `${c.hit_rate}%`}
                  </td>
                  <td className={cn('py-1 text-right', (c.avg_pnl ?? 0) >= 0 ? 'text-long' : 'text-short')}>
                    {signed(c.avg_pnl)}
                  </td>
                  <td className={cn('py-1 text-right', VERDICT_CLASS[c.verdict] || 'text-muted-foreground')}>
                    {c.verdict}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      ) : null}

      <p className="mt-2 leading-relaxed text-foreground/80">{data.conclusion}</p>
      <p className="mt-1 leading-relaxed text-muted-foreground/70">{data.disclaimer}</p>
    </div>
  )
}
