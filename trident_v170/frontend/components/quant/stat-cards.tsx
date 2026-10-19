'use client'

import { useEffect, useRef, useState } from 'react'
import { Activity, Gauge, CheckCircle2, Cpu } from 'lucide-react'
import { cn } from '@/lib/utils'
import { API_BASE } from '@/lib/api'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import {
  useChannelPoll,
  CHANNEL_EVENT,
  periodToQuery,
  type PeriodRange,
} from '@/lib/data-channel-standards'
import { parseBjTimestamp } from '@/lib/tz-utils'

type Props = {
  events: ApiEvent[]
  activeMarket: MarketCategory
  // v180 P0+：「今日信号」卡片点击 → 滚动到 EventBus 并按"今日/仅信号"过滤。
  // 由 LiveDesk 注入；未注入时按钮降级为只显示文案不响应点击。
  onSignalsCardClick?: () => void
  // v188 P0+：当前是否处于"今日信号过滤"激活态（卡片需要视觉提示）。
  signalsFilterActive?: boolean
  // v180 P0+：全局时段(LiveDesk 顶层 PeriodSelector 选定),
  // 影响 3 张"今日"卡片(今日信号 / 市场情绪 / 今日已分析)。
  // 没注入时退化为"上海日"以保证旧调用方不报错。
  period?: PeriodRange
}

type TodayStats = {
  news_count: number
  analyzed_count: number
  total_count?: number
  signal_count?: number
  // v300+ 新鲜度：仅最近 N 小时内的 BUY/SELL；LLM/数据源全挂时如实归零。
  live_signal_count?: number
  live_signal_window_hours?: number
}

type ModelHealth = {
  name: string
  model: string
  available: boolean
  status_code: number | null
  latency_ms: number
  note: string
}

type ModelsPayload = {
  channels: ModelHealth[]
  recent_models: { model: string; count: number }[]
  current_model: string | null
  current_model_at: string | null
  pending_news: number
  analyzed_24h: number
  engine_openrouter: { openrouter_ok: boolean; status_code: number | null; error: string; ts: number } | null
  engine_model_pool: string[] | null
}

export function StatCards({ events, activeMarket, onSignalsCardClick, signalsFilterActive = false, period }: Props) {
  const [today, setToday] = useState<TodayStats | null>(null)
  const [models, setModels] = useState<ModelsPayload | null>(null)
  // v180 P0+：先按全局时段筛(切到近 7 天,只统计 7 天内事件),
  // 再按 activeMarket 筛;这样「市场情绪」「样本」都跟时段一致。
  // 没 period 时(旧调用方)不过滤。
  // v302+ P0+【修复】用 parseBjTimestamp 把 ApiEvent.timestamp 强制按 Asia/Shanghai 解析,
  // 避免浏览器/系统时区不是 UTC+8 时(new Date 把无 tz 串当浏览器本地时间),
  // 跨浏览器产生 8h 偏差(今天 0~8 点的新闻被错误归入"昨日")。
  const periodFiltered = period && (period.fromTs !== null || period.toTs !== null)
    ? events.filter((e) => {
        if (!e.timestamp) return false
        const t = parseBjTimestamp(e.timestamp)
        if (t === null) return false
        if (period.fromTs !== null && t < period.fromTs) return false
        if (period.toTs !== null && t > period.toTs) return false
        return true
      })
    : events
  const filtered = activeMarket === 'ALL'
    ? periodFiltered
    : periodFiltered.filter((e) => e.market_category === activeMarket)

  // 情绪样本：只看已分析且非 HOLD 的信号，避免 HOLD/未分析污染平均值
  const doneSignals = filtered.filter(
    (e) => e.analysis_status === 'DONE' && e.action !== 'HOLD' && e.action !== 'FAILED',
  )
  const sample = doneSignals.length
  const rawAvg = sample > 0
    ? doneSignals.reduce((s, e) => s + e.score, 0) / sample
    : 0
  const buyPct = sample > 0
    ? Math.round(doneSignals.filter((e) => e.action === 'BUY').length / sample * 100)
    : 0
  // 今日信号数：v300+ 优先读后端 live_signal_count（仅最近 N 小时 BUY/SELL），
  // 这样 LLM/数据源全挂时数字如实归零，不会拿早上 stale 单撑场。
  // 兜底链：live → 全天 signal_count → 当前列表计数 → 0。
  const signalCount = today?.live_signal_count
    ?? today?.signal_count
    ?? filtered.filter(
      (e) => e.analysis_status === 'DONE' && e.action !== 'HOLD' && e.action !== 'FAILED',
    ).length
  const newsCount = today?.news_count ?? filtered.length
  const analyzedCount = today?.analyzed_count ?? filtered.filter((e) => e.analysis_status === 'DONE').length

  const labelMap: Record<string, string> = {
    ALL: '全市场',
    CRYPTO: '加密货币',
    GOLD: '黄金',
    OIL: '原油',
    // v180 P0+：补 STOCK，否则「美股/A股/个股/海力士」新闻会被后端转 OTHER 兜底，
    // 前端只能看到「其他」一类 — 看起来像 AI 没分类。详见 DATA_CHANNEL_STANDARDS.md §5。
    // v212 fix: 'OTHER' 已从 MarketCategory 删除——以前被它接住的「美股/A股/个股」现归 STOCK；剩极少数"已通过筛选但无直接品种"事件归 AGGREGATE。
    // v?? 海力士 cap：单列一类（韩股海力士/Micron），不再混入 STOCK 大类。
    STOCK: '股票',
    HYNIX: '海力士',
    MACRO: '宏观',
    AGGREGATE: '聚合事件',
  }

  // v170.4 P0+: loadToday 通过 ref 暴露给顶层 useChannelPoll，避免把 hook 放进 useEffect
  const loadTodayRef = useRef<(() => void) | null>(null)
  const aliveRef = useRef(false)
  // v180 P0+：period 放进 ref 让 useChannelPoll 5s 轮询能拿到最新值
  const periodRef = useRef<PeriodRange | undefined>(period)
  useEffect(() => {
    periodRef.current = period
  }, [period])

  useEffect(() => {
    let cancelled = false
    async function loadToday() {
      try {
        // v180 P0+：走 periodToQuery 把全局时段转 hours/from_ts/to_ts 拼到 URL
        // 没 period 时(旧调用方)不带参数,后端退化为"上海日"兼容
        const params = periodRef.current ? periodToQuery(periodRef.current) : null
        const qs = params?.toString() ?? ''
        const url = `${API_BASE}/events/today${qs ? `?${qs}` : ''}`
        const res = await fetch(url)
        if (!res.ok) return
        const data = await res.json() as TodayStats
        if (!cancelled) setToday(data)
      } catch {
        // 计数接口失败时回退到当前列表长度
      }
    }
    void loadToday()
    loadTodayRef.current = loadToday
    aliveRef.current = true
    return () => {
      cancelled = true
      aliveRef.current = false
      loadTodayRef.current = null
    }
  }, [])

  // v170.4 P0+: 走统一 useChannelPoll — 5s 轮询 + SSE quick_sim_update 事件触发立即重拉。
  // 之前 15s 轮询 + 反思页 10s 轮询 → 用户看 "26 今日信号 vs 59 累计已结算" 等待太长。
  // 必须在组件顶层调用 hook（Rules of Hooks）。
  useChannelPoll({
    refresh: () => {
      if (!aliveRef.current) return
      loadTodayRef.current?.()
    },
    intervalMs: 5_000,
    triggerOn: [
      CHANNEL_EVENT.QUICK_SIM_UPDATE,
      CHANNEL_EVENT.REFRESH_TODAY,
      CHANNEL_EVENT.REFRESH_REVIEW,
    ],
  })

  // LLM 通道健康：30s 轮询（接口本身 30s 缓存）
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/health/models`, { cache: 'no-store' })
        if (!res.ok) return
        const data = await res.json() as ModelsPayload
        if (!cancelled && data && Array.isArray(data.channels)) setModels(data)
      } catch {
        // 忽略：接口不可用时保持旧值/空
      }
    }
    void load()
    const timer = setInterval(() => void load(), 30_000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const tone = rawAvg >= 0.2 ? 'text-long' : rawAvg <= -0.2 ? 'text-short' : 'text-primary'
  const mood = rawAvg >= 0.25 ? '看涨' : rawAvg <= -0.25 ? '看跌' : '中性'
  const moodSub = rawAvg >= 0.25 ? '偏多信号' : rawAvg <= -0.25 ? '偏空信号' : '信号分散'

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {/* LLM 通道健康（放今日信号左边） */}
      <div className="glass rounded-lg border border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            LLM 通道
          </span>
          <Cpu className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        </div>
        <div className="mt-2 space-y-1">
          {!models || models.channels.length === 0 ? (
            <p className="font-mono text-[11px] text-muted-foreground">探测中…</p>
          ) : (
            models.channels.map((m) => (
              <div key={m.name} className="flex items-center gap-1.5 font-mono text-[11px]">
                <span
                  className={`h-1.5 w-1.5 shrink-0 rounded-full ${m.available ? 'bg-long' : 'bg-short'}`}
                  title={m.available ? '可用' : '不可用'}
                />
                <span className="w-16 shrink-0 truncate text-foreground" title={m.name}>{m.name}</span>
                <span className={`ml-auto ${m.available ? 'text-long' : 'text-short'}`}>
                  {m.available ? `${Math.round(m.latency_ms / 10) / 100}s` : `${m.status_code ?? 'ERR'}`}
                </span>
              </div>
            ))
          )}
        </div>
        <p className="mt-1 border-t border-border/50 pt-1 font-mono text-[10px] text-muted-foreground">
          当前分析:{' '}
          <span className="text-foreground">
            {models?.current_model ?? '—'}
          </span>
          {models?.current_model_at && (
            <span className="text-muted-foreground/70"> @ {models.current_model_at.slice(11, 16)}</span>
          )}
        </p>
        {models?.engine_openrouter && (
          <p className="font-mono text-[10px] text-muted-foreground">
            引擎探测 OpenRouter:{' '}
            <span className={models.engine_openrouter.openrouter_ok ? 'text-long' : 'text-short'}>
              {models.engine_openrouter.openrouter_ok ? '✓ 可用' : `✗ ${models.engine_openrouter.status_code ?? 'ERR'}`}
            </span>
            {!models.engine_openrouter.openrouter_ok && models.engine_openrouter.error && (
              <span className="block truncate text-short/70" title={models.engine_openrouter.error}>
                {models.engine_openrouter.error}
              </span>
            )}
          </p>
        )}
        {models?.engine_model_pool && models.engine_model_pool.length > 0 && (
          <p className="font-mono text-[10px] text-muted-foreground" title={models.engine_model_pool.join(' | ')}>
            引擎池: <span className="text-foreground">{models.engine_model_pool.slice(0, 4).join(', ')}</span>
            {models.engine_model_pool.length > 4 && <span className="text-muted-foreground/70"> …</span>}
          </p>
        )}
        <p className="font-mono text-[10px] text-muted-foreground">
          待分析 <span className={models && models.pending_news > 50 ? 'text-short' : 'text-foreground'}>{models?.pending_news ?? '—'}</span> 条
          {' · '}24h 分析 {models?.analyzed_24h ?? '—'} 条
        </p>
      </div>

      {/* v180 P0+：「今日信号」卡片改为可点击 —— 点击后滚动到 EventBus 并按
          "今日 + 仅信号"过滤，让用户看到这 33 条信号对应的真实新闻流。
          v188 P0+：sticky toggle —— 激活态用 ring + 实心背景明显区分；再点一次关闭。
          v180 P0+：标题改"信号"+ 右上角时段徽章,跟 ReviewOverview 4 张卡片同源同窗。*/}
      <button
        type="button"
        onClick={onSignalsCardClick}
        disabled={!onSignalsCardClick}
        title={
          signalsFilterActive
            ? `已过滤为今日 ${signalCount.toLocaleString()} 条信号（再点关闭）`
            : onSignalsCardClick
            ? `点击过滤为今日 ${signalCount.toLocaleString()} 条信号对应的事件流`
            : '点击展开今日信号对应事件流（未启用）'
        }
        className={cn(
          'glass group rounded-lg border px-4 py-3 text-left transition-all duration-200',
          signalsFilterActive
            ? 'border-long bg-long/10 ring-2 ring-long/50 shadow-md shadow-long/20'
            : 'border-border hover:border-long/60 hover:bg-long/5 hover:shadow-sm',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-long/50',
          !onSignalsCardClick && 'cursor-default hover:border-border hover:bg-transparent',
        )}
      >
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground group-hover:text-long/90">
            窗口信号
            {signalsFilterActive && <span className="ml-1 text-long">●</span>}
          </span>
          <div className="flex items-center gap-1">
            {period && (
              <span
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[8px] text-primary"
                title={`当前全局时段 · ${period.label}`}
              >
                {period.label}
              </span>
            )}
            <Activity className={cn('h-4 w-4 text-long transition-transform group-hover:scale-110')} aria-hidden="true" />
          </div>
        </div>
        <p className={cn('mt-2 font-mono text-2xl font-bold tabular-nums', 'text-long')}>
          {signalCount.toLocaleString()}
        </p>
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          {labelMap[activeMarket] ?? activeMarket} · 窗口新闻 {newsCount} · 库内 {today?.total_count ?? newsCount} · 已分析 {analyzedCount}
          {today?.live_signal_window_hours ? (
            <span className="ml-1 text-muted-foreground/70">
              · 实时窗口 {today.live_signal_window_hours}h
              {(today?.signal_count ?? 0) > signalCount ? (
                <span className="text-muted-foreground/60">
                  （全天 {(today?.signal_count ?? 0).toLocaleString()}，陈旧已滤）
                </span>
              ) : null}
            </span>
          ) : null}
        </p>
        <p className="mt-0.5 font-mono text-[9px] text-muted-foreground/60" title="信号数 = ai_decisions 中窗口内的 BUY/SELL 决策行（一条新闻可拆出主品种+副品种多个决策）；新闻数 = 窗口内入库的新闻条数。两表口径不同，非统计错误。v300+ 默认仅统计最近 N 小时 BUY/SELL，LLM/数据源全挂时如实归零。">
          信号按决策行计（多品种新闻拆多个信号）｜新闻按条计｜实时窗口 {today?.live_signal_window_hours ?? '–'}h
        </p>
        {/* v303+ P0+【调试】实时显示 live_signal_count vs signal_count vs 新闻数, 便于
            排查"窗口数字永远是 0"类 BUG (历史: ai_decisions.ts 列 v302+ 加了但 INSERT
            漏写 → 全 NULL → 窗口查询全漏)。这里直接读后端原始字段, 不进兜底链。
            后端 ts 列非 NULL 时 live 应该 > 0; 若 live==0 且 signal>0 → ts 数据有问题
            (或窗口内真的没新信号); 若 live==0 且 signal==0 → 真无信号. */}
        <p
          className="mt-0.5 font-mono text-[9px] text-muted-foreground/50"
          title={`DB 原始计数 (v303+ 调试):
live_signal_count (近 ${today?.live_signal_window_hours ?? '?'}h BUY/SELL) = ${today?.live_signal_count ?? '?'}
signal_count (全天 BUY/SELL) = ${today?.signal_count ?? '?'}
news_count (窗口内新闻) = ${today?.news_count ?? '?'}
analyzed_count (窗口已分析) = ${today?.analyzed_count ?? '?'}

若 live=0 且 signal>0: ai_decisions.ts 列有 NULL 或窗口过滤逻辑异常.
若 live=0 且 signal=0: 窗口内确实无 BUY/SELL (LLM 全 HOLD 或没新决策).
若 signal=0 且 analyzed>0: LLM 全部 HOLD/FAIL, 这是真实情况.`}
        >
          [DB] live={today?.live_signal_count ?? '–'} · signal={today?.signal_count ?? '–'} · news={today?.news_count ?? '–'} · analyzed={today?.analyzed_count ?? '–'}
        </p>
        {onSignalsCardClick ? (
          <p className={cn(
            'mt-1 font-mono text-[9px]',
            signalsFilterActive ? 'text-long font-semibold' : 'text-long/80 group-hover:underline',
          )}>
            {signalsFilterActive ? '✓ 已过滤为窗口信号（再点关闭）' : '👉 点击过滤为窗口信号 →'}
          </p>
        ) : null}
      </button>

      <div className="glass rounded-lg border border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            市场情绪（已分析信号）
          </span>
          {period && (
            <span
              className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[8px] text-primary"
              title={`当前全局时段 · ${period.label} 内的样本`}
            >
              {period.label}
            </span>
          )}
          <Gauge className={cn('h-4 w-4', tone)} aria-hidden="true" />
        </div>
        <p
          className={cn('mt-2 font-mono text-2xl font-bold tabular-nums', tone)}
          title="sentiment_score 平均强度（-100%..+100%）：+30% 偏多、-30% 偏空、±25% 内中性。底层是 ai_decisions.sentiment_score 字段（-1..+1 标度），这里 ×100 转成 % 显示。"
        >
          {/* v300+ P0+：rawAvg 是 sentiment_score 平均值（-1..1 标度），显示时 ×100 转 %，
              否则用户会把 0.32 误读成"0.32%"（实际是 32% 偏多强度）。阈值保持 -1..1 标度。 */}
          {mood} {rawAvg > 0 ? '+' : ''}{(rawAvg * 100).toFixed(0)}%
        </p>
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          {moodSub} · 看多 {buyPct}% (样本 {sample})
        </p>
      </div>

      <div className="glass rounded-lg border border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            窗口已分析
          </span>
          <div className="flex items-center gap-1">
            {period && (
              <span
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[8px] text-primary"
                title={`当前全局时段 · ${period.label} 内 AI 处理完成的新闻`}
              >
                {period.label}
              </span>
            )}
            <CheckCircle2 className="h-4 w-4 text-primary" aria-hidden="true" />
          </div>
        </div>
        <p className="mt-2 font-mono text-2xl font-bold tabular-nums text-primary">
          {analyzedCount.toLocaleString()}
        </p>
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          窗口内新闻 → AI 信号处理完成数
        </p>
      </div>
    </div>
  )
}
