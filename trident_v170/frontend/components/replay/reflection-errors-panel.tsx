'use client'

import { useEffect, useState, useCallback } from 'react'
import { Loader2, RefreshCcw, Brain, Filter, AlertTriangle, NotepadText, Sparkles, ChevronDown, ChevronRight } from 'lucide-react'
import { apiGet, apiSend } from '@/lib/api'
import { cn } from '@/lib/utils'
import { ERROR_CODE_NAME } from '@/lib/constants'
import { EvidenceBadges } from './evidence-badges'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'
import {
  parseReflectionRecent,
  parseReflectionStats,
  type DecisionError as DecisionErrorT,
  type ReflectionRecent as ReflectionRecentT,
  type ReflectionStats as ReflectionStatsT,
} from '@/lib/schemas/reflection'
import {
  useReflectionDrafts,
  draftKey as makeDraftKey,
  type ReflectionDraft,
} from './reflection-errors/use-reflection-drafts'

// 决策错误码面板 — 从 decision_errors 读最近反思错误条目
// 来源：程序化校验器(checker) / LLM 盲区反思(llm)
// severity: 1=INFO / 2=WARN / 3=CRIT — 字段保留供诊断用，UI 不再视觉分层
// P0 起所有错误一律平铺显示，不分级别
//
// v275+ 拆分:
//   - 类型(DecisionError / ReflectionRecent / ReflectionStats) → @/lib/schemas/reflection
//   - LS 读写 + ReflectionDraft 类型 + 状态机 → ./reflection-errors/*
//   - 本文件只剩数据加载 + UI 渲染

// P0：移除 SEVERITY_LABEL 视觉分层常量 — 所有错误一律同等显示

// Re-export types 方便面板内调用(useApiPoll 这种"读后 parse"的 hook 会用到)
type DecisionError = DecisionErrorT
type ReflectionRecent = ReflectionRecentT
type ReflectionStats = ReflectionStatsT

const ORIGIN_LABEL: Record<string, { label: string; color: string }> = {
  checker: { label: '程序化校验', color: 'text-primary border-primary/40 bg-primary/10' },
  llm: { label: 'LLM 盲区', color: 'text-chart-3 border-chart-3/40 bg-chart-3/10' },
}

// v302+ P1 重构: ERROR_CODE_NAME 已抽取到 @/lib/constants (共享常量)

// P0：SeverityBadge 已移除 — 错误一律平铺显示，不分级别
// severity 数值仍保留在 DecisionError.severity 字段，供后端诊断使用

function OriginBadge({ origin }: { origin: string }) {
  const o = ORIGIN_LABEL[origin] || { label: origin, color: 'text-muted-foreground border-border bg-secondary/30' }
  return (
    <span className={cn('inline-flex items-center rounded border px-1 py-0.5 font-mono text-[9px]', o.color)}>
      {o.label}
    </span>
  )
}

// v275+ 反思卡 — 自由文本 + AI 追问方向 + 核心原因勾选 + 改进项分离
// 设计原则：一事一议 / 不强制层数 / 不强制追问 / 不强制核心原因标记 / 不强制改进项
function ReflectionCard(props: {
  errorKey: string
  decisionId: number
  errorCode: string
  draft: ReflectionDraft
  crossAssetSnapshot?: ReflectionRecent['cross_asset_snapshot']
  promptPreview?: string
  onUpdate: (patch: Partial<ReflectionDraft>) => void
  onSave: () => void
  onClose: () => void
  onSuggestFollowups: () => void
  onInsertQuestion: (q: string) => void
}) {
  const {draft, crossAssetSnapshot, promptPreview} = props
  return (
    <div className="mt-2 space-y-2 border-t border-border pt-2">
      {/* 当时实际上下文（只读) — 不让用户从零开始假设 */}
      <div className="rounded border border-border bg-background/40 p-1.5 text-[10px]">
        <div className="mb-0.5 text-muted-foreground">📌 当时实际（只读）：</div>
        {crossAssetSnapshot?.macro_regime && (
          <div>
            跨资产状态:
            <span className="text-foreground">
              {crossAssetSnapshot.macro_regime === 'RISK_OFF_GLOBAL' ? '🔴 系统性收缩'
                : crossAssetSnapshot.macro_regime === 'RISK_ON_GLOBAL' ? '🟢 风险偏好回升'
                : crossAssetSnapshot.macro_regime === 'FLIGHT_TO_SAFETY' ? '🟡 避险模式'
                : crossAssetSnapshot.macro_regime === 'MIXED' ? '⚪ 板块分化'
                : '⚪ 数据不足'}
            </span>
          </div>
        )}
        <div className="mt-0.5 text-muted-foreground">
          prompt 注入预览 {promptPreview
            ? `前 ${promptPreview.length} 字符可展开上方「▸ Prompt 注入预览」查看`
            : '(无)'}
        </div>
      </div>

      {/* 反思正文 — 完全自由，无段数 / 无字数 / 无模板 */}
      <div>
        <label className="mb-0.5 block text-muted-foreground">
          🤔 我的反思（自由文本 — 几句都行，几百字也行，不强制格式）
        </label>
        <textarea
          value={draft.text}
          onChange={(ev) => props.onUpdate({text: ev.target.value})}
          rows={4}
          placeholder={'例：我当时把『美联储放鹰』当成短期利空，但忽略了 30 天趋势;\n下次应该看 30d 资金流向而不是当日标题。\n\n—— 没有的话就空着，不强制。'}
          className="w-full rounded border border-border bg-background px-1.5 py-1 font-mono text-[10px] text-foreground resize-y"
        />
      </div>

      {/* AI 追问方向（可选，不强制) */}
      <div>
        <button
          type="button"
          onClick={props.onSuggestFollowups}
          disabled={draft.loadingFollowups}
          className="inline-flex items-center gap-1 rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-40"
          title="让 LLM 给 2-3 个不同方向的追问（覆盖 假设 / 事件 / 数据 / 推理链 至少 2 维度) — 点击建议问题可自动填到上方反思区"
        >
          {draft.loadingFollowups
            ? <Loader2 className="h-3 w-3 animate-spin" />
            : <Sparkles className="h-3 w-3 text-primary" />}
          {draft.loadingFollowups ? 'AI 想追问方向中…' : '💡 让 AI 给追问方向（可选）'}
        </button>
        {draft.lastError && (
          <div className="mt-0.5 text-[10px] text-alert">追问接口失败：{draft.lastError}</div>
        )}
        {draft.aiQuestions.length > 0 && (
          <ul className="mt-1 space-y-1">
            {draft.aiQuestions.map((q, idx) => (
              <li key={idx}>
                <button
                  type="button"
                  onClick={() => props.onInsertQuestion(q)}
                  className="w-full rounded border border-border bg-background/40 px-1.5 py-1 text-left font-mono text-[10px] text-foreground hover:bg-secondary/40"
                  title="点击把这句追问填到上方反思区（仅作为引导问句，你后续写答案)"
                >
                  <span className="text-primary">{idx + 1}.</span> {q}
                  <span className="ml-2 text-muted-foreground">[点击 → 填入]</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 核心原因标记（可选) */}
      <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
        <input
          type="checkbox"
          checked={draft.coreMarked}
          onChange={(ev) => props.onUpdate({coreMarked: ev.target.checked})}
          className="h-3 w-3"
        />
        ✓ 我觉得已经写到核心原因了
        <span className="text-muted-foreground">（不勾也行 — 觉得还不够就继续追问）</span>
      </label>

      {/* 改进项 — 与反思分离，指向可执行环节 */}
      <div>
        <label className="mb-0.5 block text-muted-foreground">
          🛠 改进项（指向具体可执行环节 — 不要写「应该更小心」这种空话）
        </label>
        <textarea
          value={draft.improvementText}
          onChange={(ev) => props.onUpdate({improvementText: ev.target.value})}
          rows={2}
          placeholder="例:① 改 prompt 在『8 段』前强制列举 30d 趋势  ② prompt §跨资产 加『至少 2 个反证』  ③ 把 E001e 校验放进 gating 前置"
          className="w-full rounded border border-border bg-background px-1.5 py-1 font-mono text-[10px] text-foreground resize-y"
        />
      </div>

      {/* 保存 / 关闭 */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={props.onSave}
          className="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-primary hover:bg-primary/20"
        >
          💾 保存到本地
        </button>
        <button
          type="button"
          onClick={props.onClose}
          className="inline-flex items-center gap-1 rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
        >
          ✕ 收起
        </button>
        {draft.savedAt && (
          <span className="text-[10px] text-muted-foreground">
            上次保存 {new Date(draft.savedAt).toLocaleString('zh-CN', {hour12: false})}
          </span>
        )}
        <span className="ml-auto text-[9px] text-muted-foreground">
          深度由你判断 — 一事一议，不强制层数
        </span>
      </div>
    </div>
  )
}

function str(v: any): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try { return JSON.stringify(v) } catch { return String(v) }
}

export function ReflectionErrorsPanel({
  // v200 IA：允许从外部（如 FailureStatsPanel 点击排行行）传入初始过滤码
  // 使用 key+value 比较避免重复 set。传 null/undefined 表示不清除（保持用户当前选择）。
  initialCode,
}: { initialCode?: string | null } = {}) {
  const [recent, setRecent] = useState<ReflectionRecent | null>(null)
  const [stats, setStats] = useState<ReflectionStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [runMsg, setRunMsg] = useState<string | null>(null)
  const [filterCode, setFilterCode] = useState<string>(initialCode ?? '')
  const [filterOrigin, setFilterOrigin] = useState<string>('')
  const [limit, setLimit] = useState<number>(30)

  // v275+ 自由反思卡状态机 — 整段下沉到 ./reflection-errors/use-reflection-drafts
  // (本组件只剩数据加载 + UI 渲染)
  const {
    reflections,
    openReflection,
    closeReflection,
    updateDraft,
    saveReflection,
    requestFollowups,
    insertQuestion,
  } = useReflectionDrafts()

  /** 与 useReflectionDrafts 的 draftKey 同名导出,保持 JSX 旧调用 `draftKey(...)` 不破。 */
  const draftKey = makeDraftKey

  // v200 IA：外部点击分类 → 同步下方过滤码（仅在显式传值时更新，不清空用户选择）
  useEffect(() => {
    if (initialCode !== undefined && initialCode !== null && initialCode !== filterCode) {
      setFilterCode(initialCode)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialCode])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ limit: String(limit) })
      if (filterCode) params.set('code', filterCode)
      if (filterOrigin) params.set('origin', filterOrigin)
      const [r1Raw, r2Raw] = await Promise.all([
        apiGet<unknown>(`/reflection/recent?${params}`),
        apiGet<unknown>(`/reflection/stats?days=30`),
      ])
      // v275+: zod safeParse —— schema mismatch 不再 throw,而是写 error + 保留旧数据
      const r1 = parseReflectionRecent(r1Raw)
      if (r1.ok === false) {
        console.warn('[reflection/recent] schema mismatch:', r1.error)
        setError(`response shape mismatch: ${r1.error}`)
        return
      }
      const r2 = parseReflectionStats(r2Raw)
      if (r2.ok === false) {
        console.warn('[reflection/stats] schema mismatch:', r2.error)
        setError(`stats shape mismatch: ${r2.error}`)
        return
      }
      setRecent(r1.data)
      setStats(r2.data)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [limit, filterCode, filterOrigin])

  // v302 P0+：手写 setInterval + alive flag → usePollRefresh（统一 mounted 保护 + cleanup）
  // 原版是 /review 里唯一显式 alive 标志的反射错误面板 —— 其它面板都没有这个保护，
  // unmount 期间 in-flight fetch 仍会触发 setState（React 18 默默吞掉，但有 warning）。
  usePollRefresh(load, 60_000, [load])

  const handleRun = async () => {
    setRunning(true); setRunMsg(null)
    try {
      const data = await apiSend<{
        mode?: string
        checker_inserted?: number
        checker_codes?: string[]
        summary?: { checker_inserted?: number; checker_codes?: string[] }
      }>('/reflection/run', 'POST', { limit: 10, scope: 'recent', skip_llm: true })
      const inserted = data.checker_inserted ?? data.summary?.checker_inserted ?? 0
      const codes = (data.checker_codes ?? data.summary?.checker_codes ?? []).join(', ')
      setRunMsg(`✓ 已处理 ${data.mode ?? 'batch'}: 写入 ${inserted} 个错误码 ${codes ? `(${codes})` : ''}`)
      void load()
    } catch (e) {
      setRunMsg(`✗ 失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRunning(false)
    }
  }

  if (loading && !recent) {
    return (
      // v200 IA：加载态也加 id，让 FailureStatsPanel drill-down 滚到正确位置
      <section id="reflection-errors" className="rounded-lg border border-border bg-card p-3 scroll-mt-3">
        <div className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 加载决策错误码…
        </div>
      </section>
    )
  }

  if (error) {
    return (
      <section id="reflection-errors" className="rounded-lg border border-short/40 bg-short/10 p-3 font-mono text-[11px] text-short scroll-mt-3">
        加载失败：{error}
      </section>
    )
  }

  if (!recent) return null

  const totalErrors = stats?.total ?? recent.total
  const codeEntries = Object.entries(stats?.by_code ?? recent.code_stats).sort((a, b) => b[1] - a[1])
  const maxCodeCount = codeEntries.length > 0 ? codeEntries[0][1] : 0

  return (
    // v200 IA：加 id 供 FailureStatsPanel drill-down 时滚到此
    <section id="reflection-errors" className="rounded-lg border border-border bg-card p-3 scroll-mt-3">
      <header className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <Brain className="h-4 w-4 text-primary" />
          <h2 className="font-mono text-sm font-bold uppercase tracking-widest">决策错误码</h2>
          <span className="rounded border border-chart-3/40 bg-chart-3/10 px-2 py-0.5 font-mono text-[10px] text-chart-3">REFLECTION</span>
        </div>
        <span className="font-mono text-[10px] text-muted-foreground">
          最近 {recent.total} 条 · 30 天 {totalErrors} 条
        </span>
        {/* v180 P0+：数据通道统一口径 §6 — 标明源表 + 时区 */}
        <span className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[9px] text-primary">
          数据 = [真实] reflection_recent + reflection_stats · 北京时区
        </span>
        <button type="button" onClick={() => void load()} disabled={loading}
          className="ml-auto inline-flex items-center gap-1 rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-40">
          <RefreshCcw className={cn('h-3 w-3', loading && 'animate-spin')} /> 刷新
        </button>
        <button type="button" onClick={handleRun} disabled={running}
          className="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/10 px-2 py-1 font-mono text-[10px] text-primary hover:bg-primary/20 disabled:opacity-40">
          {running ? <Loader2 className="h-3 w-3 animate-spin" /> : <Filter className="h-3 w-3" />}
          {running ? '运行中…' : '跑一次'}
        </button>
      </header>

      {runMsg && (
        <div className={cn(
          'mt-2 rounded border p-1.5 font-mono text-[10px]',
          runMsg.startsWith('✓') ? 'border-long/30 bg-long/10 text-long' : 'border-short/30 bg-short/10 text-short',
        )}>
          {runMsg}
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2 font-mono text-[10px]">
        <label className="flex items-center gap-1 text-muted-foreground">
          错误码:
          <select value={filterCode} onChange={e => setFilterCode(e.target.value)}
            className="rounded border border-border bg-background px-1 py-0.5 text-[10px]">
            <option value="">全部</option>
            {Object.keys(ERROR_CODE_NAME).map(c => (
              <option key={c} value={c}>{c} {ERROR_CODE_NAME[c]}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-muted-foreground">
          来源:
          <select value={filterOrigin} onChange={e => setFilterOrigin(e.target.value)}
            className="rounded border border-border bg-background px-1 py-0.5 text-[10px]">
            <option value="">全部</option>
            <option value="checker">checker</option>
            <option value="llm">llm</option>
          </select>
        </label>
        <label className="flex items-center gap-1 text-muted-foreground">
          条数:
          <select value={limit} onChange={e => setLimit(Number(e.target.value))}
            className="rounded border border-border bg-background px-1 py-0.5 text-[10px]">
            <option value={20}>20</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={300}>300</option>
            <option value={500}>500</option>
            <option value={1500}>1500（v225 不限）</option>
          </select>
        </label>
        <span className="ml-auto text-muted-foreground">匹配 {recent.total} 条</span>
      </div>

      {codeEntries.length > 0 && (
        <div className="mt-2 rounded-md border border-border bg-background/40 p-2 font-mono">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">错误码分布（30 天）</div>
          <div className="space-y-1">
            {codeEntries.map(([code, cnt]) => (
              <div key={code} className="flex items-center gap-2 text-[10px]">
                <span className="w-32 shrink-0 truncate text-muted-foreground">
                  {code} {ERROR_CODE_NAME[code] ?? ''}
                </span>
                <div className="h-2.5 flex-1 overflow-hidden rounded bg-secondary">
                  <div className="h-full rounded bg-short/60"
                    style={{ width: `${Math.max((cnt / maxCodeCount) * 100, 2)}%` }} />
                </div>
                <span className="w-12 shrink-0 text-right text-foreground">{cnt} 笔</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* v232.5 P0+: 错误码 → 累计 pnl 影响。
          "这些错误码累计让你亏了多少期望值"。用户反馈 2026-10-06:
          高胜率盈亏比 0.30 — 现在每个错误码都被钉上具体 pnl。
          按 total_pnl ASC(最负向优先）排序，让你一眼看到亏钱元凶。 */}
      {Array.isArray(recent.code_pnl_impact) && recent.code_pnl_impact.length > 0 && (() => {
        const impacts = [...recent.code_pnl_impact].sort(
          (a, b) => (a.total_pnl ?? 0) - (b.total_pnl ?? 0),
        )
        const totalAllPnl = impacts.reduce(
          (s, i) => s + (Number(i.total_pnl) || 0), 0,
        )
        const worst = impacts[0]
        const worstPct = worst && worst.total_pnl != null ? (Number(worst.total_pnl) * 100).toFixed(2) : '—'
        return (
          <div className="mt-2 rounded-md border border-short/40 bg-short/5 p-2 font-mono">
            <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-short">
              <AlertTriangle className="h-3 w-3" /> 错误码 → 累计 pnl 影响（v232.5 P0+）
            </div>
            <p className="mb-1 text-[9px] text-muted-foreground">
              累计影响 {totalAllPnl >= 0 ? '+' : ''}{(totalAllPnl * 100).toFixed(2)}%。
              最亏元凶 <span className="font-bold text-short">{worst?.code ?? '—'}</span>，
              累计 {(worst?.total_pnl ?? 0) >= 0 ? '+' : ''}{worstPct}% （{worst?.n_settled ?? 0} 笔已结算），
              打中这个码时胜率 {worst?.win_rate_when_coded != null
                ? `${(worst.win_rate_when_coded * 100).toFixed(1)}%`
                : '—'}
            </p>
            <div className="space-y-0.5">
              {impacts.slice(0, 8).map((i) => {
                const tp = Number(i.total_pnl) || 0
                const ap = Number(i.avg_pnl)
                const wr = Number(i.win_rate_when_coded)
                const tone = tp < 0 ? 'text-short' : tp > 0 ? 'text-long' : 'text-muted-foreground'
                return (
                  <div key={i.code} className="flex items-center gap-2 text-[10px]">
                    <span className="w-16 shrink-0 text-muted-foreground">{i.code}</span>
                    <span className="w-28 shrink-0 truncate text-foreground">
                      {ERROR_CODE_NAME[i.code] ?? ''}
                    </span>
                    <span className={`w-16 shrink-0 text-right tabular-nums ${tone}`}>
                      {(tp * 100).toFixed(2)}%
                    </span>
                    <span className="w-12 shrink-0 text-right tabular-nums text-muted-foreground">
                      {Number.isFinite(ap) ? `${(ap * 100).toFixed(2)}%` : '—'}
                    </span>
                    <span className="ml-auto w-12 shrink-0 text-right text-muted-foreground">
                      {i.n_settled ?? 0}笔/{i.n_errors ?? 0}
                    </span>
                    <span className="w-12 shrink-0 text-right text-muted-foreground">
                      {Number.isFinite(wr) ? `${(wr * 100).toFixed(0)}%` : '—'}
                    </span>
                  </div>
                )
              })}
            </div>
            <p className="mt-1 text-[9px] text-muted-foreground">
              排序：total_pnl 升序（最负向优先）。列：码 · 中文 · 累计 pnl · 平均 pnl · 已结算/总 · 命中率。
              数据 = [真实] decision_errors JOIN ai_decisions WHERE settled=1。
            </p>
          </div>
        )
      })()}

      {/* v225: 子原因精细化 — 把 E001 系列的真因拆开看，不再一个"数据缺失"打天下 */}
      {recent.code_subkinds && Object.keys(recent.code_subkinds).length > 0 && (
        <div className="mt-2 rounded-md border border-primary/30 bg-primary/5 p-2 font-mono">
          <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-primary">
            <Brain className="h-3 w-3" /> 子原因精细化（v225）— 让每个 root cause 独立可见
          </div>
          <div className="space-y-0.5 text-[10px]">
            {Object.entries(recent.code_subkinds)
              .sort((a, b) => b[1] - a[1])
              .map(([k, n]) => {
                const [code, sk] = k.split('/')
                return (
                  <div key={k} className="flex items-center gap-2">
                    <span className="w-16 shrink-0 text-muted-foreground">{code}</span>
                    <span className="flex-1 truncate text-foreground">{sk}</span>
                    <span className="w-12 shrink-0 text-right text-primary">{n} 笔</span>
                  </div>
                )
              })}
          </div>
          <p className="mt-1 text-[9px] text-muted-foreground">
            反思系统不再一个错误码否定全部 — 每一笔决策的真因独立列出。例：E001a/b/c/d/e/f 各代表不同维度的"缺什么数据"。
          </p>
        </div>
      )}

      {/* v225: 跨资产 K 线快照 — 反思时不只看当前品种，看全市场形态 */}
      {recent.cross_asset_snapshot && !recent.cross_asset_snapshot.error && (recent.cross_asset_snapshot.total_symbols ?? 0) > 0 && (
        <div className={cn(
          'mt-2 rounded-md border p-2 font-mono',
          recent.cross_asset_snapshot.macro_regime === 'RISK_OFF_GLOBAL'
            ? 'border-short/40 bg-short/10'
            : recent.cross_asset_snapshot.macro_regime === 'RISK_ON_GLOBAL'
              ? 'border-long/40 bg-long/10'
              : 'border-border bg-background/40',
        )}>
          <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider">
            <span className="text-muted-foreground">🌐 跨资产快照（v225 反思系统扩）</span>
            <span className="text-foreground">{
              recent.cross_asset_snapshot.macro_regime === 'RISK_OFF_GLOBAL' ? '🔴 系统性风险偏好收缩'
              : recent.cross_asset_snapshot.macro_regime === 'RISK_ON_GLOBAL' ? '🟢 系统性风险偏好回升'
              : recent.cross_asset_snapshot.macro_regime === 'FLIGHT_TO_SAFETY' ? '🟡 避险模式'
              : recent.cross_asset_snapshot.macro_regime === 'MIXED' ? '⚪ 板块分化'
              : '⚪ 数据不足'
            }</span>
          </div>
          {recent.cross_asset_snapshot.warning && (
            <div className="mb-1 rounded border border-short/30 bg-short/5 px-1.5 py-1 text-[10px] text-short">
              {recent.cross_asset_snapshot.warning}
            </div>
          )}
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px] md:grid-cols-4">
            {Object.entries(recent.cross_asset_snapshot.pct_only || {})
              .sort((a, b) => Math.abs(b[1] as number) - Math.abs(a[1] as number))
              .map(([sym, pct]) => {
                const p = pct as number
                return (
                  <div key={sym} className="flex items-center gap-1">
                    <span className={
                      p > 1 ? 'text-long'
                      : p < -1 ? 'text-short'
                      : 'text-muted-foreground'
                    }>
                      {p > 0 ? '▲' : p < 0 ? '▼' : '•'} {sym}
                    </span>
                    <span className="ml-auto font-mono text-foreground">{p > 0 ? '+' : ''}{p.toFixed(2)}%</span>
                  </div>
                )
              })}
          </div>
          <p className="mt-1 text-[9px] text-muted-foreground">
            数据 = external_indicators (Yahoo Finance) + factor_snapshots · 24h 窗口 · 仅反思用，不影响交易信号
          </p>
        </div>
      )}

      {recent.prompt_preview && (
        <details className="mt-2 rounded-md border border-border bg-background/40 p-2 font-mono text-[10px]">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            ▸ Prompt 注入预览（这正是 AI 决策时看到的内容）
          </summary>
          <pre className="mt-1 whitespace-pre-wrap text-foreground">{recent.prompt_preview}</pre>
        </details>
      )}

      {recent.errors.length === 0 ? (
        <div className="mt-2 rounded border border-border bg-background/40 p-2 font-mono text-[11px] text-muted-foreground">
          暂无错误码（先点击「跑一次」对已有决策触发程序化校验）
        </div>
      ) : (
        <div className="mt-2 space-y-1">
          {recent.errors.map(e => {
            const k = draftKey(e.decision_id, e.error_code)
            const draft = reflections[k]
            const isOpen = draft?.open
            const hasSaved = !!draft?.savedAt
            return (
              <div key={`${e.id ?? e.decision_id}-${e.error_code}-${e.created_at}`}
                className="rounded border border-border bg-card p-1.5 font-mono text-[10px]">
                <div className="flex items-start gap-2">
                  <div className="flex shrink-0 flex-col items-start gap-0.5">
                    <span className="font-bold text-foreground">{e.error_code}</span>
                    <OriginBadge origin={e.origin} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-foreground">{str(e.detail)}</div>
                    <EvidenceBadges refs={e.evidence_refs} />
                    <div className="mt-0.5 flex flex-wrap items-center gap-2 text-muted-foreground">
                      <span>decision_id={e.decision_id}</span>
                      {e.source && <span>· source={e.source}</span>}
                      <span>· {e.created_at}</span>
                      {hasSaved && (
                        <span className="rounded border border-long/30 bg-long/10 px-1 py-0.5 text-long">
                          ✓ 已写反思 {new Date(draft!.savedAt!).toLocaleString('zh-CN', {hour12: false})}
                        </span>
                      )}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => isOpen
                      ? closeReflection(e.decision_id, e.error_code)
                      : openReflection(e.decision_id, e.error_code)}
                    className={
                      'shrink-0 inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] '
                      + (isOpen
                        ? 'border-primary/40 bg-primary/10 text-primary'
                        : 'border-border bg-background/40 text-muted-foreground hover:text-foreground')
                    }
                    title={isOpen ? '收起反思卡' : '为这条错误写自由反思 — 一事一议，不强制层数'}
                  >
                    <NotepadText className="h-3 w-3" />
                    {isOpen ? '收起' : '📝 写反思'}
                  </button>
                </div>
                {/* v275+ 反思卡渲染处 — 见下方补齐 */}
                {isOpen && draft && (
                  <ReflectionCard
                    errorKey={k}
                    decisionId={e.decision_id}
                    errorCode={e.error_code}
                    draft={draft}
                    crossAssetSnapshot={recent.cross_asset_snapshot}
                    promptPreview={recent.prompt_preview}
                    onUpdate={(patch) => updateDraft(e.decision_id, e.error_code, patch)}
                    onSave={() => saveReflection(e.decision_id, e.error_code)}
                    onClose={() => closeReflection(e.decision_id, e.error_code)}
                    onSuggestFollowups={() => requestFollowups(e.decision_id, e.error_code)}
                    onInsertQuestion={(q) => insertQuestion(e.decision_id, e.error_code, q)}
                  />
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}