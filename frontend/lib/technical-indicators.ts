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
/** 指数移动平均 EMA
 *
 * v403 口径统一（与后端 `technical_indicators.ema` 逐值一致）：
 *   种子 = 第 0 根收盘价，从第 0 根起就有值（不再前置 period-1 根 null、不再用 SMA 播种）。
 *   理由：后端决策链用的就是这个口径；前端改成 SMA 播种后，两套在同一张图上
 *   给出两条不同的 EMA，且差异在拐点处最明显。一致性由 parity 测试锁住。 */
export function ema(bars: ReplayKlineBar[], period = 12): IndicatorSeries {
  if (bars.length === 0) return []
  const k = 2 / (period + 1)
  const out: IndicatorSeries = []
  let prev = bars[0].close
  bars.forEach((bar, i) => {
    if (i > 0) prev = bar.close * k + prev * (1 - k)
    out.push({ time: bar.time, value: round(prev) })
  })
  return out
}

/** 布林带：中轨 SMA(period)，上下轨 ±mult*std
 *
 * v403 口径统一（与后端 `technical_indicators.bollinger` 逐值一致）：
 *   标准差用**总体标准差**（除以 N）。
 *   历史注记：v302 曾以"与 TradingView 对齐"为由改成样本标准差（N-1），
 *   但 TradingView `ta.stdev()` 的 biased 参数默认为 true，即**总体标准差**；
 *   反而改完才偏离。偏差量 sqrt(N/(N-1))：period=20 时带宽宽 2.6%，
 *   会穿透到 bollingerWidthPct → squeeze 分位判定，属于会改变读数的漂移。
 *   一致性由 `technical-indicators.parity.test.ts` 用后端真实 bar 逐点锁住。 */
export function bollinger(
  bars: ReplayKlineBar[],
  period = 20,
  mult = 2,
): { middle: IndicatorSeries; upper: IndicatorSeries; lower: IndicatorSeries } {
  const middle: IndicatorSeries = []
  const upper: IndicatorSeries = []
  const lower: IndicatorSeries = []
  const denom = period > 0 ? period : 1  // 防御 period=0
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

/** MACD：DIF=EMA12-EMA26，DEA=EMA9(DIF)，柱 = DIF-DEA
 *
 * v407 口径统一（与后端 `technical_indicators.macd` 逐值一致）：
 *   1) DEA 改为对 DIF 做 EMA 递推（种子 = 首个 DIF），不再"跳过前 8 个再 SMA 播种"；
 *   2) 柱不再 ×2 —— 后端 hist = macd - signal。原来前端柱高是后端的 2 倍，
 *      在两套读数里"柱子在扩还是缩"会给出相反的相对强度。
 */
export function macd(
  bars: ReplayKlineBar[],
): { dif: IndicatorSeries; dea: IndicatorSeries; hist: IndicatorSeries } {
  const emaFast = ema(bars, 12)
  const emaSlow = ema(bars, 26)
  const difVals: number[] = bars.map((_, i) => {
    const f = emaFast[i]?.value
    const s = emaSlow[i]?.value
    return (f as number) - (s as number)
  })
  const k = 2 / (9 + 1)
  let dea = difVals[0]
  const dif: IndicatorSeries = []
  const deaSeries: IndicatorSeries = []
  const hist: IndicatorSeries = []
  bars.forEach((bar, i) => {
    if (i > 0) dea = difVals[i] * k + dea * (1 - k)
    dif.push({ time: bar.time, value: round(difVals[i], 6) })
    deaSeries.push({ time: bar.time, value: round(dea, 6) })
    hist.push({ time: bar.time, value: round(difVals[i] - dea, 6) })
  })
  return { dif, dea: deaSeries, hist }
}

/** RSI（Wilder 平滑，period=14） */
/** Wilder RSI。前 period 根为 null，第 period 根出首个值。
 *
 * v403 口径统一（与后端 `technical_indicators.rsi` 逐值一致）：原实现把首个值
 * 放在第 period+1 根、且多平滑了一次，整条序列相对后端**错位一根**。
 * 图上的 RSI 与决策链读到的 RSI 因此差一根 bar —— 在拐点上恰好是两种结论。 */
export function rsi(bars: ReplayKlineBar[], period = 14): IndicatorSeries {
  const out: IndicatorSeries = []
  let avgGain = 0
  let avgLoss = 0
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
        out.push({ time: bar.time, value: avgLoss === 0 ? round(100) : round(100 - 100 / (1 + avgGain / avgLoss), 2) })
        return
      }
      out.push({ time: bar.time, value: null })
      return
    }
    avgGain = (avgGain * (period - 1) + gain) / period
    avgLoss = (avgLoss * (period - 1) + loss) / period
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
    if (i < period - 1) {
      out.push({ time: bar.time, value: null })
      return
    }
    if (i === period - 1) {
      // 种子 = 前 period 个 TR 的均值（含第 0 根的 H-L），与后端同口径
      const seed = trs.slice(0, period).reduce((a, b) => a + b, 0) / period
      out.push({ time: bar.time, value: round(seed, 4) })
      return
    }
    const prev = out[i - 1]?.value
    if (prev === null || prev === undefined) {
      out.push({ time: bar.time, value: null })
      return
    }
    const cur = (prev * (period - 1) + trs[i]) / period
    out.push({ time: bar.time, value: round(cur, 4) })
  })
  return out
}

/** VWAP（成交量加权均价，累计口径）
 *
 * 口径与后端 `technical_indicators.vwap` 逐值一致：
 *   TP=(H+L+C)/3；VWAP=Σ(TP×Vol)/Σ(Vol)，从首根起累计；量为 0 的根记 null。
 */
export function vwap(bars: ReplayKlineBar[]): IndicatorSeries {
  const out: IndicatorSeries = []
  let cumTpVol = 0
  let cumVol = 0
  bars.forEach((bar) => {
    const v = bar.volume || 0
    if (v === 0) {
      out.push({ time: bar.time, value: null })
      return
    }
    const tp = (bar.high + bar.low + bar.close) / 3
    cumTpVol += tp * v
    cumVol += v
    out.push({ time: bar.time, value: cumVol ? round(cumTpVol / cumVol, 4) : null })
  })
  return out
}

/** Donchian 通道（period 内最高/最低/中轨 + 价格位置）
 *
 * 口径与后端 `technical_indicators.donchian` 一致：
 *   upper=窗口最高，lower=窗口最低，mid=(upper+lower)/2；
 *   前 period-1 根为 null。position 是价格在通道中的 0..1 位置。
 */
export function donchian(bars: ReplayKlineBar[], period = 20): {
  upper: IndicatorSeries
  mid: IndicatorSeries
  lower: IndicatorSeries
  position: IndicatorSeries
} {
  const n = bars.length
  const upper: IndicatorSeries = []
  const mid: IndicatorSeries = []
  const lower: IndicatorSeries = []
  const position: IndicatorSeries = []
  for (let i = 0; i < n; i++) {
    if (i < period - 1) {
      upper.push({ time: bars[i].time, value: null })
      mid.push({ time: bars[i].time, value: null })
      lower.push({ time: bars[i].time, value: null })
      position.push({ time: bars[i].time, value: null })
      continue
    }
    let hi = -Infinity
    let lo = Infinity
    for (let k = i - period + 1; k <= i; k++) {
      hi = Math.max(hi, bars[k].high)
      lo = Math.min(lo, bars[k].low)
    }
    const m = (hi + lo) / 2
    const pos = hi > lo ? (bars[i].close - lo) / (hi - lo) : 0.5
    upper.push({ time: bars[i].time, value: round(hi, 6) })
    mid.push({ time: bars[i].time, value: round(m, 6) })
    lower.push({ time: bars[i].time, value: round(lo, 6) })
    position.push({ time: bars[i].time, value: round(pos, 4) })
  }
  return { upper, mid, lower, position }
}

/** SuperTrend — ATR 自适应趋势轨，line 为当前有效轨，direction 1=多头 / -1=空头。
 *
 * 口径与后端 `technical_indicators.supertrend` 逐值一致：
 *   hl2=(H+L)/2；基础上轨=hl2+k·ATR、下轨=hl2-k·ATR；
 *   上轨只降不升（前收站上才允许上移），下轨只升不降（前收跌破才允许下移）；
 *   多头跌破下轨翻空、空头涨破上轨翻多。ATR 暖机段为 null。
 */
export function supertrend(
  bars: ReplayKlineBar[],
  period = 10,
  multiplier = 3,
): { line: IndicatorSeries; direction: (number | null)[] } {
  const n = bars.length
  const line: IndicatorSeries = []
  const direction: (number | null)[] = new Array(n).fill(null)
  if (n < period + 1) {
    bars.forEach((b) => line.push({ time: b.time, value: null }))
    return { line, direction }
  }
  const atrVals = atr(bars, period).map((p) => p.value)
  const start = atrVals.findIndex((v) => v !== null)
  if (start < 0) {
    bars.forEach((b) => line.push({ time: b.time, value: null }))
    return { line, direction }
  }
  let fub: number | null = null
  let flb: number | null = null
  let dirn: number | null = null
  for (let i = 0; i < n; i++) {
    if (i < start || atrVals[i] === null) {
      line.push({ time: bars[i].time, value: null })
      continue
    }
    const hl2 = (bars[i].high + bars[i].low) / 2
    const bub = hl2 + multiplier * (atrVals[i] as number)
    const blb = hl2 - multiplier * (atrVals[i] as number)
    if (fub === null || flb === null) {
      fub = bub
      flb = blb
      dirn = bars[i].close >= hl2 ? 1 : -1
    } else {
      const prevClose = bars[i - 1].close
      fub = bub < fub || prevClose > fub ? bub : fub
      flb = blb > flb || prevClose < flb ? blb : flb
      if (dirn === 1 && bars[i].close < flb) dirn = -1
      else if (dirn === -1 && bars[i].close > fub) dirn = 1
    }
    direction[i] = dirn
    line.push({ time: bars[i].time, value: round(dirn === 1 ? flb as number : fub as number, 6) })
  }
  return { line, direction }
}

/** 线性回归辅助：给最后一根 value 是否有效做判断 */
export function hasValue(series: IndicatorSeries, index: number): boolean {
  return !!series[index] && series[index].value !== null
}

/** OBV（On-Balance Volume，量能趋势累计）
 *
 * 口径与后端 `technical_indicators.obv` 逐值一致：首根为 0；
 * 收盘上涨累加当日量、下跌扣减当日量，平盘不动。
 */
export function obv(bars: ReplayKlineBar[]): IndicatorSeries {
  const out: IndicatorSeries = []
  let cur = 0
  bars.forEach((bar, i) => {
    if (i > 0) {
      if (bar.close > bars[i - 1].close) cur += bar.volume || 0
      else if (bar.close < bars[i - 1].close) cur -= bar.volume || 0
    }
    out.push({ time: bar.time, value: round(cur, 4) })
  })
  return out
}

/** StochRSI（RSI 上再做一次随机指标），返回 {k, d}。
 *
 * 口径与后端 `technical_indicators.stoch_rsi` 逐值一致：
 *   %K raw = (RSI - 窗口最低 RSI) / (窗口最高 RSI - 窗口最低 RSI) × 100；
 *   %K = raw 的 smooth_k 根 SMA；%D = %K 的 smooth_d 根 SMA；RSI 无效段为 null。
 */
export function stochRsi(
  bars: ReplayKlineBar[],
  period = 14,
  smoothK = 3,
  smoothD = 3,
): { k: IndicatorSeries; d: IndicatorSeries } {
  const rsiVals = rsi(bars, period).map((p) => p.value)
  const n = rsiVals.length
  const raw: (number | null)[] = new Array(n).fill(null)
  for (let i = 0; i < n; i++) {
    if (i < period - 1 || rsiVals[i] === null) continue
    const win = rsiVals.slice(i - period + 1, i + 1).filter((v): v is number => v !== null)
    if (win.length < period) continue
    const lo = Math.min(...win)
    const hi = Math.max(...win)
    raw[i] = hi === lo ? 50 : round(((rsiVals[i] as number) - lo) / (hi - lo) * 100, 2)
  }
  const smaOf = (seq: (number | null)[], m: number): (number | null)[] => {
    const out: (number | null)[] = new Array(n).fill(null)
    for (let i = 0; i < n; i++) {
      if (i < m - 1) continue
      const win = seq.slice(i - m + 1, i + 1).filter((v): v is number => v !== null)
      out[i] = win.length ? round(win.reduce((a, b) => a + b, 0) / win.length, 2) : null
    }
    return out
  }
  const kS = smaOf(raw, smoothK)
  const dS = smaOf(kS, smoothD)
  return {
    k: bars.map((b, i) => ({ time: b.time, value: kS[i] })),
    d: bars.map((b, i) => ({ time: b.time, value: dS[i] })),
  }
}

/** Stochastic %K（随机指标，k_period=14，smooth=3）
 *
 * 口径与后端 `technical_indicators.stoch` 逐值一致：
 *   raw = (C - 窗口最低 Low) / (窗口最高 High - 窗口最低 Low) × 100；
 *   输出 = raw 最近 smooth 根均值；窗口不足 smooth 根时为 null。
 */
export function stoch(bars: ReplayKlineBar[], kPeriod = 14, smooth = 3): IndicatorSeries {
  const n = bars.length
  const raw: (number | null)[] = new Array(n).fill(null)
  for (let i = 0; i < n; i++) {
    if (i < kPeriod - 1) continue
    let hi = -Infinity
    let lo = Infinity
    for (let k = i - kPeriod + 1; k <= i; k++) {
      hi = Math.max(hi, bars[k].high)
      lo = Math.min(lo, bars[k].low)
    }
    raw[i] = hi === lo ? 50 : round((bars[i].close - lo) / (hi - lo) * 100, 4)
  }
  return bars.map((bar, i) => {
    if (i < smooth - 1) return { time: bar.time, value: null }
    const win = raw.slice(i - smooth + 1, i + 1).filter((v): v is number => v !== null)
    const value = win.length === smooth ? round(win.reduce((a, b) => a + b, 0) / win.length, 4) : null
    return { time: bar.time, value }
  })
}

/** MFI（资金流量指标，量价加权 RSI，period=14）
 *
 * 口径与后端 `technical_indicators.mfi` 逐值一致：
 *   Typical Price = (high + low + close) / 3；Raw Money Flow = TP × volume；
 *   TP 上升计正向流量、下降计负向流量；窗口内 MFI = 100 - 100/(1 + 正向/负向)。
 *   窗口满 period 后才出值，之前为 null；负向流量为 0 时记 100（与后端同口径）。
 */
export function mfi(bars: ReplayKlineBar[], period = 14): IndicatorSeries {
  const n = bars.length
  const out: IndicatorSeries = []
  if (n < period + 1) {
    return bars.map((bar) => ({ time: bar.time, value: null }))
  }
  const tp = bars.map((b) => (b.high + b.low + b.close) / 3)
  const flow = tp.map((t, i) => t * (bars[i].volume || 0))
  // 口径与后端 `technical_indicators.mfi` 逐值一致：
  // 每个窗口取 [i-period+1, i]，首根作基准，只累加窗口内相邻根之间的流量方向
  // （窗口首根自身的流量不计入分子/分母），向后滑动时重扫窗口。
  for (let i = 0; i < n; i++) {
    if (i < period) {
      out.push({ time: bars[i].time, value: null })
      continue
    }
    let pos = 0
    let neg = 0
    for (let j = i - period + 2; j <= i; j++) {
      if (tp[j] > tp[j - 1]) pos += flow[j]
      else if (tp[j] < tp[j - 1]) neg += flow[j]
    }
    out.push({ time: bars[i].time, value: neg <= 0 ? 100 : round(100 - 100 / (1 + pos / neg), 2) })
  }
  return out
}

/** CCI（顺势指标，20）：典型价偏离均值 / (0.015 × 平均绝对偏差)，口径对齐后端 cci。 */
export function cci(bars: ReplayKlineBar[], period = 20): IndicatorSeries {
  const n = bars.length
  const out: IndicatorSeries = []
  if (n < period) {
    return bars.map((bar) => ({ time: bar.time, value: null }))
  }
  const tp = bars.map((b) => (b.high + b.low + b.close) / 3)
  for (let i = 0; i < n; i++) {
    if (i < period - 1) {
      out.push({ time: bars[i].time, value: null })
      continue
    }
    const win = tp.slice(i - period + 1, i + 1)
    const ma = win.reduce((s, x) => s + x, 0) / period
    const md = win.reduce((s, x) => s + Math.abs(x - ma), 0) / period
    out.push({ time: bars[i].time, value: md === 0 ? 0 : round((tp[i] - ma) / (0.015 * md), 4) })
  }
  return out
}

/** Aroon（25）：up/down 为距最近高/新低的周期占比，返回 { up, down }。口径对齐后端 aroon。 */
export function aroon(bars: ReplayKlineBar[], period = 25): {
  up: IndicatorSeries
  down: IndicatorSeries
} {
  const n = bars.length
  const up: IndicatorSeries = []
  const down: IndicatorSeries = []
  if (n < period + 1) {
    return {
      up: bars.map((b) => ({ time: b.time, value: null })),
      down: bars.map((b) => ({ time: b.time, value: null })),
    }
  }
  const hs = bars.map((b) => b.high)
  const ls = bars.map((b) => b.low)
  for (let i = 0; i < n; i++) {
    if (i < period) {
      up.push({ time: bars[i].time, value: null })
      down.push({ time: bars[i].time, value: null })
      continue
    }
    const wh = hs.slice(i - period, i + 1)
    const wl = ls.slice(i - period, i + 1)
    // 与后端一致：取窗口内"最近一次"出现的极值（max 左侧优先，再倒序找最后一次）
    const hiIdx = wh.length - 1 - wh.slice().reverse().indexOf(Math.max(...wh))
    const loIdx = wl.length - 1 - wl.slice().reverse().indexOf(Math.min(...wl))
    up.push({ time: bars[i].time, value: round((100 * hiIdx) / period, 2) })
    down.push({ time: bars[i].time, value: round((100 * loIdx) / period, 2) })
  }
  return { up, down }
}

/** ADX（14）Wilder 趋势强度：返回 { adx, plusDI, minusDI }，口径对齐后端 adx。 */
export function adx(bars: ReplayKlineBar[], period = 14): {
  adx: IndicatorSeries
  plusDI: IndicatorSeries
  minusDI: IndicatorSeries
} {
  const n = bars.length
  const adxOut: IndicatorSeries = []
  const pdiOut: IndicatorSeries = []
  const ndiOut: IndicatorSeries = []
  if (n < period + 1) {
    for (let i = 0; i < n; i++) {
      adxOut.push({ time: bars[i].time, value: null })
      pdiOut.push({ time: bars[i].time, value: null })
      ndiOut.push({ time: bars[i].time, value: null })
    }
    return { adx: adxOut, plusDI: pdiOut, minusDI: ndiOut }
  }
  const hs = bars.map((b) => b.high)
  const ls = bars.map((b) => b.low)
  const cs = bars.map((b) => b.close)
  const tr: number[] = new Array(n).fill(0)
  const pdm: number[] = new Array(n).fill(0)
  const ndm: number[] = new Array(n).fill(0)
  for (let i = 1; i < n; i++) {
    tr[i] = Math.max(hs[i] - ls[i], Math.abs(hs[i] - cs[i - 1]), Math.abs(ls[i] - cs[i - 1]))
    const up = hs[i] - hs[i - 1]
    const dn = ls[i - 1] - ls[i]
    pdm[i] = up > dn && up > 0 ? up : 0
    ndm[i] = dn > up && dn > 0 ? dn : 0
  }
  let sTr = tr.slice(1, period + 1).reduce((s, x) => s + x, 0)
  let sPdm = pdm.slice(1, period + 1).reduce((s, x) => s + x, 0)
  let sNdm = ndm.slice(1, period + 1).reduce((s, x) => s + x, 0)
  const dx: (number | null)[] = new Array(n).fill(null)
  for (let i = 0; i < n; i++) {
    if (i < period) {
      adxOut.push({ time: bars[i].time, value: null })
      pdiOut.push({ time: bars[i].time, value: null })
      ndiOut.push({ time: bars[i].time, value: null })
      continue
    }
    if (i > period) {
      sTr = sTr - sTr / period + tr[i]
      sPdm = sPdm - sPdm / period + pdm[i]
      sNdm = sNdm - sNdm / period + ndm[i]
    }
    const pdi = sTr > 0 ? (100 * sPdm) / sTr : 0
    const ndi = sTr > 0 ? (100 * sNdm) / sTr : 0
    pdiOut.push({ time: bars[i].time, value: round(pdi, 2) })
    ndiOut.push({ time: bars[i].time, value: round(ndi, 2) })
    const denom = pdi + ndi
    dx[i] = denom > 0.01 ? round((100 * Math.abs(pdi - ndi)) / denom, 4) : 0
    if (i === period) adxOut.push({ time: bars[i].time, value: null })
  }
  const first = 2 * period - 1
  if (first >= n) return { adx: adxOut, plusDI: pdiOut, minusDI: ndiOut }
  const window = dx.slice(period, first + 1).filter((x): x is number => x !== null)
  if (window.length === 0) return { adx: adxOut, plusDI: pdiOut, minusDI: ndiOut }
  let avgDx = window.reduce((s, x) => s + x, 0) / window.length
  for (let i = 0; i < n; i++) {
    if (i < first) {
      if (!adxOut[i]) adxOut[i] = { time: bars[i].time, value: null }
      continue
    }
    if (i === first) {
      adxOut[i] = { time: bars[i].time, value: round(avgDx, 2) }
      continue
    }
    if (dx[i] !== null) {
      avgDx = (avgDx * (period - 1) + (dx[i] as number)) / period
      adxOut[i] = { time: bars[i].time, value: round(avgDx, 2) }
    } else if (!adxOut[i]) {
      adxOut[i] = { time: bars[i].time, value: null }
    }
  }
  return { adx: adxOut, plusDI: pdiOut, minusDI: ndiOut }
}

/** 滚动已实现波动：收益率样本标准差（%，未年化），口径对齐后端 realized_vol。 */
export function realizedVol(bars: ReplayKlineBar[], period = 20): IndicatorSeries {
  const n = bars.length
  const out: IndicatorSeries = []
  if (n < period + 1) {
    return bars.map((b) => ({ time: b.time, value: null }))
  }
  const rets = new Array<number>(n).fill(0)
  for (let i = 1; i < n; i++) {
    const prev = bars[i - 1].close
    rets[i] = prev !== 0 ? ((bars[i].close - prev) / prev) * 100 : 0
  }
  for (let i = 0; i < n; i++) {
    if (i < period) {
      out.push({ time: bars[i].time, value: null })
      continue
    }
    const w = rets.slice(i - period + 1, i + 1)
    const mean = w.reduce((s, x) => s + x, 0) / w.length
    const variance = w.reduce((s, x) => s + (x - mean) ** 2, 0) / w.length
    out.push({ time: bars[i].time, value: round(Math.sqrt(variance), 4) })
  }
  return out
}

/** Ichimoku 云图：转换线/基准线/先行带 A/B（前移 26），口径对齐后端 ichimoku。 */
export function ichimoku(
  bars: ReplayKlineBar[],
  opts: { tenkan?: number; kijun?: number; senkouB?: number; displacement?: number } = {},
): {
  tenkan: IndicatorSeries
  kijun: IndicatorSeries
  senkouA: IndicatorSeries
  senkouB: IndicatorSeries
} {
  const tenkanP = opts.tenkan ?? 9
  const kijunP = opts.kijun ?? 26
  const senkouBP = opts.senkouB ?? 52
  const disp = opts.displacement ?? 26
  const n = bars.length
  const hs = bars.map((b) => b.high)
  const ls = bars.map((b) => b.low)
  const mid = (period: number, idx: number): number | null => {
    if (idx < period - 1) return null
    const winH = hs.slice(idx - period + 1, idx + 1)
    const winL = ls.slice(idx - period + 1, idx + 1)
    return (Math.max(...winH) + Math.min(...winL)) / 2
  }
  const tenkan: IndicatorSeries = []
  const kijun: IndicatorSeries = []
  const senkouBRaw: (number | null)[] = []
  const senkouARaw: (number | null)[] = []
  for (let i = 0; i < n; i++) {
    const t = mid(tenkanP, i)
    const k = mid(kijunP, i)
    const b = mid(senkouBP, i)
    tenkan.push({ time: bars[i].time, value: t === null ? null : round(t) })
    kijun.push({ time: bars[i].time, value: k === null ? null : round(k) })
    senkouBRaw.push(b)
    senkouARaw.push(t !== null && k !== null ? (t + k) / 2 : null)
  }
  const shift = <T,>(src: (T | null)[]): (T | null)[] => {
    const out: (T | null)[] = new Array(disp).fill(null)
    for (let i = 0; i < n - disp; i++) out.push(src[i])
    return out
  }
  const senkouA = shift(senkouARaw)
  const senkouB = shift(senkouBRaw)
  return {
    tenkan,
    kijun,
    senkouA: senkouA.map((v, i) => ({ time: bars[i].time, value: v === null ? null : round(v) })),
    senkouB: senkouB.map((v, i) => ({ time: bars[i].time, value: v === null ? null : round(v) })),
  }
}
