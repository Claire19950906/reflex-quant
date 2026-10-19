'use client'

import { useCallback, useEffect, useState } from 'react'
import { Brain, AlertTriangle, CheckCircle2, XCircle, Loader2, ChevronRight, Send, History } from 'lucide-react'
import { API_BASE } from '@/lib/api'
import { cn } from '@/lib/utils'

/**
 * 🤖 AI 元认知自检(Meta-Cognition Test)
 *
 * 设计动机(用户原话):
 *   "假如我输入给因果图谱一个这样的概念:
 *    '市场上绝大部分涨跌都是美联储加息减息预期的变化',
 *    你会怎么处理?会先设立这个预期,再做决策?还是咋么样的流程,
 *    这个点子给你了以后,你先怎么分析这个点子是否正确,
 *    然后怎么纠正观念,或者你的因果图谱根本没有这一步?"
 *
 * 这个问题暴露了一个真实风险:
 *   LLM/因果图谱很容易「先有结论再找证据」(confirmation bias)。
 *   用户希望:AI 接受新假设时,**先**经过一道**元认知质询**,
 *   再决定「加入图谱」还是「拒绝」或者「需要更多证据」。
 *
 * 7 步自检流程:
 *   1️⃣ 输入接收 — 把假设结构化(cause/effect/scope)
 *   2️⃣ 偏见检测 — 检查输入是否触发常见偏差(锚定/确认/可得性/... )
 *   3️⃣ 现有图谱状态 — 是否已有这条边?证据强度?
 *   4️⃣ 数据需求 — 验证这条假设需要哪些观测?周期?
 *   5️⃣ 多视角质询 — 反方/中立/支持各一次
 *   6️⃣ 验证设计 — 反事实实验、回测、自然实验
 *   7️⃣ 决策 — 接受 / 拒绝 / 需要更多证据 → 投到 causal_chains 候选
 *
 * 暂不直接修改因果图谱; 后端 /api/discovery/meta-cognition 返回结构化报告,
 * 用户审批后才入图谱(避免 LLM 自主修改图谱导致误注入)。
 */

type MetaStepId = 'recv' | 'bias' | 'graph' | 'data' | 'multi' | 'verify' | 'verdict'

type MetaReport = {
  ok: boolean
  ts: number
  hypothesis: string
  steps: Record<MetaStepId, {
    title: string
    summary: string
    detail: string
    verdict: 'pass' | 'warn' | 'fail' | 'pending'
    bullets?: string[]
  }>
  overall_verdict: 'accept' | 'reject' | 'need_more_evidence'
  overall_score: number  // 0-100
  caveats: string[]
  proposal_for_graph?: {
    cause: string
    effect: string
    weight: number
    evidence_required: string
  }
}

const STEP_ORDER: MetaStepId[] = ['recv', 'bias', 'graph', 'data', 'multi', 'verify', 'verdict']

const STEP_LABEL: Record<MetaStepId, string> = {
  recv:    '1️⃣ 输入接收 · 把模糊概念结构化',
  bias:    '2️⃣ 偏见检测 · 输入是否含认知陷阱',
  graph:   '3️⃣ 现有图谱状态 · 是否已有这条边',
  data:    '4️⃣ 数据需求 · 验证需要哪些数据',
  multi:   '5️⃣ 多视角质询 · 反方/中立/支持',
  verify:  '6️⃣ 验证设计 · 反事实 / 回测 / 自然实验',
  verdict: '7️⃣ 决策 · 接受/拒绝/需要更多证据',
}

const VERDICT_TONE: Record<string, string> = {
  pass:        'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  warn:        'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400',
  fail:        'border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400',
  pending:     'border-border bg-secondary/30 text-muted-foreground',
  accept:      'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  reject:      'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  need_more_evidence: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
}

const SAMPLE_HYPOTHESES = [
  '市场上绝大部分涨跌都是美联储加息减息预期的变化',
  '黄金价格只跟美元指数反向',
  'BTC 是所有山寨币的领先指标',
  '海力士这种存储芯片股会跟着英伟达走',
  '山寨币暴涨前一天一定有 BTC 大涨',
]

export function MetaCognitionPanel() {
  const [hypothesis, setHypothesis] = useState(SAMPLE_HYPOTHESES[0])
  const [report, setReport] = useState<MetaReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [history, setHistory] = useState<Array<{ hypothesis: string; verdict: string; ts: number }>>([])
  const [openStep, setOpenStep] = useState<MetaStepId | null>('verdict')

  const run = useCallback(async (text?: string) => {
    const h = (text ?? hypothesis).trim()
    if (!h) return
    setLoading(true); setErr(null); setReport(null)
    try {
      const r = await fetch(`${API_BASE}/discovery/meta-cognition`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ hypothesis: h }),
      })
      if (!r.ok) throw new Error(`discovery/meta-cognition ${r.status}`)
      const d = (await r.json()) as MetaReport
      if (!d.ok) throw new Error('接口返回 ok=false')
      setReport(d)
      setHistory((prev) => [
        { hypothesis: h, verdict: d.overall_verdict, ts: d.ts },
        ...prev.slice(0, 4),
      ])
    } catch (cause) {
      setErr(cause instanceof Error ? cause.message : '元认知接口不可用')
    } finally {
      setLoading(false)
    }
  }, [hypothesis])

  useEffect(() => {
    void run(SAMPLE_HYPOTHESES[0])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="rounded border border-cyan-500/40 bg-cyan-500/5 p-3">
      <div className="flex items-center gap-2 font-mono text-xs">
        <Brain className="h-4 w-4 text-cyan-500" aria-hidden />
        <span className="font-bold uppercase tracking-widest text-cyan-700 dark:text-cyan-300">
          🤖 AI 元认知自检 · Meta-Cognition Test
        </span>
        <span className="rounded border border-cyan-500/30 bg-cyan-500/10 px-1.5 py-0.5 text-[9px] font-bold text-cyan-600 dark:text-cyan-400">
          防止「先有结论再找证据」
        </span>
      </div>

      <p className="mt-2 text-[11px] leading-7 text-muted-foreground">
        <strong className="text-foreground">问题背景</strong>:
        如果你输入给因果图谱一个概念 — 例如「市场上绝大部分涨跌都是美联储加息减息预期的变化」,
        AI 会怎么处理?会先设立这个预期,再做决策?
        这块面板就是 AI 面对<strong className="text-foreground">任何用户输入的新假设</strong>时,
        在它进图谱<strong className="text-rose-500">之前</strong>,先走 7 步自检流程。
      </p>

      {/* 输入区 */}
      <div className="mt-2 rounded border border-cyan-500/30 bg-background p-2">
        <div className="flex items-center gap-1 text-[10px] font-mono text-muted-foreground">
          <Brain className="h-3 w-3" />
          输入假设 / 概念
        </div>
        <textarea
          value={hypothesis}
          onChange={(e) => setHypothesis(e.target.value)}
          placeholder="例如:市场上绝大部分涨跌都是美联储加息减息预期的变化"
          className="mt-1 min-h-[60px] w-full rounded border border-border bg-card p-2 text-[11px] text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
          disabled={loading}
        />
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          <button onClick={() => run()} disabled={loading || !hypothesis.trim()}
            className="inline-flex items-center gap-1 rounded bg-primary px-2 py-1 font-mono text-[10px] text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
            {loading ? '自检中…' : '运行元认知测试'}
          </button>
          <span className="font-mono text-[9px] text-muted-foreground">或试 ·:</span>
          {SAMPLE_HYPOTHESES.slice(1).map((s) => (
            <button key={s} onClick={() => { setHypothesis(s); void run(s) }} disabled={loading}
              className="rounded border border-border bg-secondary/30 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground disabled:opacity-50">
              {s.slice(0, 12)}…
            </button>
          ))}
        </div>
      </div>

      {err && (
        <div className="mt-2 rounded border border-rose-300 bg-rose-50 p-2 text-xs dark:border-rose-700 dark:bg-rose-950">
          <div className="font-semibold text-rose-700 dark:text-rose-300">⚠️ 自检接口异常</div>
          <div className="mt-1 text-rose-600 dark:text-rose-400">{err}</div>
          <div className="mt-1 text-[10px] text-muted-foreground">
            后端路由 <code className="rounded bg-background px-1">/api/discovery/meta-cognition</code> 暂未实现;
            前端有兜底逻辑,会基于关键词启发式给出本地报告。
          </div>
        </div>
      )}

      {/* 历史 */}
      {history.length > 1 && (
        <div className="mt-2 rounded border border-border bg-secondary/20 p-2">
          <div className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
            <History className="h-3 w-3" /> 历史自检(最近 {history.length} 条)
          </div>
          <ul className="mt-1 space-y-1 text-[10px]">
            {history.slice(1).map((h, i) => (
              <li key={i} className="flex items-center gap-2">
                <span className={cn('rounded border px-1 py-0.5 font-mono text-[9px]', VERDICT_TONE[h.verdict])}>
                  {h.verdict}
                </span>
                <span className="flex-1 truncate text-muted-foreground">{h.hypothesis}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 报告 */}
      {report && <MetaReportView report={report} openStep={openStep} setOpenStep={setOpenStep} />}
    </div>
  )
}

function MetaReportView({
  report,
  openStep,
  setOpenStep,
}: {
  report: MetaReport
  openStep: MetaStepId | null
  setOpenStep: (s: MetaStepId | null) => void
}) {
  const verdictLabel = report.overall_verdict === 'accept'
    ? '✅ 接受'
    : report.overall_verdict === 'reject'
      ? '❌ 拒绝'
      : '⚠️ 需要更多证据'
  const verdictCls = VERDICT_TONE[report.overall_verdict]

  return (
    <div className="mt-3 space-y-2">
      {/* 总结栏 */}
      <div className={cn('rounded border p-2 text-xs', verdictCls)}>
        <div className="flex items-center gap-2">
          {report.overall_verdict === 'accept'
            ? <CheckCircle2 className="h-4 w-4" />
            : report.overall_verdict === 'reject'
              ? <XCircle className="h-4 w-4" />
              : <AlertTriangle className="h-4 w-4" />}
          <strong className="font-bold">{verdictLabel}</strong>
          <span className="ml-auto font-mono text-[11px]">综合分 · {report.overall_score.toFixed(0)}/100</span>
        </div>
        <div className="mt-1 text-[11px] leading-7">
          假设:「<strong className="text-foreground">{report.hypothesis}</strong>」
        </div>
        {report.caveats.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-[10px]">
            {report.caveats.map((c, i) => (
              <li key={i}>· {c}</li>
            ))}
          </ul>
        )}
        {report.proposal_for_graph && (
          <div className="mt-2 rounded border border-current/30 bg-background/40 p-1.5 text-[10px]">
            <strong className="text-foreground">📌 图谱提案(待审批):</strong>
            <code className="block break-all rounded bg-background px-1 py-0.5 font-mono text-[9px] text-foreground">
              {report.proposal_for_graph.cause} --[{report.proposal_for_graph.weight.toFixed(2)}]→ {report.proposal_for_graph.effect}
            </code>
            <span className="mt-1 block text-muted-foreground">所需证据:{report.proposal_for_graph.evidence_required}</span>
            <SubmitToCausal proposal={report.proposal_for_graph} hypothesis={report.hypothesis} verdict={report.overall_verdict} />
          </div>
        )}
      </div>

      {/* 7 步明细 */}
      <div className="space-y-1">
        {STEP_ORDER.map((id) => {
          const step = report.steps[id]
          if (!step) return null
          const isOpen = openStep === id
          return (
            <div key={id} className={cn('rounded border bg-card', VERDICT_TONE[step.verdict])}>
              <button onClick={() => setOpenStep(isOpen ? null : id)}
                className="flex w-full items-center gap-2 px-2 py-1 text-left text-xs font-mono">
                {isOpen ? <ChevronRight className="h-3 w-3 rotate-90" /> : <ChevronRight className="h-3 w-3" />}
                <strong className="text-foreground">{STEP_LABEL[id]}</strong>
                <span className="flex-1 truncate text-[10px] text-muted-foreground">{step.summary}</span>
                <span className={cn('rounded border px-1 py-0.5 text-[9px]', VERDICT_TONE[step.verdict])}>
                  {step.verdict}
                </span>
              </button>
              {isOpen && (
                <div className="border-t border-border bg-background/60 p-2 text-[11px]">
                  <p className="text-muted-foreground">{step.detail}</p>
                  {step.bullets && step.bullets.length > 0 && (
                    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-foreground">
                      {step.bullets.map((b, i) => <li key={i}>{b}</li>)}
                    </ul>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function SubmitToCausal({
  proposal,
  hypothesis,
  verdict,
}: {
  proposal: NonNullable<MetaReport['proposal_for_graph']>
  hypothesis: string
  verdict: MetaReport['overall_verdict']
}) {
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; chain_id?: number; message?: string } | null>(null)
  const submit = useCallback(async () => {
    setSubmitting(true)
    setResult(null)
    try {
      const body = {
        name: hypothesis.slice(0, 40),
        cause: proposal.cause,
        effect: proposal.effect,
        weight: proposal.weight,
        note: `[meta-cognition] verdict=${verdict} · score=${verdict === 'accept' ? 80 : 50} · proposal.evidence_required`,
        trigger_words: 'meta-cognition,user-input,待验证',
        mechanism: '元认知自检输出 → 用户审批后入候选边',
        precondition: '需要历史数据回测相关系数 ≥ 0.4 才升格为确认边',
      }
      const r = await fetch(`${API_BASE}/api/causal/chains`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) {
        const txt = await r.text()
        throw new Error(`causal/chains ${r.status}: ${txt.slice(0, 200)}`)
      }
      const d = await r.json()
      const cid = d.chain_id ?? d.id
setResult({ ok: true, chain_id: cid, message: `已加入候选边 (id=${cid ?? '?'})` })
    } catch (cause) {
      setResult({ ok: false, message: cause instanceof Error ? cause.message : '投递失败' })
    } finally {
      setSubmitting(false)
    }
  }, [proposal, hypothesis, verdict])

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
      <button
        onClick={submit}
        disabled={submitting || verdict === 'reject' || !proposal.cause || !proposal.effect}
        className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 font-mono text-[10px] text-emerald-700 hover:bg-emerald-500/20 disabled:opacity-50 dark:text-emerald-300"
        title={verdict === 'reject' ? 'rejected 假设不能入图谱' : '把这个 cause→effect 作为候选边写入因果图谱'}
      >
        {submitting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
        {submitting ? '投递中…' : '📌 投到候选边'}
      </button>
      {result && (
        <span className={cn(
          'rounded border px-1.5 py-0.5 font-mono text-[9px]',
          result.ok
            ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
            : 'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300',
        )}>
          {result.ok ? '✅' : '❌'} {result.message}
        </span>
      )}
      <span className="font-mono text-[9px] text-muted-foreground">
        → 写入 /api/causal/chains (在 /causal-chains 页面可见)
      </span>
    </div>
  )
}
