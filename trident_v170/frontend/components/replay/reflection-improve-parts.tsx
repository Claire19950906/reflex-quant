import { Target, CheckCircle2, XCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import type {
  AssetActionStats,
  Bucket,
  Pattern,
  StrategyPattern,
  StrategyReflection,
  TriggerPattern,
} from '@/lib/types-reflection-improve'

/** 严重度 → Tailwind 边框/背景/文本配色 */
export const SEVERITY_COLORS: Record<StrategyPattern['severity'], string> = {
  low: 'border-border bg-secondary/30 text-muted-foreground',
  medium: 'border-hold/40 bg-hold/10 text-hold',
  high: 'border-short/40 bg-short/10 text-short',
}

/** 即时涨跌胜率 + LLM 建议 */
export function PatternCard({
  icon,
  title,
  pattern,
}: {
  icon: React.ReactNode
  title: string
  pattern: Pattern
}) {
  const upPct = (pattern.win_rate_after_up * 100).toFixed(0)
  const downPct = (pattern.win_rate_after_down * 100).toFixed(0)
  return (
    <div className="rounded border border-border/60 bg-background/40 p-2">
      <div className="mb-1 flex items-center gap-1.5 font-mono text-[10px] font-semibold uppercase tracking-widest text-foreground">
        {icon}
        {title}
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div className="rounded border border-long/30 bg-long/5 p-1.5">
          <div className="text-[10px] text-muted-foreground">即时涨 ≥0.3% 后胜率</div>
          <div className="font-mono text-sm font-bold text-long">{upPct}%</div>
          <div className="text-[9px] text-muted-foreground">样本 {pattern.total_after_up}</div>
        </div>
        <div className="rounded border border-short/30 bg-short/5 p-1.5">
          <div className="text-[10px] text-muted-foreground">即时跌 ≥0.3% 后胜率</div>
          <div className="font-mono text-sm font-bold text-short">{downPct}%</div>
          <div className="text-[9px] text-muted-foreground">样本 {pattern.total_after_down}</div>
        </div>
      </div>
      <p className="mt-1.5 text-[10px] leading-relaxed text-muted-foreground">
        <Target className="mr-0.5 inline h-3 w-3 text-primary" />
        {pattern.advice}
      </p>
    </div>
  )
}

/** 入场时延分箱 — 绿=胜 / 红=负 */
export function TriggerCard({ win, loss }: { win: TriggerPattern; loss: TriggerPattern }) {
  const totalWin = Object.values(win.buckets).reduce((a, b) => a + b, 0)
  const totalLoss = Object.values(loss.buckets).reduce((a, b) => a + b, 0)
  const renderBucket = (k: keyof Bucket) => {
    const wn = win.buckets[k]
    const ln = loss.buckets[k]
    const wt = totalWin ? (wn / totalWin) * 100 : 0
    const lt = totalLoss ? (ln / totalLoss) * 100 : 0
    return (
      <div key={k} className="flex flex-col gap-0.5">
        <div className="flex items-center justify-between font-mono text-[10px] text-muted-foreground">
          <span>{k}</span>
          <span>胜 {wn} / 负 {ln}</span>
        </div>
        <div className="flex h-2 gap-0.5">
          <div className="rounded-sm bg-long" style={{ width: `${wt}%` }} />
          <div className="rounded-sm bg-short" style={{ width: `${lt}%` }} />
        </div>
      </div>
    )
  }
  return (
    <div className="rounded border border-border/60 bg-background/40 p-2">
      <div className="mb-1 font-mono text-[10px] font-semibold uppercase tracking-widest text-foreground">
        入场距新闻发布时间（绿=胜 / 红=负）
      </div>
      <div className="space-y-1">
        {(['<60s', '60-300s', '300-900s', '900s+'] as const).map(renderBucket)}
      </div>
      {win.best_delay_sec !== null && loss.median_delay_sec !== null && (
        <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
          最佳入场：<b className="text-long">命中后 {win.best_delay_sec}s</b> · 输家入场中位 {loss.median_delay_sec}s
        </p>
      )}
    </div>
  )
}

/** Top 资产 × 动作统计表（按笔数降序） */
export function TopAssetAction({ rows }: { rows: AssetActionStats[] }) {
  return (
    <div className="rounded border border-border/60 bg-background/40 p-2">
      <div className="mb-1 font-mono text-[10px] font-semibold uppercase tracking-widest text-foreground">
        Top 资产 × 动作（按笔数降序）
      </div>
      <table className="w-full font-mono text-[10px]">
        <thead className="text-muted-foreground">
          <tr className="border-b border-border/40">
            <th className="py-0.5 text-left">资产·方向</th>
            <th className="py-0.5 text-right">笔数</th>
            <th className="py-0.5 text-right">胜率</th>
            <th className="py-0.5 text-right">均Pnl%</th>
            <th className="py-0.5 text-right">持仓分</th>
            <th className="py-0.5 text-right">入场分位</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, idx) => (
            <tr key={`${r.asset}_${r.action}_${idx}`} className="border-b border-border/20 last:border-b-0">
              <td className={cn('py-0.5', r.action === 'BUY' ? 'text-long' : 'text-short')}>
                {r.asset} {r.action}
              </td>
              <td className="py-0.5 text-right text-foreground">{r.count}</td>
              <td className={cn('py-0.5 text-right', r.win_rate >= 0.5 ? 'text-long' : 'text-short')}>
                {(r.win_rate * 100).toFixed(0)}%
              </td>
              <td className={cn('py-0.5 text-right', r.avg_pnl_pct >= 0 ? 'text-long' : 'text-short')}>
                {r.avg_pnl_pct >= 0 ? '+' : ''}{r.avg_pnl_pct.toFixed(2)}%
              </td>
              <td className="py-0.5 text-right text-muted-foreground">{r.avg_hold_minutes.toFixed(0)}m</td>
              <td className="py-0.5 text-right text-muted-foreground">
                {r.score_p20 != null && r.score_p80 != null
                  ? `${r.score_p20.toFixed(2)}~${r.score_p80.toFixed(2)}`
                  : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** v201 #1: LLM 策略级反思 Card */
export function StrategyReflectionCard({
  reflection,
  acting,
  onApprove,
  onReject,
}: {
  reflection: StrategyReflection
  acting: boolean
  onApprove: () => void
  onReject: () => void
}) {
  return (
    <div className="rounded border border-primary/30 bg-background/60 p-2">
      {/* Header */}
      <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
        <span className="rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-primary">
          #{reflection.id}
        </span>
        <span>
          {reflection.period_days} 天 · {reflection.sample_size} 笔样本
        </span>
        {reflection.overall_confidence != null && (
          <span className="ml-auto rounded border border-border bg-secondary/40 px-1.5 py-0.5">
            置信度 {(reflection.overall_confidence * 100).toFixed(0)}%
          </span>
        )}
      </div>

      {/* LLM Summary */}
      <p className="mb-1.5 text-[11px] leading-relaxed text-foreground">
        {reflection.llm_summary || '(无 LLM 总结)'}
      </p>

      {/* Patterns */}
      {reflection.strategy_patterns.length > 0 && (
        <div className="mb-1.5 space-y-1">
          {reflection.strategy_patterns.map((p, idx) => (
            <div
              key={idx}
              className={cn(
                'rounded border px-1.5 py-1 font-mono text-[10px]',
                SEVERITY_COLORS[p.severity] || SEVERITY_COLORS.medium,
              )}
            >
              <div className="flex items-center gap-1.5">
                <span className="rounded bg-background/40 px-1 py-0.5 text-[9px] uppercase">
                  {p.severity}
                </span>
                <span>{p.pattern}</span>
              </div>
              {p.supporting_data && (
                <div className="mt-0.5 text-[9px] opacity-80">
                  📊 {p.supporting_data}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Evidence */}
      {reflection.evidence_summary && (
        <div className="mb-1.5 rounded border border-border/40 bg-secondary/20 p-1.5 font-mono text-[10px] text-muted-foreground">
          <span className="font-semibold text-foreground">证据：</span>
          {reflection.evidence_summary}
        </div>
      )}

      {/* Recommendation */}
      {reflection.recommendations.length > 0 && (
        <div className="mb-1.5 space-y-0.5">
          <div className="font-mono text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            推荐参数调整（{reflection.recommendations.length}）
          </div>
          {reflection.recommendations.map((r, idx) => (
            <div
              key={idx}
              className="flex items-center gap-1.5 rounded border border-border/40 bg-secondary/20 px-1.5 py-0.5 font-mono text-[10px]"
            >
              <code className="text-primary">{r.key}</code>
              <span className={cn(
                'rounded px-1 py-0.5 text-[9px]',
                r.direction === 'up' || r.direction === 'on'
                  ? 'bg-long/20 text-long'
                  : 'bg-short/20 text-short',
              )}>
                {r.direction === 'up' || r.direction === 'on' ? '↑' : '↓'}{r.direction}
              </span>
              <span className="text-muted-foreground">magnitude {(r.magnitude * 100).toFixed(0)}%</span>
              <span className="ml-auto text-muted-foreground">
                conf {(r.confidence * 100).toFixed(0)}%
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-1.5 pt-1">
        <button
          type="button"
          onClick={onApprove}
          disabled={acting}
          className="flex flex-1 items-center justify-center gap-1 rounded border border-long/40 bg-long/10 px-2 py-1 font-mono text-[10px] text-long hover:bg-long/20 disabled:opacity-50"
        >
          <CheckCircle2 className="h-3 w-3" />
          {acting ? '处理中…' : '审批'}
        </button>
        <button
          type="button"
          onClick={onReject}
          disabled={acting}
          className="flex items-center justify-center gap-1 rounded border border-short/40 bg-short/10 px-2 py-1 font-mono text-[10px] text-short hover:bg-short/20 disabled:opacity-50"
        >
          <XCircle className="h-3 w-3" />
          驳回
        </button>
      </div>
    </div>
  )
}