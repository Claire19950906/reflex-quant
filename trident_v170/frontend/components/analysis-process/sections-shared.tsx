'use client'

/**
 * v203+: 分区组件共享 UI 工具 —— SectionCard(分区壳) + KeyValue 行 + 字段树形渲染。
 *
 * 每个分区 = 一个 SectionCard,左侧色条 + 阶段号 + 标题 + 折叠按钮。
 * 内容区 KeyValue 行 + 折叠子块 + JSON 兜底展示。
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'

export type SectionTone = 'blue' | 'purple' | 'green' | 'red' | 'gold'

const TONE_STYLES: Record<SectionTone, { border: string; badge: string; bg: string }> = {
  blue:   { border: 'border-l-primary/70',     badge: 'bg-primary/15 text-primary border-primary/40', bg: 'bg-primary/5' },
  purple: { border: 'border-l-purple-500/70',  badge: 'bg-purple-500/15 text-purple-600 border-purple-500/40 dark:text-purple-300', bg: 'bg-purple-500/5' },
  green:  { border: 'border-l-long/70',        badge: 'bg-long/15 text-long border-long/40', bg: 'bg-long/5' },
  red:    { border: 'border-l-short/70',       badge: 'bg-short/15 text-short border-short/40', bg: 'bg-short/5' },
  gold:   { border: 'border-l-hold/70',        badge: 'bg-hold/15 text-hold border-hold/40', bg: 'bg-hold/5' },
}

export function SectionCard({
  tone,
  step,
  title,
  subtitle,
  badge,
  children,
  defaultOpen = true,
}: {
  tone: SectionTone
  step: string
  title: string
  subtitle?: string
  badge?: string
  children: ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const t = TONE_STYLES[tone]
  return (
    <div className={`rounded-lg border border-border ${t.border} border-l-4 ${t.bg}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left"
      >
        <span
          className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border font-mono text-[11px] font-bold ${t.badge}`}
        >
          {step}
        </span>
        <span className="flex min-w-0 flex-1 items-baseline gap-2">
          <span className="font-mono text-xs font-bold uppercase tracking-wider text-foreground">
            {title}
          </span>
          {subtitle ? (
            <span className="truncate font-mono text-[10px] text-muted-foreground">
              {subtitle}
            </span>
          ) : null}
        </span>
        {badge ? (
          <span className={`rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-wider ${t.badge}`}>
            {badge}
          </span>
        ) : null}
        <span className="font-mono text-[10px] text-muted-foreground">
          {open ? '−' : '+'}
        </span>
      </button>
      {open ? <div className="border-t border-border/50 px-3 py-3">{children}</div> : null}
    </div>
  )
}

export function KeyValue({
  label,
  value,
  mono = false,
  accent = '',
  className = '',
}: {
  label: string
  value: ReactNode
  mono?: boolean
  accent?: string
  className?: string
}) {
  return (
    <div className={`flex items-start gap-2 py-1 ${className}`}>
      <span className="w-24 shrink-0 pt-0.5">
        <PlainLabel
          term={label}
          className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground"
        />
      </span>
      <span
        className={`min-w-0 flex-1 break-words text-[12px] ${mono ? 'font-mono' : ''} ${accent || 'text-foreground/90'}`}
      >
        {value || '—'}
      </span>
    </div>
  )
}

export function KeyValueGrid({
  items,
}: {
  items: Array<{ label: string; value: ReactNode; mono?: boolean; accent?: string }>
}) {
  return (
    <div className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
      {items.map((it, i) => (
        <KeyValue key={i} label={it.label} value={it.value} mono={it.mono} accent={it.accent} />
      ))}
    </div>
  )
}

export function SubBlock({
  title,
  children,
  tone = 'muted',
}: {
  title: string
  children: ReactNode
  tone?: 'muted' | 'warn' | 'note'
}) {
  const styles = {
    muted: 'border-border bg-muted/40 text-foreground/85',
    warn:  'border-hold/40 bg-hold/5 text-foreground/85',
    note:  'border-primary/30 bg-primary/5 text-foreground/85',
  }[tone]
  return (
    <div className={`mt-2 rounded-md border px-3 py-2 text-[12px] ${styles}`}>
      <div className="mb-1 font-mono text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
        {title}
      </div>
      {children}
    </div>
  )
}

export function JsonDump({ data }: { data: unknown }) {
  return (
    <pre className="thin-scroll max-h-72 overflow-auto rounded-md border border-border bg-background/60 px-3 py-2 font-mono text-[10px] leading-relaxed text-foreground/80">
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}

// ─────── 术语白话翻译表 ───────
const GLOSSARY: Record<string, string> = {
  '入库':'这条新闻刚刚从抓取渠道(东财/金十/FRED 等)落库,带有时间戳和指纹',
  'Gate 筛选':'不重要的新闻会被"Gate"过滤掉,Gate 看 5 个维度 —— 主资产/类型/时效/强度/评级',
  'AI 推理':'把新闻文本+证据+Gate 信号丢给大模型,模型给出"涨/跌/不动"的判断和原因',
  '交易执行':'AI 决定"买卖"→ 模型推断在指定时间内实际赚多少',
  '复盘归因':'仓单结束后,系统回头看:模型当时为什么对/为什么错,归类到错误原因',
  '经验沉淀':'复盘结论提炼为"以后遇到类似情况该怎么处理"的规则,写入知识库',
  main_asset:'最可能命中的标的(币/股票/大宗)',
  kind:'这条新闻的类型(政策/公告/突发事件/行情等)',
  timing:'时效评级 —— 多快该看',
  strength:'信号强度,0-10 分',
  verdict:'综合判定 —— 可交易/阻断/观察',
  core_impact:'AI 提取出来的"一句话影响总结"',
  evidence_action:'证据指向的方向(BUY/SELL/HOLD)',
  suggested_action:'AI 推荐的买卖动作(BUY/SELL/HOLD)',
  evidence_confidence:'AI 对自己证据的把握程度,0-1',
  market_confirmation:'行情是否跟新闻吻合(双向校验)',
  impact_magnitude:'模型估算的价格冲击幅度',
  event_phase:'事件阶段(初发/发酵/高潮/衰退)',
  expected_horizon:'期望持仓时间(15m/1h/4h/24h/3d 等)',
  forward_pnl:'模型预测的价格涨跌幅度,负数=跌',
  pnl:'实际盈亏百分比,负数=亏',
  is_correct:'模型判断对错(WIN/CORRECT/LOSS/PENDING 等)',
  MFE:'maximum positive excursion ——仓单持有期里最大的浮动盈利',
  MAE:'maximum negative excursion ——仓单持有期里最大的浮动亏损',
  reasoning:'AI 写的"为什么这样判"的那段理由',
  reasoning_segments:'AI 拆出来的 8 段思考',
  factor_ranking:'AI 列出的"什么因素排第一、什么因素排第二"',
  elasticity_window:'AI 估计的"消息会在哪个时间窗口里继续推价格"',
  hard_data_evidence:'硬数据(行情/链上/FRED)佐证',
  invalidation_condition:'什么情况下这个判断"不再成立"',
  attribution_code:'归因编号 —— 对应一个固定的错误类型表',
  review_insights:'复盘后沉淀出来的"规则"',
  experience_entries:'经验库里的"条目"(已结构化教训)',
  causal_chain_links:'系统里"什么新闻 → 推什么标的"的因果规则',
  trigger_words:'原话里识别到的触发关键词',
  counter_factual:'反事实分析 —— 如果当时不下场会怎样',
  primary_decision_id:'该新闻下多个 AI 决策中的"主决策"(一般是最高分)',
  agent_model_id:'使用的大模型型号(gpt-4o / deepseek-v3 / qwen-max 等)',
  decisions_settled:'该新闻下已了结的场数',
  decisions_win:'已了结中赚的场数',
  latest_pnl:'最近一笔决策的预估盈亏',
  latest_verdict:'最近一笔决策的对错',
  rule_active:'该规则当前是否启用',
}

export function PlainLabel({ term, className = '' }: { term: string; className?: string }) {
  const explain = GLOSSARY[term]
  return (
    <span className={`group relative inline-block ${className}`}>
      <span className="cursor-help border-b border-dotted border-muted-foreground/60">{term}</span>
      {explain ? (
        <span className="pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 hidden w-72 -translate-x-1/2 rounded-md border border-border bg-background/95 px-3 py-2 text-left text-[11px] leading-relaxed text-foreground/90 shadow-lg group-hover:block">
          <span className="mb-1 block font-mono text-[10px] font-bold uppercase tracking-wider text-primary">
            {term}
          </span>
          {explain}
        </span>
      ) : null}
    </span>
  )
}

// ─────── Timeline 横向节点(阶段流水线视觉) ───────
export function Timeline({
  nodes,
  activeIdx,
  onSelect,
}: {
  nodes: Array<{
    key: string
    title: string
    plain: string
    state: 'done' | 'partial' | 'pending' | 'skipped'
    detail?: string
  }>
  activeIdx?: number
  onSelect?: (idx: number) => void
}) {
  return (
    <div className="rounded-lg border border-border bg-card/60 p-3">
      <div className="flex items-stretch gap-1">
        {nodes.map((n, i) => {
          const isActive = i === activeIdx
          const STATE_STYLE = {
            done:    { dot: 'border-long bg-long text-white',  bar: 'bg-long/70' },
            partial: { dot: 'border-hold bg-hold text-white',  bar: 'bg-hold/70' },
            pending: { dot: 'border-border bg-muted text-muted-foreground', bar: 'bg-border' },
            skipped: { dot: 'border-border bg-muted text-muted-foreground', bar: 'bg-border/40' },
          }[n.state]
          const STATE_LABEL = {
            done: '✓ 已完成',partial: '◐ 进行中',pending: '○ 待处理',skipped: '− 未触发',
          }[n.state]
          return (
            <button
              key={n.key}
              type="button"
              onClick={() => onSelect?.(i)}
              className="flex min-w-0 flex-1 items-stretch text-left transition-opacity hover:opacity-100"
            >
              <div className="flex w-full flex-col items-center gap-1">
                <div className="flex w-full items-center">
                  <div className={`h-0.5 flex-1 ${i === 0 ? 'opacity-0' : STATE_STYLE.bar}`} />
                  <span
                    className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full border-2 font-mono text-[11px] font-bold ${STATE_STYLE.dot} ${
                      isActive ? 'ring-2 ring-primary/40 ring-offset-2 ring-offset-card' : ''
                    }`}
                  >
                    {n.state === 'done' ? '✓' : n.state === 'pending' ? (i + 1) : n.state === 'partial' ? '◐' : '−'}
                  </span>
                  <div className={`h-0.5 flex-1 ${i === nodes.length - 1 ? 'opacity-0' : STATE_STYLE.bar}`} />
                </div>
                <div className="text-center">
                  <div className={`text-[12px] font-bold ${isActive ? 'text-foreground' : 'text-foreground/85'}`}>
                    {n.title}
                  </div>
                  <div className="mt-0.5 text-[10px] text-muted-foreground">{n.plain}</div>
                  <div className="mt-0.5 font-mono text-[9px] uppercase tracking-wider text-muted-foreground/70">
                    {STATE_LABEL}
                  </div>
                </div>
                {n.detail ? (
                  <div className="mt-0.5 line-clamp-1 max-w-full text-[10px] text-muted-foreground">
                    {n.detail}
                  </div>
                ) : null}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────
// StepPlayer —— 步骤回放器
// 把一组步骤拆成 sequence,自动 / 手动切换当前段,看 typing 文本。
// ───────────────────────────────────────────────────────────

export type PlayerStep = {
  id: string
  title: string
  /** 白话版:这一步到底在做什么 */
  plain: string
  /** 模型 / 系统的原文输出(打字机展示) */
  body: string
  hint?: string
  tone?: 'blue' | 'purple' | 'green' | 'red' | 'gold' | 'long' | 'short' | 'hold'
}

const TONE_GLYPH: Record<NonNullable<PlayerStep['tone']>, string> = {
  blue: '🔵', purple: '🟣', green: '🟢', red: '🔴', gold: '🟡',
  long: '🟢', short: '🔴', hold: '🟡',
}
const TONE_BG: Record<NonNullable<PlayerStep['tone']>, string> = {
  blue: 'border-primary/50 bg-primary/10',
  purple: 'border-purple-500/50 bg-purple-500/10',
  green: 'border-long/80 bg-long/10',
  red: 'border-short/80 bg-short/10',
  gold: 'border-hold/80 bg-hold/10',
  long: 'border-long/80 bg-long/10',
  short: 'border-short/80 bg-short/10',
  hold: 'border-hold/80 bg-hold/10',
}

export function StepPlayer({
  steps,
  title,
  intro,
  finishNote,
  defaultSpeed = 1,
}: {
  steps: PlayerStep[]
  title: string
  intro?: string
  finishNote?: string
  defaultSpeed?: number
}) {
  const [idx, setIdx] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(defaultSpeed)
  const [typedLen, setTypedLen] = useState(0)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const activeStepRef = useRef<HTMLDivElement | null>(null)

  const step = steps[idx]
  const total = steps.length
  const progress = ((idx + 1) / total) * 100
  const done = idx >= total - 1 && typedLen >= (step?.body.length ?? 0)

  // 推进 idx
  useEffect(() => {
    if (!playing) return
    const stayMs = Math.max(700, 2200 / speed)
    const t = setTimeout(() => {
      setIdx((i) => {
        if (i >= total - 1) {
          setPlaying(false)
          return i
        }
        return i + 1
      })
    }, stayMs)
    return () => clearTimeout(t)
  }, [playing, idx, speed, total])

  // 切换 step 时:重置 typing + 滚动到当前
  useEffect(() => {
    setTypedLen(0)
    activeStepRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [idx])

  // typing 动画
  useEffect(() => {
    if (!step) return
    const target = step.body.length
    if (typedLen >= target) return
    const t = setInterval(() => {
      setTypedLen((n) => Math.min(target, n + 3))
    }, Math.max(20, 60 / speed))
    return () => clearInterval(t)
  }, [step, speed, typedLen])

  const goPrev = () => setIdx((i) => Math.max(0, i - 1))
  const goNext = () => setIdx((i) => Math.min(total - 1, i + 1))
  const restart = () => { setIdx(0); setTypedLen(0); setPlaying(true) }
  const skipTyping = () => setTypedLen(step?.body.length ?? 0)

  if (!step) return null
  const shown = step.body.slice(0, typedLen)
  const isTyping = typedLen < step.body.length

  return (
    <div
      ref={containerRef}
      className="rounded-lg border-2 border-purple-500/40 bg-purple-500/5 p-4"
    >
      {/* 标题 + intro */}
      <div className="mb-3 flex items-baseline gap-2">
        <span className="text-base">▶</span>
        <span className="font-mono text-[11px] font-bold uppercase tracking-widest text-purple-700 dark:text-purple-300">
          {title}
        </span>
        <span className="font-mono text-[10px] text-muted-foreground">· {total} 步</span>
      </div>
      {intro ? (
        <p className="mb-3 text-[12px] leading-relaxed text-foreground/85">{intro}</p>
      ) : null}

      {/* 进度条 */}
      <div className="mb-3 flex items-center gap-2">
        <div className="relative h-2 flex-1 overflow-hidden rounded-full border border-border bg-background/60">
          <div
            className="absolute inset-y-0 left-0 bg-purple-500 transition-all duration-300"
            style={{ width: `${progress}%` }}
          />
        </div>
        <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
          {idx + 1} / {total}
        </span>
      </div>

      {/* 控制按钮 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setPlaying((v) => !v)}
          className="rounded border border-purple-500/50 bg-purple-500/20 px-3 py-1 font-mono text-[12px] font-bold text-purple-700 hover:bg-purple-500/30 dark:text-purple-200"
        >
          {playing ? '⏸ 暂停' : '▶ 播放'}
        </button>
        <button
          type="button"
          onClick={goPrev}
          disabled={idx === 0}
          className="rounded border border-border bg-background px-2 py-1 font-mono text-[11px] disabled:opacity-40"
        >
          ⏮ 上一步
        </button>
        <button
          type="button"
          onClick={goNext}
          disabled={idx >= total - 1}
          className="rounded border border-border bg-background px-2 py-1 font-mono text-[11px] disabled:opacity-40"
        >
          ⏭ 下一步
        </button>
        <button
          type="button"
          onClick={restart}
          className="rounded border border-border bg-background px-2 py-1 font-mono text-[11px]"
        >
          ↻ 重头
        </button>
        {isTyping ? (
          <button
            type="button"
            onClick={skipTyping}
            className="rounded border border-border bg-background px-2 py-1 font-mono text-[11px]"
          >
            ⤳ 全文
          </button>
        ) : null}
        <div className="ml-auto flex items-center gap-1">
          <span className="font-mono text-[10px] text-muted-foreground">速度</span>
          {[0.5, 1, 2, 5].map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setSpeed(s)}
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${
                speed === s
                  ? 'border-purple-500/60 bg-purple-500/30 text-purple-700 dark:text-purple-200'
                  : 'border-border bg-background text-muted-foreground'
              }`}
            >
              {s}×
            </button>
          ))}
        </div>
      </div>

      {/* 当前 step 卡片(打字机展示) */}
      <div
        ref={activeStepRef}
        className={`rounded-md border-2 ${TONE_BG[step.tone || 'purple']} p-4 transition-all`}
      >
        <div className="mb-2 flex items-baseline gap-2">
          <span className="text-base">{TONE_GLYPH[step.tone || 'purple']}</span>
          <span className="font-mono text-[11px] font-bold uppercase tracking-wider text-foreground">
            Step {idx + 1} / {total}
          </span>
          <span className="text-[14px] font-bold text-foreground">{step.title}</span>
          {step.hint ? (
            <span className="ml-auto font-mono text-[10px] text-muted-foreground">
              {step.hint}
            </span>
          ) : null}
        </div>
        <p className="mb-3 text-[12px] leading-relaxed text-foreground/85">{step.plain}</p>
        <div className="rounded-md border border-border bg-background/70 p-3 font-mono text-[12px] leading-relaxed text-foreground/90">
          {shown}
          {isTyping ? (
            <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-purple-500" />
          ) : null}
        </div>
      </div>

      {/* 步骤缩略图列表(点状态指示跳转) */}
      <div className="mt-3 flex flex-wrap gap-1">
        {steps.map((s, i) => {
          const state = i < idx ? 'done' : i === idx ? 'active' : 'pending'
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => { setIdx(i); setPlaying(false) }}
              className={`flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-[10px] ${
                state === 'active'
                  ? 'border-purple-500/60 bg-purple-500/20 text-purple-700 dark:text-purple-200'
                  : state === 'done'
                      ? 'border-long/40 bg-long/10 text-long'
                      : 'border-border bg-background text-muted-foreground'
              }`}
              title={s.title}
            >
              <span>{state === 'done' ? '✓' : state === 'active' ? '●' : i + 1}</span>
              <span className="max-w-[120px]">{s.title}</span>
            </button>
          )
        })}
      </div>

      {/* 完成提示 */}
      {done && finishNote ? (
        <div className="mt-3 rounded-md border border-long/40 bg-long/10 px-3 py-2 text-[12px] text-long">
          ✓ {finishNote}
        </div>
      ) : null}
    </div>
  )
}
