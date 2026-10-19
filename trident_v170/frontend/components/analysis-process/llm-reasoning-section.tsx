'use client'

/**
 * v203+: 分区 3 —— LLM 推理过程。
 * 展示 reasoning 35 字 + reasoning_path 八段(带白话引导) + factor_ranking + elasticity_window。
 */

import type { APLlmCallMeta, APReasoning, APDecision } from '@/lib/analysis-process-data'
import { JsonDump, KeyValueGrid, SectionCard, StepPlayer, SubBlock, type PlayerStep } from './sections-shared'

// 八段骨架 key → 白话标题(模型在这里做了什么)
const SEGMENT_PLAIN: Record<string, { title: string; why: string }> = {
  macro_context:        { title: '① 看了大盘环境',          why: '模型先看 ETH/标的相关大盘现在什么状态(涨/跌/震荡)' },
  price_action_check:   { title: '② 核对了最近行情',        why: '最近 24h/72h 价格走势是否符合消息预期' },
  event_classification: { title: '③ 给新闻分了类',          why: '政策类/公司公告/突发事件/行情类,不同类处理逻辑不同' },
  impact_assessment:    { title: '④ 估了冲击力',            why: '模型估"这条新闻会让价格动多少%(MFE/MAE 上限)' },
  historical_comparison:{ title: '⑤ 比了历史类似新闻',       why: '过去类似消息出现后,标的反应是涨还是跌' },
  cross_signal_validation:{ title:'⑥ 用了其他指标交叉验证',  why: '链上数据/资金费率/持仓量等佐证消息的真实性' },
  decision_logic:       { title: '⑦ 给出买卖动作',          why: '综合 1~6,决定 BUY/SELL/HOLD,定标的 + 持仓期' },
  risk_factors:         { title: '⑧ 列了可能翻车的因素',    why: '如果这些因素出现,前面 7 步的结论就要作废' },
}

function plainSegment(key: string | null | undefined): { title: string; why: string } {
  if (!key) return { title: '步骤', why: '' }
  return SEGMENT_PLAIN[key] || { title: key, why: '' }
}

function formatJsonOrText(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v || '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export function LlmReasoningSection({
  reasoning,
  llm_meta,
  decision,
}: {
  reasoning: APReasoning | null
  llm_meta: APLlmCallMeta | null
  decision: APDecision | null
}) {
  if (!reasoning) {
    return (
      <SectionCard tone="purple" step="3" title="LLM 推理过程" subtitle="无 ai_decision 关联">
        <div className="font-mono text-xs text-muted-foreground">
          没有 AI 决策 —— 可能 Gate 阶段判定为不交易,或决策异步队列未追到。
        </div>
      </SectionCard>
    )
  }
  const segments = Array.isArray(reasoning.reasoning_segments) ? reasoning.reasoning_segments : []
  const factorList = Array.isArray(reasoning.factor_ranking) ? reasoning.factor_ranking : []
  const playerSteps: PlayerStep[] = segments.map((s, i) => {
    const p = plainSegment(s.key)
    return {
      id: `${i}-${s.key || 'step'}`,
      title: p.title,
      plain: p.why,
      body: s.value || '(空)',
      tone: 'purple',
      hint: `第 ${i + 1} 步 / 共 ${segments.length} 步`,
    }
  })
  return (
    <SectionCard
      tone="purple"
      step="3"
      title="LLM 推理过程"
      subtitle={`decision #${reasoning.decision_id} · ${decision?.target_asset || '—'}`}
      badge={decision?.suggested_action || '—'}
    >
      <div className="space-y-3">
        <SubBlock title="LLM 原文摘要" tone="note">
          <p className="text-[12px] leading-relaxed text-foreground/90">
            {reasoning.reasoning || '—'}
          </p>
        </SubBlock>

        {playerSteps.length > 0 ? (
          <div className="space-y-2">
            <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              ▶ AI 推理回放器(点播放,看 AI 一步步怎么想)
            </div>
            <StepPlayer
              steps={playerSteps}
              title="AI 的 8 步思考过程"
              intro={`这条新闻触发了一个 AI 推理会话。下面把模型的思考按顺序拆成 ${playerSteps.length} 段。点「播放」,看每一步在做什么、原文是怎么写的。`}
              finishNote={`8 步推理播完 —— 最终动作是 ${decision?.suggested_action || '—'} ${decision?.target_asset || ''} ${decision?.timeframe || ''}`}
              defaultSpeed={1}
            />
          </div>
        ) : null}

        {segments.length > 0 ? (
          <details className="rounded-md border border-border bg-background/40">
            <summary className="cursor-pointer px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              ▾ 原始 JSON 数组(8 段 thinking_steps)
            </summary>
            <div className="border-t border-border px-3 py-2">
              <JsonDump data={segments} />
            </div>
          </details>
        ) : null}

        {Object.keys(reasoning.elasticity_window || {}).length > 0 ? (
          <SubBlock title="弹性窗口(elasticity_window)" tone="muted">
            <JsonDump data={reasoning.elasticity_window} />
          </SubBlock>
        ) : null}

        {llm_meta ? (
          <SubBlock title="LLM 调用元数据 + 后端兜底字段" tone="warn">
            <KeyValueGrid
              items={[
                { label: 'agent_model', value: llm_meta.agent_model_id || '—', mono: true },
                {
                  label: 'evidence_action',
                  value: llm_meta.evidence_action || '—',
                  mono: true,
                  accent:
                    llm_meta.evidence_action !== decision?.suggested_action
                      ? 'text-short'
                      : 'text-foreground/90',
                },
                {
                  label: 'market_confirm',
                  value: llm_meta.market_confirmation || '—',
                  mono: true,
                },
                { label: 'event_strength', value: llm_meta.event_strength || '—', mono: true },
                {
                  label: 'impact_magnitude',
                  value: String(llm_meta.impact_magnitude ?? '—'),
                  mono: true,
                  accent: llm_meta.impact_magnitude === 0 ? 'text-hold' : '',
                },
                {
                  label: 'evidence_conf',
                  value: String(llm_meta.evidence_confidence ?? '—'),
                  mono: true,
                },
                { label: 'attention', value: String(llm_meta.attention_score ?? '—'), mono: true },
                { label: 'event_phase', value: llm_meta.event_phase || '—', mono: true },
                { label: 'timeframe', value: llm_meta.timeframe_match || '—', mono: true },
                { label: 'gate_reason', value: llm_meta.trade_gate_reason || '—', mono: true },
                { label: 'no_trade', value: llm_meta.no_trade_reason || '—', mono: true },
              ]}
            />
            {llm_meta.evidence_action &&
            decision?.suggested_action &&
            llm_meta.evidence_action !== decision.suggested_action ? (
              <div className="mt-2 rounded border border-short/30 bg-short/5 px-2 py-1.5 text-[11px] text-short">
                ⚠ evidence_action 与 suggested_action 矛盾 ——
                evidence=<span className="font-mono font-bold">{llm_meta.evidence_action}</span>
                {' '}vs LLM=<span className="font-mono font-bold">{decision.suggested_action}</span>
              </div>
            ) : null}
          </SubBlock>
        ) : null}

        {(reasoning.verification || reasoning.hard_data_evidence || reasoning.invalidation_condition) ? (
          <SubBlock title="证据 / 失效条件" tone="muted">
            <div className="space-y-1">
              {reasoning.hard_data_evidence ? (
                <div>
                  <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-primary">
                    hard_data:
                  </span>
                  <span className="ml-1 text-[12px] text-foreground/90">
                    {reasoning.hard_data_evidence}
                  </span>
                </div>
              ) : null}
              {reasoning.invalidation_condition ? (
                <div>
                  <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-short">
                    invalidation:
                  </span>
                  <span className="ml-1 text-[12px] text-foreground/90">
                    {reasoning.invalidation_condition}
                  </span>
                </div>
              ) : null}
              {reasoning.verification ? (
                <div>
                  <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-hold">
                    verification:
                  </span>
                  <span className="ml-1 font-mono text-[11px] text-foreground/90">
                    {formatJsonOrText(reasoning.verification)}
                  </span>
                </div>
              ) : null}
            </div>
          </SubBlock>
        ) : null}

        {decision ? (
          <details className="rounded-md border border-border bg-background/40">
            <summary className="cursor-pointer px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              ▾ ai_decision 全字段 JSON
            </summary>
            <div className="border-t border-border px-3 py-2">
              <JsonDump data={decision} />
            </div>
          </details>
        ) : null}
      </div>
    </SectionCard>
  )
}