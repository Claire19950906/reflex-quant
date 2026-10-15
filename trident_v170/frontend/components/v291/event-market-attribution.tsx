'use client'

/**
 * EventMarketAttribution — 事件-行情归因面板 (v291)
 *
 * 实时显示: 算法检测到的临界涨跌 + 关联事件 + AI 解释
 *  - 左: critical_price_moves 列表
 *  - 右: 选中 move 后, 展示其 top-3 关联事件
 *  - AI 没接时: 显示 "未配置 LLM" 提示, 不假装已解释
 */

import { useCallback, useEffect, useState } from 'react'
import { cn } from '@/lib/utils'
import { Zap, ArrowUp, ArrowDown, Link2, AlertCircle, Sparkles, Clock } from 'lucide-react'

type Move = {
  id: number
  asset: string
  ts: number
  start_price: number
  peak_price: number
  end_price: number
  move_pct: number
  move_kind: string
  duration_min: number
  algo_score: number
  context: Record<string, unknown>
  created_at: string
}

type Attribution = {
  id: number
  move_id: number
  news_event_id: number | null
  asset: string
  time_lag_sec: number
  algo_score: number
  algo_reasons: string
  ai_called: number
  ai_explanation: string | null
  ai_confidence: number | null
  ai_error: string | null
  move_ts: number
  move_pct: number
  move_kind: string
  duration_min: number
  ev_news_id: number | null
  ev_source: string
  ev_action: string
  ev_score: number
  ev_ts: number
}

function formatTs(ts: number): string {
  return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
}

function kindBadge(kind: string): { label: string; cls: string; Icon: typeof ArrowUp } {
  if (kind === 'fast_rise') return { label: '快涨', cls: 'text-long bg-long/15 border-long/40', Icon: ArrowUp }
  if (kind === 'fast_fall') return { label: '快跌', cls: 'text-short bg-short/15 border-short/40', Icon: ArrowDown }
  if (kind === 'slow_rise') return { label: '慢涨', cls: 'text-amber-500 bg-amber-500/10 border-amber-500/40', Icon: ArrowUp }
  if (kind === 'slow_fall') return { label: '慢跌', cls: 'text-orange-500 bg-orange-500/10 border-orange-500/40', Icon: ArrowDown }
  return { label: kind, cls: 'text-muted-foreground bg-muted/30', Icon: Zap }
}

type Props = {
  refreshMs?: number
  className?: string
  // v2026.10.15: page 层统一轮询时点 (number 改变时强制 fetchAll 一次)
  externalRefreshToken?: number
}

export function EventMarketAttribution({ refreshMs = 60_000, className, externalRefreshToken }: Props) {
  const [moves, setMoves] = useState<Move[]>([])
  const [stats, setStats] = useState<Record<string, unknown> | null>(null)
  const [selectedMoveId, setSelectedMoveId] = useState<number | null>(null)
  const [attributions, setAttributions] = useState<Attribution[]>([])
  const [attrLoading, setAttrLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const [m, s] = await Promise.all([
        fetch('/api/v291/critical-moves?hours=48&limit=40').then((r) => r.json()),
        fetch('/api/v291/stats?hours=48').then((r) => r.json()),
      ])
      if (m?.ok) setMoves(m.rows ?? [])
      if (s?.ok) setStats(s)
      setLastUpdated(Date.now())
    } catch {
      // 主屏不该被拽崩
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchAttributions = useCallback(async (moveId: number) => {
    setAttrLoading(true)
    try {
      const r = await fetch(`/api/v291/move-attributions?move_id=${moveId}&limit=5`).then((x) => x.json())
      if (r?.ok) setAttributions(r.rows ?? [])
    } catch {
      setAttributions([])
    } finally {
      setAttrLoading(false)
    }
  }, [])

  useEffect(() => {
    // v302+ P0 优化: 如果有 externalRefreshToken (page 在推统一 tick),
    // 不再启动内部 setInterval, 避免与 page 推的 tick 重复 poll (60s + 60s = ~30s 平均一次)
    void fetchAll()
    if (externalRefreshToken !== undefined) {
      // page 推时由下面的 externalRefreshToken useEffect 负责拉取
      return
    }
    const id = setInterval(() => void fetchAll(), refreshMs)
    return () => clearInterval(id)
    // fetchAll 是 useCallback([]) 稳定的; externalRefreshToken 仅用来决定要不要建 setInterval,
    // 不进 deps 避免每次 page tick 重新挂载定时器(fetch 仍由下面的 useEffect 负责)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchAll, refreshMs])
  // v2026.10.15: 响应 page 层统一 tick. externalRefreshToken 改变时立即拉取
  useEffect(() => {
    if (externalRefreshToken && externalRefreshToken > 0) void fetchAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externalRefreshToken])

  useEffect(() => {
    if (selectedMoveId !== null) {
      void fetchAttributions(selectedMoveId)
    }
  }, [selectedMoveId, fetchAttributions])

  const statsByAsset = (
    stats as { by_asset?: Record<string, { moves: number; attributions: number; max_score: number; ai_explained: number }> } | null
  )?.by_asset ?? {}

  return (
    <div className={cn('flex flex-col gap-2 rounded border border-border bg-card p-3', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Zap className="h-4 w-4 text-foreground" />
          <h3 className="font-mono text-[12px] font-semibold uppercase tracking-wide text-foreground">
            事件-行情归因 (v291)
          </h3>
          <span className="font-mono text-[9px] text-muted-foreground/60">
            · algo-first · AI 仅在低置信度时回写
          </span>
        </div>
        <div className="flex items-center gap-2 font-mono text-[9px] text-muted-foreground/60">
          {lastUpdated !== null && <span>upd {new Date(lastUpdated).toISOString().slice(11, 19)}Z</span>}
          <span>auto {Math.round(refreshMs / 1000)}s</span>
        </div>
      </div>

      {/* 顶部 asset 统计 */}
      <div className="grid grid-cols-2 gap-1.5 md:grid-cols-5">
        {['BTC', 'ETH', 'SOL', 'XAU', 'WTI'].map((a) => {
          const s = statsByAsset[a]
          return (
            <div
              key={a}
              className="rounded border border-muted-foreground/30 bg-muted/10 px-2 py-1.5 font-mono text-[10px]"
            >
              <div className="flex items-center justify-between">
                <span className="font-semibold text-foreground">{a}</span>
                <span className="text-muted-foreground/70">48h</span>
              </div>
              <div className="mt-0.5 flex items-center justify-between text-muted-foreground">
                <span>
                  moves <span className="font-semibold text-foreground">{s?.moves ?? 0}</span>
                </span>
                <span>
                  attrs <span className="text-foreground">{s?.attributions ?? 0}</span>
                </span>
              </div>
            </div>
          )
        })}
      </div>

      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        {/* 左: 临界点列表 */}
        <div className="flex min-h-[300px] flex-col rounded border border-muted-foreground/30 bg-background/40">
          <div className="border-b border-muted-foreground/30 bg-muted/20 px-2 py-1.5 font-mono text-[10px] text-muted-foreground">
            临界点 (Critical Moves) · {moves.length} 条
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading && moves.length === 0 ? (
              <div className="p-3 text-[11px] text-muted-foreground">loading…</div>
            ) : moves.length === 0 ? (
              <div className="p-3 text-[11px] text-muted-foreground">
                48h 内无显著临界点. 后台 worker 每 5-10min 跑一次 (algo-first).
              </div>
            ) : (
              <ul className="divide-y divide-muted-foreground/10">
                {moves.map((m) => {
                  const badge = kindBadge(m.move_kind)
                  const isSelected = selectedMoveId === m.id
                  return (
                    <li
                      key={m.id}
                      onClick={() => setSelectedMoveId(m.id)}
                      className={cn(
                        'cursor-pointer px-2 py-1.5 font-mono text-[10.5px] transition hover:bg-muted/40',
                        isSelected && 'bg-foreground/10',
                      )}
                    >
                      <div className="flex items-center justify-between gap-1">
                        <div className="flex items-center gap-1.5">
                          <span className={cn('rounded border px-1 text-[9px] font-semibold', badge.cls)}>
                            {badge.label}
                          </span>
                          <span className="font-semibold text-foreground">{m.asset}</span>
                          <span className="text-muted-foreground/70">
                            {m.move_pct >= 0 ? '+' : ''}
                            {m.move_pct.toFixed(2)}%
                          </span>
                        </div>
                        <span className="text-[9px] text-muted-foreground/60">
                          dur {m.duration_min}m
                        </span>
                      </div>
                      <div className="mt-0.5 flex items-center justify-between text-muted-foreground/80">
                        <span className="truncate">{formatTs(m.ts)}</span>
                        <span>algo {m.algo_score.toFixed(2)}</span>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        </div>

        {/* 右: 选中 move 的归因详情 */}
        <div className="flex min-h-[300px] flex-col rounded border border-muted-foreground/30 bg-background/40">
          <div className="border-b border-muted-foreground/30 bg-muted/20 px-2 py-1.5 font-mono text-[10px] text-muted-foreground">
            选中临界点的归因
            {selectedMoveId !== null && (
              <button
                onClick={() => setSelectedMoveId(null)}
                className="ml-2 text-[9px] text-muted-foreground/60 hover:text-foreground"
              >
                ✕ 取消
              </button>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {selectedMoveId === null ? (
              <div className="p-3 text-[11px] text-muted-foreground">
                ← 点左侧临界点查看 top-3 关联事件
              </div>
            ) : attrLoading ? (
              <div className="p-3 text-[11px] text-muted-foreground">loading attributions…</div>
            ) : attributions.length === 0 ? (
              <div className="p-3 text-[11px] text-muted-foreground">
                该 move 没有 top-K 关联事件 (算法阈值内无候选)
              </div>
            ) : (
              <ul className="space-y-2">
                {attributions.map((a) => {
                  const badge = kindBadge(a.move_kind)
                  return (
                    <li
                      key={a.id}
                      className="rounded border border-muted-foreground/30 bg-card/60 p-2 font-mono text-[10.5px]"
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-1.5">
                          <span className={cn('rounded border px-1 text-[9px] font-semibold', badge.cls)}>
                            {badge.label}
                          </span>
                          <span className="text-foreground">
                            {a.asset} {a.move_pct >= 0 ? '+' : ''}
                            {a.move_pct.toFixed(2)}%
                          </span>
                          <span className="text-muted-foreground/70">· {a.duration_min}m</span>
                        </div>
                        <span className="text-muted-foreground/80">algo {a.algo_score.toFixed(2)}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-1 text-muted-foreground/80">
                        <Link2 className="h-3 w-3" />
                        <span>
                          事件 #{a.ev_news_id ?? a.news_event_id} · {a.ev_action || '—'} · lag{' '}
                          {a.time_lag_sec >= 0 ? `+${a.time_lag_sec}s` : `${a.time_lag_sec}s`}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[9.5px] text-muted-foreground/60">
                        {a.algo_reasons}
                      </div>
                      {a.ai_called ? (
                        a.ai_explanation ? (
                          <div className="mt-1.5 rounded border border-amber-500/30 bg-amber-500/5 p-1.5">
                            <div className="flex items-center gap-1 text-[9.5px] font-semibold text-amber-600 dark:text-amber-300">
                              <Sparkles className="h-3 w-3" />
                              AI 解释
                              {a.ai_confidence !== null && (
                                <span className="text-muted-foreground/70">
                                  (conf {a.ai_confidence.toFixed(2)})
                                </span>
                              )}
                            </div>
                            <div className="mt-0.5 text-[10px] text-foreground/90">
                              {a.ai_explanation}
                            </div>
                          </div>
                        ) : (
                          <div className="mt-1.5 flex items-center gap-1 text-[10px] text-red-600 dark:text-red-300">
                            <AlertCircle className="h-3 w-3" />
                            AI 调用失败: {a.ai_error || 'unknown'}
                          </div>
                        )
                      ) : (
                        <div className="mt-1.5 flex items-center gap-1 text-[9.5px] text-muted-foreground/50">
                          <Clock className="h-2.5 w-2.5" />
                          未触发 AI (algo_score={a.algo_score.toFixed(2)} ≥ 0.7 时跳过)
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
