'use client'

import { useMemo } from 'react'
import type { SimSignal } from '@/lib/replay-data'
import {
  actionColor,
  fmtPct,
  fmtPrice,
  fmtTime,
  verdictColor,
} from '@/lib/replay-data'
import { AnalysisSteps } from './analysis-steps'
import { PeerReviewsBlock } from './peer-reviews'
import { TradeDecisionNote } from './trade-decision'
import { StructuredPanel } from '@/components/structured/structured-panel'

type Props = {
  signal: SimSignal | null
}

type ConsensusModel = {
  action?: string
  score?: number
  reasoning?: string
}

// 来源平台标签归一化：把后端 source 字符串映射成简短平台名（FRED 用专属高亮）
function sourceLabel(source: string): string {
  const s = (source || '').toLowerCase()
  if (s.includes('jin10') || s.includes('金十')) return '金十'
  if (s.includes('blockbeats') || s.includes('律动')) return '律动'
  if (s.includes('techflow') || s.includes('深潮')) return '深潮'
  if (s.includes('eastmoney') || s.includes('东财')) return '东财'
  if (s.includes('financialjuice') || s.startsWith('ws:')) return 'FinancialJuice'
  if (s.includes('tree') || s.includes('telegram')) return 'TreeNews'
  if (s.includes('fred') || s.includes('stlouisfed')) return 'FRED'
  const cleaned = (source || '').replace(/^\[?[a-z]*:?/i, '').trim()
  return cleaned.slice(0, 12) || '—'
}

function sourceBadge(source: string) {
  const isFred = sourceLabel(source) === 'FRED'
  return (
    <span
      className={`rounded border px-1.5 py-px font-mono text-[9px] font-bold uppercase tracking-wider ${
        isFred
          ? 'border-primary/50 bg-primary/10 text-primary'
          : 'border-border bg-secondary text-muted-foreground'
      }`}
    >
      {sourceLabel(source)}
    </span>
  )
}

export function SignalDetail({ signal }: Props) {
  if (!signal) {
    return (
      <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-border bg-card">
        <span className="font-mono text-xs text-muted-foreground">选择一条信号查看完整推理链</span>
      </div>
    )
  }

  const consensus = useMemo<Record<string, ConsensusModel>>(() => {
    const c = signal.extra_models_consensus
    if (!c) return {}
    if (typeof c === 'string') {
      try { return JSON.parse(c) } catch { return {} }
    }
    return c as Record<string, ConsensusModel>
  }, [signal.extra_models_consensus])

  return (
    <div className="thin-scroll flex h-full min-h-0 flex-col gap-3 overflow-y-auto rounded-md border border-border bg-card p-3">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] text-muted-foreground">#{signal.id}</span>
        <span className="font-mono text-base font-bold text-foreground">{signal.asset}</span>
        <span className={`rounded border px-2 py-0.5 font-mono text-xs font-semibold ${actionColor(signal.action)}`}>
          {signal.action}
        </span>
        <span className={`rounded border px-2 py-0.5 font-mono text-xs font-semibold ${verdictColor(signal.is_correct)}`}>
          {signal.is_correct || 'PENDING'}
        </span>
        <span className="ml-auto rounded border border-hold/50 bg-hold/10 px-2 py-0.5 font-mono text-[10px] text-hold">
          模拟盘 · {signal.settled ? '已结算' : '实时跟踪'}
        </span>
      </div>

      {/* News context */}
      <Section title="触发新闻">
        <div className="font-mono text-[11px] leading-relaxed text-foreground">
          {signal.news_text || '(无原文)'}
        </div>
        <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
          <span>来源 {sourceBadge(signal.source)}</span>
          <span>·</span>
          <span>{fmtTime(signal.news_time)}</span>
          <span>·</span>
          <span>score {(signal.score ?? 0).toFixed(2)}</span>
          {signal.event_strength && (
            <>
              <span>·</span>
              <span>强度 {signal.event_strength}</span>
            </>
          )}
          {signal.market_category && (
            <>
              <span>·</span>
              <span>{signal.market_category}</span>
            </>
          )}
        </div>
      </Section>

      {/* Price & PnL */}
      <Section title="价格 · 盈亏">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <KV label="入场价" value={fmtPrice(signal.entry_price, signal.asset)} />
          <KV
            label={signal.settled ? '出场价' : '当前价'}
            value={
              signal.settled
                ? (() => {
                    const hasPartial = signal.partial_exit_price != null && signal.partial_exit_price > 0
                    if (!hasPartial) return fmtPrice(signal.exit_price, signal.asset)
                    return (
                      <span className="inline-flex flex-col gap-0.5">
                        <span className="text-hold">
                          部分 {(signal.partial_exit_ratio ?? 0) * 100}% @ {fmtPrice(signal.partial_exit_price, signal.asset)}
                        </span>
                        <span className="text-muted-foreground">
                          剩余 {(1 - (signal.partial_exit_ratio ?? 0)) * 100}% @ {fmtPrice(signal.exit_price, signal.asset)}
                        </span>
                      </span>
                    )
                  })()
                : fmtPrice(signal.current_price, signal.asset)
            }
          />
          <KV label="期间最高" value={fmtPrice(signal.max_price, signal.asset)} />
          <KV label="期间最低" value={fmtPrice(signal.min_price, signal.asset)} />
          <KV
            label={signal.settled ? '结算 PnL' : '实时 PnL'}
            value={
              <span className={((signal.settled ? signal.forward_pnl : signal.current_pnl_pct) || 0) >= 0 ? 'text-long' : 'text-short'}>
                {fmtPct(signal.settled ? signal.forward_pnl : signal.current_pnl_pct)}
              </span>
            }
          />
          <KV label="MFE" value={fmtPct(signal.settled ? signal.mfe_pct : signal.live_mfe_pct)} tone={((signal.settled ? signal.mfe_pct : signal.live_mfe_pct) || 0) > 0 ? 'good' : 'neutral'} />
          <KV label="MAE" value={fmtPct(signal.settled ? signal.mae_pct : signal.live_mae_pct)} tone={((signal.settled ? signal.mae_pct : signal.live_mae_pct) || 0) > 0 ? 'bad' : 'neutral'} />
          <KV
            label="MFE 用时"
            value={
              signal.mfe_time_mins != null
                ? `${signal.mfe_time_mins.toFixed(0)} min`
                : '—'
            }
          />
        </div>
      </Section>

      {/* Reasoning path */}
      <Section title="大模型归因">
        <pre className="whitespace-pre-wrap break-words rounded border border-border bg-secondary p-2 font-mono text-[11px] leading-relaxed text-foreground">
          {signal.reasoning || '(无主模型推理)'}
        </pre>
        {signal.reasoning_path && (
          <div className="mt-2">
            <AnalysisSteps
              reasoningPath={signal.reasoning_path}
              mainAsset={signal.asset}
              summary={{
                action: signal.action,
                score: signal.score,
                reason: signal.reasoning ?? '',
                horizon: signal.expected_horizon ?? undefined,
                eventStrength: signal.event_strength ?? undefined,
                directCatalyst: signal.direct_catalyst != null ? signal.direct_catalyst : undefined,
                marketConfirmation: signal.market_confirmation ?? undefined,
                heatScore: signal.heat_score,
                impactScore: signal.impact_score,
                assetImpact: signal.asset_impact,
                invalidation: signal.invalidation_condition ?? undefined,
              }}
            />
          </div>
        )}
        {signal.peer_reviews && signal.peer_reviews.length > 0 && (
          <div className="mt-2">
            <PeerReviewsBlock mainAction={signal.action} peers={signal.peer_reviews} />
          </div>
        )}
        {signal.prediction_type && (
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            prediction_type: <span className="text-foreground">{signal.prediction_type}</span>
          </div>
        )}
        {signal.expected_horizon && (
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            horizon: <span className="text-foreground">{signal.expected_horizon}</span>
          </div>
        )}
        {signal.invalidation_condition && (
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            invalidation: <span className="text-foreground">{signal.invalidation_condition}</span>
          </div>
        )}
      </Section>

      {/* 交易决策说明：下单/不下单原因 */}
      <Section title="交易决策">
        <TradeDecisionNote
          action={signal.action}
          score={signal.score}
          entryPrice={signal.entry_price}
          noTradeReason={signal.no_trade_reason}
          gateReason={signal.trade_gate_reason}
        />
      </Section>

      {/* 4 sub-models consensus */}
      {Object.keys(consensus).length > 0 && (
        <Section title="四模型子决策 (extra_models_consensus)">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {Object.entries(consensus).map(([model, c]) => (
              <div key={model} className="rounded border border-border bg-secondary p-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] font-semibold text-foreground">{model}</span>
                  <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${actionColor(c.action || '')}`}>
                    {c.action || '—'}
                  </span>
                </div>
                <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                  score {c.score != null ? (c.score as number).toFixed(2) : '—'}
                </div>
                {c.reasoning && (
                  <div className="mt-1 whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed text-muted-foreground">
                    {c.reasoning}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Doubao 2nd-pass */}
      {signal.doubao_reasoning && (
        <Section title="豆包复核 (doubao)">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[11px] text-muted-foreground">doubao action:</span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${actionColor(
                signal.doubao_action || ''
              )}`}
            >
              {signal.doubao_action || '—'}
            </span>
          </div>
          <pre className="mt-2 whitespace-pre-wrap break-words rounded border border-border bg-secondary p-2 font-mono text-[11px] leading-relaxed text-foreground">
            {signal.doubao_reasoning}
          </pre>
        </Section>
      )}

      {/* Meta */}
      <Section title="元数据">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <KV label="news_id" value={String(signal.news_id)} />
          <KV label="cluster_size" value={String(signal.cluster_size ?? '—')} />
          <KV label="market_confirmation" value={signal.market_confirmation || '—'} />
          <KV label="timeframe_match" value={signal.timeframe_match || '—'} />
          <KV label="direct_catalyst" value={signal.direct_catalyst != null ? String(signal.direct_catalyst) : '—'} />
          <KV label="event_phase" value={signal.event_phase || '—'} />
          <KV label="entry_time" value={fmtTime(signal.entry_time)} />
          <KV label="settled" value={String(signal.settled)} />
        </div>
        {signal.decision_context && (
          <pre className="mt-2 whitespace-pre-wrap break-words rounded border border-border bg-secondary p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {signal.decision_context}
          </pre>
        )}
      </Section>

      {/* v172 结构化输出层（事件原子 + 时间线 + 结构化输入/校验 + 影响曲线） */}
      <StructuredPanel newsId={signal.news_id} />
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{title}</h4>
      {children}
    </div>
  )
}

function KV({
  label,
  value,
  tone,
}: {
  label: string
  value: React.ReactNode
  tone?: 'good' | 'bad' | 'neutral'
}) {
  const toneCls =
    tone === 'good' ? 'text-long' : tone === 'bad' ? 'text-short' : 'text-foreground'
  return (
    <div className="rounded border border-border bg-secondary px-2 py-1.5">
      <div className="font-mono text-[10px] text-muted-foreground">{label}</div>
      <div className={`font-mono text-xs font-semibold ${toneCls}`}>{value}</div>
    </div>
  )
}
