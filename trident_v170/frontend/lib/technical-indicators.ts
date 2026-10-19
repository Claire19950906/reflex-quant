// 技术指标纯函数库（轻量手写，无第三方依赖）
// 输入 bars 需按时间升序。全部返回与输入等长的数组，不足窗口期的前段为 null。
import type { ReplayKlineBar } from '@/lib/replay-data'

export type IndicatorSeries = { time: number; value: number | null }[]

function round(v: number, digits = 4): number {
  const p = 10 ** digits
  return Math.round(v * p) / p
}

/** 简单移动平均 SMA */
export function sma(bars: ReplayKlineBar[], period: number): IndicatorSeries {
  return bars.map((bar, i) => {
    if (i < period - 1) return { time: bar.time, value: null }
    let sum = 0
    for (let k = i - period + 1; k <= i; k++) sum += bars[k].close
    return { time: bar.time, value: round(sum / period) }
  })
}

/** 指数移动平均 EMA（种子=SMA(period)） */
export function ema(bars: ReplayKlineBar[], period: number): IndicatorSeries {
  const k = 2 / (period + 1)
  let prev: number | null = null
  return bars.map((bar, i) => {
    if (i < period - 1) return { time: bar.time, value: null }
    if (i === period - 1) {
      let sum = 0
      for (let j = 0; j < period; j++) sum += bars[j].close
      prev = sum / period
    } else if (prev !== null) {
      prev = bar.close * k + prev * (1 - k)
    }
    return { time: bar.time, value: prev === null ? null : round(prev) }
  })
}

/** 布林带：中轨 SMA(period)，上下轨 ±mult*std
 *
 * v302+ P0+ 修复（与 TradingView / 通达信对齐）：
 *   标准差从「总体标准差」(N) 改为「样本标准差」(N-1)。
 *   原因：period=20 时总体标准差会低估真实波动 ~5%，
 *         导致 Bollinger Squeeze 易误判为「缩量盘整期」。
 *   影响：bollingerWidthPct / bollingerSqueeze 自动跟随（同源）。
 *   边界：period=1 时退化为 0（避免除零），实际调用 period>=2。
 */
export function bollinger(
  bars: ReplayKlineBar[],
  period = 20,
  mult = 2,
): { middle: IndicatorSeries; upper: IndicatorSeries; lower: IndicatorSeries } {
  const middle: IndicatorSeries = []
  const upper: IndicatorSeries = []
  const lower: IndicatorSeries = []
  const denom = period > 1 ? period - 1 : period  // 防御 period=1
  bars.forEach((bar, i) => {
    if (i < period - 1) {
      middle.push({ time: bar.time, value: null })
      upper.push({ time: bar.time, value: null })
      lower.push({ time: bar.time, value: null })
      return
    }
    let sum = 0
    for (let k = i - period + 1; k <= i; k++) sum += bars[k].close
    const m = sum / period
    let variance = 0
    for (let k = i - period + 1; k <= i; k++) variance += (bars[k].close - m) ** 2
    const std = Math.sqrt(variance / denom)
    middle.push({ time: bar.time, value: round(m) })
    upper.push({ time: bar.time, value: round(m + mult * std) })
    lower.push({ time: bar.time, value: round(m - mult * std) })
  })
  return { middle, upper, lower }
}

/** v277+ — 布林带宽度归一化：(upper - lower) / middle × 100（百分比）。
 *  用于「Bollinger Squeeze」识别：宽度小 = 缩量盘整，宽度大 = 波动放大。
 *  前 period-1 根为 null（暖机期）。 */
export function bollingerWidthPct(
  bars: ReplayKlineBar[],
  period = 20,
  mult = 2,
): IndicatorSeries {
  const { middle, upper, lower } = bollinger(bars, period, mult)
  return bars.map((bar, i) => {
    const m = middle[i]?.value
    const u = upper[i]?.value
    const l = lower[i]?.value
    if (m == null || u == null || l == null || m === 0) {
      return { time: bar.time, value: null }
    }
    return { time: bar.time, value: round(((u - l) / m) * 100, 4) }
  })
}

/** v277+ — Bollinger Squeeze 状态：每根 bar 是否处于"近 windowLookback 根带宽的 quantile 分位以下"。
 *  返回每根 bar 的：
 *    - isSqueeze: true/false（带宽处于历史低位 = 缩量盘整期）
 *    - widthPct:  当前归一化宽度（百分比，null 表示暖机期）
 *    - threshold: 当前窗口的 quantile 分位阈值（用于 tooltip 显示「低于 X% 分位」）
 *
 *  归因页 K 线异常识别用：当异动发生在 squeeze 期 → 大概率是噪音，应弱化或隐藏。
 *
 *  @param bars           K 线序列（升序）
 *  @param period         布林带周期，默认 20
 *  @param mult           布林带标准差倍数，默认 2
 *  @param windowLookback 历史窗口长度，默认 100 根
 *  @param quantile       分位阈值，默认 0.2（20% 分位以下视为 squeeze）
 */
export function bollingerSqueezeMask(
  bars: ReplayKlineBar[],
  period = 20,
  mult = 2,
  windowLookback = 100,
  quantile = 0.2,
): { time: number; isSqueeze: boolean; widthPct: number | null; threshold: number | null }[] {
  const widths = bollingerWidthPct(bars, period, mult)
  return bars.map((bar, i) => {
    const v = widths[i]?.value
    if (v == null) {
      return { time: bar.time, isSqueeze: false, widthPct: null, threshold: null }
    }
    // 收集历史窗口内的有效宽度值（含当前 bar）
    const start = Math.max(0, i - windowLookback + 1)
    const histWidths: number[] = []
    for (let k = start; k <= i; k++) {
      const wv = widths[k]?.value
      if (wv != null) histWidths.push(wv)
    }
    // 样本不足（< 20 根）→ 不判定 squeeze，避免冷启动误判
    if (histWidths.length < 20) {
      return { time: bar.time, isSqueeze: false, widthPct: v, threshold: null }
    }
    const sorted = [...histWidths].sort((a, b) => a - b)
    const idx = Math.min(Math.floor(sorted.length * quantile), sorted.length - 1)
    const th = sorted[idx]
    return {
      time: bar.time,
      isSqueeze: v <= th,
      widthPct: v,
      threshold: round(th, 4),
    }
  })
}

/** v278 — 标准 Pivot Point（TradingView 同款「Pivot Points Standard」算法）
 *  基于「前 period 根 K 线」的 H/L/C 计算 7 道价格位（Pivot + S1-S3 / R1-R3）：
 *
 *    Pivot (P) = (H + L + C) / 3
 *    R1        = 2 * P - L
 *    S1        = 2 * P - H
 *    R2        = P + (H - L)
 *    S2        = P - (H - L)
 *    R3        = H + 2 * (P - L)
 *    S3        = L - 2 * (H - P)
 *
 *  这是全球最经典的技术支撑阻力算法（被 TradingView / Investing.com / StockCharts 共用），
 *  完全本地计算，无需外部数据源。
 *
 *  @param bars   升序 K 线序列（接受任意有 high/low/close 字段的 K 线类型）
 *  @param period 参考周期（默认 1 = 前 1 根 K；用户可传 1/24/288 等，按 timeframe 自适应）
 *  @returns      {pivot, r1, r2, r3, s1, s2, s3, high, low, close}；样本不足返回 null
 */
export function pivotPoint(
  bars: { high: number; low: number; close: number }[],
  period = 1,
): {
  p: number
  r1: number
  r2: number
  r3: number
  s1: number
  s2: number
  s3: number
  high: number
  low: number
  close: number
  period: number
} | null {
  if (bars.length < period) return null
  // 取最近 period 根的 H/L/C（默认 period=1 → 前一根；period=144 → 前 24h @ 10m）
  const slice = bars.slice(-period)
  let hi = -Infinity
  let lo = Infinity
  for (const b of slice) {
    if (b.high > hi) hi = b.high
    if (b.low < lo) lo = b.low
  }
  const c = slice[slice.length - 1].close
  const p = (hi + lo + c) / 3
  const r1 = 2 * p - lo
  const s1 = 2 * p - hi
  const r2 = p + (hi - lo)
  const s2 = p - (hi - lo)
  const r3 = hi + 2 * (p - lo)
  const s3 = lo - 2 * (hi - p)
  return {
    p: round(p),
    r1: round(r1),
    r2: round(r2),
    r3: round(r3),
    s1: round(s1),
    s2: round(s2),
    s3: round(s3),
    high: round(hi),
    low: round(lo),
    close: round(c),
    period,
  }
}

/** v278 — 按 K 线 timeframe 自动选择 Pivot 参考周期（bar 数）
 *  目的：日内高频（3m/5m/10m）→ 取前 24h；日内低频（1h/4h）→ 取前一日；周/月 → 取前一周/前月
 *  返回推荐 period bar 数（bar 数而非天数，按 144 = 24h @ 10m 计算）
 */
export function pivotPeriodForTimeframe(timeframe: string): { period: number; label: string } {
  // 144 = 24h @ 10m；与现有 live-kline-chart 的 K 线密度一致
  switch (timeframe) {
    case '3m':
      return { period: 480, label: '前 24h' }   // 480 根 3m = 24h
    case '5m':
      return { period: 288, label: '前 24h' }   // 288 根 5m = 24h
    case '10m':
      return { period: 144, label: '前 24h' }   // 144 根 10m = 24h
    case '1h':
      return { period: 24, label: '前 24h' }    // 24 根 1h = 24h
    case '4h':
      return { period: 6, label: '前 24h' }     // 6 根 4h = 24h
    case '1d':
      return { period: 7, label: '前 7 日' }      // 7 根 1d = 1 周
    default:
      return { period: 144, label: '前 24h' }
  }
}

/** MACD：DIF=EMA12-EMA26，DEA=EMA9(DIF)，MACD 柱 = 2*(DIF-DEA) */
export function macd(
  bars: ReplayKlineBar[],
): { dif: IndicatorSeries; dea: IndicatorSeries; hist: IndicatorSeries } {
  const closes = bars.map((b) => b.close)
  const emaFast = ema(bars, 12)
  const emaSlow = ema(bars, 26)
  const difRaw = bars.map((bar, i) => {
    const f = emaFast[i]?.value
    const s = emaSlow[i]?.value
    return { time: bar.time, value: f !== null && s !== null ? f - s : null }
  })
  // DEA = EMA9 of DIF（null 跳过）
  const deaRaw: { time: number; value: number | null }[] = []
  const valid: number[] = []
  difRaw.forEach((p, i) => {
    if (p.value === null) {
      deaRaw.push({ time: p.time, value: null })
      return
    }
    valid.push(p.value)
    if (valid.length < 9) {
      deaRaw.push({ time: p.time, value: null })
      return
    }
    if (valid.length === 9) {
      const seed = valid.reduce((a, b) => a + b, 0) / 9
      deaRaw.push({ time: p.time, value: round(seed, 6) })
      return
    }
    const prev = deaRaw[i - 1]?.value
    if (prev === null) {
      deaRaw.push({ time: p.time, value: null })
      return
    }
    const k = 2 / (9 + 1)
    deaRaw.push({ time: p.time, value: round(p.value * k + prev * (1 - k), 6) })
  })
  const hist = bars.map((bar, i) => {
    const d = difRaw[i]?.value
    const e = deaRaw[i]?.value
    return { time: bar.time, value: d !== null && e !== null ? round((d - e) * 2, 6) : null }
  })
  return { dif: difRaw as IndicatorSeries, dea: deaRaw, hist }
}

/** RSI（Wilder 平滑，period=14） */
export function rsi(bars: ReplayKlineBar[], period = 14): IndicatorSeries {
  const out: IndicatorSeries = []
  let avgGain = 0
  let avgLoss = 0
  let seeded = false
  bars.forEach((bar, i) => {
    if (i === 0) {
      out.push({ time: bar.time, value: null })
      return
    }
    const chg = bar.close - bars[i - 1].close
    const gain = Math.max(chg, 0)
    const loss = Math.max(-chg, 0)
    if (i <= period) {
      avgGain += gain
      avgLoss += loss
      if (i === period) {
        avgGain /= period
        avgLoss /= period
        seeded = true
      }
      out.push({ time: bar.time, value: null })
      return
    }
    if (seeded) {
      avgGain = (avgGain * (period - 1) + gain) / period
      avgLoss = (avgLoss * (period - 1) + loss) / period
    }
    if (avgLoss === 0) {
      out.push({ time: bar.time, value: round(100) })
    } else {
      const rs = avgGain / avgLoss
      out.push({ time: bar.time, value: round(100 - 100 / (1 + rs), 2) })
    }
  })
  return out
}

/** KDJ（9,3,3）：RSV=(C-L9)/(H9-L9)*100，K=EMA3(RSV)，D=EMA3(K)，J=3K-2D */
export function kdj(
  bars: ReplayKlineBar[],
  period = 9,
): { k: IndicatorSeries; d: IndicatorSeries; j: IndicatorSeries } {
  const rsvList: (number | null)[] = []
  bars.forEach((bar, i) => {
    if (i < period - 1) {
      rsvList.push(null)
      return
    }
    let hh = -Infinity
    let ll = Infinity
    for (let k = i - period + 1; k <= i; k++) {
      hh = Math.max(hh, bars[k].high)
      ll = Math.min(ll, bars[k].low)
    }
    rsvList.push(hh === ll ? 50 : ((bar.close - ll) / (hh - ll)) * 100)
  })
  let kVal = 50
  let dVal = 50
  const k: IndicatorSeries = []
  const d: IndicatorSeries = []
  const j: IndicatorSeries = []
  rsvList.forEach((rsv, i) => {
    if (rsv === null) {
      k.push({ time: bars[i].time, value: null })
      d.push({ time: bars[i].time, value: null })
      j.push({ time: bars[i].time, value: null })
      return
    }
    kVal = (2 / 3) * kVal + (1 / 3) * rsv
    dVal = (2 / 3) * dVal + (1 / 3) * kVal
    const jVal = 3 * kVal - 2 * dVal
    k.push({ time: bars[i].time, value: round(kVal, 2) })
    d.push({ time: bars[i].time, value: round(dVal, 2) })
    j.push({ time: bars[i].time, value: round(jVal, 2) })
  })
  return { k, d, j }
}

/** ATR（Wilder，period=14） */
export function atr(bars: ReplayKlineBar[], period = 14): IndicatorSeries {
  const out: IndicatorSeries = []
  const trs: number[] = []
  bars.forEach((bar, i) => {
    if (i === 0) {
      trs.push(bar.high - bar.low)
    } else {
      const prevClose = bars[i - 1].close
      trs.push(Math.max(bar.high - bar.low, Math.abs(bar.high - prevClose), Math.abs(bar.low - prevClose)))
    }
    if (i < period) {
      out.push({ time: bar.time, value: null })
      return
    }
    if (i === period) {
      const sum = trs.slice(1, period + 1).reduce((a, b) => a + b, 0)
      const seed = sum / period
      out.push({ time: bar.time, value: round(seed, 4) })
      return
    }
    const prev = out[i - 1]?.value
    if (prev === null) {
      out.push({ time: bar.time, value: null })
      return
    }
    const cur = (prev * (period - 1) + trs[i]) / period
    out.push({ time: bar.time, value: round(cur, 4) })
  })
  return out
}

/** 线性回归辅助：给最后一根 value 是否有效做判断 */
export function hasValue(series: IndicatorSeries, index: number): boolean {
  return !!series[index] && series[index].value !== null
}
