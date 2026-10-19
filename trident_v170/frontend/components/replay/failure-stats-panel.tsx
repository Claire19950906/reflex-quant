'use client'

import { useState } from 'react'
import { apiSend } from '@/lib/api'
import { useApiPoll } from '@/lib/hooks/use-api-poll'
import { useSegmentDrift } from '@/lib/hooks/use-segment-drift'
import { SegmentHistoryHeatmap } from './segment-history-heatmap'
import { FailureClusteringPanel } from './failure-clustering-panel'
import { RotateCcw, RotateCw, Loader2 } from 'lucide-react'
import { useToast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import { SegmentDriftBadge } from './segment-drift-badge'
import type { ReviewListResp } from '@/lib/types-reasoning-review'

// 失败原因统一汇总面板 — 跨来源（新闻回溯 + 复盘归因）聚合 attribution_reviews，
// 展示「哪类失败最多」的统计排行。
//
// v170 P0+ 数据拉取迁移：从手写 useEffect + setInterval + alive flag + 自管理 setState
// 改为 useApiPoll ——
//   · 自动 mounted 检测（替代手写 alive flag）
//   · inFlight flag 防上一轮未结束就被新一轮覆盖
//   · intervalMs + deps 与 9 面板对齐
type ReasonRow = { code: number; name: string; count: number; pct: number }
type StatsResp = {
  total: number
  sources: Record<string, number>
  statuses: Record<string, number>
  reasons: ReasonRow[]
  news_count?: number
  review_count?: number
  reset_count?: number
}

function StatCell({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="rounded-md border border-border bg-card p-2">
      <div className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={`mt-0.5 font-mono text-lg ${accent ? 'text-short' : 'text-foreground'}`}>{value}</div>
    </div>
  )
}

// 失败统计 60s 轮询间隔。与失败原因变化频次匹配（人工纠错 / 撤销重置触发后立即刷新即可）。
const FAILURE_STATS_POLL_MS = 60_000

export function FailureStatsPanel({
  // v200 IA：点击排行行 → 调用 onSelectCode(code)，父组件负责滚动 + 预填过滤码
  onSelectCode,
}: { onSelectCode?: (code: number) => void } = {}) {
  const { toast } = useToast()
  const {
    data,
    error,
    loading,
    refresh,
  } = useApiPoll<StatsResp>('/review/attributions/stats', {
    intervalMs: FAILURE_STATS_POLL_MS,
  })
  // v302+ P1: 拉近期 reviews(limit=20, 7 天)做段位漂移分析
  // 与 attributions/stats 共用 60s 轮询,避免请求放大
  const { data: reviewData } = useApiPoll<ReviewListResp>(
    '/reasoning/review/list?limit=20&days=7',
    { intervalMs: FAILURE_STATS_POLL_MS },
  )
  const driftResult = useSegmentDrift(reviewData?.items)
  // 重置确认 UI 三态：closed → open（确认弹窗）→ submit（请求中）
  const [resetOpen, setResetOpen] = useState(false)
  const [resetScope, setResetScope] = useState<'all' | 'news' | 'review'>('all')
  const [resetDays, setResetDays] = useState<number>(0)
  const [submitting, setSubmitting] = useState(false)
  const [resetMsg, setResetMsg] = useState<string | null>(null)

  const handleReset = async () => {
    toast.confirm(
      `确认重置「${resetScope === 'all' ? '全部' : resetScope === 'news' ? '仅新闻回溯' : '仅复盘归因'}」`
      + `${resetDays > 0 ? ` ${resetDays} 天前` : ''} 的 pending 归因？\n`
      + `数据将软删除（human_status=reset），不计入统计，但数据库保留可恢复。`,
      async () => {
        setSubmitting(true)
        setResetMsg(null)
        try {
          const j = await apiSend<{ reset: number; before: number }>(
            '/review/attributions/reset',
            'POST',
            { scope: resetScope, days: resetDays, confirm: true },
          )
          setResetMsg(`✓ 已重置 ${j.reset} 条（${j.before} 条 pending 中的 ${j.reset} 条归档）`)
          setResetOpen(false)
          toast.success(`已重置 ${j.reset} 条归档归因`)
          void refresh() // 立即刷新（不等下次 60s 轮询）
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e)
          setResetMsg(`✗ 重置失败：${msg}`)
          toast.error('重置失败：' + msg)
        } finally {
          setSubmitting(false)
        }
      },
    )
  }

  const handleUnreset = async () => {
    const resetCount = data?.reset_count ?? 0
    if (resetCount <= 0) {
      setResetMsg('✗ 当前没有 reset 记录可恢复')
      return
    }
    toast.confirm(
      `确认撤销「${resetScope === 'all' ? '全部' : resetScope === 'news' ? '仅新闻回溯' : '仅复盘归因'}」`
      + `${resetDays > 0 ? ` ${resetDays} 天前` : ''} 的 reset 归因？\n`
      + `这些记录将从归档恢复为 pending，重新进入复核队列。`,
      async () => {
        setSubmitting(true)
        setResetMsg(null)
        try {
          const j = await apiSend<{ unreset: number }>(
            '/review/attributions/unreset',
            'POST',
            { scope: resetScope, days: resetDays, confirm: true },
          )
          setResetMsg(`↺ 已恢复 ${j.unreset} 条归档归因（重新进入 pending 复核队列）`)
          toast.success(`已恢复 ${j.unreset} 条归档归因`)
          void refresh() // 立即刷新（不等下次 60s 轮询）
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e)
          setResetMsg(`✗ 撤销重置失败：${msg}`)
          toast.error('撤销重置失败：' + msg)
        } finally {
          setSubmitting(false)
        }
      },
    )
  }

  const reasons = data?.reasons ?? []
  const maxCount = Math.max(1, ...reasons.map((r) => r.count))
  const top = reasons[0]
  const resetCount = data?.reset_count ?? 0

  return (
    <section className="surface-card p-3">
      <div className="flex items-center gap-2">
        <h2 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">
          失败原因统一汇总
        </h2>
        <span className="rounded border border-short/40 bg-short/10 px-2 py-0.5 font-mono text-[10px] text-short">
          FAILURE STATS
        </span>
        {driftResult.alerts.length > 0 && (
          <span className="ml-1" title="近期 20 条 reviews 的 8 段命中率 vs 6 月基线;偏差越大 = 当前批次越偏科">
            <SegmentDriftBadge result={driftResult} max={2} compact />
          </span>
        )}
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
        把新闻回溯归因 + 复盘归因统一汇总（最终归因以人工纠错为准），一眼看清哪类失败最多。
      </p>
      {/* v180 P0+：数据通道统一口径 §6 — 标明源表 + 时区 */}
      <span className="mt-1 inline-block rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[9px] text-primary">
        数据 = [真实] attribution_reviews（全量累计）· 北京时区
      </span>

      {/* 数据来源 + 重置入口：让用户能一键归档 pending 归因（不删数据） */}
      {data && data.total > 0 && (
        <div className="mt-2 space-y-2 rounded border border-border bg-card p-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] text-muted-foreground">
              📌 这些数字 = attribution_reviews 表里<strong>真实累计</strong>的归因记录
              （news {data.sources?.news ?? 0} + review {data.review_count ?? 0}，
              human_status 分布：待复核 <b>{data.statuses?.pending ?? 0}</b> ·
              已确认 <b>{data.statuses?.agree ?? 0}</b> ·
              已纠错 <b>{data.statuses?.corrected ?? 0}</b>，
              reset 不计入 <span className="text-muted-foreground/70">（已归档 {(data.reset_count ?? 0)} 条，可恢复）</span>）
            </span>
            <button
              type="button"
              onClick={() => { setResetOpen(!resetOpen); setResetMsg(null) }}
              className="ml-auto inline-flex items-center gap-1 rounded border border-short/40 bg-short/10 px-2 py-1 font-mono text-[10px] text-short hover:bg-short/20"
              title="重置（软删除）pending 归因 — 数据保留可恢复"
            >
              <RotateCcw className="h-3 w-3" />
              重置 pending ({data.statuses?.pending ?? 0})
            </button>
            <button
              type="button"
              onClick={handleUnreset}
              disabled={submitting || resetCount <= 0}
              className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:bg-accent disabled:opacity-40"
              title="撤销重置：把 human_status=reset 恢复为 pending，重新进入复核队列"
            >
              <RotateCw className="h-3 w-3" />
              撤销重置 ({resetCount})
            </button>
            <a
              href="/review"
              className="inline-flex items-center rounded border border-border px-2 py-1 font-mono text-[10px] text-muted-foreground hover:bg-accent"
              title="跳到复盘页复核归因（逐条 agree/corrected）"
            >
              去复核
            </a>
          </div>

          {resetOpen && (
            <div className="rounded border border-short/30 bg-short/5 p-2 font-mono text-[11px]">
              <div className="mb-2 text-short">⚠ 重置范围（仅影响 human_status=pending，归档后不计入统计）：</div>
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-1">
                  <input type="radio" name="scope" value="all" checked={resetScope === 'all'}
                    onChange={() => setResetScope('all')} />
                  全部
                </label>
                <label className="flex items-center gap-1">
                  <input type="radio" name="scope" value="news" checked={resetScope === 'news'}
                    onChange={() => setResetScope('news')} />
                  仅新闻回溯
                </label>
                <label className="flex items-center gap-1">
                  <input type="radio" name="scope" value="review" checked={resetScope === 'review'}
                    onChange={() => setResetScope('review')} />
                  仅复盘归因
                </label>
                <span className="mx-2 text-muted-foreground">|</span>
                <label className="flex items-center gap-1">
                  天数:
                  <select
                    value={resetDays}
                    onChange={(e) => setResetDays(Number(e.target.value))}
                    className="rounded border border-border bg-background px-1 py-0.5 text-[10px]"
                  >
                    <option value={0}>全部</option>
                    <option value={7}>仅 7 天前</option>
                    <option value={30}>仅 30 天前</option>
                    <option value={90}>仅 90 天前</option>
                  </select>
                </label>
                <button
                  type="button"
                  onClick={handleReset}
                  disabled={submitting}
                  className="ml-auto inline-flex items-center gap-1 rounded bg-short px-2 py-1 text-[10px] text-white hover:bg-short/80 disabled:opacity-50"
                >
                  {submitting && <Loader2 className="h-3 w-3 animate-spin" />}
                  {submitting ? '处理中...' : '确认归档'}
                </button>
              </div>
            </div>
          )}

          {resetMsg && (
            <div className={`rounded border p-1.5 font-mono text-[10px] ${resetMsg.startsWith('✓') ? 'border-long/30 bg-long/10 text-long' : 'border-short/30 bg-short/10 text-short'}`}>
              {resetMsg}
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="mt-2 rounded border border-short/40 bg-short/10 p-2 font-mono text-[11px] text-short">
          加载失败：{error}
        </div>
      )}

      {!error && !data && loading && (
        <div className="mt-2 rounded border border-border bg-card p-2 font-mono text-[11px] text-muted-foreground">
          加载失败原因统计中…
        </div>
      )}

      {!error && data && data.total === 0 && (
        <div className="mt-2 rounded border border-border bg-card p-2 font-mono text-[11px] text-muted-foreground">
          暂无失败归因数据（先运行复盘或新闻归因后再查看）。
        </div>
      )}

      {!error && data && data.total > 0 && (
        <>
          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <StatCell label="失败总数" value={String(data.total)} />
            <StatCell label="复盘归因" value={String(data.sources?.review ?? 0)} />
            <StatCell label="新闻回溯" value={String(data.sources?.news ?? 0)} />
            <StatCell label="待人工复核" value={String(data.statuses?.pending ?? 0)} />
          </div>

          {top && (
            <div className="mt-2 rounded-md border border-border bg-card p-2.5 font-mono">
              <span className="text-[11px] text-muted-foreground">出现次数最多的失败原因（仅供参考，不代表主次）：</span>
              <span className="ml-1 text-[12px] font-bold text-foreground">
                {top.code} {top.name}
              </span>
              <span className="ml-1 text-[11px] text-muted-foreground">
                {top.count} 次 · {top.pct}%
              </span>
            </div>
          )}

          {/* v302+ P1: 段位漂移小结 — 列出与历史基线偏离最大的前 3 个段,
              给操作员一个"为何失败集中在这类"的归因入口。空数据时不渲染。 */}
          {driftResult.alerts.length > 0 && (
            <div className="mt-2 rounded-md border border-border bg-card p-2.5 font-mono">
              <div className="text-[11px] text-muted-foreground">
                📊 段位漂移 — 近期 {driftResult.total} 条 reviews vs 6 月基线,前 {Math.min(3, driftResult.alerts.length)} 个偏离:
              </div>
              <div className="mt-1 flex flex-wrap gap-1">
                {driftResult.alerts.slice(0, 3).map((a) => {
                  // delta > 0 = 当前命中率比基线高(超量);< 0 = 低(缺测)
                  // 字段是 0..1 比例,显示时 ×100 取整为百分点
                  const currentPct = Math.round(a.current * 100)
                  const deltaPp = Math.round(a.delta * 100)
                  const tone = a.delta >= 0
                    ? 'text-long bg-long/10 border-long/30'
                    : 'text-short bg-short/10 border-short/30'
                  return (
                    <span
                      key={a.stage}
                      className={cn(
                        'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]',
                        tone,
                      )}
                      title={`基线 ${Math.round(a.baseline * 100)}% · 当前 ${currentPct}% · 偏差 ${deltaPp > 0 ? '+' : ''}${deltaPp}pp · 严重度 ${a.severity}`}
                    >
                      <span className="font-bold">{a.stage}</span>
                      <span>
                        {currentPct}% ({deltaPp > 0 ? '+' : ''}{deltaPp}pp)
                      </span>
                    </span>
                  )
                })}
              </div>
              <p className="mt-1 text-[10px] text-muted-foreground/80">
                ↑ 缺失段位(偏差负)可能是失败根因;详见段位漂移 / 复盘失败归因
              </p>
            </div>
          )}

          {/* v302+ P2 PR-16: 段位一致性快照 — 7 天 × 8 段热力图,放在"段位漂移"块下面,
              让操作员看到"当前 vs 7 天趋势"的关系,而不只是"当前 vs 静态基线"。 */}
          <div className="mt-2">
            <SegmentHistoryHeatmap days={7} />
          </div>

          {/* v302+ P2 PR-17: 失败模式聚类 — 30 天错误码聚类(趋势/品种/example)
              放在"段位一致性快照"之后,与 PR-16 共同构成"全景失败视图":
              · PR-16: 8 段命中率 × 7 天(维度=段)
              · PR-17: N 错误码 × 趋势/品种(维度=错误模式) */}
          <div className="mt-2">
            <FailureClusteringPanel days={30} min_count={2} topN={5} />
          </div>

          <div className="mt-2 space-y-1">
            {reasons.map((r) => {
              const clickable = typeof onSelectCode === 'function'
              const handle = () => {
                if (!clickable) return
                onSelectCode!(r.code)
                // 滚到下方 ReflectionErrorsPanel（id="reflection-errors"）
                if (typeof window !== 'undefined') {
                  window.requestAnimationFrame(() => {
                    document
                      .getElementById('reflection-errors')
                      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                  })
                }
              }
              return (
                <div
                  key={r.code}
                  role={clickable ? 'button' : undefined}
                  tabIndex={clickable ? 0 : undefined}
                  onClick={clickable ? handle : undefined}
                  onKeyDown={
                    clickable
                      ? (e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            handle()
                          }
                        }
                      : undefined
                  }
                  className={`flex items-center gap-2 font-mono text-[10px] ${
                    clickable ? 'cursor-pointer rounded hover:bg-secondary/40' : ''
                  }`}
                  aria-label={
                    clickable ? `按错误码 ${r.code} 过滤下方错误实例` : undefined
                  }
                >
                  <span
                    className="w-28 shrink-0 truncate text-foreground"
                    title={`${r.code} ${r.name}`}
                  >
                    {r.code} {r.name}
                  </span>
                  <div className="h-2.5 flex-1 overflow-hidden rounded bg-secondary">
                    <div
                      className="h-full rounded bg-muted-foreground/40"
                      style={{ width: `${Math.max((r.count / maxCount) * 100, 2)}%` }}
                    />
                  </div>
                  <span className="w-16 shrink-0 text-right text-foreground">
                    {r.count} 笔 · {r.pct}%
                  </span>
                  {clickable && (
                    <span aria-hidden className="text-[9px] text-muted-foreground/70">↳</span>
                  )}
                </div>
              )
            })}
            {reasons.length === 0 && (
              <div className="font-mono text-[11px] text-muted-foreground">有失败单但尚未完成归因。</div>
            )}
          </div>
        </>
      )}
    </section>
  )
}