'use client'

import { useState, useCallback, useEffect, useMemo } from 'react'
import { ChevronDown, ChevronRight, Loader2, CheckCircle2, XCircle, CircleDashed, Network, History, AlertTriangle, Sparkles, ArrowRightLeft, Lightbulb, Brain } from 'lucide-react'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { CrossAssetItem, Review, SegmentPosition, DecisionTextResp } from '@/lib/types-reasoning-review'
import { STEPS, DIM_LABELS } from '@/lib/types-reasoning-review'
import { CopyMarkdownButton } from './copy-markdown-button'

// v170 P0+ 巨型组件拆分：从 reasoning-review-panel.tsx 拆出
//   - 6 个小组件 + DecisionCard（204 行），纯展示 + 局部 state
//   - 主面板只做编排 + 数据拉取

// ─── 小组件 ──────────────────────────────────────────────────────────

/** 完备度徽章（0-100，纯展示，不分级别） */
export function ScoreBadge({ score }: { score: number }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-0.5 font-mono text-[12px] font-bold text-foreground"
      title={`完备度 ${score.toFixed(1)}%`}
    >
      {score.toFixed(0)}%
    </span>
  )
}

/** 行动 pill（BUY/SELL/HOLD） */
export function ActionPill({ action }: { action: string }) {
  const a = action.toUpperCase()
  const tone =
    a === 'BUY' || a === 'LONG'
      ? 'border-foreground/30 bg-foreground/10 text-foreground'
      : a === 'SELL' || a === 'SHORT'
        ? 'border-foreground/30 bg-foreground/10 text-foreground'
        : 'border-border bg-card text-muted-foreground'
  return (
    <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold ${tone}`}>
      {a || '—'}
    </span>
  )
}

/** 盈亏 pill */
export function PnlPill({ pnl, isCorrect }: { pnl: number | null; isCorrect: 0 | 1 | null }) {
  if (pnl === null && isCorrect === null) {
    return <span className="font-mono text-[10px] text-muted-foreground">未结算</span>
  }
  if (isCorrect === 1) {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[10px] text-foreground">
        <CheckCircle2 className="h-3 w-3" />
        {pnl !== null ? `+${(pnl * 100).toFixed(2)}%` : '正确'}
      </span>
    )
  }
  if (isCorrect === 0) {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
        <XCircle className="h-3 w-3" />
        {pnl !== null ? `${(pnl * 100).toFixed(2)}%` : '错误'}
      </span>
    )
  }
  return (
    <span className="font-mono text-[10px] text-muted-foreground">
      {pnl !== null ? `${(pnl * 100).toFixed(2)}%` : '—'}
    </span>
  )
}

/** 8 段 dot：8 个圆点显示 present / missing，点击按需加载原文 */
export function StepDots({
  steps,
  decisionId,
  shownSegment,
  onDotClick,
}: {
  steps: { present: string[]; missing: string[]; pct: number }
  decisionId: number
  shownSegment: string | null
  onDotClick: (segment: string, idx: number) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {STEPS.map((seg, idx) => {
        const present = steps.present.includes(seg)
        const active = shownSegment === seg
        return (
          <button
            key={seg}
            type="button"
            onClick={() => onDotClick(seg, idx)}
            title={`${seg}（${present ? '已出现' : '缺失'}）· #${decisionId}`}
            className={cn(
              'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors',
              active
                ? 'border-foreground/40 bg-foreground/10 text-foreground'
                : present
                  ? 'border-border bg-background text-foreground'
                  : 'border-border bg-background/40 text-muted-foreground',
            )}
          >
            {present ? (
              <CheckCircle2 className="h-3 w-3" />
            ) : (
              <CircleDashed className="h-3 w-3" />
            )}
            <span>
              {idx + 1}.{seg}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** 单维明细行 */
export function DimRow({
  dimKey,
  review,
}: {
  dimKey: keyof typeof DIM_LABELS
  review: Review
}) {
  const label = DIM_LABELS[dimKey]
  let present = false
  let extras: string[] = []
  let detail = ''
  switch (dimKey) {
    case 'steps':
      present = review.steps.missing.length === 0
      extras = review.steps.missing
      detail = `${review.steps.present.length}/${STEPS.length}（${review.steps.pct.toFixed(0)}%）`
      break
    case 'counter_consensus':
      present = review.counter_consensus.present
      extras = review.counter_consensus.keywords_hit
      detail = review.counter_consensus.present ? '已出现对立思考' : '未出现对立/反方观点'
      break
    case 'causal_mechanism':
      present = review.causal_mechanism.present
      extras = review.causal_mechanism.keywords_hit
      detail = review.causal_mechanism.present ? '已说明传导机制' : '未出现因果/传导关键词'
      break
    case 'cross_asset_direction': {
      const cad = review.cross_asset_direction
      present = cad.present
      extras = []
      detail = cad.detail
      break
    }
    case 'assumption':
      present = review.assumption.present
      extras = review.assumption.keywords_hit
      detail = review.assumption.present ? '已明说前提条件' : '未出现假设/前提关键词'
      break
    case 'historical_pattern':
      present = review.historical_pattern.present
      extras = review.historical_pattern.keywords_hit
      detail = review.historical_pattern.present ? '已引用历史样本' : '未引用历史样本'
      break
  }
  return (
    <div className="rounded border border-border bg-card p-2 font-mono text-[11px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-bold text-foreground">{label}</span>
        {present ? (
          <CheckCircle2 className="h-3 w-3 text-foreground" />
        ) : (
          <XCircle className="h-3 w-3 text-muted-foreground" />
        )}
        <span className="text-muted-foreground">{detail}</span>
      </div>
      {extras.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
          {extras.map((kw) => (
            <span
              key={kw}
              className="rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono"
            >
              {kw}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/** 跨资产方向网格 */
export function CrossAssetGrid({
  cad,
}: {
  cad: Review['cross_asset_direction']
}) {
  if (!cad.present) {
    return (
      <div className="rounded border border-border bg-background/40 p-2 font-mono text-[10px] text-muted-foreground">
        {cad.detail}
      </div>
    )
  }
  return (
    <div className="rounded border border-border bg-background/40 p-2">
      <div className="mb-1 flex flex-wrap items-center gap-2 font-mono text-[10px] text-muted-foreground">
        <Network className="h-3 w-3" />
        <span>对齐 {cad.aligned_count}</span>
        <span>·</span>
        <span>冲突 {cad.conflict_count}</span>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        {cad.items.map((it) => {
          const cls = it.neutral
            ? 'border-border bg-card text-muted-foreground'
            : it.conflicts
              ? 'border-border bg-background text-foreground'
              : it.aligned
                ? 'border-foreground/30 bg-foreground/10 text-foreground'
                : 'border-border bg-card text-muted-foreground'
          const arrow = it.neutral ? '·' : it.aligned ? '↑' : it.conflicts ? '↓' : '?'
          return (
            <span
              key={it.symbol}
              className={cn(
                'rounded border px-1.5 py-0.5 font-mono text-[10px]',
                cls,
              )}
              title={`${it.symbol} 24h ${it.pct_24h ?? '?'}%`}
            >
              {arrow} {it.symbol}
              {it.pct_24h !== null && (
                <span className="ml-1 text-muted-foreground">
                  {it.pct_24h.toFixed(2)}%
                </span>
              )}
            </span>
          )
        })}
      </div>
    </div>
  )
}

/** 单条决策卡 */
export function DecisionCard({ review }: { review: Review }) {
  const [open, setOpen] = useState(false)
  const [shownSegment, setShownSegment] = useState<string | null>(null)
  const [loadingText, setLoadingText] = useState(false)
  const [text, setText] = useState<DecisionTextResp | null>(null)
  const [textErr, setTextErr] = useState<string | null>(null)

  const loadText = useCallback(async () => {
    setLoadingText(true)
    setTextErr(null)
    try {
      const data = await apiGet<DecisionTextResp>(`/reasoning/decision/${review.decision_id}/text`)
      setText(data)
    } catch (e) {
      setTextErr(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoadingText(false)
    }
  }, [review.decision_id])

  // 切换展开 → 自动预加载原文（避免用户每点一次 dot 都重 fetch）
  useEffect(() => {
    if (open && !text && !loadingText && !textErr) {
      void loadText()
    }
  }, [open, text, loadingText, textErr, loadText])

  // 点击 dot → 切到对应段（在原文里高亮 / 滚到该段）
  const handleDotClick = useCallback(
    (segment: string, _idx: number) => {
      if (!open) {
        setOpen(true)
        setShownSegment(segment)
        return
      }
      setShownSegment((cur) => (cur === segment ? null : segment))
    },
    [open],
  )

  // 在原文里高亮当前选中段（按 char_index 切片显示前后 60 字）
  const highlightSnippet = useMemo(() => {
    if (!shownSegment || !text) return null
    const pos = text.segments.positions.find((p) => p.segment === shownSegment)
    if (!pos || pos.char_index === null) return null
    const i = pos.char_index
    const start = Math.max(0, i - 40)
    const end = Math.min(text.full_text.length, i + shownSegment.length + 60)
    return {
      before: text.full_text.slice(start, i),
      hit: text.full_text.slice(i, i + shownSegment.length),
      after: text.full_text.slice(i + shownSegment.length, end),
    }
  }, [shownSegment, text])

  return (
    <div className="rounded border border-border bg-card p-2 font-mono text-[11px]">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full flex-wrap items-center gap-2 text-left"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3 w-3 text-muted-foreground" />
        )}
        <span className="font-bold text-foreground">#{review.decision_id}</span>
        <span className="rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-foreground">
          {review.asset || '?'}
        </span>
        <ActionPill action={review.action} />
        <ScoreBadge score={review.completion_score} />
        <PnlPill pnl={review.forward_pnl} isCorrect={review.is_correct} />
        {/* v302+ P1: 一键导出本条复盘为 Markdown(粘贴到 Notion/飞书/研报)
            stopPropagation 避免点 Copy 按钮时触发展开/折叠 */}
        <CopyMarkdownButton review={review} text={text} compact stopPropagation />
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          推理 {review.reasoning_length} 字
        </span>
      </button>

      {open && (
        <div className="mt-2 flex flex-col gap-2">
          {/* 8 段 dot */}
          <div className="rounded border border-border bg-background/40 p-2">
            <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
              <Brain className="h-3 w-3" />
              8 段推理链（点击 dot 查看原文）
            </div>
            <StepDots
              steps={review.steps}
              decisionId={review.decision_id}
              shownSegment={shownSegment}
              onDotClick={handleDotClick}
            />
          </div>

          {/* 6 维明细 */}
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            <DimRow dimKey="counter_consensus" review={review} />
            <DimRow dimKey="causal_mechanism" review={review} />
            <DimRow dimKey="assumption" review={review} />
            <DimRow dimKey="historical_pattern" review={review} />
            <div className="md:col-span-2">
              <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
                <ArrowRightLeft className="h-3 w-3" />
                跨资产传导方向
              </div>
              <CrossAssetGrid cad={review.cross_asset_direction} />
            </div>
          </div>

          {/* 因子概念 */}
          {review.factor_concepts.count > 0 && (
            <div className="rounded border border-border bg-background/40 p-2">
              <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
                <Sparkles className="h-3 w-3" />
                因子概念（{review.factor_concepts.count}）
              </div>
              <div className="flex flex-wrap items-center gap-1">
                {review.factor_concepts.concepts.map((c) => (
                  <span
                    key={c}
                    className="rounded border border-border bg-card px-1.5 py-0.5 font-mono text-[10px] text-foreground"
                  >
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* 校准 */}
          {review.calibration.confidence !== null && (
            <div className="rounded border border-border bg-background/40 p-2 font-mono text-[10px] text-muted-foreground">
              <Lightbulb className="mr-1 inline h-3 w-3" />
              置信度{' '}
              <span className="font-bold text-foreground">
                {review.calibration.confidence.toFixed(2)}
              </span>
              {review.calibration.pnl_realized !== null && (
                <>
                  {' · 实现 pnl '}
                  <span className="font-bold text-foreground">
                    {(review.calibration.pnl_realized * 100).toFixed(2)}%
                  </span>
                </>
              )}
            </div>
          )}

          {/* notes */}
          {review.notes.length > 0 && (
            <div className="rounded border border-border bg-background/40 p-2">
              <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
                <AlertTriangle className="h-3 w-3" />
                信息性诊断
              </div>
              <ul className="list-inside list-disc space-y-0.5 font-mono text-[10px] text-foreground">
                {review.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </div>
          )}

          {/* 选中段原文高亮 */}
          {shownSegment && (
            <div className="rounded border border-border bg-background/40 p-2">
              <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
                <History className="h-3 w-3" />
                段原文（{shownSegment}）
              </div>
              {loadingText && (
                <span className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  加载原文…
                </span>
              )}
              {textErr && (
                <span className="font-mono text-[10px] text-muted-foreground">
                  加载失败：{textErr}
                </span>
              )}
              {text && highlightSnippet && (
                <div className="font-mono text-[10px] leading-relaxed text-foreground">
                  …{highlightSnippet.before}
                  <mark className="rounded bg-foreground/20 px-0.5 font-bold text-foreground">
                    {highlightSnippet.hit}
                  </mark>
                  {highlightSnippet.after}…
                </div>
              )}
              {text && !highlightSnippet && (
                <span className="font-mono text-[10px] text-muted-foreground">
                  段「{shownSegment}」在该决策文本中未出现
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

