'use client'

import { useEffect, useRef, useState } from 'react'
import { Activity, Gauge, CheckCircle2, Lock } from 'lucide-react'
import { cn } from '@/lib/utils'
import { apiGet } from '@/lib/api'
import {
  useChannelPoll,
  CHANNEL_EVENT,
  periodToQuery,
  type PeriodRange,
} from '@/lib/data-channel-standards'
import { useFlashTimer } from '@/lib/hooks/use-flash-timer'
import type {
  PaperStats,
  QuickSimOverall,
  ReviewSnapshot,
  TodayStats,
  TodayFlow,
  FlashPrev,
  FlashState,
} from '@/lib/types/review-overview'

// ─── 命名常量（消除魔法数字） ──────────────────────────────────────

/** 数据"心跳"动画：数字刚刷新时短暂高亮，让用户能看到 polling 在工作。 */
const FLASH_MS = 700

/**
 * 数字变化检测 epsilon。
 * 选 1e-6 是因为 winrate_pct 是 pct 标度（0-100），浮点 round-trip 后可能产生 1e-15
 * 量级误差；1e-6 远大于误差，又远小于"用户能感知的最小变化"（约 0.01%），合适。
 * 不用 === 是因为 JSON 反序列化、百分制四则运算都可能产生微小漂移。
 */
const FLASH_EPSILON = 1e-6

// 反思系统顶部总览带：A 真实持仓战绩 / B 快速评测 / 生效经验。
// 独立拉取数据，不依赖下方面板内部 state，让首页一眼看到整体表现。
// 共享类型见 @/lib/types/review-overview。


function fmtPnl(v: number | null | undefined): { text: string; tone: 'long' | 'short' | 'muted' } {
  if (v == null) return { text: '—', tone: 'muted' }
  return { text: `${v > 0 ? '+' : ''}${v.toFixed(2)}%`, tone: v >= 0 ? 'long' : 'short' }
}

function toneOfWinrate(wr_pct: number | null | undefined): string {
  // v300+ P0+：入参改为 winrate_pct（0-100 标度），与 B 卡片同单位；阈值同步到 50
  if (wr_pct == null) return 'text-foreground'
  return wr_pct >= 50 ? 'text-long' : 'text-short'
}

// v232.5 P0+: 盈亏比格式化 + 着色 — < 1 即便是正期望也是危险信号
function fmtRatio(v: number | string | null | undefined): {
  text: string
  tone: 'long' | 'short' | 'warn' | 'muted'
} {
  if (v == null) return { text: '—', tone: 'muted' }
  // JSON 不允许 Infinity, 后端用字符串 "∞" 或极大值表示
  if (typeof v === 'string') {
    if (v === 'Infinity' || v === 'inf') return { text: '∞(无亏损)', tone: 'long' }
    const n = Number(v)
    if (Number.isFinite(n)) return fmtRatio(n)
    return { text: '—', tone: 'muted' }
  }
  if (!Number.isFinite(v)) return { text: '∞(无亏损)', tone: 'long' }
  const r = v
  let tone: 'long' | 'short' | 'warn' | 'muted'
  if (r >= 1.5) tone = 'long'           // 健康:赚的笔比亏的多 50%
  else if (r >= 1.0) tone = 'warn'      // 持平:刚好够,但容错率低
  else tone = 'short'                   // 危险:亏损幅度比盈利大
  return { text: r.toFixed(2), tone }
}

// v232.5 P0+: 期望值格式化 + 着色 — 负期望就是负期望,胜率再高也救不了
// 单位：与同卡片 fmtPnl(avg_pnl) 保持一致 — 后端 forward_pnl 是 pct 形式(5.0 = 5%)，
// 期望值 = wr*avg_win + (1-wr)*avg_loss 也是 pct 形式,前端不要再 *100。
// 旧版误把 v 当作 decimal 乘 100，导致 -0.07% 显示成 "-7%/笔"（100 倍误差）。
/**
 * 期望值中性带阈值（pct 单位，期望值是 pct form）。
 * 推导：paper 模拟盘单笔名义本金约 $1000，目标月化 5% → 日化 5/30 ≈ 0.17%/笔，
 * 折半取 0.05%/笔 作"基本无收益"判定阈值。低于此值才显示中性，避免过分敏感。
 * 若改成 0 表示严格按符号判断，UI 上"0.02%/笔"也会染红，没必要。
 */
const EXPECTANCY_NEUTRAL_BAND_PCT = 0.05

function fmtExpectancy(v: number | null | undefined): {
  text: string
  tone: 'long' | 'short' | 'warn' | 'muted'
} {
  if (v == null) return { text: '—', tone: 'muted' }
  let tone: 'long' | 'short' | 'warn' | 'muted'
  // 阈值也按 pct 走：>0.05% 算正向，-0.05~0.05 算中性，<-0.05% 算负向
  if (v > EXPECTANCY_NEUTRAL_BAND_PCT) tone = 'long'
  else if (v > -EXPECTANCY_NEUTRAL_BAND_PCT) tone = 'warn'
  else tone = 'short'
  return {
    text: `${v >= 0 ? '+' : ''}${v.toFixed(2)}%/笔`,
    tone,
  }
}

export function ReviewOverview({ period }: { period: PeriodRange }) {
  const [a, setA] = useState<PaperStats | null>(null)
  const [b, setB] = useState<QuickSimOverall | null>(null)
  const [activeCount, setActiveCount] = useState<number | null>(null)
  const [deactCount, setDeactCount] = useState<number | null>(null)
  // v170.4 P0+: 今日信号 / 今日已结算（后端 today 字段）— 与首页 StatCards 完全同源同口径
  const [today, setToday] = useState<TodayStats>({
    signals: 0,
    settled: 0,
    date: '',
    windowSignals: null,
    windowSettled: null,
  })
  // 窗口内新闻流速(与 /api/events/today 同步) — v180 P0+: 接 period 过滤
  const [todayFlow, setTodayFlow] = useState<TodayFlow | null>(null)
  // v176.7：让用户看到 polling 在工作 —— 上次更新时间戳 + 数字变化时短暂高亮
  const [lastUpdated, setLastUpdated] = useState<number>(Date.now())
  const [tick, setTick] = useState(0) // 每秒自增，让「X 秒前」实时刷新
  const prevRef = useRef<FlashPrev>({
    aWinrate: null,
    bWinrate: null,
    activeCount: null,
    newsCount: null,
  })
  // v170.4 P0+: 装载 loadSnapshot/Insights/Today，供顶层 useChannelPoll 回调用
  // （避免把 hook 调用放进 useEffect，违反 Rules of Hooks）
  const loadSnapshotRef = useRef<(() => void) | null>(null)
  const loadInsightsRef = useRef<(() => void) | null>(null)
  const loadTodayRef = useRef<(() => void) | null>(null)
  const aliveRef = useRef(false)
  // v302 P0+：相对时间 tick 的 setInterval 句柄，visibilitychange 时停掉避免后台重渲染
  const timerRef = useRef<number | null>(null)
  // v180 P0+：period 放进 ref 让 useChannelPoll 每 5s 调用能读到最新值
  const periodRef = useRef<PeriodRange>(period)
  useEffect(() => {
    periodRef.current = period
  }, [period])

  // v176.7：数字变化时短暂高亮（FLASH_MS ms 后自动熄灭）。
  // 用 useFlashTimer 替代裸 window.setTimeout —— 自动取消 unmount 后挂起的定时器，
  // 数字频繁变化时旧定时器被新定时器自然接管（不叠加延长），每 key 独立跟踪。
  const [flash, setFlash] = useState<FlashState>({
    a: false,
    b: false,
    rule: false,
    news: false,
  })
  const flashOnFor = useFlashTimer(setFlash, FLASH_MS)

  useEffect(() => {
    let alive = true

    // 与模拟盘同源：paper = /api/replay/stats 口径（真实持仓），
    // quick_sim = quick_sim.snapshot() 口径（快速评测），保证反思系统与模拟盘数字一致。
    // v170.4 P0+: 后端新增 today.{signals,settled}，与首页 StatCards 同步口径
    //                解决"26 今日信号 vs 59 累计已结算"让用户卡死的问题。
    // v180 P0+：传入 hours / from_ts / to_ts（PeriodSelector 选定），
    //                让 B·快速评测 卡片按所选时段过滤；today 字段保留不动。
    const loadSnapshot = () => {
      const params = periodToQuery(periodRef.current)
      const qs = params.toString()
      return apiGet<ReviewSnapshot>(`/review/snapshot${qs ? `?${qs}` : ''}`)
        .then((d: ReviewSnapshot) => {
          if (!alive) return
          const nextA = d?.paper ?? null
          const nextB = d?.quick_sim ?? null
          setA(nextA)
          setB(nextB)
          setToday({
            signals: Number(d?.today?.signals ?? 0),
            settled: Number(d?.today?.settled ?? 0),
            date: d?.today?.date ?? '',
            // v180 P0+：窗口统计（如 PeriodSelector 选中 7d/30d/自定义），用于对账条
            windowSignals: d?.window?.signals ?? null,
            windowSettled: d?.window?.settled ?? null,
          })
          // v176.7：检测数字变化并触发短暂高亮
          // v300+ P0+：A/B 都读 winrate_pct（0-100 标度），单位一致才能稳定比较
          const prev = prevRef.current
          const aWin = nextA?.winrate_pct ?? null
          const bWin = nextB?.winrate_pct ?? null
          const aChanged =
            prev.aWinrate !== null && aWin !== null && Math.abs(prev.aWinrate - aWin) > FLASH_EPSILON
          const bChanged =
            prev.bWinrate !== null && bWin !== null && Math.abs(prev.bWinrate - bWin) > FLASH_EPSILON
          prevRef.current = { ...prev, aWinrate: aWin, bWinrate: bWin }
          if (aChanged || bChanged) {
            // 同时点亮多个 key 时，复用一次 setFlash（避免连续两次 setState）
            setFlash((f) => ({ ...f, a: aChanged, b: bChanged }))
            // flashOnFor 接受 key[]，每 key 独立跟踪定时器，旧定时器自动被新定时器覆盖
            const onKeys: Array<'a' | 'b'> = []
            if (aChanged) onKeys.push('a')
            if (bChanged) onKeys.push('b')
            flashOnFor(onKeys)
          }
          setLastUpdated(Date.now())
        })
        .catch(() => {})
    }

    const loadInsights = () => {
      // v180 P0+：改用 /api/review/insights/count(支持 hours/from_ts/to_ts),
      // 与全局时段保持一致 —— 切到"近 7 天"则统计 7 天内新生效/新停用规则。
      const params = periodToQuery(periodRef.current)
      const qs = params.toString()
      const url = `/review/insights/count${qs ? `?${qs}` : ''}`
      return apiGet<{ active?: number; deact?: number; total?: number }>(url)
        .then((d: { active?: number; deact?: number; total?: number }) => {
          if (!alive) return
          const active = Number(d?.active ?? 0)
          const deact = Number(d?.deact ?? 0)
          setActiveCount(active)
          setDeactCount(deact)
          const prev = prevRef.current
          if (prev.activeCount !== null && prev.activeCount !== active) {
            setFlash((f) => (f.rule ? f : { ...f, rule: true }))
            flashOnFor('rule')
          }
          prevRef.current = { ...prev, activeCount: active }
          setLastUpdated(Date.now())
        })
        .catch(() => {})
    }

    // 窗口内新闻流速（与 StatCards 同源 /api/events/today，但走 hours/from_ts/to_ts 过滤）—— v180 P0+
    // 让反思页能对照"为什么 paper 还是 0" = 因为所选窗口内还没结算;
    // 切到"近 7 天"则显示 7 天内的新闻/分析/信号流速。
    const loadToday = () => {
      const params = periodToQuery(periodRef.current)
      const qs = params.toString()
      const url = `/events/today${qs ? `?${qs}` : ''}`
      return apiGet<{
        news_count?: number
        analyzed_count?: number
        signal_count?: number
        live_signal_count?: number | null
        live_signal_window_hours?: number
      }>(url)
        .then((d: {
          news_count?: number
          analyzed_count?: number
          signal_count?: number
          // v300+ 新鲜度：仅最近 N 小时 BUY/SELL，LLM/数据源全挂时如实归零。
          // 与首页 StatCards 走同源同口径：反思页"实时窗口信号"也读这个，
          // 不读 signal_count（旧全天计数会把早场 stale 单撑场）。
          live_signal_count?: number | null
          live_signal_window_hours?: number
        }) => {
          if (!alive) return
          const flow = {
            news: Number(d?.news_count ?? 0),
            analyzed: Number(d?.analyzed_count ?? 0),
            // 实时信号数：window 内的 BUY/SELL ∩ 最近 N 小时（LLM/数据源全挂时归零）。
            // 兼容旧后端（无 live_signal_count）时退化到 signal_count。
            signal: Number(d?.live_signal_count ?? d?.signal_count ?? 0),
            signalStale: Number(d?.signal_count ?? 0),
            liveWindowHours: Number(d?.live_signal_window_hours ?? 0),
          }
          setTodayFlow(flow)
          const prev = prevRef.current
          if (prev.newsCount !== null && prev.newsCount !== flow.news) {
            setFlash((f) => (f.news ? f : { ...f, news: true }))
            flashOnFor('news')
          }
          prevRef.current = { ...prev, newsCount: flow.news }
          setLastUpdated(Date.now())
        })
        .catch(() => {})
    }

    // 首次拉取（在 effect 内一次性执行），后续由组件顶层 useChannelPoll 驱动
    void loadSnapshot()
    void loadInsights()
    void loadToday()

    // 把刷新函数挂到 ref 上，供顶层 useChannelPoll 调用
    loadSnapshotRef.current = loadSnapshot
    loadInsightsRef.current = loadInsights
    loadTodayRef.current = loadToday
    aliveRef.current = true

    return () => {
      alive = false
      aliveRef.current = false
      loadSnapshotRef.current = null
      loadInsightsRef.current = null
      loadTodayRef.current = null
    }
  }, [])

  // v170.4 P0+: 统一实时通道 — 必须在组件顶层调用 hook，不能放在 useEffect 里（违反 Rules of Hooks）
  // 5s 轮询 + SSE quick_sim_update 立即触发；backend 已对齐 _QUICK_SIM_CACHE = 2s
  useChannelPoll({
    refresh: () => {
      if (!aliveRef.current) return
      loadSnapshotRef.current?.()
      loadInsightsRef.current?.()
      loadTodayRef.current?.()
    },
    intervalMs: 5_000,
    triggerOn: [
      CHANNEL_EVENT.QUICK_SIM_UPDATE,
      CHANNEL_EVENT.SETTLEMENT,
      CHANNEL_EVENT.REFRESH_REVIEW,
    ],
  })

  // v180 P0+：period 变化时立即重拉一次（不等 5s 轮询窗口），
  // 让 PeriodSelector 切档有即时反馈。
  useEffect(() => {
    if (!aliveRef.current) return
    loadSnapshotRef.current?.()
  }, [period])

  // v176.7：每秒自增一次 tick，让「X 秒前」实时刷新（数字本身不变也看得到 polling 在工作）
  // v302 P0+：tab 切到后台就暂停（visibilitychange），避免后台每 1s re-render 浪费 CPU；
  //         每秒触发 setTick 即使在 hidden tab 也会引发 reconcile，且组件本身用户看不到。
  useEffect(() => {
    const start = () => {
      if (timerRef.current != null) return  // 已在跑
      timerRef.current = window.setInterval(() => setTick((x) => x + 1), 1000)
    }
    const stop = () => {
      if (timerRef.current != null) {
        window.clearInterval(timerRef.current)
        timerRef.current = null
      }
    }
    const onVisibility = () => (document.hidden ? stop() : start())
    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])
  const ageSec = Math.max(0, Math.floor((Date.now() - lastUpdated) / 1000))
  const ageLabel =
    ageSec < 5 ? '刚刚' : ageSec < 60 ? `${ageSec}s 前` : `${Math.floor(ageSec / 60)}m 前`

  const aPnl = fmtPnl(a?.avg_pnl)
  const bPnl = fmtPnl(b?.total_pnl_pct)
  const bWinrate = b?.winrate_pct ?? null
  // v232.5 P0+: A 卡片底部的盈亏比 + 期望值
  const aRatio = fmtRatio(a?.profit_loss_ratio)
  const aExpectancy = fmtExpectancy(a?.expected_value_per_trade)
  const aExpectancyLabel = a?.expectancy_label ?? null

  // ── 主数字永远显示胜率（即使为 0）；"全平/待结算"放底部 ──
  const aSettled = a?.settled ?? 0
  const aWins = a?.wins ?? 0
  const aLosses = a?.losses ?? 0
  const aDecided = aWins + aLosses
  const aHasData = aDecided > 0
  // A 卡片底部辅助：缺数据原因
  const aFootnote = !a
    ? ''
    : aHasData
      ? ''
      : aSettled > 0
        ? '· 暂无胜负（已结算但胜负相等）'
        : aSettled === 0 && (a?.tracking ?? 0) > 0
          ? '· 待结算（持仓未到期）'
          : '· 待结算'
  // 平均盈亏：forward_pnl 全 NULL 时显式说「待结算」而不是「0.00%」
  const aAvgPnlText = a?.avg_pnl == null && aSettled > 0 ? '待结算' : aPnl.text

  // B 卡片底部辅助
  const bSettled = b?.settled ?? 0
  const bWins = b?.wins ?? 0
  const bLosses = b?.losses ?? 0
  const bDecided = bWins + bLosses
  const bFootnote = !b
    ? ''
    : b?.open === 0 && (b?.total ?? 0) > 0
      ? '· ✓ 全结（待新交易结算）'
      : bSettled > 0 && bDecided === 0
        ? `· ${bSettled} 笔全平（无胜负）`
        : '· 待结算'

  return (
    <section className="space-y-2">
      {/* v180 P0+：时段选择器已上移到 LiveDesk 顶层(全局 PeriodSelector)
          — 这里只显示当前时段,4 张卡片自动跟随。卡片右上角小徽章标 "时段"。*/}
      {/* v180 P0+：全局时段徽章(注入到每张卡片右上角),让"4 张卡片同源同窗"视觉化。
          关键:同一个 period.label 出现在 7 张卡片上 = 用户一眼能验证"这 4 个数字时间窗一致" */}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <div
          className={cn(
            'surface-card p-3 transition-all duration-500',
            flash.a && 'ring-2 ring-primary/40 bg-primary/5',
          )}
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">A · 真实持仓战绩</span>
            <div className="flex items-center gap-1">
              <span
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[8px] text-primary"
                title={`窗口内开仓的真实持仓战绩(按 entry_time 过滤) · ${period.label}`}
              >
                {period.label}
              </span>
              <Gauge className="h-4 w-4 text-primary" aria-hidden="true" />
            </div>
          </div>
          <p className={cn('mt-2 font-mono text-2xl font-bold tabular-nums', toneOfWinrate(a?.winrate_pct))}>
            {/* v300+ P0+：A/B 卡片同单位显示（pct），不读 decimal winrate 后再 ×100 */}
            {a?.winrate_pct != null ? `${a.winrate_pct.toFixed(1)}%` : '—'}
          </p>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            共 {a?.total ?? '—'} 笔 · 跟踪 {a?.tracking ?? '—'} · 已结算 {aSettled} 笔 · 平均盈亏{' '}
            <span className={aPnl.tone === 'long' ? 'text-long' : aPnl.tone === 'short' ? 'text-short' : 'text-muted-foreground'}>
              {aAvgPnlText}
            </span>
            <span className="text-muted-foreground/70">{aFootnote}</span>
          </p>
          {/* v232.5 P0+: 盈亏比 + 期望值 — 让"高胜率但负期望"的假象显形 */}
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            盈亏比{' '}
            <span
              className={
                aRatio.tone === 'long' ? 'text-long'
                  : aRatio.tone === 'short' ? 'text-short'
                    : aRatio.tone === 'warn' ? 'text-hold'
                      : 'text-muted-foreground'
              }
              title="平均盈利 / |平均亏损|。< 1.0 即亏损幅度大于盈利,胜率再高也是亏。1.5+ 算健康。"
            >
              {aRatio.text}
            </span>
            {' · '}期望值{' '}
            <span
              className={
                aExpectancy.tone === 'long' ? 'text-long'
                  : aExpectancy.tone === 'short' ? 'text-short'
                    : aExpectancy.tone === 'warn' ? 'text-hold'
                      : 'text-muted-foreground'
              }
              title="winrate*avg_win + (1-winrate)*avg_loss。负期望=长期必亏,与胜率无关。"
            >
              {aExpectancy.text}
            </span>
            {aExpectancyLabel && aExpectancyLabel !== '数据不足' && (
              <span
                className={
                  aExpectancyLabel === '正向' ? 'ml-1 inline-flex items-center gap-0.5 text-long'
                    : aExpectancyLabel === '负向' ? 'ml-1 inline-flex items-center gap-0.5 text-short'
                      : 'ml-1 inline-flex items-center gap-0.5 text-hold'
                }
              >
                {aExpectancyLabel === '正向' ? '✓' : aExpectancyLabel === '负向' ? '⚠' : '≈'}
                {aExpectancyLabel}
              </span>
            )}
          </p>
          {/* v300.5 P0+：口径披露 — 防止「总盈亏 -0.25%」被误读为「含全部持仓」 */}
          {(a?.tracking ?? 0) > 0 && aSettled > 0 && (
            <p
              className="mt-0.5 font-mono text-[9px] text-muted-foreground/60"
              title="口径说明: 胜率 / 平均盈亏 / 盈亏比 / 期望值 均仅统计已结算仓位;tracking(N)= 持仓未到期,未计入上述任何指标。浮盈亏可在 LiveDesk 顶部查看。"
            >
              · 上述数值仅含已结算 {aSettled} 笔; 跟踪 {a?.tracking ?? 0} 笔浮盈亏未计入
            </p>
          )}
        </div>

        <div
          className={cn(
            'surface-card p-3 transition-all duration-500',
            flash.b && 'ring-2 ring-primary/40 bg-primary/5',
          )}
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">B · 快速评测</span>
            <div className="flex items-center gap-1">
              <span
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[8px] text-primary"
                title="当前全局时段(与上方 + 下方 2 张卡片同源同窗)"
              >
                {period.label}
              </span>
              <Activity className="h-4 w-4 text-hold" aria-hidden="true" />
            </div>
          </div>
          <p className={cn('mt-2 font-mono text-2xl font-bold tabular-nums', bWinrate != null ? (bWinrate >= 50 ? 'text-long' : 'text-short') : 'text-muted-foreground')}>
            {bWinrate != null ? `${bWinrate.toFixed(1)}%` : '—'}
          </p>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            共 {b?.total ?? '—'} 笔 · 待结算 {b?.open ?? '—'} · 已结算 {bSettled} · 总盈亏{' '}
            <span className={bPnl.tone === 'long' ? 'text-long' : bPnl.tone === 'short' ? 'text-short' : 'text-muted-foreground'}>
              {bPnl.text}
            </span>
            {b?.open === 0 && (b?.total ?? 0) > 0 ? (
              <span
                className="ml-1 inline-flex items-center gap-0.5 text-muted-foreground/70"
                title="所有快速评测已结算，胜率 = wins/settled 在新交易结完前不会再变"
              >
                <Lock className="h-2.5 w-2.5" aria-hidden="true" />
                全结
              </span>
            ) : null}
            <span className="text-muted-foreground/70">{bFootnote}</span>
          </p>
          {/* v300.5 P0+：口径披露 — B 卡片总盈亏同样仅含已结算笔数 */}
          {(b?.open ?? 0) > 0 && bSettled > 0 && (
            <p
              className="mt-0.5 font-mono text-[9px] text-muted-foreground/60"
              title="口径说明: 胜率 / 总盈亏 仅统计已结算仓位;open(M)= 持仓未到期,未计入上述任何指标。浮盈亏可在 LiveDesk 顶部查看。"
            >
              · 上述总盈亏仅含已结算 {bSettled} 笔; 待结算 {b?.open ?? 0} 笔浮盈亏未计入
            </p>
          )}
        </div>

        <div
          className={cn(
            'surface-card p-3 transition-all duration-500',
            flash.rule && 'ring-2 ring-primary/40 bg-primary/5',
          )}
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">生效经验规则</span>
            <div className="flex items-center gap-1">
              <span
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[8px] text-primary"
                title="窗口内新生效 / 新停用规则(切到全期才能看到所有历史规则)"
              >
                {period.label}
              </span>
              <CheckCircle2 className="h-4 w-4 text-long" aria-hidden="true" />
            </div>
          </div>
          <p className="mt-2 font-mono text-2xl font-bold tabular-nums text-long">{activeCount ?? '—'}</p>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            生效 {activeCount ?? '—'} 条 · 停用 {deactCount ?? '—'} 条（回灌 AI 分析）
          </p>
        </div>

        {/* 今日新闻流速（与首页 StatCards 同源 /api/events/today）—— 解释"为什么 paper=0" */}
        <div
          className={cn(
            'surface-card p-3 transition-all duration-500',
            flash.news && 'ring-2 ring-primary/40 bg-primary/5',
          )}
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">新闻流速</span>
            <div className="flex items-center gap-1">
              <span
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[8px] text-primary"
                title="窗口内新闻 / 分析 / 信号流速(与首页 StatCards 同源 /api/events/today)"
              >
                {period.label}
              </span>
              <span className="font-mono text-[9px] text-muted-foreground/60">↔ 首页</span>
            </div>
          </div>
          <p className="mt-2 font-mono text-2xl font-bold tabular-nums text-foreground">
            {todayFlow ? todayFlow.news.toLocaleString() : '—'}
          </p>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            入库 {todayFlow?.news ?? '—'} · 已分析 {todayFlow?.analyzed ?? '—'} · 信号 {todayFlow?.signal ?? '—'}
            {todayFlow && todayFlow.liveWindowHours > 0 ? (
              <span className="ml-1 text-muted-foreground/70">
                · 实时窗口 {todayFlow.liveWindowHours}h
                {todayFlow.signalStale > todayFlow.signal ? (
                  <span className="text-muted-foreground/60">
                    （全天 {todayFlow.signalStale.toLocaleString()}，陈旧已滤）
                  </span>
                ) : null}
              </span>
            ) : null}
          </p>
          <p className="mt-0.5 font-mono text-[9px] text-muted-foreground/60" title="反思页战绩 = 已结算的 ai_decisions（settlement 后才计入胜率）；首页新闻流速 = 当日实时入库。今日新闻进来后要等持仓到期结算才会进入反思页战绩统计。">
            反思战绩 = 已结算；首页 = 实时入库
          </p>
        </div>
      </div>

    </section>
  )
}