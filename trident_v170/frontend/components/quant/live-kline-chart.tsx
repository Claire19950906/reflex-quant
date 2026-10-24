'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { TrendingUp } from 'lucide-react'
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
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import { API_BASE } from '@/lib/api'
import { useIsDark } from '@/hooks/use-is-dark'
import { rsi as rsiFn, bollinger, bollingerSqueezeMask, pivotPoint, pivotPeriodForTimeframe } from '@/lib/technical-indicators'
import type { Jin10CalendarItem } from '@/components/replay/jin10-calendar-panel'

// 需求3：实时台统一 lightweight-charts 自绘（替代官方 TradingView embed iframe 黑盒），
// 使「事件标注 / 品种底色 / 开市时段连续形态」可叠加、可自定义。
// 数据源走后端 /api/market/history/{asset}（真实 K 线，绝不 mock）。

type Props = {
  market: MarketCategory // 'CRYPTO' | 'GOLD' | 'OIL'
  events: ApiEvent[]
}

type HistoryBar = { time: number; open: number; high: number; low: number; close: number; volume?: number }
type HistoryResp = {
  points: unknown[]
  bars: HistoryBar[]
  count: number
  missing: string
  anomalies?: AnomalyMarker[]   // Q5: 异常扫描结果（柱底 belowBar markers 用）
}

// 品种主题：one accent 色贯穿边框 / 顶部色带 / 徽章 / 水印，一眼分辨当前 K 线属哪个品种
const MARKET_MAP: Record<string, { asset: string; label: string; name: string; accent: string; badge: string }> = {
  CRYPTO: { asset: 'BTC', label: 'BTC/USDT', name: '比特币', accent: '#f7931a', badge: 'rgba(247,147,26,0.12)' }, // BTC 橙
  GOLD: { asset: 'XAU', label: 'XAU/USD', name: '黄金', accent: '#e8b84b', badge: 'rgba(232,184,75,0.14)' },      // 黄金金
  OIL: { asset: 'WTI', label: 'WTI/USD', name: '原油', accent: '#39a877', badge: 'rgba(57,168,119,0.14)' },       // 原油绿
}

// 事件 target_asset 别名归一（后端可能返回 BTC/GOLD/OIL/带 USDT 后缀等）
const ASSET_ALIASES: Record<string, string[]> = {
  BTC: ['BTC', 'BTCUSDT', 'BITCOIN'],
  XAU: ['XAU', 'GOLD', 'XAUUSD', 'XAUUSDT'],
  WTI: ['WTI', 'OIL', 'WTIUSD', 'USOIL', 'CLUSDT'],
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
  },
  dark: {
    background: '#16171b',
    text: '#9aa3af',
    grid: '#26282e',
    border: '#33363d',
    up: '#2ebd85',
    down: '#f6465d',
    flat: '#9aa3af',
  },
} as const

const SH_OFFSET_MS = 8 * 3600 * 1000

// ── v221.9 三行五类事件布局（避免堆叠） ─────────────────────────
// 设计：3 行 × 多 marker，行间不挤压，行内 rowIndex 错开
//
// ┌─ 柱上方 aboveBar ─────────────────────────────────────────────
// │  💜 紫字 交易点  BUY/SELL 价    [top: 4  + rowIndex*24]   (默认隐藏，工具栏 toggle)
// │  📅 蓝字 日历  ≥3星宏观数据    [top: 28 + rowIndex*24]
// │  📰 红字 高优新闻  vip_tag 非空  [top: 52 + rowIndex*24]
// │  🟢 绿字 交易信号  BUY/SELL     [top: 76 + rowIndex*24]   (v283+ 从 inBar 上移)
// ├─ 柱内 inBar ─────────────────────────────────────────────────
// │  （无 HTML 标签 —— 信号已上移到 aboveBar，柱体内仅留 native arrow）
// ├─ 柱下方 belowBar ─────────────────────────────────────────────
// │  ⚠ 彩色异动⭐ K 线异常          [bottom: 20 + rowIndex*24]  (可点按 AI 归因)
// │  📰 淡红字 普通新闻 HOLD 无 vip  [bottom: 44 + rowIndex*24]
// └──────────────────────────────────────────────────────────────
//
// 关键：
// · 异常按 direction 分色（bullish=绿、bearish=红、neutral=黄）—— 不强制红色
// · 高优新闻 vs 普通新闻：vip_tag 非空（FED 等 VIP 公告）= 高优
// · 异常 marker 加 ⭐ → 点按触发 AI 归因（/api/market/attribute）
// · 同 kind+time 多个 marker 按 rowIndex 递增错开；超过 3 行自动折叠为 "+N more"
// · v283+ 文字颜色 = 事件本身颜色（lbl.color），不再覆盖
// ── v283+ 硬约束 ──────────────────────────────────────────────────
// · 所有覆盖在 K 线上的新闻文字元素必须满足：
//   ① pointer-events: none（默认），仅异常点击触发 AI 归因时 pointer-events: auto
//   ② 定位远离柱体（顶部 top ≤ 76+3×24 = 148px，底部 bottom ≤ 44+3×24 = 116px）
//   ③ 同一时间堆叠超过 3 个时折叠为 "+N more" 计数 chip，不再向上/下扩展
//   ④ 透明度 0.18 背景 + 同色边框，不使用 solid 背景（避免视觉遮挡）
// · 任何新增文字元素（hover tip、tooltip、AI 归因面板）必须沿用上述规则
const COLOR_CALENDAR = '#3b82f6'  // blue-500  柱上·日历
const COLOR_HIGH_NEWS = '#ef4444' // red-500   柱上·高优新闻
const COLOR_SIGNAL = '#22c55e'    // green-500 柱内·交易信号
const COLOR_NEWS = '#fca5a5'      // red-300   柱下·普通新闻（淡化）
// v290+ — 交易点（BUY/SELL 入场/出场价）紫色，按钮默认关闭，点开后 aboveBar 显示
// 与 green inBar 信号区分：信号=快速识别（开/关）；交易点=详细价位（toggle）
const COLOR_TRADE_POINT = '#a855f7'  // purple-500 柱上·交易点
const COLOR_TRADE_POINT_BG = 'rgba(168,85,247,0.18)'  // 18% 同色填充
const COLOR_TRADE_POINT_BORDER = 'rgba(168,85,247,0.85)'
// 异常按方向分色（v221.9 恢复彩色，与方向一致）
const COLOR_ANOM_BULL = '#22c55e' // green-500  看涨
const COLOR_ANOM_BEAR = '#ef4444' // red-500    看跌
const COLOR_ANOM_NEUT = '#fbbf24' // amber-400  中性

// R15+: 价格结构形态 (双顶/双底/吞没/锤子/射击之星/十字星) — 形态即描述, 不预报方向
const STRUCTURE_COLOR: Record<string, string> = {
  double_top: '#a855f7',      // purple-500 顶部反转
  double_bottom: '#a855f7',   // purple-500 底部反转
  shooting_star: '#ef4444',   // red-500 顶部反转(单K)
  bearish_engulfing: '#ef4444', // red-500 顶部反转(双K)
  hammer: '#22c55e',          // green-500 底部反转(单K)
  bullish_engulfing: '#22c55e', // green-500 底部反转(双K)
  doji: '#fbbf24',            // amber-400 方向不明
}
const STRUCTURE_LABEL: Record<string, string> = {
  double_top: '双顶', double_bottom: '双底',
  shooting_star: '射击之星', bearish_engulfing: '看跌吞没',
  hammer: '锤子', bullish_engulfing: '看涨吞没',
  doji: '十字星',
}

interface StructureMarker { ts: number; name: string; name_zh: string; high: number; low: number; close: number; info: any }

// v277+ — 布林带 + Squeeze 期专用色
const COLOR_BOLL_UP = '#60a5fa'    // blue-400   上轨
const COLOR_BOLL_MID = '#facc15'   // yellow-400 中轨
const COLOR_BOLL_LOW = '#60a5fa'   // blue-400   下轨
const COLOR_SQUEEZE_BG = 'rgba(148, 163, 184, 0.18)' // slate-400 18% — Squeeze 段背景

// v278 — Pivot Point 技术支撑阻力位专用色（TradingView 同款经典 7 线）
//   R1/R2/R3 阻力位由浅到深绿；S1/S2/S3 支撑位由浅到深红；Pivot 中线橙色实线
const COLOR_PIVOT_P = '#fb923c'    // orange-400 中心 Pivot 实线
const COLOR_PIVOT_R1 = '#86efac'   // green-300  R1 阻力位 1
const COLOR_PIVOT_R2 = '#4ade80'   // green-400  R2 阻力位 2
const COLOR_PIVOT_R3 = '#22c55e'   // green-500  R3 阻力位 3
const COLOR_PIVOT_S1 = '#fca5a5'   // red-300    S1 支撑位 1
const COLOR_PIVOT_S2 = '#f87171'   // red-400    S2 支撑位 2
const COLOR_PIVOT_S3 = '#ef4444'   // red-500    S3 支撑位 3

// v278 — 实时订单簿深度堆积柱状图颜色（买卖盘）
const COLOR_BOOK_BID = 'rgba(34, 197, 94, 0.55)'  // green-500 半透明  买盘
const COLOR_BOOK_ASK = 'rgba(239, 68, 68, 0.55)'  // red-500   半透明  卖盘

// v278 — 订单簿深度子组件：右侧 90px 柱状堆积图（heatmap 风格）
//   通过 series.priceToCoordinate(price) 把每档价格映射到 Y 坐标
//   v278.1 升级为「热力图」：每档高度压缩到 4px 让 50 档全部可见
//     · 每档 alpha 按 qty/maxQty 归一化（堆积越强越不透明，热度感）
//     · 每档宽度按 qty 缩放（细/粗 = 弱/强堆积）
//     · 买盘（绿）贴中间价上方，卖盘（红）贴中间价下方
//   中间价差用虚线分隔，top 标签带档位数+ts
//   useEffect 订阅 visibleTimeRangeChange，重渲染时跟随 chart 缩放/平移
function OrderbookOverlay({
  orderbook,
  series,
  source,
}: {
  orderbook: { bids: [number, number][]; asks: [number, number][]; ts_ms: number }
  series: ISeriesApi<'Candlestick'>
  source: string
}) {
  const [tick, setTick] = useState(0)  // 触发重渲染（订阅 chart timeScale 变化）
  const [maxQty, setMaxQty] = useState(0)
  const [totalQty, setTotalQty] = useState({ bid: 0, ask: 0 })
  const [layout, setLayout] = useState<{
    asks: { y: number; qty: number; price: number; alpha: number; width: number }[]
    bids: { y: number; qty: number; price: number; alpha: number; width: number }[]
    midY: number
    midPrice: number
  } | null>(null)

  // 每 250ms 重算 layout（chart zoom/pan 时跟手）
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
      // 卖盘按价格升序（最低卖价 → 中间）；买盘按价格降序（最高买价 → 中间）
      const asksAsc = [...orderbook.asks].sort((a, b) => a[0] - b[0])   // 低→高
      const bidsDesc = [...orderbook.bids].sort((a, b) => b[0] - a[0])  // 高→低
      const midPrice = (asksAsc[0][0] + bidsDesc[0][0]) / 2
      // 取本侧 top 30 档（太多反而挡 K 线）+ 计算 max qty
      const asksTop = asksAsc.slice(0, 30)
      const bidsTop = bidsDesc.slice(0, 30)
      const mq = Math.max(
        ...asksTop.map(([, q]) => q),
        ...bidsTop.map(([, q]) => q),
        0.0000001,
      )
      setMaxQty(mq)
      setTotalQty({
        bid: bidsTop.reduce((s, [, q]) => s + q, 0),
        ask: asksTop.reduce((s, [, q]) => s + q, 0),
      })
      // 计算每档 alpha + width（heatmap 风：档越深越不透明、越宽）
      const asksLayout = asksTop.map(([p, q]) => {
        const y = series.priceToCoordinate(p) ?? 0
        const ratio = q / mq
        return {
          y, qty: q, price: p,
          alpha: 0.18 + ratio * 0.72,        // 0.18 ~ 0.90 渐变
          width: 24 + ratio * 60,            // 24px ~ 84px 渐变（heatmap 宽度条）
        }
      })
      const bidsLayout = bidsTop.map(([p, q]) => {
        const y = series.priceToCoordinate(p) ?? 0
        const ratio = q / mq
        return {
          y, qty: q, price: p,
          alpha: 0.18 + ratio * 0.72,
          width: 24 + ratio * 60,
        }
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

  const ROW_H = 5  // 每档 5px（紧凑热力图，30 档 = 150px）

  return (
    <div className="pointer-events-none absolute right-0 top-0 z-[4] h-full w-[90px]">
      {/* 顶部：档位数 + 数据源 */}
      <div className="absolute right-1 top-1 flex items-center gap-1 rounded bg-card/90 px-1.5 py-0.5 font-mono text-[8px] text-muted-foreground shadow-sm">
        <span>📊 深度</span>
        <span style={{ color: '#ef4444' }}>卖 {layout.asks.length}</span>
        <span>·</span>
        <span style={{ color: '#22c55e' }}>买 {layout.bids.length}</span>
      </div>
      {/* 底部：买卖总量 + 时间戳 */}
      <div className="absolute bottom-1 right-1 flex items-center gap-1 rounded bg-card/90 px-1.5 py-0.5 font-mono text-[8px] text-muted-foreground shadow-sm">
        <span style={{ color: '#ef4444' }}>∑{totalQty.ask.toFixed(2)}</span>
        <span>·</span>
        <span style={{ color: '#22c55e' }}>∑{totalQty.bid.toFixed(2)}</span>
      </div>
      {/* 中间价分隔虚线（画在中线 Y 位置） */}
      {layout.midY > 0 && layout.midY < 9999 && (
        <div
          className="absolute left-0 right-0 border-t border-dashed pointer-events-none"
          style={{ top: `${layout.midY}px`, borderColor: 'rgba(148, 163, 184, 0.55)' }}
        />
      )}
      {/* asks（卖盘）heatmap：中线上方，红色 */}
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
              background: `rgba(239, 68, 68, ${row.alpha})`,  // 红色按 qty 渐变
              borderRight: row.alpha > 0.7 ? '1px solid #fca5a5' : 'none',
            }}
            title={`卖 ${row.price}  量 ${row.qty}  (${(row.alpha * 100).toFixed(0)}%)`}
          />
        )
      })}
      {/* bids（买盘）heatmap：中线下方，绿色 */}
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
              background: `rgba(34, 197, 94, ${row.alpha})`,  // 绿色按 qty 渐变
              borderRight: row.alpha > 0.7 ? '1px solid #86efac' : 'none',
            }}
            title={`买 ${row.price}  量 ${row.qty}  (${(row.alpha * 100).toFixed(0)}%)`}
          />
        )
      })}
    </div>
  )
}

// v278 — Pivot 主图左内侧位置标注子组件
//   7 道价格线在主图左侧各画一个彩色小标签，跟随 chart 价格区间（series.priceToCoordinate）实时定位
//   标签内容：键（P/R1-R3/S1-S3）+ 价格 + 中/支/阻
//   颜色：橙 P / 绿 R / 红 S（与价格线同色），加深底色 + 白字
//   紧凑 9px 字体，每档只占 ~16px 高度，避免互相遮挡
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

  // 7 个标签：键 / 价 / 色 / 中文角色
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
  void tick  // 触发重渲染

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

function shTickFormatter(time: Time, tickMarkType: TickMarkType): string {
  if (typeof time !== 'number') return String(time)
  const d = new Date(time * 1000 + SH_OFFSET_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  if (tickMarkType === TickMarkType.Time || tickMarkType === TickMarkType.TimeWithSeconds) return hm
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${hm}`
}

// 事件时间字符串 → epoch 秒。后端 timestamp/ai_time 为北京时间「YYYY-MM-DD HH:MM:SS」（无时区）。
function parseEventTs(s: string | null | undefined): number | null {
  if (!s) return null
  const t = String(s).trim()
  if (!t || t === '——') return null
  if (/^\d{4}-\d{2}-\d{2}T/.test(t)) {
    const ms = Date.parse(t)
    return Number.isNaN(ms) ? null : Math.floor(ms / 1000)
  }
  const m = t.replace(' ', 'T')
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(m)) {
    const ms = Date.parse(m + '+08:00')
    return Number.isNaN(ms) ? null : Math.floor(ms / 1000)
  }
  const ms = Date.parse(t)
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000)
}

// 事件时间向前对齐最近的开市 K 线（取 ≤ time 的最近一根）
function alignTime(time: number, bars: HistoryBar[]): number {
  if (!bars.length) return time
  let anchor = bars[0].time
  for (const bar of bars) {
    if (bar.time <= time) anchor = bar.time
    else break
  }
  return anchor
}

// v221.9: actionMarker 已废弃——三行五类事件分类由 classifyEvent 决定
//   · action ∈ {BUY, SELL}   → 'signal'   （柱内绿点）
//   · action === HOLD + vip_tag 非空  → 'high_news'  （柱上红字）
//   · action === HOLD + 无 vip_tag    → 'news'       （柱下淡红字）
type EventKind = 'signal' | 'high_news' | 'news'

function classifyEvent(e: ApiEvent): EventKind {
  if (e.action === 'BUY' || e.action === 'SELL') return 'signal'
  // HOLD 类：vip_tag 非空 = 高优（FED 公告 / 重大事件）；否则普通新闻
  return (e.vip_tag ?? '').trim() ? 'high_news' : 'news'
}

// v221.9: 异常按 direction 取色（彩色异动⭐，不再强制红色）
function anomalyColor(direction: string): string {
  if (direction === 'bullish' || direction === 'up') return COLOR_ANOM_BULL
  if (direction === 'bearish' || direction === 'down') return COLOR_ANOM_BEAR
  return COLOR_ANOM_NEUT
}

function titleText(s: string): string {
  const t = (s ?? '').replace(/\s+/g, ' ').trim()
  if (!t) return ''
  // Q5 改：保留前 18 字作为可见标签（鼠标停在上面时图表仍能呈现完整 news_text 来源）
  return t.length > 18 ? `${t.slice(0, 18)}…` : t
}

// Q7+：浮层标签截断 —— 避免堆叠挤压 chart 主图区。
// 异常类型本身短（"MACD金叉" 4 字），不截；news_text 取前 N 字 + …。
function truncateLabel(s: string, n: number): string {
  const t = (s ?? '').replace(/\s+/g, ' ').trim()
  if (!t) return ''
  if (t.length <= n) return t
  return `${t.slice(0, n)}…`
}

// Q5: 异常类型 marker（API 返回的 scan_anomalies 结构）
type AnomalyMarker = {
  ts: number | null
  type: string
  direction: 'bullish' | 'bearish' | 'neutral'
  indicator: string
}

export function LiveKlineChart({ market, events }: Props) {
  const isDark = useIsDark()
  const meta = MARKET_MAP[market] ?? MARKET_MAP.CRYPTO

  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  // v278 — Pivot Point 7 条水平价格线（P/S1-S3/R1-R3）的引用，附在 candle series 上
  //   用 IPriceLine 接口便于 setData 时整体 remove + 重建
  const pivotLinesRef = useRef<{ [k: string]: IPriceLine | null }>({
    P: null, R1: null, R2: null, R3: null, S1: null, S2: null, S3: null,
  })
  const rsiLineRef = useRef<ISeriesApi<'Line'> | null>(null)
  const rsiHistRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // v277+ — 布林带三轨 line series（叠加在主图上，可独立 toggle）
  const bollUpRef = useRef<ISeriesApi<'Line'> | null>(null)
  const bollMidRef = useRef<ISeriesApi<'Line'> | null>(null)
  const bollLowRef = useRef<ISeriesApi<'Line'> | null>(null)
  // v277+ — Squeeze 段：每根 bar 的 squeeze 状态 + X 像素坐标
  //   用 HTML overlay 画半透明灰底，叠加在主图上
  const [squeezeBands, setSqueezeBands] = useState<
    { time: number; isSqueeze: boolean; widthPct: number | null; threshold: number | null; x: number; w: number }[]
  >([])
  const [squeezePositions, setSqueezePositions] = useState<Record<number, { x: number; w: number }>>({})
  // v277+ — 滤噪音 toggle：开启后自动隐藏 squeeze 期的异动 marker
  const [filterNoise, setFilterNoise] = useState(false)
  // v277+ — BOLL 主图叠加 toggle
  const [showBoll, setShowBoll] = useState(true)
  // v278 — Pivot Point 技术支撑阻力位 toggle（默认开 → 进归因页直接看到 7 道支撑阻力线）
  const [showPivot, setShowPivot] = useState(true)
  // v278 — 当前计算出的 pivot 信息（用于 Header 徽章显示参考周期）
  const [pivotInfo, setPivotInfo] = useState<{
    p: number; r1: number; r2: number; r3: number; s1: number; s2: number; s3: number
    period: number; label: string
  } | null>(null)
  // v278 — 实时订单簿深度 toggle（默认关）
  const [showBook, setShowBook] = useState(false)
  // v290+ — 交易点 toggle（默认关）：开启后柱上方显示 BUY/SELL 入场/出场价（紫色）
  const [showTradePoints, setShowTradePoints] = useState(false)
  // v278 — 订单簿快照数据：{ bids: [[price, qty], ...], asks: [[price, qty], ...], ts_ms, source }
  const [orderbook, setOrderbook] = useState<{
    bids: [number, number][]
    asks: [number, number][]
    ts_ms: number
    source: string
  } | null>(null)
  const [orderbookStatus, setOrderbookStatus] = useState<'idle' | 'loading' | 'ok' | 'unavailable'>('idle')
  // Q5: markers 拆双 ref —— events 在柱子上方（aboveBar / 绿框），anomalies 在柱子下方（belowBar / 红框）
  // v221.8: 三色规则 → 拆三 ref：calendar（蓝 aboveBar）、signal（绿 aboveBar）、anomaly（红 belowBar）
  // v221.9: 三行布局 → 拆四 ref：aboveBar（日历+高优）/ inBar（信号绿点）/ belowBar（异常+普通新闻）
  //       同 position 内不同 kind 用 HTML overlay 区分 Y 位置
  const aboveMarkersRef = useRef<{ detach: () => void } | null>(null)  // 柱上：calendar + high_news
  const inBarMarkersRef = useRef<{ detach: () => void } | null>(null)  // 柱内：signal 绿点
  const belowMarkersRef = useRef<{ detach: () => void } | null>(null)  // 柱下：anomaly + news
  const [chartReady, setChartReady] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [missing, setMissing] = useState('')
  // Q7+：浮层 marker label —— lightweight-charts v5 createSeriesMarkers 不渲染 text 字段，
  // 必须用 HTML overlay 显示「新闻事件内容 / 异常类型」。位置由 chart.timeScale().timeToCoordinate
  // 实时换算，订阅可见时间范围变化。
  type MarkerLabel = {
    key: string
    // v221.9: 五类事件 — 三行布局
    //   calendar     → 柱上方 aboveBar 蓝字   [top: 14]
    //   high_news    → 柱上方 aboveBar 红字   [top: 32]   (ApiEvent HOLD + vip_tag)
    //   signal       → 柱内 inBar 绿点         [inBar]
    //   news         → 柱下方 belowBar 淡红字 [bottom: 32]  (ApiEvent HOLD 无 vip_tag)
    //   anomaly      → 柱下方 belowBar 彩色   [bottom: 14]  (可点按 AI 归因)
    //   trade_point  → 柱上方 aboveBar 紫字   [top: 14]    (BUY/SELL 入场/出场价，默认隐藏，工具栏 toggle)
    kind: 'calendar' | 'high_news' | 'signal' | 'news' | 'anomaly' | 'trade_point'
    time: number  // unix seconds，已对齐到 bar
    text: string
    color: string
    position: 'aboveBar' | 'inBar' | 'belowBar'
    rowIndex: number  // 同 kind+time 内顺序（0, 1, 2…），用于错开堆叠
    // 异常额外字段：用于 AI 归因 + 显示方向
    anomalyTs?: number  // 异常原始 ts（未对齐到 bar）
    anomalyDirection?: 'bullish' | 'bearish' | 'neutral'
    // v290+ — 交易点额外字段：入场/出场价 + BUY/SELL 方向
    tradeAction?: 'BUY' | 'SELL'
    tradePrice?: number | null
  }
  const [markerLabels, setMarkerLabels] = useState<MarkerLabel[]>([])
  const [labelPositions, setLabelPositions] = useState<Record<string, { x: number; y: number }>>({})
  // 需求：K 线周期统一为 10m 默认 + 5m/3m 切换（币安/OKX 原生 3m/5m，10m 后端 5m 聚合）
  const [timeframe, setTimeframe] = useState('10m')
  // v221.7: 异常识别覆盖范围（30 天）。
  // v304: K 线改为全量加载 —— HISTORY_LIMIT 根送进图表, 默认只显示
  //   VISIBLE_BARS 根(锚定右端), 用户可缩放/拖动回看全部历史。
  const ANOMALY_WINDOW_DAYS = 30
  // 后端一次返回的最大根数(10m × 4320 ≈ 30 天, 日线 × 3000 ≈ 8 年)
  const HISTORY_LIMIT = timeframe === '1d' ? 3000 : 4320
  // 默认可见根数(barSpacing=28 时约铺满一屏)
  const VISIBLE_BARS = timeframe === '1d' ? 60 : 48

  // 当前品种直接关联事件（按 target_asset 精确/别名匹配，避免跨品种误标）
  const relatedEvents = useMemo(
    () =>
      events.filter((e) => {
        const t = (e.target_asset ?? '').toUpperCase().replace(/USDT$|USD$|-USDT-SWAP$/, '').trim()
        return (ASSET_ALIASES[meta.asset] ?? [meta.asset]).includes(t)
      }),
    [events, meta.asset],
  )

  // v221.8：金十财经日历事件（仅 3星以上，宏观数据公布 → 蓝色 markers）
  const [calendarEvents, setCalendarEvents] = useState<Jin10CalendarItem[]>([])
  useEffect(() => {
    let alive = true
    fetch(`${API_BASE}/news/jin10/calendar`, { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { status?: string; items?: Jin10CalendarItem[] }) => {
        if (!alive) return
        if (d?.status === 'ok' && Array.isArray(d.items)) {
          setCalendarEvents(d.items.filter((i) => (i.star ?? 0) >= 3))
        }
      })
      .catch(() => {/* 静默失败：日历不可用不影响 K 线主图 */})
    return () => {
      alive = false
    }
  }, [])

  // Q5: hover tooltip —— 当前悬停 bar 上有事件/异常时显示全 news_text
  // v221.9: 五类 kind — calendar / high_news / signal / news / anomaly
  // v290+ : 加 trade_point（紫·交易价位）
  const [hoverTip, setHoverTip] = useState<
    | { kind: 'calendar'; title: string; body: string; x: number; y: number; star: number | null }
    | { kind: 'high_news'; title: string; body: string; x: number; y: number; vipTag: string }
    | { kind: 'signal'; title: string; body: string; x: number; y: number; action: 'BUY' | 'SELL' }
    | { kind: 'news'; title: string; body: string; x: number; y: number }
    | { kind: 'anomaly'; title: string; body: string; direction: string; x: number; y: number }
    | { kind: 'trade_point'; title: string; body: string; x: number; y: number; tradeAction: 'BUY' | 'SELL'; tradePrice: number | null }
    | null
  >(null)
  // v221.9: AI 归因结果面板 —— 点击 ⭐ 异常 marker 触发
  const [aiAttrib, setAiAttrib] = useState<{ ts: number; loading: boolean; data?: any; error?: string } | null>(null)

  // v221.9: 触发 AI 归因 —— 调用 /api/market/attribute?asset=XXX&ts=YYY
  const triggerAiAttribution = useCallback(async (ts: number, label: string) => {
    setAiAttrib({ ts, loading: true })
    try {
      const r = await fetch(
        `${API_BASE}/market/attribute?asset=${encodeURIComponent(meta.asset)}&ts=${encodeURIComponent(String(ts))}`,
        { cache: 'no-store' },
      )
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const data = await r.json()
      setAiAttrib({ ts, loading: false, data })
    } catch (err: any) {
      setAiAttrib({ ts, loading: false, error: String(err?.message ?? err) })
    }
  }, [meta.asset])
  // 最近一次拉到的 K 线 + anomalies，供 crosshair 查表
  const [chartBars, setChartBars] = useState<HistoryBar[]>([])
  const [chartAnomalies, setChartAnomalies] = useState<AnomalyMarker[]>([])
  const [chartStructures, setChartStructures] = useState<StructureMarker[]>([])

  // 建图表（主题切换重建）
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
      rightPriceScale: { borderColor: palette.border },
      timeScale: {
        borderColor: palette.border,
        tickMarkFormatter: shTickFormatter,
        timeVisible: true,
        secondsVisible: false,
        // v304: 默认放大到截图那种程度 —— barSpacing 14 → 28
        //   28px/根, 一屏约 45~55 根 10m K 线, 实体和影线细节清晰可辨
        barSpacing: 28,
        minBarSpacing: 4,
        maxBarSpacing: 60,
        rightOffset: 6,
        fixLeftEdge: false,
        lockVisibleTimeRangeOnResize: true,
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
    // ── RSI 副图：折线 + 柱状图 ──
    // 折线：RSI 原始值（0~100），橙色
    // 柱状图：RSI - 50（向上=多头动量，向下=空头动量），颜色按方向
    // 超买(>70) 与 超卖(<30) 单独高亮（红/绿加亮）
    const rsiLine = chart.addSeries(LineSeries, {
      color: '#f7a440',
      lineWidth: 2,
      priceScaleId: 'rsi_pane',
      lastValueVisible: true,
      priceLineVisible: false,
      title: 'RSI 14',
    })
    const rsiHist = chart.addSeries(HistogramSeries, {
      priceScaleId: 'rsi_pane',
      color: 'rgba(46, 189, 133, 0.55)',
      base: 0,
      lastValueVisible: false,
      priceLineVisible: false,
      title: 'RSI - 50',
    })
    // RSI 副图独立价格尺度：固定 0~100 范围，方便读超买/超卖
    chart.priceScale('rsi_pane').applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
      borderColor: palette.border,
      autoScale: false,
    })
    // 主图压低，给 RSI 副图让出顶部 22% 空间
    candle.priceScale().applyOptions({
      scaleMargins: { top: 0.02, bottom: 0.22 },
    })
    // 30/70 超买超卖参考线
    rsiLine.createPriceLine({
      price: 70,
      color: 'rgba(239, 68, 68, 0.55)',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: '超买 70',
    })
    rsiLine.createPriceLine({
      price: 30,
      color: 'rgba(46, 189, 133, 0.55)',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: '超卖 30',
    })
    rsiLine.createPriceLine({
      price: 50,
      color: 'rgba(154, 163, 175, 0.35)',
      lineWidth: 1,
      lineStyle: 3,
      axisLabelVisible: false,
      title: '中线 50',
    })

    // v277+ — 布林带三轨 line series（叠加在主图 candle 上）
    //   visible 默认由 showBoll toggle 控制，setData 时再二次确认
    const bollUp = chart.addSeries(LineSeries, {
      color: COLOR_BOLL_UP,
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      title: 'BOLL 上轨',
    })
    const bollMid = chart.addSeries(LineSeries, {
      color: COLOR_BOLL_MID,
      lineWidth: 1,
      lineStyle: 2, // dashed — 中轨虚线，与上下轨实线区分
      lastValueVisible: false,
      priceLineVisible: false,
      title: 'BOLL 中轨',
    })
    const bollLow = chart.addSeries(LineSeries, {
      color: COLOR_BOLL_LOW,
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      title: 'BOLL 下轨',
    })

    chartRef.current = chart
    candleRef.current = candle
    rsiLineRef.current = rsiLine
    rsiHistRef.current = rsiHist
    bollUpRef.current = bollUp
    bollMidRef.current = bollMid
    bollLowRef.current = bollLow
    setChartReady(true)
    return () => {
      // v221.9: 三 ref 都 detach
      aboveMarkersRef.current?.detach()
      aboveMarkersRef.current = null
      inBarMarkersRef.current?.detach()
      inBarMarkersRef.current = null
      belowMarkersRef.current?.detach()
      belowMarkersRef.current = null
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      rsiLineRef.current = null
      rsiHistRef.current = null
      bollUpRef.current = null
      bollMidRef.current = null
      bollLowRef.current = null
      setChartReady(false)
    }
  }, [isDark])

  // 拉真实 K 线 + 叠加事件标注
  useEffect(() => {
    if (!chartReady || !candleRef.current || !chartRef.current) return
    let alive = true
    setMissing('')
    setError(null)
    // R15+: 并行拉价格结构形态 (失败不影响 K 线渲染)
    const structuresP = fetch(
      `${API_BASE}/tech/structure?symbol=${encodeURIComponent(meta.asset)}&days=${ANOMALY_WINDOW_DAYS}`,
      { cache: 'no-store' },
    ).then((r) => (r.ok ? r.json() : { structures: [] })).catch(() => ({ structures: [] }))
    // v304: 异常点改为"先算当前窗口并落库, 再读累积历史" ——
    //   单纯读 /tech/anomaly 只反映当前 30 天, 历史累积的异常无法回看。
    //   persist 默认 true, 每次调用都会写入 anomaly_history(幂等, 永不删除)。
    const anomalyP = fetch(
      `${API_BASE}/tech/anomaly?symbol=${encodeURIComponent(meta.asset)}` +
      `&interval=${encodeURIComponent(timeframe)}&days=${ANOMALY_WINDOW_DAYS}&persist=true`,
      { cache: 'no-store' },
    )
      .then((r) => (r.ok ? r.json() : { anomalies: [] }))
      .catch(() => ({ anomalies: [] }))
      .then(async (a: any) => {
        // 再拉累积历史(长期保存的异常点, 供回看与反思)
        try {
          const h = await fetch(
            `${API_BASE}/tech/anomaly-history?symbol=${encodeURIComponent(meta.asset)}` +
            `&interval=${encodeURIComponent(timeframe)}&limit=3000`,
            { cache: 'no-store' },
          )
          if (h.ok) {
            const hd = await h.json()
            if (Array.isArray(hd.anomalies) && hd.anomalies.length) {
              return { anomalies: hd.anomalies, persisted: a.persisted ?? 0 }
            }
          }
        } catch { /* 历史读不到就用当次计算结果 */ }
        return { anomalies: a.anomalies ?? [], persisted: a.persisted ?? 0 }
      })
    Promise.all([
      fetch(`${API_BASE}/market/history/${meta.asset}?interval=${timeframe}&limit=${HISTORY_LIMIT}&days=${ANOMALY_WINDOW_DAYS}`, { cache: 'no-store' }),
      structuresP,
      anomalyP,
    ]).then(([r, struct, anom]) => {
      if (!alive || !candleRef.current || !chartRef.current) return
      if (!r.ok) return Promise.reject(new Error(`HTTP ${r.status}`))
      return Promise.all([r.json(), Promise.resolve(struct), Promise.resolve(anom)])
    })
      .then((triple) => {
        // 上游 then 有两处 early return（!alive / !candleRef / !chartRef / !r.ok），
        // 所以这里收到的可能是 undefined。strict 模式下直接解构会报错，先挡一道。
        if (!triple) return
        const [d, struct, anom] = triple as [HistoryResp, any, any]
        if (!alive || !candleRef.current || !chartRef.current) return
        const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
        const allBars = [...(d.bars ?? [])].sort((a, b) => a.time - b.time)
        const structures: StructureMarker[] = Array.isArray(struct?.structures)
          ? struct.structures : []
        ;(chartRef.current as any).__structures = structures
        // v304: 异常点来自 anomaly_history(累积保存), 后端结构为
        //   {ts, kind, pattern, evidence, score}; 前端 AnomalyMarker 用
        //   {ts, type, direction, indicator}, 这里做映射。
        const rawAnoms: any[] = Array.isArray(anom?.anomalies) ? anom.anomalies : []
        const anomalies: AnomalyMarker[] = rawAnoms.map((a: any) => {
          const kind = a.kind ?? a.type ?? 'anomaly'
          const direction: AnomalyMarker['direction'] =
            kind === 'top_reversal' ? 'bearish'
              : kind === 'bottom_reversal' ? 'bullish' : 'neutral'
          const ev: string[] = Array.isArray(a.evidence) ? a.evidence : []
          return {
            ts: a.ts ?? null,
            type: a.pattern ? String(a.pattern) : kind,
            direction,
            indicator: ev.length ? ev.join('+') : (a.kind ?? kind),
          }
        })
        // v304: 不再截断 DISPLAY_BARS —— 全量历史 K 线送进图表(setData),
        //   默认可见根数由 VISIBLE_BARS 控制。用户可自由缩放/拖动回看全部历史。
        const bars = allBars
        // v278 — 存「全量 K 线」给 pivot/orderbook 等需要 24h+ 跨度的功能用
        setChartBars(allBars)
        setChartAnomalies(anomalies)
        setChartStructures(structures)
        setMissing(d.missing || (bars.length ? '' : '该品种暂无历史 K 线数据'))
        candleRef.current.setData(
          bars.map((b) => ({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })),
        )
        // ── 计算并填充 RSI 折线 + 柱状图 ──
        // rsi() 返回 { time, value: number|null } 序列，value 范围 0~100
        if (rsiLineRef.current && rsiHistRef.current && bars.length >= 15) {
          const rsiBars = bars.map((b) => ({
            time: b.time,
            open: b.open,
            high: b.high,
            low: b.low,
            close: b.close,
            volume: b.volume ?? 0,  // RSI 不用 volume，但 ReplayKlineBar 类型要求非空
          }))
          const series = rsiFn(rsiBars, 14)
          // 折线：跳过 null（暖机期）
          rsiLineRef.current.setData(
            series
              .filter((p) => p.value != null)
              .map((p) => ({ time: p.time as Time, value: p.value as number })),
          )
          // 柱状图：RSI - 50（向上/向下），超买超卖单独高亮
          rsiHistRef.current.setData(
            series
              .filter((p) => p.value != null)
              .map((p) => {
                const v = p.value as number
                const dev = v - 50
                let color: string
                if (v >= 70) color = 'rgba(239, 68, 68, 0.85)'         // 超买 → 加亮红
                else if (v <= 30) color = 'rgba(46, 189, 133, 0.85)'   // 超卖 → 加亮绿
                else if (dev >= 0) color = 'rgba(46, 189, 133, 0.55)'  // 多头 → 浅绿
                else color = 'rgba(239, 68, 68, 0.55)'                  // 空头 → 浅红
                return { time: p.time as Time, value: dev, color }
              }),
          )
        } else if (rsiLineRef.current && rsiHistRef.current) {
          // 数据不足 15 根 → 清空
          rsiLineRef.current.setData([])
          rsiHistRef.current.setData([])
        }

        // ── v277+ — 布林带三轨线 + Squeeze 段背景 ──
        // 计算：bollinger() 返回 { middle, upper, lower } 序列（暖机期 null）
        // bollingerSqueezeMask() 返回每根 bar 的 squeeze 状态
        if (bollUpRef.current && bollMidRef.current && bollLowRef.current && bars.length >= 20) {
          const bollBars = bars.map((b) => ({
            time: b.time,
            open: b.open,
            high: b.high,
            low: b.low,
            close: b.close,
            volume: b.volume ?? 0,
          }))
          const { middle, upper, lower } = bollinger(bollBars, 20, 2)
          // 跳过 null 暖机期
          const toLineData = (series: { time: number; value: number | null }[]) =>
            series
              .filter((p) => p.value != null)
              .map((p) => ({ time: p.time as Time, value: p.value as number }))
          bollUpRef.current.setData(toLineData(upper))
          bollMidRef.current.setData(toLineData(middle))
          bollLowRef.current.setData(toLineData(lower))
          // Squeeze mask（用于背景色 + 异常透明度）
          const squeezeMask = bollingerSqueezeMask(bollBars, 20, 2, 100, 0.2)
          setSqueezeBands(
            squeezeMask.map((s) => ({
              time: s.time,
              isSqueeze: s.isSqueeze,
              widthPct: s.widthPct,
              threshold: s.threshold,
              x: 0,
              w: 0,
            })),
          )
        } else if (bollUpRef.current && bollMidRef.current && bollLowRef.current) {
          // 数据不足 20 根 → 清空
          bollUpRef.current.setData([])
          bollMidRef.current.setData([])
          bollLowRef.current.setData([])
          setSqueezeBands([])
        }

        // v278 — Pivot Point 计算（仅缓存数值信息到 state 显示在 Header 徽章；7 条 price line 由独立 useEffect 渲染）
        if (candleRef.current && bars.length) {
          const { period, label } = pivotPeriodForTimeframe(timeframe)
          const pp = pivotPoint(bars, period)
          if (pp) {
            setPivotInfo({
              p: pp.p, r1: pp.r1, r2: pp.r2, r3: pp.r3,
              s1: pp.s1, s2: pp.s2, s3: pp.s3,
              period, label,
            })
          } else {
            setPivotInfo(null)
          }
        } else {
          setPivotInfo(null)
        }
        // Q5 拆双 ref → v221.8 拆三 ref → v221.9 按 position 拆三 ref：先 detach 旧的
        aboveMarkersRef.current?.detach()
        aboveMarkersRef.current = null
        inBarMarkersRef.current?.detach()
        inBarMarkersRef.current = null
        belowMarkersRef.current?.detach()
        belowMarkersRef.current = null

        const aboveMarks: any[] = []   // 柱上：calendar + news + signal + trade_point（HTML overlay 区分 top:4/28/52/76）
        const inBarMarks: any[] = []   // 柱内：价格结构形态（structure，因果形态识别）
        const belowMarks: any[] = []   // 柱下：仅异常点 anomaly（v304: 事件文字已全部上移）
        const newLabels: MarkerLabel[] = []

        // R15+: 价格结构形态 —— 用柱内 inBar 圆形 marker 显示
        //   因果可识别, 形态即描述, 不预报方向
        for (const s of structures) {
          const alignedT = alignTime(s.ts, bars)
          if (alignedT == null) continue
          const color = STRUCTURE_COLOR[s.name] ?? '#fbbf24'
          const shape =
            s.name === 'double_top' || s.name === 'double_bottom' ? 'square' :
            s.name === 'shooting_star' || s.name === 'hammer' ? 'arrowDown' :
            'circle'
          inBarMarks.push({
            time: alignedT as Time,
            position: 'inBar' as const,
            color,
            shape: shape as any,
            text: '',
          })
          // 颈线/关键价位: 双顶双底画水平线参考
          if (s.info?.level != null) {
            try {
              const ref = candleRef.current
              if (ref) {
                ref.createPriceLine({
                  price: s.info.level,
                  color: 'rgba(168,85,247,0.45)',
                  lineWidth: 1,
                  lineStyle: 3,                  // dashed
                  axisLabelVisible: false,
                  title: `${STRUCTURE_LABEL[s.name] ?? s.name} ${s.info.level.toFixed(2)}`,
                })
              }
            } catch { /* 价格线已存在时抛错忽略 */ }
          }
        }

        // ── v283+ 取消时间窗口过滤 ───────────────────────────────────────
        // 历史版本有 `if (ts < t0 - 3600 || ts > t1 + 3600) continue` —— 这是
        // 错误的：异常/日历/交易信号都是"有数据就保存"，不应被 K 线窗口卡掉。
        // 可视范围由 lightweight-charts 自身负责（窗口外的 marker 自动裁剪）。
        // 所以这里所有 event 循环都直接使用，不再加窗口过滤。
        // ─────────────────────────────────────────────────────────────────

        // v221.9: 同 kind+time 多个 marker 的 rowIndex 计数器（从上到下错开）
        const rowCounter = new Map<string, number>()
        const nextRow = (kind: string, time: number): number => {
          const k = `${kind}-${time}`
          const n = rowCounter.get(k) ?? 0
          rowCounter.set(k, n + 1)
          return n
        }

        if (bars.length) {
          const t0 = bars[0].time
          const t1 = bars[bars.length - 1].time

          // ── (a) 柱上方 aboveBar：📅 蓝字 日历 (star ≥ 3) ──
          for (const c of calendarEvents) {
            if (c.star == null || c.star < 3) continue
            const ts = parseEventTs(c.pub_time)
            if (ts == null) continue
            const alignedT = alignTime(ts, bars)
            const rowIndex = nextRow('calendar', alignedT)
            const fullText = `${c.star}★ ${c.title}`
            aboveMarks.push({
              time: alignedT as Time,
              position: 'aboveBar' as const,
              color: COLOR_CALENDAR,
              shape: 'circle' as const,
              text: fullText,
            })
            const labelText = truncateLabel(fullText, 14)
            if (labelText) {
              newLabels.push({
                key: `c-${alignedT}-${c.title}`,
                kind: 'calendar',
                time: alignedT,
                text: labelText,
                color: COLOR_CALENDAR,
                position: 'aboveBar',
                rowIndex,
              })
            }
          }

          // ── (b) 柱上方 aboveBar：📰 红字 高优新闻 (HOLD + vip_tag) ──
          for (const e of relatedEvents) {
            if (e.action !== 'HOLD') continue
            if (!(e.vip_tag ?? '').trim()) continue
            const ts = parseEventTs(e.timestamp) ?? parseEventTs(e.ai_time)
            if (ts == null) continue
            const alignedT = alignTime(ts, bars)
            const rowIndex = nextRow('high_news', alignedT)
            const fullText = (e.news_text ?? '').replace(/\s+/g, ' ').trim() || '高优新闻'
            aboveMarks.push({
              time: alignedT as Time,
              position: 'aboveBar' as const,
              color: COLOR_HIGH_NEWS,
              shape: 'circle' as const,
              text: e.vip_tag,
            })
            const labelText = truncateLabel(fullText, 12)
            if (labelText) {
              newLabels.push({
                key: `hn-${alignedT}-${e.news_id}`,
                kind: 'high_news',
                time: alignedT,
                text: labelText,
                color: COLOR_HIGH_NEWS,
                position: 'aboveBar',
                rowIndex,
              })
            }
          }

          // ── (c) 柱上方 aboveBar：🟢 绿点 交易信号 (BUY/SELL) ──
          // v283+ — 从 inBar 移到 aboveBar top:76：
          //   · 原 inBar HTML label 用 top:50%，实际飘在图表中线，根本没贴柱体（用户看到的"没显示"就是这个 bug）
          //   · native arrow 跟随 aboveBar 画在柱顶上方，不再挡柱体
          //   · 信号 own layer top:76（trade_point 4 / calendar 28 / high_news 52 都不冲突）
          for (const e of relatedEvents) {
            if (e.action !== 'BUY' && e.action !== 'SELL') continue
            const ts = parseEventTs(e.timestamp) ?? parseEventTs(e.ai_time)
            if (ts == null) continue
            const alignedT = alignTime(ts, bars)
            const rowIndex = nextRow('signal', alignedT)
            const fullText = (e.news_text ?? '').replace(/\s+/g, ' ').trim() || (e.action === 'BUY' ? '看涨' : '看跌')
            aboveMarks.push({
              time: alignedT as Time,
              position: 'aboveBar' as const,
              color: COLOR_SIGNAL,
              shape: e.action === 'BUY' ? ('arrowUp' as const) : ('arrowDown' as const),
              text: '',
            })
            const labelText = truncateLabel(fullText, 10)
            if (labelText) {
              newLabels.push({
                key: `s-${alignedT}-${e.news_id}`,
                kind: 'signal',
                time: alignedT,
                text: labelText,
                color: COLOR_SIGNAL,
                position: 'aboveBar',
                rowIndex,
              })
            }
          }

          // ── (c2) 柱上方 aboveBar：💜 紫字 交易点 (BUY/SELL 入场/出场价，默认隐藏) ──
          // v290+ — 与 green inBar 信号区分：
          //   · 信号绿点 = 快速识别，开盘即显示
          //   · 交易点紫字 = 详细价位（含 entry_price / exit_price），toggle 后才显示
          // 显示位置：复用 entry_time（无则回退 timestamp）
          // 显示价格：BUY → entry_price；SELL → exit_price（无则回退 entry_price）
          if (showTradePoints) {
            for (const e of relatedEvents) {
              if (e.action !== 'BUY' && e.action !== 'SELL') continue
              // 优先用 entry_time（实际成交时刻），无则回退到事件时间戳
              const ts =
                parseEventTs(e.entry_time) ??
                parseEventTs(e.timestamp) ??
                parseEventTs(e.ai_time)
              if (ts == null) continue
              const alignedT = alignTime(ts, bars)
              const rowIndex = nextRow('trade_point', alignedT)
              // 选价：BUY 看入场价；SELL 看出场价；都缺时回退到 max_price
              const price =
                e.action === 'BUY'
                  ? (e.entry_price ?? e.max_price ?? null)
                  : (e.exit_price ?? e.entry_price ?? e.min_price ?? null)
              const priceStr = price != null ? price.toFixed(2) : '—'
              const actionLabel = e.action === 'BUY' ? '多' : '空'
              // 用 news_text 简短摘要做标题，否则只看价位
              const fullText = (e.news_text ?? '').replace(/\s+/g, ' ').trim()
              const labelText = fullText
                ? `${actionLabel}@${priceStr} · ${truncateLabel(fullText, 8)}`
                : `${actionLabel}@${priceStr}`
              aboveMarks.push({
                time: alignedT as Time,
                position: 'aboveBar' as const,
                color: COLOR_TRADE_POINT,
                shape: e.action === 'BUY' ? ('arrowUp' as const) : ('arrowDown' as const),
                text: '',
              })
              newLabels.push({
                key: `tp-${alignedT}-${e.news_id}`,
                kind: 'trade_point',
                time: alignedT,
                text: labelText,
                color: COLOR_TRADE_POINT,
                position: 'aboveBar',
                rowIndex,
                tradeAction: e.action,
                tradePrice: price,
              })
            }
          }

          // ── (d) 柱上方 aboveBar：🟥 淡红字 普通新闻 ──
          //   v304 用户要求: K 线下方只放异常点, 事件内容文字一律放上方。
          //   原为 belowBar, 与异常点抢同一层导致拥挤/遮挡。
          for (const e of relatedEvents) {
            if (e.action !== 'HOLD') continue
            if ((e.vip_tag ?? '').trim()) continue
            const ts = parseEventTs(e.timestamp) ?? parseEventTs(e.ai_time)
            if (ts == null) continue
            const alignedT = alignTime(ts, bars)
            const rowIndex = nextRow('news', alignedT)
            const fullText = (e.news_text ?? '').replace(/\s+/g, ' ').trim() || '普通新闻'
            aboveMarks.push({
              time: alignedT as Time,
              position: 'aboveBar' as const,
              color: COLOR_NEWS,
              shape: 'circle' as const,
              text: '',
            })
            const labelText = truncateLabel(fullText, 12)
            if (labelText) {
              newLabels.push({
                key: `n-${alignedT}-${e.news_id}`,
                kind: 'news',
                time: alignedT,
                text: labelText,
                color: COLOR_NEWS,
                position: 'aboveBar',
                rowIndex,
              })
            }
          }

          // ── (e) 柱下方 belowBar：⭐ 彩色异动 (K 线异常，按 direction 分色) ──
          //   v304 用户要求: 所有异常点一律在 K 线下方, 不论方向/颜色;
          //   上方留给日历与事件内容文字。
          for (const a of anomalies) {
            if (a.ts == null) continue
            let alignedT = alignTime(a.ts, bars)
            let pinned = false
            let agoLabel = ''
            if (alignedT < t0) {
              alignedT = t0
              pinned = true
              const daysAgo = Math.max(1, Math.round((t0 - (a.ts as number)) / 86400))
              agoLabel = daysAgo >= 1 ? `${daysAgo}d前` : `${Math.round((t0 - (a.ts as number)) / 3600)}h前`
            } else if (alignedT > t1) {
              alignedT = t1
              pinned = true
            }
            const rowIndex = nextRow('anomaly', alignedT)
            const aColor = anomalyColor(a.direction)
            const markerText = pinned ? `${a.type}·${agoLabel}` : a.type
            belowMarks.push({
              time: alignedT as Time,
              position: 'belowBar' as const,
              color: aColor,
              shape: 'circle' as const,
              text: markerText,
            })
            newLabels.push({
              key: `a-${a.ts}-${a.indicator}`,
              kind: 'anomaly',
              time: alignedT,
              text: pinned ? agoLabel : `⭐${a.type}`,
              color: aColor,
              position: 'belowBar',
              rowIndex,
              anomalyTs: a.ts,
              anomalyDirection: a.direction,
            })
          }
        }

        // v221.9: 三个 position 各自 createSeriesMarkers
        // 先 detach 旧的 (防止 reload 累积)
        // 用局部函数而不是内联 `ref.current?.detach()`: strict 模式下 CFA 会把
        // .current 收窄成 null(下方才有非空赋值), 内联可选链会报 "detach 不存在于
        // never"。走函数参数可以把类型还原成声明的联合类型, 不丢类型信息。
        const detachMarkers = (r: { current: { detach: () => void } | null }) => {
          r.current?.detach()
          r.current = null
        }
        detachMarkers(aboveMarkersRef)
        detachMarkers(inBarMarkersRef)
        detachMarkers(belowMarkersRef)
        if (aboveMarks.length) {
          aboveMarkersRef.current = createSeriesMarkers(candleRef.current, aboveMarks)
        }
        if (inBarMarks.length) {
          inBarMarkersRef.current = createSeriesMarkers(candleRef.current, inBarMarks)
        }
        if (belowMarks.length) {
          belowMarkersRef.current = createSeriesMarkers(candleRef.current, belowMarks)
        }
        setMarkerLabels(newLabels)
        // R16+/v304: 锚定右端, 默认只显示最后 VISIBLE_BARS 根(铺满一屏)。
        //   不用 fitContent() —— 那会把全量历史(数千根)挤成一条线。
        window.requestAnimationFrame(() => {
          const ts = chartRef.current?.timeScale()
          if (!ts) return
          const total = bars.length
          if (total === 0) {
            ts.fitContent()
            return
          }
          const visible = Math.min(total, VISIBLE_BARS)
          ts.setVisibleLogicalRange({ from: total - visible - 4, to: total - 1 })
        })
      })
      .catch(() => {
        if (alive) setError('K 线加载失败')
      })
    return () => {
      alive = false
    }
  }, [market, relatedEvents, calendarEvents, chartReady, isDark, timeframe, showTradePoints])

  // Q5: crosshair hover 提示 —— 当前 bar 上若有事件/异常，弹全 news_text / 异常详情
  useEffect(() => {
    if (!chartReady || !chartRef.current) return
    const chart = chartRef.current
    const handler = (param: any) => {
      // 鼠标移出图表或无时间点 → 隐藏
      if (!param || !param.time || param.point == null) {
        setHoverTip(null)
        return
      }
      const t = Number(param.time)
      const px = param.point.x as number
      const py = param.point.y as number
      const bars = chartBars
      if (!bars.length) {
        setHoverTip(null)
        return
      }
      // 找事件（精确匹配对齐后的时间戳）
      // v221.9: 五类 kind — 优先查 calendar，再 high_news，再 signal，再 news，最后 anomaly
      // ① 日历事件
      for (const c of calendarEvents) {
        if (c.star == null || c.star < 3) continue
        const ts = parseEventTs(c.pub_time)
        if (ts == null) continue
        if (alignTime(ts, bars) === t) {
          setHoverTip({
            kind: 'calendar',
            title: `${c.star}★ ${c.title}`,
            body: [
              `公布时间：${c.pub_time}`,
              `预测：${c.consensus ?? '—'}  前值：${c.previous ?? '—'}  实际：${c.actual ?? '—'}`,
            ].join('\n'),
            x: px,
            y: py,
            star: c.star ?? null,
          })
          return
        }
      }
      // ② 高优新闻（HOLD + vip_tag）
      for (const e of relatedEvents) {
        if (classifyEvent(e) !== 'high_news') continue
        const ts = parseEventTs(e.timestamp) ?? parseEventTs(e.ai_time)
        if (ts == null) continue
        if (alignTime(ts, bars) === t) {
          const body = (e.news_text ?? '').replace(/\s+/g, ' ').trim()
          if (!body) continue
          setHoverTip({
            kind: 'high_news',
            title: `${e.vip_tag} · 高优新闻`,
            body,
            x: px,
            y: py,
            vipTag: e.vip_tag,
          })
          return
        }
      }
      // ③ 交易信号（BUY/SELL）
      for (const e of relatedEvents) {
        if (classifyEvent(e) !== 'signal') continue
        const ts = parseEventTs(e.timestamp) ?? parseEventTs(e.ai_time)
        if (ts == null) continue
        if (alignTime(ts, bars) === t) {
          const body = (e.news_text ?? '').replace(/\s+/g, ' ').trim()
          if (!body) continue
          setHoverTip({
            kind: 'signal',
            title: e.action === 'BUY' ? '🟢 看涨信号' : '🟢 看跌信号',
            body,
            x: px,
            y: py,
            action: e.action as 'BUY' | 'SELL',
          })
          return
        }
      }
      // ④ 普通新闻（HOLD 无 vip）
      for (const e of relatedEvents) {
        if (classifyEvent(e) !== 'news') continue
        const ts = parseEventTs(e.timestamp) ?? parseEventTs(e.ai_time)
        if (ts == null) continue
        if (alignTime(ts, bars) === t) {
          const body = (e.news_text ?? '').replace(/\s+/g, ' ').trim()
          if (!body) continue
          setHoverTip({
            kind: 'news',
            title: '📰 普通新闻',
            body,
            x: px,
            y: py,
          })
          return
        }
      }
      // ⑤ 异常（⭐ 可点按 AI 归因）
      for (const a of chartAnomalies) {
        if (a.ts == null) continue
        if (alignTime(a.ts, bars) === t) {
          setHoverTip({
            kind: 'anomaly',
            title: `⭐ ${a.type}`,
            body: `方向: ${a.direction === 'bullish' ? '看涨 ↑' : a.direction === 'bearish' ? '看跌 ↓' : '中性'} · 指标: ${a.indicator}\n(点按 marker 触发 AI 归因)`,
            direction: a.direction,
            x: px,
            y: py,
          })
          return
        }
      }
      // ⑥ 交易点（💜 仅 showTradePoints=true 时才查找）
      // v290+ — 用 entry_time 定位；BUY 取 entry_price，SELL 取 exit_price
      if (showTradePoints) {
        for (const e of relatedEvents) {
          if (e.action !== 'BUY' && e.action !== 'SELL') continue
          const ts =
            parseEventTs(e.entry_time) ??
            parseEventTs(e.timestamp) ??
            parseEventTs(e.ai_time)
          if (ts == null) continue
          if (alignTime(ts, bars) === t) {
            const price =
              e.action === 'BUY'
                ? (e.entry_price ?? e.max_price ?? null)
                : (e.exit_price ?? e.entry_price ?? e.min_price ?? null)
            const body = (e.news_text ?? '').replace(/\s+/g, ' ').trim()
            setHoverTip({
              kind: 'trade_point',
              title: e.action === 'BUY' ? '买入信号' : '卖出信号',
              body: body || `入场/出场时间：${e.entry_time ?? e.timestamp ?? '—'}`,
              x: px,
              y: py,
              tradeAction: e.action,
              tradePrice: price,
            })
            return
          }
        }
      }
      setHoverTip(null)
    }
    chart.subscribeCrosshairMove(handler)
    return () => {
      try {
        chart.unsubscribeCrosshairMove(handler)
      } catch {
        // chart 已销毁，忽略
      }
    }
  }, [chartReady, relatedEvents, calendarEvents, chartBars, chartAnomalies, showTradePoints])

  // Q7+：浮层标签位置实时换算 —— 监听 chart.timeScale() 可见范围变化 + crosshair 移动，
  // 把每个 markerLabel.time 映射成像素 x。y 用固定 top/bottom 偏移（事件贴顶、异常贴底），
  // 避免与价格线耦合（事件无 price）。
  useEffect(() => {
    if (!chartReady || !chartRef.current) return
    const chart = chartRef.current
    const ts = chart.timeScale()
    const recompute = () => {
      if (!markerLabels.length) {
        setLabelPositions({})
        return
      }
      const next: Record<string, { x: number; y: number }> = {}
      for (const lbl of markerLabels) {
        const x = ts.timeToCoordinate(lbl.time as Time)
        if (x == null) continue
        // y 用 CSS 锚定：事件 top:14px 贴顶、异常 bottom:14px 贴底
        next[lbl.key] = { x, y: 0 }
      }
      setLabelPositions(next)
    }
    // 初始化一次
    recompute()
    // 订阅可见时间范围变化（zoom/pan 触发）
    const handler = () => recompute()
    ts.subscribeVisibleTimeRangeChange(handler)
    return () => {
      try {
        ts.unsubscribeVisibleTimeRangeChange(handler)
      } catch {
        // chart 已销毁
      }
    }
  }, [chartReady, markerLabels])

  // v277+ — Squeeze 段坐标实时换算（X 像素 + 宽度）
  //   每根 bar 一段，叠加在主图底层做半透明背景色
  useEffect(() => {
    if (!chartReady || !chartRef.current) return
    const chart = chartRef.current
    const ts = chart.timeScale()
    const recomputeSqueeze = () => {
      if (!squeezeBands.length) {
        setSqueezePositions({})
        return
      }
      const next: Record<number, { x: number; w: number }> = {}
      for (let i = 0; i < squeezeBands.length; i++) {
        const s = squeezeBands[i]
        const x = ts.timeToCoordinate(s.time as Time)
        if (x == null) continue
        // 计算 bar 宽度：相邻 bar 的中点距离 / 2
        const nextBar = squeezeBands[i + 1]
        const nextX = nextBar ? ts.timeToCoordinate(nextBar.time as Time) : null
        let w: number
        if (nextX != null) {
          w = Math.max(2, (nextX - x) / 2) // 半宽
        } else {
          // 最后一段：使用前一段的宽度
          const prev = squeezeBands[i - 1]
          const prevX = prev ? ts.timeToCoordinate(prev.time as Time) : null
          w = prevX != null ? Math.max(2, x - prevX) : 6
        }
        next[s.time] = { x, w }
      }
      setSqueezePositions(next)
    }
    recomputeSqueeze()
    const handler = () => recomputeSqueeze()
    ts.subscribeVisibleTimeRangeChange(handler)
    return () => {
      try {
        ts.unsubscribeVisibleTimeRangeChange(handler)
      } catch {
        // chart 已销毁
      }
    }
  }, [chartReady, squeezeBands])

  // v277+ — BOLL 系列可见性切换
  useEffect(() => {
    if (!chartReady) return
    const vis = showBoll
    bollUpRef.current?.applyOptions({ visible: vis })
    bollMidRef.current?.applyOptions({ visible: vis })
    bollLowRef.current?.applyOptions({ visible: vis })
  }, [chartReady, showBoll])

  // v278 — Pivot Point 7 条价格线可见性切换
  //   showPivot=true  → 重建 7 条线（P 中心实线 + S1-S3/R1-R3 虚线）
  //   showPivot=false → 全部 remove（彻底从主图消失，不占 GPU）
  //   依赖 chartBars（state 形式持有的全量 K 线），toggle 时不需要重拉数据
  useEffect(() => {
    if (!chartReady || !candleRef.current) return
    const c = candleRef.current
    // 1) 清理旧 PriceLine
    for (const k of Object.keys(pivotLinesRef.current)) {
      const ln = pivotLinesRef.current[k]
      if (ln) {
        try { c.removePriceLine(ln) } catch {}
        pivotLinesRef.current[k] = null
      }
    }
    // 2) showPivot 关闭 → 完事
    if (!showPivot) return
    // 3) 重新算 + 创建
    if (!chartBars.length) return
    const { period, label } = pivotPeriodForTimeframe(timeframe)
    const pp = pivotPoint(chartBars, period)
    if (!pp) return
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
  }, [chartReady, showPivot, timeframe, chartBars])

  // v278 — 实时订单簿深度轮询（showBook=true 时每 10s 拉一次；后端缓存 10s，去重 OK）
  useEffect(() => {
    if (!showBook) {
      // toggle 关：清空数据 + 状态
      setOrderbook(null)
      setOrderbookStatus('idle')
      return
    }
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    const load = async () => {
      setOrderbookStatus('loading')
      try {
        const r = await fetch(
          `${API_BASE}/market/depth/${encodeURIComponent(meta.asset)}?limit=50`,
          { cache: 'no-store' },
        )
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const j = await r.json()
        if (!alive) return
        if (j?.status === 'ok' && Array.isArray(j.bids) && Array.isArray(j.asks)) {
          setOrderbook({
            bids: (j.bids as [string | number, string | number][]).map(([p, q]) => [Number(p), Number(q)]),
            asks: (j.asks as [string | number, string | number][]).map(([p, q]) => [Number(p), Number(q)]),
            ts_ms: Number(j.ts_ms) || Date.now(),
            source: String(j.source || ''),
          })
          setOrderbookStatus('ok')
        } else {
          setOrderbook(null)
          setOrderbookStatus('unavailable')
        }
      } catch {
        if (!alive) return
        setOrderbook(null)
        setOrderbookStatus('unavailable')
      }
      // 下一轮（仅在 alive 状态）
      if (alive) timer = setTimeout(load, 10_000)
    }
    load()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [showBook, meta.asset])

  // v277+ — 暴露"每根 bar 是否处于 squeeze 期"的快速查询 Map（marker 渲染时用）
  const squeezeByTime = useMemo(() => {
    const m = new Map<number, { isSqueeze: boolean; widthPct: number | null; threshold: number | null }>()
    for (const s of squeezeBands) {
      m.set(s.time, { isSqueeze: s.isSqueeze, widthPct: s.widthPct, threshold: s.threshold })
    }
    return m
  }, [squeezeBands])

  // v277+ — 当前 squeeze 期 bar 数（避免 onClick 里用陈旧闭包）
  const squeezeBarCount = useMemo(() => squeezeBands.filter((s) => s.isSqueeze).length, [squeezeBands])

  return (
    <section
      className="flex h-full flex-col overflow-hidden rounded-lg border bg-card"
      style={{ borderColor: `${meta.accent}66` }}
    >
      {/* 品种顶部色带（一眼分辨品种底色） */}
      <div className="h-1 shrink-0" style={{ backgroundColor: meta.accent }} />

      {/* Header：品种标题 + 强调色徽章 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-2.5">
        <TrendingUp className="h-4 w-4" style={{ color: meta.accent }} aria-hidden="true" />
        <h2 className="text-sm font-semibold text-foreground">
          {meta.label} · {timeframe.toUpperCase()} K线
        </h2>
        <div className="flex items-center gap-0.5 rounded border border-border bg-card p-0.5" role="group" aria-label="K线周期">
          {(['3m', '5m', '10m'] as const).map((tf) => (
            <button
              key={tf}
              type="button"
              onClick={() => setTimeframe(tf)}
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
                timeframe === tf ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-secondary'
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
        {/* v304: K 线已改为全量历史加载 + 可缩放回看, 异常点累积保存(永不删除) */}
        <span
          className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
          title={`全量 K 线历史已加载（最多 ${HISTORY_LIMIT} 根），可缩放/拖动回看；异常点来自 anomaly_history 累积记录，扫描窗口 ${ANOMALY_WINDOW_DAYS} 天`}
        >
          全量历史 · 异常累积
        </span>

        {/* v277+ — 布林带主图叠加 toggle */}
        <button
          type="button"
          onClick={() => setShowBoll((v) => !v)}
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] transition-colors ${
            showBoll
              ? 'bg-blue-500/20 text-blue-700 dark:text-blue-300'
              : 'bg-secondary text-muted-foreground'
          }`}
          title={showBoll ? '点击隐藏布林带（蓝/黄三轨线）' : '点击显示布林带（识别缩量盘整期）'}
        >
          📊 BOLL
        </button>

        {/* v277+ — 滤布林带噪音 toggle：开启后自动隐藏 squeeze 期的异动 marker */}
        <button
          type="button"
          onClick={() => setFilterNoise((v) => !v)}
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] transition-colors ${
            filterNoise
              ? 'bg-amber-500/30 text-amber-700 dark:text-amber-300 ring-1 ring-amber-500/50'
              : 'bg-secondary text-muted-foreground'
          }`}
          title={
            filterNoise
              ? '已开启：自动隐藏布林带收窄期（缩量盘整）的异动 marker，过滤噪音'
              : '点击开启：自动隐藏布林带收窄期（缩量盘整）的异动 marker'
          }
        >
          🎯 滤布林噪音
          {filterNoise && squeezeBarCount > 0 && (
            <span className="ml-1 rounded bg-amber-500/40 px-1 text-[9px] text-foreground">
              -{squeezeBarCount}
            </span>
          )}
        </button>

        {/* v278 — Pivot Point 技术支撑阻力位 toggle（TradingView 同款，默认开） */}
        <button
          type="button"
          onClick={() => setShowPivot((v) => !v)}
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] transition-colors ${
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

        {/* v278 — 实时订单簿深度 toggle（合约挂单堆积柱状图） */}
        <button
          type="button"
          onClick={() => setShowBook((v) => !v)}
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] transition-colors ${
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
            <span className="ml-1 rounded bg-rose-500/40 px-1 text-[9px] text-foreground">
              不可用
            </span>
          )}
        </button>

        {/* v290+ — 交易点 toggle（默认关）：开启后柱上方紫字显示 BUY/SELL 入场/出场价 */}
        {(() => {
          const tpCount = relatedEvents.filter(
            (e) => e.action === 'BUY' || e.action === 'SELL'
          ).length
          return (
            <button
              type="button"
              onClick={() => setShowTradePoints((v) => !v)}
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] transition-colors ${
                showTradePoints
                  ? 'bg-purple-500/25 text-purple-700 dark:text-purple-300 ring-1 ring-purple-500/50 font-semibold'
                  : 'bg-secondary text-muted-foreground'
              }`}
              title={
                showTradePoints
                  ? `已开启：上方紫字显示 ${tpCount} 个交易点（BUY 多 / SELL 空 + 入场/出场价）—— 再点关闭`
                  : `点击开启：在 K 线上方显示交易点（紫字 BUY 入场价 / SELL 出场价，含简短摘要）`
              }
            >
              💜 交易点
              {showTradePoints && tpCount > 0 && (
                <span className="ml-1 rounded bg-purple-500/40 px-1 text-[9px] text-foreground">
                  {tpCount}
                </span>
              )}
            </button>
          )
        })()}
        {/* v221.9: 五色徽章 —— 蓝(日历) / 红(高优) / 绿(信号) / 淡红(新闻) / 彩色(异常) */}
        {calendarEvents.length > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px]"
            style={{ color: COLOR_CALENDAR, backgroundColor: 'rgba(59,130,246,0.12)' }}
            title={`过去 7 天内 ${calendarEvents.length} 个 3星以上日历事件（蓝字 aboveBar）`}
          >
            📅 {calendarEvents.length} 日历
          </span>
        )}
        {relatedEvents.filter((e) => classifyEvent(e) === 'high_news').length > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px]"
            style={{ color: COLOR_HIGH_NEWS, backgroundColor: 'rgba(239,68,68,0.12)' }}
            title={`${relatedEvents.filter((e) => classifyEvent(e) === 'high_news').length} 个高优新闻（HOLD + vip_tag，红字 aboveBar）`}
          >
            📰 {relatedEvents.filter((e) => classifyEvent(e) === 'high_news').length} 高优
          </span>
        )}
        {relatedEvents.filter((e) => e.action === 'BUY' || e.action === 'SELL').length > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px]"
            style={{ color: COLOR_SIGNAL, backgroundColor: 'rgba(34,197,94,0.12)' }}
            title={`${relatedEvents.filter((e) => e.action === 'BUY' || e.action === 'SELL').length} 个交易信号（BUY/SELL，绿点 inBar）`}
          >
            🟢 {relatedEvents.filter((e) => e.action === 'BUY' || e.action === 'SELL').length} 信号
          </span>
        )}
        {relatedEvents.filter((e) => classifyEvent(e) === 'news').length > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px]"
            style={{ color: COLOR_NEWS, backgroundColor: 'rgba(252,165,165,0.12)' }}
            title={`${relatedEvents.filter((e) => classifyEvent(e) === 'news').length} 个普通新闻（HOLD 无 vip，淡红字 belowBar）`}
          >
            🗞️ {relatedEvents.filter((e) => classifyEvent(e) === 'news').length} 新闻
          </span>
        )}
        {chartAnomalies.length > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px]"
            style={{ background: 'linear-gradient(90deg, rgba(34,197,94,0.12), rgba(239,68,68,0.12), rgba(251,191,36,0.12))' }}
            title={`累积历史共 ${chartAnomalies.length} 个异常点（来自 anomaly_history，永不删除；按 direction 分色，⭐ 可点按 AI 归因）`}
          >
            <span style={{ color: COLOR_ANOM_BULL }}>⭐</span> {chartAnomalies.length} 异动
          </span>
        )}
        <span
          className="ml-auto inline-flex items-center rounded-full px-2 py-0.5 font-mono text-[10px]"
          style={{ color: meta.accent, backgroundColor: meta.badge }}
        >
          {meta.name}
        </span>
      </div>

      {/* Chart area */}
      <div className="relative min-h-0 flex-1">
        {missing && (
          <div className="absolute inset-x-0 top-0 z-10 mx-2 mt-2 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 font-mono text-[11px] text-amber-500">
            {missing}
          </div>
        )}
        {error && (
          <div className="absolute inset-x-0 top-0 z-10 mx-2 mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 font-mono text-[11px] text-short">
            {error}
          </div>
        )}
        {/* 品种左下角水印 */}
        <div
          className="pointer-events-none absolute left-3 top-2 z-10 rounded px-2 py-0.5 font-mono text-[10px] font-bold"
          style={{ color: meta.accent, backgroundColor: meta.badge, border: `1px solid ${meta.accent}55` }}
        >
          {meta.label}
        </div>
        <div ref={containerRef} className="h-full w-full" />

        {/* v278 — 实时订单簿深度 overlay（右侧 90px 宽柱状堆积图）
            · 卖盘（asks）→ 价格低 → 高，从上到下画，红色；挂单位置 = 阻力区
            · 买盘（bids）→ 价格低 → 高，从上到下画，绿色；挂单位置 = 支撑区
            · 用 candleRef.priceToCoordinate(price) 把价格映射到 Y 坐标
            · 每档宽度按 qty 归一化到最大档
            · z-[4] 在 squeeze(z-3) 之上、markers(z-5) 之下，避免遮挡文字 */}
        {showBook && orderbook && chartReady && candleRef.current && (
          <OrderbookOverlay
            orderbook={orderbook}
            series={candleRef.current}
            source={orderbook.source}
          />
        )}

        {/* v278 — Pivot 主图左内侧位置标注：每条价格线在主图上画一个彩色标签
            标签内容：P/S1-S3/R1-R3 + 价格 + role
            位置：用 series.priceToCoordinate 把价格映射到 Y，左侧贴边
            颜色跟价格线一致（橙 P / 绿 R / 红 S），透明度按当前 Y 是否在可见范围动态调
            z-[3] 在 squeeze 之上、book 之下，避免遮挡买卖盘 */}
        {showPivot && pivotInfo && chartReady && candleRef.current && (
          <PivotLabelsOverlay
            pivot={pivotInfo}
            series={candleRef.current}
          />
        )}

        {/* v277+ — Squeeze 段背景色：把布林带收窄期（缩量盘整）的 bar 用半透明灰底标出来
            z-index 介于 chart 与 marker 之间（z-1），不遮挡 K 线但可见 */}
        {squeezeBands.length > 0 && Object.keys(squeezePositions).length > 0 && (
          <div className="pointer-events-none absolute inset-x-0 top-0 z-[3] h-full">
            {squeezeBands.map((s) => {
              const pos = squeezePositions[s.time]
              if (!pos || !s.isSqueeze) return null
              return (
                <div
                  key={`sq-${s.time}`}
                  className="absolute top-0 h-full"
                  style={{
                    left: `${pos.x - pos.w}px`,
                    width: `${pos.w * 2}px`,
                    background: COLOR_SQUEEZE_BG,
                    borderLeft: '1px dashed rgba(148, 163, 184, 0.4)',
                    borderRight: '1px dashed rgba(148, 163, 184, 0.4)',
                  }}
                  title={
                    s.widthPct != null && s.threshold != null
                      ? `布林带收窄期：宽度 ${s.widthPct.toFixed(2)}% ≤ 历史 20% 分位阈值 ${s.threshold.toFixed(2)}%（缩量盘整）`
                      : '布林带收窄期（缩量盘整）'
                  }
                />
              )
            })}
          </div>
        )}

        {/* Q7+: 浮层 marker 标签 —— lightweight-charts v5 createSeriesMarkers 不渲染 text，
            用 HTML overlay 把每个事件/异常的文字内容显式标到对应 bar 旁边。
            v283+ 位置规则（与文件顶部注释一致）：
              · 交易点 (trade_point) aboveBar top:4  + rowIndex*24   (默认隐藏)
              · 日历   (calendar)    aboveBar top:28 + rowIndex*24
              · 高优新闻(high_news)  aboveBar top:52 + rowIndex*24
              · 交易信号(signal)     aboveBar top:76 + rowIndex*24   (v283+ 从 inBar 上移)
              · 普通新闻(news)       aboveBar top:100+ rowIndex*24   (v304 从下方上移, 独占第 5 层)
              · 异常   (anomaly)     belowBar bottom:20 + rowIndex*24 (v304: 下方独占, 可点按 AI 归因)
            v283+/v304 硬约束：文字标签严格在 aboveBar / belowBar 两区，柱体本身永远干净。
              v304 用户要求：K 线下方只放异常点，事件内容文字（日历/新闻/信号）一律在上方。
              ① 文字用事件本身颜色（lbl.color）
              ② 容器 pointer-events: none，仅异常可点击
              ③ 堆叠 >3 折叠为 "+N more" chip，不再向柱体方向扩张 */}
        <div className="pointer-events-none absolute inset-0 z-[5] overflow-hidden">
          {markerLabels.map((lbl) => {
            const pos = labelPositions[lbl.key]
            if (!pos) return null

            // ── v283+ 硬约束 #3：堆叠 >3 折叠为 +N more chip ─────────────
            // 同 kind+time+position 超过 3 个时，第 4 个起折叠为单个 "+N more"，
            // 绝对不允许向柱体方向继续扩张（避免进入 K 线柱体造成视觉遮挡）。
            // 注意 rowIndex 是按 kind+time 计数，所以这里再加 position 维度区分上下层。
            if (lbl.rowIndex >= 3) {
              const groupKey = `${lbl.kind}-${lbl.time}-${lbl.position}`
              const groupItems = markerLabels.filter(
                (x) => `${x.kind}-${x.time}-${x.position}` === groupKey && x.rowIndex >= 3,
              )
              // 只渲染组内第一个折叠项（避免重复 "+N more" chip）
              const firstIdx = markerLabels.findIndex(
                (x) => `${x.kind}-${x.time}-${x.position}` === groupKey && x.rowIndex >= 3,
              )
              if (markerLabels.indexOf(lbl) !== firstIdx) return null
              // 计算折叠 chip 的位置（与该组第 4 个 label 同位置 = 紧贴第 3 个之后）
              let chipLayerStyle: React.CSSProperties
              if (lbl.position === 'aboveBar') {
                let baseTop = 28
                if (lbl.kind === 'trade_point') baseTop = 4
                else if (lbl.kind === 'high_news') baseTop = 52
                else if (lbl.kind === 'signal') baseTop = 76
                // v304: news(事件内容文字) 已上移, 独占第 5 层, 不与日历抢 top:28
                else if (lbl.kind === 'news') baseTop = 100
                chipLayerStyle = { top: `${baseTop + 3 * 24}px`, bottom: 'auto' }
              } else {
                // v304: 下方只剩 anomaly → 统一 bottom:20
                chipLayerStyle = { bottom: `${20 + 3 * 24}px`, top: 'auto' }
              }
              return (
                <div
                  key={`collapsed-${groupKey}`}
                  className="absolute whitespace-nowrap rounded-md px-2 py-0.5 font-mono text-[10px] font-bold shadow-sm ring-1 ring-black/5 pointer-events-auto"
                  style={{
                    color: '#6b7280',
                    backgroundColor: 'rgba(107,114,128,0.18)',
                    border: '1px dashed #6b7280',
                    transform: 'translateX(-50%)',
                    left: `${pos.x}px`,
                    ...chipLayerStyle,
                  }}
                  title={`还有 ${groupItems.length} 个同类事件因堆叠超过 3 行被折叠（点击展开 / 缩放查看完整）`}
                >
                  ＋{groupItems.length} more
                </div>
              )
            }

            // v277+ — 布林带 Squeeze 期异常 marker 噪音处理：
            //   · filterNoise 开启 → 完全隐藏（视为噪音剔除）
            //   · filterNoise 关闭 → 透明度降到 40% + tooltip 提示
            if (lbl.kind === 'anomaly') {
              const sq = squeezeByTime.get(lbl.time)
              if (sq?.isSqueeze) {
                if (filterNoise) return null // 滤噪：直接隐藏
                // 弱化：40% 透明度
                // 用 style.opacity 覆盖，不依赖 Tailwind class
                // v222 — 与普通异常分支保持一致的间距：bottom 20 + rowIndex * 24
                return (
                  <div
                    key={lbl.key}
                    className={`absolute whitespace-nowrap rounded-md px-2 py-1 font-mono text-[11px] font-bold leading-snug shadow-sm`}
                    style={{
                      color: lbl.color,
                      border: `1px dashed ${lbl.color}`,
                      opacity: 0.4,
                      transform: 'translateX(-50%)',
                      left: `${pos.x}px`,
                      bottom: `${20 + lbl.rowIndex * 24}px`,
                      top: 'auto',
                      maxWidth: '200px',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      pointerEvents: 'auto',
                      cursor: lbl.anomalyTs != null ? 'pointer' : 'default',
                    }}
                    title={
                      sq.widthPct != null && sq.threshold != null
                        ? `⚠️ 此异动处于布林带收窄期（宽度 ${sq.widthPct.toFixed(2)}% ≤ 20% 分位 ${sq.threshold.toFixed(2)}%），疑似噪音。\n点击触发 AI 归因`
                        : '⚠️ 此异动处于布林带收窄期，疑似噪音'
                    }
                    onClick={lbl.anomalyTs != null ? () => triggerAiAttribution(lbl.anomalyTs!, lbl.text) : undefined}
                  >
                    ⚠️🔇 {lbl.text}
                  </div>
                )
              }
            }
            // 每个 kind 用不同 emoji 前缀：
            //   📅 日历  📈 信号（向上 BUY / 向下 SELL）  ◆ 异常  💜 交易点
            const prefix =
              lbl.kind === 'calendar' ? '📅'
                : lbl.kind === 'high_news' ? '📰'
                  : lbl.kind === 'signal' ? '🟢'
                    : lbl.kind === 'news' ? '🗞️'
                      : lbl.kind === 'trade_point'
                        ? (lbl.tradeAction === 'BUY' ? '💜⬆' : '💜⬇')
                        : '⭐'
            // 计算 y 位置：belowBar 贴底；aboveBar 按 kind 分层（calendar 上、event 下），同 kind 内 rowIndex 错开
            // v290+ — 交易点放最上（top:4）单独一层，避免和日历/高优新闻挤压
            // v222 — 加大行间距（原 16 → 24）和基础偏移（原 14/32 → 20/44），避免标签互相挤压
            let layerStyle: React.CSSProperties
            if (lbl.position === 'inBar') {
              // 柱内居中
              layerStyle = { top: '50%', transform: 'translate(-50%, -50%)', bottom: 'auto' }
            } else if (lbl.position === 'aboveBar') {
              // 柱上方四层（自上而下）：
              //   trade_point  →  top: 4    （最顶，紫·交易点）
              //   calendar     →  top: 28   （蓝·日历）
              //   high_news    →  top: 52   （红·高优）
              //   signal       →  top: 76   （绿·交易信号，原 inBar 已上移避免挡柱体）
              let baseTop = 28
              if (lbl.kind === 'trade_point') baseTop = 4
              else if (lbl.kind === 'high_news') baseTop = 52
              else if (lbl.kind === 'signal') baseTop = 76
              // v304: news(事件内容文字) 独占第 5 层 top:100
              else if (lbl.kind === 'news') baseTop = 100
              layerStyle = { top: `${baseTop + lbl.rowIndex * 24}px`, bottom: 'auto' }
            } else {
              // v304: 柱下方只剩异常点 → 统一 bottom:20
              layerStyle = { bottom: `${20 + lbl.rowIndex * 24}px`, top: 'auto' }
            }
            // 异常 marker 加 cursor: pointer + onClick → AI 归因
            const isAnomaly = lbl.kind === 'anomaly'
            const isClickable = isAnomaly && lbl.anomalyTs != null
            // v222 — 标签按 kind 同色背景（color + 18% alpha）+ 同色边框，让蓝/绿/红一眼可辨
            // v283+ 文字用事件本身颜色（用户要求：文字颜色 = 事件颜色）
            // 将 hex 转 rgba 用于半透明背景
            const hexToRgba = (hex: string, alpha: number): string => {
              const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex)
              if (!m) return hex
              const r = parseInt(m[1], 16), g = parseInt(m[2], 16), b = parseInt(m[3], 16)
              return `rgba(${r},${g},${b},${alpha})`
            }
            const labelBg = hexToRgba(lbl.color, 0.18)         // 18% 同色填充（事件/日历）
            const labelBgSolid = hexToRgba(lbl.color, 0.92)     // 异常用更深背景（信号已移到 aboveBar，用 18%）
            const bgStyle = isAnomaly ? labelBgSolid : labelBg
            const textColor = lbl.color                          // 文字用事件本身颜色（所有 kind 一致）
            // 用 bg 透明 + 边框，避免遮挡 K 线；最多 ~24 字符，超出 width 截断
            return (
              <div
                key={lbl.key}
                className={`absolute whitespace-nowrap rounded-md px-2 py-1 font-mono text-[11px] font-bold leading-snug shadow-md ring-1 ring-black/5 ${
                  isClickable ? 'pointer-events-auto cursor-pointer hover:scale-110 transition-transform' : ''
                }`}
                style={{
                  color: textColor,
                  backgroundColor: bgStyle,
                  border: `1px solid ${lbl.color}`,
                  // 把标签居中在 marker 上方/下方
                  transform: lbl.position === 'inBar' ? 'translate(-50%, -50%)' : 'translateX(-50%)',
                  left: `${pos.x}px`,
                  ...layerStyle,
                  maxWidth: '200px',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
                title={isClickable ? `点击触发 AI 归因（${lbl.text}）` : lbl.text}
                onClick={isClickable ? () => triggerAiAttribution(lbl.anomalyTs!, lbl.text) : undefined}
              >
                {prefix} {lbl.text}
              </div>
            )
          })}
        </div>

        {/* v221.9: AI 归因结果面板 —— 点击 ⭐ 异常 marker 触发 */}
        {aiAttrib && (
          <div
            className="pointer-events-auto absolute right-3 top-3 z-30 max-h-[80%] w-[420px] overflow-auto rounded-lg border bg-card/95 p-3 shadow-2xl backdrop-blur"
            style={{ borderColor: 'rgba(251,191,36,0.7)' }}
            role="dialog"
          >
            <div className="mb-2 flex items-center justify-between">
              <span className="font-mono text-xs font-bold text-amber-400">
                ⭐ AI 归因 · ts={aiAttrib.ts}
              </span>
              <button
                type="button"
                onClick={() => setAiAttrib(null)}
                className="rounded px-2 py-0.5 text-xs text-muted-foreground hover:bg-secondary"
              >
                ✕ 关闭
              </button>
            </div>
            {aiAttrib.loading ? (
              <div className="py-4 text-center font-mono text-xs text-muted-foreground">
                AI 正在分析异动原因……
              </div>
            ) : aiAttrib.error ? (
              <div className="font-mono text-xs text-red-400">错误：{aiAttrib.error}</div>
            ) : (
              <div className="font-mono text-[11px] leading-relaxed text-foreground">
                <pre className="whitespace-pre-wrap break-words">
                  {typeof aiAttrib.data === 'string'
                    ? aiAttrib.data
                    : JSON.stringify(aiAttrib.data, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}

        {/* Q5: hover tooltip —— 鼠标停在事件/异常 marker 对应 bar 时显示完整新闻内容 */}
        {hoverTip && (
          <div
            className="pointer-events-none absolute z-20 max-w-[420px] rounded border bg-card/95 px-3 py-2 font-mono text-[11px] shadow-lg backdrop-blur"
            style={{
              left: Math.min(hoverTip.x + 12, 1000),
              top: Math.max(hoverTip.y - 8, 0),
              // v221.9 五色 tooltip 边框 → v290+ 加 trade_point（紫）
              borderColor:
                hoverTip.kind === 'calendar'
                  ? 'rgba(59,130,246,0.7)'    // 蓝
                  : hoverTip.kind === 'high_news'
                    ? 'rgba(239,68,68,0.7)'   // 红
                    : hoverTip.kind === 'signal'
                      ? 'rgba(34,197,94,0.7)' // 绿
                      : hoverTip.kind === 'news'
                        ? 'rgba(252,165,165,0.7)' // 淡红
                        : hoverTip.kind === 'anomaly'
                          ? 'rgba(251,191,36,0.7)' // 金（异常）
                          : hoverTip.kind === 'trade_point'
                            ? 'rgba(168,85,247,0.85)' // 紫（交易点）
                            : 'rgba(154,163,175,0.7)',
            }}
            role="tooltip"
          >
            <div
              className="mb-1 font-bold"
              style={{
                // v221.9 五色 tooltip 标题 → v290+ 加 trade_point（紫）
                color:
                  hoverTip.kind === 'calendar'
                    ? 'rgb(59,130,246)'
                    : hoverTip.kind === 'high_news'
                      ? 'rgb(239,68,68)'
                      : hoverTip.kind === 'signal'
                        ? 'rgb(34,197,94)'
                        : hoverTip.kind === 'news'
                          ? 'rgb(252,165,165)'
                          : hoverTip.kind === 'anomaly'
                            ? 'rgb(251,191,36)'
                            : hoverTip.kind === 'trade_point'
                              ? 'rgb(168,85,247)'
                              : 'rgb(154,163,175)',
              }}
            >
              {hoverTip.kind === 'calendar' ? '📅 日历 · '
                : hoverTip.kind === 'high_news' ? '📰 高优 · '
                  : hoverTip.kind === 'signal' ? '🟢 信号 · '
                    : hoverTip.kind === 'news' ? '🗞️ 新闻 · '
                      : hoverTip.kind === 'trade_point'
                        ? (hoverTip.tradeAction === 'BUY' ? '💜 多 · ' : '💜 空 · ')
                        : '⭐ 异常 · '}
              {hoverTip.title}
              {hoverTip.kind === 'trade_point' && hoverTip.tradePrice != null && (
                <span className="ml-2 rounded bg-purple-500/20 px-1.5 text-purple-700 dark:text-purple-300">
                  @ {hoverTip.tradePrice.toFixed(2)}
                </span>
              )}
            </div>
            <div className="whitespace-pre-wrap break-words leading-snug text-foreground">
              {hoverTip.body}
            </div>
          </div>
        )}
      </div>

      {/* v221.9 图例：三行五类事件 */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-border px-3 py-1.5 font-mono text-[10px] text-muted-foreground">
        <span style={{ color: COLOR_CALENDAR }}>📅 柱顶·日历（≥3星）</span>
        <span style={{ color: COLOR_HIGH_NEWS }}>📰 柱顶·高优（VIP）</span>
        <span style={{ color: COLOR_TRADE_POINT }}>💜 柱顶·交易点（toggle，默认关）</span>
        <span style={{ color: COLOR_SIGNAL }}>🟢 柱内·信号（BUY/SELL）</span>
        <span style={{ color: COLOR_NEWS }}>🗞️ 柱底·新闻（HOLD）</span>
        <span style={{ color: COLOR_ANOM_BEAR }}>⭐ 柱底·异动（点按 AI）</span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-1 w-3 rounded" style={{ background: 'rgba(46,189,133,0.85)' }} />
          <span className="inline-block h-1 w-3 rounded" style={{ background: 'rgba(239,68,68,0.85)' }} />
          <span>RSI 柱（多头/空头）</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-px w-3" style={{ background: '#f7a440' }} />
          <span>RSI 14 折线</span>
        </span>
        <span>· 70 超买 / 30 超卖</span>
        <span className="ml-auto" style={{ color: meta.accent }}>
          ● {meta.name}
        </span>
      </div>
    </section>
  )
}