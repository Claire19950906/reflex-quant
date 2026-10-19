'use client'

import type {
  APAttributionReview,
  APNewsTradeReview,
  APStructuredReflection,
  APTradeReflection,
  APDecision,
} from '@/lib/analysis-process-data'
import { fmtTs, fmtPctSigned, verdictColor } from '@/lib/analysis-process-data'
import {
  JsonDump,
  KeyValueGrid,
  SectionCard,
  StepPlayer,
  SubBlock,
  type PlayerStep,
} from './sections-shared'

// ───────────────────────────────────────────────────────────
// buildReflectionSteps —— 把全部反思 / 归因 / 二次复核拆成 sequence
// 每条反思 = 一个 step;每条归因 = 一个 step;每条结构化 = 一个 step
// ───────────────────────────────────────────────────────────

function buildReflectionSteps(
  reflections: APTradeReflection[],
  structured: APStructuredReflection[],
  attributions: APAttributionReview[],
  news_trade_review: APNewsTradeReview | null,
  decision: APDecision | null,
): PlayerStep[] {
  const steps: PlayerStep[] = []

  // 0) 决策快照(放在最前,做上下文)
  if (decision) {
    steps.push({
      id: 'r-decision',
      title: '◐ 决策快照(复盘对象)',
      plain: '先复盘这条决策:最终动作、标的、最终结果(盈利/亏损/未结算)。',
      body: `[action] ${decision.suggested_action || '—'}\n[asset] ${
        decision.target_asset || '—'
      }\n[timeframe] ${decision.expected_horizon || '—'}\n[forward_pnl] ${
        decision.forward_pnl ?? '—'
      }\n[is_correct] ${decision.is_correct || '—'}\n[settled] ${
        decision.settled ?? '—'
      }`,
      tone: decision.is_correct === 'WIN' ? 'long' : decision.is_correct === 'LOSS' ? 'short' : 'hold',
    })
  }

  // 1) news_trade_review 缓存摘要
  if (news_trade_review) {
    const parsed = news_trade_review.summary_parsed
      ? JSON.stringify(news_trade_review.summary_parsed, null, 2).slice(0, 600)
      : ''
    steps.push({
      id: 'r-review',
      title: '① news_trade_review 摘要',
      plain:
        'review_agent 异步调 LLM,对这条新闻的"事件 × 决策 × 市场反馈"做了一次全文总结,缓存到这一行。',
      body: `[news_id] ${news_trade_review.news_id}\n[summary] ${String(
        news_trade_review.summary || '—',
      ).slice(0, 400)}\n${
        parsed ? `[summary_parsed]\n${parsed}` : ''
      }`,
      tone: 'blue',
    })
  }

  // 2) 每条 trade_reflection 一步
  reflections.forEach((r, i) => {
    const tone =
      r.attribution_code === null || r.attribution_code === undefined
        ? 'gold'
        : r.attribution_code && Number(r.attribution_code) < 50
          ? 'long'
          : 'short'
    steps.push({
      id: `r-refl-${i}`,
      title: `②.${i + 1} trade_reflection #${r.id}`,
      plain:
        'LLM 给这笔交易写了反思原文:当时模型看了什么、怎么想、为什么这样判。',
      body: `[decision_id] ${r.decision_id}\n[reflected_at] ${fmtTs(
        r.reflected_at,
      )}\n[attribution_code] ${
        r.attribution_code ?? '—'
      }\n[reflection]\n${String(r.reflection || '—').slice(0, 400)}\n${
        r.attribution_text
          ? `\n[attribution_text]\n${String(r.attribution_text).slice(0, 200)}`
          : ''
      }`,
      tone: tone as PlayerStep['tone'],
    })
  })

  // 3) 每条 structured_reflection 一步(实际:校验分数 + 缺失事件 + 状态)
  structured.forEach((s, i) => {
    const verdictTone =
      s.verdict === 'pass'
        ? 'long'
        : s.verdict === 'fail'
          ? 'short'
          : 'hold'
    steps.push({
      id: `r-str-${i}`,
      title: `③.${i + 1} structured_reflection #${s.id}`,
      plain:
        '对这条反思做了"二次校验"——评分覆盖度(0-1)、是否幻觉(0-1)、缺哪些事件、状态是否被晋升为 insight。',
      body: `[decision_id] ${s.decision_id}\n[news_id] ${
        String(s.news_id || '—')
      }\n[asset] ${String(s.asset || '—')}\n[event_type_code] ${String(
        s.event_type_code || '—',
      )}\n[verdict] ${String(s.verdict || '—')}\n[score_coverage] ${
        s.score_coverage ?? '—'
      }\n[score_fabrication] ${
        s.score_fabrication ?? '—'
      }\n[entities_fabricated] ${String(
        s.entities_fabricated || '—',
      )}\n[events_missing] ${String(
        s.events_missing || '—',
      )}\n[status] ${String(
        s.status || '—',
      )}\n[retry_count] ${String(
        s.retry_count ?? '—',
      )}\n[last_error] ${String(
        s.last_error || '—',
      )}\n[promoted_insight_id] ${String(
        s.promoted_insight_id ?? '—',
      )}\n[created_at] ${String(s.created_at || '—')}`,
      tone: verdictTone as PlayerStep['tone'],
    })
  })

  // 4) 每条 attribution_review 一步(人工二次复核)
  attributions.forEach((a, i) => {
    const status = String(a.human_status || 'pending')
    const tone =
      status === 'agree' ? 'long' : status === 'disagree' ? 'short' : 'hold'
    steps.push({
      id: `r-attr-${i}`,
      title: `④.${i + 1} attribution_review #${a.id}`,
      plain:
        '你 / 团队成员对自动归因是否同意 —— agree / disagree / pending 状态机。',
      body: `[ref_id] ${String(a.ref_id || '—')}\n[trade_id] ${String(
        a.trade_id || '—',
      )}\n[code] ${String(a.code || '—')}\n[text] ${String(
        a.text || '—',
      ).slice(0, 300)}\n[human_status] ${status}`,
      tone: tone as PlayerStep['tone'],
    })
  })

  // 5) 兜底
  if (steps.length === 0) {
    steps.push({
      id: 'r-empty',
      title: '这条决策还没有复盘数据',
      plain:
        '持仓 settle 后 review_agent 会异步启动;若 1 小时仍为空,可能是 review 任务没在跑。',
      body: '没有 news_trade_review / trade_reflection / structured_reflection / attribution_review 任何记录',
      tone: 'gold',
    })
  }

  return steps
}

export function ReflectionSection({
  reflections,
  structured,
  attributions,
  news_trade_review,
  decision,
}: {
  reflections: APTradeReflection[]
  structured: APStructuredReflection[]
  attributions: APAttributionReview[]
  news_trade_review: APNewsTradeReview | null
  decision: APDecision | null
}) {
  const hasAny =
    reflections?.length ||
    structured?.length ||
    attributions?.length ||
    news_trade_review
  return (
    <SectionCard
      tone="red"
      step="5"
      title="复盘归因"
      subtitle="trade_reflections + structured + attribution"
      badge={
        hasAny
          ? `${(reflections?.length || 0) + (structured?.length || 0) + (attributions?.length || 0)} 条`
          : '待复盘'
      }
    >
      <div className="space-y-3">
        {hasAny ? (
          <>
            <p className="text-[12px] leading-relaxed text-foreground/90">
              仓单结束后系统自动回顾"模型当时为什么对/为什么错",并把错误类型归到一个固定编号上(归因编码 attribution_code),方便下次遇到同类问题能提前躲开。
            </p>

            {/* ▶ 反思过程细节回放器(默认展开) */}
            <div className="space-y-2">
              <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                ▶ 反思过程细节回放器
              </div>
              <StepPlayer
                steps={buildReflectionSteps(
                  reflections || [],
                  structured || [],
                  attributions || [],
                  news_trade_review || null,
                  decision,
                )}
                title="复盘归因 · 全部字段逐步"
                intro={`把 ${(reflections?.length || 0) + (structured?.length || 0) + (attributions?.length || 0)} 条反思 / 归因 / 二次复核逐条拆开,看每一条在 LLM 这一侧生成了什么、字段值是什么。`}
                finishNote={
                  decision
                    ? `本次复盘结论:${decision.is_correct === 'WIN' ? '✓ 验证 AI 这次对了' : decision.is_correct === 'LOSS' ? '✗ AI 这次错了' : '⏳ 未结算'}`
                    : '播放完毕'
                }
                defaultSpeed={1.5}
              />
            </div>

            {news_trade_review ? (
              <details className="rounded-md border border-border bg-background/40">
                <summary className="cursor-pointer px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                  ▾ news_trade_review 全字段 JSON
                </summary>
                <div className="border-t border-border px-3 py-2">
                  <JsonDump data={news_trade_review} />
                </div>
              </details>
            ) : null}

            {reflections?.length ? (
              <SubBlock title={`trade_reflections × ${reflections.length} · 自动反思`} tone="muted">
                <p className="mb-2 text-[11px] text-muted-foreground">
                  LLM 给每笔交易写了反思原文 —— 当时 AI 看了什么 / 怎么想 / 为什么这样判。
                </p>
                {reflections.map((r, i) => (
                  <div key={r.id || i} className="border-t border-border py-2 first:border-t-0 first:pt-0">
                    <div className="mb-1 flex items-baseline justify-between gap-2">
                      <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                        reflection #{r.id} · {fmtTs(r.reflected_at)}
                      </span>
                      {r.attribution_code ? (
                        <span className="rounded border border-hold/40 bg-hold/10 px-1.5 py-0.5 font-mono text-[10px] text-hold">
                          归因编号 {String(r.attribution_code)}
                        </span>
                      ) : null}
                    </div>
                    <p className="text-[12px] leading-relaxed text-foreground/90">
                      {String(r.reflection || '—')}
                    </p>
                    {r.attribution_text ? (
                      <div className="mt-1 text-[11px] text-muted-foreground">
                        归因说明: {String(r.attribution_text)}
                      </div>
                    ) : null}
                  </div>
                ))}
              </SubBlock>
            ) : null}

            {structured?.length ? (
              <SubBlock title={`structured_reflections × ${structured.length} · 结构化归因`} tone="muted">
                <p className="mb-2 text-[11px] text-muted-foreground">
                  三段式 —— 这次错/对属于哪种类型、根本原因是什么、以后该怎么避。
                </p>
                {structured.map((s, i) => (
                  <div key={s.id || i} className="border-t border-border py-2 first:border-t-0 first:pt-0">
                    <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                      structured #{s.id} · {String(s.result_type || '—')}
                    </div>
                    <KeyValueGrid
                      items={[
                        { label: 'result_type', value: String(s.result_type || '—'), mono: true },
                        { label: 'root_cause', value: String(s.root_cause || '—') },
                        { label: 'lessons', value: String(s.lessons || '—') },
                      ]}
                    />
                  </div>
                ))}
              </SubBlock>
            ) : null}

            {attributions?.length ? (
              <SubBlock title={`attribution_reviews × ${attributions.length} · 人工二次复核`} tone="muted">
                <p className="mb-2 text-[11px] text-muted-foreground">
                  你 / 团队成员对自动归因是否同意(agree / disagree / pending)。
                </p>
                {attributions.map((a, i) => (
                  <div key={a.id || i} className="border-t border-border py-2 first:border-t-0 first:pt-0">
                    <div className="mb-1 flex items-baseline justify-between gap-2">
                      <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                        attribution #{a.id} · ref={String(a.ref_id || '—')} · trade={String(a.trade_id || '—')}
                      </span>
                      <span
                        className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${(a.human_status || 'pending') === 'pending' ? 'border-hold/40 bg-hold/10 text-hold' : (a.human_status || '') === 'agree' ? 'border-long/40 bg-long/10 text-long' : 'border-short/40 bg-short/10 text-short'}`}
                      >
                        {String(a.human_status || 'pending')}
                      </span>
                    </div>
                    <div className="text-[11px] text-foreground/85">
                      <span className="mr-1 font-mono text-[10px] text-muted-foreground">code:</span>
                      {String(a.code || '—')}
                      <span className="mx-2 font-mono text-[10px] text-muted-foreground">·</span>
                      <span>{String(a.text || '—')}</span>
                    </div>
                  </div>
                ))}
              </SubBlock>
            ) : null}
          </>
        ) : (
          <div className="rounded-md border border-hold/40 bg-hold/5 px-3 py-3 text-[12px] text-foreground/85">
            <div className="mb-1 font-mono text-[10px] font-bold uppercase tracking-wider text-hold">
              ⏳ 待复盘
            </div>
            <p className="leading-relaxed">
              这条决策还没有被复盘队列追到。复盘异步调度通常在持仓 settle 后
              N 分钟内启动;若超过 1 小时仍为空,可能是 review_agent 没在跑,或归因规则缺失。
            </p>
            {decision ? (
              <div className="mt-2 rounded border border-border bg-background/60 px-2 py-1.5 font-mono text-[11px]">
                <span className="text-muted-foreground">决策快照:</span>
                {' '}
                {decision.target_asset || '—'} {decision.suggested_action || '—'} · settled=
                {String(decision.settled ?? '—')} · pnl=
                {fmtPctSigned(decision.forward_pnl as number | null)} · verdict=
                <span className={verdictColor(decision.is_correct as string | null | undefined)}>
                  {String(decision.is_correct || '—')}
                </span>
              </div>
            ) : null}
          </div>
        )}
      </div>
    </SectionCard>
  )
}