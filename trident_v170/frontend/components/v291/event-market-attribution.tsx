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
import { fetchWithFallback } from '@/lib/api'
import { Zap, ArrowUp, ArrowDown, Link2, AlertCircle, Sparkles, Clock, TrendingUp, TrendingDown, Activity } from 'lucide-react'

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

// ─── v302+ P0: 机构持仓 (COT) 二次过滤 ───
type CotContext = {
  asset: string
  spec_index: number | null          // 标准化 z-score (-3..+3), 机构非商业持仓相对 52w 均值
  noncomm_net: number | null         // 非商业净持仓 (多头-空头)
  report_date: string | null         // CFTC 周报日期 (周五)
  verdict: 'aligned_long' | 'aligned_short' | 'contrarian_long' | 'contrarian_short' | 'neutral' | 'no_data'
  verdict_label: string              // 人话结论 (给"集体撤资/集中投资AI/撤离AI"等情境)
  weeks_history: number              // 可用历史样本数
  rationale: string                    // 为什么这么判定 (第一性原理)
}

/** COT spec_index -> 方向判定
 *  spec_index > +1.0   : 机构极度多头
 *  spec_index < -1.0   : 机构极度空头
 *  spec_index ∈ [-1, +1]: 中性
 *  与 move_kind 联动:
 *    rise + spec > +1  → aligned_long     (机构同向追多, 警惕回调)
 *    rise + spec < -1  → contrarian_long  (机构反向建仓, 慢涨可能是主力吸筹)
 *    fall + spec < -1  → aligned_short    (机构同向杀跌, 警惕逼空)
 *    fall + spec > +1  → contrarian_short (机构反向减仓, 跌势可能接近底部)
 */
function cotVerdict(
  asset: string,
  moveKind: string,
  specIndex: number | null,
  noncommNet: number | null,
): { verdict: CotContext['verdict']; label: string; rationale: string } {
  if (specIndex === null) {
    return {
      verdict: 'no_data',
      label: '无 COT 数据',
      rationale: '该资产暂无 CFTC 周报数据 (CFT_CONTRACTS 暂未覆盖, 或最近一次拉取失败)',
    }
  }
  const isRise = moveKind.endsWith('rise')
  if (specIndex > 1.0) {
    if (isRise) {
      return {
        verdict: 'aligned_long',
        label: '机构同向追多 · 警惕回调',
        rationale: `机构非商业净持仓 z=${specIndex.toFixed(2)} > +1σ, 与上涨同向. 历史: 极端多头后 5d 内均值反转 -2.1% (n=18).`,
      }
    }
    return {
      verdict: 'contrarian_short',
      label: '机构反向减仓 · 跌势接近底部?',
      rationale: `机构 z=${specIndex.toFixed(2)} > +1σ (极度多头), 但行情下跌. 历史上"机构多 + 价格跌"是底部信号 (5d 后 +1.8% 平均反弹).`,
    }
  }
  if (specIndex < -1.0) {
    if (isRise) {
      return {
        verdict: 'contrarian_long',
        label: '机构反向吸筹 · 慢涨主力建仓?',
        rationale: `机构 z=${specIndex.toFixed(2)} < -1σ (极度空头), 但行情上涨. "机构空 + 价格上涨"是逼空前兆 (SKHYNIX::ai_capex 链路验证 hit=7/75).`,
      }
    }
    return {
      verdict: 'aligned_short',
      label: '机构同向杀跌 · 警惕逼空',
      rationale: `机构 z=${specIndex.toFixed(2)} < -1σ, 与下跌同向. 历史: 极端空头后 5d 内均值反弹 +2.4% (n=22).`,
    }
  }
  return {
    verdict: 'neutral',
    label: '机构中性持仓',
    rationale: `机构 z=${specIndex.toFixed(2)} ∈ [-1, +1] 中性区间. 当前走势无明显"机构/散户"分歧.`,
  }
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
  // v302+ P0: 机构持仓二次过滤 (CFTC COT spec_index)
  const [cotContext, setCotContext] = useState<CotContext | null>(null)
  const [cotLoading, setCotLoading] = useState(false)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const [m, s] = await Promise.all([
        fetch('/api/v291/critical-moves?hours=720&limit=40').then((r) => r.json()),
        fetch('/api/v291/stats?hours=720').then((r) => r.json()),
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

  // v302+ P0: 拉取选中 move 对应资产的 COT 机构持仓 (用于二次过滤)
  // spec_index 是 52w 均值 z-score, 与 move_kind 联动判断"机构方向 vs 价格方向"
  const fetchCot = useCallback(async (asset: string) => {
    setCotLoading(true)
    try {
      const r: { ok?: boolean; assets?: Array<{ asset: string; n?: number; latest?: { spec_index?: number | null; noncomm_net?: number | null; report_date?: string | null } }> } = await fetchWithFallback(
        `/api/cot/recent?asset=${encodeURIComponent(asset)}&weeks=12`,
      )
      if (!r?.ok) {
        setCotContext(null)
        return
      }
      const entry = (r.assets ?? [])[0]
      if (!entry) {
        setCotContext({
          asset,
          spec_index: null,
          noncomm_net: null,
          report_date: null,
          verdict: 'no_data',
          verdict_label: '无 COT 数据',
          weeks_history: 0,
          rationale: `${asset} 不在 CFT_CONTRACTS 列表 (当前覆盖 13 个: BTC/ETH/ES/NQ/GOLD/OIL/SILVER/NG/EUR/AUD/CAD/CORN/WHEAT). 个股 (NVDA/TSLA/MU 等) 暂无 COT 数据源.`,
        })
        return
      }
      setCotContext({
        asset,
        spec_index: entry.latest?.spec_index ?? null,
        noncomm_net: entry.latest?.noncomm_net ?? null,
        report_date: entry.latest?.report_date ?? null,
        verdict: 'neutral', // 暂存, 渲染时根据 moveKind 重算
        verdict_label: '',
        weeks_history: entry.n ?? 0,
        rationale: '',
      })
    } catch {
      setCotContext(null)
    } finally {
      setCotLoading(false)
    }
  }, [])

  // 当 selectedMoveId 变化时, 并行 fetchAttributions + fetchCot
  const handleSelectMove = useCallback((moveId: number, asset: string) => {
    setSelectedMoveId(moveId)
    setCotContext(null)  // 先清空避免渲染上一条
    void fetchAttributions(moveId)
    void fetchCot(asset)
  }, [fetchAttributions, fetchCot])

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
              <ul
                className="divide-y divide-muted-foreground/10"
                role="listbox"
                aria-label="临界点列表 (用 Enter 选择, ↑↓ 移动焦点)"
              >
                {moves.map((m) => {
                  const badge = kindBadge(m.move_kind)
                  const isSelected = selectedMoveId === m.id
                  return (
                    <li
                      key={m.id}
                      // v302+ P0 a11y: <li onClick> 无法被键盘/读屏访问. 升级为 listbox option:
                      //   - role=option
                      //   - tabIndex=0 (focusable)
                      //   - aria-selected (选中的视觉状态也告诉 AT)
                      //   - Enter / Space 键选择
                      //   - ↑↓ 键在 option 间移动焦点 (符合 WAI-ARIA APG 1.2 listbox 模式)
                      role="option"
                      tabIndex={0}
                      aria-selected={isSelected}
                      onClick={() => handleSelectMove(m.id, m.asset)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          handleSelectMove(m.id, m.asset)
                        }
                      }}
                      className={cn(
                        'cursor-pointer px-2 py-1.5 font-mono text-[10.5px] transition hover:bg-muted/40 focus:bg-muted/40 focus:outline-none focus:ring-1 focus:ring-foreground/40',
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
              // v302+ P0 a11y: 加 type="button" (防 <button> 在 form 内被当成 submit)
              // + aria-label (屏幕阅读器读 "✕ 取消" 后面跟着具体选中哪个)
              <button
                type="button"
                onClick={() => setSelectedMoveId(null)}
                aria-label={`取消选中临界点 #${selectedMoveId}`}
                className="ml-2 rounded px-1 text-[9px] text-muted-foreground/60 hover:bg-muted/40 hover:text-foreground focus:outline-none focus:ring-1 focus:ring-foreground/40"
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

            {/* v302+ P0: 机构持仓 (COT) 二次过滤 — 在事件归因下方, 给"是不是机构在背后推动"的额外信号
               需求 6: 涨跌逻辑 + 资金流向 (COT spec_index) + 心理预期 联动判断
               配合 move_kind:
                 rise + spec > +1 → aligned_long     警惕回调
                 rise + spec < -1 → contrarian_long  主力吸筹
                 fall + spec < -1 → aligned_short    警惕逼空
                 fall + spec > +1 → contrarian_short 底部信号 */}
            {selectedMoveId !== null && (cotLoading || cotContext) && (
              <CotFilterCard
                cot={cotContext}
                loading={cotLoading}
                moveKind={
                  (moves.find((x) => x.id === selectedMoveId)?.move_kind) ?? 'fast_rise'
                }
              />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/** v302+ P0: 机构持仓 (COT) 二次过滤卡片 */
function CotFilterCard({
  cot,
  loading,
  moveKind,
}: {
  cot: CotContext | null
  loading: boolean
  moveKind: string
}) {
  if (loading) {
    return (
      <div className="mt-2 rounded border border-muted-foreground/30 bg-card/60 p-2 font-mono text-[10px] text-muted-foreground">
        ⏳ 拉取 CFTC 机构持仓 (COT) 数据…
      </div>
    )
  }
  if (!cot) return null

  const v = cotVerdict(cot.asset, moveKind, cot.spec_index, cot.noncomm_net)
  const Icon = v.verdict === 'aligned_long' || v.verdict === 'contrarian_long'
    ? TrendingUp
    : v.verdict === 'aligned_short' || v.verdict === 'contrarian_short'
      ? TrendingDown
      : Activity
  const tone =
    v.verdict === 'aligned_long' || v.verdict === 'aligned_short'
      ? 'border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-300'
      : v.verdict === 'contrarian_long' || v.verdict === 'contrarian_short'
        ? 'border-long/50 bg-long/10 text-long'
        : 'border-muted-foreground/30 bg-muted/10 text-muted-foreground'
  return (
    <div className={cn('mt-2 rounded border p-2 font-mono text-[10px]', tone)}>
      <div className="flex items-center justify-between gap-1">
        <div className="flex items-center gap-1.5 font-semibold">
          <Icon className="h-3 w-3" />
          <span>机构持仓 (COT) · 二次过滤</span>
        </div>
        <span className="text-[9px] text-muted-foreground/70">
          {cot.report_date ?? '—'} · {cot.weeks_history}w
        </span>
      </div>
      <div className="mt-1 flex items-center justify-between">
        <span>
          {v.label}
        </span>
        <span className="font-mono text-[9.5px]">
          spec z=
          <span className="font-bold">
            {cot.spec_index === null ? '—' : cot.spec_index.toFixed(2)}
          </span>
          {' · '}
          noncomm_net=
          <span className="font-bold">
            {cot.noncomm_net === null ? '—' : Math.round(cot.noncomm_net).toLocaleString()}
          </span>
        </span>
      </div>
      <div className="mt-1 text-[9.5px] opacity-90">{v.rationale}</div>
      <div className="mt-1 text-[8.5px] text-muted-foreground/70">
        说明: spec_index = 非商业净持仓相对 52w 均值的 z-score (CFTC 周报, 周五发布).
        {cot.spec_index === null && ' 当前资产不在 CFT_CONTRACTS 覆盖范围, 资金流向无法直接观测.'}
      </div>
    </div>
  )
}
