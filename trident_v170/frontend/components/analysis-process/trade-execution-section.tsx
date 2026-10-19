'use client'

/**
 * v203+: 分区 4 —— 交易执行。
 * 展示 quick_sim_trades + paper_trading_run:入场/出场/浮盈浮亏/PnL。
 */

import type { APQuickSim } from '@/lib/analysis-process-data'
import { fmtPctSigned, fmtTs, verdictColor } from '@/lib/analysis-process-data'
import { JsonDump, KeyValueGrid, SectionCard, SubBlock } from './sections-shared'

function MiniCurve({ mfe, mae }: { mfe: number | null; mae: number | null }) {
  // 极简 ASCII 持仓曲线:左右各画一条横线比例
  // v210.1: mfe/mae 在 DB 里就是百分数（如 0.16 = 0.16%），不要 ×100
  if (mfe === null && mae === null) return null
  const max = Math.max(Math.abs(mfe || 0), Math.abs(mae || 0), 0.001)
  const w = 32
  const pos = Math.round(((mfe || 0) / max) * w)
  const neg = Math.round(((mae || 0) / max) * w)
  return (
    <div className="flex items-center gap-2 font-mono text-[10px]">
      <span className="w-12 text-right text-long">MFE +{(mfe || 0).toFixed(1)}%</span>
      <div className="flex-1">
        <div className="relative h-2 rounded bg-border/40">
          <div
            className="absolute top-0 h-1 rounded bg-long/70"
            style={{
              left: '50%',
              width: `${(pos / w) * 50}%`,
            }}
          />
          <div
            className="absolute bottom-0 h-1 rounded bg-short/70"
            style={{
              right: '50%',
              width: `${(neg / w) * 50}%`,
            }}
          />
          <div className="absolute left-1/2 top-0 h-2 w-px bg-muted-foreground/60" />
        </div>
      </div>
      <span className="w-12 text-short">MAE -{Math.abs(mae || 0).toFixed(1)}%</span>
    </div>
  )
}

export function TradeExecutionSection({
  quick_sim,
  paper_run,
}: {
  quick_sim: APQuickSim[]
  paper_run: Record<string, unknown>[]
}) {
  if (!quick_sim?.length && !paper_run?.length) {
    return (
      <SectionCard tone="green" step="4" title="交易执行" subtitle="quick_sim + paper_run">
        <div className="font-mono text-xs text-muted-foreground">
          没有模拟持仓记录 —— 决策可能为 HOLD,或持仓异步队列未追到。
        </div>
      </SectionCard>
    )
  }
  return (
    <SectionCard
      tone="green"
      step="4"
      title="交易执行"
      subtitle={`quick_sim × ${quick_sim?.length ?? 0} · paper_run × ${paper_run?.length ?? 0}`}
      badge={`${quick_sim?.length ?? 0} 笔`}
    >
      <div className="space-y-3">
        {quick_sim?.map((qs, i) => {
          const verdict = String(qs.is_correct || '')
          const pnl = typeof qs.forward_pnl === 'number' ? qs.forward_pnl : (qs.pnl_pct as number | undefined) ?? null
          const horizon = String(qs.expected_horizon || '')
          const mfe = qs.mfe as number | null
          const mae = qs.mae as number | null
          // v210.1: pnl/mfe/mae 在 DB 里都是百分数（如 0.1625 = 0.1625%），不要 ×100
          const pnlExplain =
            verdict === 'WIN' || verdict === 'CORRECT'
              ? `模型判断对了 —— 实际涨幅超过了模型预测的阈值(预测 ${pnl != null ? `${pnl.toFixed(2)}%` : '?'},实际路径更乐观)。`
              : verdict === 'LOSS' || verdict === 'INCORRECT'
                ? `模型判断错了 —— 价格没按模型预期走,期间 MFE ${mfe != null ? `+${mfe.toFixed(2)}%` : '?'} / MAE ${mae != null ? `${mae.toFixed(2)}%` : '?'}。`
                : `还没结算 —— 模型预测 ${pnl != null ? `${pnl.toFixed(2)}%` : '?'},等 ${horizon} 到了会自动算。`
          return (
            <SubBlock
              key={qs.id || i}
              title={`quick_sim #${qs.id} · ${qs.asset || '—'} ${qs.action || '—'} · horizon ${horizon || '—'}`}
              tone={verdict === 'WIN' || verdict === 'CORRECT' ? 'note' : verdict === 'LOSS' || verdict === 'INCORRECT' ? 'warn' : 'muted'}
            >
              <p className="mb-2 text-[12px] leading-relaxed text-foreground/90">
                {pnlExplain}
              </p>
              <div className="flex flex-wrap items-baseline gap-3">
                <span className={`font-mono text-base font-bold tabular-nums ${verdictColor(verdict)}`}>
                  {fmtPctSigned(pnl)}
                </span>
                <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                  {verdict || '未结算'}
                </span>
              </div>
              <KeyValueGrid
                items={[
                  { label: 'asset', value: qs.asset || '—', mono: true },
                  { label: 'action', value: qs.action || '—', mono: true },
                  { label: 'horizon', value: horizon || '—', mono: true },
                  { label: 'entry', value: String(qs.entry_price ?? '—'), mono: true },
                  { label: 'exit', value: String(qs.exit_price ?? '—'), mono: true },
                  { label: 'pnl', value: fmtPctSigned(pnl), mono: true },
                  { label: 'settled_at', value: fmtTs(qs.settled_at as number | string | null | undefined), mono: true },
                  { label: 'created_at', value: fmtTs((qs.created_at ?? qs.opened_at) as number | string | null | undefined), mono: true },
                ]}
              />
              <div className="mt-2">
                <MiniCurve mfe={mfe} mae={mae} />
              </div>
              <details className="mt-2 rounded border border-border bg-background/60">
                <summary className="cursor-pointer px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                  ▾ 完整字段
                </summary>
                <div className="border-t border-border px-2 py-2">
                  <JsonDump data={qs} />
                </div>
              </details>
            </SubBlock>
          )
        })}

        {paper_run?.length ? (
          <SubBlock title={`paper_trading_run × ${paper_run.length}`} tone="muted">
            <JsonDump data={paper_run} />
          </SubBlock>
        ) : null}
      </div>
    </SectionCard>
  )
}
