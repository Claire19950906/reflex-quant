// v170 P0+：kline-chart 共享纯函数与常量
//
// 从 kline-chart.tsx 抽出来的无 JSX 部分（CHART_THEME / IND_COLORS / 时区格式化 /
// marker 时间对齐），主组件只保留 chart lifecycle 与渲染。
//
// 约束（务必遵守）：
//   - 改 SH_OFFSET_MS 时同步检查 trading-replay 后端的时间序列化（utc+8 假设）。

import type { Time } from 'lightweight-charts'
import { TickMarkType } from 'lightweight-charts'
import type { ReplayKlineBar } from '@/lib/replay-data'

/** 图表主色板：light/dark 各一套，与 use-is-dark hook 联动 */
export const CHART_THEME = {
  light: {
    background: '#ffffff',
    text: '#5b6472',
    grid: '#eceff3',
    border: '#dfe3e9',
    up: '#00b061',
    down: '#ef4444',
    entry: '#f0932b',
    stop: '#ef4444',
    take: '#00b061',
    exit: '#64748b',
  },
  dark: {
    background: '#16171b',
    text: '#9aa3af',
    grid: '#26282e',
    border: '#33363d',
    up: '#2ebd85',
    down: '#f6465d',
    entry: '#f7a440',
    stop: '#f6465d',
    take: '#2ebd85',
    exit: '#9aa3af',
  },
} as const

/** 主图叠加 / 副图指标配色（明暗通用） */
export const IND_COLORS = {
  ma: '#f7a440',      // 橙
  ema: '#8b5cf6',     // 紫
  bollUp: '#3b82f6',  // 蓝
  bollMid: '#f59e0b', // 黄
  bollLow: '#3b82f6',
  dif: '#f7a440',
  dea: '#8b5cf6',
  histUp: '#2ebd85',
  histDown: '#f6465d',
  rsiK: '#f7a440',
  rsiD: '#8b5cf6',
  kdjK: '#f7a440',
  kdjD: '#3b82f6',
  kdjJ: '#f6465d',
} as const

/** 图表时间轴统一显示上海时区（UTC+8），与交易记录时间（引擎写库用上海时区）一致 */
export const SH_OFFSET_MS = 8 * 3600 * 1000

/** lightweight-charts 时间轴刻度 — 始终按上海时区输出 */
export function shTickFormatter(time: Time, tickMarkType: TickMarkType): string {
  if (typeof time !== 'number') return String(time)
  const d = new Date(time * 1000 + SH_OFFSET_MS)
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  // Time / TimeWithSeconds 类只显示时分；日期类显示 月-日 时分
  if (tickMarkType === TickMarkType.Time || tickMarkType === TickMarkType.TimeWithSeconds) {
    return hm
  }
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${hm}`
}

/**
 * 需求10：回测复盘时，新闻 / 入场时间对齐对应资产向前最近的开市 K 线（取 ≤ time 的最近一根）。
 * 不用“绝对最近”或“当日开盘首根”。
 */
export function alignMarkerTime(time: number, bars: ReplayKlineBar[]): number {
  if (!bars.length) return time
  let anchor = bars[0].time
  for (const bar of bars) {
    if (bar.time <= time) anchor = bar.time
    else break
  }
  return anchor
}
