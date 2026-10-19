/**
 * data-channel-standards.ts — v170 P0+ 统一数据通道字段映射 + 渲染标准
 *
 * 设计原则（详见 docs/数据通道统一口径.md）：
 *  1. 所有数据源共用同一套字段名、格式化函数、徽章样式；
 *  2. 任何新增统计面板必须从这里 import，禁止重新实现；
 *  3. 底层数据时间一律 UTC ISO8601 存储，前端展示统一 fmtBeijing()；
 *  4. 自动对账仅告警，本模块不修复脏数据（脏数据由 id_reconcile 修复）。
 */

import { formatDual } from './tz-utils'

// ──────────────────────────────────────────────────────────────────────
// §1 时间格式化（统一 Asia/Shanghai UTC+8）
// ──────────────────────────────────────────────────────────────────────

/** ISO8601/epoch/SQL 时间串 → "YYYY-MM-DD HH:MM:SS +08"（空值返回 —）*/
export function fmtBeijing(input: string | number | null | undefined): string {
  if (input === null || input === undefined || input === '') return '—'
  let epoch: number
  if (typeof input === 'number') {
    epoch = input < 1e12 ? input : Math.floor(input / 1000)
  } else {
    const s = String(input).trim()
    if (!s) return '—'
    if (/^\d{10,}$/.test(s)) {
      epoch = parseInt(s, 10)
      if (epoch > 1e12) epoch = Math.floor(epoch / 1000)
    } else {
      // v302+ P0+【修复】后端 raw_news/timestamp 字符串代表"北京时间"，无 tz 那部分应
      // 补 +08:00 而不是 +Z（UTC）。旧逻辑补 Z 会把北京时间当 UTC，8h 漂移。
      const normalized = s.includes('T') ? s : s.replace(' ', 'T')
      const hasTz = /(Z|[+\-]\d{2}:?\d{2})$/.test(normalized)
      const dt = new Date(hasTz ? normalized : normalized + '+08:00')
      epoch = Math.floor(dt.getTime() / 1000)
    }
  }
  if (!Number.isFinite(epoch) || epoch <= 0) return '—'
  return formatDual(epoch).cn
}

/** 短时间（YYYY-MM-DD HH:MM:SS）— 列表/卡片密集展示用 */
export function fmtBeijingShort(input: string | number | null | undefined): string {
  const s = fmtBeijing(input)
  if (s === '—') return s
  const m = /^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})/.exec(s)
  return m ? `${m[1]} ${m[2]}` : s
}

/** 仅日期 YYYY-MM-DD */
export function fmtBeijingDate(input: string | number | null | undefined): string {
  const s = fmtBeijing(input)
  if (s === '—') return s
  return s.slice(0, 10)
}

// ──────────────────────────────────────────────────────────────────────
// §2 数据来源（4 维度：实时/模拟/真实/回测）
// ──────────────────────────────────────────────────────────────────────

export type DataSource = 'live' | 'simulated' | 'real' | 'backtest'

export const DATA_SOURCE_META: Record<DataSource, {
  label: string
  badge: string
  short: string
  color: string
  description: string
}> = {
  live:      { label: '实时信号', badge: '[实时]', short: 'L',
               color: 'border-buy/40 bg-buy/10 text-buy',
               description: 'ai_decisions + 未 settle 的 quick_sim_trades' },
  simulated: { label: '模拟交易', badge: '[模拟]', short: 'S',
               color: 'border-hold/40 bg-hold/10 text-hold',
               description: 'quick_sim_trades 已 settle 的部分' },
  real:      { label: '真实持仓', badge: '[真实]', short: 'R',
               color: 'border-primary/40 bg-primary/10 text-primary',
               description: 'paper_trading.positions + trade_reflections_v2' },
  backtest:  { label: '回测',     badge: '[回测]', short: 'B',
               color: 'border-short/40 bg-short/10 text-short',
               description: 'backtest / experience_rules' },
}

/** 推导数据来源（基于表名 + settled 标记） */
export function inferDataSource(opts: {
  table?: string
  settled?: boolean | null
  source?: string | null
}): DataSource {
  if (opts.source) {
    const s = String(opts.source).toLowerCase()
    if (s.includes('paper')) return 'real'
    // v275+: 当 source 字符串本身就含 'settled'(如 quick_sim_settled)时,
    // 即使没传 opts.settled,也应识别为已结算模拟;否则算 live。
    if (s.includes('quick_sim') || s.includes('simulation')) {
      return opts.settled === true || s.includes('settled') ? 'simulated' : 'live'
    }
    if (s.includes('backtest')) return 'backtest'
  }
  if (opts.table) {
    if (opts.table === 'paper_trading') return 'real'
    if (opts.table === 'quick_sim_trades') return opts.settled ? 'simulated' : 'live'
    if (opts.table === 'trade_reflections_v2') return 'real'
    if (opts.table === 'ai_decisions') return 'live'
    if (opts.table === 'backtest_results' || opts.table === 'experience_rules') return 'backtest'
  }
  return 'live'
}

/** [模拟]/[实时]/[真实]/[回测] 徽章 */
export function DataSourceBadge({ source }: { source: DataSource | null | undefined }) {
  if (!source) return null
  const m = DATA_SOURCE_META[source]
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold ${m.color}`}
      title={m.description}
    >
      {m.badge}
    </span>
  )
}

// ──────────────────────────────────────────────────────────────────────
// §3 时间范围选择器（4 标准：今日/近7天/近30天/自定义）
// ──────────────────────────────────────────────────────────────────────

export type PeriodKey = 'today' | '7d' | '30d' | 'custom'

export const PERIOD_META: Record<PeriodKey, { label: string; sub: string; days: number | 'custom' }> = {
  today:  { label: '今日',     sub: '00:00-现在', days: 1 },
  '7d':   { label: '近 7 天', sub: '7 日窗口',   days: 7 },
  '30d':  { label: '近 30 天', sub: '30 日窗口',  days: 30 },
  custom: { label: '自定义',   sub: '起止区间',   days: 'custom' },
}

/** 把 period 转换为 UTC ISO 起止时间，传给后端 API */
export function periodToUTC(opts: {
  period: PeriodKey
  now?: Date
  customFrom?: string
  customTo?: string
}): { from: string; to: string } {
  const now = opts.now ?? new Date()
  const toIso = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, 'Z')
  const BJT_OFFSET_MS = 8 * 3600 * 1000
  const toBjt = (d: Date) => new Date(d.getTime() + BJT_OFFSET_MS)
  const fromBjt = (d: Date) => new Date(d.getTime() - BJT_OFFSET_MS)

  if (opts.period === 'today') {
    const bjt = toBjt(now)
    const dayStartBjt = new Date(Date.UTC(bjt.getUTCFullYear(), bjt.getUTCMonth(), bjt.getUTCDate()))
    return { from: toIso(fromBjt(dayStartBjt)), to: toIso(now) }
  }
  if (opts.period === '7d' || opts.period === '30d') {
    const days = opts.period === '7d' ? 7 : 30
    return { from: toIso(new Date(now.getTime() - days * 86400 * 1000)), to: toIso(now) }
  }
  const fromDate = opts.customFrom
    ? new Date(`${opts.customFrom}T00:00:00+08:00`)
    : new Date(now.getTime() - 7 * 86400 * 1000)
  const toDate = opts.customTo
    ? new Date(`${opts.customTo}T23:59:59+08:00`)
    : now
  return { from: toIso(fromDate), to: toIso(toDate) }
}

// ──────────────────────────────────────────────────────────────────────
// §4 判定状态（3 态：已判定/待判定/失败）+ 卡住原因
// ──────────────────────────────────────────────────────────────────────

export type VerdictStatus = 'decided' | 'pending' | 'failed'

export const PENDING_REASON_META: Record<string, { label: string; hint: string; color: string }> = {
  no_price_data:        { label: '无价格数据', hint: '时间窗内 kline 缺失',     color: 'border-hold/40 bg-hold/10 text-hold' },
  extreme_unsettled:    { label: '极值缺失',   hint: 'max_price/min_price 未填', color: 'border-short/40 bg-short/10 text-short' },
  reflection_timeout:   { label: '超时',       hint: 'reflection worker >30s',  color: 'border-short/40 bg-short/10 text-short' },
  data_quarantine:      { label: '已隔离',     hint: '被 id_reconcile 隔离',     color: 'border-short/40 bg-short/10 text-short' },
  model_retry_exceeded: { label: 'LLM 失败',   hint: '重试 3 次仍失败',         color: 'border-short/40 bg-short/10 text-short' },
  unknown:              { label: '未知原因',   hint: '需人工排查',              color: 'border-short/40 bg-short/10 text-short' },
}

export function inferVerdictStatus(
  verdict: string | null | undefined,
  _pendingReason?: string | null,
): VerdictStatus {
  const v = (verdict || '').toUpperCase().trim()
  if (v === 'WIN' || v === 'LOSS' || v === 'FLAT') return 'decided'
  if (v === 'FAILED' || v === 'HOLD') return 'failed'
  return 'pending'
}

export function VerdictStatusBadge({
  status, reason,
}: { status: VerdictStatus; reason?: string | null }) {
  const meta = {
    decided: { label: '已判定',    color: 'border-buy/40 bg-buy/10 text-buy',    icon: 'OK' },
    pending: { label: '待判定',    color: 'border-hold/40 bg-hold/10 text-hold', icon: '⏳' },
    failed:  { label: '判定失败', color: 'border-short/40 bg-short/10 text-short', icon: 'X' },
  }[status]
  const reasonMeta = reason ? PENDING_REASON_META[reason] || PENDING_REASON_META.unknown : null
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold ${meta.color}`}
      title={reasonMeta ? `${meta.label} · ${reasonMeta.label}：${reasonMeta.hint}` : meta.label}
    >
      <span>{meta.icon}</span>
      <span>{meta.label}</span>
      {reasonMeta && status !== 'decided' && (
        <span className="ml-1 font-mono text-[9px] opacity-80">· {reasonMeta.label}</span>
      )}
    </span>
  )
}

// ──────────────────────────────────────────────────────────────────────
// §5 分类标签（market_category + event_type_code 双标）
// ──────────────────────────────────────────────────────────────────────

export const MARKET_CATEGORY_META: Record<string, { label: string; color: string }> = {
  CRYPTO:    { label: '加密货币',         color: 'border-buy/40 bg-buy/10 text-buy' },
  GOLD:      { label: '黄金',             color: 'border-hold/40 bg-hold/10 text-hold' },
  OIL:       { label: '原油',             color: 'border-short/40 bg-short/10 text-short' },
  STOCK:     { label: '股票',             color: 'border-primary/40 bg-primary/10 text-primary' },
  MACRO:     { label: '宏观',             color: 'border-muted/40 bg-muted/10 text-muted-foreground' },
  COMMODITY: { label: '大宗',             color: 'border-primary/40 bg-primary/10 text-primary' },
  OTHER:     { label: '已隔离(低价值)', color: 'border-short/40 bg-short/10 text-short' },
}

export function CategoryChip({
  marketCategory, eventTypeCode,
}: { marketCategory?: string | null; eventTypeCode?: string | null }) {
  const mc = (marketCategory || '').toUpperCase()
  const m = MARKET_CATEGORY_META[mc] || MARKET_CATEGORY_META.OTHER
  return (
    <span className="inline-flex items-center gap-1 font-mono text-[10px]">
      <span className={`rounded border px-1.5 py-0.5 font-bold ${m.color}`}>{m.label}</span>
      {eventTypeCode && (
        <span className="rounded border border-border bg-secondary/40 px-1.5 py-0.5 text-muted-foreground">
          {eventTypeCode}
        </span>
      )}
    </span>
  )
}

// ──────────────────────────────────────────────────────────────────────
// §6 盈亏 / 价格格式化（统一精度）
// ──────────────────────────────────────────────────────────────────────

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${(v * 100).toFixed(digits)}%`
}

export function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return v.toFixed(2)
  if (Math.abs(v) >= 1)    return v.toFixed(4)
  return v.toFixed(6)
}

// ──────────────────────────────────────────────────────────────────────
// §7 动作徽章（BUY/SELL/HOLD/FAILED）
// ──────────────────────────────────────────────────────────────────────

export function actionBadge(action: string | null | undefined): { label: string; color: string } {
  const a = (action || '').toUpperCase()
  if (a === 'BUY' || a === 'LONG')   return { label: '买入', color: 'border-buy/40 bg-buy/10 text-buy' }
  if (a === 'SELL' || a === 'SHORT') return { label: '卖出', color: 'border-short/40 bg-short/10 text-short' }
  if (a === 'FAILED')                return { label: '失败', color: 'border-short/40 bg-short/10 text-short' }
  return { label: '观望', color: 'border-hold/40 bg-hold/10 text-hold' }
}

export function ActionBadge({ action }: { action: string | null | undefined }) {
  const m = actionBadge(action)
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold ${m.color}`}>
      {m.label}
    </span>
  )
}

// ──────────────────────────────────────────────────────────────────────
// §8 实时通道：统一轮询 hook + SSE 主动推送（v170.4 P0+）
// ──────────────────────────────────────────────────────────────────────
//
// 设计要点：
//  - 所有"准实时"数据面板（反思页、StatCards、模拟盘面板）一律走 useChannelPoll，
//    严禁各自裸写 setInterval；这样 backend 缓存 TTL（_QUICK_SIM_CACHE）
//    与前端轮询间隔对齐，不会再产生"已结算 59 个但反思系统还卡在 50"的延迟感。
//  - 后端 quick_sim_watcher() 每次结算后 SSE 广播 { type: "quick_sim_update" }，
//    useChannelPoll 订阅同一连接 → 收到事件立即触发 refreshFn（不等轮询窗口）。
//  - onChannelEvent() 是轻量 publish/subscribe：其它面板（ReviewHistoryPanel /
//    FailureStatsPanel / ReviewPanel）也可以订阅"有结算就打"等事件，按需刷新。
//
// 通道默认间隔：反思 / 模拟盘 = 5s（与 backend 2s 缓存对齐），其它面板 = 10s。

import { useCallback, useEffect, useRef } from 'react'
import { API_BASE } from './api'

/** SSE 事件订阅/广播：所有 useChannelPoll 实例共享同一个 EventSource 连接 */
let _sseSource: EventSource | null = null
let _sseSubscribers = 0
let _sseReconnectTimer: ReturnType<typeof setTimeout> | null = null
type ChannelEventHandler = (data: unknown) => void
const _channelHandlers = new Map<string, Set<ChannelEventHandler>>()

/** v302+ P0+：清掉已 CLOSED 的旧 _sseSource 引用,避免下次 ensureSSE 拿到僵尸。
 *  这是 SSE 单例的"显式 GC 入口"。 */
function clearStaleSSE() {
  if (_sseSource && _sseSource.readyState === EventSource.CLOSED) {
    _sseSource = null
  }
}

function ensureSSE(): EventSource {
  clearStaleSSE()
  if (_sseSource && _sseSource.readyState !== EventSource.CLOSED) return _sseSource
  const url = `${API_BASE}/events/stream`
  const es = new EventSource(url)
  _sseSource = es
  es.onmessage = (ev) => {
    try {
      const parsed = JSON.parse(ev.data)
      const type = parsed?.type ?? 'message'
      const set = _channelHandlers.get(type)
      if (set && set.size > 0) {
        for (const fn of set) {
          try { fn(parsed) } catch { /* 单个回调异常不影响其它 */ }
        }
      }
    } catch {
      /* keepalive / 非 JSON 帧忽略 */
    }
  }
  // v302+ P0+: 监听 error,CLOSED 时清掉引用,否则下次 ensureSSE 会拿到僵尸
  // EventSource(已 CLOSED) 永远 readyState===2,不会变回 OPEN,后续 addEventListener 也无效
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      _sseSource = null
      if (typeof console !== 'undefined') {
        console.warn('[sse] connection closed, will reconnect on next subscribe')
      }
    }
  }
  return es
}

function subscribeSSE(): void {
  _sseSubscribers += 1
  if (_sseSubscribers === 1) ensureSSE()
}
function unsubscribeSSE(): void {
  _sseSubscribers = Math.max(0, _sseSubscribers - 1)
}

/** 全局订阅通道事件（同 type 多 handler）；返回反订阅函数 */
export function onChannelEvent(type: string, handler: ChannelEventHandler): () => void {
  subscribeSSE()
  let set = _channelHandlers.get(type)
  if (!set) {
    set = new Set()
    _channelHandlers.set(type, set)
  }
  set.add(handler)
  return () => {
    set?.delete(handler)
    if (set && set.size === 0) _channelHandlers.delete(type)
    unsubscribeSSE()
  }
}

/** 手动广播本地事件（不经过 SSE，便于同页跨组件联动） */
export function emitChannelEvent(type: string, data: unknown): void {
  const set = _channelHandlers.get(type)
  if (!set) return
  for (const fn of set) {
    try { fn(data) } catch { /* 忽略 */ }
  }
}

/** 通道事件类型常量（统一字面量，避免拼写错） */
export const CHANNEL_EVENT = {
  QUICK_SIM_UPDATE: 'quick_sim_update',  // 后端 quick_sim_watcher 推送：建仓/结算
  SETTLEMENT: 'settlement',            // 同上别名（兼容）
  REFRESH_REVIEW: 'refresh:review',    // 本地广播：刷新反思系统全部面板
  REFRESH_TODAY: 'refresh:today',      // 本地广播：刷新今日流速
} as const

export type UseChannelPollOpts = {
  /** 拉取函数；返回 Promise；抛出不会中断轮询 */
  refresh: () => Promise<void> | void
  /** 轮询间隔（毫秒）。默认 5000；反思/模拟盘建议 5000；其它面板 10000 */
  intervalMs?: number
  /** 是否启用轮询；默认 true；false 表示只等 SSE 触发 */
  enabled?: boolean
  /** 订阅这些 channel 事件，每次触发立即执行 refresh（不等轮询窗口） */
  triggerOn?: string[]
}

/**
 * useChannelPoll — v170.4 P0+ 统一实时通道 hook
 *
 * 行为：
 *  - 首次挂载立即执行一次 refresh
 *  - 每 intervalMs 毫秒轮询
 *  - 订阅 triggerOn 中的 channel 事件，收到后立即 refresh
 *  - 卸载时清理 timer 与 SSE 订阅
 *
 * 用法：
 *   useChannelPoll({
 *     refresh: loadSnapshot,
 *     intervalMs: 5000,
 *     triggerOn: [CHANNEL_EVENT.QUICK_SIM_UPDATE, CHANNEL_EVENT.REFRESH_REVIEW],
 *   })
 */
export function useChannelPoll(opts: UseChannelPollOpts): void {
  const { refresh, intervalMs = 5000, enabled = true, triggerOn = [] } = opts
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh

  const wrappedRefresh = useCallback(() => {
    void Promise.resolve(refreshRef.current()).catch(() => {
      /* 单次失败不打断轮询 */
    })
  }, [])

  useEffect(() => {
    if (!enabled) return
    wrappedRefresh()
    const id = window.setInterval(wrappedRefresh, Math.max(1000, intervalMs))
    const unsubs: Array<() => void> = []
    for (const t of triggerOn) {
      unsubs.push(onChannelEvent(t, wrappedRefresh))
    }
    return () => {
      window.clearInterval(id)
      for (const u of unsubs) u()
    }
  }, [enabled, intervalMs, wrappedRefresh, triggerOn.join('|')])
}

/**
 * §8.1 统一口径对照表（解决 "26 今日信号 vs 59 累计已结算" 疑惑）
 *
 * 三个口径并列展示：
 *  - 今日新建（signals today）  = ai_decisions.created_at 今天 + BUY/SELL
 *  - 今日已结算（settled today）= quick_sim_trades.exit_ts 今天 + settled=1
 *  - 累计已结算（all-time settled）= quick_sim_trades.settled=1 全部
 */
export function reconcileChannelCounters(input: {
  todaySignals?: number
  todaySettled?: number
  allTimeSettled?: number
}): {
  text: string
  tone: 'muted' | 'primary' | 'long' | 'short'
} {
  const todayS = Math.max(0, Number(input.todaySignals ?? 0))
  const todaySet = Math.max(0, Number(input.todaySettled ?? 0))
  const allSet = Math.max(0, Number(input.allTimeSettled ?? 0))
  if (todayS === 0 && todaySet === 0 && allSet === 0) {
    return { text: '无数据', tone: 'muted' }
  }
  if (todayS > 0 && todaySet === 0) {
    return { text: `今日 ${todayS} 信号 · 待首笔结算`, tone: 'primary' }
  }
  if (todaySet > 0 && todayS > 0 && todaySet >= todayS) {
    return { text: `今日 ${todaySet}/${todayS} 已结 · 累计 ${allSet}`, tone: 'long' }
  }
  return { text: `今日 ${todaySet} 已结 · 累计 ${allSet}`, tone: 'long' }
}

// ──────────────────────────────────────────────────────────────────────
// §9 统一时段选择器 PeriodSelector（v180 P0+ 新增）
// ──────────────────────────────────────────────────────────────────────
//
// 设计要点（统一口径文档 §2）：
//   1. 固定 4 个标准时段：今日 / 近 7 天 / 近 30 天 / 自定义 — 任何复盘面板必须复用，
//      禁止自行扩展（如「近 3 天」「近 24 小时」等相近口径只作为内部 hours 值使用）；
//   2. 「今日」按 Asia/Shanghai 当日 0:00 计算，与 UTC 存储自动换算；
//   3. 「自定义」输出 fromTs/toTs（UTC 秒），后端优先认这两个参数；
//   4. onChange 回调把时段键值（today/7d/30d/custom）+ 解析后的 fromTs/toTs + hours
//      一次性交付，避免各组件重复解析。

// 注：PeriodKey 与 §3 的同名类型同型（'today' | '7d' | '30d' | 'custom'），
// §9 复用 §3 的类型定义，避免重复声明导致 TS2300。

export interface PeriodRange {
  key: PeriodKey
  label: string
  /** 回看窗口小时数（与后端 hours 参数对齐）；today 强制约定 24 但 toTs 用 23:59:59 */
  hours: number
  /** UTC 秒（含端点）；仅 key=custom 或 today 时严格设置，7d/30d 由后端按 hours 算 */
  fromTs: number | null
  toTs: number | null
}

export const PERIOD_PRESETS: ReadonlyArray<{ key: PeriodKey; label: string; hours: number }> = [
  { key: 'today', label: '今日', hours: 24 },
  { key: '7d', label: '近 7 天', hours: 24 * 7 },
  { key: '30d', label: '近 30 天', hours: 24 * 30 },
  { key: 'custom', label: '自定义', hours: 0 },
]

/** 北京时区当日 00:00:00 对应的 UTC 秒（含端点起点） */
export function beijingTodayStartTs(now: Date = new Date()): number {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  })
  const [y, m, d] = fmt.format(now).split('-').map(Number)
  const utcStart = Date.UTC(y, m - 1, d, 0, 0, 0) - 8 * 3600 * 1000
  return Math.floor(utcStart / 1000)
}

/** 北京时区次日 00:00:00（即「今日」的 toTs 端点） */
export function beijingTomorrowStartTs(now: Date = new Date()): number {
  return beijingTodayStartTs(now) + 24 * 3600
}

/**
 * 把时段键 + 自定义日期解析成 PeriodRange（前端直接传给后端）。
 * - key=today: 今日 00:00 ~ 明日 00:00（北京时间）
 * - key=7d/30d: hours > 0，后端按 hours 计算；fromTs/toTs 留 null
 * - key=custom: 要求 fromDate/toDate（YYYY-MM-DD，北京时区）；返回对应 00:00 ~ 24:00
 */
export function resolvePeriod(
  key: PeriodKey,
  opts?: { fromDate?: string; toDate?: string; now?: Date },
): PeriodRange {
  const now = opts?.now ?? new Date()
  if (key === 'today') {
    const fromTs = beijingTodayStartTs(now)
    const toTs = beijingTomorrowStartTs(now)
    return { key, label: '今日', hours: 24, fromTs, toTs }
  }
  if (key === '7d' || key === '30d') {
    const hours = key === '7d' ? 24 * 7 : 24 * 30
    return { key, label: key === '7d' ? '近 7 天' : '近 30 天', hours, fromTs: null, toTs: null }
  }
  // custom
  if (!opts?.fromDate || !opts?.toDate) {
    return { key, label: '自定义', hours: 0, fromTs: null, toTs: null }
  }
  const from = new Date(`${opts.fromDate}T00:00:00+08:00`)
  const to = new Date(`${opts.toDate}T00:00:00+08:00`)
  const fromTs = Math.floor(from.getTime() / 1000)
  const toTs = Math.floor(to.getTime() / 1000) + 24 * 3600
  return {
    key,
    label: `${opts.fromDate} ~ ${opts.toDate}`,
    hours: 0,
    fromTs: Math.min(fromTs, toTs),
    toTs: Math.max(fromTs, toTs),
  }
}

/**
 * 把 PeriodRange 渲染成后端可消费的查询参数（直接拼 URLSearchParams）。
 * v300.5 P0+：改为 fromTs-driven —— 任何 period 只要 fromTs/toTs 齐了都走精确时间窗。
 * - 修复「今日」label 骗人 bug：'today' 现在传 beijingTodayStartTs/beijingTomorrowStartTs,
 *   而不是 hours=24 滚动窗(过去 24h)
 * - 7d/30d 仍走 hours(它们在 resolvePeriod 里就故意不填 fromTs/toTs)
 * - custom 行为不变
 * - 未来新加 period: 只要在 resolvePeriod 里 set 了 fromTs/toTs,这里自动正确路由
 */
export function periodToQuery(p: PeriodRange): URLSearchParams {
  const sp = new URLSearchParams()
  if (p.fromTs !== null && p.toTs !== null) {
    sp.set('from_ts', String(p.fromTs))
    sp.set('to_ts', String(p.toTs))
  } else {
    sp.set('hours', String(p.hours))
  }
  return sp
}

/**
 * PeriodSelector — 标准时段选择器组件（统一口径文档 §8 面板复用清单）。
 * 用法：
 *   <PeriodSelector
 *     value={period} onChange={(p) => setPeriod(p)}
 *     customDateRange={{ fromDate, toDate }}
 *     onCustomChange={(d) => setCustom(d)}
 *   />
 */
export function PeriodSelector(props: {
  value: PeriodKey
  onChange: (next: PeriodKey) => void
  customDateRange: { fromDate: string; toDate: string }
  onCustomChange: (next: { fromDate: string; toDate: string }) => void
  size?: 'sm' | 'md'
}) {
  const { value, onChange, customDateRange, onCustomChange, size = 'md' } = props
  const padding = size === 'sm' ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-1 text-xs'
  return (
    <div className="inline-flex flex-wrap items-center gap-1 rounded-md border border-border bg-card p-1">
      {PERIOD_PRESETS.map((p) => {
        const active = value === p.key
        return (
          <button
            key={p.key}
            type="button"
            onClick={() => onChange(p.key)}
            className={`rounded font-mono transition-all ${padding} ${
              active
                ? 'bg-primary/15 text-primary ring-1 ring-primary/40'
                : 'text-muted-foreground hover:bg-secondary/60 hover:text-foreground'
            }`}
          >
            {p.label}
          </button>
        )
      })}
      {value === 'custom' && (
        <div className="ml-1 flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
          <input
            type="date"
            value={customDateRange.fromDate}
            onChange={(e) => onCustomChange({ ...customDateRange, fromDate: e.target.value })}
            className={`rounded border border-border bg-background ${padding}`}
          />
          <span>~</span>
          <input
            type="date"
            value={customDateRange.toDate}
            onChange={(e) => onCustomChange({ ...customDateRange, toDate: e.target.value })}
            className={`rounded border border-border bg-background ${padding}`}
          />
          <span className="rounded border border-primary/30 bg-primary/10 px-1 text-primary">
            北京时区
          </span>
        </div>
      )}
    </div>
  )
}