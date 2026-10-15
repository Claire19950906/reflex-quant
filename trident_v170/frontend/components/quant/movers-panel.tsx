'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useToast } from '@/components/ui/toast'

// v220：module-scope 常量（SnapshotIndex 子组件也要用）
const SNAPSHOT_LS_KEY = 'trident:attribution:snapshots'
const SNAPSHOT_PREFIX = 'trident:attribution:snapshot:'

// v302+ P0: 把 TF_TO_* 三张表从函数体内提到 module scope — 之前放在
// MoversPanel 函数体内意味着每次 render 都会重新分配 Record 对象,
// 引发 useEffect 依赖判断不稳定(reference equality) + 浪费 GC。
// 30 天事件存档（用户 2026-10-03：「保存归因页所有生成的数据在本地作为知识图谱参考」）
const TF_TO_BAR_LIMIT: Readonly<Record<string, number>> = Object.freeze({
  '3m': 144, '5m': 144, '10m': 720, '1h': 180, '4h': 180, '30d': 180,
})
const TF_TO_INTERVAL: Readonly<Record<string, string>> = Object.freeze({
  '30d': '4h',  // 30d 直接走 4h K 线
})
const TF_TO_HOURS: Readonly<Record<string, number>> = Object.freeze({
  '3m': 7, '5m': 12, '10m': 24, '1h': 168, '4h': 720, '30d': 720,
})

import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'
import { ExternalLink } from 'lucide-react'
import { API_BASE } from '@/lib/api'
import { useIsDark } from '@/hooks/use-is-dark'
import { pivotPoint, pivotPeriodForTimeframe } from '@/lib/technical-indicators'
import GateReflectionList from '@/components/quant/gate-reflection-list'
import { AssetLeaderboard } from '@/components/quant/asset-leaderboard'
import { AssetHeatmap } from '@/components/quant/asset-heatmap'
import { EventHeatmap } from '@/components/quant/event-heatmap'
import { AnomalyTypePanel } from '@/components/quant/anomaly-type-panel'
import { AbnormalDetailPanel } from '@/components/quant/abnormal-detail-panel'

type MoverEvent = {
  ts: number
  action: 'BUY' | 'SELL' | 'HOLD'
  score: number
  content: string
  source: string
  // v20251014: 6 维综合 signal_weight (来自后端 signal_weight_evaluator)
  //   前端用于 K 线 marker 颜色饱和度: strong/medium/weak/ignored + cold_signal
  //   字段为可空 — 旧决策(无 signal_weight 字段) 仍能渲染
  signal_weight?: number | null
  weight_tier?: 'strong' | 'medium' | 'weak' | 'ignored' | null
  weight_explain?: string | null
  cold_signal?: boolean
}

// 需求4：3 分钟异动反推点（由后端 abnormal_detector 检测）
// 需求2：异动 AI 归因（attribution 由后端后台归因线程写入，可能稍后到位）
type AttributionCandidate = {
  cause: string
  source?: string
  confidence?: string
  speculative?: boolean
}
type Attribution = {
  summary?: string
  candidates?: AttributionCandidate[]
  error?: string
}
type AbnormalPoint = {
  ts: number
  pct_change: number | null
  vol_ratio: number | null
  trigger_type: string // 'pct' | 'volume' | 'both' | 'swing' (v221 新增 swing)
  attribution?: Attribution
}

// 需求：星标事件 AI 归因 —— 行情主要驱动事件（后端 attribute_market_drivers 返回）
type MarketDriver = {
  ts: number
  content: string
  source: string
  action: string
  score: number
  reason: string
  importance: string // 'high' | 'medium'
}
type MarketDriverResult = {
  summary?: string
  drivers?: MarketDriver[]
  error?: string
}

type MoverItem = {
  asset: string
  price: number | null
  change24h: number | null
  source: string
  source_count: number | null
  events: MoverEvent[]
  abnormals?: AbnormalPoint[]
  drivers?: MarketDriverResult
}

type HistoryBar = { time: number; open: number; high: number; low: number; close: number; volume?: number }
type HistoryPoint = { time: number; close: number }
type HistoryResp = { points: HistoryPoint[]; bars: HistoryBar[]; count: number; missing: string }

// 资产代号 → 中文名,集中定义在 lib/asset-meta.ts (v302+ P0 重构,消除 4 处重复)
import { ASSET_META as ASSET_META_LIB } from '@/lib/asset-meta'
const ASSET_META: Readonly<Record<string, string>> = ASSET_META_LIB

const ACTION_META: Record<string, { label: string; cls: string }> = {
  BUY: { label: '看涨', cls: 'text-long bg-long/10 border-long/30' },
  SELL: { label: '看跌', cls: 'text-short bg-short/10 border-short/30' },
  HOLD: { label: '中性', cls: 'text-muted-foreground bg-background border-border' },
}

const CHART_THEME = {
  light: {
    background: '#ffffff',
    text: '#5b6472',
    grid: '#eceff3',
    border: '#dfe3e9',
    up: '#00b061',
    down: '#ef4444',
    flat: '#64748b',
    line: '#3b82f6',
  },
  dark: {
    background: '#16171b',
    text: '#9aa3af',
    grid: '#26282e',
    border: '#33363d',
    up: '#2ebd85',
    down: '#f6465d',
    flat: '#9aa3af',
    line: '#60a5fa',
  },
} as const

const SH_OFFSET_MS = 8 * 3600 * 1000

// v280 — Pivot Point 技术支撑阻力位专用色（与 live-kline-chart 一致）
//   R1/R2/R3 阻力位由浅到深绿；S1/S2/S3 支撑位由浅到深红；Pivot 中线橙色实线
const COLOR_PIVOT_P = '#fb923c'    // orange-400 中心 Pivot 实线
const COLOR_PIVOT_R1 = '#86efac'   // green-300  R1 阻力位 1
const COLOR_PIVOT_R2 = '#4ade80'   // green-400  R2 阻力位 2
const COLOR_PIVOT_R3 = '#22c55e'   // green-500  R3 阻力位 3
const COLOR_PIVOT_S1 = '#fca5a5'   // red-300    S1 支撑位 1
const COLOR_PIVOT_S2 = '#f87171'   // red-400    S2 支撑位 2
const COLOR_PIVOT_S3 = '#ef4444'   // red-500    S3 支撑位 3

// v280 — 实时订单簿深度热力图（买卖盘）
const COLOR_BOOK_BID = 'rgba(34, 197, 94, 0.55)'  // green-500 半透明  买盘
const COLOR_BOOK_ASK = 'rgba(239, 68, 68, 0.55)'  // red-500   半透明  卖盘

// ── v278 累计合并工具函数 ────────────────────────────────────────
// 用户 2026-10-07 反馈：「现在又正常了，但是只识别了近期，没有全部识别出，
// 并且要保存自己现实过的所有数据呈现保留在归因页的 k 线上」
//
// 核心问题：每次 fetch 后 setMarkersData 整体替换，导致：
//   1. 切到 10m（5天）后再切回 30d，30d 的标记数据已丢失
//   2. 同一资产不同周期看到的事件不累计
//   3. auto-save 是覆盖式，存档只有最近一次的快照
//
// 修复：每次写入 state / localStorage 时按 key 去重合并，保留最广范围。
// ──────────────────────────────────────────────────────────────────

/** 按 key 函数合并两个数组并去重 — 后传入的优先覆盖同 key 的旧值 */
function mergeUnique<T>(a: T[], b: T[], keyFn: (x: T) => string): T[] {
  const m = new Map<string, T>()
  for (const x of a ?? []) {
    try {
      const k = keyFn(x)
      if (k) m.set(k, x)
    } catch { /* skip invalid entry */ }
  }
  for (const x of b ?? []) {
    try {
      const k = keyFn(x)
      if (k) m.set(k, x)  // b 优先覆盖同 key 的
    } catch { /* skip invalid entry */ }
  }
  return [...m.values()]
}

/** 按 time 去重 + 排序合并 K 线 bars — 同 time 的以最新 OHLC 为准 */
function mergeBars(a: any[], b: any[]): any[] {
  const m = new Map<number, any>()
  for (const x of a ?? []) {
    if (typeof x?.time === 'number') m.set(x.time, x)
  }
  for (const x of b ?? []) {
    if (typeof x?.time === 'number') m.set(x.time, x)  // 新 bars 优先
  }
  return [...m.values()].sort((x, y) => x.time - y.time)
}

/** 计算 markersData 的覆盖时间范围（最早/最晚事件时间） */
function markersCoverage(m: {
  calendar?: any[]; impact?: any[]; signals?: any[]; abnormals?: any[]; swingPoints?: any[];
}): { earliest: number; latest: number; count: number } {
  let earliest = 0
  let latest = 0
  let count = 0
  const bump = (ts: any) => {
    const t = Number(ts)
    if (!t || !isFinite(t)) return
    if (!earliest || t < earliest) earliest = t
    if (t > latest) latest = t
    count++
  }
  for (const c of m.calendar ?? []) bump(c?.ts_bj)
  for (const i of m.impact ?? []) bump(i?.event_time)
  for (const s of m.signals ?? []) bump(s?.news_time ?? s?.created_at)
  for (const a of m.abnormals ?? []) bump(a?.ts)
  for (const sw of m.swingPoints ?? []) bump(sw?.ts ?? sw?.time)
  return { earliest, latest, count }
}

function shTickFormatter(time: Time, tickMarkType: TickMarkType): string {
  if (typeof time !== 'number') return String(time)
  const d = new Date(time * 1000 + SH_OFFSET_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  if (tickMarkType === TickMarkType.Time || tickMarkType === TickMarkType.TimeWithSeconds) return hm
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${hm}`
}

function fmtTs(ts: number): string {
  const d = new Date(ts * 1000 + SH_OFFSET_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
}

// 新闻按自然日分组（今天/昨天/MM-DD），避免几十条堆在一起看不清
function dayLabel(ts: number): string {
  const d = new Date(ts * 1000 + SH_OFFSET_MS)
  const now = new Date(Date.now() + SH_OFFSET_MS)
  const y = d.getUTCFullYear(); const m = d.getUTCMonth() + 1; const dd = d.getUTCDate()
  const ty = now.getUTCFullYear(); const tm = now.getUTCMonth() + 1; const td = now.getUTCDate()
  if (y === ty && m === tm && dd === td) return '今天'
  const yst = new Date(Date.now() + SH_OFFSET_MS - 86400000)
  if (y === yst.getUTCFullYear() && m === yst.getUTCMonth() + 1 && dd === yst.getUTCDate()) return '昨天'
  return `${String(m).padStart(2, '0')}-${String(dd).padStart(2, '0')}`
}

// 需求10：新闻/事件时间向前对齐最近的开市 K 线（取 ≤ time 的最近一根，不用“绝对最近”）
function alignTime(time: number, bars: HistoryBar[]): number {
  if (!bars.length) return time
  let anchor = bars[0].time
  for (const bar of bars) {
    if (bar.time <= time) anchor = bar.time
    else break
  }
  return anchor
}

function actionMarker(action: string) {
  if (action === 'BUY') return { colorKey: 'up', shape: 'arrowUp' as const, text: '看涨' }
  if (action === 'SELL') return { colorKey: 'down', shape: 'arrowDown' as const, text: '看跌' }
  return { colorKey: 'flat', shape: 'circle' as const, text: '' }
}

// v275: 自分析交易点 K 线验证
//   对每个 B/S 信号: 找信号后 30 分钟 K 线 close 相对信号时刻 close 的变化
//   判定: BUY 信号后涨 >= 0.1% → match; 跌 >= 0.1% → mismatch; 否则 neutral
//         SELL 信号后跌 >= 0.1% → match; 涨 >= 0.1% → mismatch; 否则 neutral
function computeSignalVerdicts(signals: any[], bars: HistoryBar[]) {
  if (!signals?.length || !bars?.length) {
    return { items: [], match: 0, mismatch: 0, neutral: 0 }
  }
  // 按时间升序排序 bars 以便二分查找
  const sortedBars = [...bars].sort((a, b) => a.time - b.time)
  const horizonSec = 30 * 60  // 30 分钟

  let match = 0
  let mismatch = 0
  let neutral = 0
  const items: any[] = []

  for (const s of signals) {
    let ts = 0
    const t = s.news_time
    if (typeof t === 'number') ts = t
    else if (typeof t === 'string') {
      const ms = Date.parse(t)
      ts = isNaN(ms) ? 0 : Math.floor(ms / 1000)
    }
    if (!ts) continue

    // 找信号时刻的 close (≤ ts 最近一根 K 线)
    let baseClose: number | null = null
    let baseIdx = -1
    for (let i = 0; i < sortedBars.length; i++) {
      if (sortedBars[i].time <= ts) {
        baseClose = sortedBars[i].close
        baseIdx = i
      } else {
        break
      }
    }
    if (baseClose == null || baseIdx < 0) continue

    // 找信号后 [ts, ts+30min] 内最后一根 K 线
    let endClose: number | null = null
    for (let i = baseIdx; i < sortedBars.length; i++) {
      const t2 = sortedBars[i].time
      if (t2 > ts + horizonSec) break
      endClose = sortedBars[i].close
    }
    if (endClose == null || endClose === baseClose) {
      // K 线数据不足（窗口外）→ 跳过
      continue
    }

    const movePct = ((endClose - baseClose) / baseClose) * 100
    let verdict: 'match' | 'mismatch' | 'neutral'
    const absMove = Math.abs(movePct)
    if (absMove < 0.1) {
      verdict = 'neutral'
    } else if (s.action === 'BUY') {
      verdict = movePct > 0 ? 'match' : 'mismatch'
    } else if (s.action === 'SELL') {
      verdict = movePct < 0 ? 'match' : 'mismatch'
    } else {
      verdict = 'neutral'
    }

    if (verdict === 'match') match++
    else if (verdict === 'mismatch') mismatch++
    else neutral++

    items.push({
      ...s,
      news_time: ts,
      move_pct: movePct,
      verdict,
      base_close: baseClose,
      end_close: endClose,
    })
  }

  // 按时间倒序（最新在前）
  items.sort((a, b) => b.news_time - a.news_time)
  return { items, match, mismatch, neutral }
}

// K 线事件标注常驻显示标题（无需 hover/点击），截断到 12 字控制密集度
function titleText(s: string): string {
  const t = (s ?? '').replace(/\s+/g, ' ').trim()
  if (!t) return ''
  return t.length > 12 ? `${t.slice(0, 12)}…` : t
}

// 需求：K 线叠加交易量 + 均线。简单移动平均（前端计算，数据不足返回 null）
function sma(values: Array<number | null | undefined>, period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null)
  for (let i = period - 1; i < values.length; i++) {
    let sum = 0
    let ok = true
    for (let j = i - period + 1; j <= i; j++) {
      const v = values[j]
      if (typeof v !== 'number' || !Number.isFinite(v)) {
        ok = false
        break
      }
      sum += v
    }
    if (ok) out[i] = sum / period
  }
  return out
}

const MA_PERIODS = [5, 10, 20] as const
const MA_COLORS = ['#f59e0b', '#3b82f6', '#a855f7'] as const
type CalEvent = {
  ts_bj: number
  title: string
  star: number
  actual: string | null
  consensus: string | null
  is_released: boolean
  surprise: string
  age_hours: number
}
const RSI_PERIOD = 14

// RSI(14) Wilder 平滑：前 14 根用 SMA 种子，其后 EMA 平滑。
// 返回长度=values.length；前 period-1 为 null。
function rsi(closes: Array<number>, period: number = RSI_PERIOD): Array<number | null> {
  const n = closes.length
  const out: Array<number | null> = new Array(n).fill(null)
  if (n < period + 1) return out
  let gainSum = 0
  let lossSum = 0
  for (let i = 1; i <= period; i++) {
    const ch = closes[i] - closes[i - 1]
    if (ch > 0) gainSum += ch
    else lossSum += -ch
  }
  let avgGain = gainSum / period
  let avgLoss = lossSum / period
  const rs0 = avgLoss === 0 ? 100 : avgGain / avgLoss
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + rs0)
  for (let i = period + 1; i < n; i++) {
    const ch = closes[i] - closes[i - 1]
    const gain = ch > 0 ? ch : 0
    const loss = ch < 0 ? -ch : 0
    avgGain = (avgGain * (period - 1) + gain) / period
    avgLoss = (avgLoss * (period - 1) + loss) / period
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss)
  }
  return out
}

// 归因页 K 线 MACD：与 kline-chart.tsx 的 lib/technical-indicators.macd 行为一致。
// 输入收盘价数组；返回 DIF / DEA / HIST 三条等长序列，前段不足窗口期 = null。
//   DIF = EMA12 - EMA26
//   DEA = EMA9(DIF)
//   HIST = 2 * (DIF - DEA)（柱状：正=多头，负=空头）
function emaArr(values: Array<number>, period: number): Array<number | null> {
  const n = values.length
  const out: Array<number | null> = new Array(n).fill(null)
  if (n < period) return out
  const k = 2 / (period + 1)
  // 种子：前 period 个的 SMA
  let seed = 0
  for (let i = 0; i < period; i++) seed += values[i]
  seed /= period
  out[period - 1] = seed
  let prev = seed
  for (let i = period; i < n; i++) {
    const v = values[i] * k + prev * (1 - k)
    out[i] = v
    prev = v
  }
  return out
}

function macd(closes: Array<number>): {
  dif: Array<number | null>
  dea: Array<number | null>
  hist: Array<number | null>
} {
  const n = closes.length
  const emaFast = emaArr(closes, 12)
  const emaSlow = emaArr(closes, 26)
  const dif: Array<number | null> = new Array(n).fill(null)
  for (let i = 0; i < n; i++) {
    const f = emaFast[i]
    const s = emaSlow[i]
    dif[i] = f !== null && s !== null ? f - s : null
  }
  // DEA = EMA9(DIF)，null 跳过；种子 = 前 9 个非空 DIF 的平均
  const dea: Array<number | null> = new Array(n).fill(null)
  const validBuf: number[] = []
  for (let i = 0; i < n; i++) {
    const d = dif[i]
    if (d === null) continue
    validBuf.push(d)
    if (validBuf.length < 9) continue
    if (validBuf.length === 9) {
      let s = 0
      for (const v of validBuf) s += v
      dea[i] = s / 9
      continue
    }
    const prev = dea[i - 1]
    if (prev === null) continue
    const k = 2 / (9 + 1)
    dea[i] = d * k + prev * (1 - k)
  }
  const hist: Array<number | null> = new Array(n).fill(null)
  for (let i = 0; i < n; i++) {
    const d = dif[i]
    const e = dea[i]
    hist[i] = d !== null && e !== null ? (d - e) * 2 : null
  }
  return { dif, dea, hist }
}

const MACD_COLORS = {
  dif: '#f7a440',    // 橙
  dea: '#8b5cf6',    // 紫
  histUp: '#2ebd85', // 涨绿
  histDown: '#f6465d', // 跌红
} as const

// v172: 异动标签具体化（用户反馈"大K线徒增量能没识别"——光"量能异动"三字看不出触发原因）
//   拼装优先级：'both' 同时给 pct + 量比；'volume' 只给量比；'pct' 只给 pct
//   示例：'+3.2%' / '2.5×量' / '+3.2% 2.5×量'
function abnormalLabel(a: AbnormalPoint): string {
  const parts: string[] = []
  const t = a.trigger_type
  // v221：新增 'swing' 类型（5 根累计 swing 触发，pct 为累计涨跌幅）
  if (a.pct_change != null && (t === 'pct' || t === 'both' || t === 'swing')) {
    const pct = a.pct_change
    parts.push(`${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`)
  }
  if (a.vol_ratio != null && (t === 'volume' || t === 'both')) {
    parts.push(`${a.vol_ratio.toFixed(1)}×量`)
  }
  if (t === 'swing') {
    parts.push('连摆')  // 标识滚动摆动触发
  }
  if (!parts.length) return '异动'
  return parts.join(' ')
}

// 需求2：异动点方向配色（涨绿 / 跌红，与 K 线涨跌色一致；无涨跌数据时中性金）
function abnormalColor(a: AbnormalPoint, palette: { up: string; down: string }): string {
  if (a.pct_change == null) return '#f59e0b'
  return a.pct_change >= 0 ? palette.up : palette.down
}

// v280 — 订单簿深度热力图子组件：右侧 90px 柱状堆积图（每档 5px 紧密堆叠 + qty 渐变）
function OrderbookOverlay({
  orderbook,
  series,
}: {
  orderbook: { bids: [number, number][]; asks: [number, number][]; ts_ms: number }
  series: ISeriesApi<'Candlestick'>
}) {
  const [tick, setTick] = useState(0)
  const [totalQty, setTotalQty] = useState({ bid: 0, ask: 0 })
  const [layout, setLayout] = useState<{
    asks: { y: number; qty: number; price: number; alpha: number; width: number }[]
    bids: { y: number; qty: number; price: number; alpha: number; width: number }[]
    midY: number
    midPrice: number
  } | null>(null)

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 250)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    try {
      if (!orderbook.asks.length || !orderbook.bids.length) {
        setLayout(null)
        return
      }
      const asksAsc = [...orderbook.asks].sort((a, b) => a[0] - b[0])
      const bidsDesc = [...orderbook.bids].sort((a, b) => b[0] - a[0])
      const midPrice = (asksAsc[0][0] + bidsDesc[0][0]) / 2
      const asksTop = asksAsc.slice(0, 30)
      const bidsTop = bidsDesc.slice(0, 30)
      const mq = Math.max(
        ...asksTop.map(([, q]) => q),
        ...bidsTop.map(([, q]) => q),
        0.0000001,
      )
      setTotalQty({
        bid: bidsTop.reduce((s, [, q]) => s + q, 0),
        ask: asksTop.reduce((s, [, q]) => s + q, 0),
      })
      const asksLayout = asksTop.map(([p, q]) => {
        const y = series.priceToCoordinate(p) ?? 0
        const ratio = q / mq
        return { y, qty: q, price: p, alpha: 0.18 + ratio * 0.72, width: 24 + ratio * 60 }
      })
      const bidsLayout = bidsTop.map(([p, q]) => {
        const y = series.priceToCoordinate(p) ?? 0
        const ratio = q / mq
        return { y, qty: q, price: p, alpha: 0.18 + ratio * 0.72, width: 24 + ratio * 60 }
      })
      const midY = series.priceToCoordinate(midPrice) ?? 0
      setLayout({ asks: asksLayout, bids: bidsLayout, midY, midPrice })
    } catch {
      setLayout(null)
    }
  }, [orderbook, series, tick])

  if (!layout) {
    return (
      <div className="pointer-events-none absolute right-0 top-0 z-[4] flex h-full w-[90px] items-start justify-end pt-12 pr-1">
        <div className="rounded bg-card/80 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
          订单簿无数据
        </div>
      </div>
    )
  }

  const ROW_H = 5

  return (
    <div className="pointer-events-none absolute right-0 top-0 z-[4] h-full w-[90px]">
      <div className="absolute right-1 top-1 flex items-center gap-1 rounded bg-card/90 px-1.5 py-0.5 font-mono text-[8px] text-muted-foreground shadow-sm">
        <span>📊 深度</span>
        <span style={{ color: '#ef4444' }}>卖 {layout.asks.length}</span>
        <span>·</span>
        <span style={{ color: '#22c55e' }}>买 {layout.bids.length}</span>
      </div>
      <div className="absolute bottom-1 right-1 flex items-center gap-1 rounded bg-card/90 px-1.5 py-0.5 font-mono text-[8px] text-muted-foreground shadow-sm">
        <span style={{ color: '#ef4444' }}>∑{totalQty.ask.toFixed(2)}</span>
        <span>·</span>
        <span style={{ color: '#22c55e' }}>∑{totalQty.bid.toFixed(2)}</span>
      </div>
      {layout.midY > 0 && layout.midY < 9999 && (
        <div
          className="absolute left-0 right-0 border-t border-dashed pointer-events-none"
          style={{ top: `${layout.midY}px`, borderColor: 'rgba(148, 163, 184, 0.55)' }}
        />
      )}
      {layout.asks.map((row, i) => {
        if (row.y < 0 || row.y > 9999) return null
        return (
          <div
            key={`a-${i}-${row.price}`}
            className="absolute right-1 flex items-center justify-end overflow-hidden rounded-[1px]"
            style={{
              top: `${row.y - ROW_H / 2}px`,
              height: `${ROW_H - 0.5}px`,
              width: `${row.width}px`,
              background: `rgba(239, 68, 68, ${row.alpha})`,
              borderRight: row.alpha > 0.7 ? '1px solid #fca5a5' : 'none',
            }}
            title={`卖 ${row.price}  量 ${row.qty}  (${(row.alpha * 100).toFixed(0)}%)`}
          />
        )
      })}
      {layout.bids.map((row, i) => {
        if (row.y < 0 || row.y > 9999) return null
        return (
          <div
            key={`b-${i}-${row.price}`}
            className="absolute right-1 flex items-center justify-end overflow-hidden rounded-[1px]"
            style={{
              top: `${row.y - ROW_H / 2}px`,
              height: `${ROW_H - 0.5}px`,
              width: `${row.width}px`,
              background: `rgba(34, 197, 94, ${row.alpha})`,
              borderRight: row.alpha > 0.7 ? '1px solid #86efac' : 'none',
            }}
            title={`买 ${row.price}  量 ${row.qty}  (${(row.alpha * 100).toFixed(0)}%)`}
          />
        )
      })}
    </div>
  )
}

// v280 — Pivot 主图左内侧位置标注子组件
//   7 道价格线在主图左侧各画一个彩色小标签，跟随 chart 价格区间实时定位
//   标签内容：键（P/R1-R3/S1-S3）+ 价格 + 中/支/阻
//   颜色：橙 P / 绿 R / 红 S（与价格线同色），加深底色 + 白字
function PivotLabelsOverlay({
  pivot,
  series,
}: {
  pivot: { p: number; r1: number; r2: number; r3: number; s1: number; s2: number; s3: number; period: number; label: string }
  series: ISeriesApi<'Candlestick'>
}) {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 250)
    return () => clearInterval(id)
  }, [])

  const labels: { k: string; price: number; bg: string; role: string }[] = [
    { k: 'R3', price: pivot.r3, bg: '#22c55e', role: '阻力' },
    { k: 'R2', price: pivot.r2, bg: '#4ade80', role: '阻力' },
    { k: 'R1', price: pivot.r1, bg: '#86efac', role: '阻力' },
    { k: 'P',  price: pivot.p,  bg: '#fb923c', role: '中线' },
    { k: 'S1', price: pivot.s1, bg: '#fca5a5', role: '支撑' },
    { k: 'S2', price: pivot.s2, bg: '#f87171', role: '支撑' },
    { k: 'S3', price: pivot.s3, bg: '#ef4444', role: '支撑' },
  ]

  // 价格精度（动态：金 1 位 / 加密 2 位 / WTI 2 位）
  const prec = pivot.p >= 1000 ? 1 : pivot.p >= 10 ? 2 : 4
  const fmt = (p: number) => p.toFixed(prec)
  void tick

  return (
    <div className="pointer-events-none absolute left-1 top-0 z-[3] h-full w-[80px]">
      {labels.map((lb) => {
        const y = series.priceToCoordinate(lb.price)
        if (y == null || y < 0 || y > 9999) return null
        return (
          <div
            key={lb.k}
            className="absolute left-0 flex items-center gap-0.5 rounded-r px-1 py-0.5 font-mono text-[9px] leading-none shadow-sm"
            style={{
              top: `${y - 7}px`,
              background: lb.bg,
              color: lb.k === 'P' ? '#fff' : '#0a0a0a',
              borderLeft: `3px solid ${lb.bg}`,
            }}
            title={`${lb.k} ${lb.role}位 = ${fmt(lb.price)}（基于${pivot.label} ${pivot.period} 根 K 线）`}
          >
            <span className="font-bold">{lb.k}</span>
            <span className="text-[8px] opacity-80">{fmt(lb.price)}</span>
          </div>
        )
      })}
    </div>
  )
}

// v2026.10.15: 增加 externalRefreshToken 接收 page 推过来的统一 tick
export function MoversPanel({ externalRefreshToken }: { externalRefreshToken?: number } = {}) {
  // v302+ P0: 替代散落的 window.confirm/alert — toast 异步、不阻塞主线程、不打断 K 线轮询。
  const { toast } = useToast()
  const isDark = useIsDark()
  const router = useRouter()
  const [items, setItems] = useState<MoverItem[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [missing, setMissing] = useState<string>('')
  const [chartReady, setChartReady] = useState(false)
  // v220：默认仍是 10m 粒度（事件标注仍清晰），但 bar limit 单独拉长到 5 天。
  const [timeframe, setTimeframe] = useState<string>('10m')
  // v220：30 天时段 — 用户「至少显示生成近 30 天的这三类所有事件」。
  // 4h × 180 根 = 720h = 30d，覆盖后端 news_events 30 天视野 + 30 天图例 marker。
  // v220：用户偏好「仍是 10m 粒度，但默认跨度更长」 → 10m 拉到 720 根 ≈ 5 天。
  // v302+ P0: TF_TO_BAR_LIMIT / TF_TO_INTERVAL / TF_TO_HOURS 已提升到 module scope。
  // 30 天事件存档（用户 2026-10-03：「保存归因页所有生成的数据在本地作为知识图谱参考」）
  // SNAPSHOT_LS_KEY / SNAPSHOT_PREFIX 已提升到 module scope（SnapshotIndex 子组件复用）
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null)  // 上次保存时间，显示在 header
  // v278 — 「累计全量加载」状态：用户点按后强制按 30d 拉一次最长区间，
  //   并把结果与现有 markersData 合并（保留更早期看到的事件）
  const [backfillBusy, setBackfillBusy] = useState(false)
  const [backfillMsg, setBackfillMsg] = useState<string | null>(null)
  const [markersData, setMarkersData] = useState<{
    calendar: any[]
    impact: any[]
    signals: any[]
    bars: any[]
    abnormals: any[]
    swingPoints?: any[]     // v221.3: 拐点 + 事前氛围
  }>({ calendar: [], impact: [], signals: [], bars: [], abnormals: [], swingPoints: [] })
  // v275: 蓝色事件 K 线影响验证（按 ts_bj 索引，便于 marker 文字拼接 & 面板渲染）
  const [calImpact, setCalImpact] = useState<Record<number, any>>({})
  // v275: 图表高度自适应 — 根据 aboveBar marker 数动态算（用户:「做成高度自适应」）
  //   base = 480px，每多 1 个 aboveBar marker +14px（lw-charts marker ~14px 行高），封顶 800px
  const [chartHeight, setChartHeight] = useState<number>(480)
  // v284 — HTML overlay 重算需要最新 chartHeight，但 recomputeOverlay deps=[]（不重创建）
  //   用 ref 拿实时值；chartHeight 变化时通过后续 useEffect 触发 setOverlayRev
  const chartHeightRef = useRef<number>(chartHeight)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [attribMap, setAttribMap] = useState<Record<string, Attribution | null>>({})
  const [attribLoading, setAttribLoading] = useState<string | null>(null)
  // v280 — Pivot Point 7 线（默认开）+ 当前计算的 pivot 信息（Header 徽章 + PivotLabelsOverlay 用）
  const [showPivot, setShowPivot] = useState(true)
  const [pivotInfo, setPivotInfo] = useState<{
    p: number; r1: number; r2: number; r3: number; s1: number; s2: number; s3: number; period: number; label: string
  } | null>(null)
  const pivotLinesRef = useRef<{ [k: string]: IPriceLine | null }>({})
  // v280 — 实时订单簿深度（合约挂单堆积热力图；默认关，按钮开启）
  const [showBook, setShowBook] = useState(false)
  const [orderbook, setOrderbook] = useState<{ bids: [number, number][]; asks: [number, number][]; ts_ms: number } | null>(null)
  const [orderbookStatus, setOrderbookStatus] = useState<'idle' | 'loading' | 'ok' | 'unavailable'>('idle')
  // 需求：图例颜色点击切换 — 蓝/红/绿 三组线索独立隐藏/显示。
  // 默认全开；点击图例中对应颜色文字切换。
  // v20251014：HOLD 灰点已从 K 线移除（见 1944 行），此规则改为「所有有 _group 的 marker 按图例过滤」。
  // 持久化到 localStorage（用户 2026-10-03：偏好跨刷新保留，不用每次手动重开）。
  const CLUE_FILTERS_LS_KEY = 'trident:attribution:clueFilters'
  const [clueFilters, setClueFilters] = useState<{ blue: boolean; red: boolean; green: boolean }>(() => {
    if (typeof window === 'undefined') return { blue: true, red: true, green: true }
    try {
      const saved = window.localStorage.getItem(CLUE_FILTERS_LS_KEY)
      if (saved) {
        const j = JSON.parse(saved)
        return {
          blue: j.blue !== false,  // 缺失字段默认 true
          red: j.red !== false,
          green: j.green !== false,
        }
      }
    } catch { /* SSR / 隐私模式 / 损坏数据 → 全部默认 */ }
    return { blue: true, red: true, green: true }
  })
  useEffect(() => {
    try { window.localStorage.setItem(CLUE_FILTERS_LS_KEY, JSON.stringify(clueFilters)) } catch {}
  }, [clueFilters])
  const toggleClue = (k: 'blue' | 'red' | 'green') =>
    setClueFilters((s) => ({ ...s, [k]: !s[k] }))

  // v277 — 自动保存 markersData 到 localStorage（debounce 1.5s 避免频繁写）
  //   用户反馈（2026-10-07）：「所有辨别过的信息都保存在归因页 k 线的原图上」
  //   实现：markersData 一变化就 debounce 1.5s 自动写 localStorage，
  //         不再依赖用户手动点「💾 保存到本地」按钮。
  const autoSaveTimerRef = useRef<number | null>(null)
  useEffect(() => {
    if (!selected) return
    // 跳过空 state（防止初次清空覆盖已存档）
    const total = (markersData.calendar?.length ?? 0)
      + (markersData.impact?.length ?? 0)
      + (markersData.signals?.length ?? 0)
      + (markersData.abnormals?.length ?? 0)
      + (markersData.bars?.length ?? 0)
    if (total === 0) return
    if (autoSaveTimerRef.current !== null) {
      window.clearTimeout(autoSaveTimerRef.current)
    }
    autoSaveTimerRef.current = window.setTimeout(() => {
      try {
        const key = `${SNAPSHOT_PREFIX}${selected}`
        // v278 修复：先读旧存档，与当前 markersData 合并后再写入（避免覆盖式丢失历史）
        let existing: any = {}
        try {
          const raw = window.localStorage.getItem(key)
          if (raw) existing = JSON.parse(raw) || {}
        } catch { /* 损坏 → 视为空 */ }
        // bars 用旧+新合并去重（保留最广时间窗）；其它 marker 按 key 合并
        const mergedBars = mergeBars(existing.bars ?? [], markersData.bars ?? [])
        const mergedCalendar = mergeUnique(
          existing.calendar ?? [],
          markersData.calendar ?? [],
          (e: any) => `${e?.ts_bj ?? 0}:${(e?.title ?? '').slice(0, 40)}`,
        )
        const mergedImpact = mergeUnique(
          existing.impact ?? [],
          markersData.impact ?? [],
          (e: any) => `${e?.event_source ?? ''}:${e?.event_id ?? ''}:${e?.event_time ?? 0}`,
        )
        const mergedSignals = mergeUnique(
          existing.signals ?? [],
          markersData.signals ?? [],
          (e: any) => `${e?.id ?? 0}:${e?.news_time ?? ''}:${e?.action ?? ''}`,
        )
        const mergedAbnormals = mergeUnique(
          existing.abnormals ?? [],
          markersData.abnormals ?? [],
          (e: any) => `${e?.ts ?? 0}`,
        )
        const mergedSwing = mergeUnique(
          existing.swingPoints ?? [],
          markersData.swingPoints ?? [],
          (e: any) => `${e?.ts ?? e?.time ?? 0}:${e?.kind ?? e?.type ?? ''}`,
        )
        const snap = {
          ...markersData,
          calendar: mergedCalendar,
          impact: mergedImpact,
          signals: mergedSignals,
          abnormals: mergedAbnormals,
          swingPoints: mergedSwing,
          bars: mergedBars,
          saved_at: new Date().toISOString(),
          saved_at_bj: new Date().toLocaleString('zh-CN', { hour12: false }),
          asset: selected,
          auto_saved: true,
          cumulative: true,  // 标记为累计存档（vs v277 一次性覆盖存档）
        }
        window.localStorage.setItem(key, JSON.stringify(snap))
        // 同步索引（保存到本地 / 全局列表）— bar_count 用合并后的最广区间
        const idxRaw = window.localStorage.getItem(SNAPSHOT_LS_KEY)
        const idx: Array<{ asset: string; saved_at: string; bar_count: number; saved_at_bj: string }> = idxRaw ? JSON.parse(idxRaw) : []
        const filtered = idx.filter((x) => x.asset !== selected)
        filtered.unshift({
          asset: selected ?? '',
          saved_at: snap.saved_at,
          saved_at_bj: snap.saved_at_bj,
          bar_count: mergedBars.length,
        })
        window.localStorage.setItem(SNAPSHOT_LS_KEY, JSON.stringify(filtered.slice(0, 50)))
        setSnapshotAt(snap.saved_at_bj + ' · 累计')
      } catch (e) {
        console.warn('[attribution auto-save] 失败', e)
      }
    }, 1500)
    return () => {
      if (autoSaveTimerRef.current !== null) {
        window.clearTimeout(autoSaveTimerRef.current)
      }
    }
  }, [selected, markersData])

  // v277 — 自动恢复 markersData from localStorage on selected 变化（首次）
  //   用户反馈：「不要只是辨别今天的数据，所有辨别过得的信息都保存在归因页 k 线的原图上」
  //   实现：selected 切换 → 检查 localStorage 是否有上次存档 → 若有则与当前 markersData 合并
  //         （不再因为 curTotal > 0 而跳过，避免 fetch 完成后存档历史丢失）。
  //   v278：合并而非覆盖 — 之前的 cur 数据（最新 fetch）与存档历史（之前看过的）合并保留。
  const _restoredFor = useRef<string | null>(null)
  useEffect(() => {
    if (!selected || typeof window === 'undefined') return
    if (_restoredFor.current === selected) return  // 同一资产只恢复一次
    try {
      const raw = window.localStorage.getItem(`${SNAPSHOT_PREFIX}${selected}`)
      if (!raw) return
      const snap = JSON.parse(raw)
      setMarkersData((cur) => {
        // v278：无论 cur 是否有数据，都合并存档（保留历史）
        // 只有 bars 用存档的（fetch 会用最新 bars 覆盖 state，无需用旧的）
        if (!snap?.bars || snap.bars.length === 0) return cur
        const mergedBars = mergeBars(cur.bars ?? [], Array.isArray(snap.bars) ? snap.bars : [])
        const mergedCalendar = mergeUnique(
          cur.calendar ?? [],
          Array.isArray(snap.calendar) ? snap.calendar : [],
          (e: any) => `${e?.ts_bj ?? 0}:${(e?.title ?? '').slice(0, 40)}`,
        )
        const mergedImpact = mergeUnique(
          cur.impact ?? [],
          Array.isArray(snap.impact) ? snap.impact : [],
          (e: any) => `${e?.event_source ?? ''}:${e?.event_id ?? ''}:${e?.event_time ?? 0}`,
        )
        const mergedSignals = mergeUnique(
          cur.signals ?? [],
          Array.isArray(snap.signals) ? snap.signals : [],
          (e: any) => `${e?.id ?? 0}:${e?.news_time ?? ''}:${e?.action ?? ''}`,
        )
        const mergedAbnormals = mergeUnique(
          cur.abnormals ?? [],
          Array.isArray(snap.abnormals) ? snap.abnormals : [],
          (e: any) => `${e?.ts ?? 0}`,
        )
        const mergedSwing = mergeUnique(
          cur.swingPoints ?? [],
          Array.isArray(snap.swingPoints) ? snap.swingPoints : [],
          (e: any) => `${e?.ts ?? e?.time ?? 0}:${e?.kind ?? e?.type ?? ''}`,
        )
        return {
          calendar: mergedCalendar,
          impact: mergedImpact,
          signals: mergedSignals,
          bars: mergedBars,
          abnormals: mergedAbnormals,
          swingPoints: mergedSwing,
        }
      })
      setSnapshotAt((snap?.saved_at_bj ?? null) + ' · 已合并')
      _restoredFor.current = selected
    } catch (e) {
      console.warn('[attribution auto-restore] 失败', e)
    }
  }, [selected])

  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const maRefs = useRef<Array<ISeriesApi<'Line'> | null>>([])
  const rsiRef = useRef<ISeriesApi<'Line'> | null>(null)
  const macdDifRef = useRef<ISeriesApi<'Line'> | null>(null)
  const macdDeaRef = useRef<ISeriesApi<'Line'> | null>(null)
  const macdHistRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const markersRef = useRef<{ detach: () => void } | null>(null)

  // v279: HTML overlay 文字层（替代 lw-charts 内置水平文字）
  //   lw-charts 的 marker text 沿图标右边水平渲染，多 marker 在相近 X 时文字水平堆叠重叠
  //   这里把文字抽到 HTML overlay 自定义位置 + 上下交错算法
  type OverlayLabel = {
    key: string           // unique key (ts + src + idx)
    text: string          // 显示文本（已截短）
    color: string         // 文字色（带 textShadow 加粗体确保可读，不再用背景框）
    slot: 'above' | 'below' | 'inBar'  // 垂直锚点
    barTime: number       // 锚定的 K 线时间戳（用于 X 坐标）
    row: number           // 同 K 线同槽的交错行号（0=最贴蜡烛，往外推）
    side: -1 | 1           // 左右微偏移方向（zigzag）
    _x?: number            // 视口 X 像素（重算时写入，初始可空）
    _y?: number            // 视口 Y 像素（重算时写入，初始可空）
    // v287: barHigh/barLow/barClose 不再使用（Y 落到 3 条固定行），仅 barTime 用于 X
  }
  const overlayLabelsRef = useRef<OverlayLabel[]>([])
  const [overlayLabels, setOverlayLabels] = useState<OverlayLabel[]>([])
  // 触发 overlay 重算的版本号（视口大小变化、chart 重绘、marker 重建 都递增）
  const [overlayRev, setOverlayRev] = useState(0)

  // 重新计算 overlay 坐标（X/Y）— 在 marker 数据更新 + 图表滚动/缩放/尺寸变化时调用
  //   v287 — 三条固定横排（修复 v286 残留：inBar 标签贴在柱体里、红/绿蜡烛色对色时经常看不见）：
  //   v286: inBar 放 closeY - 8 → 红/绿柱体色对色时 textShadow 也不够、5+ 信号无可见文字
  //   v287 设计：
  //     - 3 条固定 Y 行（不再让 inBar 紧贴 closeY）:
  //         above → 顶部 4% (chartH * 0.04)
  //         inBar → 顶部空白带中部 (chartH * 0.24) — K 线柱顶 32% 之上
  //         below → 底部 96% (chartH * 0.96)
  //     - X 严格 = 蜡烛 timeToCoordinate（不横向 stagger，保留时间归属）
  //     - 不画引导线 — 标签 X 与 K 线 X 完全对齐
  //   v292 — 按 lab.row 垂直堆叠（用户要求「文字回到它原来的位置」）:
  //     - 同 (barTime, slot) 多标签：above/inBar 向下推 (row * ROW_H)，
  //       below 向上推 (row * ROW_H)，避免同一时间点堆叠成一坨看不见
  //     - 不同时间点的标签 X 错开，但仍按 row 错开防水平覆盖
  //     - overflow-visible：>5 row 时允许溢出 chart 边界（与 markers 同语义）
  const recomputeOverlay = useCallback(() => {
    const chart = chartRef.current
    const candle = candleRef.current
    if (!chart || !candle) { setOverlayLabels([]); return }
    const labels = overlayLabelsRef.current
    if (!labels.length) { setOverlayLabels([]); return }
    const ts = chart.timeScale()
    const chartH = chartHeightRef.current || 480
    const ZONE_PAD = 4
    const ROW_H = 12  // 每行垂直间距（px）—— 与 label line-height(10px)+ padding(2px) 一致
    const ABOVE_Y = Math.max(ZONE_PAD, chartH * 0.04)           // 顶部 4% 一行
    const INBAR_Y = chartH * 0.24                               // 顶部空白带中部（K 线柱顶 32% 之上）
    const BELOW_Y = Math.min(chartH - ZONE_PAD, chartH * 0.96)  // 底部 96% 一行
    const out: OverlayLabel[] = []
    for (const lab of labels) {
      const xRaw = ts.timeToCoordinate(lab.barTime as Time)
      if (xRaw == null) continue  // 超出可视范围
      const rowOffset = (lab.row ?? 0) * ROW_H
      let yBase: number
      if (lab.slot === 'above') {
        // above：向下推（贴柱体越近 row 越大 → row=0 紧贴蜡烛顶；row=1/2/3 往上腾）
        yBase = ABOVE_Y + rowOffset
      } else if (lab.slot === 'below') {
        // below：向上推（row=0 紧贴蜡烛底；row=1/2/3 往下腾 — bottom 坐标语义）
        yBase = BELOW_Y - rowOffset
      } else {
        // inBar — 向下推（INBAR_Y 已在 candle 上方空带，row=0/1/2 错开避免覆盖）
        yBase = INBAR_Y + rowOffset
      }
      out.push({ ...lab, _x: xRaw, _y: yBase })
    }
    setOverlayLabels(out)
  }, [])

  // 订阅图表可视范围变化 → 触发 overlay 重算
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const onRangeChange = () => setOverlayRev((v) => v + 1)
    const ts = chart.timeScale()
    ts.subscribeVisibleTimeRangeChange(onRangeChange)
    ts.subscribeVisibleLogicalRangeChange(onRangeChange)
    return () => {
      try { ts.unsubscribeVisibleTimeRangeChange(onRangeChange) } catch {}
      try { ts.unsubscribeVisibleLogicalRangeChange(onRangeChange) } catch {}
    }
  }, [chartReady, chartHeight])

  // overlayRev 变化后重算坐标（marker 重建后也会调用 setOverlayRev）
  useEffect(() => { recomputeOverlay() }, [overlayRev, recomputeOverlay])
  // v284 — chartHeight 变化后同步 ref 并触发重算（pane 高度变化会改变可用交错区间）
  useEffect(() => {
    chartHeightRef.current = chartHeight
    setOverlayRev((v) => v + 1)
  }, [chartHeight])

  // 需求：归因页 ↔ 资产图谱连通 — 跳到 /knowledge-graph?asset=XAU，
  // causal-graph-panel 通过 useSearchParams 接收并自动选中该品种。
  const goGraph = (asset: string) => {
    router.push(`/knowledge-graph?asset=${encodeURIComponent(asset)}`)
  }

  // v220：当前选中品种 + 它的 30 天事件（先算出，后面 buildSnapshot / 按钮都能用）
  //   这里提前一份，避免在 hooks 之后才定义 currentEvents 导致 buildSnapshot 用不到。
  const current = (items ?? []).find((i) => i.asset === selected) ?? null
  const currentEvents = current?.events ?? []

  // v234：5 类彩色事件统计（与右上角图例一一对应）— 给 K 线下方统计面板用
  //   BUY / SELL / HOLD 新闻来自 currentEvents；★3+ 日历来自 markersData.calendar；
  //   柱内 B/S 交易信号来自 markersData.signals。
  const eventStats = useMemo(() => {
    const buyEvs = currentEvents.filter((e: any) => e.action === 'BUY')
    const sellEvs = currentEvents.filter((e: any) => e.action === 'SELL')
    const holdEvs = currentEvents.filter((e: any) => e.action === 'HOLD')
    const calEvs = (markersData.calendar ?? []).filter((c: any) => (c.star ?? 0) >= 3)
    const sigEvs = markersData.signals ?? []
    return {
      buy: {
        count: buyEvs.length,
        top: [...buyEvs].sort((a: any, b: any) => Math.abs(b.score ?? 0) - Math.abs(a.score ?? 0)).slice(0, 3),
      },
      sell: {
        count: sellEvs.length,
        top: [...sellEvs].sort((a: any, b: any) => Math.abs(b.score ?? 0) - Math.abs(a.score ?? 0)).slice(0, 3),
      },
      hold: {
        count: holdEvs.length,
        top: [...holdEvs].sort((a: any, b: any) => (b.ts ?? 0) - (a.ts ?? 0)).slice(0, 3),
      },
      calendar: {
        count: calEvs.length,
        top: [...calEvs].sort((a: any, b: any) => (b.star ?? 0) - (a.star ?? 0)).slice(0, 3),
      },
      signal: {
        count: sigEvs.length,
        buy: sigEvs.filter((s: any) => s.action === 'BUY').length,
        sell: sigEvs.filter((s: any) => s.action === 'SELL').length,
        top: [...sigEvs].sort((a: any, b: any) => Math.abs(b.score ?? 0) - Math.abs(a.score ?? 0)).slice(0, 3),
      },
    }
  }, [currentEvents, markersData.calendar, markersData.signals])

  // v278 — 「累计全量」覆盖范围：展示当前 markersData 涵盖的「最早/最晚事件时间」。
  //   这是用户反馈「要保存自己现实过的所有数据呈现保留在归因页的k线上」的关键 UX：
  //   让用户看到：当前 K 线虽然只显示 5 天，但累计存档已涵盖 30 天。
  const coverage = useMemo(() => {
    const c = markersCoverage(markersData)
    if (!c.earliest || !c.latest) return null
    return c
  }, [markersData])

  // v278 — 「📊 累计全量加载」按钮：强制按 30d 拉最长区间，把结果与当前 markersData 合并。
  //   之前用户切换 timeframe 时，旧 markers 会被新 fetch 覆盖；现在只要按一次这个按钮，
  //   即可把最长 30 天的事件一次拉全并永久并入累计状态。
  const backfillHistorical = useCallback(async () => {
    if (!selected) {
      setBackfillMsg('⚠ 请先选中一个品种')
      return
    }
    setBackfillBusy(true)
    setBackfillMsg('🔄 正在按 30d × 4h 拉最长区间 + 累计 30 天日历/impact/信号…')
    try {
      const now = Math.floor(Date.now() / 1000)
      const from30 = now - 30 * 86400
      // 并发拉：bars / calendar / impact / signals
      const [barsR, calR, impactR, signalR] = await Promise.all([
        fetch(`${API_BASE}/market/history/${encodeURIComponent(selected)}?interval=4h&limit=180`, { cache: 'no-store' }),
        fetch(`${API_BASE}/v189/calendar/events-for-asset?asset=${encodeURIComponent(selected)}&hours=720&star_min=3`, { cache: 'no-store' }),
        fetch(`${API_BASE}/v201/impact/by-asset?asset=${encodeURIComponent(selected)}&from_ts=${from30}&to_ts=${now}&severity_min=low&limit=200`, { cache: 'no-store' }),
        fetch(`${API_BASE}/trading/signals?asset=${encodeURIComponent(selected)}&from_ts=${from30}&to_ts=${now}&hide_neutral=1&limit=500`, { cache: 'no-store' }).catch(() => null),
      ])
      const barsJ = barsR.ok ? await barsR.json() : { bars: [] }
      const calJ = calR.ok ? await calR.json() : { events: [] }
      const impactJ = impactR.ok ? await impactR.json() : { items: [] }
      const signalJ = signalR && signalR.ok ? await signalR.json() : null
      const newBars = Array.isArray(barsJ?.bars) ? barsJ.bars : []
      const newCal = Array.isArray(calJ?.events) ? calJ.events : []
      const newImpact = Array.isArray(impactJ?.items) ? impactJ.items : []
      const newSignals = signalJ ? (Array.isArray(signalJ) ? signalJ : (Array.isArray(signalJ?.signals) ? signalJ.signals : (Array.isArray(signalJ?.items) ? signalJ.items : []))) : []
      setMarkersData((cur) => ({
        calendar: mergeUnique(cur.calendar ?? [], newCal, (e: any) => `${e?.ts_bj ?? 0}:${(e?.title ?? '').slice(0, 40)}`),
        impact: mergeUnique(cur.impact ?? [], newImpact, (e: any) => `${e?.event_source ?? ''}:${e?.event_id ?? ''}:${e?.event_time ?? 0}`),
        signals: mergeUnique(cur.signals ?? [], newSignals, (e: any) => `${e?.id ?? 0}:${e?.news_time ?? ''}:${e?.action ?? ''}`),
        // bars 用 fetch 来的最新 4h bars（保持 timeframe 兼容）
        bars: newBars.length > 0 ? newBars : (cur.bars ?? []),
        abnormals: cur.abnormals ?? [],
        swingPoints: cur.swingPoints ?? [],
      }))
      // 同时把 timeframe 切到 30d，确保 chart 能渲染 30 天 bars
      setTimeframe('30d')
      const summary = `✅ 累计完成：+${newCal.length} 日历 / +${newImpact.length} impact / +${newSignals.length} 信号 / ${newBars.length} bars（30d 区间）`
      setBackfillMsg(summary)
      console.log('[backfill]', summary)
    } catch (e: any) {
      const err = `❌ 累计失败：${e?.message ?? e}`
      setBackfillMsg(err)
      console.warn('[backfill] 失败', e)
    } finally {
      setBackfillBusy(false)
      // 8s 后自动清除成功消息
      window.setTimeout(() => setBackfillMsg(null), 8000)
    }
  }, [selected])

  // v220：归因页本地存档（用户 2026-10-03：「保存归因页所有生成的数据在本地作为知识图谱参考辅助决策的资料」）
  //   一次性把当前 K 线 + 3 类图例 marker + 异动 + 驱动摘要 + AI 归因打包成 JSON，
  //   落到 localStorage（容量够覆盖 30 天 × 5 币 ≈ 1-2 MB），同时支持导出/复制/查看/清空。
  //   结构刻意贴近「节点-边」形态：assets / events / signals / bars / attributions，
  //   方便后续接 Neo4j / NetworkX 直接 ingest。
  const buildSnapshot = useCallback(() => {
    const it = (items ?? []).find((x) => x.asset === selected) ?? null
    const tfInterval = TF_TO_INTERVAL[timeframe] ?? timeframe
    const tfHours = TF_TO_HOURS[timeframe] ?? 24
    const snap = {
      schema: 'trident.attribution.snapshot.v1',
      saved_at: new Date().toISOString(),
      saved_at_bj: new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 19).replace('T', ' '),
      asset: selected,
      asset_name: ASSET_META[selected ?? ''] ?? '',
      timeframe,
      timeframe_interval: tfInterval,
      timeframe_hours: tfHours,
      bar_count: markersData.bars.length,
      window: {
        from_ts: markersData.bars[0]?.time ?? 0,
        to_ts: markersData.bars[markersData.bars.length - 1]?.time ?? 0,
        from_bj: fmtTs(markersData.bars[0]?.time ?? 0),
        to_bj: fmtTs(markersData.bars[markersData.bars.length - 1]?.time ?? 0),
      },
      mover: it
        ? {
            asset: it.asset,
            price: it.price,
            change24h: it.change24h,
            source: it.source,
            source_count: it.source_count,
          }
        : null,
      events_30d: currentEvents,
      // 知识图谱节点（每个 id 都稳定可索引）
      nodes: {
        calendar: markersData.calendar.map((e: any) => ({
          id: `cal:${e.ts_bj ?? e.ts}:${(e.title ?? '').slice(0, 40)}`,
          kind: 'calendar',
          ts: e.ts_bj ?? e.ts,
          star: e.star,
          title: e.title,
          actual: e.actual ?? null,
          forecast: e.forecast ?? null,
          surprise: e.surprise ?? null,
          country: e.country ?? null,
          is_released: e.is_released ?? false,
        })),
        impact: markersData.impact.map((e: any) => ({
          id: `impact:${e.event_id ?? e.event_source}:${e.event_time}`,
          kind: 'impact',
          ts: e.event_time,
          title: e.event_title,
          severity: e.impact_severity,
          direction: e.direction,
          impact_pct: e.impact_pct,
          source: e.event_source,
        })),
        news: currentEvents.map((e: any, i: number) => ({
          id: `news:${e.ts}:${i}`,
          kind: 'news',
          ts: e.ts,
          action: e.action,
          score: e.score,
          content: e.content,
          source: e.source,
        })),
        signals: markersData.signals.map((s: any) => ({
          id: `sig:${s.id ?? s.created_at ?? Math.random()}`,
          kind: 'signal',
          ts: s.news_time ?? null,
          asset: s.asset,
          action: s.action,
          score: s.score,
          reasoning: s.reasoning,
        })),
        abnormals: (markersData.abnormals ?? []).map((a: any) => ({
          id: `abn:${selected}:${a.ts}`,
          kind: 'abnormal',
          ts: a.ts,
          pct_change: a.pct_change,
          vol_ratio: a.vol_ratio,
          trigger_type: a.trigger_type,
          interval: a.interval,
        })),
      },
      edges: [
        ...markersData.calendar.map((e: any) => ({ from: selected, to: `cal:${e.ts_bj ?? e.ts}:${(e.title ?? '').slice(0, 40)}`, rel: 'has_calendar_event' })),
        ...markersData.impact.map((e: any) => ({ from: selected, to: `impact:${e.event_id ?? e.event_source}:${e.event_time}`, rel: 'has_impact' })),
        ...currentEvents.map((e: any, i: number) => ({ from: selected, to: `news:${e.ts}:${i}`, rel: 'has_news' })),
        ...markersData.signals.map((s: any) => ({ from: selected, to: `sig:${s.id ?? s.created_at ?? Math.random()}`, rel: 'has_signal' })),
        ...(markersData.abnormals ?? []).map((a: any) => ({ from: selected, to: `abn:${selected}:${a.ts}`, rel: 'has_abnormal' })),
      ],
      bars: markersData.bars,
      anomaly_attributions: attribMap,
      drivers: it?.drivers ?? null,
      attribution_metadata: {
        saved_by: 'trident-frontend',
        schema_version: 1,
        time_window_hours: tfHours,
        events_horizon_days: 30,
      },
    }
    return snap
  }, [items, selected, timeframe, markersData, currentEvents, attribMap])

  // 保存到 localStorage（按资产 key；全局索引 SNAPSHOT_LS_KEY 列表快速浏览）
  const saveSnapshot = useCallback(() => {
    try {
      const snap = buildSnapshot()
      const key = `${SNAPSHOT_PREFIX}${selected}`
      window.localStorage.setItem(key, JSON.stringify(snap))
      const idxRaw = window.localStorage.getItem(SNAPSHOT_LS_KEY)
      const idx: Array<{ asset: string; saved_at: string; bar_count: number; saved_at_bj: string }> = idxRaw ? JSON.parse(idxRaw) : []
      const filtered = idx.filter((x) => x.asset !== selected)
      filtered.unshift({
        asset: selected ?? '',
        saved_at: snap.saved_at,
        saved_at_bj: snap.saved_at_bj,
        bar_count: snap.bar_count,
      })
      window.localStorage.setItem(SNAPSHOT_LS_KEY, JSON.stringify(filtered.slice(0, 50)))
      setSnapshotAt(snap.saved_at_bj)
      return snap
    } catch (e) {
      console.error('[snapshot] 保存失败', e)
      return null
    }
  }, [buildSnapshot, selected])

  // 导出 JSON 文件下载（带时间戳避免覆盖）
  const downloadSnapshot = useCallback(() => {
    const snap = saveSnapshot()
    if (!snap) return
    try {
      const blob = new Blob([JSON.stringify(snap, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `trident-attribution-${selected ?? 'unknown'}-${snap.saved_at.replace(/[:.]/g, '-')}.json`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (e) {
      console.error('[snapshot] 下载失败', e)
    }
  }, [saveSnapshot, selected])

  // 复制 JSON 到剪贴板（无需下载也能跑流程）
  const copySnapshot = useCallback(async () => {
    const snap = buildSnapshot()
    if (!snap) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(snap, null, 2))
      setSnapshotAt(snap.saved_at_bj + ' · 已复制')
    } catch (e) {
      console.error('[snapshot] 复制失败', e)
    }
  }, [buildSnapshot])

  // 查看 / 加载已存档（返回原始 JSON 字符串，方便弹窗展示）
  const loadSnapshotRaw = useCallback(() => {
    if (!selected) return null
    try {
      return window.localStorage.getItem(`${SNAPSHOT_PREFIX}${selected}`)
    } catch { return null }
  }, [selected])

  // 清空当前资产存档
  const clearSnapshot = useCallback(() => {
    if (!selected) return
    try {
      window.localStorage.removeItem(`${SNAPSHOT_PREFIX}${selected}`)
      const idxRaw = window.localStorage.getItem(SNAPSHOT_LS_KEY)
      const idx = idxRaw ? JSON.parse(idxRaw) : []
      window.localStorage.setItem(SNAPSHOT_LS_KEY, JSON.stringify(idx.filter((x: any) => x.asset !== selected)))
      setSnapshotAt(null)
    } catch (e) {
      console.error('[snapshot] 清空失败', e)
    }
  }, [selected])
  const askAttribution = async (asset: string, ts: number) => {
    const key = `${asset}:${ts}`
    setAttribLoading(key)
    try {
      const r = await fetch(`${API_BASE}/market/attribute?asset=${encodeURIComponent(asset)}&ts=${ts}`, { cache: 'no-store' })
      const d = r.ok ? await r.json() : null
      const attr: Attribution | null = d?.attribution ?? d ?? null
      setAttribMap((m) => ({ ...m, [key]: attr }))
    } catch {
      setAttribMap((m) => ({ ...m, [key]: { error: 'AI 归因失败' } }))
    } finally {
      setAttribLoading(null)
    }
  }

  // 拉取排行 + 事件
  useEffect(() => {
    let alive = true
    const load = () => {
      // v170: 透传 timeframe 让后端异动检测切到对应 interval
      fetch(`${API_BASE}/market/movers?timeframe=${timeframe}`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => {
          if (!alive) return
          const list = Array.isArray(d.items) ? (d.items as MoverItem[]) : []
          setItems(list)
          setSelected((cur) => cur ?? list[0]?.asset ?? null)
        })
        .catch((err) => { if (alive) setLoadErr(String(err)) })
    }
    void load()
    const timer = window.setInterval(load, 30_000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [timeframe])
  // v2026.10.15: 响应 page 层统一 tick. externalRefreshToken 改变时立即拉取
  useEffect(() => {
    if (externalRefreshToken && externalRefreshToken > 0) {
      fetch(`${API_BASE}/market/movers?timeframe=${timeframe}`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => {
          const list = Array.isArray(d.items) ? (d.items as MoverItem[]) : []
          setItems(list)
          setSelected((cur) => cur ?? list[0]?.asset ?? null)
        })
        .catch((err) => setLoadErr(String(err)))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externalRefreshToken])

  // 建图表（主题切换时重建）
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { color: palette.background },
        textColor: palette.text,
        fontFamily: 'var(--font-stack)',
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: palette.grid },
        horzLines: { color: palette.grid },
      },
      crosshair: { mode: 1 },
      // v279: 给 aboveBar/belowBar HTML overlay marker 留更大上下空间（label row×26/22px 阶梯堆叠）
      //   top margin 0.32 → 给 aboveBar 标签最多 5 行堆叠不裁切（row=4 时 Y=130px 在 0.32×480=154px 内）
      //   bottom 0.30 → 给 belowBar 标签最多 5 行堆叠
      rightPriceScale: { borderColor: palette.border, scaleMargins: { top: 0.32, bottom: 0.30 } },
      timeScale: {
        borderColor: palette.border,
        tickMarkFormatter: shTickFormatter,
        timeVisible: true,
        secondsVisible: false,
      },
      localization: { timeFormatter: shTickFormatter },
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: palette.up,
      downColor: palette.down,
      borderUpColor: palette.up,
      borderDownColor: palette.down,
      wickUpColor: palette.up,
      wickDownColor: palette.down,
      priceLineVisible: true,
      lastValueVisible: true,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    // 修复(v200)：主K线面板压低，给下方的 MACD/RSI/Vol 让出顶部空间。
    // 340px 高度 4 面板纵向分配:
    //   主K线+MA: top=0.04 bottom=0.36  → 占 60% (≈204px)
    //   MACD:      top=0.64 bottom=0.42  → 占 22% (≈75px)
    //   RSI:       top=0.78 bottom=0.28  → 占 14% (≈48px)
    //   Vol:       top=0.86 bottom=0.04  → 占 10% (≈34px)
    //   顶部留 4% 给标题/异常标注（与原版一致）。
    // v170+ 调整 (用户 2026-10-03)：把 MACD 移到交易量底下，Vol 提到 MACD 旧槽，RSI 居中。
    //   主K线+MA: top=0.04 bottom=0.42  → 占 54%
    //   Vol:      top=0.62 bottom=0.26  → 占 12%（原 MACD 槽，柱状图挪上来）
    //   RSI:      top=0.78 bottom=0.14  → 占 8%（略压，给底部让位）
    //   MACD:     top=0.90 bottom=0.02  → 占 8%（位于底部，独立条形高度）
    // v172 再紧凑 (用户 2026-10-03)：Vol/MACD 进一步压扁，整体下移，给 K 线再让 6% 高度。
    //   主K线+MA: top=0.18 bottom=0.24  → 占 58% (v275: 加大 top 给 aboveBar marker 堆叠空间)
//   Vol:      top=0.78 bottom=0.16  → 占 6%（gap=2%）
//   RSI:      top=0.86 bottom=0.09  → 占 5%（gap=2%）
//   MACD:     top=0.93 bottom=0.03  → 占 4%（gap=2%，最瘦）
//   校验: 18(top) + 58(K) + 2 + 6(V) + 2 + 5(R) + 2 + 4(M) + 3(bot) = 100%
//   v279: top 0.18 → 0.32 / bottom 0.24 → 0.30 给 HTML overlay label 阶梯堆叠留空间
//         (用户: 「再上下交错一下」 — row×26/22px 阶梯, row=4 时需要 130px 上下空间)
    candle.priceScale().applyOptions({ scaleMargins: { top: 0.32, bottom: 0.30 } })
    // 交易量：紧贴 K 线下方（gap=2%，柱状占 6%）
    const volScaleId = 'volume'
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: volScaleId,
      priceFormat: { type: 'volume' },
      priceLineVisible: false,
      lastValueVisible: false,
    })
    chart.priceScale(volScaleId).applyOptions({ scaleMargins: { top: 0.78, bottom: 0.16 } })
    // 均线 MA5 / MA10 / MA20
    const maSeries = MA_PERIODS.map((_, i) =>
      chart.addSeries(LineSeries, {
        color: MA_COLORS[i],
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      }),
    )
    // RSI(14) 子图：独立的右侧价标 (rsiScaleId)，scaleMargins 把价格区间挤到底部 18%
    const rsiScaleId = 'rsi'
    const rsi = chart.addSeries(LineSeries, {
      color: '#a78bfa',
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: true,
      priceScaleId: rsiScaleId,
      title: 'RSI(14)',
    })
    chart.priceScale(rsiScaleId).applyOptions({
      scaleMargins: { top: 0.86, bottom: 0.09 },
      borderColor: palette.border,
      autoScale: false,
      // RSI 固定 0-100；70/30 用 priceLine 标阈值
    })
    // RSI 阈值线（70 超买 / 30 超卖）
    rsi.createPriceLine({
      price: 70,
      color: 'rgba(239, 68, 68, 0.55)',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: '70',
    })
    rsi.createPriceLine({
      price: 30,
      color: 'rgba(34, 197, 94, 0.55)',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: '30',
    })
    // MACD 子图：DIF/DEA 双线 + HIST 柱；独立价标 (macdScaleId)，autoScale 让柱体撑满底部
    const macdScaleId = 'macd'
    const macdDif = chart.addSeries(LineSeries, {
      color: MACD_COLORS.dif,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceScaleId: macdScaleId,
      title: 'DIF',
    })
    const macdDea = chart.addSeries(LineSeries, {
      color: MACD_COLORS.dea,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceScaleId: macdScaleId,
      title: 'DEA',
    })
    const macdHist = chart.addSeries(HistogramSeries, {
      priceScaleId: macdScaleId,
      priceFormat: { type: 'price', precision: 6, minMove: 0.000001 },
      priceLineVisible: false,
      lastValueVisible: false,
    })
    chart.priceScale(macdScaleId).applyOptions({
      scaleMargins: { top: 0.93, bottom: 0.03 },
      borderColor: palette.border,
      autoScale: true,
    })
    // 零轴线（多空分界）
    macdDif.createPriceLine({
      price: 0,
      color: 'rgba(148, 163, 184, 0.6)',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: '0',
    })
    chartRef.current = chart
    candleRef.current = candle
    volumeRef.current = volume
    maRefs.current = maSeries
    rsiRef.current = rsi
    macdDifRef.current = macdDif
    macdDeaRef.current = macdDea
    macdHistRef.current = macdHist
    setChartReady(true)
    return () => {
      markersRef.current?.detach()
      markersRef.current = null
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      volumeRef.current = null
      maRefs.current = []
      rsiRef.current = null
      macdDifRef.current = null
      macdDeaRef.current = null
      macdHistRef.current = null
      setChartReady(false)
    }
  }, [isDark])

  // 选中品种 → 拉走势 + 标注事件
  useEffect(() => {
    if (!selected || !chartReady || !candleRef.current || !chartRef.current) return
    const item = (items ?? []).find((i) => i.asset === selected)
    let alive = true
    setMissing('')
    // 分钟线窗口：3m/5m/10m 取 144 根（约 7h/12h/24h），长周期沿用默认 96 根
    // v220：30d 走 4h × 180 = 30 天；其他按时段对应 limit
    const limit = TF_TO_BAR_LIMIT[timeframe] ?? 144
    const interval = TF_TO_INTERVAL[timeframe] ?? timeframe
    fetch(`${API_BASE}/market/history/${selected}?interval=${interval}&limit=${limit}`, { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(async (d: HistoryResp) => {
        if (!alive || !candleRef.current || !chartRef.current) return
        const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
        const bars = [...(d.bars ?? [])].sort((a, b) => a.time - b.time)
        setMissing(d.missing || (bars.length ? '' : '该品种暂无历史 K 线数据'))
        candleRef.current.setData(
          bars.map((b) => ({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })),
        )
        // 交易量柱状（涨绿跌红，独立底部价标）
        if (volumeRef.current) {
          volumeRef.current.setData(
            bars.map((b) => ({
              time: b.time as Time,
              value: typeof b.volume === 'number' ? b.volume : 0,
              color: (b.close >= b.open ? palette.up : palette.down) + 'aa',
            })),
          )
        }
                const closes = bars.map((b) => b.close)
        maRefs.current.forEach((s, i) => {
          if (!s) return
          const ma = sma(closes, MA_PERIODS[i])
          s.setData(
            bars
              .map((b, k) => ({ time: b.time as Time, value: ma[k] }))
              .filter((p): p is { time: Time; value: number } => typeof p.value === 'number'),
          )
        })
        // RSI(14) Wilder：单独 pane 上 0-100，70/30 阈值线已建
        if (rsiRef.current) {
          const rsiArr = rsi(closes)
          rsiRef.current.setData(
            bars
              .map((b, k) => ({ time: b.time as Time, value: rsiArr[k] }))
              .filter((p): p is { time: Time; value: number } => typeof p.value === 'number'),
          )
        }
        // MACD(12,26,9)：DIF/DEA 线 + HIST 柱（涨绿跌红），零轴线已建
        if (macdDifRef.current && macdDeaRef.current && macdHistRef.current) {
          const { dif, dea, hist } = macd(closes)
          const mapLine = (arr: Array<number | null>) =>
            bars
              .map((b, k) => ({ time: b.time as Time, value: arr[k] }))
              .filter((p): p is { time: Time; value: number } => typeof p.value === 'number')
          macdDifRef.current.setData(mapLine(dif))
          macdDeaRef.current.setData(mapLine(dea))
          macdHistRef.current.setData(
            bars
              .map((b, k) => ({
                time: b.time as Time,
                value: hist[k] ?? 0,
                color: (hist[k] ?? 0) >= 0 ? MACD_COLORS.histUp : MACD_COLORS.histDown,
              }))
              .filter((p) => p.value !== 0),
          )
        }

        markersRef.current?.detach()
        markersRef.current = null
        const events = item?.events ?? []
        const abnormals = item?.abnormals ?? []
        const driverTs = new Set((item?.drivers?.drivers ?? []).map((d) => d.ts))

        const marks: any[] = []
        const t0 = bars[0]?.time ?? 0
        const t1 = bars[bars.length - 1]?.time ?? 0

        // v216 归因页 K 线三行分布标注 — 把不同来源的 marker 分到三个垂直槽位避免文字堆叠
        //   aboveBar (柱上方):  ★3+ 日历 (蓝字) / impact 高优新闻 (红字) — 二选一，按优先级保留
        //   belowBar (柱下方):  news 新闻 (红字) / abnormal 异动圆点 — 二选一，次级截短
        //   inBar    (柱内中部): trading-signals 绿点 (体积小，不占垂直空间)
        //   日历 ★<3 不再渲染 (用户：只保留重要度 3 颗星以上的日历)
        //   marker 文字统一截短至 16 字符以内，避免水平溢出
        type ImpactRow = {
          event_source: string
          event_id: string
          event_title: string
          event_time: number
          asset: string
          impact_pct: number
          impact_severity: string
          direction: string
        }
        type SignalRow = {
          id: number
          news_time: number | null
          asset: string
          action: string
          score: number | null
          reasoning: string | null
          created_at: string | null
        }
        const horizonH = Math.max(24, Math.ceil((t1 - t0) / 3600) + 12)
        const calP = fetch(
          `${API_BASE}/v189/calendar/events-for-asset?asset=${encodeURIComponent(selected)}&hours=${horizonH}&star_min=3`,
          { cache: 'no-store' },
        ).then((r) => (r.ok ? r.json() : null)).catch(() => null)
        // v275: 蓝色事件 K 线影响验证
        const calImpactP = fetch(
          `${API_BASE}/v275/calendar/impact-by-asset?asset=${encodeURIComponent(selected)}&hours=${horizonH}&star_min=3&window_min=30`,
          { cache: 'no-store' },
        ).then((r) => (r.ok ? r.json() : null)).catch(() => null)
        const impactP = fetch(
          `${API_BASE}/v201/impact/by-asset?asset=${encodeURIComponent(selected)}&from_ts=${t0 - 3600}&to_ts=${t1 + 3600}&severity_min=low&limit=200`,
          { cache: 'no-store' },
        ).then((r) => (r.ok ? r.json() : null)).catch(() => null)
        const signalP = fetch(
          // v221.3: limit 20→500（30 天窗口下覆盖密度），新增 from_ts/to_ts 时间窗
          //         hide_neutral=1 默认过滤 HOLD+score=0（无方向噪声）
          `${API_BASE}/trading/signals?asset=${encodeURIComponent(selected)}&limit=500&from_ts=${t0 - 3600}&to_ts=${t1 + 3600}&hide_neutral=1`,
          { cache: 'no-store' },
        ).then((r) => (r.ok ? r.json() : null)).catch(() => null)
        // v221.3: 拐点图层（swing/cum4 + 事前氛围核实）
        const swingP = fetch(
          `${API_BASE}/abnormal/swing-points?asset=${encodeURIComponent(selected)}&timeframe=${encodeURIComponent(timeframe === '10m' ? '10m' : '3m')}&pre_window_hours=4`,
          { cache: 'no-store' },
        ).then((r) => (r.ok ? r.json() : null)).catch(() => null)
        const [calRes, calImpactRes, impactRes, signalRes, swingRes] = await Promise.all([calP, calImpactP, impactP, signalP, swingP])

        // v275: 把 K 线影响验证结果按 ts_bj 索引
        const calImpactMap: Record<number, any> = {}
        for (const ev of (calImpactRes?.events ?? [])) {
          const ts = Number(ev?.ts_bj ?? 0)
          if (ts > 0) calImpactMap[ts] = ev
        }
        setCalImpact(calImpactMap)

        // 日历 marker（v216：仅 ★3+，统一蓝色，文字截短）
        const calEvents: CalEvent[] = calRes?.events ?? []
        // v220：把 markers 数据快照推到 state（用户：「保存归因页所有生成的数据到本地作为知识图谱」）
        // v278：与现有 markersData 合并（不覆盖）— 保留之前看到的更早期 marker，
        //       bars 用当前 timeframe 的新 bars（lightweight-charts 不支持混合时间粒度）
        const newCalendar = calEvents
        const newImpact = impactRes?.items ?? []
        const newSignals = Array.isArray(signalRes) ? signalRes : []
        const newAbnormals = abnormals
        const newSwing = Array.isArray(swingRes) ? swingRes : []
        setMarkersData((cur) => ({
          calendar: mergeUnique(
            cur.calendar ?? [],
            newCalendar,
            (e: any) => `${e?.ts_bj ?? 0}:${(e?.title ?? '').slice(0, 40)}`,
          ),
          impact: mergeUnique(
            cur.impact ?? [],
            newImpact,
            (e: any) => `${e?.event_source ?? ''}:${e?.event_id ?? ''}:${e?.event_time ?? 0}`,
          ),
          signals: mergeUnique(
            cur.signals ?? [],
            newSignals,
            (e: any) => `${e?.id ?? 0}:${e?.news_time ?? ''}:${e?.action ?? ''}`,
          ),
          // bars 用新 bars（必须匹配当前 timeframe 粒度）
          bars,
          abnormals: mergeUnique(
            cur.abnormals ?? [],
            newAbnormals,
            (e: any) => `${e?.ts ?? 0}`,
          ),
          swingPoints: mergeUnique(
            cur.swingPoints ?? [],
            newSwing,
            (e: any) => `${e?.ts ?? e?.time ?? 0}:${e?.kind ?? e?.type ?? ''}`,
          ),
        }))
        for (const ce of calEvents) {
          if (ce.star < 3) continue  // ★3+ 才显示 (用户：低星日历太杂，去掉)
          if (ce.ts_bj < t0 - 3600 || ce.ts_bj > t1 + 3600) continue
          const at = alignTime(ce.ts_bj, bars)
          const starTxt = '★'.repeat(Math.min(5, ce.star))
          let txt = `${starTxt} ${titleText(ce.title)}`
          if (ce.is_released && ce.actual) {
            // 已公布 → 加实际值；超长时只保留标题
            const surp = ` (${ce.actual})${ce.surprise && parseFloat(ce.surprise) !== 0 ? '★' + ce.surprise : ''}`
            txt = `${starTxt} ${titleText(ce.title)}${surp}`
          }
          // v275: 蓝色事件 K 线影响徽章（仅在 has_data=true 时追加）
          const ki = calImpactMap[Number(ce.ts_bj)]?.kline_impact
          if (ki && ki.has_data) {
            const v = ki.verdict
            const pct = Math.abs(ki.impact_pct ?? 0)
            const arrow = v === 'follow' ? '↑' : v === 'reverse' ? '↓' : v === 'no_reaction' ? '·' : '~'
            const colorTag = v === 'follow' ? '🟢' : v === 'reverse' ? '🔴' : v === 'no_reaction' ? '⚪' : '🟡'
            txt += ` ${colorTag}${arrow}${pct.toFixed(2)}%`
          }
          marks.push({
            time: at as Time,
            position: 'aboveBar' as const,
            color: '#3b82f6',
            shape: ce.is_released ? ('square' as const) : ('circle' as const),
            text: txt.slice(0, 24),  // v275: 加徽章后允许稍长（24 字）
            _pri: 4,
            _slot: 'aboveBar',
            _src: 'calendar',
            _group: 'blue' as const,  // v220：图例颜色分组 — 蓝字线索
            _verdict: ki?.verdict,     // v275: 给面板/hover 用
          })
        }

        // v216 impact 红色新闻 marker — 来自 event_kline_impact (worker 已算好 event_title)
        // 槽位: aboveBar (顶部) — 与 ★3+ 日历冲突时降级 (slot 仲裁时丢弃)
        const impactItems: ImpactRow[] = impactRes?.items ?? []
        const seenNewsKeys = new Set<string>()
        for (const it of impactItems) {
          if (it.event_time < t0 - 3600 || it.event_time > t1 + 3600) continue
          const at = alignTime(it.event_time, bars)
          const ttl = titleText(it.event_title)
          if (!ttl) continue
          // 同 K 线桶去重 (避免 worker 多次写入同一事件的重复标题)
          const dedupKey = `${at}::${ttl}`
          if (seenNewsKeys.has(dedupKey)) continue
          seenNewsKeys.add(dedupKey)
          marks.push({
            time: at as Time,
            position: 'aboveBar' as const,
            color: '#ef4444',
            shape: 'circle',
            text: ttl.slice(0, 16),  // 截短
            _pri: it.impact_severity === 'high' ? 4 : (it.impact_severity === 'medium' ? 3 : 1),
            _slot: 'aboveBar',
            _src: 'impact',
            _group: 'red' as const,  // v220：图例颜色分组 — 红字线索
          })
        }

        // v216 绿色交易信号 — 来自 ai_decisions (首页交易信号)
        // 槽位: inBar (柱内中部) — 与柱顶/柱底文字互不干扰 (用户："在首页我们截取进来的交易信号显示在这个K线上用绿色点")
        const signals: SignalRow[] = Array.isArray(signalRes) ? signalRes : []
        const seenSignalKeys = new Set<string>()
        for (const s of signals) {
          const tsRaw = s.news_time ?? null
          if (tsRaw === null || tsRaw === undefined) continue
          let tsNum = 0
          if (typeof tsRaw === 'number') tsNum = tsRaw
          else if (typeof tsRaw === 'string') {
            // raw_news.timestamp 是 ISO 串 ('2026-08-31T07:00:00+08:00')
            const ms = Date.parse(tsRaw)
            tsNum = isNaN(ms) ? 0 : Math.floor(ms / 1000)
          }
          if (!tsNum) continue
          if (tsNum < t0 - 3600 || tsNum > t1 + 3600) continue
          const at = alignTime(tsNum, bars)
          // 同 K 线桶去重
          const dedupKey = `${at}::${s.action}`
          if (seenSignalKeys.has(dedupKey)) continue
          seenSignalKeys.add(dedupKey)
          // v221.3: 绿点加大可读性 — 圆点 + 大写字母 B/S + 实心绿色
          //   原 circle + '•' 字太小，截图看不见。
          //   改用 arrowUp/arrowDown + 'B'/'S' 文本，方向明确 + 尺寸足够。
          const isBuy = s.action === 'BUY'
          marks.push({
            time: at as Time,
            position: 'inBar' as const,
            color: isBuy ? '#22c55e' : '#ef4444',  // 绿/红按方向区分（涨绿跌红）
            // v221.3: 改 arrowUp/arrowDown + 'B'/'S'（圆点+• 看不见，改方向箭头+字母）
            shape: isBuy ? ('arrowUp' as const) : ('arrowDown' as const),
            text: isBuy ? 'B' : 'S',  // B=Buy / S=Sell，方向一眼可见
            _pri: 1,
            _slot: 'inBar',
            _src: 'signal',
            _group: 'green' as const,  // v220：图例颜色分组 — 绿点线索（保留兼容性，图例不变）
            _signal_action: s.action,
            _signal_score: s.score,  // v221.3: 保留 score 用于 hover 显示
          })
        }

        if (bars.length) {
          // v232: 新闻事件按 K 线聚类，按动作色（BUY=绿/SELL=红）错开显示
          //   - BUY  → aboveBar (柱顶, 绿色↑)   — 与 SELL/异动 完全错开
          //   - SELL → belowBar (柱底, 红色↓)   — 与 BUY/calendar 完全错开
          //   - HOLD → 不在 K 线上显示（v20251014：用户反馈灰点无价值；改在「关联新闻时间线」列表查看）
          //   同 action 多事件同 K 线：lw-charts 自动垂直堆叠，避免横向重叠
          const byTime = new Map<number, MoverEvent[]>()
          for (const e of events) {
            if (e.ts < t0 - 3600 || e.ts > t1 + 3600) continue
            const at = alignTime(e.ts, bars)
            const arr = byTime.get(at) ?? []
            arr.push(e)
            byTime.set(at, arr)
          }
          for (const [at, evs] of [...byTime.entries()].sort((a, b) => a[0] - b[0])) {
            // 按动作分桶；driver 事件排到队首（视觉优先）
            const buyEvs = evs
              .filter((e) => e.action === 'BUY')
              .sort((a, b) => Math.abs(b.score) - Math.abs(a.score))
            const sellEvs = evs
              .filter((e) => e.action === 'SELL')
              .sort((a, b) => Math.abs(b.score) - Math.abs(a.score))
            // v20251014: holdCount 移除 — HOLD/中性新闻不再在 K 线上显示（用户反馈：灰点无价值）
            //   HOLD 新闻保留在「关联新闻时间线」列表（line 2700+）中查看
            const driverEv = evs.find((e) => driverTs.has(e.ts))

            // 把 driver（如果存在）提到队首
            const promote = (arr: MoverEvent[]) => {
              if (!driverEv) return arr
              const idx = arr.indexOf(driverEv)
              if (idx <= 0) return arr
              const copy = arr.slice()
              copy.splice(idx, 1)
              copy.unshift(driverEv)
              return copy
            }

            const buyTop = promote(buyEvs).slice(0, 2)   // 同 K 线最多 2 条同方向
            const sellTop = promote(sellEvs).slice(0, 2)

            // BUY → aboveBar（绿色 ↑，柱顶）
            buyTop.forEach((e, i) => {
              // v20251014: 6 维综合 signal_weight → 颜色饱和度（强=实心, 弱=透明）
              //   tier: strong >0.7 / medium 0.3-0.7 / weak 0.1-0.3 / ignored <0.1
              //   cold_signal: 市场不反应 → 降级为半透明
              const w = (e as any).signal_weight as number | undefined
              const tier = (e as any).weight_tier as string | undefined
              const cold = (e as any).cold_signal as boolean | undefined
              const buyColor = cold
                ? '#86efac'                            // 冷信号 → 浅绿
                : tier === 'strong'
                  ? '#15803d'                          // 强 → 深绿
                  : tier === 'weak' || tier === 'ignored'
                    ? '#86efac'                        // 弱 → 浅绿
                    : '#22c55e'                        // 默认 / medium
              marks.push({
                time: at as Time,
                position: 'aboveBar' as const,
                color: buyColor,                       // 按 weight 调饱和度
                shape: 'arrowUp' as const,
                text: titleText(e.content).slice(0, 10),  // 截短到 10 字
                _pri: 2 + (buyTop.length - 1 - i) * 0.1,  // driver 排前
                _slot: 'aboveBar',
                _src: 'news',
                _group: 'green' as const,                 // 绿字线索 = 看涨新闻
                _news_action: 'BUY',
                _signal_weight: w ?? null,                // 前端悬浮窗用
                _weight_tier: tier ?? null,
                _cold_signal: cold ?? false,
              })
            })

            // SELL → belowBar（红色 ↓，柱底）
            sellTop.forEach((e, i) => {
              const w = (e as any).signal_weight as number | undefined
              const tier = (e as any).weight_tier as string | undefined
              const cold = (e as any).cold_signal as boolean | undefined
              const sellColor = cold
                ? '#fca5a5'                            // 冷信号 → 浅红
                : tier === 'strong'
                  ? '#991b1b'                          // 强 → 深红
                  : tier === 'weak' || tier === 'ignored'
                    ? '#fca5a5'                        // 弱 → 浅红
                    : '#ef4444'                        // 默认 / medium
              marks.push({
                time: at as Time,
                position: 'belowBar' as const,
                color: sellColor,
                shape: 'arrowDown' as const,
                text: titleText(e.content).slice(0, 10),
                _pri: 2 + (sellTop.length - 1 - i) * 0.1,
                _slot: 'belowBar',
                _src: 'news',
                _group: 'red' as const,                   // 红字线索 = 看跌新闻
                _news_action: 'SELL',
                _signal_weight: w ?? null,
                _weight_tier: tier ?? null,
                _cold_signal: cold ?? false,
              })
            })

            // v20251014: HOLD/中性新闻不在 K 线上显示（用户多次反馈：灰点无价值、无法识别内容）
            //   · 原逻辑：聚合显示一个无名灰点（color=palette.flat, shape=circle, text=''）
            //   · 移除原因：HOLD 事件没有方向性，对交易决策无指导价值；用户已能从「关联新闻时间线」列表查看全部
            //   · 影响：仅在 K 线上不再看到 HOLD 聚合灰点；BUY/SELL 新闻标题、★3+ 日历、异动反推均保留
            //   · 列表展示：见下方「关联新闻时间线（近 30 天 · {currentEvents.length} 条）」section
          }
          // 3 分钟异动反推星标（彩色实心圆点，涨绿/跌红，belowBar 与事件标注错开）
          for (const a of abnormals) {
            if (a.ts < t0 - 3600 || a.ts > t1 + 3600) continue
            const at = alignTime(a.ts, bars) as Time
            // v220：异动 marker 按涨跌方向归到对应颜色组（涨→绿，跌→红；中性无方向→不归组，始终可见）
            const abGroup: 'red' | 'green' | undefined =
              a.pct_change == null ? undefined : a.pct_change >= 0 ? 'green' : 'red'
            marks.push({
              time: at,
              position: 'belowBar' as const,
              color: abnormalColor(a, palette),
              shape: 'circle' as const,
              text: abnormalLabel(a),
              _pri: 0,
              _slot: 'belowBar',
              _src: 'abnormal',
              _group: abGroup,
            })
            // v221.3：拐点 marker（swing/cum4 专属）— 在 aboveBar 顶层画大三角
            //   已预期(label=expected) → 蓝色三角（市场已铺垫）
            //   凭空突袭(label=surprise) → 紫色三角（无预警）
            //   用 _src='swing_point' 与 _swing_atmosphere 区分 hover 信息
            if (a.trigger_type === 'swing' || a.trigger_type === 'cum4') {
              const swing = (Array.isArray(swingRes) ? swingRes : []).find(
                (sp: any) => Math.abs(sp.ts - a.ts) < 300,
              )
              const atmos = swing?.atmosphere || { atmosphere_score: 0, label: 'surprise' }
              const isExpected = atmos.label === 'expected'
              marks.push({
                time: at,
                position: 'aboveBar' as const,
                color: isExpected ? '#3b82f6' : '#a855f7',  // 蓝=预期 / 紫=突袭
                shape: 'arrowDown' as const,
                text: isExpected ? '预期' : '突袭',
                _pri: 5,  // 高优先级（与 ★3+ 日历同级）
                _slot: 'aboveBar',
                _src: 'swing_point',
                _group: 'blue' as const,  // 归到蓝色图例组（与日历同组，复用图例切换）
                _swing_atmosphere: atmos,
                _swing_direction: a.pct_change != null && a.pct_change >= 0 ? 'bullish' : 'bearish',
                _swing_trigger_type: a.trigger_type,
              })
            }
            // v216: 异常附近 (±30min) 的事件新闻改用 inBar 红色文字（避免和顶部 ★3+ 日历冲突）
            const nearby = impactItems.find((it) => Math.abs(it.event_time - a.ts) <= 1800)
            if (nearby && nearby.event_title) {
              const ttl = titleText(nearby.event_title)
              if (ttl) {
                marks.push({
                  time: at,
                  position: 'inBar' as const,
                  color: '#ef4444',
                  shape: 'circle',
                  text: ttl.slice(0, 12),  // 截短
                  _pri: nearby.impact_severity === 'high' ? 4 : 3,
                  _slot: 'inBar',
                  _src: 'abnormal_news',
                  _group: 'red' as const,  // v220：异常附近新闻 = 红字线索
                })
              }
            }
          }
        }
        // v172 槽位仲裁简化（用户 2026-10-03 反馈）：
        //   原 v216 逻辑「同 K 线同槽 → 丢弃低优」导致 红/绿事件被同槽日历 / 灰点挤掉
        //   新规则：每个 marker 按 _pri DESC + 同 _pri 插入顺序 排序后全部保留
        //   同 K 线 / 同槽允许多 marker（lightweight-charts 自动垂直堆叠，不重叠）
        //   仅 inBar 文本截到 8 字符防水平重叠；aboveBar/belowBar 保留 16 字
        const byBar = new Map<number, any[]>()
        for (const m of marks) {
          const t = (m.time as any)?.timestamp ?? m.time
          const arr = byBar.get(t) ?? []
          arr.push(m)
          byBar.set(t, arr)
        }
        const arbitrated: any[] = []
        for (const [, group] of byBar) {
          // _pri DESC；同 _pri 保持插入顺序（Array.sort 稳定排序 → 用户："插入顺序全部显示"）
          group.sort((a, b) => (b._pri ?? 0) - (a._pri ?? 0))
          for (const m of group) {
            if (m._slot === 'inBar' && m.text && m.text.length > 8) m.text = m.text.slice(0, 8)
            m.position = m._slot
            arbitrated.push(m)
          }
        }
        // v172 去掉 stagger 抢槽逻辑（用户反馈："只是排序显示的界面问题"）
        //   相邻 K 线 marker 也保留，依赖 lw-charts 自动垂直堆叠
        //   仅按 _pri DESC + 同 _pri 时间 ASC 排序（保证视觉层次）
        const staggered = [...arbitrated].sort((a, b) => {
          const dp = (b._pri ?? 0) - (a._pri ?? 0)
          if (dp !== 0) return dp
          // 同 _pri：按时间 ASC（视觉从左到右）
          const ta = (a.time as any)?.timestamp ?? a.time
          const tb = (b.time as any)?.timestamp ?? b.time
          return ta - tb
        })
        // v172 上限从 80 → 120（用户："全部显示"——避免低优 marker 被总名额挤掉）
        const MAX_MARKS = 120
        const sortedMarks = [...staggered].sort((a, b) => (b._pri ?? 0) - (a._pri ?? 0))
        // v220：按图例颜色过滤 — 用户点击图例关闭某色后立即隐藏对应 marker。
        // v20251014：HOLD 灰点已从 K 线上移除（见 1944 行），这里「_group 未设置」现在仅指
        //   异动中性圆点（无 _group 但 text=''）和未来可能新增的无分组 marker，均始终保留。
        const filteredMarks = sortedMarks.filter((m: any) => {
          const g = m._group
          if (g === 'blue' && !clueFilters.blue) return false
          if (g === 'red' && !clueFilters.red) return false
          if (g === 'green' && !clueFilters.green) return false
          return true
        })

        // v279：构建 HTML overlay 文字层
        //   lw-charts 自带文字水平渲染，多 marker 同 X 时重叠堆在一起
        //   这里把每条 marker 的 text + 颜色 + 槽位抽离到 overlay，按 (barTime, slot) 分组计算 row
        //   同组 row 0/1/2 交错向上推 14px + 左右 ±6px zigzag 形成上下交错
        const barByTime = new Map<number, { high: number; low: number; close: number }>()
        for (const b of bars) barByTime.set(b.time, { high: b.high, low: b.low, close: b.close })
        // 用最近邻 K 线（时间最近）兜底
        const findBar = (ts: number) => {
          if (barByTime.has(ts)) return barByTime.get(ts)!
          let bestDiff = Infinity, best: any = null
          for (const b of bars) {
            const d = Math.abs(b.time - ts)
            if (d < bestDiff) { bestDiff = d; best = b }
          }
          return best ? { high: best.high, low: best.low, close: best.close } : { high: 0, low: 0, close: 0 }
        }
        // 按 (barTime, slot) 计数 → 算 row
        const slotCounter = new Map<string, number>()
        const slotSideToggle = new Map<string, number>()
        const overlayLabels: OverlayLabel[] = []
        for (const m of filteredMarks.slice(0, MAX_MARKS)) {
          if (!m.text) continue  // 无文字（如异常圆点）跳过
          const slot = m.position === 'aboveBar' ? 'above' : m.position === 'belowBar' ? 'below' : 'inBar'
          const mts = (m.time as any)?.timestamp ?? (m.time as any)
          const bar = findBar(mts)
          const key = `${mts}:${slot}`
          const row = slotCounter.get(key) ?? 0
          slotCounter.set(key, row + 1)
          const sideN = slotSideToggle.get(key) ?? 0
          slotSideToggle.set(key, sideN + 1)
          const side: -1 | 1 = sideN % 2 === 0 ? -1 : 1
          // 颜色：m.color 直接用（lw-charts 已设好的色），无背景框，靠 textShadow + 粗体确保可读
          const color = m.color as string
          const text = String(m.text).slice(0, 14)
          overlayLabels.push({
            key: `${key}:${m.text}:${row}`,
            text,
            color,
            slot: slot as any,
            barTime: mts,
            row,
            side,
          })
        }
        overlayLabelsRef.current = overlayLabels
        // 立刻触发一次重算（marker 刚构建完，X/Y 可算）
        recomputeOverlay()
        setOverlayRev((v) => v + 1)

        // createSeriesMarkers 不读 _pri/_slot/_src/_group/_signal_action/_signal_score/_swing_*/_news_action — 剥掉私有字段
        // v279：再剥掉 text（交给 HTML overlay 渲染，避免 lw-charts 内置水平文字堆叠重叠）
        const cleaned = filteredMarks.slice(0, MAX_MARKS).map(({
          _pri, _slot, _src, _group, _signal_action, _signal_score,
          _swing_atmosphere, _swing_direction, _swing_trigger_type,
          _news_action, _verdict,
          text, _textLen, ...m
        }: any) => m)
        if (cleaned.length && candleRef.current) {
          markersRef.current = createSeriesMarkers(candleRef.current, cleaned)
        }
        // v288: 不再随 marker 数放大 chartHeight —
        //   标签已在 3 条固定横排（v287），markers 已被剥 text 用 HTML overlay 渲染（v279），
        //   chart 高度与 marker 数量无关。固定 440px（比 480 还小 40px），柱子不再被「放大」。
        const target = 440
        setChartHeight(target)
        window.requestAnimationFrame(() => chartRef.current?.timeScale().fitContent())
      })
      .catch(() => { if (alive) setMissing('走势数据加载失败') })
    return () => { alive = false }
  }, [selected, chartReady, items, isDark, timeframe, clueFilters])

  // v280 — Pivot Point 7 道价格线 + Header 徽章信息
  //   依赖 markersData.bars + timeframe；toggle 关时彻底从主图 remove（不占 GPU）
  useEffect(() => {
    if (!chartReady || !candleRef.current) return
    const c = candleRef.current
    const bars = markersData.bars
    // 1) 清理旧 PriceLine
    for (const k of Object.keys(pivotLinesRef.current)) {
      const ln = pivotLinesRef.current[k]
      if (ln) {
        try { c.removePriceLine(ln) } catch {}
        pivotLinesRef.current[k] = null
      }
    }
    // 2) showPivot 关闭 → 完事
    if (!showPivot) {
      setPivotInfo(null)
      return
    }
    // 3) bars 不够 → 不算
    if (!bars.length) return
    const { period, label } = pivotPeriodForTimeframe(timeframe)
    const pp = pivotPoint(bars, period)
    if (!pp) {
      setPivotInfo(null)
      return
    }
    setPivotInfo({
      p: pp.p, r1: pp.r1, r2: pp.r2, r3: pp.r3,
      s1: pp.s1, s2: pp.s2, s3: pp.s3,
      period, label,
    })
    const levels: { k: string; v: number; color: string; title: string; lineStyle?: number; lineWidth?: number }[] = [
      { k: 'R3', v: pp.r3, color: COLOR_PIVOT_R3, title: 'R3 阻力',    lineWidth: 1 },
      { k: 'R2', v: pp.r2, color: COLOR_PIVOT_R2, title: 'R2 阻力',    lineStyle: 2, lineWidth: 1 },
      { k: 'R1', v: pp.r1, color: COLOR_PIVOT_R1, title: 'R1 阻力',    lineStyle: 2, lineWidth: 1 },
      { k: 'P',  v: pp.p,  color: COLOR_PIVOT_P,  title: 'Pivot 中线', lineWidth: 1 },
      { k: 'S1', v: pp.s1, color: COLOR_PIVOT_S1, title: 'S1 支撑',    lineStyle: 2, lineWidth: 1 },
      { k: 'S2', v: pp.s2, color: COLOR_PIVOT_S2, title: 'S2 支撑',    lineStyle: 2, lineWidth: 1 },
      { k: 'S3', v: pp.s3, color: COLOR_PIVOT_S3, title: 'S3 支撑',    lineWidth: 1 },
    ]
    for (const lv of levels) {
      try {
        const ln = c.createPriceLine({
          price: lv.v,
          color: lv.color,
          lineWidth: (lv.lineWidth ?? 1) as any,
          lineStyle: (lv.lineStyle ?? 0) as any,
          axisLabelVisible: true,
          title: `${lv.title} ${lv.v}`,
        })
        pivotLinesRef.current[lv.k] = ln
      } catch {}
    }
  }, [chartReady, showPivot, timeframe, markersData.bars])

  // v280 — 实时订单簿深度轮询（showBook=true 时每 10s 拉一次；后端缓存 10s）
  useEffect(() => {
    if (!showBook || !selected) {
      setOrderbook(null)
      setOrderbookStatus('idle')
      return
    }
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    const load = async () => {
      setOrderbookStatus('loading')
      try {
        // v2026.10.15 bugfix: API_BASE 已经包含 /api 前缀, 不要再加一次
        // 之前写成 /api/market/orderbook → 实际请求 /api/api/market/orderbook → 404
        // 后端真实路径是 /api/market/depth/{asset} (与 live-kline-chart.tsx:1465 同口径)
        const r = await fetch(`${API_BASE}/market/depth/${selected}`)
        if (!alive) return
        if (!r.ok) {
          setOrderbookStatus('unavailable')
          setTimeout(pick, 15000)
          return
        }
        const j = await r.json()
        if (!alive) return
        if (j?.bids && j?.asks) {
          setOrderbook({ bids: j.bids, asks: j.asks, ts_ms: Date.now() })
          setOrderbookStatus('ok')
        } else {
          setOrderbookStatus('unavailable')
        }
      } catch {
        if (!alive) return
        setOrderbookStatus('unavailable')
      }
      timer = setTimeout(pick, 10000)
    }
    const pick = () => load()
    pick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [showBook, selected])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      {loadErr && (
        <div className="mb-4 rounded border border-short/40 bg-short/10 px-3 py-2 text-xs text-short">排行接口异常：{loadErr}</div>
      )}

      {/* ─── 重构（v188）：K线置顶 + 右侧栏上下堆叠辅助面板 ─── */}
      <div className="grid gap-4 lg:grid-cols-4">
        {/* 左侧主区（col-span-3）：K线 + 异常识别内联列表 + 事件时间线 */}
        <div className="space-y-4 lg:col-span-3">
          {/* 走势 + 事件标注（K线置顶） */}
          <section className="surface-card p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
            {selected ? `${selected}（${ASSET_META[selected] ?? ''}）走势 · 事件标注` : '走势 · 事件标注'}
          </h3>
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-0.5 rounded border border-border bg-card p-0.5">
              {(['3m', '5m', '10m', '1h', '4h', '30d'] as const).map((tf) => (
                <button
                  key={tf}
                  type="button"
                  onClick={() => setTimeframe(tf)}
                  className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
                    timeframe === tf ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-secondary'
                  }`}
                >
                  {tf}
                </button>
              ))}
            </div>
            {/* v170: 10m 切换 → 一键重算 abnormal_signals（10min 量能识别） */}
            <button
              type="button"
              onClick={() => {
                // v302+ P0: 用 toast.confirm 替代 window.confirm — 不阻塞 K 线轮询、不强制用户点完才能做其他事
                toast.confirm(
                  '清空 abnormal_signals（multi_factor/sector）并按 10m interval 全量重算？',
                  async () => {
                    try {
                      const r = await fetch(`${API_BASE}/admin/abnormal-signals/recompute-10m`, { method: 'POST' })
                      if (!r.ok) throw new Error(`HTTP ${r.status}`)
                      const j = await r.json()
                      toast.success(`重算完成：删除 ${j.deleted} 条，新增 ${j.inserted} 条 (触发 ${j.triggered} 个)`)
                    } catch (e) {
                      toast.error(`重算失败：${e instanceof Error ? e.message : String(e)}`)
                    }
                  },
                )
              }}
              className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 font-mono text-[10px] text-amber-700 hover:bg-amber-500/20 dark:text-amber-300"
              title="按 10m interval 全量重算 abnormal_signals（multi_factor/sector）"
            >
              ⟳ 重算 10m 异常签名
            </button>
            {/* v280 — Pivot Point 7 线 toggle（默认开） */}
            <button
              type="button"
              onClick={() => setShowPivot((v) => !v)}
              className={`rounded-full px-2 py-0.5 font-mono text-[10px] transition-colors ${
                showPivot
                  ? 'bg-orange-500/25 text-orange-700 dark:text-orange-300 ring-2 ring-orange-500/50 font-semibold'
                  : 'bg-secondary text-muted-foreground'
              }`}
              title={
                showPivot
                  ? `已开启：显示 7 道技术支撑阻力位（${pivotInfo?.label ?? '前 24h'}，橙色实线 Pivot + 绿/红虚线 R1-R3/S1-S3）—— 再点关闭`
                  : '点击开启：叠加 TradingView 同款 Pivot Point（基于前 24h K 线 H/L/C 自动算 7 道支撑阻力位）'
              }
            >
              📐 Pivot{showPivot && pivotInfo && <span className="text-[9px]">·7线</span>}
            </button>
            {/* v280 — 实时订单簿深度 toggle */}
            <button
              type="button"
              onClick={() => setShowBook((v) => !v)}
              className={`rounded-full px-2 py-0.5 font-mono text-[10px] transition-colors ${
                showBook
                  ? 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 ring-1 ring-emerald-500/40'
                  : 'bg-secondary text-muted-foreground'
              }`}
              title={
                showBook
                  ? `已开启：右侧实时显示订单簿深度（买卖盘堆积柱状图，每 10s 刷新）`
                  : '点击开启：右侧叠加实时订单簿深度（买盘绿/卖盘红堆积柱状图，识别上/下方的挂单墙）'
              }
            >
              📊 订单簿
              {showBook && orderbookStatus === 'ok' && orderbook && (
                <span className="ml-1 rounded bg-emerald-500/40 px-1 text-[9px] text-foreground">
                  {orderbook.bids.length}+{orderbook.asks.length}
                </span>
              )}
              {showBook && orderbookStatus === 'unavailable' && (
                <span className="ml-1 rounded bg-rose-500/40 px-1 text-[9px] text-foreground">无数据</span>
              )}
            </button>
          </div>
          {/* v220：本地存档 / 知识图谱导出（用户：「保存归因页所有生成的数据在本地作为知识图谱参考」） */}
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 font-mono text-[10px]">
            <button
              type="button"
              onClick={saveSnapshot}
              className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 text-primary transition-colors hover:bg-primary/20"
              title="把当前品种的 K 线 + 3 类图例 marker + 异动 + AI 归因 + 驱动摘要全部存到 localStorage（多资产独立存档）"
            >
              💾 保存到本地
            </button>
            <button
              type="button"
              onClick={downloadSnapshot}
              className="rounded border border-border bg-card px-2 py-0.5 text-foreground/80 transition-colors hover:bg-secondary"
              title="保存后下载 .json 文件（带时间戳）"
            >
              📥 导出 JSON
            </button>
            <button
              type="button"
              onClick={copySnapshot}
              className="rounded border border-border bg-card px-2 py-0.5 text-foreground/80 transition-colors hover:bg-secondary"
              title="复制 JSON 到剪贴板（适合贴进 LLM 知识图谱 prompt）"
            >
              📋 复制 JSON
            </button>
            <button
              type="button"
              onClick={() => {
                const raw = loadSnapshotRaw()
                if (!raw) {
                  alert(`未找到 ${selected} 的本地存档，请先点「💾 保存到本地」`)
                  return
                }
                const w = window.open('', '_blank', 'width=820,height=720')
                if (w) {
                  w.document.write(
                    `<title>${selected} 归因存档</title><pre style="font:12px ui-monospace,monospace;padding:16px;background:#0b1020;color:#e2e8f0;white-space:pre-wrap;word-break:break-all;">${raw.replace(/[<&>]/g, (c: string) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c as '&' | '<' | '>'] ?? c))}</pre>`,
                  )
                  w.document.close()
                }
              }}
              className="rounded border border-border bg-card px-2 py-0.5 text-foreground/80 transition-colors hover:bg-secondary"
              title="弹窗查看已存档的 JSON"
            >
              👁 查看存档
            </button>
            <button
              type="button"
              onClick={() => {
                // v302+ P0: 用 toast.confirm 替代 window.confirm
                toast.confirm(`清空 ${selected} 的本地存档？此操作不可撤销。`, () => clearSnapshot())
              }}
              className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-amber-700 transition-colors hover:bg-amber-500/20 dark:text-amber-300"
              title="清空当前品种存档（其他资产不受影响）"
            >
              🗑 清空
            </button>
            {/* v278 — 「📊 累计全量加载」按钮：强制 30d × 4h + 累计 30 天日历/impact/信号
                用户 2026-10-07 反馈：「只识别了近期，要保存所有辨别过的数据」 */}
            <button
              type="button"
              disabled={backfillBusy || !selected}
              onClick={backfillHistorical}
              className={`rounded border border-cyan-500/40 px-2 py-0.5 transition-colors ${
                backfillBusy
                  ? 'cursor-wait border-cyan-500/60 bg-cyan-500/30 text-cyan-900 dark:text-cyan-100'
                  : 'bg-cyan-500/10 text-cyan-700 hover:bg-cyan-500/20 dark:text-cyan-300'
              }`}
              title="强制按 30 天 × 4h 拉最长区间，把日历/impact/信号一次性累计到本地（合并不覆盖，永久保存）"
            >
              {backfillBusy ? '⏳ 累计中…' : '📊 累计全量加载'}
            </button>
            {backfillMsg && (
              <span className={`ml-1 inline-flex items-center gap-1 rounded px-2 py-0.5 font-mono text-[10px] ${
                backfillMsg.startsWith('❌')
                  ? 'bg-red-500/10 text-red-700 dark:text-red-300'
                  : backfillMsg.startsWith('⚠')
                  ? 'bg-amber-500/10 text-amber-700 dark:text-amber-300'
                  : 'bg-cyan-500/10 text-cyan-700 dark:text-cyan-300'
              }`}>
                {backfillMsg}
              </span>
            )}
            {snapshotAt && (
              <span className="ml-1 inline-flex items-center gap-1 rounded bg-emerald-500/10 px-2 py-0.5 text-emerald-700 dark:text-emerald-300">
                ✓ 已存：{snapshotAt}
              </span>
            )}
            {/* v278 — 累计覆盖范围：展示 markersData 涵盖的最早/最晚事件时间 */}
            {coverage && (
              <span
                className="ml-1 inline-flex items-center gap-1 rounded bg-blue-500/10 px-2 py-0.5 font-mono text-[10px] text-blue-700 dark:text-blue-300"
                title="当前 markersData 中所有事件的最早/最晚时间（受 chart timeframe 渲染范围限制）"
              >
                📐 累计：{fmtTs(coverage.earliest).slice(5, 16)} → {fmtTs(coverage.latest).slice(5, 16)}
                <span className="text-muted-foreground">({coverage.count} 事件)</span>
              </span>
            )}
            <span className="ml-1 text-muted-foreground">
              ({markersData.bars.length} bars · {markersData.calendar.length} 日历 · {markersData.impact.length} impact · {markersData.signals.length} 信号 · {markersData.abnormals.length} 异动 · {currentEvents.length} 新闻)
            </span>
          </div>
        </div>

        {/* v275: 紧凑彩色图例移到图表上方一行（不再压在 K 线上方，避免吃掉 aboveBar marker 空间） */}
        <div className="mb-2 flex flex-wrap items-center justify-end gap-x-3 gap-y-1 rounded border border-border bg-card/60 px-2.5 py-1.5 font-mono text-[10px]">
          <button
            type="button"
            onClick={() => toggleClue('green')}
            className={`flex items-center gap-1 transition-opacity hover:opacity-100 ${clueFilters.green ? 'opacity-100' : 'opacity-40 line-through'}`}
            title={clueFilters.green ? '点击隐藏绿字看涨新闻' : '点击显示绿字看涨新闻'}
          >
            <span className="inline-block h-2 w-2 shrink-0 rounded-full bg-[#22c55e]" />
            <span style={{ color: '#22c55e' }}>绿字看涨</span>
          </button>
          <button
            type="button"
            onClick={() => toggleClue('red')}
            className={`flex items-center gap-1 transition-opacity hover:opacity-100 ${clueFilters.red ? 'opacity-100' : 'opacity-40 line-through'}`}
            title={clueFilters.red ? '点击隐藏红字看跌新闻' : '点击显示红字看跌新闻'}
          >
            <span className="inline-block h-2 w-2 shrink-0 rounded-full bg-[#ef4444]" />
            <span style={{ color: '#ef4444' }}>红字看跌</span>
          </button>
          <button
            type="button"
            onClick={() => toggleClue('blue')}
            className={`flex items-center gap-1 transition-opacity hover:opacity-100 ${clueFilters.blue ? 'opacity-100' : 'opacity-40 line-through'}`}
            title={clueFilters.blue ? '点击隐藏蓝字 ★3+ 日历' : '点击显示蓝字 ★3+ 日历'}
          >
            <span className="inline-block h-2 w-2 shrink-0 rounded-full bg-[#3b82f6]" />
            <span style={{ color: '#3b82f6' }}>★3+日历</span>
          </button>
          {/* v20251014: 灰点中性图例已移除（K 线上不再显示 HOLD 聚合灰点，见 1944 行） */}
          <span className="flex items-center gap-1">
            <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-[#22c55e] ring-1 ring-[#22c55e]/50" />
            <span style={{ color: '#86efac' }}>柱内绿点B/S</span>
          </span>
          {/* v275: 影响验证图例 */}
          <span className="ml-1 border-l border-border pl-2">影响:</span>
          <span className="flex items-center gap-1" title="跟随事件方向波动">
            <span>🟢</span><span style={{ color: '#22c55e' }}>跟随</span>
          </span>
          <span className="flex items-center gap-1" title="反向事件方向波动">
            <span>🔴</span><span style={{ color: '#ef4444' }}>反向</span>
          </span>
          <span className="flex items-center gap-1" title="微弱波动 / 市场无明显反应">
            <span>🟡</span><span style={{ color: '#9ca3af' }}>微弱</span>
          </span>
          <span className="flex items-center gap-1" title="未公布 / 数据不足">
            <span>⚪</span><span style={{ color: '#9ca3af' }}>中性</span>
          </span>
        </div>

        <div
          className={`relative w-full overflow-hidden rounded border border-border bg-card transition-[height] duration-300 ${
            showPivot && pivotInfo ? 'pl-[88px]' : ''
          }`}
          style={{ height: chartHeight }}
        >
          {missing && (
            <div className="absolute inset-x-0 top-0 z-10 mx-2 mt-2 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 font-mono text-[11px] text-amber-500">
              {missing}
            </div>
          )}
          <div ref={containerRef} className="h-full w-full" />
          {/* v279: HTML overlay 文字层 — 替代 lw-charts 内置水平文字，按 (barTime, slot) 行号交错 */}
          {/* overflow-visible: 极多行堆叠时（>5 row）让 label 溢出 chart 边界不被裁 */}
          {/* v283+ 修复（用户要求）：
              · 文字色 = 事件本身颜色（lab.color），所有事件类型一致（蓝=日历 / 红=异动 / 绿=信号 / 灰=HOLD）
              · 移除 bg-card/95 + ring 实心背景框，靠 textShadow + 粗体保可读，不再挡柱体
              · 信号（inBar）标签稍微抬高（-8px）以免压住 native arrowUp/arrowDown */}
          <div className="pointer-events-none absolute inset-0 z-[5] overflow-visible">
            {overlayLabels.map((lab) => {
              const x = lab._x ?? 0
              const y = lab._y ?? 0
              return (
                <div
                  key={lab.key}
                  className="absolute whitespace-nowrap font-mono text-[9px] leading-[10px] font-semibold"
                  style={{
                    // v287: X 严格等于蜡烛 timeToCoordinate、Y 落到 3 条固定横排之一（above/inBar/below），标签 ↔ 时间点一一对应
                    left: `${x}px`,
                    top: `${y - 5}px`,
                    // 文字色 = 事件本身颜色
                    color: lab.color,
                    // 透明背景，仅靠 textShadow + 粗体保证可读（不挡 K 线柱体）
                    textShadow: isDark
                      ? '0 0 3px rgba(0,0,0,0.95), 0 0 6px rgba(0,0,0,0.7), 0 1px 2px rgba(0,0,0,0.9)'
                      : '0 0 3px rgba(255,255,255,0.95), 0 0 6px rgba(255,255,255,0.7), 0 1px 2px rgba(255,255,255,0.9)',
                    transform: 'translate(14px, 0)',  // 从图标右移 14px 起始
                  }}
                >
                  {lab.text}
                </div>
              )
            })}
          </div>

          {/* v280 — Pivot 主图左内侧位置标注（每条价格线左侧贴彩色标签） */}
          {showPivot && pivotInfo && chartReady && candleRef.current && (
            <PivotLabelsOverlay pivot={pivotInfo} series={candleRef.current} />
          )}
          {/* v280 — 实时订单簿深度热力图（右侧 90px 柱状堆积） */}
          {showBook && orderbook && chartReady && candleRef.current && (
            <OrderbookOverlay orderbook={orderbook} series={candleRef.current} />
          )}
        </div>

        {/* v234：5 类彩色事件统计（与右上角图例一一对应）— 给 K 线下方统计面板用 */}
        <div className="mt-3 rounded border border-border bg-card/40 p-3">
          <div className="mb-2 flex items-center justify-between">
            <h4 className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              🎯 彩色事件统计 · {eventStats.buy.count + eventStats.sell.count + eventStats.hold.count + eventStats.calendar.count + eventStats.signal.count} 条事件
            </h4>
            <span className="font-mono text-[9px] text-muted-foreground/70">按重要度/分数排序 · 各取 Top 3</span>
          </div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            {/* 1. 绿字看涨 (BUY) */}
            <div className="rounded border border-[#22c55e]/30 bg-[#22c55e]/5 p-2">
              <div className="mb-1.5 flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-full bg-[#22c55e]" />
                  <span className="font-mono text-[10px] font-semibold" style={{ color: '#22c55e' }}>绿字看涨</span>
                </div>
                <span className="rounded bg-[#22c55e]/20 px-1.5 py-0.5 font-mono text-[10px] font-bold" style={{ color: '#22c55e' }}>{eventStats.buy.count}</span>
              </div>
              <div className="space-y-1">
                {eventStats.buy.top.length === 0 ? (
                  <div className="font-mono text-[9px] text-muted-foreground/60">— 无 —</div>
                ) : eventStats.buy.top.map((e: any, i: number) => (
                  <div key={i} className="font-mono text-[9px] leading-snug">
                    <div className="flex items-baseline gap-1">
                      <span className="shrink-0 text-muted-foreground/80">{fmtTs(e.ts)}</span>
                      {e.score != null && (
                        <span className="shrink-0 rounded bg-[#22c55e]/15 px-1 text-[8px]" style={{ color: '#22c55e' }}>
                          +{Math.abs(e.score).toFixed(2)}
                        </span>
                      )}
                    </div>
                    <div className="truncate text-foreground/80" title={e.content}>
                      {titleText(e.content)}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* 2. 红字看跌 (SELL) */}
            <div className="rounded border border-[#ef4444]/30 bg-[#ef4444]/5 p-2">
              <div className="mb-1.5 flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-full bg-[#ef4444]" />
                  <span className="font-mono text-[10px] font-semibold" style={{ color: '#ef4444' }}>红字看跌</span>
                </div>
                <span className="rounded bg-[#ef4444]/20 px-1.5 py-0.5 font-mono text-[10px] font-bold" style={{ color: '#ef4444' }}>{eventStats.sell.count}</span>
              </div>
              <div className="space-y-1">
                {eventStats.sell.top.length === 0 ? (
                  <div className="font-mono text-[9px] text-muted-foreground/60">— 无 —</div>
                ) : eventStats.sell.top.map((e: any, i: number) => (
                  <div key={i} className="font-mono text-[9px] leading-snug">
                    <div className="flex items-baseline gap-1">
                      <span className="shrink-0 text-muted-foreground/80">{fmtTs(e.ts)}</span>
                      {e.score != null && (
                        <span className="shrink-0 rounded bg-[#ef4444]/15 px-1 text-[8px]" style={{ color: '#ef4444' }}>
                          -{Math.abs(e.score).toFixed(2)}
                        </span>
                      )}
                    </div>
                    <div className="truncate text-foreground/80" title={e.content}>
                      {titleText(e.content)}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* 3. ★3+ 日历 */}
            <div className="rounded border border-[#3b82f6]/30 bg-[#3b82f6]/5 p-2">
              <div className="mb-1.5 flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-full bg-[#3b82f6]" />
                  <span className="font-mono text-[10px] font-semibold" style={{ color: '#3b82f6' }}>★3+日历</span>
                </div>
                <span className="rounded bg-[#3b82f6]/20 px-1.5 py-0.5 font-mono text-[10px] font-bold" style={{ color: '#3b82f6' }}>{eventStats.calendar.count}</span>
              </div>
              <div className="space-y-1">
                {eventStats.calendar.top.length === 0 ? (
                  <div className="font-mono text-[9px] text-muted-foreground/60">— 无 —</div>
                ) : eventStats.calendar.top.map((c: any, i: number) => (
                  <div key={i} className="font-mono text-[9px] leading-snug">
                    <div className="flex items-baseline gap-1">
                      <span className="shrink-0 text-muted-foreground/80">{fmtTs(c.ts_bj ?? c.ts ?? 0)}</span>
                      <span className="shrink-0 rounded bg-[#3b82f6]/15 px-1 text-[8px]" style={{ color: '#3b82f6' }}>
                        {'★'.repeat(Math.min(5, c.star ?? 0))}
                      </span>
                    </div>
                    <div className="truncate text-foreground/80" title={c.title}>
                      {titleText(c.title)}
                      {c.is_released && c.actual && <span className="ml-1 text-muted-foreground/70">[{c.actual}]</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* 4. 中性新闻 (HOLD) — v20251014: 原"灰点中性"改名"K 线上不再显示灰点；下方仍展示统计与列表" */}
            <div className="rounded border border-[#9ca3af]/30 bg-[#9ca3af]/5 p-2">
              <div className="mb-1.5 flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#9ca3af]" />
                  <span className="font-mono text-[10px] font-semibold" style={{ color: '#9ca3af' }}>中性新闻</span>
                </div>
                <span className="rounded bg-[#9ca3af]/20 px-1.5 py-0.5 font-mono text-[10px] font-bold" style={{ color: '#9ca3af' }}>{eventStats.hold.count}</span>
              </div>
              <div className="space-y-1">
                {eventStats.hold.top.length === 0 ? (
                  <div className="font-mono text-[9px] text-muted-foreground/60">— 无 —</div>
                ) : eventStats.hold.top.map((e: any, i: number) => (
                  <div key={i} className="font-mono text-[9px] leading-snug">
                    <div className="flex items-baseline gap-1">
                      <span className="shrink-0 text-muted-foreground/80">{fmtTs(e.ts)}</span>
                    </div>
                    <div className="truncate text-foreground/80" title={e.content}>
                      {titleText(e.content)}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* 5. 柱内绿点 B/S（信号） */}
            <div className="rounded border border-[#86efac]/30 bg-[#86efac]/5 p-2">
              <div className="mb-1.5 flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#22c55e] ring-1 ring-[#22c55e]/50" />
                  <span className="font-mono text-[10px] font-semibold" style={{ color: '#86efac' }}>柱内B/S</span>
                </div>
                <span className="rounded bg-[#22c55e]/20 px-1.5 py-0.5 font-mono text-[10px] font-bold" style={{ color: '#22c55e' }}>
                  {eventStats.signal.count}
                  <span className="ml-1 text-[8px] opacity-75">B{eventStats.signal.buy}/S{eventStats.signal.sell}</span>
                </span>
              </div>
              <div className="space-y-1">
                {eventStats.signal.top.length === 0 ? (
                  <div className="font-mono text-[9px] text-muted-foreground/60">— 无 —</div>
                ) : eventStats.signal.top.map((s: any, i: number) => {
                  const isBuy = s.action === 'BUY'
                  const tsNum = typeof s.news_time === 'number'
                    ? s.news_time
                    : (typeof s.news_time === 'string' && !isNaN(Date.parse(s.news_time))
                      ? Math.floor(Date.parse(s.news_time) / 1000) : 0)
                  return (
                    <div key={i} className="font-mono text-[9px] leading-snug">
                      <div className="flex items-baseline gap-1">
                        <span className="shrink-0 text-muted-foreground/80">{tsNum ? fmtTs(tsNum) : '—'}</span>
                        <span className={`shrink-0 rounded px-1 text-[8px] font-bold ${isBuy ? 'bg-[#22c55e]/15' : 'bg-[#ef4444]/15'}`} style={{ color: isBuy ? '#22c55e' : '#ef4444' }}>
                          {isBuy ? 'B' : 'S'}
                        </span>
                        {s.score != null && (
                          <span className="shrink-0 text-muted-foreground/70">{Math.abs(s.score).toFixed(2)}</span>
                        )}
                      </div>
                      <div className="truncate text-foreground/80" title={s.symbol ?? s.reason ?? ''}>
                        {s.symbol ?? s.reason ?? '—'}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        </div>

        {/* 门卫反思（第一层 AI 语义筛选复盘：正向判断兑现 / 反向异动漏检） */}
        <GateReflectionList asset={selected} />

        {/* v220：本地存档浏览 — 列出所有已存资产（用户：「保存归因页所有生成的数据在本地作为知识图谱」） */}
        <SnapshotIndex
          current={selected}
          onClear={clearSnapshot}
          onLoad={(asset) => setSelected(asset)}
        />

        {/* 事件时间线 */}
        <div className="mt-4">
          <h4 className="mb-2 font-mono text-xs uppercase tracking-wider text-muted-foreground">
            关联新闻时间线（近 30 天 · {currentEvents.length} 条）
          </h4>
          {current?.drivers?.summary && (
            <div className="mb-3 rounded border border-primary/30 bg-primary/5 px-3 py-2.5">
              <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-primary">
                ⭐ 主要驱动事件 · AI 分析
              </div>
              <p className="text-xs leading-relaxed text-foreground">{current.drivers.summary}</p>
              {(current.drivers.drivers ?? []).length > 0 && (
                <ul className="mt-1.5 space-y-1">
                  {(current.drivers.drivers ?? []).map((d, j) => (
                    <li key={j} className="flex items-start gap-2 text-[11px] leading-relaxed text-foreground/85">
                      <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${d.importance === 'high' ? 'bg-long' : 'bg-hold'}`} />
                      <span className="shrink-0 font-mono text-muted-foreground">{fmtTs(d.ts)}</span>
                      <span className="min-w-0 flex-1">{d.content}</span>
                      <span className="shrink-0 rounded bg-muted/60 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{d.source}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {/* v275: 蓝色事件 K 线影响验证 — 每个 ★3+ 日历事件 vs 事件后 30min K 线波动 */}
          {Object.keys(calImpact).length > 0 && (
            <div className="mb-3 rounded border border-[#3b82f6]/30 bg-[#3b82f6]/5 px-3 py-2">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div className="font-mono text-[10px] uppercase tracking-wider" style={{ color: '#3b82f6' }}>
                  🎯 蓝色事件 K 线影响验证 · {Object.keys(calImpact).length} 个事件 · 扫描事件后 30 分钟 K 线
                </div>
                <div className="flex items-center gap-2 font-mono text-[9px]">
                  <span className="rounded bg-[#22c55e]/15 px-1.5 py-0.5" style={{ color: '#22c55e' }}>
                    🟢 跟随 ({Object.values(calImpact).filter((v: any) => v?.kline_impact?.verdict === 'follow').length})
                  </span>
                  <span className="rounded bg-[#ef4444]/15 px-1.5 py-0.5" style={{ color: '#ef4444' }}>
                    🔴 反向 ({Object.values(calImpact).filter((v: any) => v?.kline_impact?.verdict === 'reverse').length})
                  </span>
                  <span className="rounded bg-muted/40 px-1.5 py-0.5 text-muted-foreground">
                    🟡 微弱 ({Object.values(calImpact).filter((v: any) => v?.kline_impact?.verdict === 'neutral').length})
                  </span>
                  <span className="rounded bg-muted/40 px-1.5 py-0.5 text-muted-foreground">
                    ⚪ 无反应 ({Object.values(calImpact).filter((v: any) => v?.kline_impact?.verdict === 'no_reaction').length})
                  </span>
                </div>
              </div>
              <ul className="space-y-1.5">
                {Object.values(calImpact)
                  .filter((ev: any) => ev?.kline_impact?.has_data)
                  .sort((a: any, b: any) => {
                    const order: Record<string, number> = { follow: 0, reverse: 1, neutral: 2, no_reaction: 3 }
                    const va = order[a?.kline_impact?.verdict ?? ''] ?? 9
                    const vb = order[b?.kline_impact?.verdict ?? ''] ?? 9
                    if (va !== vb) return va - vb
                    return (b?.kline_impact?.impact_pct ?? 0) - (a?.kline_impact?.impact_pct ?? 0)
                  })
                  .slice(0, 12)
                  .map((ev: any, i: number) => {
                    const ki = ev.kline_impact
                    const v = ki.verdict
                    const dot = v === 'follow' ? '🟢' : v === 'reverse' ? '🔴' : v === 'no_reaction' ? '⚪' : '🟡'
                    const label = v === 'follow' ? '跟随' : v === 'reverse' ? '反向' : v === 'no_reaction' ? '无反应' : '微弱'
                    const labelColor = v === 'follow' ? '#22c55e' : v === 'reverse' ? '#ef4444' : '#9ca3af'
                    const sevColor = ki.severity === 'high' ? '#ef4444' : ki.severity === 'medium' ? '#f59e0b' : '#9ca3af'
                    return (
                      <li
                        key={`${ev.ts_bj}-${i}`}
                        className="flex flex-wrap items-center gap-2 rounded border border-border bg-card/60 px-2 py-1.5 font-mono text-[11px]"
                      >
                        <span className="shrink-0">{dot}</span>
                        <span className="shrink-0 text-muted-foreground">{fmtTs(ev.ts_bj)}</span>
                        <span className="shrink-0 rounded bg-[#3b82f6]/15 px-1.5 py-0.5 font-semibold" style={{ color: '#3b82f6' }}>
                          {'★'.repeat(Math.min(5, ev.star ?? 0))}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-foreground/85" title={ev.title}>
                          {titleText(ev.title)}
                          {ev.is_released && ev.actual && (
                            <span className="ml-1 text-muted-foreground/70">[{ev.actual}{ev.surprise && parseFloat(ev.surprise) !== 0 ? ` 偏差${ev.surprise}` : ''}]</span>
                          )}
                        </span>
                        <span className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold" style={{ backgroundColor: labelColor + '20', color: labelColor }}>
                          {label}
                        </span>
                        <span className="shrink-0 text-[10px]" style={{ color: sevColor }}>
                          {ki.impact_pct >= 0 ? '+' : ''}{ki.impact_pct.toFixed(2)}%
                        </span>
                        <span className="shrink-0 text-[9px] text-muted-foreground/70" title={`up ${ki.up_pct}% / dn ${ki.dn_pct}%`}>
                          ↑{ki.up_pct.toFixed(2)} ↓{ki.dn_pct.toFixed(2)}
                        </span>
                      </li>
                    )
                  })}
              </ul>
              {Object.values(calImpact).filter((ev: any) => ev?.kline_impact?.has_data).length === 0 && (
                <div className="font-mono text-[10px] text-muted-foreground/70">未公布的日历事件或 K 线数据不足，无法验证影响。</div>
              )}
            </div>
          )}

          {/* v275: 自分析交易点 K 线验证 — 每个 B/S 信号 vs 信号后 30 分钟 K 线走势 */}
          {markersData.signals.length > 0 && markersData.bars.length > 0 && (() => {
            const ver = computeSignalVerdicts(markersData.signals, markersData.bars)
            if (ver.items.length === 0) return null
            return (
              <div className="mb-3 rounded border border-emerald-500/30 bg-emerald-500/5 px-3 py-2">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="font-mono text-[10px] uppercase tracking-wider text-emerald-700 dark:text-emerald-300">
                    📊 自分析交易点 K 线验证 · {markersData.signals.length} 个信号 · 信号后 30 分钟走势对照
                  </div>
                  <div className="flex items-center gap-2 font-mono text-[9px]">
                    <span className="rounded bg-[#22c55e]/15 px-1.5 py-0.5" style={{ color: '#22c55e' }}>
                      ✅ 一致 ({ver.match})
                    </span>
                    <span className="rounded bg-[#ef4444]/15 px-1.5 py-0.5" style={{ color: '#ef4444' }}>
                      ❌ 不一致 ({ver.mismatch})
                    </span>
                    <span className="rounded bg-muted/40 px-1.5 py-0.5 text-muted-foreground">
                      ⚪ 中性 ({ver.neutral})
                    </span>
                  </div>
                </div>
                <ul className="space-y-1.5">
                  {ver.items.slice(0, 10).map((it: any, i: number) => {
                    const isBuy = it.action === 'BUY'
                    const v = it.verdict
                    const dot = v === 'match' ? '✅' : v === 'mismatch' ? '❌' : '⚪'
                    const colorBg = v === 'match' ? 'bg-[#22c55e]/20' : v === 'mismatch' ? 'bg-[#ef4444]/20' : 'bg-muted/40'
                    const colorFg = v === 'match' ? '#22c55e' : v === 'mismatch' ? '#ef4444' : '#9ca3af'
                    return (
                      <li
                        key={`${it.news_time}-${i}`}
                        className="flex flex-wrap items-center gap-2 rounded border border-border bg-card/60 px-2 py-1.5 font-mono text-[11px]"
                      >
                        <span className="shrink-0">{dot}</span>
                        <span className="shrink-0 text-muted-foreground">{fmtTs(it.news_time)}</span>
                        <span
                          className={`shrink-0 rounded px-1.5 py-0.5 font-semibold ${isBuy ? 'bg-[#22c55e]/20' : 'bg-[#ef4444]/20'}`}
                          style={{ color: isBuy ? '#22c55e' : '#ef4444' }}
                        >
                          {isBuy ? '↑ BUY' : '↓ SELL'}
                        </span>
                        {it.score != null && (
                          <span className="shrink-0 rounded bg-muted/40 px-1 py-0.5 text-[9px] text-muted-foreground">
                            {isBuy ? '+' : '-'}{Math.abs(it.score).toFixed(2)}
                          </span>
                        )}
                        <span className="min-w-0 flex-1 truncate text-foreground/85" title={it.reasoning ?? ''}>
                          {it.reasoning ? titleText(it.reasoning) : '— 无信号理由 —'}
                        </span>
                        <span className={`shrink-0 rounded ${colorBg} px-1.5 py-0.5 text-[10px] font-semibold`} style={{ color: colorFg }}>
                          {v === 'match' ? '一致' : v === 'mismatch' ? '不一致' : '中性'}
                        </span>
                        <span className="shrink-0 text-[10px]" style={{ color: it.move_pct >= 0 ? '#22c55e' : '#ef4444' }}>
                          {it.move_pct >= 0 ? '+' : ''}{it.move_pct.toFixed(2)}%
                        </span>
                      </li>
                    )
                  })}
                </ul>
              </div>
            )
          })()}

          {current && (current.abnormals ?? []).length > 0 && (
            <div className="mb-3 rounded border border-amber-500/30 bg-amber-500/5 px-3 py-2">
              <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-amber-500">
                3 分钟异动反推 · 时间锚回溯（AI 只引用公开免费数据源，不确定则列候选并标注推测）
              </div>
              <ul className="space-y-1.5">
                {(current.abnormals ?? []).slice(0, 6).map((a, i) => {
                  // v282+: HONESTY — pct_change nullable,不用 ?? 0 假装有数
                  const up = a.pct_change != null && a.pct_change >= 0
                  const dotColor = a.pct_change == null ? '#f59e0b' : up ? '#22c55e' : '#ef4444'
                  const aKey = `${current.asset}:${a.ts}`
                  const attr = a.attribution ?? attribMap[aKey] ?? null
                  const loading = attribLoading === aKey
                  const cands = attr?.candidates ?? []
                  return (
                    <li key={i} className="rounded border border-border bg-card/40 px-2 py-1.5">
                      <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-muted-foreground">
                        <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: dotColor }} />
                        <span className="text-foreground/80">{fmtTs(a.ts)}</span>
                        <span>{a.pct_change != null ? `单根${a.pct_change >= 0 ? '+' : ''}${a.pct_change.toFixed(2)}%` : ''}</span>
                        <span>{a.vol_ratio != null ? `· 量比 ${a.vol_ratio.toFixed(2)}×` : ''}</span>
                      </div>
                      {attr?.summary && (
                        <div className="mt-1 flex items-start gap-1.5 text-[11px] leading-relaxed text-foreground/85">
                          <span className="shrink-0 font-mono text-[9px] uppercase tracking-wider text-primary/80">AI 归因</span>
                          <span>{attr.summary}</span>
                        </div>
                      )}
                      {!attr?.summary && attr?.error && (
                        <div className="mt-1 font-mono text-[10px] text-muted-foreground/80">{attr.error}</div>
                      )}
                      {!attr?.summary && !attr?.error && (
                        <button
                          type="button"
                          onClick={() => askAttribution(current.asset, a.ts)}
                          className="mt-1 inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary transition-colors hover:bg-primary/20"
                        >
                          {loading ? 'AI 分析中…' : '🤖 AI 分析：这个时间点发生了什么？'}
                        </button>
                      )}
                      {cands.length > 0 && (
                        <ul className="mt-1 space-y-0.5">
                          {cands.map((c, j) => (
                            <li key={j} className="font-mono text-[10px] leading-relaxed text-muted-foreground/85">
                              <span className={c.confidence === 'high' ? 'text-long' : c.confidence === 'medium' ? 'text-hold' : 'text-muted-foreground'}>
                                {c.confidence === 'high' ? '●' : c.confidence === 'medium' ? '◐' : '○'}
                              </span>
                              {' '}{c.cause}
                              {c.source ? <span className="text-muted-foreground/60">（{c.source}）</span> : null}
                              {c.speculative ? <span className="text-hold"> · 推测</span> : null}
                            </li>
                          ))}
                        </ul>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          )}
          {currentEvents.length === 0 ? (
            <div className="font-mono text-xs text-muted-foreground">该品种暂无关联新闻事件</div>
          ) : (
            <div className="space-y-4">
              {(() => {
                // 按自然日分组；主要驱动事件置顶（⭐ + AI 驱动原因），其余按时间倒序
                const driverByTs = new Map((current?.drivers?.drivers ?? []).map((d) => [d.ts, d]))
                const groups = new Map<string, MoverEvent[]>()
                for (const e of currentEvents.slice(0, 40)) {
                  const k = dayLabel(e.ts)
                  const arr = groups.get(k) ?? []
                  arr.push(e)
                  groups.set(k, arr)
                }
                return Array.from(groups.entries()).map(([label, evs]) => {
                  const sorted = [...evs].sort((a, b) => {
                    const da = driverByTs.has(a.ts) ? 1 : 0
                    const db = driverByTs.has(b.ts) ? 1 : 0
                    if (db - da !== 0) return db - da
                    return b.ts - a.ts
                  })
                  return (
                    <div key={label}>
                      <div className="mb-2 flex items-center gap-2">
                        <span className="font-mono text-[11px] font-semibold text-foreground/80">{label}</span>
                        <span className="h-px flex-1 bg-border" />
                        <span className="font-mono text-[10px] text-muted-foreground">{sorted.length} 条</span>
                      </div>
                      <ul className="space-y-2">
                        {sorted.map((e, i) => {
                          const am = ACTION_META[e.action] ?? ACTION_META.HOLD
                          const drv = driverByTs.get(e.ts)
                          const isOpen = expanded === `${e.ts}-${i}`
                          const dotCls = e.action === 'BUY' ? 'bg-long' : e.action === 'SELL' ? 'bg-short' : 'bg-muted-foreground'
                          return (
                            <li
                              key={`${e.ts}-${i}`}
                              className={`rounded-lg border px-3.5 py-2.5 transition-colors ${
                                drv ? 'border-primary/50 bg-primary/5' : 'border-border bg-card'
                              }`}
                            >
                              {/* 顶部元信息行：时间 · 方向 · 主要驱动 · 来源（来源右对齐，不与正文抢行） */}
                              <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
                                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotCls}`} />
                                <span className="font-mono text-[11px] text-muted-foreground">{fmtTs(e.ts)}</span>
                                <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${am.cls}`}>{am.label}</span>
                                {drv && (
                                  <span className="inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-primary">
                                    ⭐ 主要驱动
                                  </span>
                                )}
                                <span className="ml-auto shrink-0 rounded bg-muted/60 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                                  {e.source}
                                </span>
                              </div>
                              {/* 正文独占一行，可点击展开/收起 */}
                              <p
                                className="cursor-pointer text-[13px] leading-6 text-foreground"
                                onClick={() => setExpanded(isOpen ? null : `${e.ts}-${i}`)}
                                title={isOpen ? '点击收起' : '点击展开全文'}
                                style={
                                  isOpen
                                    ? undefined
                                    : { display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }
                                }
                              >
                                {e.content}
                              </p>
                              {!isOpen && e.content.length > 70 && (
                                <span className="mt-0.5 font-mono text-[10px] text-muted-foreground/60">点击展开全文 ↑</span>
                              )}
                              {/* AI 驱动原因单独成行，左边框强调 */}
                              {drv?.reason && (
                                <div className="mt-2 border-l-2 border-primary/50 pl-2.5">
                                  <p className="text-[12px] leading-relaxed text-foreground/85">
                                    <span className="font-mono text-[9px] uppercase tracking-wider text-primary/80">AI 驱动</span>{' '}
                                    {drv.reason}
                                  </p>
                                </div>
                              )}
                            </li>
                          )
                        })}
                      </ul>
                    </div>
                  )
                })
              })()}
            </div>
          )}
        </div>
        </section>

        {/* v232：异常识别 + 新闻类型判断（K 线下方 · 左侧主区） */}
        <AnomalyTypePanel />
        </div>

        {/* 右侧栏（col-span-1，上下堆叠辅助面板） */}
        <aside className="flex flex-col gap-3 lg:col-span-1">
          {/* 排行卡片（窄列：2 列网格） */}
          <div className="grid grid-cols-2 gap-2">
            {(items ?? []).map((m) => {
              const chg = m.change24h
              const up = chg == null ? null : chg >= 0
              const active = m.asset === selected
              return (
                <div
                  key={m.asset}
                  className={`group relative rounded border p-2 transition-colors ${
                    active ? 'border-primary bg-card' : 'border-border bg-card/60 hover:bg-card'
                  }`}
                >
                  <button
                    type="button"
                    onClick={(ev) => { ev.stopPropagation(); goGraph(m.asset) }}
                    title={`打开 ${m.asset} 资产图谱（5-why 因果）`}
                    className="absolute right-1 top-1 inline-flex items-center gap-0.5 rounded px-1 py-0.5 font-mono text-[9px] text-muted-foreground opacity-0 transition-opacity hover:bg-secondary hover:text-primary group-hover:opacity-100"
                  >
                    <ExternalLink className="h-2.5 w-2.5" />图谱
                  </button>
                  <button
                    type="button"
                    onClick={() => setSelected(m.asset)}
                    className="block w-full text-left"
                  >
                    <div className="flex items-center gap-1">
                      <span className="font-mono text-xs font-bold text-foreground">{m.asset}</span>
                      <span className="truncate font-mono text-[9px] text-muted-foreground">{ASSET_META[m.asset] ?? ''}</span>
                      {m.events.length > 0 && (
                        <span className="ml-auto rounded bg-muted/50 px-1 py-0.5 font-mono text-[9px] text-muted-foreground">
                          {m.events.length}
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 font-mono text-sm text-foreground">
                      {m.price == null ? '—' : m.price.toLocaleString('en-US', { maximumFractionDigits: 2 })}
                    </div>
                    <div className={`font-mono text-[10px] ${up == null ? 'text-muted-foreground' : up ? 'text-long' : 'text-short'}`}>
                      {chg == null ? '涨跌 —' : `${up ? '▲' : '▼'} ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`}
                    </div>
                  </button>
                </div>
              )
            })}
          </div>

          {/* 资产热力图 + 事件热力图（窄列上下堆叠） */}
          <AssetHeatmap onSelectAsset={(a) => setSelected(a)} />
          <EventHeatmap hours={24} limit_assets={10} />

          {/* 资产涨跌排行榜 */}
          <AssetLeaderboard onSelectAsset={(a) => setSelected(a)} />

          {/* 异常详情面板 */}
          <AbnormalDetailPanel />
        </aside>
      </div>
    </main>
  )
}

// ── v220：本地存档浏览子组件 ──────────────────────────────────────────
function SnapshotIndex(props: {
  current: string | null
  onClear: () => void
  onLoad: (asset: string) => void
}) {
  // v302+ P0: 顶层 useToast — 替代 onClick 里的 window.confirm。
  // 不能在 callback 里用 hook,所以拿到闭包顶部。
  const { toast: indexToast } = useToast()
  const [idx, setIdx] = useState<Array<{ asset: string; saved_at: string; bar_count: number; saved_at_bj: string }>>([])
  const [bytes, setBytes] = useState<number>(0)

  const refresh = useCallback(() => {
    try {
      const raw = window.localStorage.getItem(SNAPSHOT_LS_KEY)
      const list = raw ? (JSON.parse(raw) as Array<{ asset: string; saved_at: string; bar_count: number; saved_at_bj: string }>) : []
      setIdx(list)
      // 估算总占用
      let total = 0
      for (let i = 0; i < window.localStorage.length; i++) {
        const k = window.localStorage.key(i)
        if (k && k.startsWith(SNAPSHOT_PREFIX)) {
          const v = window.localStorage.getItem(k) ?? ''
          total += (k.length + v.length) * 2  // UTF-16 = 2 字节/字符
        }
      }
      setBytes(total)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    refresh()
    // 监听跨 tab 写入 + 同 tab 写入：3s 一次轮询（轻量）
    const t = window.setInterval(refresh, 3000)
    return () => window.clearInterval(t)
  }, [refresh, props.current])

  if (idx.length === 0) {
    return (
      <div className="mt-4 rounded border border-dashed border-border bg-card/40 px-3 py-2 font-mono text-[10px] text-muted-foreground">
        📚 本地存档库为空 — 点上方「💾 保存到本地」即可把当前品种的 K 线 + 3 类图例 marker + 异动 + AI 归因 + 驱动摘要打包存为 JSON。
      </div>
    )
  }
  return (
    <div className="mt-4 rounded border border-border bg-card/40 px-3 py-2 font-mono text-[10px]">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <span className="text-muted-foreground">
          📚 本地存档（{idx.length} 个资产 · {bytes > 0 ? `${(bytes / 1024).toFixed(1)} KB` : '0 KB'}）
        </span>
        <span className="text-[9px] text-muted-foreground/70">
          点击「载入」切到该资产查看；存档以「节点-边」结构存储（assets / events / signals / bars / attributions），可直接喂 Neo4j / NetworkX
        </span>
      </div>
      <ul className="flex flex-wrap gap-1.5">
        {idx.map((it) => (
          <li
            key={it.asset + it.saved_at}
            className={`flex items-center gap-1 rounded border px-1.5 py-0.5 ${
              it.asset === props.current ? 'border-primary/60 bg-primary/10 text-primary' : 'border-border bg-background/60'
            }`}
          >
            <span className="font-mono">{it.asset}</span>
            <span className="text-muted-foreground">{it.bar_count}bars</span>
            <span className="text-muted-foreground">{it.saved_at_bj}</span>
            <button
              type="button"
              onClick={() => props.onLoad(it.asset)}
              className="rounded bg-primary/10 px-1 text-primary hover:bg-primary/20"
              title={`切到 ${it.asset} 查看该存档`}
            >
              载入
            </button>
            {it.asset === props.current && (
              <button
                type="button"
                onClick={() => {
                  // v302+ P0: 用 indexToast.confirm 替代 window.confirm(hook 已在闭包顶部取过)
                  indexToast.confirm(`清空 ${it.asset} 的本地存档？`, () => {
                    props.onClear()
                    refresh()
                  })
                }}
                className="rounded bg-amber-500/10 px-1 text-amber-700 hover:bg-amber-500/20 dark:text-amber-300"
                title="清空该资产存档"
              >
                清空
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
