/**
 * technical-indicators 业务逻辑测试。
 *
 * 重点守护（v403 起口径以后端 technical_indicators.py 为准）:
 *  - bollinger 用总体标准差 (除以 N)，与后端及 TradingView ta.stdev(biased=true) 一致
 *  - sma / kdj 暖机期前段为 null；ema 从第 0 根起有值（种子=首根收盘）
 *  - rsi 首个有效位在第 period 根；atr 首个有效位在第 period-1 根
 *  - kdj J = 3K - 2D 不变量
 *  - 与后端的逐值一致性由 technical-indicators.parity.test.ts 用真实 bar 锁住
 */
import { describe, it, expect } from 'vitest'
import type { ReplayKlineBar } from '@/lib/replay-data'
import {
  sma, ema, bollinger, bollingerWidthPct,
  rsi, kdj, atr, hasValue,
} from './technical-indicators'

/** 构造等差数列 K 线 (1, 2, 3, ..., N),便于手算期望值。 */
function makeBars(n: number, start = 1): ReplayKlineBar[] {
  return Array.from({ length: n }, (_, i) => ({
    time: 1_700_000_000 + i * 60,
    open: start + i, high: start + i + 0.5, low: start + i - 0.5,
    close: start + i, volume: 1000,
  }))
}

/** 构造常量 K 线 (同一价格),便于测试零方差。 */
function makeConstantBars(n: number, price = 100): ReplayKlineBar[] {
  return Array.from({ length: n }, (_, i) => ({
    time: 1_700_000_000 + i * 60,
    open: price, high: price, low: price, close: price, volume: 1000,
  }))
}

describe('sma', () => {
  it('暖机期 (前 period-1 根) 返回 null', () => {
    const s = sma(makeBars(5), 3)
    expect(s[0].value).toBeNull()
    expect(s[1].value).toBeNull()
    expect(s[2].value).not.toBeNull()
  })
  it('3 根窗口:close=1,2,3 → SMA=2', () => {
    expect(sma(makeBars(3), 3)[2].value).toBe(2)
  })
  it('等差数列 SMA 等于中点', () => {
    // close=1..20, period=20 → SMA = 10.5
    expect(sma(makeBars(20), 20)[19].value).toBe(10.5)
  })
})

describe('ema — v403 起与后端同口径（种子=首根收盘）', () => {
  it('从第 0 根起就有值，首值 = 首根收盘', () => {
    const e = ema(makeBars(5), 3)
    expect(e[0].value).toBe(1)
    expect(e.every((p) => p.value !== null)).toBe(true)
  })
  it('EMA 递推 (k=0.5 for period=3): close=1,2,3,4 → i=3 时 = 3.125', () => {
    // seed=1 → 1.5 → 2.25 → 3.125
    expect(ema(makeBars(4), 3)[3].value).toBe(3.125)
  })
})

describe('bollinger — 总体标准差（与后端一致）', () => {
  it('常量 K 线:带宽应为 0 (variance=0)', () => {
    const { upper, lower, middle } = bollinger(makeConstantBars(20, 100), 20, 2)
    expect(upper[19].value).toBe(100)
    expect(lower[19].value).toBe(100)
    expect(middle[19].value).toBe(100)
  })
  it('暖机期 (前 period-1 根) 三轨都为 null', () => {
    const { upper, lower, middle } = bollinger(makeBars(5), 3)
    for (let i = 0; i < 2; i++) {
      expect(upper[i].value).toBeNull()
      expect(lower[i].value).toBeNull()
      expect(middle[i].value).toBeNull()
    }
  })
  it('等差数列 1..20 带宽应等于 2*sqrt(33.25) (总体标准差)', () => {
    // mean=10.5, sum((x-10.5)^2)=665, population var=665/20=33.25, std≈5.7663
    // upper = 10.5 + 2*5.7663 ≈ 22.0326
    // lower = 10.5 - 2*5.7663 ≈ -1.0326
    const { upper, lower, middle } = bollinger(makeBars(20), 20, 2)
    expect(middle[19].value).toBe(10.5)
    expect(upper[19].value).toBeCloseTo(22.0326, 3)
    expect(lower[19].value).toBeCloseTo(-1.0326, 3)
  })
  it('带宽数值锁定（总体标准差；样本标准差口径会宽 2.6%）', () => {
    const { upper, lower } = bollinger(makeBars(20), 20, 2)
    const width = upper[19].value! - lower[19].value!
    expect(width).toBeCloseTo(23.0653, 3)
  })
  it('period=1 边界:不退化为 NaN', () => {
    const { upper, lower, middle } = bollinger(makeConstantBars(5, 50), 1, 2)
    for (let i = 0; i < 5; i++) {
      expect(middle[i].value).toBe(50)
      expect(upper[i].value).toBe(50)
      expect(lower[i].value).toBe(50)
    }
  })
})

describe('bollingerWidthPct — 跟随 bollinger（总体标准差）', () => {
  it('常量 K 线:宽度 = 0%', () => {
    expect(bollingerWidthPct(makeConstantBars(20, 100), 20, 2)[19].value).toBe(0)
  })
  it('暖机期返回 null', () => {
    const w = bollingerWidthPct(makeBars(5), 3)
    for (let i = 0; i < 2; i++) expect(w[i].value).toBeNull()
  })
  it('等差数列 1..20 宽度百分比 (≈219.669%)', () => {
    // width = 23.0653, mean = 10.5 → 23.0653 / 10.5 * 100 ≈ 219.669%
    expect(bollingerWidthPct(makeBars(20), 20, 2)[19].value).toBeCloseTo(219.669, 2)
  })
})

describe('rsi', () => {
  it('空数组返回空数组', () => {
    expect(rsi([])).toEqual([])
  })
  it('长度 < 2 全部 null', () => {
    expect(rsi(makeBars(1))[0].value).toBeNull()
  })
  it('常量 K 线:暖机期 (i<period) 全 null，第 period 根出首个值', () => {
    // period=14 时,前 14 根 (i=0..13) 为 null，i=14 起有效（与后端一致）
    const r = rsi(makeConstantBars(20, 100), 14)
    for (let i = 0; i < 14; i++) expect(r[i].value).toBeNull()
    expect(r[14].value).not.toBeNull()
  })
  it('常量 K 线 (无变化):i>=14 时 RSI=100 (avgLoss=0 兜底)', () => {
    const r = rsi(makeConstantBars(20, 100), 14)
    expect(r[14].value).toBe(100)
    expect(r[19].value).toBe(100)
  })
  it('单调递增序列:RSI 应接近 100', () => {
    expect(rsi(makeBars(20), 14)[19].value!).toBeGreaterThan(95)
  })
})

describe('kdj', () => {
  it('暖机期 (i < period-1) 三轨都为 null;数组长度=bars 长度', () => {
    // period=9, 5 根 bars → 全部暖机,k/d/j 全 null
    const { k, d, j } = kdj(makeBars(5), 9)
    expect(k).toHaveLength(5)
    expect(d).toHaveLength(5)
    expect(j).toHaveLength(5)
    for (let i = 0; i < 5; i++) {
      expect(k[i].value).toBeNull()
      expect(d[i].value).toBeNull()
      expect(j[i].value).toBeNull()
    }
  })
  it('20 根 K 线: i<8 暖机 null, i>=8 有效', () => {
    const { k, d, j } = kdj(makeBars(20), 9)
    for (let i = 0; i < 8; i++) {
      expect(k[i].value).toBeNull()
    }
    for (let i = 8; i < 20; i++) {
      expect(k[i].value).not.toBeNull()
      expect(d[i].value).not.toBeNull()
      expect(j[i].value).not.toBeNull()
    }
  })
  it('J = 3K - 2D 不变量 (精度 1 位,k/d 已 round(2) 故允许 0.05 误差)', () => {
    const { k, d, j } = kdj(makeBars(20), 9)
    for (let i = 8; i < 20; i++) {
      const kk = k[i].value!, dd = d[i].value!, jj = j[i].value!
      // k/d 都 round 到 2 位,j 也 round 到 2 位;3*k-2*d 会有 ±0.05 量级误差
      expect(jj).toBeCloseTo(3 * kk - 2 * dd, 1)
    }
  })
})

describe('atr (Wilder)', () => {
  it('constant K-line: TR=0 → ATR=0', () => {
    const a = atr(makeConstantBars(20, 100), 14)
    expect(a[14].value).toBe(0)
  })
  it('暖机期 (i<period-1) 为 null，第 period-1 根出种子值', () => {
    // 与后端一致：种子 = 前 period 个 TR 的均值（含第 0 根 H-L=1，其余 13 根 TR=1.5）
    const a = atr(makeBars(20), 14)
    for (let i = 0; i < 13; i++) expect(a[i].value).toBeNull()
    expect(a[13].value).toBeCloseTo((1 + 13 * 1.5) / 14, 4)
  })
})

describe('hasValue', () => {
  it('null value → false', () => {
    expect(hasValue([{ time: 1, value: null }], 0)).toBe(false)
  })
  it('非 null value → true', () => {
    expect(hasValue([{ time: 1, value: 5 }], 0)).toBe(true)
  })
  it('越界 index → false', () => {
    expect(hasValue([], 0)).toBe(false)
  })
})
