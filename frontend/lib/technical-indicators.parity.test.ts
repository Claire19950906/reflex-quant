/**
 * 前端 / 后端指标一致性测试（parity）。
 *
 * 为什么存在这个文件：
 *   技术指标有两套实现 —— 后端 `backend/src_python/technical_indicators.py`（决策链在用）
 *   与前端 `frontend/lib/technical-indicators.ts`（K 线图渲染用）。两套并行就会漂移，
 *   而且历史上确实漂过：布林带用了样本标准差、RSI/ATR 整条序列错位一根 bar、
 *   EMA 换成了 SMA 播种。这类漂移**不会报错**，只会让人在图上看到一个值、
 *   在读数里看到另一个值，然后在拐点上得出相反结论。
 *
 * 做法：
 *   后端拿真实 BTC 4h K 线算一遍，连同输入 bars 一起写成
 *   `frontend/lib/__fixtures__/indicator-parity.json`；这里逐点断言。
 *   重新生成：`python3 backend/scripts/export_indicator_parity_fixture.py --asset BTC --timeframe 4h --bars 240`
 *
 * 容差说明：前端为显示做了 round（EMA/SMA 4 位、RSI 2 位、ATR 4 位），后端不 round，
 * 所以容差取"round 半格 + 一点浮点余量"。
 */
import { describe, it, expect } from 'vitest'
import fixtureJson from './__fixtures__/indicator-parity.json'
import type { ReplayKlineBar } from '@/lib/replay-data'
import { sma, ema, bollinger, bollingerWidthPct, rsi, atr, macd, kdj, mfi, obv, stochRsi, vwap, donchian, supertrend, cci, aroon, stoch, adx, ichimoku, realizedVol } from './technical-indicators'

type Fixture = {
  meta: { asset: string; timeframe: string }
  bars: ReplayKlineBar[]
  expected: {
    sma20: (number | null)[]
    ema20: number[]
    rsi14: (number | null)[]
    atr14: (number | null)[]
    bb20: { upper: (number | null)[]; mid: (number | null)[]; lower: (number | null)[] }
    bb20_width_pct: (number | null)[]
    macd: { macd: number[]; signal: number[]; hist: number[] }
    kdj: { k: (number | null)[]; d: (number | null)[]; j: (number | null)[] }
    mfi14: (number | null)[]
    obv: number[]
    stoch_rsi14: { k: (number | null)[]; d: (number | null)[] }
    vwap: (number | null)[]
    donchian20: { upper: (number | null)[]; mid: (number | null)[]; lower: (number | null)[]; position: (number | null)[] }
    supertrend10: { line: (number | null)[]; direction: (number | null)[] }
    cci20: (number | null)[]
    aroon25: { up: (number | null)[]; down: (number | null)[] }
    adx14: { adx: (number | null)[]; plus_di: (number | null)[]; minus_di: (number | null)[] }
    ichimoku: { tenkan: (number | null)[]; kijun: (number | null)[]; senkou_a: (number | null)[]; senkou_b: (number | null)[]; chikou: (number | null)[] }
    realized_vol20: (number | null)[]
    stoch14: (number | null)[]
  }
}

const fx = fixtureJson as unknown as Fixture

function cmpSeries(actual: (number | null)[], expected: (number | null)[], tol: number, label: string) {
  expect(actual.length, `${label}: 长度`).toBe(expected.length)
  for (let i = 0; i < expected.length; i++) {
    const e = expected[i]
    const a = actual[i]
    if (e == null) {
      expect(a, `${label}: i=${i} 应为 null 前端却是 ${a}`).toBeNull()
      continue
    }
    expect(a, `${label}: i=${i} 后端 ${e} 但前端为 null（暖机位差一根？）`).not.toBeNull()
    expect(Math.abs((a as number) - e), `${label}: i=${i} 前端 ${a} / 后端 ${e}`).toBeLessThanOrEqual(tol)
  }
}

describe('指标 parity（后端为准，真实 BTC 4h 数据）', () => {
  it('fixture 是真实数据（240 根、价格在合理量级）', () => {
    expect(fx.bars.length).toBe(240)
    expect(fx.bars.every((b) => b.close > 0)).toBe(true)
  })

  it('sma(20) 逐点一致', () => {
    const actual = sma(fx.bars, 20).map((p) => p.value)
    cmpSeries(actual, fx.expected.sma20, 1e-3, 'sma20')
  })

  it('ema(20) 逐点一致（种子=首根收盘，无暖机 null）', () => {
    const actual = ema(fx.bars, 20).map((p) => p.value)
    cmpSeries(actual, fx.expected.ema20, 1e-3, 'ema20')
    expect(actual[0], 'EMA 从第 0 根就有值').not.toBeNull()
  })

  it('rsi(14) 逐点一致（含首个有效位在第 14 根）', () => {
    const actual = rsi(fx.bars, 14).map((p) => p.value)
    cmpSeries(actual, fx.expected.rsi14, 0.02, 'rsi14')
    const firstExpected = fx.expected.rsi14.findIndex((v) => v != null)
    expect(actual.findIndex((v) => v != null), 'RSI 首个有效位').toBe(firstExpected)
  })

  it('atr(14) 逐点一致（含首个有效位在第 13 根）', () => {
    const actual = atr(fx.bars, 14).map((p) => p.value)
    cmpSeries(actual, fx.expected.atr14, 1e-3, 'atr14')
    const firstExpected = fx.expected.atr14.findIndex((v) => v != null)
    expect(actual.findIndex((v) => v != null), 'ATR 首个有效位').toBe(firstExpected)
  })

  it('bollinger(20,2) 三轨逐点一致（总体标准差）', () => {
    const bb = bollinger(fx.bars, 20, 2)
    cmpSeries(bb.middle.map((p) => p.value), fx.expected.bb20.mid, 1e-3, 'bb.mid')
    cmpSeries(bb.upper.map((p) => p.value), fx.expected.bb20.upper, 1e-3, 'bb.upper')
    cmpSeries(bb.lower.map((p) => p.value), fx.expected.bb20.lower, 1e-3, 'bb.lower')
  })

  it('macd(12,26,9) 三线逐点一致（DEA=对 DIF 递推，柱不乘 2）', () => {
    const m = macd(fx.bars)
    cmpSeries(m.dif.map((p) => p.value), fx.expected.macd.macd, 1e-3, 'macd.dif')
    cmpSeries(m.dea.map((p) => p.value), fx.expected.macd.signal, 1e-3, 'macd.dea')
    cmpSeries(m.hist.map((p) => p.value), fx.expected.macd.hist, 1e-3, 'macd.hist')
  })

  it('kdj(9,3,3) 三线逐点一致', () => {
    const v = kdj(fx.bars, 9)
    cmpSeries(v.k.map((p) => p.value), fx.expected.kdj.k, 0.02, 'kdj.k')
    cmpSeries(v.d.map((p) => p.value), fx.expected.kdj.d, 0.02, 'kdj.d')
    cmpSeries(v.j.map((p) => p.value), fx.expected.kdj.j, 0.05, 'kdj.j')
  })

  it('bollingerWidthPct 逐点一致', () => {
    const actual = bollingerWidthPct(fx.bars, 20, 2).map((p) => p.value)
    cmpSeries(actual, fx.expected.bb20_width_pct, 1e-3, 'bb.width')
  })

  it('mfi(14) 逐点一致（窗口满期后才出值）', () => {
    const actual = mfi(fx.bars, 14).map((p) => p.value)
    cmpSeries(actual, fx.expected.mfi14, 0.02, 'mfi14')
  })

  it('obv 逐点一致', () => {
    const actual = obv(fx.bars).map((p) => p.value)
    cmpSeries(actual, fx.expected.obv, 0.01, 'obv')
  })

  it('stochRsi(14,3,3) 的 K/D 逐点一致', () => {
    const sr = stochRsi(fx.bars, 14, 3, 3)
    cmpSeries(sr.k.map((p) => p.value), fx.expected.stoch_rsi14.k, 0.11, 'stochRsi.k')
    cmpSeries(sr.d.map((p) => p.value), fx.expected.stoch_rsi14.d, 0.11, 'stochRsi.d')
  })

  it('vwap 逐点一致（累计口径）', () => {
    const actual = vwap(fx.bars).map((p) => p.value)
    cmpSeries(actual, fx.expected.vwap, 0.01, 'vwap')
  })

  it('donchian(20) 上/中/下轨与位置逐点一致', () => {
    const dc = donchian(fx.bars, 20)
    cmpSeries(dc.upper.map((p) => p.value), fx.expected.donchian20.upper, 1e-3, 'dc.upper')
    cmpSeries(dc.mid.map((p) => p.value), fx.expected.donchian20.mid, 1e-3, 'dc.mid')
    cmpSeries(dc.lower.map((p) => p.value), fx.expected.donchian20.lower, 1e-3, 'dc.lower')
    cmpSeries(dc.position.map((p) => p.value), fx.expected.donchian20.position, 1e-3, 'dc.position')
  })

  it('supertrend(10,3) 轨线与方向逐点一致', () => {
    const st = supertrend(fx.bars, 10, 3)
    cmpSeries(st.line.map((p) => p.value), fx.expected.supertrend10.line, 1e-3, 'st.line')
    cmpSeries(st.direction.map((v) => v), fx.expected.supertrend10.direction, 0, 'st.direction')
  })

  it('cci(20) 逐点一致', () => {
    const actual = cci(fx.bars, 20).map((p) => p.value)
    cmpSeries(actual, fx.expected.cci20, 0.02, 'cci20')
  })

  it('aroon(25) up/down 逐点一致', () => {
    const ar = aroon(fx.bars, 25)
    cmpSeries(ar.up.map((p) => p.value), fx.expected.aroon25.up, 0.02, 'aroon.up')
    cmpSeries(ar.down.map((p) => p.value), fx.expected.aroon25.down, 0.02, 'aroon.down')
  })

  it('stoch(14,3) %K 逐点一致', () => {
    const actual = stoch(fx.bars, 14, 3).map((p) => p.value)
    cmpSeries(actual, fx.expected.stoch14, 0.02, 'stoch14')
  })

  it('adx(14) ADX/+DI/-DI 逐点一致', () => {
    const a = adx(fx.bars, 14)
    cmpSeries(a.adx.map((p) => p.value), fx.expected.adx14.adx, 0.02, 'adx')
    cmpSeries(a.plusDI.map((p) => p.value), fx.expected.adx14.plus_di, 0.02, '+DI')
    cmpSeries(a.minusDI.map((p) => p.value), fx.expected.adx14.minus_di, 0.02, '-DI')
  })

  it('ichimoku 转换/基准/先行带 A/B 逐点一致', () => {
    const ic = ichimoku(fx.bars)
    cmpSeries(ic.tenkan.map((p) => p.value), fx.expected.ichimoku.tenkan, 1e-3, 'tenkan')
    cmpSeries(ic.kijun.map((p) => p.value), fx.expected.ichimoku.kijun, 1e-3, 'kijun')
    cmpSeries(ic.senkouA.map((p) => p.value), fx.expected.ichimoku.senkou_a, 1e-3, 'senkouA')
    cmpSeries(ic.senkouB.map((p) => p.value), fx.expected.ichimoku.senkou_b, 1e-3, 'senkouB')
  })

  it('realizedVol(20) 滚动波动率逐点一致', () => {
    const actual = realizedVol(fx.bars, 20).map((p) => p.value)
    cmpSeries(actual, fx.expected.realized_vol20, 1e-3, 'realizedVol20')
  })
})
