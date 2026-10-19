'use client'

import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiSend } from '@/lib/api'
import {
  periodToQuery,
  type PeriodRange,
} from '@/lib/data-channel-standards'
import { useQuickSimHorizon } from '@/lib/hooks/use-quick-sim-horizon'
import { useApiPoll } from '@/lib/hooks/use-api-poll'
import { useToast } from '@/components/ui/toast'
import { PatternAuditBlock, type PatternAudit } from './pattern-audit'

// 反思系统面板（块 A：历史真实交易复盘）— 总结分析 + 失败原因归类 + 经验规则
// 数据源：ai_decisions 闸门后真实持仓（quick_sim_trades 的参考信号已移入块 B）
//
// v290 P0+（反思系统时段统一）：删除组件内部 scope 选择器（默认 'week' 与
// 顶部全局 PeriodSelector 默认 'today' 冲突 → 出现"两套数据打架"），改为
// 接收 props.period，所有 API 调用走 periodToQuery 统一出参。
//
// v170 P0+（巨型组件拆分）：规律自审块（PatternAuditItem/Block + 类型 + 渲染常量）
// 拆到 ./pattern-audit.tsx（约 200 行），本文件仅做编排与数据加载，渲染由子组件承接。

type ReviewReport = {
  report_id?: number
  scope: string
  generated_at: string
  samples: number
  new_insights?: number
  summary: {
    overview?: string
    strengths?: string[]
    weaknesses?: string[]
    patterns?: string[]
    improvements?: string[]
    logical_reflection?: string[]
    first_principles?: string[]
    pattern_audits?: PatternAudit[]
  }
  aggregate?: {
    total: number
    decided: number
    wins: number
    losses: number
    holds: number
    winrate: number | null
    avg_pnl: number | null
    total_pnl: number
  }
  failures: {
    analyzed: number
    failed_trades: number
    stats: Record<string, number>
  }
  successes?: {
    analyzed: number
    won_trades: number
    stats: Record<string, number>
  }
  feedback?: {
    insight_started_at: string | null
    before?: { total: number; winrate: number | null; total_pnl: number }
    after?: { total: number; winrate: number | null; total_pnl: number }
    winrate_delta?: number | null
  }
  models?: Array<{
    model: string
    total: number
    wins: number
    losses: number
    decided: number
    winrate: number | null
    avg_pnl: number | null
    total_pnl: number | null
  }>
  insight_evaluation?: {
    evaluated: number
    deactivated: string[]
    reactivated?: string[]
    rescued_no_sample?: string[]
  }
  insights: string[]
  insights_by_origin?: { real?: string[]; quick_sim?: string[] }
  prompt_suggestions?: string[]
}

// v290 P0+：删除 SCOPES 常量 —— 时段由顶部全局 PeriodSelector 唯一控制，
// 组件内部不再持有「今日/本周/全部」三档按钮（保留就容易漂移）。
// 历史报告列表里 h.scope 字段仍兼容（旧报告存的是 today|week|all 离散档）。

function ListBlock({ title, items, tone }: { title: string; items?: string[]; tone?: 'good' | 'bad' | 'plain' }) {
  if (!items || items.length === 0) return null
  const color = tone === 'good' ? 'text-long' : tone === 'bad' ? 'text-short' : 'text-foreground'
  return (
    <div>
      <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{title}</div>
      <ul className="mt-1 space-y-0.5">
        {items.map((item, i) => (
          <li key={i} className={`text-[11px] leading-relaxed ${color}`}>• {item}</li>
        ))}
      </ul>
    </div>
  )
}

// 经验来源标记：快速评测 / 历史交易 / 归因规则。
// 快速评测经验用琥珀色，与提示库页面的颜色语义一致，便于按来源挑选删除。
const ORIGIN_META: Record<string, { label: string; cls: string; title: string }> = {
  quick_sim: {
    label: '快速评测',
    cls: 'border-amber-500/40 bg-amber-500/10 text-amber-500',
    title: '由快速评测样本提炼；重置模拟盘会删除其底层样本，此经验将无法再验证，可手动删除',
  },
  rule: {
    label: '归因',
    cls: 'border-secondary bg-secondary text-muted-foreground',
    title: '归因规则',
  },
  real: {
    label: '历史交易',
    cls: 'border-long/40 bg-long/10 text-long',
    title: '由历史真实交易复盘提炼',
  },
}

function OriginBadge({ origin }: { origin?: string }) {
  const meta = ORIGIN_META[origin || 'real'] ?? ORIGIN_META.real
  return (
    <span className={`shrink-0 rounded border px-1 py-px font-mono text-[8px] ${meta.cls}`} title={meta.title}>
      {meta.label}
    </span>
  )
}



export function ReviewPanel({ period }: { period: PeriodRange }) {
  const { toast } = useToast()
  // v290 P0+：删掉内部 [scope, setScope] state —— 全局 period 唯一来源，
  // 顶部 PeriodSelector 一切换立即通过 props 传进来，杜绝 scope 漂移。
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<ReviewReport | null>(null)
  const [history, setHistory] = useState<ReviewReport[]>([])
  const [currentInsights, setCurrentInsights] = useState<string[]>([])
  const [currentInsightItems, setCurrentInsightItems] = useState<Array<{ id: number; rule: string; origin: string }>>([])
  const [deactivatedInsights, setDeactivatedInsights] = useState<Array<{ id?: number; rule: string; source_samples: number; created_at: string; origin?: string }>>([])
  // 管理视图全量经验（含生效+停用，带 id/origin）——用于逐条手动删除。
  // 与 currentInsightItems 的区别：那个上限 8 条（= 注入 prompt 的窗口），
  // 超过 8 条会看不到也删不掉，所以删除视图单独取全量。
  const [allInsights, setAllInsights] = useState<Array<{ id: number; rule: string; origin: string; active: number; source_samples: number }>>([])
  const [showInsights, setShowInsights] = useState(true)
  const [deletingId, setDeletingId] = useState<number | null>(null)
  const [togglingId, setTogglingId] = useState<number | null>(null)
  const [showAllDeactivated, setShowAllDeactivated] = useState(false)
  // 生效经验：是否展开全部（默认只显示注入 prompt 的前 8 条窗口）
  const [showAllActive, setShowAllActive] = useState(false)
  // 经验来源筛选：全部 / 历史交易 / 快速评测 / 归因
  const [originFilter, setOriginFilter] = useState<'all' | 'real' | 'quick_sim' | 'rule'>('all')
  // 快速评测持仓时长（分钟）—— v170 P0+：从独立 hook 取，与 review-history-panel 同源
  // 之前在 loadInsights 里顺手取，耦合到 insights 接口；现在 useQuickSimHorizon 内部 usePoll 兜底，
  // 后端字段名变更也不影响这里。
  const qsHorizon = useQuickSimHorizon()

  // v170 P0+：10s 轮询「生效经验」改用 useApiPoll —— 复用 abort / deps 机制，
  // 替代原 useEffect + window.setInterval 复制粘贴模式（缺 unmount 保护）。
  const insightsPoll = useApiPoll<{
    insights?: string[]
    active_items?: Array<{ id: number; rule: string; origin: string }>
    deactivated_recent?: Array<{ id?: number; rule: string; source_samples: number; created_at: string; origin?: string }>
  }>('/review/insights', {
    intervalMs: 10_000,
    deps: [],
    onError: () => {/* 静默：经验拉取失败不弹错 */},
  })

  const loadAllInsights = useCallback(async () => {
    try {
      const data = await apiGet<typeof allInsights>('/review/insights/all')
      setAllInsights(data)
    } catch {
      // 静默：全量列表拿不到不影响主展示（删除按钮会退回 8 条窗口的可见范围）
    }
  }, [])

  // 同步 insightsPoll.data → 三个独立 state（兼容下游读取）
  useEffect(() => {
    if (insightsPoll.data) {
      const d = insightsPoll.data
      setCurrentInsights(d.insights ?? [])
      setCurrentInsightItems(d.active_items ?? [])
      setDeactivatedInsights(d.deactivated_recent ?? [])
    }
  }, [insightsPoll.data])

  // 每次 insights 拉取成功，附带拉一次 all（确保 200 条以上不被截断）
  useEffect(() => {
    if (insightsPoll.data) void loadAllInsights()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [insightsPoll.data])

  // 主动重拉（用户操作后）：暴露给 toggleInsight / removeByOrigin / removeInsight
  const refreshInsights = insightsPoll.refresh

  const loadHistory = useCallback(async () => {
    try {
      const data = await apiGet<ReviewReport[]>('/review/reports?limit=3')
      setHistory(data)
      if (!report && data.length > 0) setReport(data[0])
    } catch {
      // ignore
    }
  }, [report])

  useEffect(() => {
    void loadHistory()
    void refreshInsights()
  }, [loadHistory, refreshInsights])

  // 停用经验列表：优先用全量视图（无 200 条上限），拿不到时退回接口给的最近停用。
  // 两个来源字段结构一致，可互换渲染。
  const deactivated = allInsights.length > 0
    ? allInsights.filter((i) => i.active !== 1)
    : deactivatedInsights

  const removeInsight = async (id: number, rule: string) => {
    toast.confirm(
      `删除这条经验？\n\n${rule}\n\n删除后不可恢复（重置模拟盘不会恢复它）。`,
      async () => {
        setDeletingId(id)
        setError(null)
        try {
          await apiSend(`/review/insights/${id}`, 'DELETE')
          await refreshInsights()
          toast.success('已删除经验 #' + (id ?? ''))
        } catch (cause) {
          const msg = cause instanceof Error ? cause.message : '删除失败'
          setError(msg)
          toast.error('删除失败：' + msg)
        } finally {
          setDeletingId(null)
        }
      },
    )
  }

  // 手动启用/停用一条经验。自动停用是单向的（停用后永不被重评），
  // 所以这是把有价值经验挑回来的唯一出口。
  const toggleInsight = async (id: number) => {
    setTogglingId(id)
    setError(null)
    try {
      await apiSend(`/review/insights/${id}/toggle`, 'PATCH')
      await refreshInsights()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '切换失败')
    } finally {
      setTogglingId(null)
    }
  }

  const removeByOrigin = async (origin: string, label: string, count: number) => {
    toast.confirm(
      `删除全部「${label}」经验（${count} 条）？\n\n删除后不可恢复。`,
      async () => {
        setError(null)
        try {
          await apiSend(`/review/insights/by-origin/${origin}`, 'DELETE')
          await refreshInsights()
          toast.success(`已批量删除「${label}」 ${count} 条经验`)
        } catch (cause) {
          const msg = cause instanceof Error ? cause.message : '批量删除失败'
          setError(msg)
          toast.error('批量删除失败：' + msg)
        }
      },
    )
  }

  const run = async () => {
    setLoading(true)
    setError(null)
    try {
      // v290 P0+：复盘时段走 periodToQuery(period) —— 与顶部 ReviewOverview、
      // 块 B ReviewHistoryPanel 严格同源同窗，杜绝"两套数据打架"。
      const params = periodToQuery(period)
      // 后端 run endpoint 仍保留 scope 字段（向后兼容）—— 窗口非空时它会被忽略。
      params.set('scope', 'all')
      const data = await apiSend<ReviewReport>(`/review/run?${params.toString()}`, 'POST')
      setReport(data)
      void loadHistory()
      void refreshInsights()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '复盘失败')
    } finally {
      setLoading(false)
    }
  }

  const stats = report?.failures?.stats ?? {}
  const statTotal = Object.values(stats).reduce((a, b) => a + b, 0)
  const succStats = report?.successes?.stats ?? {}
  const succTotal = Object.values(succStats).reduce((a, b) => a + b, 0)
  // 当期聚合：真实口径在 summary._aggregate（顶层 aggregate 字段已废弃为空）
  const curAgg = (report?.summary as {
    _aggregate?: { total?: number; decided?: number; winrate?: number | null; total_pnl?: number } | null
  } | null)?._aggregate ?? null
  return (
    <div className="surface-card shrink-0 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">A · 历史真实交易复盘</h3>
        <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[9px] text-primary">
          数据 = ai_decisions 闸门后真实持仓
        </span>
        {/* v290 P0+：删掉内部 scope 选择器；只显示当前全局时段徽章，
            顶部 PeriodSelector 一切,这里立即跟随。 */}
        <span
          className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[9px] text-primary"
          title={`本面板时段由顶部 PeriodSelector 控制（当前 ${period.label}）`}
        >
          时段 = {period.label}
        </span>
        <button
          type="button"
          onClick={() => void run()}
          disabled={loading}
          className="rounded border border-primary/50 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary disabled:cursor-wait disabled:opacity-60"
        >
          {loading ? 'AI 复盘中…（可能需要 1-3 分钟）' : '运行复盘'}
        </button>
        {report?.generated_at && (
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">
            {/* v290 P0+：显示全局时段（period.label）而不是 report.scope（后端旧字段），
                保证面板时段徽章与运行结果口径完全一致。 */}
            {period.label}复盘
            {report.samples > 0 ? ` · 真实持仓 ${report.samples} 笔` : ''}
            {report.new_insights ? ` · 新经验 +${report.new_insights}` : ''}
          </span>
        )}
      </div>

      {/* AI 分析当前参考：经验总结（来源=历史真实交易复盘；参考信号在块 B）
          显示条件：生效经验 或 停用经验 任一存在 —— 全停用时也要能查看/手动删除，
          否则用户看不到（也无法清理）那些已停用的旧经验。 */}
      {(allInsights.length > 0 || currentInsights.length > 0 || deactivatedInsights.length > 0) && (
        <div className="mt-2">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-widest text-long/80">
              AI 分析当前参考
            </span>
            <button
              type="button"
              onClick={() => setShowInsights((v) => !v)}
              className="rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground"
            >
              {showInsights ? '收起 ▾' : '展开 ▸'}
            </button>
            <span className="ml-auto font-mono text-[9px] text-muted-foreground/70">
              {allInsights.length > 0
                ? `${allInsights.filter((i) => i.active === 1).length} 条生效 · ${allInsights.filter((i) => i.active !== 1).length} 条停用`
                : `${currentInsights.length} 条生效 · ${deactivatedInsights.length} 条停用`}
            </span>
          </div>
          {showInsights && (
            <div className="mt-1.5">
              {/* 经验总结（来源=历史真实交易复盘；参考信号已移入块 B） */}
              <div className="rounded-md border border-long/25 bg-long/5 p-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-long/80">
                    经验总结（{allInsights.length > 0 ? allInsights.filter((i) => i.active === 1).length : currentInsights.length} 条生效）
                  </span>
                  <div className="flex items-center gap-1 font-mono text-[9px]">
                    {([
                      ['all', '全部'],
                      ['real', '历史交易'],
                      ['quick_sim', '快速评测'],
                      ['rule', '归因'],
                    ] as const).map(([key, label]) => (
                      <button
                        key={key}
                        type="button"
                        onClick={() => setOriginFilter(key)}
                        className={`rounded border px-1.5 py-px transition ${
                          originFilter === key
                            ? 'border-long/60 bg-long/15 text-long'
                            : 'border-border text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  {(() => {
                    const qsTotal = allInsights.filter((i) => i.origin === 'quick_sim').length
                    return qsTotal > 0 ? (
                      <button
                        type="button"
                        onClick={() => void removeByOrigin('quick_sim', '快速评测', qsTotal)}
                        title="删除全部来源=快速评测的经验（重置模拟盘不会自动清理它们）"
                        className="ml-auto rounded border border-amber-500/40 px-1.5 py-px font-mono text-[9px] text-amber-500 hover:bg-amber-500/10"
                      >
                        清空快速评测经验（{qsTotal}）
                      </button>
                    ) : null
                  })()}
                </div>
                <div className="mt-0.5 flex items-center gap-2 font-mono text-[9px] text-muted-foreground/60">
                  <span className="min-w-0 flex-1">
                    每条左侧标记来源；停用的经验不再回灌 AI，只有样本数最高的前 8 条真正注入 AI 分析。
                  </span>
                  {(() => {
                    const fullCount = allInsights.length > 0
                      ? allInsights.filter((i) => i.active === 1).length
                      : currentInsightItems.length
                    return fullCount > 8 ? (
                      <button
                        type="button"
                        onClick={() => setShowAllActive((v) => !v)}
                        className="shrink-0 rounded border border-border px-1.5 py-px text-muted-foreground hover:text-foreground"
                      >
                        {showAllActive ? '只看注入前 8 条' : `展开全部 ${fullCount} 条`}
                      </button>
                    ) : null
                  })()}
                </div>

                {allInsights.length === 0 && currentInsights.length === 0 && (
                  <div className="mt-1 space-y-1">
                    <div className="font-mono text-[10px] text-muted-foreground">暂无生效经验</div>
                    <div className="text-[10px] leading-relaxed text-muted-foreground/70">
                      可能原因：旧经验被自动停用（加入后全局胜率 &lt;40%）、本次复盘 LLM 生成失败、或复盘样本不足（&lt;5）。
                      点击"运行复盘"重新生成。
                    </div>
                  </div>
                )}

                {/* 生效经验：默认收起到注入前 8 条，可按来源筛选、展开全部 */}
                {(() => {
                  const fullActive = allInsights.length > 0
                    ? allInsights.filter((i) => i.active === 1)
                    : currentInsightItems
                  if (fullActive.length === 0) return null
                  const injected = currentInsightItems.length > 0
                    ? currentInsightItems
                    : fullActive.slice(0, 8)
                  const baseList = showAllActive || allInsights.length === 0 ? fullActive : injected
                  const activeList = originFilter === 'all'
                    ? baseList
                    : baseList.filter((i) => i.origin === originFilter)
                  if (activeList.length === 0) {
                    return (
                      <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                        该来源下暂无生效经验
                      </div>
                    )
                  }
                  return (
                    <ul className="mt-1 space-y-1">
                      {activeList.map((item) => (
                        <li key={item.id} className="flex items-start gap-1.5">
                          <OriginBadge origin={item.origin} />
                          <span className="min-w-0 flex-1 text-[11px] leading-relaxed text-foreground">✦ {item.rule}</span>
                          <button
                            type="button"
                            onClick={() => void toggleInsight(item.id)}
                            disabled={togglingId === item.id}
                            title="停用这条经验（不再回灌 AI 分析，可随时重新启用）"
                            className="shrink-0 rounded border border-border px-1 py-px font-mono text-[9px] text-muted-foreground hover:bg-secondary disabled:opacity-50"
                          >
                            {togglingId === item.id ? '…' : '停用'}
                          </button>
                          <button
                            type="button"
                            onClick={() => void removeInsight(item.id, item.rule)}
                            disabled={deletingId === item.id}
                            className="shrink-0 rounded border border-short/40 px-1 py-px font-mono text-[9px] text-short hover:bg-short/10 disabled:opacity-50"
                          >
                            {deletingId === item.id ? '…' : '删除'}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )
                })()}

                {/* 停用经验：同样可逐条删除（此前只能看不能删） */}
                {deactivated.length > 0 && (
                  <div className="mt-1.5 border-t border-border/40 pt-1">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground/60">
                        已停用（{deactivated.length} 条 · 非丢失，不再回灌 AI）
                      </span>
                      {deactivated.length > 5 && (
                        <button
                          type="button"
                          onClick={() => setShowAllDeactivated((v) => !v)}
                          className="rounded border border-border px-1.5 py-px font-mono text-[9px] text-muted-foreground hover:text-foreground"
                        >
                          {showAllDeactivated ? '只看最近 5 条' : `展开全部 ${deactivated.length} 条`}
                        </button>
                      )}
                    </div>
                    <ul className="mt-0.5 space-y-0.5">
                      {(showAllDeactivated ? deactivated : deactivated.slice(0, 5))
                        .filter((item) => originFilter === 'all' || item.origin === originFilter)
                        .map((item, i) => (
                        <li key={item.id ?? i} className="flex items-start gap-1.5">
                          <OriginBadge origin={item.origin} />
                          <span className="min-w-0 flex-1 text-[10px] leading-relaxed text-muted-foreground/50 line-through">
                            ✖ {item.rule}
                          </span>
                          {typeof item.id === 'number' && (
                            <>
                              <button
                                type="button"
                                onClick={() => void toggleInsight(item.id as number)}
                                disabled={togglingId === item.id}
                                title="重新启用这条经验（自动停用是单向的，停用后不会自动恢复）"
                                className="shrink-0 rounded border border-long/40 px-1 py-px font-mono text-[9px] text-long hover:bg-long/10 disabled:opacity-50"
                              >
                                {togglingId === item.id ? '…' : '启用'}
                              </button>
                              <button
                                type="button"
                                onClick={() => void removeInsight(item.id as number, item.rule)}
                                disabled={deletingId === item.id}
                                className="shrink-0 rounded border border-short/30 px-1 py-px font-mono text-[9px] text-short/80 hover:bg-short/10 disabled:opacity-50"
                              >
                                {deletingId === item.id ? '…' : '删除'}
                              </button>
                            </>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 text-[11px] text-short">{error}</div>
      )}

      {report && (
        <div className="mt-2 grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div className="rounded-md border border-border bg-card p-2.5">
            <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              复盘总结（生成于 {report.generated_at}）
            </div>
            {report.summary?.overview && (
              <div className="text-[11px] leading-relaxed text-foreground">{report.summary.overview}</div>
            )}
            <div className="mt-2 grid grid-cols-2 gap-3 font-mono text-[11px]">
              <span>真实持仓 <b className="text-foreground">{curAgg?.total ?? report.samples ?? 0}</b> 笔</span>
              <span>胜率 <b className="text-foreground">{curAgg?.winrate != null ? `${(curAgg.winrate * 100).toFixed(1)}%` : '—'}</b></span>
              <span>总盈亏 <b className={(curAgg?.total_pnl ?? 0) >= 0 ? 'text-long' : 'text-short'}>{curAgg?.total_pnl != null ? `${curAgg.total_pnl > 0 ? '+' : ''}${curAgg.total_pnl.toFixed(2)}%` : '—'}</b></span>
              <span>已判定 <b className="text-foreground">{curAgg?.decided ?? 0}</b></span>
            </div>
            <div className="mt-2 space-y-2">
              <ListBlock title="强项" items={report.summary?.strengths} tone="good" />
              <ListBlock title="弱项" items={report.summary?.weaknesses} tone="bad" />
              <ListBlock title="规律发现" items={report.summary?.patterns} />
              <PatternAuditBlock audits={report.summary?.pattern_audits} />
              <ListBlock title="改进建议" items={report.summary?.improvements} />
              <ListBlock
                title="逻辑推理反思（本次交易逻辑链条的自我审视）"
                items={report.summary?.logical_reflection}
                tone="plain"
              />
              <ListBlock
                title="第一性原理总结规律（不依赖样本归纳的底层规律）"
                items={report.summary?.first_principles}
                tone="plain"
              />
            </div>

            {report.feedback && (report.feedback.before || report.feedback.after) && (
              <div className="mt-2 rounded-md border border-border bg-card p-2.5">
                <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                  回灌效果验证{report.feedback.insight_started_at ? '' : '（尚无生效经验）'}
                </div>
                <div className="grid grid-cols-3 gap-2 font-mono text-[11px]">
                  <span>回灌前胜率 <b className="text-foreground">{report.feedback.before?.winrate != null ? `${(report.feedback.before.winrate * 100).toFixed(1)}%` : '—'}</b></span>
                  <span>回灌后胜率 <b className="text-foreground">{report.feedback.after?.winrate != null ? `${(report.feedback.after.winrate * 100).toFixed(1)}%` : '—'}</b></span>
                  <span>变化 <b className={(report.feedback.winrate_delta ?? 0) >= 0 ? 'text-long' : 'text-short'}>
                    {report.feedback.winrate_delta != null ? `${report.feedback.winrate_delta >= 0 ? '+' : ''}${(report.feedback.winrate_delta * 100).toFixed(1)}%` : '—'}
                  </b></span>
                </div>
                {(report.feedback.before?.total ?? 0) > 0 && (report.feedback.after?.total ?? 0) > 0 && (
                  <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                    样本 {report.feedback.before?.total ?? 0} → {report.feedback.after?.total ?? 0} · 总盈亏
                    {report.feedback.before?.total_pnl != null ? ` ${report.feedback.before.total_pnl > 0 ? '+' : ''}${report.feedback.before.total_pnl.toFixed(2)}%` : ''}
                    {' → '}
                    {report.feedback.after?.total_pnl != null ? ` ${report.feedback.after.total_pnl > 0 ? '+' : ''}${report.feedback.after.total_pnl.toFixed(2)}%` : ''}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="space-y-3">
            {report.models && report.models.length > 0 && (
              <div className="rounded-md border border-border bg-card p-2.5">
                <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                  模型维度（胜率 / 盈亏）
                </div>
                <div className="space-y-1 font-mono text-[10px]">
                  {report.models.slice(0, 6).map((m) => (
                    <div key={m.model} className="flex items-center gap-2">
                      <span className="w-32 shrink-0 truncate text-muted-foreground" title={m.model}>{m.model}</span>
                      <span className="w-10 shrink-0 text-right">{m.total} 笔</span>
                      <span className={`w-12 shrink-0 text-right ${(m.winrate ?? 0) >= 0.5 ? 'text-long' : 'text-short'}`}>
                        {m.winrate != null ? `${(m.winrate * 100).toFixed(0)}%` : '—'}
                      </span>
                      <span className={`text-right ${(m.total_pnl ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>
                        {m.total_pnl != null ? `${m.total_pnl > 0 ? '+' : ''}${m.total_pnl.toFixed(2)}%` : '—'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="rounded-md border border-border bg-card p-2.5">
              <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                失败原因归类（真实持仓 {report.failures?.analyzed ?? 0}/{report.failures?.failed_trades ?? 0} 笔）
              </div>
              {statTotal > 0 ? (
                <div className="space-y-1">
                  {Object.entries(stats).sort((a, b) => b[1] - a[1]).map(([reason, count]) => {
                    const pct = statTotal ? Math.round(count / statTotal * 100) : 0
                    return (
                      <div key={reason} className="flex items-center gap-2 font-mono text-[10px]">
                        <span className="w-24 shrink-0 text-muted-foreground">{reason}</span>
                        <div className="h-2 flex-1 overflow-hidden rounded bg-secondary">
                          <div className="h-full rounded bg-short/60" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="w-14 shrink-0 text-right text-foreground">{count} 笔 · {pct}%</span>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <div className="font-mono text-[11px] text-muted-foreground">
                  {report.failures?.failed_trades ? '归因分析中（部分失败单未归类）' : '本期无失败单'}
                </div>
              )}
            </div>

            <div className="rounded-md border border-long/30 bg-long/5 p-2.5">
              <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-long/80">
                成功原因归类（真实持仓 {report.successes?.analyzed ?? 0}/{report.successes?.won_trades ?? 0} 笔）
              </div>
              {succTotal > 0 ? (
                <div className="space-y-1">
                  {Object.entries(succStats).sort((a, b) => b[1] - a[1]).map(([reason, count]) => {
                    const pct = succTotal ? Math.round(count / succTotal * 100) : 0
                    return (
                      <div key={reason} className="flex items-center gap-2 font-mono text-[10px]">
                        <span className="w-24 shrink-0 text-muted-foreground">{reason}</span>
                        <div className="h-2 flex-1 overflow-hidden rounded bg-secondary">
                          <div className="h-full rounded bg-long/60" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="w-14 shrink-0 text-right text-foreground">{count} 笔 · {pct}%</span>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <div className="font-mono text-[11px] text-muted-foreground">
                  {report.successes?.won_trades ? '归因分析中（部分成功单未归类）' : '本期无成功单'}
                </div>
              )}
            </div>

            {report.insights && report.insights.length > 0 && (
              <div className="rounded-md border border-primary/30 bg-primary/5 p-2.5">
                <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-primary/80">
                  经验规则（已自动回灌后续 AI 分析）
                </div>
                {/* 按来源分组展示：历史交易提炼 / 快速评测提炼 */}
                {(report.insights_by_origin?.real?.length || 0) > 0 && (
                  <div className="mb-1">
                    <div className="font-mono text-[9px] text-muted-foreground">▍来源：历史交易复盘</div>
                    <ul className="space-y-0.5">
                      {report.insights_by_origin!.real!.map((rule, i) => (
                        <li key={`r${i}`} className="text-[11px] leading-relaxed text-foreground">✦ {rule}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {(report.insights_by_origin?.quick_sim?.length || 0) > 0 && (
                  <div>
                    <div className="font-mono text-[9px] text-amber-500/80">▍来源：快速评测提炼（{qsHorizon} 分钟模拟）</div>
                    <ul className="space-y-0.5">
                      {report.insights_by_origin!.quick_sim!.map((rule, i) => (
                        <li key={`q${i}`} className="text-[11px] leading-relaxed text-foreground">✦ {rule}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {(!report.insights_by_origin || (!report.insights_by_origin.real?.length && !report.insights_by_origin.quick_sim?.length)) && (
                  <ul className="space-y-0.5">
                    {report.insights.map((rule, i) => (
                      <li key={i} className="text-[11px] leading-relaxed text-foreground">✦ {rule}</li>
                    ))}
                  </ul>
                )}
                {report.insight_evaluation?.deactivated && report.insight_evaluation.deactivated.length > 0 && (
                  <div className="mt-1.5 border-t border-short/30 pt-1.5 text-[10px] text-short">
                    已自动停用 {report.insight_evaluation.deactivated.length} 条低效经验（样本≥10 且胜率&lt;40%）
                  </div>
                )}
                {report.insight_evaluation?.reactivated && report.insight_evaluation.reactivated.length > 0 && (
                  <div className="mt-1.5 border-t border-long/30 pt-1.5 text-[10px] text-long">
                    已自动恢复 {report.insight_evaluation.reactivated.length} 条经验（命中胜率回升≥55%）
                  </div>
                )}
                {report.insight_evaluation?.rescued_no_sample && report.insight_evaluation.rescued_no_sample.length > 0 && (
                  <div className="mt-1.5 border-t border-hold/30 pt-1.5 text-[10px] text-hold">
                    已恢复 {report.insight_evaluation.rescued_no_sample.length} 条「停用但无命中样本」的经验
                    （旧连坐停用遗留，无任何针对它的证据；样本够了若胜率差会再次停用）
                  </div>
                )}
              </div>
            )}

            {/* 快速评测提炼的提示词/模型调整建议（不自动改，供参考） */}
            {report.prompt_suggestions && report.prompt_suggestions.length > 0 && (
              <div className="rounded-md border border-indigo-500/30 bg-indigo-500/5 p-2.5">
                <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-indigo-500/80">
                  提示词/模型建议（来自快速评测 · 不自动改，供参考）
                </div>
                <ul className="space-y-0.5">
                  {report.prompt_suggestions.map((s, i) => (
                    <li key={i} className="text-[11px] leading-relaxed text-foreground">💡 {s}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {history.length > 0 && report && (
        <div className="mt-2 flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
          <span>历史复盘：</span>
          {history.map((h) => (
            <button
              key={h.report_id ?? h.generated_at}
              type="button"
              onClick={() => setReport(h)}
              className={`rounded border px-1.5 py-0.5 transition ${
                report.report_id === h.report_id
                  ? 'border-primary bg-primary/15 text-primary'
                  : 'border-border text-muted-foreground hover:text-foreground'
              }`}
            >
              {h.scope === 'today' ? '今日' : h.scope === 'week' ? '本周' : '全部'} · {h.generated_at?.slice(5, 16)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
