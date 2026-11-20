'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, Gauge, CheckCircle2, Cpu } from 'lucide-react'
import { cn } from '@/lib/utils'
import { API_BASE } from '@/lib/api'
import { sharedJson } from '@/lib/shared-json'
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
  // 后端探针归类（_classify_probe_failure）：
  // ok / rate_limit / quota_exhausted / auth / network / timeout / api_error / circuit_open
  category?: string
  circuit_state?: string
  // true = 该通道与其它通道共用同一次探测结果（同账户同模型去重）
  shared_probe?: boolean
  // 实时调用链路熔断状态（与门卫/主分析同一组落盘熔断器），区别于探针轻量探测的 circuit_state。
  // state: open / half_open / closed / null(无实时熔断器)
  runtime_circuit?: { state?: string | null; breakers?: Record<string, string> }
}

// 探针失败类别的中文短标签：面板要能一眼看出「红」的原因，而不是只看到一个 429。
const PROBE_CATEGORY_LABEL: Record<string, string> = {
  ok: '可用',
  rate_limit: '限流',
  // v323: 套餐/余额用完 —— 探测也不会好，必须充值或换 key（不是瞬时限流）
  quota_exhausted: '额度耗尽',
  auth: '鉴权失败',
  network: '网络异常',
  timeout: '超时',
  api_error: '接口错误',
  circuit_open: '熔断中',
  unknown: '未知',
}

type ModelsPayload = {
  channels: ModelHealth[]
  recent_models: { model: string; count: number }[]
  current_model: string | null
  current_model_at: string | null
  pending_news: number
  analyzed_24h: number
  // engine_openrouter 来自引擎写入的 runtime/llm_health.json：
  //   enabled=false → 该通道当前是停用状态（key 失效/未启用），不是"正在故障"；
  //   stale=true    → 这是一份过期快照（引擎只在开仓前探测，间隔可能很长）。
  engine_openrouter: {
    openrouter_ok: boolean
    status_code: number | null
    error: string
    ts: number
    enabled?: boolean
    stale?: boolean
    age_sec?: number | null
  } | null
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
        // v310 P0 fix: 显式 no-store —— 本文件下面 /ai/health 那处早就带了,
        // 只有这处裸 fetch 漏了, 浏览器 HTTP cache 可能把「今日计数」缓存住,
        // 表现为切时段后数字不刷新。
        // v499: 三个组件同参数轮询同一接口 → 走 sharedJson 4s TTL 合并
        const data = await sharedJson<TodayStats>(url, 4000, { cache: 'no-store' })
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

  // v352: 同优先级的多条通道（同名不同 key）原来会连排四行 "MiniMax-..." + 同一个 18.6s，
  // 看起来像重复渲染。按 model 合并（name 是被截断的通道名，model 才是真正在跑的模型）：
  // MiniMax-M2.7 ×4 · 16.9s，部分可用时显示 2/4。
  const channelGroups = useMemo(() => {
    const map = new Map<
      string,
      { name: string; total: number; ok: number; worstLatency: number; code: number | null; note: string; rtOpen: boolean; rtHalf: boolean }
    >()
    for (const m of models?.channels ?? []) {
      const key = m.model || m.name
      const g =
        map.get(key) ??
        { name: key, total: 0, ok: 0, worstLatency: 0, code: null, note: m.note, rtOpen: false, rtHalf: false }
      g.total += 1
      const rt = m.runtime_circuit?.state
      if (rt === 'open') g.rtOpen = true
      else if (rt === 'half_open') g.rtHalf = true
      if (m.available) {
        g.ok += 1
        g.worstLatency = Math.max(g.worstLatency, m.latency_ms ?? 0)
      } else if (g.code == null) {
        g.code = m.status_code ?? null
      }
      map.set(key, g)
    }
    return [...map.values()].sort((a, b) => (b.ok / b.total) - (a.ok / a.total))
  }, [models])

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
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
            <div className="space-y-1">
              <p className="font-mono text-[11px] text-muted-foreground">探测中…</p>
              <p className="font-mono text-[11px] leading-relaxed text-muted-foreground/70">
                等 /api/health/models 返回；接口未就绪时保持空态，不显示假值 · 30s 自动重探
              </p>
            </div>
          ) : (
            channelGroups.slice(0, 4).map((g) => {
              const allOk = g.ok === g.total
              const latency = `${Math.round(g.worstLatency / 10) / 100}s`
              // 实时熔断优先于探针轻量结果：探针 3s 探测通过不代表实时调用没在熔断。
              const dotCls = g.rtOpen || (!allOk && g.ok === 0)
                ? 'bg-short'
                : g.rtHalf || (!allOk && g.ok > 0)
                  ? 'bg-yellow-500/80'
                  : 'bg-long'
              const textCls = g.rtOpen || (!allOk && g.ok === 0)
                ? 'text-short'
                : g.rtHalf || (!allOk && g.ok > 0)
                  ? 'text-yellow-600'
                  : 'text-long'
              const right = g.rtOpen
                ? '熔断'
                : g.rtHalf
                  ? '半开'
                  : allOk
                    ? latency
                    : g.ok > 0
                      ? `${g.ok}/${g.total}`
                      : `${g.code ?? 'ERR'}`
              const rtNote = g.rtOpen ? '实时熔断中' : g.rtHalf ? '实时半开探测' : ''
              return (
                <div
                  key={g.name}
                  className="flex items-center gap-1.5 font-mono text-[11px]"
                  title={[g.name, g.note, rtNote, allOk ? '探针全部可用' : `${g.ok}/${g.total} 探针可用`]
                    .filter(Boolean)
                    .join(' · ')}
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotCls}`}
                    title={rtNote || (allOk ? '可用' : `${g.ok}/${g.total} 可用`)}
                  />
                  <span className="max-w-[7.5rem] shrink-0 truncate text-foreground" title={g.name}>
                    {g.name}
                  </span>
                  {g.total > 1 ? (
                    <span className="shrink-0 text-muted-foreground/60" title={`${g.total} 个同名通道`}>
                      ×{g.total}
                    </span>
                  ) : null}
                  <span className={`ml-auto shrink-0 ${textCls}`}>
                    {right}
                  </span>
                </div>
              )
            })
          )}
        </div>
        {channelGroups.length > 4 ? (
          <a
            href="/connectivity"
            className="mt-1 block font-mono text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
            title={`共 ${models?.channels.length ?? 0} 个通道 / ${channelGroups.length} 组，${models?.channels.filter((c) => c.available).length ?? 0} 个可用`}
          >
            +{channelGroups.length - 4} 组通道 · 去联通页看 →
          </a>
        ) : null}
        <p className="mt-1 border-t border-border/50 pt-1 font-mono text-[11px] text-muted-foreground">
          当前分析:{' '}
          <span className="text-foreground">
            {models?.current_model ?? '—'}
          </span>
          {models?.current_model_at && (
            <span className="text-muted-foreground/70"> @ {models.current_model_at.slice(11, 16)}</span>
          )}
        </p>
        {models?.engine_openrouter && (() => {
          const eo = models.engine_openrouter
          // v320 P0: 后端版本不一致时（8100 上跑的是旧 api_server 模块，只回
          // openrouter_ok/ts，没有 enabled/stale），前端会把一份几小时前的
          // 快照当成"当前故障"长期挂红叉 → 这里按 ts 自行兜底判定"陈旧"。
          const eoAgeSec = eo.age_sec != null
            ? eo.age_sec
            : (typeof eo.ts === 'number' && eo.ts > 0 ? Date.now() / 1000 - eo.ts : null)
          const eoStale = eo.stale === true
            || (eo.stale == null && eoAgeSec != null && eoAgeSec > 30 * 60)
          // 三种状态要分清，否则会把"已停用"或"过期快照"当成正在故障：
          //   停用(enabled=false) → 灰色"— 已停用"（OpenRouter 默认停用，key 失效 401）
          //   陈旧(stale=true)     → 黄色"⚠ 快照过期(N 分钟前)"，不是实时状态
          //   实时                 → 绿✓/红✗
          if (eo.enabled === false) {
            return (
              <p className="font-mono text-[11px] text-muted-foreground">
                引擎探测 OpenRouter:{' '}
                <span className="text-muted-foreground">— 已停用</span>
                <span className="block truncate text-muted-foreground/70" title={eo.error}>
                  {eo.error || '该通道当前未启用'}
                </span>
              </p>
            )
          }
          if (eoStale) {
            const mins = Math.round((eoAgeSec ?? 0) / 60)
            return (
              <p className="font-mono text-[11px] text-muted-foreground">
                引擎探测 OpenRouter:{' '}
                <span className="text-short/90">⚠ 快照过期({mins} 分钟前)</span>
                <span className="block truncate text-muted-foreground/70" title={eo.error}>
                  {eo.error || '引擎仅在开仓前探测，非实时状态'}
                </span>
              </p>
            )
          }
          return (
            <p className="font-mono text-[11px] text-muted-foreground">
              引擎探测 OpenRouter:{' '}
              <span className={eo.openrouter_ok ? 'text-long' : 'text-short'}>
                {eo.openrouter_ok ? '✓ 可用' : `✗ ${eo.status_code ?? 'ERR'}`}
              </span>
              {!eo.openrouter_ok && eo.error && (
                <span className="block truncate text-short/70" title={eo.error}>
                  {eo.error}
                </span>
              )}
            </p>
          )
        })()}
        {models?.engine_model_pool && models.engine_model_pool.length > 0 && (
          <p className="font-mono text-[11px] text-muted-foreground" title={models.engine_model_pool.join(' | ')}>
            引擎池: <span className="text-foreground">{models.engine_model_pool.slice(0, 4).join(', ')}</span>
            {models.engine_model_pool.length > 4 && <span className="text-muted-foreground/70"> …</span>}
          </p>
        )}
        <p className="font-mono text-[11px] text-muted-foreground">
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
          'glass group rounded-lg border px-4 py-3 text-left transition-all duration-slow ease-out',
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
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[11px] text-primary"
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
        {/* v354: 0 是个"歧义数字" —— 可能是 LLM 全 HOLD、也可能是窗口真没新闻。
            原来只留一行 9px 调试字（[DB] live=…），读者看不出是哪种。这里直接给结论，
            调试字段全部收进 title，正文不再常显。 */}
        {signalCount === 0 ? (
          <p className="mt-1 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {analyzedCount > 0
              ? `窗口已分析 ${analyzedCount.toLocaleString()} 条新闻，LLM 全部 HOLD/FAIL —— 确实没有可下单信号，不是统计失败。`
              : '窗口内暂无可分析新闻（上游未产生新闻或后端未就绪），0 是空窗口径。'}
          </p>
        ) : null}
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground/60" title={`信号数 = ai_decisions 中窗口内的 BUY/SELL 决策行（一条新闻可拆出主品种+副品种多个决策）；新闻数 = 窗口内入库的新闻条数。两表口径不同，非统计错误。v300+ 默认仅统计最近 N 小时 BUY/SELL，LLM/数据源全挂时如实归零。

DB 原始计数 (v303+ 调试):
live_signal_count (近 ${today?.live_signal_window_hours ?? '?'}h BUY/SELL) = ${today?.live_signal_count ?? '?'}
signal_count (全天 BUY/SELL) = ${today?.signal_count ?? '?'}
news_count (窗口内新闻) = ${today?.news_count ?? '?'}
analyzed_count (窗口已分析) = ${today?.analyzed_count ?? '?'}

若 live=0 且 signal>0: ai_decisions.ts 列有 NULL 或窗口过滤逻辑异常.
若 live=0 且 signal=0: 窗口内确实无 BUY/SELL (LLM 全 HOLD 或没新决策).
若 signal=0 且 analyzed>0: LLM 全部 HOLD/FAIL, 这是真实情况.`}>
          信号按决策行计（多品种新闻拆多个信号）｜新闻按条计｜实时窗口 {today?.live_signal_window_hours ?? '–'}h
        </p>
        {onSignalsCardClick ? (
          <p className={cn(
            'mt-1 font-mono text-[11px]',
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
              className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[11px] text-primary"
              title={`当前全局时段 · ${period.label} 内的样本`}
            >
              {period.label}
            </span>
          )}
          <Gauge className={cn('h-4 w-4', tone)} aria-hidden="true" />
        </div>
        {/* v354: 样本 0 时不再显示「中性 +0%」—— 那是"没数据"被误读成"情绪中性" */}
        {sample === 0 ? (
          <>
            <p className="mt-2 font-mono text-2xl font-bold tabular-nums text-muted-foreground">样本 0</p>
            <p className="mt-0.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
              本时段没有已分析的 AI 决策，情绪不可测 —— 不显示 0% 这种假中性。
            </p>
          </>
        ) : (
          <>
            <p
              className={cn('mt-2 font-mono text-2xl font-bold tabular-nums', tone)}
              title="sentiment_score 平均强度（-100%..+100%）：+30% 偏多、-30% 偏空、±25% 内中性。底层是 ai_decisions.sentiment_score 字段（-1..+1 标度），这里 ×100 转成 % 显示。"
            >
              {mood} {rawAvg > 0 ? '+' : ''}{(rawAvg * 100).toFixed(0)}%
            </p>
            <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
              {moodSub} · 看多 {buyPct}% (样本 {sample})
            </p>
          </>
        )}
      </div>

      <div className="glass rounded-lg border border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            窗口已分析
          </span>
          <div className="flex items-center gap-1">
            {period && (
              <span
                className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[11px] text-primary"
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
