'use client'

import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiSend } from '@/lib/api'
import {
  periodToQuery,
  type PeriodRange,
} from '@/lib/data-channel-standards'
import { useQuickSimHorizon } from '@/lib/hooks/use-quick-sim-horizon'
import { useApiPoll } from '@/lib/hooks/use-api-poll'
import type { ReviewSignals, NewsTrade, TradeRow, NewsTradeDetail } from '@/lib/types-review-history'
import { SignalGroup, fmtTs, fmtTime, fmtPct, TradeStatusBadge, TradeSparkline, NewsListSidebar, ReviewReferenceSignals } from './review-history-parts'
import { DataState } from '@/components/ui/data-state'

// v188 跨源溯源面板的时间格式化（v302 P0+：从 JSX 闭包提到模块作用域）
// 原版每次渲染都重建三个 arrow + 读 new Date().getFullYear()，提到模块后仅创建一次。
// 与 review-history-parts.fmtTime 行为一致（replace 'T' 后 slice），不依赖外部 state。
function fmtCrossTs(t: string): string {
  // ISO "YYYY-MM-DDTHH:MM:SS+ZZ:ZZ" → "MM-DD HH:MM"
  if (!t) return ''
  return String(t).slice(5, 16).replace('T', ' ')
}

function fmtCrossTsFull(t: string): string {
  // ISO → "YYYY-MM-DD HH:MM"（带年份，跨年用）
  if (!t) return ''
  return String(t).slice(0, 16).replace('T', ' ')
}

const CURRENT_YEAR = String(new Date().getFullYear())

function fmtCrossTsAdaptive(t: string): string {
  if (!t) return ''
  // 同年省略年份（节省横向空间），跨年才显示
  return String(t).slice(0, 4) === CURRENT_YEAR ? fmtCrossTs(t) : fmtCrossTsFull(t)
}

function sameCrossTs(a: string, b: string): boolean {
  if (!a || !b) return false
  return fmtCrossTs(a) === fmtCrossTs(b)
}

// 块 B：快速评测复盘（历史新闻交易回溯）
// 数据源：quick_sim_trades —— 全部信号不过闸门、按快速评测时长（配置项 QUICK_SIM_HORIZON_MINUTES，
// 当前 20 分钟）真实行情结算，
// 按新闻维度聚合，AI 归因失败（17 类）/ 成功（10 类）+ 整体总结（缓存 news_trade_reviews）。
// 另含"提炼的参考信号"（同为快速评测数据）：AI 分析新新闻时参考的统计规律。
//
// v170 P0+ 重构：
//   - 类型定义 → lib/types-review-history.ts
//   - 子组件 + helpers → components/replay/review-history-parts.tsx
//   本文件由原 981 行减至 ~700 行。

export function ReviewHistoryPanel({ period }: { period: PeriodRange }) {
  const [list, setList] = useState<NewsTrade[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<NewsTradeDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  // v180 P0+：单笔交易展开（K线时间线 + 入出场标记）
  const [expandedTradeId, setExpandedTradeId] = useState<number | null>(null)
  // v188 P0+：跨源溯源面板展开状态（点 source 名 → 展开/收起；切新闻自动收起）
  const [showTrace, setShowTrace] = useState(false)
  // v290 P0+（反思系统时段统一）：period 由 props 注入（顶部 PeriodSelector 唯一来源），
  // 删除内部 [periodKey, setPeriodKey] / [customDateRange, setCustomDateRange] state。
  const [signals, setSignals] = useState<ReviewSignals | null>(null)
  const [showSignals, setShowSignals] = useState(true)
  // 新闻列表筛选：全部 / 仅盈利 / 仅亏损 / 已复盘 / 未复盘
  const [filter, setFilter] = useState<'all' | 'win' | 'loss' | 'reviewed' | 'unreviewed'>('all')
  // 快速评测持仓时长（分钟）—— v170 P0+：从独立 hook 取，与 review-panel 同源
  // 之前每面板各自 useEffect 拉一次（重复 RTT + 双份 state），现在 useQuickSimHorizon 内部 usePoll 兜底。
  const qsHorizon = useQuickSimHorizon()

  // v170 P0+：10s 轮询「参考信号」改用 useApiPoll —— 复用 abort / deps / transform 机制，
  // 替代原 useEffect + window.setInterval 复制粘贴模式（缺 unmount 保护）。
  // 注：loadList / loadPendingCount 仍保留 useCallback —— 它们依赖 period state 且
  // 与 selectNews / runReview / runRejudgeBatch 等用户操作联动，留 useCallback 更直观。
  const signalsPoll = useApiPoll<ReviewSignals>('/review/signals', {
    intervalMs: 10_000,
    deps: [],
    onError: () => {/* 静默：信号拉取失败不影响主展示 */},
  })
  // 同步 signalsPoll.data → signals state（兼容下游所有读取 points）
  useEffect(() => {
    if (signalsPoll.data) setSignals(signalsPoll.data)
  }, [signalsPoll.data])

  const loadList = useCallback(async () => {
    try {
      // v180 P0+：走 periodToQuery(period) 统一出参；今日/7d/30d 用 hours，自定义用 from_ts/to_ts
      const params = periodToQuery(period)
      params.set('limit', '200')
      const data = await apiGet<NewsTrade[]>(`/history/news-trades?${params.toString()}`)
      setList(data)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '列表加载失败')
    }
  }, [period])

  useEffect(() => {
    void loadList()
  }, [loadList])

  const loadDetail = useCallback(async (newsId: number) => {
    setLoadingDetail(true)
    try {
      const data = await apiGet<NewsTradeDetail>(`/history/news-trades/${newsId}`)
      setDetail(data)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '详情加载失败')
    } finally {
      setLoadingDetail(false)
    }
  }, [])

  const selectNews = async (newsId: number) => {
    setSelectedId(newsId)
    setShowTrace(false)  // v188 P0+：切新闻自动收起溯源面板
    await loadDetail(newsId)
  }

  const runReview = async (newsId: number) => {
    setReviewing(true)
    try {
      await apiSend(`/history/news-trades/${newsId}/review`, 'POST')
      await loadDetail(newsId)
      void loadList()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'AI 复盘失败')
    } finally {
      setReviewing(false)
    }
  }

  // v180 P0+：重新判定（统一口径 §5）
  // 仅对有 pending 交易的新闻可见；点击后用最新行情重算 pnl/verdict。
  const [rejudging, setRejudging] = useState(false)
  const runRejudge = async (newsId: number) => {
    setRejudging(true)
    try {
      const data = await apiSend<{
        news_id: number
        retried: number
        updated: Array<{ trade_id: number; new_verdict: string; pnl_pct: number }>
        still_pending: Array<{ trade_id: number; reason: string }>
      }>(`/history/news-trades/${newsId}/rejudge`, 'POST')
      setError(
        data.still_pending.length > 0
          ? `已重新判定 ${data.updated.length} 笔；仍有 ${data.still_pending.length} 笔待 K线/极值就绪`
          : null,
      )
      await loadDetail(newsId)
      void loadList()
      void loadPendingCount()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '重新判定失败')
    } finally {
      setRejudging(false)
    }
  }

  // v180 P0+：批量重新判定 — 当前窗口内所有 pending 交易
  const runRejudgeBatch = async () => {
    setRejudging(true)
    try {
      const params = periodToQuery(period)
      params.set('limit', '50')
      const data = await apiSend<{
        summary: { news: number; retried: number; updated: number; still_pending: number }
      }>(`/history/news-trades/rejudge-batch?${params.toString()}`, 'POST')
      const s = data.summary
      setError(
        s.still_pending > 0
          ? `批量重判完成：${s.news} 条新闻 · 重新判定 ${s.updated} 笔；仍有 ${s.still_pending} 笔待 K线/极值就绪`
          : null,
      )
      void loadList()
      void loadPendingCount()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '批量重新判定失败')
    } finally {
      setRejudging(false)
    }
  }

  // v180 P0+：是否有待判定交易（用于显示「重新判定」按钮）
  const hasPending = (detail?.trades ?? []).some(
    (t) => t.pending_reason && t.pending_reason.length > 0,
  )

  // v180 P0+：窗口内 pending 总览（顶部徽章用）
  const [pendingCount, setPendingCount] = useState<{ news_with_pending: number; pending_trades: number } | null>(null)
  const loadPendingCount = useCallback(async () => {
    try {
      const params = periodToQuery(period)
      const data = await apiGet<{ news_with_pending: number; pending_trades: number }>(
        `/history/news-trades/pending-count?${params.toString()}`,
      )
      setPendingCount({ news_with_pending: data.news_with_pending, pending_trades: data.pending_trades })
    } catch {
      // 静默失败 — 徽章只是辅助信息
    }
  }, [period])
  useEffect(() => { void loadPendingCount() }, [loadPendingCount])

  const filteredList = (list ?? []).filter((item) => {
    if (filter === 'win') return item.wins > item.losses
    if (filter === 'loss') return item.losses > item.wins
    if (filter === 'reviewed') return item.has_review
    if (filter === 'unreviewed') return !item.has_review
    return true
  })

  return (
    <div className="surface-card shrink-0 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          B · 快速评测复盘（新闻交易回溯）
        </h3>
        {/* v290 P0+：删掉内部 PeriodSelector —— 时段由顶部 PeriodSelector 唯一控制，
            这里只读显示当前时段徽章，杜绝"两个时段控件打架"。 */}
        <span
          className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[9px] text-primary"
          title={`本面板时段由顶部 PeriodSelector 控制（当前 ${period.label}）`}
        >
          时段 = {period.label}
        </span>
        <span className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[9px] text-primary">
          数据 = [模拟] quick_sim_trades（settled=1）· 北京时区
        </span>
        {/* v180 P0+：pending 总览徽章（统一口径 §5） */}
        {pendingCount && pendingCount.pending_trades > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded border border-hold/50 bg-hold/10 px-1.5 py-0.5 font-mono text-[9px] text-hold"
            title={`当前时段内 ${pendingCount.news_with_pending} 条新闻含待判定交易`}
          >
            <span>⏳ 待判定 {pendingCount.pending_trades} 笔 / {pendingCount.news_with_pending} 条新闻</span>
            <button
              type="button"
              disabled={rejudging}
              onClick={() => void runRejudgeBatch()}
              className="rounded border border-hold/60 px-1 font-mono text-[9px] hover:bg-hold/20 disabled:cursor-wait disabled:opacity-50"
              title="对当前时段内所有 pending 交易重判（最多 50 条新闻）"
            >
              {rejudging ? '…' : '🔁 批量重判'}
            </button>
          </span>
        )}
        <button
          type="button"
          onClick={() => void loadList()}
          className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
        >
          刷新
        </button>
        <div className="flex items-center gap-1 font-mono text-[9px]">
          {([
            ['all', '全部'],
            ['win', '仅盈利'],
            ['loss', '仅亏损'],
            ['reviewed', '已复盘'],
            ['unreviewed', '未复盘'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setFilter(key)}
              className={`rounded border px-1.5 py-0.5 transition ${
                filter === key
                  ? 'border-primary/60 bg-primary/15 text-primary'
                  : 'border-border text-muted-foreground hover:text-foreground'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="ml-auto flex items-center gap-1 font-mono text-[9px] text-muted-foreground/70">
          {/* v180 P0+：时段数据源标注（统一口径 §2 / §3） */}
          <span className="rounded border border-hold/40 bg-hold/10 px-1 py-0.5 text-hold">
            [{period.label}]
          </span>
          <span className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 text-primary">
            quick_sim
          </span>
          {list && list.length > 0 ? (
            <>快速评测 {list.reduce((a, b) => a + b.trade_count, 0)} 笔（{list.length} 条新闻） · 全部信号不过闸门，{qsHorizon} 分钟真实行情结算</>
          ) : (
            <>全部信号（不过闸门）{qsHorizon} 分钟真实行情结算 · 看模型判断力</>
          )}
        </span>
      </div>

      {error && <div className="mt-2 rounded border border-short/40 bg-short/10 p-2 text-[11px] text-short">加载失败：{error}</div>}

      <div className="mt-2 grid grid-cols-1 gap-2 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        {/* 左：新闻列表 */}
        <DataState
          loading={!list && !error}
          loadingLabel="正在加载已结算的快速评测交易…"
          empty={!!list && list.length === 0}
          emptyLabel="该窗口内暂无已结算的快速评测交易（引擎运行并产生信号后这里会出现）"
          data={list}
          className="font-mono text-[10px]"
        >
        {(l) => (
          <NewsListSidebar items={filteredList} selectedId={selectedId} onSelect={(id) => void selectNews(id)} />
        )}
        </DataState>

        {/* 右：详情 */}
        <div className="min-h-[200px] rounded-md border border-border bg-card p-2.5">
          {!selectedId && (
            <div className="font-mono text-[10px] text-muted-foreground">← 点击左侧新闻查看交易明细与 AI 复盘</div>
          )}
          {selectedId && (
            <DataState
              loading={loadingDetail}
              loadingLabel="加载详情…"
              empty={!detail}
              emptyLabel="无法加载该新闻的详情"
              data={detail}
              className="font-mono text-[10px]"
            >
              {(d) => (
                <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-foreground">
                  新闻 #{d.news_id} ·{' '}
                  {/* v188 P0+：点 source 名展开/收起跨源溯源面板（不加任何额外按钮/徽章） */}
                  <button
                    type="button"
                    onClick={() => setShowTrace((s) => !s)}
                    title={showTrace ? '收起' : '展开'}
                    className={`cursor-pointer font-mono text-[10px] font-bold uppercase tracking-wider underline-offset-2 transition-colors hover:underline ${
                      showTrace ? 'text-primary' : 'text-foreground hover:text-primary'
                    }`}
                  >
                    {d.source}
                  </button>
                </span>
                <span className="font-mono text-[9px] text-muted-foreground">{fmtTime(d.news_time)}</span>
                <span className="ml-auto font-mono text-[9px] text-muted-foreground">
                  共 {d.aggregate.total} 笔 · 已判定 {d.aggregate.decided}
                  {d.aggregate.winrate != null ? ` · 胜率 ${fmtPct(d.aggregate.winrate)}` : ''}
                  {' · 总盈亏 '}
                  <span className={d.aggregate.total_pnl >= 0 ? 'text-long' : 'text-short'}>
                    {d.aggregate.total_pnl > 0 ? '+' : ''}{d.aggregate.total_pnl.toFixed(2)}%
                  </span>
                </span>
                <button
                  type="button"
                  disabled={reviewing}
                  onClick={() => void runReview(d.news_id)}
                  className="rounded border border-primary/50 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary disabled:cursor-wait disabled:opacity-60"
                >
                  {reviewing ? 'AI 复盘分析中…' : d.review ? '重新 AI 复盘' : '运行 AI 复盘'}
                </button>
                {/* v180 P0+：仅在有待判定交易时显示「重新判定」按钮 */}
                {hasPending && (
                  <button
                    type="button"
                    disabled={rejudging}
                    onClick={() => void runRejudge(d.news_id)}
                    className="rounded border border-hold/50 bg-hold/10 px-2 py-0.5 font-mono text-[10px] text-hold disabled:cursor-wait disabled:opacity-60"
                    title="用最新 K线重算 pending 交易（不会破坏已有 WIN/LOSS 判定）"
                  >
                    {rejudging ? '重新判定中…' : '🔁 重新判定'}
                  </button>
                )}
              </div>

              <p className="rounded border border-border bg-secondary/20 p-1.5 text-[11px] leading-snug text-foreground/85">
                {d.content}
              </p>

              {/* v188 P0+：跨源溯源面板（点 source 名时展开）。默认收起，零页面复杂度。
                  v188 P0.2：去掉"距今多久"，只显示绝对时间（跨年加年份前缀）
                  v302 P0+：时间格式化函数 fmtCrossTs/fmtCrossTsFull/fmtCrossTsAdaptive/sameCrossTs
                  已在模块作用域定义（避免每次渲染重建）。 */}
              {showTrace && d.cross_trace && (() => {
                const ct = d.cross_trace
                return (
                  <div className="rounded border border-primary/40 bg-primary/5 p-2">

                    {ct.primary_source && (
                      <div className="mt-1.5 flex flex-wrap items-baseline gap-2 border-b border-border/60 pb-1.5">
                        <span className="rounded border border-amber-500/40 bg-amber-500/10 px-1 py-0.5 font-mono text-[9px] text-amber-700 dark:text-amber-300">
                          📌 一手源
                        </span>
                        <span className="font-mono text-[10px] font-semibold text-foreground/90">
                          {ct.primary_source}
                        </span>
                        {ct.primary_origin_info && (
                          <span className="font-mono text-[9px] text-muted-foreground">
                            {ct.primary_origin_info}
                          </span>
                        )}
                      </div>
                    )}

                    <ul className="mt-1.5 space-y-1">
                      {ct.sources.map((s) => {
                        const isEarliest = s.is_earliest
                        const isCurrent = s.is_current
                        const isOnly = ct.cluster_size <= 1 && isCurrent
                        const earliest = ct.sources.find((x) => x.is_earliest)
                        const lagSeconds = earliest && !sameCrossTs(s.first_seen_ts, earliest.first_seen_ts)
                          ? Math.max(0, Math.floor((new Date(s.first_seen_ts).getTime() - new Date(earliest.first_seen_ts).getTime()) / 1000))
                          : 0
                        const fmtLag = (sec: number): string => {
                          if (sec <= 0) return '同时'
                          if (sec < 60) return `晚 ${sec}秒`
                          if (sec < 3600) return `晚 ${Math.round(sec / 60)}分钟`
                          return `晚 ${(sec / 3600).toFixed(1)}小时`
                        }
                        return (
                          <li
                            key={s.id}
                            className={`flex flex-wrap items-baseline gap-2 rounded px-1 py-0.5 ${
                              isCurrent
                                ? 'border border-primary/40 bg-primary/10'
                                : isEarliest
                                ? 'bg-blue-500/5'
                                : ''
                            }`}
                          >
                            {isEarliest && !isOnly && (
                              <span className="rounded border border-blue-500/40 bg-blue-500/10 px-1 py-0.5 font-mono text-[9px] text-blue-700 dark:text-blue-300">
                                🔵 最早
                              </span>
                            )}
                            {isCurrent && (
                              <span className="rounded border border-primary/50 bg-primary/20 px-1 py-0.5 font-mono text-[9px] text-primary">
                                ⭐ 当前
                              </span>
                            )}
                            <span className="font-mono text-[10px] font-semibold text-foreground/90">
                              {s.source}
                            </span>
                            <span className="font-mono text-[9px] text-muted-foreground">
                              {fmtCrossTsAdaptive(s.first_seen_ts)}
                            </span>
                            {/* 相对最早的滞后（同集群内部排序） */}
                            {!isEarliest && lagSeconds > 0 && (
                              <span className="rounded border border-border bg-muted/30 px-1 py-0.5 font-mono text-[9px] text-muted-foreground">
                                {fmtLag(lagSeconds)}
                              </span>
                            )}
                            {s.snippet && (
                              <span className="w-full pl-1 font-mono text-[9px] italic text-muted-foreground/80">
                                &ldquo;{s.snippet.replace(/^\[hash:[a-f0-9]+\]\s*/, '')}&rdquo;
                              </span>
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  </div>
                )
              })()}

              {/* AI 总结（缓存） */}
              {d.review && (
                <div className="rounded border border-primary/30 bg-primary/5 p-2">
                  <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-primary">
                    AI 复盘总结
                    {d.review_generated_at && (
                      <span className="font-mono text-[9px] normal-case text-muted-foreground">生成于 {d.review_generated_at}</span>
                    )}
                  </div>
                  {d.review.verdict && (
                    <div className="mt-1 text-[11px] font-semibold text-foreground">结论：{d.review.verdict}</div>
                  )}
                  {d.review.key_insight && (
                    <div className="mt-0.5 text-[11px] text-foreground/90">💡 核心教训：{d.review.key_insight}</div>
                  )}
                  {d.review.detail && (
                    <div className="mt-1 text-[11px] leading-relaxed text-muted-foreground">📋 {d.review.detail}</div>
                  )}
                  {d.review.rule_check && (
                    <div className="mt-1 text-[10px] leading-relaxed text-hold">⚖ 经验校验：{d.review.rule_check}</div>
                  )}
                  {(d.review.failures && d.review.failures.length > 0) && (
                    <div className="mt-1.5 border-t border-border/40 pt-1">
                      <div className="font-mono text-[9px] uppercase tracking-wider text-short">失败归因（17 类）</div>
                      <ul className="mt-0.5 space-y-0.5">
                        {d.review.failures.map((f, i) => (
                          <li key={i} className="text-[10px] leading-relaxed text-muted-foreground">
                            #{f.decision_id} <span className="text-short">{f.reason_name}</span>：{f.reason_text}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {(d.review.successes && d.review.successes.length > 0) && (
                    <div className="mt-1.5 border-t border-border/40 pt-1">
                      <div className="font-mono text-[9px] uppercase tracking-wider text-long">成功归因（10 类）</div>
                      <ul className="mt-0.5 space-y-0.5">
                        {d.review.successes.map((s, i) => (
                          <li key={i} className="text-[10px] leading-relaxed text-muted-foreground">
                            #{s.decision_id} <span className="text-long">{s.reason_name}</span>：{s.reason_text}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              )}
              {!d.review && (
                <div className="rounded border border-border bg-secondary/20 p-2 font-mono text-[10px] text-muted-foreground">
                  尚未生成 AI 复盘总结，点击右上角"运行 AI 复盘"（可能需要 1-3 分钟）
                </div>
              )}

              {/* 交易明细 */}
              <div>
                <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                  <span>交易明细（{d.trades.length} 笔 · 全部信号不过闸门，{qsHorizon} 分钟结算）</span>
                  {/* v180 P0+：数据源 + 时段标注（统一口径 §2 / §3） */}
                  <span className="rounded border border-hold/40 bg-hold/10 px-1 py-0.5 normal-case text-hold">
                    [模拟]
                  </span>
                  <span className="normal-case text-muted-foreground/70">quick_sim_trades · 北京时区</span>
                </div>
                <div className="thin-scroll mt-1 max-h-[30vh] space-y-1 overflow-y-auto pr-0.5">
                  {d.trades.map((t) => {
                    const expanded = expandedTradeId === t.id
                    return (
                      <div key={t.id}
                        className={`rounded border bg-secondary/25 px-2 py-1 transition-colors ${
                          expanded ? 'border-primary/50 bg-primary/5' : 'border-border cursor-pointer hover:border-primary/40'
                        }`}
                        onClick={() => !expanded && setExpandedTradeId(t.id)}>
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[10px]">
                          <span className="text-foreground">#{t.decision_id}</span>
                          <span className="text-foreground">{t.asset}</span>
                          <span className={t.action === 'BUY' ? 'text-long' : 'text-short'}>{t.action}</span>
                          <span className={t.gate_passed ? 'text-long' : 'text-muted-foreground'}>
                            {t.gate_passed ? '闸门过' : '闸门拒'}
                          </span>
                          <TradeStatusBadge trade={t} />
                          <span className={t.pnl_pct != null && t.pnl_pct >= 0 ? 'text-long' : 'text-short'}>
                            {t.pnl_pct != null ? `${t.pnl_pct > 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%` : '—'}
                          </span>
                          <span className="ml-auto text-muted-foreground/70">
                            入场 {fmtTs(t.entry_ts)}
                            {t.interval_minutes != null ? `（新闻后 ${t.interval_minutes} 分钟）` : ''}
                            {' · '}{t.expected_horizon || '-'} · 阶段 {t.event_phase || '-'} · 强度 {t.event_strength || '-'}
                          </span>
                          {/* v180 P0+：展开 / 收起按钮 */}
                          <button
                            type="button"
                            className="ml-1 rounded border border-border px-1 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground"
                            onClick={(e) => {
                              e.stopPropagation()
                              setExpandedTradeId(expanded ? null : t.id)
                            }}
                          >
                            {expanded ? '收起' : '展开'}
                          </button>
                        </div>
                        {t.reasoning && (
                          <div className="mt-0.5 line-clamp-2 text-[10px] leading-snug text-muted-foreground/80">{t.reasoning}</div>
                        )}
                        {/* v180 P0+：展开后渲染 K线时间线 + 决策上下文四段（信号/AI/对比/持仓） */}
                        {expanded && (
                          <div className="mt-1 space-y-1" onClick={(e) => e.stopPropagation()}>
                            <TradeSparkline trade={t} />
                            <div className="grid grid-cols-2 gap-1 font-mono text-[9px]">
                              <div className="rounded border border-border bg-background/60 p-1">
                                <div className="text-muted-foreground">🎯 信号阶段</div>
                                <div className="text-foreground">phase={t.event_phase || '-'} · strength={t.event_strength || '-'} · horizon={t.expected_horizon || '-'}</div>
                              </div>
                              <div className="rounded border border-border bg-background/60 p-1">
                                <div className="text-muted-foreground">🛡 闸门理由</div>
                                <div className="line-clamp-2 text-foreground">{t.gate_reason || '—（无）'}</div>
                              </div>
                              <div className="rounded border border-border bg-background/60 p-1">
                                <div className="text-muted-foreground">📈 仓位 / 净值</div>
                                <div className="text-foreground">notional={t.notional_usdt?.toFixed?.(0) ?? '-'} · 入价 {t.entry_price?.toFixed?.(2) ?? '-'} → 出价 {t.exit_price?.toFixed?.(2) ?? '-'}</div>
                              </div>
                              <div className="rounded border border-border bg-background/60 p-1">
                                <div className="text-muted-foreground">🧮 判定</div>
                                <div className="text-foreground">verdict={t.is_correct || '-'} · pnl_pct={t.pnl_pct?.toFixed?.(2) ?? '-'}%</div>
                              </div>
                            </div>
                            {t.reasoning && (
                              <details className="rounded border border-border bg-background/60 p-1">
                                <summary className="cursor-pointer font-mono text-[9px] text-muted-foreground">▸ AI 推理全文</summary>
                                <pre className="mt-1 whitespace-pre-wrap text-[10px] leading-snug text-foreground/85">{t.reasoning}</pre>
                              </details>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            </div>
              )}
            </DataState>
          )}
        </div>
      </div>

      {/* 提炼的参考信号：快速评测数据，AI 分析新新闻时参考 */}
      <ReviewReferenceSignals
        signals={signals}
        showSignals={showSignals}
        onToggleShow={() => setShowSignals((v) => !v)}
      />
    </div>
  )
}
