'use client'

import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { API_BASE } from '@/lib/api'

// 历史新闻交易回溯 — 左：已结束交易的新闻列表（滑动条回看）；右：该新闻的交易信息 + AI 归因总结

type NewsTradeItem = {
  news_id: number
  source: string
  content: string
  news_time: string
  trade_count: number
  wins: number
  losses: number
  winrate: number | null
  total_pnl: number
  latest_exit_ts: number
  has_review: boolean
}

type TradeRow = {
  id: number
  decision_id: number
  asset: string
  action: string
  score: number | null
  evidence_confidence: number | null
  gate_passed: number
  gate_reason: string
  notional_usdt: number
  entry_price: number
  entry_ts: number
  horizon_minutes: number
  exit_price: number | null
  exit_ts: number | null
  pnl_pct: number | null
  pnl_usdt: number | null
  verdict: string
  is_correct: string
  reasoning: string
  decision_context: string
  expected_horizon: string
  event_strength: string
  event_phase: string
  prediction_type: string
  sentiment_score: number
  interval_minutes: number | null
}

type FailureItem = { decision_id: number; reason_code: number; reason_name: string; reason_text: string; confidence: number }
type SuccessItem = FailureItem

type NewsReview = {
  verdict: string
  key_insight: string
  detail: string
  rule_check: string
  failures: FailureItem[]
  successes: SuccessItem[]
  aggregate?: { total: number; decided: number; wins: number; losses: number; winrate: number | null; total_pnl: number }
}

type NewsDetail = {
  news_id: number
  source: string
  content: string
  news_time: string
  trades: TradeRow[]
  aggregate: { total: number; decided: number; wins: number; losses: number; winrate: number | null; total_pnl: number }
  review: NewsReview | null
  review_generated_at: string | null
}

const pnlColor = (v: number | null | undefined) =>
  v == null ? 'text-muted-foreground' : v >= 0 ? 'text-long' : 'text-short'
const fmtPct = (v: number | null | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)
const fmtTs = (ts: number | null | undefined) => {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
const fmtInterval = (m: number | null | undefined) => {
  if (m == null) return '—'
  if (m < 60) return `${m}分`
  return `${(m / 60).toFixed(1)}时`
}
const cleanNews = (s: string) => s.replace(/\[hash:[a-fA-F0-9]+\]\s*/g, '')

// 来源平台标签归一化（FRED 专属高亮）
function sourceLabel(source: string): string {
  const s = (source || '').toLowerCase()
  if (s.includes('jin10') || s.includes('金十')) return '金十'
  if (s.includes('blockbeats') || s.includes('律动')) return '律动'
  if (s.includes('techflow') || s.includes('深潮')) return '深潮'
  if (s.includes('eastmoney') || s.includes('东财')) return '东财'
  if (s.includes('financialjuice') || s.startsWith('ws:')) return 'FJ'
  if (s.includes('tree') || s.includes('telegram')) return 'Tree'
  if (s.includes('fred') || s.includes('stlouisfed')) return 'FRED'
  if (s.includes('yahoo_finance')) return 'Yahoo'
  if (s.includes('marketwatch')) return 'MW'
  if (s.includes('fxstreet')) return 'FXStreet'
  const cleaned = (source || '').replace(/^\[?[a-z]*:?/i, '').trim()
  return cleaned.slice(0, 12) || '—'
}
function SourceBadge({ source }: { source: string }) {
  const isFred = sourceLabel(source) === 'FRED'
  return (
    <span
      className={`rounded px-1.5 py-px uppercase ${
        isFred ? 'bg-primary/15 text-primary' : 'bg-secondary text-muted-foreground'
      }`}
    >
      {sourceLabel(source)}
    </span>
  )
}

export default function HistoryPage() {
  return (
    <Suspense fallback={<div className="p-4 text-xs text-muted-foreground">载入中…</div>}>
      <HistoryPageInner />
    </Suspense>
  )
}

function HistoryPageInner() {
  const searchParams = useSearchParams()
  // URL ?id=N 自动选中（review-panel 证据链接 / 外链分享场景）—— 仅在初始挂载读一次
  const initialIdRef = useRef<number | null>(null)
  if (initialIdRef.current === null) {
    const raw = searchParams?.get('id')
    const n = raw ? Number(raw) : NaN
    initialIdRef.current = Number.isFinite(n) && n > 0 ? n : null
  }

  const [items, setItems] = useState<NewsTradeItem[]>([])
  const [hours, setHours] = useState(72)
  const [search, setSearch] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(initialIdRef.current)
  const [detail, setDetail] = useState<NewsDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [listLoading, setListLoading] = useState(true)
  const [reviewLoading, setReviewLoading] = useState(false)
  const [reviewError, setReviewError] = useState<string | null>(null)
  const [review, setReview] = useState<NewsReview | null>(null)
  const [reviewGenerated, setReviewGenerated] = useState<string | null>(null)
  const [showDecisions, setShowDecisions] = useState(false)
  const reviewRef = useRef<HTMLDivElement | null>(null)

  const loadList = useCallback(async (h: number) => {
    setListLoading(true)
    try {
      const r = await fetch(`${API_BASE}/history/news-trades?hours=${h}&limit=300`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`list ${r.status}`)
      const raw = await r.json()
      // v292+ shape-agnostic: 后端可能返 ① 裸数组 ② {items:[]} ③ {data:[]}
      // 三种之一; 之前 .filter is not a function 崩溃就是错把 ②/③ 当 ① 用
      const arr: NewsTradeItem[] = Array.isArray(raw)
        ? raw
        : Array.isArray((raw as any)?.items)
          ? (raw as any).items
          : Array.isArray((raw as any)?.data)
            ? (raw as any).data
            : []
      setItems(arr)
      // 自动选中第一条（最新结算的新闻）
      if (arr.length > 0) setSelectedId((prev) => prev ?? arr[0].news_id)
    } catch {
      setItems([])
    } finally {
      setListLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadList(hours)
  }, [hours, loadList])

  const loadDetail = useCallback(async (newsId: number) => {
    setDetailLoading(true)
    setDetail(null)
    setReview(null)
    setReviewGenerated(null)
    setReviewError(null)
    try {
      const r = await fetch(`${API_BASE}/history/news-trades/${newsId}`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`detail ${r.status}`)
      const data = (await r.json()) as NewsDetail
      setDetail(data)
      setReview(data.review)
      setReviewGenerated(data.review_generated_at)
    } catch {
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    if (selectedId != null) void loadDetail(selectedId)
  }, [selectedId, loadDetail])

  const runReview = async () => {
    if (selectedId == null) return
    setReviewLoading(true)
    setReviewError(null)
    try {
      const r = await fetch(`${API_BASE}/history/news-trades/${selectedId}/review`, { method: 'POST' })
      if (!r.ok) throw new Error(await r.text())
      const data = (await r.json()) as { ok: boolean; summary?: NewsReview; generated_at?: string; error?: string }
      if (data.ok && data.summary) {
        setReview(data.summary)
        setReviewGenerated(data.generated_at ?? null)
        setTimeout(() => reviewRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100)
      } else {
        setReviewError(data.error || 'AI 分析失败')
      }
    } catch (cause) {
      setReviewError(cause instanceof Error ? cause.message : 'AI 分析失败')
    } finally {
      setReviewLoading(false)
    }
  }

  const agg = detail?.aggregate
  // 该新闻是否存在失败/成功交易（按盈亏符号），用于区分「无失败单」与「归因未生成」
  const hasLosers = (detail?.trades ?? []).some((t) => (t.pnl_pct ?? 0) < 0)
  const hasWinners = (detail?.trades ?? []).some((t) => (t.pnl_pct ?? 0) > 0)
  // 列表过滤：关键词 + 来源
  const sources = Array.from(new Set(items.map((i) => i.source))).sort()
  const kw = search.trim().toLowerCase()
  const filtered = items.filter((it) => {
    if (sourceFilter && it.source !== sourceFilter) return false
    if (kw && !cleanNews(it.content).toLowerCase().includes(kw)) return false
    return true
  })
  const failStats: Record<string, number> = {}
  ;(review?.failures ?? []).forEach((f) => {
    failStats[f.reason_name] = (failStats[f.reason_name] ?? 0) + 1
  })
  const succStats: Record<string, number> = {}
  ;(review?.successes ?? []).forEach((s) => {
    succStats[s.reason_name] = (succStats[s.reason_name] ?? 0) + 1
  })

  return (
    <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4">
      <section className="surface-card p-3">
        <div className="flex items-center gap-2">
          <h1 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">历史新闻交易回溯</h1>
          <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">HISTORY</span>
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">
            已结束的模拟交易按新闻归类 · AI 归因总结成功/失败原因
          </span>
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          左侧按时间回看已结算的新闻驱动交易（滑动条选择回看窗口），点击任一条新闻，
          右侧展示该新闻关联的全部交易明细，最下方为 AI 对这笔新闻交易的回顾总结与原因分析。
        </p>
      </section>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[400px_minmax(0,1fr)]">
        {/* ── 左：新闻列表 + 滑动条 ── */}
        <div className="surface-card flex min-h-0 flex-col p-2.5">
          <div className="flex items-center justify-between gap-2">
            <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">已结束的新闻交易</h3>
            <span className="font-mono text-[10px] text-foreground">
              {filtered.length}/{items.length} 条 · 最近 {hours}h
            </span>
          </div>

          {/* 搜索 + 来源过滤 */}
          <div className="mt-2 flex gap-1.5">
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索新闻关键词…"
              className="min-w-0 flex-1 rounded border border-border bg-card px-2 py-1 font-mono text-[10px] text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
            />
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="shrink-0 rounded border border-border bg-card px-1.5 py-1 font-mono text-[10px] text-foreground focus:border-primary focus:outline-none"
              aria-label="来源过滤"
            >
              <option value="">全部来源</option>
              {sources.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          {/* 时间回看滑动条 */}
          <div className="mt-2 rounded border border-border bg-card px-2.5 py-2">
            <div className="flex items-center justify-between font-mono text-[10px] text-muted-foreground">
              <span>回看窗口</span>
              <span className="text-foreground">
                {hours < 24 ? `${hours} 小时` : hours % 24 === 0 ? `${hours / 24} 天` : `${hours} 小时`}
              </span>
            </div>
            <input
              type="range"
              min={6}
              max={720}
              step={6}
              value={hours}
              onChange={(e) => {
                setHours(Number(e.target.value))
                setSelectedId(null)
              }}
              className="mt-1.5 w-full accent-primary"
              aria-label="回看窗口（小时）"
            />
            <div className="flex justify-between font-mono text-[9px] text-muted-foreground">
              <span>6h</span><span>15天</span><span>30天</span>
            </div>
          </div>

          {/* 新闻列表 */}
          <div className="thin-scroll mt-2 min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-0.5">
            {listLoading && items.length === 0 && (
              <div className="p-3 text-center font-mono text-[11px] text-muted-foreground">加载中…</div>
            )}
            {!listLoading && filtered.length === 0 && (
              <div className="p-3 text-center font-mono text-[11px] text-muted-foreground">
                {items.length === 0
                  ? '该窗口内没有已结束的新闻交易，试试拉大回看窗口'
                  : '没有匹配的新闻，换个关键词或来源试试'}
              </div>
            )}
            {filtered.map((it) => {
              const active = it.news_id === selectedId
              return (
                <button
                  key={it.news_id}
                  type="button"
                  onClick={() => setSelectedId(it.news_id)}
                  className={`block w-full rounded-md border px-2 py-1.5 text-left transition ${
                    active ? 'border-primary bg-primary/10' : 'border-border bg-card hover:border-primary/40'
                  }`}
                >
                  <div className="flex items-center gap-1.5 font-mono text-[9px] text-muted-foreground">
                    <SourceBadge source={it.source} />
                    {it.has_review && (
                      <span className="rounded bg-long/15 px-1 py-px text-long">✓ 已分析</span>
                    )}
                    <span>{it.news_time?.slice(5, 16)}</span>
                    <span className="ml-auto">
                      {it.trade_count} 笔 · 胜率 {it.winrate != null ? `${(it.winrate * 100).toFixed(0)}%` : '—'}
                    </span>
                    <span className={`font-mono text-[10px] ${pnlColor(it.total_pnl)}`}>{fmtPct(it.total_pnl)}</span>
                  </div>
                  <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-foreground">
                    {cleanNews(it.content)}
                  </div>
                </button>
              )
            })}
          </div>
        </div>

        {/* ── 右：交易详情 + AI 总结 ── */}
        <div className="surface-card min-h-0 overflow-y-auto p-2.5">
          {detailLoading && (
            <div className="flex h-40 items-center justify-center font-mono text-[11px] text-muted-foreground">加载中…</div>
          )}
          {!detailLoading && !detail && (
            <div className="flex h-40 items-center justify-center font-mono text-[11px] text-muted-foreground">
              点击左侧新闻查看交易回溯
            </div>
          )}

          {!detailLoading && detail && (
            <div className="space-y-3">
              {/* 新闻原文 */}
              <div className="rounded-md border border-border bg-card p-2.5">
                <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-muted-foreground">
                  <SourceBadge source={detail.source} />
                  <span>{detail.news_time}</span>
                  <span className="ml-auto">
                    {detail.aggregate.total} 笔交易 · 胜率{' '}
                    {agg?.winrate != null ? `${(agg.winrate * 100).toFixed(1)}%` : '—'}
                    {' · 总盈亏 '}
                    <b className={pnlColor(agg?.total_pnl)}>{fmtPct(agg?.total_pnl)}</b>
                  </span>
                </div>
                <div className="mt-1.5 text-xs leading-relaxed text-foreground">{cleanNews(detail.content)}</div>
              </div>

              {/* 交易明细 */}
              <div className="rounded-md border border-border bg-card p-2.5">
                <div className="mb-1.5 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                  交易明细（{detail.trades.length} 笔）
                </div>
                {detail.trades.length === 0 && (
                  <div className="font-mono text-[11px] text-muted-foreground">该新闻无已结束交易</div>
                )}
                <div className="thin-scroll overflow-x-auto">
                  <table className="w-full min-w-[640px] border-collapse font-mono text-[10px]">
                    <thead>
                      <tr className="border-b border-border text-left text-muted-foreground">
                        <th className="py-1 pr-2 font-normal">入场</th>
                        <th className="py-1 pr-2 font-normal">品种</th>
                        <th className="py-1 pr-2 font-normal">方向</th>
                        <th className="py-1 pr-2 text-right font-normal">入场价</th>
                        <th className="py-1 pr-2 text-right font-normal">出场价</th>
                        <th className="py-1 pr-2 text-right font-normal">盈亏</th>
                        <th className="py-1 pr-2 text-right font-normal">新闻后</th>
                        <th className="py-1 pr-2 text-right font-normal">周期</th>
                        <th className="py-1 font-normal">判定</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.trades.map((t) => (
                        <tr key={t.id} className="border-b border-border/50 last:border-0">
                          <td className="py-1 pr-2 text-muted-foreground">{fmtTs(t.entry_ts)}</td>
                          <td className="py-1 pr-2 font-semibold text-foreground">{t.asset}</td>
                          <td className={`py-1 pr-2 ${t.action === 'BUY' ? 'text-long' : 'text-short'}`}>
                            {t.action === 'BUY' ? '多' : '空'}
                          </td>
                          <td className="py-1 pr-2 text-right text-foreground">{t.entry_price ?? '—'}</td>
                          <td className="py-1 pr-2 text-right text-foreground">{t.exit_price ?? '—'}</td>
                          <td className={`py-1 pr-2 text-right ${pnlColor(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
                          <td className="py-1 pr-2 text-right text-muted-foreground">{fmtInterval(t.interval_minutes)}</td>
                          <td className="py-1 pr-2 text-right text-muted-foreground">{t.horizon_minutes}m</td>
                          <td className={`py-1 ${
                            t.is_correct === 'WIN' ? 'text-long' : t.is_correct === 'LOSS' ? 'text-short' : 'text-muted-foreground'
                          }`}>
                            {t.is_correct === 'WIN' ? '盈利' : t.is_correct === 'LOSS' ? '亏损' : t.is_correct || '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {detail.trades.length > 0 && (
                  <div className="mt-1.5 border-t border-border/50 pt-1.5 text-[10px] text-muted-foreground">
                    胜 {agg?.wins ?? 0} · 负 {agg?.losses ?? 0} · 已判定 {agg?.decided ?? 0}/{agg?.total ?? 0} 笔
                  </div>
                )}

                {/* AI 当时怎么想的（决策原文） */}
                {detail.trades.length > 0 && (
                  <div className="mt-1.5">
                    <button
                      type="button"
                      onClick={() => setShowDecisions((v) => !v)}
                      className="flex w-full items-center gap-1 rounded border border-border bg-secondary/30 px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
                    >
                      <span>AI 当时的决策理由（回溯：当时怎么想的 vs 结果）{showDecisions ? '▾' : '▸'}</span>
                    </button>
                    {showDecisions && (
                      <div className="mt-1 space-y-1.5">
                        {detail.trades.map((t) => (
                          <div key={t.id} className="rounded border border-border/60 bg-card px-2 py-1.5">
                            <div className="flex flex-wrap items-center gap-1.5 font-mono text-[9px] text-muted-foreground">
                              <span className="font-semibold text-foreground">#{t.decision_id} {t.asset} {t.action === 'BUY' ? '多' : '空'}</span>
                              <span>判定 {t.is_correct === 'WIN' ? '盈利' : t.is_correct === 'LOSS' ? '亏损' : t.is_correct || '—'}</span>
                              <span>盈亏 {fmtPct(t.pnl_pct)}</span>
                              {t.event_phase && <span>阶段 {t.event_phase}</span>}
                              {t.expected_horizon && <span>预期 {t.expected_horizon}</span>}
                              {t.prediction_type && <span>类型 {t.prediction_type}</span>}
                            </div>
                            <div className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
                              {t.reasoning || '（无推理记录）'}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* ── AI 回顾总结（核心） ── */}
              <div ref={reviewRef} className="scroll-mt-3 rounded-md border border-primary/30 bg-primary/5 p-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <div className="font-mono text-[10px] uppercase tracking-widest text-primary/80">
                    AI 回顾总结 · 成功/失败原因分析
                  </div>
                  {reviewGenerated && (
                    <span className="font-mono text-[9px] text-muted-foreground">生成于 {reviewGenerated}</span>
                  )}
                  <button
                    type="button"
                    onClick={() => void runReview()}
                    disabled={reviewLoading}
                    className="ml-auto rounded border border-primary/50 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary disabled:cursor-wait disabled:opacity-60"
                  >
                    {reviewLoading ? 'AI 分析中…（约 30-90 秒）' : review ? '重新分析' : '运行 AI 分析'}
                  </button>
                </div>

                {reviewError && (
                  <div className="mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 text-[11px] text-short">{reviewError}</div>
                )}

                {reviewLoading && !review && (
                  <div className="mt-2 animate-pulse font-mono text-[11px] text-muted-foreground">
                    正在结合新闻原文与交易归因总结…（调用 LLM 中）
                  </div>
                )}

                {review && (
                  <div className="mt-2 space-y-2">
                    {review.verdict && (
                      <div className="flex items-center gap-2 rounded border border-border bg-card px-2 py-1.5">
                        <span className="font-mono text-[10px] text-muted-foreground">结论</span>
                        <span className="text-xs font-medium text-foreground">{review.verdict}</span>
                      </div>
                    )}
                    {review.key_insight && (
                      <div className="flex items-center gap-2 rounded border border-long/30 bg-long/5 px-2 py-1.5">
                        <span className="font-mono text-[10px] text-long/80">要点</span>
                        <span className="text-xs text-foreground">{review.key_insight}</span>
                      </div>
                    )}
                    {review.detail && (
                      <div className="rounded border border-border bg-card px-2 py-1.5 text-[11px] leading-relaxed text-foreground">
                        {review.detail}
                      </div>
                    )}
                    {review.rule_check && (
                      <div className="flex items-start gap-2 rounded border border-primary/20 bg-primary/5 px-2 py-1.5">
                        <span className="shrink-0 font-mono text-[10px] text-primary/80">经验校验</span>
                        <span className="text-[11px] leading-relaxed text-foreground">{review.rule_check}</span>
                      </div>
                    )}

                    {/* 归因统计 */}
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                      <div className="rounded-md border border-short/30 bg-short/5 p-2">
                        <div className="font-mono text-[10px] uppercase tracking-widest text-short/80">
                          失败原因归类（{Object.values(failStats).reduce((a, b) => a + b, 0)} 条）
                        </div>
                        {Object.keys(failStats).length === 0 && (
                          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                            {hasLosers ? '归因未生成（LLM 未返回结果，可重新运行分析）' : '本期无失败单'}
                          </div>
                        )}
                        <div className="mt-1 space-y-1">
                          {Object.entries(failStats).sort((a, b) => b[1] - a[1]).map(([name, n]) => (
                            <div key={name} className="flex items-center gap-2 font-mono text-[10px]">
                              <span className="w-28 shrink-0 truncate text-foreground">{name}</span>
                              <div className="h-1.5 flex-1 overflow-hidden rounded bg-secondary">
                                <div
                                  className="h-full rounded bg-short/60"
                                  style={{ width: `${(n / Math.max(Object.values(failStats).reduce((a, b) => a + b, 0), 1)) * 100}%` }}
                                />
                              </div>
                              <span className="w-8 shrink-0 text-right text-foreground">{n}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div className="rounded-md border border-long/30 bg-long/5 p-2">
                        <div className="font-mono text-[10px] uppercase tracking-widest text-long/80">
                          成功原因归类（{Object.values(succStats).reduce((a, b) => a + b, 0)} 条）
                        </div>
                        {Object.keys(succStats).length === 0 && (
                          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                            {hasWinners ? '归因未生成（LLM 未返回结果，可重新运行分析）' : '本期无成功单'}
                          </div>
                        )}
                        <div className="mt-1 space-y-1">
                          {Object.entries(succStats).sort((a, b) => b[1] - a[1]).map(([name, n]) => (
                            <div key={name} className="flex items-center gap-2 font-mono text-[10px]">
                              <span className="w-28 shrink-0 truncate text-foreground">{name}</span>
                              <div className="h-1.5 flex-1 overflow-hidden rounded bg-secondary">
                                <div
                                  className="h-full rounded bg-long/60"
                                  style={{ width: `${(n / Math.max(Object.values(succStats).reduce((a, b) => a + b, 0), 1)) * 100}%` }}
                                />
                              </div>
                              <span className="w-8 shrink-0 text-right text-foreground">{n}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>

                    {/* 逐单归因明细 */}
                    {(review.failures.length > 0 || review.successes.length > 0) && (
                      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                        {review.failures.length > 0 && (
                          <ul className="space-y-0.5">
                            {review.failures.map((f) => (
                              <li key={f.decision_id} className="text-[10px] leading-relaxed text-muted-foreground">
                                <span className="text-short">✗ #{f.decision_id}</span> {f.reason_name}
                                {f.reason_text ? `：${f.reason_text}` : ''}
                                <span className="text-muted-foreground/70">（置信 {Math.round((f.confidence ?? 0.5) * 100)}%）</span>
                              </li>
                            ))}
                          </ul>
                        )}
                        {review.successes.length > 0 && (
                          <ul className="space-y-0.5">
                            {review.successes.map((s) => (
                              <li key={s.decision_id} className="text-[10px] leading-relaxed text-muted-foreground">
                                <span className="text-long">✓ #{s.decision_id}</span> {s.reason_name}
                                {s.reason_text ? `：${s.reason_text}` : ''}
                                <span className="text-muted-foreground/70">（置信 {Math.round((s.confidence ?? 0.5) * 100)}%）</span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </main>
  )
}
