'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchLifecycle,
  fetchRecentAnalysisList,
  type APLifecycle,
  type APDecision,
  type APGate,
  type APLlmCallMeta,
  type APListItem,
} from '@/lib/analysis-process-data'
import { fmtTs, sourceLabel } from '@/lib/analysis-process-data'
import { NewsList } from './news-list'
import { MissedSignalsByDimBanner } from '@/components/v237/MissedSignalsByDimBanner'
import { useBinancePrices } from '@/hooks/use-binance-prices'
import { IngestGateSection } from './ingest-gate-section'
import { LlmReasoningSection } from './llm-reasoning-section'
import { TradeExecutionSection } from './trade-execution-section'
import { ReflectionSection } from './reflection-section'
import { ExperienceSection } from './experience-section'
import {
  JsonDump,
  KeyValueGrid,
  SectionCard,
  StepPlayer,
  Timeline,
  type PlayerStep,
} from './sections-shared'

// ─────── 后台运行时追踪类型 ───────
type TraceEntry = {
  id: number
  url: string
  method: string
  ts: number
  status: number
  latency_ms: number
  size_bytes: number
  response_shape: Record<string, ShapeInfo> | null
  err?: string
}
type ShapeInfo = {
  kind: 'object' | 'array' | 'string' | 'boolean' | 'null' | 'number'
  len?: number
  sample?: string
}

// 摘要一个 JSON 响应的 shape,只取 3 层,过滤掉 detail/error 这种纯文字字段
function summarizeShape(obj: unknown): Record<string, ShapeInfo> {
  if (typeof obj !== 'object' || obj === null) return {}
  const out: Record<string, ShapeInfo> = {}
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    out[k] = shapeOne(v)
  }
  return out
}
function shapeOne(v: unknown): ShapeInfo {
  if (v === null) return { kind: 'null' }
  if (Array.isArray(v)) {
    return { kind: 'array', len: v.length, sample: v[0] ? JSON.stringify(v[0]).slice(0, 80) : '' }
  }
  if (typeof v === 'object') {
    const sub = v as Record<string, unknown>
    return {
      kind: 'object',
      len: Object.keys(sub).length,
      sample: JSON.stringify(Object.keys(sub)).slice(0, 80),
    }
  }
  if (typeof v === 'string') return { kind: 'string', len: v.length, sample: v.slice(0, 80) }
  return { kind: typeof v as ShapeInfo['kind'] }
}

// ─────── 数据完整度诊断:本次 lifecycle 返回了哪些字段 / 哪些缺失 ───────
const LIFECYCLE_FIELD_PLAN: Array<{ key: keyof APLifecycle; label: string; table: string; critical: boolean }> = [
  { key: 'news',                    label: '新闻原文',         table: 'raw_news',            critical: true  },
  { key: 'gate',                    label: 'Gate 评级',        table: 'app_news_gate',       critical: true  },
  { key: 'decisions',               label: 'AI 决策列表',      table: 'ai_decisions',        critical: true  },
  { key: 'reasoning',               label: 'LLM 推理',         table: 'llm_reasoning',       critical: false },
  { key: 'llm_call_meta',           label: 'LLM 元数据',       table: 'llm_call_meta',       critical: false },
  { key: 'quick_sim',               label: '模拟持仓',         table: 'quick_sim_trades',    critical: false },
  { key: 'paper_run',               label: '纸面回测',         table: 'paper_trading_run',   critical: false },
  { key: 'trade_reflections',       label: '交易反思',         table: 'trade_reflections',   critical: false },
  { key: 'structured_reflections',  label: '结构化归因',       table: 'structured_reflections', critical: false },
  { key: 'attribution_reviews',     label: '人工复核',         table: 'attribution_reviews', critical: false },
  { key: 'news_trade_review',       label: '本新闻复盘缓存',   table: 'news_trade_review',   critical: false },
  { key: 'review_insights',         label: '洞察规则',         table: 'review_insights',     critical: false },
  { key: 'experience_entries',      label: '经验条目',         table: 'experience_entries',  critical: false },
  { key: 'causal_chain_links',      label: '因果链',           table: 'causal_chain_links',  critical: false },
]
function buildIntegrityReport(lc: APLifecycle | null): Array<{ key: string; label: string; table: string; critical: boolean; present: boolean; count: number }> {
  return LIFECYCLE_FIELD_PLAN.map(({ key, label, table, critical }) => {
    const v = lc ? (lc[key] as unknown) : undefined
    let present = false
    let count = 0
    if (Array.isArray(v)) {
      present = v.length > 0
      count = v.length
    } else if (v && typeof v === 'object') {
      present = Object.keys(v as object).length > 0
      count = Object.keys(v as object).length
    } else if (v !== null && v !== undefined && v !== '') {
      present = true
      count = 1
    }
    return { key: key as string, label, table, critical, present, count }
  })
}

// ─────── 后台运行时追踪 Drawer ───────
function RuntimeTraceDrawer({
  open,
  onClose,
  entries,
  lifecycle,
  onClear,
}: {
  open: boolean
  onClose: () => void
  entries: TraceEntry[]
  lifecycle: APLifecycle | null
  onClear: () => void
}) {
  if (!open) return null
  const totalLatency = entries.reduce((s, e) => s + e.latency_ms, 0)
  const totalSize = entries.reduce((s, e) => s + e.size_bytes, 0)
  const successCount = entries.filter((e) => e.status >= 200 && e.status < 400).length
  const integrity = buildIntegrityReport(lifecycle)
  const criticalMiss = integrity.filter((r) => r.critical && !r.present)
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative flex h-full w-[700px] max-w-[95vw] flex-col border-l border-border bg-card shadow-2xl">
        <div className="flex items-center justify-between border-b border-border bg-background/60 px-4 py-3">
          <div className="flex items-baseline gap-2">
            <span className="text-base">🛰️</span>
            <span className="font-mono text-sm font-bold uppercase tracking-wider text-primary">
              后台实际在跑什么
            </span>
            <span className="text-[11px] text-muted-foreground">
              · {entries.length} 次调用 · {Math.round(totalLatency)} ms ·{' '}
              {(totalSize / 1024).toFixed(1)} KB · 成功 {successCount}/{entries.length || 0}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClear}
              className="rounded border border-border bg-background px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
            >
              清空
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-border bg-background px-2 py-0.5 text-[11px] hover:text-foreground"
            >
              ✕ 关闭
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3 text-[12px]">
          {/* ── ① 数据完整度诊断 ── */}
          <section className="mb-4 rounded-lg border border-border bg-background/40 p-3">
            <h3 className="mb-2 font-mono text-[11px] font-bold uppercase tracking-wider text-foreground">
              📊 数据完整度诊断
            </h3>
            <p className="mb-2 text-[11px] text-muted-foreground">
              本次 lifecycle 接口应该返回 {integrity.length} 类字段,来自 {integrity.length} 张表。
              绿色 = 拿到了,红框 = 关键字段缺失,灰框 = 这次为空(可能没跑这阶段)。
            </p>
            {criticalMiss.length > 0 ? (
              <div className="mb-2 rounded border border-short/40 bg-short/5 px-2 py-1.5 text-[11px] text-short">
                ⚠ 关键字段缺失:{criticalMiss.map((m) => m.label).join('、')} —— 这条新闻可能没跑过这一阶段。
              </div>
            ) : null}
            <div className="grid grid-cols-2 gap-1.5">
              {integrity.map((r) => (
                <div
                  key={r.key}
                  className={`rounded border px-2 py-1 ${
                    r.present
                      ? 'border-long/30 bg-long/5'
                      : r.critical
                          ? 'border-short/40 bg-short/5'
                          : 'border-border bg-muted/30'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[12px] font-bold">{r.present ? '✓' : '✗'} {r.label}</span>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {r.present ? `${r.count}` : '空'}
                    </span>
                  </div>
                  <div className="font-mono text-[10px] text-muted-foreground">
                    → {r.table}
                    {r.critical ? ' · 关键' : ''}
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* ── ② 网络调用时间线 ── */}
          <section className="rounded-lg border border-border bg-background/40 p-3">
            <h3 className="mb-2 font-mono text-[11px] font-bold uppercase tracking-wider text-foreground">
              📡 网络调用时间线
            </h3>
            <p className="mb-2 text-[11px] text-muted-foreground">
              浏览器 → 后端 真实流量(mount 后累积)。颜色:绿=成功 / 黄=重定向 / 红=错误。
            </p>
            {entries.length === 0 ? (
              <div className="rounded border border-border bg-muted/20 px-3 py-4 text-center text-[11px] text-muted-foreground">
                还没有任何请求 —— 切换新闻、调整 only_with_decisions 开关来触发。
              </div>
            ) : (
              <div className="space-y-1.5">
                {entries.map((e) => {
                  const path = e.url.replace(/^.*\/api\//, '/api/')
                  const statusCls =
                    e.status >= 500
                      ? 'border-short/40 bg-short/10 text-short'
                      : e.status >= 400
                        ? 'border-short/40 bg-short/10 text-short'
                        : e.status >= 300
                            ? 'border-hold/40 bg-hold/10 text-hold'
                            : e.status >= 200
                                ? 'border-long/40 bg-long/10 text-long'
                                : 'border-short/40 bg-short/10 text-short'
                  return (
                    <details key={e.id} className="rounded border border-border bg-background/60">
                      <summary className="cursor-pointer px-2 py-1.5 hover:bg-background/80">
                        <div className="flex items-center gap-2">
                          <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${statusCls}`}>
                            {e.status || 'ERR'}
                          </span>
                          <span className="font-mono text-[10px] font-bold text-foreground">{e.method}</span>
                          <span className="font-mono text-[11px] text-foreground">{path}</span>
                          <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                            {e.latency_ms} ms · {(e.size_bytes / 1024).toFixed(1)} KB ·{' '}
                            {new Date(e.ts).toLocaleTimeString()}
                          </span>
                        </div>
                      </summary>
                      <div className="border-t border-border px-3 py-2 text-[11px]">
                        <div className="mb-1 font-mono text-[10px] text-muted-foreground">
                          完整 URL:{e.url}
                        </div>
                        {e.err ? (
                          <div className="rounded border border-short/40 bg-short/10 px-2 py-1 text-short">
                            ✖ {e.err}
                          </div>
                        ) : e.response_shape ? (
                          <div>
                            <div className="mb-1 font-mono text-[10px] font-bold uppercase tracking-wider text-foreground/80">
                              响应字段({Object.keys(e.response_shape).length}):
                            </div>
                            <div className="grid grid-cols-2 gap-1">
                              {Object.entries(e.response_shape).map(([k2, v2]) => (
                                <div
                                  key={k2}
                                  className="rounded border border-border bg-background/60 px-1.5 py-1 font-mono text-[10px]"
                                >
                                  <span
                                    className={
                                      v2.kind === 'null' ? 'text-short' : 'text-foreground/85'
                                    }
                                  >
                                    {v2.kind === 'null'
                                      ? '∅'
                                      : v2.kind === 'array'
                                        ? `📦${v2.len}`
                                        : v2.kind === 'object'
                                          ? `🗂️${v2.len}`
                                          : v2.kind}
                                  </span>{' '}
                                  <span className="text-foreground/90">{k2}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        ) : (
                          <div className="text-muted-foreground">非 JSON 响应</div>
                        )}
                      </div>
                    </details>
                  )
                })}
              </div>
            )}
          </section>

          {/* ── ③ 后端 handler 调用栈 ── */}
          <section className="mt-4 rounded-lg border border-border bg-background/40 p-3">
            <h3 className="mb-2 font-mono text-[11px] font-bold uppercase tracking-wider text-foreground">
              🔧 后端 handler 调用栈(本次 lifecycle 接口)
            </h3>
            <p className="mb-2 text-[11px] text-muted-foreground">
              按数据完整度逐张表展开 —— 这条新闻到底从哪些表拿过数据。
            </p>
            <ol className="space-y-1 text-[12px]">
              <li className="rounded border border-border bg-background/60 px-2 py-1">
                <span className="font-mono text-[10px] font-bold text-primary">01</span>{' '}
                FastAPI 路由
                <span className="ml-2 font-mono text-[10px] text-muted-foreground">
                  GET /api/analysis-process/news/{lifecycle?.news_id ?? '{id}'}
                </span>
              </li>
              <li className="rounded border border-border bg-background/60 px-2 py-1">
                <span className="font-mono text-[10px] font-bold text-primary">02</span> asyncpg pool
                <span className="ml-2 font-mono text-[10px] text-muted-foreground">
                  从连接池拿连接(空闲连接直接命中,~ 1 ms)
                </span>
              </li>
              {integrity.map((r, i) => (
                <li
                  key={r.key}
                  className={`rounded border px-2 py-1 ${
                    r.present
                      ? 'border-border bg-background/60'
                      : 'border-border bg-muted/20 text-muted-foreground'
                  }`}
                >
                  <span className="font-mono text-[10px] font-bold text-primary">
                    {String(i + 3).padStart(2, '0')}
                  </span>{' '}
                  SELECT * FROM <span className="font-mono">{r.table}</span>
                  <span className="ml-2 text-[10px] text-muted-foreground">
                    WHERE news_id = {lifecycle?.news_id ?? '{id}'} →{' '}
                    {r.present ? `拿到 ${r.count} 条` : '空集(这条新闻没跑这一阶段)'}
                  </span>
                </li>
              ))}
              <li className="rounded border border-border bg-background/60 px-2 py-1">
                <span className="font-mono text-[10px] font-bold text-primary">N</span>{' '}
                JSON 序列化 + 响应
                <span className="ml-2 font-mono text-[10px] text-muted-foreground">
                  把 {integrity.filter((r) => r.present).length} 张表的结果拼成一个对象返回给前端
                </span>
              </li>
            </ol>
          </section>
        </div>
      </div>
    </div>
  )
}

export function AnalysisProcessPanel() {
  const [items, setItems] = useState<APListItem[]>([])
  const [listLoading, setListLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [onlyWithDecisions, setOnlyWithDecisions] = useState(true)
  const [selected, setSelected] = useState<number | null>(null)
  const [lifecycle, setLifecycle] = useState<APLifecycle | null>(null)
  const [lcLoading, setLcLoading] = useState(false)
  const [lcError, setLcError] = useState<string | null>(null)

  // 实时报价 —— 让左栏 NewsList 行内"现价"与主页 SignalLedger 同源
  const { prices: livePrices } = useBinancePrices()

  // ─────── 后台运行时追踪 ───────
  const [traceEntries, setTraceEntries] = useState<TraceEntry[]>([])
  const [showTrace, setShowTrace] = useState(false)
  const traceIdRef = useRef(0)
  const originalFetchRef = useRef<typeof fetch | null>(null)
  // mount 时 patch window.fetch,捕获所有 /api/analysis-process/* 的请求
  useEffect(() => {
    if (typeof window === 'undefined') return
    const original = window.fetch
    originalFetchRef.current = original
    const wrapped = async function patchedFetch(
      input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : (input as Request).url
      const method = (init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase()
      const isTracked = url.includes('/api/analysis-process/')
      const entryBase: Omit<TraceEntry, 'status' | 'latency_ms' | 'size_bytes' | 'response_shape' | 'err'> = {
        id: ++traceIdRef.current,
        url,
        method,
        ts: Date.now(),
      }
      if (!isTracked) return original.apply(window, [input, init] as Parameters<typeof original>)
      const t0 = performance.now()
      try {
        const res = await original.apply(window, [input, init] as Parameters<typeof original>)
        const clone = res.clone()
        const buf = await clone.arrayBuffer()
        const latency = performance.now() - t0
        let shape: TraceEntry['response_shape'] = null
        try {
          const text = new TextDecoder().decode(buf)
          const obj = JSON.parse(text)
          shape = summarizeShape(obj)
        } catch {
          /* not JSON */
        }
        setTraceEntries((prev) =>
          [
            {
              ...entryBase,
              status: res.status,
              latency_ms: Math.round(latency * 10) / 10,
              size_bytes: buf.byteLength,
              response_shape: shape,
            } as TraceEntry,
            ...prev,
          ].slice(0, 50),
        )
        // 用 clone 的 response 返回(因为我们刚才读了 buf,但 clone 不影响原始)
        return res
      } catch (err) {
        setTraceEntries((prev) =>
          [
            {
              ...entryBase,
              status: 0,
              latency_ms: Math.round((performance.now() - t0) * 10) / 10,
              size_bytes: 0,
              response_shape: null,
              err: String(err),
            },
            ...prev,
          ].slice(0, 50),
        )
        throw err
      }
    }
    window.fetch = wrapped as typeof fetch
    return () => {
      window.fetch = original
    }
  }, [])

  const loadList = useCallback(async () => {
    setListLoading(true)
    setListError(null)
    try {
      const r = await fetchRecentAnalysisList(50, onlyWithDecisions)
      setItems(r.items)
    } catch (e: unknown) {
      setListError(e instanceof Error ? e.message : String(e))
    } finally {
      setListLoading(false)
    }
  }, [onlyWithDecisions])

  const loadDetail = useCallback(async (newsId: number) => {
    setLcLoading(true)
    setLcError(null)
    try {
      const lc = await fetchLifecycle(newsId)
      setLifecycle(lc)
    } catch (e: unknown) {
      setLcError(e instanceof Error ? e.message : String(e))
      setLifecycle(null)
    } finally {
      setLcLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadList()
  }, [loadList])

  useEffect(() => {
    if (selected !== null) {
      void loadDetail(selected)
    } else {
      setLifecycle(null)
    }
  }, [selected, loadDetail])

  useEffect(() => {
    if (selected === null && items.length > 0 && !listLoading) {
      setSelected(items[0].news_id)
    }
  }, [items, listLoading, selected])

  return (
    <div className="grid h-full grid-cols-[340px_1fr] divide-x divide-border">
      <NewsList
        items={items}
        selected={selected}
        loading={listLoading}
        onlyWithDecisions={onlyWithDecisions}
        onToggleOnlyWithDecisions={(v) => {
          setSelected(null)
          setOnlyWithDecisions(v)
        }}
        onSelect={(id) => setSelected(id)}
        onRefresh={() => void loadList()}
        livePrices={livePrices}
      />

      <div className="flex h-full flex-col overflow-hidden">
        <div className="flex-shrink-0 border-b border-border bg-background/40 px-3 py-2">
          <MissedSignalsByDimBanner variant="inline" days={2000} refreshMs={60_000} />
        </div>
        {listError ? (
          <div className="border-b border-short/30 bg-short/5 px-3 py-2 font-mono text-[11px] text-short">
            列表加载失败: {listError}
          </div>
        ) : null}

        {selected === null ? (
          <div className="flex flex-1 items-center justify-center font-mono text-xs text-muted-foreground">
            ← 选择一条新闻查看完整生命周期
          </div>
        ) : lcLoading ? (
          <div className="flex flex-1 items-center justify-center font-mono text-xs text-muted-foreground">
            加载新闻 #{selected} ...
          </div>
        ) : lcError ? (
          <div className="flex flex-1 items-center justify-center font-mono text-xs text-short">
            详情加载失败: {lcError}
          </div>
        ) : lifecycle ? (
          <DetailView lc={lifecycle} />
        ) : null}
      </div>

      {/* 🛰️ 浮动按钮 + Drawer */}
      <button
        type="button"
        onClick={() => setShowTrace(true)}
        className="fixed bottom-6 right-6 z-40 flex items-center gap-2 rounded-full border-2 border-primary/50 bg-primary/90 px-4 py-2.5 text-xs font-bold text-primary-foreground shadow-lg hover:bg-primary"
        title="打开后台运行时追踪"
      >
        🛰️ 后台跑什么
        {traceEntries.length > 0 ? (
          <span className="rounded-full bg-primary-foreground px-1.5 py-0.5 font-mono text-[10px] text-primary">
            {traceEntries.length}
          </span>
        ) : null}
      </button>
      <RuntimeTraceDrawer
        open={showTrace}
        onClose={() => setShowTrace(false)}
        entries={traceEntries}
        lifecycle={lifecycle}
        onClear={() => setTraceEntries([])}
      />
    </div>
  )
}

function DetailView({ lc }: { lc: APLifecycle }) {
  const primary = lc.primary_decision_id
    ? lc.decisions.find((d) => d.id === lc.primary_decision_id) || null
    : lc.decisions[0] ?? null
  const [activeStage, setActiveStage] = useState(0)

  const gate = lc.gate
  const reasoning = lc.reasoning
  const llmMeta = lc.llm_call_meta
  const qsList = lc.quick_sim
  const reflections = lc.trade_reflections
  const structured = lc.structured_reflections
  const attributions = lc.attribution_reviews
  const insights = lc.review_insights
  const experiences = lc.experience_entries
  const chains = lc.causal_chain_links

  const stageStates = useMemo(
    () => ({
      ingest: 'done' as const,
      gate: gate ? ('done' as const) : ('skipped' as const),
      ai: reasoning || llmMeta ? ('done' as const) : ('pending' as const),
      trade:
        qsList.length === 0
          ? ('pending' as const)
          : qsList.every((q) => q.is_correct === 'WIN')
            ? ('done' as const)
            : ('partial' as const),
      reflect:
        reflections.length === 0 && structured.length === 0 && attributions.length === 0
          ? ('pending' as const)
          : ('done' as const),
      experience:
        insights.length === 0 && experiences.length === 0
          ? ('pending' as const)
          : ('done' as const),
    }),
    [gate, reasoning, llmMeta, qsList, reflections, structured, attributions, insights, experiences],
  )

  const summary = useMemo(() => buildSummary(lc, primary), [lc, primary])
  const newsPreview =
    (lc.news?.content || '').slice(0, 320) +
    ((lc.news?.content || '').length > 320 ? '…' : '')

  return (
    <div className="flex-1 overflow-y-auto p-4">
      {/* ① Summary 头 */}
      <div className="rounded-lg border-2 border-primary/40 bg-gradient-to-br from-primary/10 via-card to-card p-4">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-primary">
            一句话总结
          </span>
          <span className="text-[10px] text-muted-foreground">
            · #{lc.news_id} · {lc.news ? sourceLabel(lc.news.source) : '—'} ·{' '}
            {lc.news ? fmtTs(lc.news.timestamp ?? lc.news.ts) : '—'}
          </span>
        </div>
        <p className="mt-2 text-base leading-relaxed text-foreground">{summary.text}</p>
        {summary.highlights.length > 0 ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {summary.highlights.map((h, i) => (
              <span
                key={i}
                className={`rounded-md border px-2 py-1 font-mono text-[11px] ${h.cls}`}
              >
                {h.label}: {h.value}
              </span>
            ))}
          </div>
        ) : null}
        <details className="mt-3">
          <summary className="cursor-pointer text-[11px] text-muted-foreground hover:text-foreground">
            ▾ 看原文(共 {(lc.news?.content || '').length} 字)
          </summary>
          <div className="mt-2 rounded-md border border-border bg-background/60 p-3">
            <p className="mb-2 text-[12px] text-foreground/85">{newsPreview}</p>
            <p className="whitespace-pre-wrap text-[12px] text-foreground/70">
              {(lc.news?.content || '').slice(320)}
            </p>
          </div>
        </details>
      </div>

      {/* ② Timeline 横向节点(同时承担导航) */}
      <div className="mt-4">
        <Timeline
          activeIdx={activeStage}
          onSelect={(i) => {
            setActiveStage(i)
            const el = document.getElementById(`stage-${i}`)
            el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
          }}
          nodes={[
            {
              key: 'ingest',
              title: '① 入库',
              plain: '新闻刚被抓回来',
              state: stageStates.ingest,
              detail: lc.news ? sourceLabel(lc.news.source) : '—',
            },
            {
              key: 'gate',
              title: '② Gate',
              plain: '判断值不值得看',
              state: stageStates.gate,
              detail: gate
                ? `${gate.main_asset || '?'} · ${gate.kind || '?'} · ${gate.strength ?? '?'}分`
                : '—',
            },
            {
              key: 'ai',
              title: '③ AI 推理',
              plain: '模型给出判断',
              state: stageStates.ai,
              detail: primary
                ? `${primary.suggested_action || '?'} ${primary.target_asset || ''}`
                : '',
            },
            {
              key: 'trade',
              title: '④ 交易',
              plain: '看实际赚亏',
              state: stageStates.trade,
              detail: qsList.length
                ? `×${qsList.length}笔 · ${qsList.filter((q) => q.is_correct === 'WIN').length}赢`
                : '未开仓',
            },
            {
              key: 'reflect',
              title: '⑤ 复盘',
              plain: '为什么对/错',
              state: stageStates.reflect,
              detail: `${reflections.length + attributions.length} 条反思`,
            },
            {
              key: 'experience',
              title: '⑥ 沉淀',
              plain: '变经验规则',
              state: stageStates.experience,
              detail: `${insights.length} 条规则`,
            },
          ]}
        />
      </div>

      {/* ③ 阶段详情(默认折叠) */}
      <div className="mt-4 space-y-3">
        <div id="stage-0">
          <IngestGateSection
            news={lc.news}
            gate={lc.gate}
            ts_iso={fmtTs(lc.news?.timestamp ?? lc.news?.ts)}
          />
        </div>
        <div id="stage-1">
          <SectionCard
            tone="blue"
            step="②"
            title="Gate 筛选 + AI 决策细节(逐步)"
            subtitle="主资产/类型/时效/强度/评级 + llm_meta 11 维"
            defaultOpen={true}
          >
            <p className="mb-3 text-[11px] leading-relaxed text-foreground/85">
              Gate 是第一道关,只让"值得看"的新闻通过,降低 AI 推理的浪费。
              下面把 Gate 4 维 + LLM 元数据 11 维全拆成步骤,点播放看 AI 是怎么一步步过这一关的。
            </p>
            <StepPlayer
              steps={buildGateSteps(gate, llmMeta, primary)}
              title="AI 决策过程 · 逐步拆解"
              intro="从资产识别 → 类别 → 时效 → 强度 → 模型证据 → 冲击 → 阶段 → 时段 → 门禁 → 评级,每一步都能看到当时用的字段值是什么。"
              finishNote={
                primary
                  ? `决策完毕 → ${primary.suggested_action || '—'} ${
                      primary.target_asset || ''
                    } · confidence ${primary.confidence ?? '—'}`
                  : '守住了 Gate,未生成交易'
              }
              defaultSpeed={1.5}
            />
          </SectionCard>
        </div>

        <div id="stage-2">
          <LlmReasoningSection
            reasoning={reasoning}
            llm_meta={llmMeta}
            decision={primary}
          />
        </div>
        <div id="stage-3">
          <TradeExecutionSection quick_sim={qsList} paper_run={lc.paper_run} />
        </div>
        <div id="stage-4">
          <ReflectionSection
            reflections={reflections}
            structured={structured}
            attributions={attributions}
            news_trade_review={lc.news_trade_review}
            decision={primary}
          />
        </div>
        <div id="stage-5">
          <ExperienceSection insights={insights} experiences={experiences} chains={chains} />
        </div>

        {primary ? (
          <details className="rounded-md border border-border bg-background/40 px-3 py-1.5">
            <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground">
              ▾ 给开发者看 · 原始 JSON(raw_news + ai_decision)
            </summary>
            <div className="mt-2 grid grid-cols-1 gap-3 border-t border-border pt-2 md:grid-cols-2">
              <div>
                <div className="mb-1 font-mono text-[10px] font-bold uppercase tracking-wider text-primary">
                  raw_news
                </div>
                <JsonDump data={lc.news} />
              </div>
              <div>
                <div className="mb-1 font-mono text-[10px] font-bold uppercase tracking-wider text-primary">
                  ai_decision(primary)
                </div>
                <JsonDump data={primary} />
              </div>
            </div>
          </details>
        ) : null}
      </div>
    </div>
  )
}


// 一句话总结生成器 —— 把生命周期 6 个字段压成一句人话
function buildSummary(
  lc: APLifecycle,
  primary: APLifecycle['decisions'][number] | null,
): { text: string; highlights: Array<{ label: string; value: string; cls: string }> } {
  const highlights: Array<{ label: string; value: string; cls: string }> = []
  if (!lc.news) return { text: '无原始新闻数据', highlights }

  const gate = lc.gate
  const reasoning = lc.reasoning
  const decided = !!primary
  const settled = primary?.settled === 1
  const verdict = String(primary?.is_correct || '').toUpperCase()

  // 路径 1:已经结算
  if (settled && primary && typeof primary.forward_pnl === 'number') {
    const pnl = primary.forward_pnl
    const ok = verdict === 'WIN' || verdict === 'CORRECT'
    const asset = primary.target_asset || gate?.main_asset || '?'
    const action = primary.suggested_action || '?'
    const horizon = primary.expected_horizon || '?'
    // v210.1: forward_pnl DB 里就是百分数（0.16 = 0.16%），不要 ×100
    const text = `模型建议 ${action} ${asset} (${horizon}),预测 ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%,判断${ok ? '对 ✓' : '错 ✗'}。`
    highlights.push(
      { label: '标的', value: asset, cls: 'border-border bg-secondary text-foreground' },
      {
        label: '建议',
        value: action,
        cls:
          action === 'BUY'
            ? 'border-long/40 bg-long/10 text-long'
            : action === 'SELL'
              ? 'border-short/40 bg-short/10 text-short'
              : 'border-hold/40 bg-hold/10 text-hold',
      },
      {
        label: '预测',
        // v210.1: forward_pnl DB 里就是百分数（0.16 = 0.16%），不要 ×100
        value: `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`,
        cls:
          pnl >= 0
            ? 'border-long/40 bg-long/10 text-long'
            : 'border-short/40 bg-short/10 text-short',
      },
      {
        label: '对错',
        value: ok ? 'WIN' : verdict || '—',
        cls: ok
          ? 'border-long/40 bg-long/10 text-long'
          : 'border-short/40 bg-short/10 text-short',
      },
    )
    return { text, highlights }
  }

  // 路径 2:已决策但未结算
  if (decided && primary) {
    const asset = primary.target_asset || gate?.main_asset || '?'
    const action = primary.suggested_action || '?'
    const horizon = primary.expected_horizon || '?'
    const text = `AI 看了之后决定 ${action} ${asset} (${horizon}),还在等市场跑出结果 —— 复盘要等 ${horizon} 后才生成。`
    highlights.push(
      { label: '标的', value: asset, cls: 'border-border bg-secondary text-foreground' },
      {
        label: '建议',
        value: action,
        cls:
          action === 'BUY'
            ? 'border-long/40 bg-long/10 text-long'
            : action === 'SELL'
              ? 'border-short/40 bg-short/10 text-short'
              : 'border-hold/40 bg-hold/10 text-hold',
      },
      { label: '持仓期', value: horizon, cls: 'border-border bg-secondary text-foreground' },
    )
    return { text, highlights }
  }

  // 路径 3:Gate 阻断
  if (gate && (gate.verdict || '').includes('阻断')) {
    const text = `Gate 阶段把这条新闻拦下了 —— ${gate.no_trade_reason || gate.trade_gate_reason || '不够重要'}。所以没让 AI 去推理。`
    highlights.push({
      label: 'Gate 状态',
      value: gate.verdict || '—',
      cls: 'border-short/40 bg-short/10 text-short',
    })
    return { text, highlights }
  }

  // 路径 4:无 AI 决策
  if (!reasoning) {
    const text = `这条新闻刚抓回来,AI 还没处理(或者排队中)。先看个原文吧。`
    return { text, highlights }
  }

  return { text: `这条新闻被抓进来了,接下来交给 AI 推理。`, highlights }
}

// ───────────────────────────────────────────────────────────
// buildGateSteps —— Gate + llm_meta 的全字段决策过程逐步回放
// ───────────────────────────────────────────────────────────

function buildGateSteps(
  gate: APGate | null,
  llmMeta: APLlmCallMeta | null,
  decision: APDecision | null,
): PlayerStep[] {
  const steps: PlayerStep[] = []
  if (gate) {
    const assetList = Array.isArray(gate.assets)
      ? gate.assets
      : typeof gate.assets === 'string'
        ? gate.assets.split(/[,，]\s*/).filter(Boolean)
        : []
    steps.push({
      id: 'g-asset',
      title: '① 资产识别',
      plain:
        '从原文里把提到的所有 ticker / 关键词识别出来,挑出 main_asset(主标的)。',
      body: `[assets 列表] ${
        assetList.length ? assetList.join(', ') : '—'
      }\n[main_asset] ${gate.main_asset || '—'}\n[选了谁] 命中强度最高的那一个,其余作为相关标的进 watchlist`,
      tone: 'blue',
    })
    steps.push({
      id: 'g-kind',
      title: '② 类别判定',
      plain:
        '判定事件属于哪一类(政策/公司公告/突发/技术升级/合作/资金流等),不同类走不同处理逻辑。',
      body: `[kind] ${gate.kind || '—'}\n[分类作用] 不同 kind 会对应不同的 is_material 阈值与下游 prompt`,
      tone: 'blue',
    })
    steps.push({
      id: 'g-timing',
      title: '③ 时效判定',
      plain:
        '事件冲击时长(短期 1-3 天 / 中期 1-2 周 / 长期 1 月+),影响持仓期与监控频率。',
      body: `[timing] ${gate.timing || '—'}`,
      tone: 'blue',
    })
    steps.push({
      id: 'g-strength',
      title: '④ 强度评分',
      plain:
        '0-10 分的强度评分 + is_material(是否物质化)—— < 阈值的直接过滤掉,不进 AI 推理。',
      body: `[strength] ${gate.strength ?? '—'} / 10\n[is_material] ${
        gate.is_material ?? '—'
      }\n[作用] 强度 < 阈值 → 直接 dispose,跳到 no_trade_reason`,
      tone: 'gold',
    })
  }
  if (llmMeta) {
    steps.push({
      id: 'g-evidence',
      title: '⑤ 模型证据 & 交叉验证',
      plain:
        'AI 给新闻的可信度打分(0-1),并确认是否和市场走势 / 链上数据对得上 —— 反证数 / 噪音过高会被拒。',
      body: `[agent_model_id] ${llmMeta.agent_model_id || '—'}\n[evidence_confidence] ${
        llmMeta.evidence_confidence ?? '—'
      }\n[evidence_action] ${llmMeta.evidence_action || '—'}\n[market_confirmation] ${
        llmMeta.market_confirmation || '—'
      }`,
      tone: 'purple',
    })
    steps.push({
      id: 'g-event',
      title: '⑥ 事件强度',
      plain: '对事件本身强度的二次描述(strong/medium/weak/none),影响仓位大小。',
      body: `[event_strength] ${llmMeta.event_strength || '—'}`,
      tone: 'purple',
    })
    steps.push({
      id: 'g-impact',
      title: '⑦ 冲击估算 & 注意力',
      plain:
        '预估"这条新闻会让价格动多少%(MFE/MAE 上限)",以及 AI 给出的注意力分数。',
      body: `[impact_magnitude] ${llmMeta.impact_magnitude ?? '—'}\n[attention_score] ${
        llmMeta.attention_score ?? '—'
      }`,
      tone: 'purple',
    })
    steps.push({
      id: 'g-phase',
      title: '⑧ 事件阶段',
      plain: '事件处于哪个阶段(initial/development/escalation/fade),决定监控节奏。',
      body: `[event_phase] ${llmMeta.event_phase || '—'}`,
      tone: 'purple',
    })
    steps.push({
      id: 'g-timeframe',
      title: '⑨ 时段匹配',
      plain:
        'AI 评估"现在市场阶段 × 新闻类型"是否契合 —— 错配会降低 confidence。',
      body: `[timeframe_match] ${llmMeta.timeframe_match || '—'}`,
      tone: 'purple',
    })
    steps.push({
      id: 'g-noTrade',
      title: '⑩ 决策门禁 & 不交易原因',
      plain:
        '综合所有维度,记录为什么能开仓 / 为什么不能开仓 —— 整条决策链的可审计关键。',
      body: `[trade_gate_reason] ${
        llmMeta.trade_gate_reason || gate?.trade_gate_reason || '—'
      }\n[no_trade_reason] ${
        llmMeta.no_trade_reason || gate?.no_trade_reason || '—'
      }`,
      tone: 'hold',
    })
  }
  if (gate) {
    steps.push({
      id: 'g-verdict',
      title: '⑪ 评级结论',
      plain:
        '最终结论(可交易 / 观察 / 阻断)—— 这一行直接决定新闻是否进入决策生成环节。',
      body: `[verdict] ${gate.verdict || '—'}\n[core_impact] ${gate.core_impact || '—'}`,
      tone: 'gold',
    })
  }
  // ⑫ 决策动作 vs 证据一致性
  if (
    llmMeta?.evidence_action &&
    decision?.suggested_action &&
    llmMeta.evidence_action !== decision.suggested_action
  ) {
    steps.push({
      id: 'g-conflict',
      title: '⑫ 证据 vs 决策冲突',
      plain:
        '⚠ 证据给出的动作与最终决策不一致 —— 说明 LLM 推理时被覆写了,值得深查。',
      body: `[evidence_action] ${llmMeta.evidence_action}\n[decided_action] ${
        decision.suggested_action
      }\n→ 这两个值应当一致;不一致 = 数据 / 流程异常`,
      tone: 'short',
    })
  }
  if (steps.length === 0) {
    steps.push({
      id: 'g-empty',
      title: '没有 Gate / llm_meta 数据',
      plain: '这条新闻没跑 Gate 评估或没拿到 LLM 元数据。',
      body: '字段为空:gate / llm_call_meta 都没追到',
      tone: 'gold',
    })
  }
  return steps
}
