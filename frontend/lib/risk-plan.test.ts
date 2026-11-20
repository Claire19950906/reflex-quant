// lib/risk-plan.test.ts — v412 前端风控档位口径（纯函数，不发请求）
import { describe, expect, it } from 'vitest'
import {
  basisNoteOf,
  positionNoteOf,
  requireNoteOf,
  describeRiskPlan,
  describeSizeFactor,
  riskTone,
  stopDistancePct,
  stopPriceFor,
  type RiskSnapshot,
} from './risk-plan'

const highSnapshot: RiskSnapshot = {
  available: true,
  asset: 'BTC',
  timeframe: '4h',
  asof: 1_760_000_000_000,
  readout: '强趋势·空头主导',
  risk: {
    level: '高',
    size_factor: 0.5,
    stop_atr_multiple: 3,
    atr_pct: 1.98,
    expected_adverse_pct: 1.78,
    drivers: ['波动扩张（带宽 80 分位）', '趋势极端（ADX 59）'],
    note: '高风险：建议仓位×0.5、止损放 3.0 倍 ATR（≈5.93%）；5 根内常见逆风 ≈1.78%',
    score: 5,
  },
}

describe('stopDistancePct', () => {
  it('ATR% × 倍数', () => {
    expect(stopDistancePct(1.98, 3)).toBeCloseTo(5.94, 2)
  })
  it('缺一项就返回 null，不猜', () => {
    expect(stopDistancePct(null, 3)).toBeNull()
    expect(stopDistancePct(2, null)).toBeNull()
  })
})

describe('stopPriceFor', () => {
  it('做多止损在下方、做空在上方', () => {
    expect(stopPriceFor(100, 'BUY', 5)).toBeCloseTo(95)
    expect(stopPriceFor(100, 'SELL', 5)).toBeCloseTo(105)
  })
  it('入场价或方向缺失时不算 —— 不许假装有绝对价', () => {
    expect(stopPriceFor(null, 'BUY', 5)).toBeNull()
    expect(stopPriceFor(100, null, 5)).toBeNull()
  })
})

describe('describeSizeFactor', () => {
  it('小于 1 显示百分比，等于 1 显示满仓', () => {
    expect(describeSizeFactor(0.5)).toBe('基准仓位的 50%')
    expect(describeSizeFactor(0.8)).toBe('基准仓位的 80%')
    expect(describeSizeFactor(1)).toContain('满仓')
    expect(describeSizeFactor(null)).toBe('—')
  })
})

describe('riskTone', () => {
  it('高=警示、中=注意、低=正常、缺=灰', () => {
    expect(riskTone('高')).toBe('short')
    expect(riskTone('中')).toBe('warn')
    expect(riskTone('低')).toBe('long')
    expect(riskTone(null)).toBe('muted')
  })
})

describe('describeRiskPlan', () => {
  it('高风险档把仓位/止损/逆风都翻译出来', () => {
    const view = describeRiskPlan(highSnapshot)
    expect(view).not.toBeNull()
    expect(view!.levelLabel).toBe('高风险')
    expect(view!.sizeLabel).toBe('基准仓位的 50%')
    expect(view!.stopLabel).toContain('3 倍 ATR')
    expect(view!.stopLabel).toContain('5.94')     // 1.98 × 3
    expect(view!.adverseLabel).toBe('≈1.78%')
    expect(view!.tone).toBe('short')
    expect(view!.actionable).toBe(true)
    expect(view!.drivers.length).toBe(2)
  })

  it('没有读数时返回 null —— 前端必须显示"没有读数"而不是中性档位', () => {
    expect(describeRiskPlan(null)).toBeNull()
    expect(describeRiskPlan({ available: false, error: 'no candles' })).toBeNull()
    expect(describeRiskPlan({ available: true, risk: null })).toBeNull()
  })

  it('缺 atr_pct 时止损只显示倍数，不编百分比', () => {
    const view = describeRiskPlan({
      available: true,
      risk: { level: '中', size_factor: 0.8, stop_atr_multiple: 2, atr_pct: null,
              expected_adverse_pct: null, drivers: [], note: null },
    })
    expect(view!.stopLabel).toBe('2 倍 ATR')
    expect(view!.adverseLabel).toBe('—')
    expect(view!.note).toContain('基准仓位的 80%')
  })
})

describe('basisNoteOf', () => {
  it('日线口径说清已验证，日内口径说清不缩仓', () => {
    expect(basisNoteOf('atr_band_daily')).toContain('已验证')
    expect(basisNoteOf('uniform_intraday')).toContain('不缩仓位')
    expect(basisNoteOf('uniform_intraday')).toContain('无依据')
  })
  it('没有口径时如实说未标注，不假装验过', () => {
    expect(basisNoteOf(null)).toContain('未标注')
  })
  it('describeRiskPlan 把口径带上', () => {
    const view = describeRiskPlan({
      available: true,
      risk: { level: '高', size_factor: 1.0, stop_atr_multiple: 2, atr_pct: 2.4,
              expected_adverse_pct: 2.16, drivers: [], note: null,
              size_basis: 'uniform_intraday' },
    })
    expect(view!.basisNote).toContain('不缩仓位')
    expect(view!.sizeLabel).toContain('满仓')
  })
})

describe('positionNoteOf', () => {
  it('把止损距离翻成敞口，并带上风险预算', () => {
    const note = positionNoteOf({ stop_pct: 3.81, position_pct: 26.2, max_risk_pct: 1 })
    expect(note).toContain('止损 3.81%')
    expect(note).toContain('26.2%')
    expect(note).toContain('1%')
  })
  it('有等风险口径时补一句，没有就不编', () => {
    expect(positionNoteOf({ stop_pct: 3.8, position_pct: 26.2, position_pct_equal_risk: 17.3 }))
      .toContain('等风险口径 17.3%')
    expect(positionNoteOf({ stop_pct: 3.8, position_pct: 26.2 })).not.toContain('等风险口径')
  })
  it('缺止损或敞口时不输出假数字', () => {
    expect(positionNoteOf({})).toBe('')
    expect(positionNoteOf({ stop_pct: 3.8 })).toBe('')
  })
})

const snapOf = (risk: Record<string, unknown>) => ({
  available: true, asset: 'BTC', timeframe: '1h', asof: null, bars: 240,
  last_close: null, risk,
}) as never

describe('oddsNote（v418）', () => {
  it('把实测赔率翻成「要赚多少才够本」', () => {
    const view = describeRiskPlan(snapOf({ level: '低', size_factor: 1,
      stop_atr_multiple: 2, atr_pct: 1.0, expected_adverse_pct: 0.45,
      odds_typical: 0.7, require_space_pct: 0.64,
      odds_note: '该周期实测赔率 0.7（顺风中位÷逆风中位）' }), 100000, 'BUY')
    expect(view?.oddsNote).toContain('0.7')
  })
  it('没给赔率就不编，留空', () => {
    const view = describeRiskPlan(snapOf({ level: '高', size_factor: 0.5,
      stop_atr_multiple: 3, atr_pct: 3.9, expected_adverse_pct: 1.76 }), 100000, 'BUY')
    expect(view?.oddsNote).toBe('')
  })
})

describe('holdingNote（v419）', () => {
  it('日线给出最短持有根数，日内明确说「拿久不会变好」', () => {
    const daily = describeRiskPlan(snapOf({ level: '中', size_factor: 0.8,
      stop_atr_multiple: 2, atr_pct: 1.8, expected_adverse_pct: 0.82,
      min_hold_bars: 3, holding_note: '该周期赔率在 3 根处即 >1' }), 4000, 'BUY')
    expect(daily?.holdingNote).toContain('3 根')
    const intraday = describeRiskPlan(snapOf({ level: '低', size_factor: 1,
      stop_atr_multiple: 2, atr_pct: 0.8, expected_adverse_pct: 0.45,
      min_hold_bars: null, holding_note: '拿久不会变好' }), 60000, 'BUY')
    expect(intraday?.holdingNote).toContain('拿久不会变好')
  })
  it('没有结论就留空', () => {
    expect(describeRiskPlan(snapOf({ level: '高', size_factor: 0.5,
      stop_atr_multiple: 3, atr_pct: 3.9 }), 60000, 'BUY')?.holdingNote).toBe('')
  })
})

describe('requireNote（v420）', () => {
  it('给出要求空间、依据与来回成本', () => {
    const view = describeRiskPlan(snapOf({ level: '低', size_factor: 1,
      stop_atr_multiple: 2, atr_pct: 0.85,
      require_space_pct: 0.66, require_basis: 'odds', round_trip_cost_pct: 0.3 }), 60000, 'BUY')
    expect(view?.requireNote).toContain('0.66%')
    expect(view?.requireNote).toContain('赔率线')
    expect(view?.requireNote).toContain('0.3%')
  })
  it('成本地板生效时说清楚是成本而非赔率', () => {
    const view = describeRiskPlan(snapOf({ level: '低', size_factor: 1,
      stop_atr_multiple: 2, atr_pct: 0.1,
      require_space_pct: 0.3, require_basis: 'cost', round_trip_cost_pct: 0.3 }), 60000, 'BUY')
    expect(view?.requireNote).toContain('成本地板')
  })
  it('没有要求空间就留空', () => {
    expect(requireNoteOf({})).toBe('')
  })
})
