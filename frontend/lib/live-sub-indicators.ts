/**
 * v458 · 实时图副图的数据装配（纯函数，可单测）
 *
 * 为什么单独抽出来：`live-kline-chart.tsx` 已经 2,300 行，把「指标序列 →
 * lightweight-charts 要的 {time,value} / {time,value,color}」这段映射塞在
 * 组件里，只能靠肉眼看图验证。抽成纯函数后可以在 vitest 里断言数值，
 * 不用起浏览器。
 *
 * 口径：全部来自 `technical-indicators.ts`（与后端 `technical_indicators.py`
 * 有 parity 测试逐值对齐），这里只负责裁剪 null 暖机段和上色。
 */
import { macd as macdFn, kdj as kdjFn, rsi as rsiFn } from './technical-indicators'

export type SubKind = 'rsi' | 'macd' | 'kdj'

export type SubBar = {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume?: number
}

export type SubLineSeries = {
  key: string
  title: string
  color: string
  points: { time: number; value: number }[]
}

export type SubHistSeries = {
  key: string
  title: string
  base: number
  points: { time: number; value: number; color: string }[]
}

export type SubBundle = { lines: SubLineSeries[]; hist: SubHistSeries[] }

/** RSI 超买/超卖/多空配色，与图上原有那套保持一致 */
const RSI_RED = 'rgba(239, 68, 68, 0.85)'
const RSI_GREEN = 'rgba(46, 189, 133, 0.85)'
const RSI_RED_SOFT = 'rgba(239, 68, 68, 0.55)'
const RSI_GREEN_SOFT = 'rgba(46, 189, 133, 0.55)'

export const SUB_COLORS = {
  rsiLine: '#f7a440',
  macdDif: '#f7a440',
  macdDea: '#4ea1ff',
  macdHistUp: 'rgba(46, 189, 133, 0.65)',
  macdHistDown: 'rgba(239, 68, 68, 0.65)',
  kdjK: '#f7a440',
  kdjD: '#4ea1ff',
  kdjJ: '#b07cff',
} as const

/** 暖机期不够就返回空 —— 图上宁可空着，也不画一条只算了一半的线 */
export const SUB_MIN_BARS = 15

function nonNull(series: { time: number; value: number | null }[]) {
  return series
    .filter((p) => p.value != null)
    .map((p) => ({ time: p.time, value: p.value as number }))
}

export function buildSubIndicators(kind: SubKind, bars: SubBar[]): SubBundle {
  const empty: SubBundle = { lines: [], hist: [] }
  if (!bars || bars.length < SUB_MIN_BARS) return empty
  const feed = bars.map((b) => ({
    time: b.time,
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
    volume: b.volume ?? 0,
  }))

  if (kind === 'rsi') {
    const series = rsiFn(feed, 14)
    return {
      lines: [{ key: 'rsi', title: 'RSI 14', color: SUB_COLORS.rsiLine, points: nonNull(series) }],
      hist: [{
        key: 'rsiDev',
        title: 'RSI - 50',
        base: 0,
        points: series.filter((p) => p.value != null).map((p) => {
          const v = p.value as number
          const dev = v - 50
          let color: string
          if (v >= 70) color = RSI_RED
          else if (v <= 30) color = RSI_GREEN
          else if (dev >= 0) color = RSI_GREEN_SOFT
          else color = RSI_RED_SOFT
          return { time: p.time, value: dev, color }
        }),
      }],
    }
  }

  if (kind === 'macd') {
    const { dif, dea, hist } = macdFn(feed)
    return {
      lines: [
        { key: 'dif', title: 'MACD DIF', color: SUB_COLORS.macdDif, points: nonNull(dif) },
        { key: 'dea', title: 'MACD DEA', color: SUB_COLORS.macdDea, points: nonNull(dea) },
      ],
      hist: [{
        key: 'macdHist',
        title: 'MACD 柱',
        base: 0,
        points: hist.filter((p) => p.value != null).map((p) => ({
          time: p.time,
          value: p.value as number,
          // 柱的正负就是 DIF 在 DEA 上方还是下方，不掺涨跌颜色
          color: (p.value as number) >= 0 ? SUB_COLORS.macdHistUp : SUB_COLORS.macdHistDown,
        })),
      }],
    }
  }

  const { k, d, j } = kdjFn(feed, 9)
  return {
    lines: [
      { key: 'k', title: 'K', color: SUB_COLORS.kdjK, points: nonNull(k) },
      { key: 'd', title: 'D', color: SUB_COLORS.kdjD, points: nonNull(d) },
      { key: 'j', title: 'J', color: SUB_COLORS.kdjJ, points: nonNull(j) },
    ],
    hist: [],
  }
}
