/**
 * v458 · 副图数据装配的断言。
 *
 * 这些断言不是"跑得过就行"，每条都锁一个具体口径：
 *   - RSI 暖机段必须被裁掉（否则图上第一条线从 0 起跳，看着像暴跌）；
 *   - MACD 柱必须等于 DIF-DEA（乘 2 的写法在别的平台常见，这里不乘）；
 *   - KDJ 的 J 必须等于 3K-2D（三个值各算各的会互相打架）；
 *   - 数据不够就不画（宁可空着，不画半条线）。
 */
import { describe, expect, it } from 'vitest'
import { SUB_MIN_BARS, buildSubIndicators, type SubBar } from './live-sub-indicators'

/** 确定性合成行情：固定种子的伪随机游走，避免测试依赖外部数据 */
function makeBars(n: number, seed = 7): SubBar[] {
  let s = seed
  const rnd = () => {
    s = (s * 1103515245 + 12345) % 2147483648
    return s / 2147483648
  }
  const bars: SubBar[] = []
  let close = 100
  for (let i = 0; i < n; i++) {
    const drift = (rnd() - 0.5) * 2
    const open = close
    close = Math.max(1, close + drift)
    bars.push({
      time: 1_700_000_000 + i * 3600,
      open,
      high: Math.max(open, close) + rnd(),
      low: Math.min(open, close) - rnd(),
      close,
      volume: 1000 + rnd() * 100,
    })
  }
  return bars
}

describe('buildSubIndicators', () => {
  const bars = makeBars(120)

  it('RSI：暖机期被裁掉，值落在 0~100，柱等于 RSI-50', () => {
    const { lines, hist } = buildSubIndicators('rsi', bars)
    expect(lines).toHaveLength(1)
    expect(hist).toHaveLength(1)
    const rsiPts = lines[0].points
    // Wilder RSI(14)：前 14 根为 null，所以正好少 14 个点
    expect(rsiPts).toHaveLength(bars.length - 14)
    for (const p of rsiPts) {
      expect(p.value).toBeGreaterThanOrEqual(0)
      expect(p.value).toBeLessThanOrEqual(100)
    }
    // 柱与线逐点同长，且 dev = v - 50
    expect(hist[0].points).toHaveLength(rsiPts.length)
    for (let i = 0; i < rsiPts.length; i++) {
      expect(hist[0].points[i].time).toBe(rsiPts[i].time)
      expect(hist[0].points[i].value).toBeCloseTo(rsiPts[i].value - 50, 6)
    }
  })

  it('RSI：超买段上红色、超卖段上绿色（配色不串）', () => {
    const { hist } = buildSubIndicators('rsi', bars)
    for (const p of hist[0].points) {
      const v = p.value + 50
      if (v >= 70) expect(p.color).toBe('rgba(239, 68, 68, 0.85)')
      else if (v <= 30) expect(p.color).toBe('rgba(46, 189, 133, 0.85)')
    }
  })

  it('MACD：DIF/DEA 同长，柱 = DIF - DEA（不乘 2）', () => {
    const { lines, hist } = buildSubIndicators('macd', bars)
    expect(lines).toHaveLength(2)
    expect(hist).toHaveLength(1)
    const dif = lines[0].points
    const dea = lines[1].points
    expect(dif).toHaveLength(bars.length)
    expect(dea).toHaveLength(bars.length)
    expect(hist[0].points).toHaveLength(bars.length)
    for (let i = 0; i < dif.length; i++) {
      expect(dea[i].time).toBe(dif[i].time)
      expect(hist[0].points[i].value).toBeCloseTo(dif[i].value - dea[i].value, 5)
    }
  })

  it('MACD：柱的正负与颜色一致（负值配红，正值配绿）', () => {
    const { hist } = buildSubIndicators('macd', bars)
    for (const p of hist[0].points) {
      if (p.value >= 0) expect(p.color).toBe('rgba(46, 189, 133, 0.65)')
      else expect(p.color).toBe('rgba(239, 68, 68, 0.65)')
    }
  })

  it('KDJ：三线同长，J = 3K - 2D', () => {
    const { lines, hist } = buildSubIndicators('kdj', bars)
    expect(lines).toHaveLength(3)
    expect(hist).toHaveLength(0)
    const k = lines[0].points
    const d = lines[1].points
    const j = lines[2].points
    expect(k).toHaveLength(bars.length - 8) // period=9，前 8 根空
    expect(d).toHaveLength(k.length)
    expect(j).toHaveLength(k.length)
    for (let i = 0; i < k.length; i++) {
      // K/D/J 各自 round 到 2 位小数：J 的误差上限 = 3×0.005 + 2×0.005 = 0.025，
      // 所以这里比的是"公式成立"，不是"逐位相等"。
      expect(Math.abs(j[i].value - (3 * k[i].value - 2 * d[i].value))).toBeLessThan(0.03)
    }
  })

  it('数据不足就不画：少于 SUB_MIN_BARS 根返回空', () => {
    const short = makeBars(SUB_MIN_BARS - 1)
    for (const kind of ['rsi', 'macd', 'kdj'] as const) {
      const b = buildSubIndicators(kind, short)
      expect(b.lines).toHaveLength(0)
      expect(b.hist).toHaveLength(0)
    }
  })
})
