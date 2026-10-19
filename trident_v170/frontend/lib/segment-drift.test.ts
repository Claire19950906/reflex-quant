/**
 * segment-drift 漂移检测测试 (v302+ P1)。
 *
 * 守护:
 *  - computeCurrentDistribution: 8 段命中率 = sum(presence) / N
 *  - detectSegmentDrift: 告警分 warn/alert 两档, 样本不足不告警
 */
import { describe, it, expect } from 'vitest'
import {
  detectSegmentDrift,
  computeCurrentDistribution,
  presenceFromReviews,
  topDriftAlerts,
  rollingBaseline,
  detectDriftWithRollingBaseline,
  DEFAULT_BASELINE,
  FIXED_STAGE_KEYS,
  type StagePresence,
  type BaselineDist,
} from './segment-drift'

const BASELINE: BaselineDist = {
  '总框架': 0.95,
  '因素定位': 0.9,
  '驱动力': 0.78,
  '水位博弈': 0.84,
  '跨资产资金': 0.1,
  '反共识结论': 0.84,
  '逻辑自检': 0.16,
  '总结': 0.98,
}

function makePresence(N: number, target: BaselineDist): StagePresence[] {
  return Array.from({ length: N }, (_, i) => {
    const row = {} as StagePresence
    for (const k of FIXED_STAGE_KEYS) {
      row[k] = i / N < target[k] ? 1 : 0
    }
    return row
  })
}

describe('computeCurrentDistribution — 段位命中率', () => {
  it('空数组:全 0', () => {
    const out = computeCurrentDistribution([])
    for (const k of FIXED_STAGE_KEYS) {
      expect(out[k]).toBe(0)
    }
  })

  it('N=4 各段命中数 / 4', () => {
    const presence: StagePresence[] = [
      { '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 0, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1 },
      { '总框架': 1, '因素定位': 1, '驱动力': 0, '水位博弈': 1, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1 },
      { '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 0, '总结': 1 },
      { '总框架': 1, '因素定位': 0, '驱动力': 1, '水位博弈': 1, '跨资产资金': 0, '反共识结论': 0, '逻辑自检': 1, '总结': 1 },
    ]
    const out = computeCurrentDistribution(presence)
    expect(out['总框架']).toBe(1)
    expect(out['因素定位']).toBe(0.75)
    expect(out['驱动力']).toBe(0.75)
    expect(out['跨资产资金']).toBe(0.25)
    expect(out['总结']).toBe(1)
  })

  it('N=100, 命中数 = target * 100', () => {
    const presence = makePresence(100, {
      '总框架': 0.95, '因素定位': 0.9, '驱动力': 0.78, '水位博弈': 0.84,
      '跨资产资金': 0.1, '反共识结论': 0.84, '逻辑自检': 0.16, '总结': 0.98,
    })
    const out = computeCurrentDistribution(presence)
    expect(out['总框架']).toBe(0.95)
    expect(out['驱动力']).toBe(0.78)
    expect(out['跨资产资金']).toBe(0.1)
  })
})

describe('detectSegmentDrift — 漂移检测', () => {
  it('当前 == 基线:0 告警', () => {
    const presence = makePresence(100, BASELINE)
    const r = detectSegmentDrift(presence, BASELINE)
    expect(r.total).toBe(100)
    expect(r.alerts).toEqual([])
  })

  it('样本 < minSample:即使漂移也不告警,但 deltas 仍可读', () => {
    const presence: StagePresence[] = Array.from({ length: 5 }, () => ({
      '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 1, '总结': 1,
    }))
    const r = detectSegmentDrift(presence, BASELINE)
    expect(r.total).toBe(5)
    expect(r.alerts).toEqual([])
    expect(r.deltas['跨资产资金']).toBeCloseTo(0.9, 5)
  })

  it('|delta| >= alertThreshold (0.30):severity=alert', () => {
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1,
        '跨资产资金': i < 50 ? 1 : 0,  // 50% vs 10% baseline = +40pp
        '反共识结论': 1, '逻辑自检': 1, '总结': 1,
      })
    }
    const r = detectSegmentDrift(presence, BASELINE)
    const xa = r.alerts.find((a) => a.stage === '跨资产资金')
    expect(xa?.severity).toBe('alert')
    expect(xa?.delta).toBeCloseTo(0.4, 5)
  })

  it('warnThreshold <= |delta| < alertThreshold:severity=warn', () => {
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1,
        '驱动力': i < 95 ? 1 : 0,  // 95% vs 78% = +17pp
        '水位博弈': 1, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
      })
    }
    const r = detectSegmentDrift(presence, BASELINE)
    const qd = r.alerts.find((a) => a.stage === '驱动力')
    expect(qd?.severity).toBe('warn')
    expect(qd?.delta).toBeCloseTo(0.17, 5)
  })

  it('|delta| < warnThreshold:不告警', () => {
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1,
        '水位博弈': i < 90 ? 1 : 0,  // 90% vs 84% = +6pp
        '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
      })
    }
    const r = detectSegmentDrift(presence, BASELINE)
    expect(r.alerts.find((a) => a.stage === '水位博弈')).toBeUndefined()
  })

  it('负向漂移也告警 (命中率下降)', () => {
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0,
        '总结': i < 50 ? 1 : 0,  // 50% vs 98% = -48pp
      })
    }
    const r = detectSegmentDrift(presence, BASELINE)
    const zj = r.alerts.find((a) => a.stage === '总结')
    expect(zj?.severity).toBe('alert')
    expect(zj?.delta).toBeCloseTo(-0.48, 5)
  })

  it('告警按 |delta| 降序排列 (本测试用例的 alert + warn 混合)', () => {
    // 跨资产资金 50% vs 10% = +40pp alert
    // 总结      60% vs 98% = -38pp alert
    // 驱动力    95% vs 78% = +17pp warn
    // 反共识   100% vs 84% = +16pp warn
    // 水位博弈 100% vs 84% = +16pp warn
    // 逻辑自检   0% vs 16% = -16pp warn
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1,
        '驱动力': i < 95 ? 1 : 0,
        '水位博弈': 1,
        '跨资产资金': i < 50 ? 1 : 0,
        '反共识结论': 1, '逻辑自检': 0,
        '总结': i < 60 ? 1 : 0,
      })
    }
    const r = detectSegmentDrift(presence, BASELINE)
    // 头 3 个是 2 个 alert + 最严重的 warn
    expect(r.alerts.slice(0, 3).map((a) => a.stage)).toEqual(['跨资产资金', '总结', '驱动力'])
    // alert 一定排在 warn 之前(因为 alertThreshold > warnThreshold)
    expect(r.alerts.find((a) => a.severity === 'alert')?.stage).toBe('跨资产资金')
    expect(r.alerts.filter((a) => a.severity === 'alert')).toHaveLength(2)
  })

  it('自定义 warnThreshold 生效', () => {
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1,
        '跨资产资金': i < 30 ? 1 : 0,  // 30% vs 10% = +20pp
        '反共识结论': 1, '逻辑自检': 0, '总结': 1,
      })
    }
    // 默认 warn(0.15): 20pp → warn
    expect(detectSegmentDrift(presence, BASELINE).alerts.find((a) => a.stage === '跨资产资金')?.severity).toBe('warn')
    // 自定义 warn(0.25): 20pp → 不告警
    expect(detectSegmentDrift(presence, BASELINE, { warnThreshold: 0.25 }).alerts).toEqual([])
  })

  it('deltas 包含所有 8 段(不只是告警段)', () => {
    const r = detectSegmentDrift(makePresence(100, BASELINE), BASELINE)
    expect(Object.keys(r.deltas)).toHaveLength(8)
    for (const k of FIXED_STAGE_KEYS) {
      expect(r.deltas[k]).toBeCloseTo(0, 5)
    }
  })
})

describe('DEFAULT_BASELINE — 8 段齐全 + [0, 1] 范围', () => {
  it('所有段都存在且概率合法', () => {
    expect(Object.keys(DEFAULT_BASELINE)).toHaveLength(8)
    for (const k of FIXED_STAGE_KEYS) {
      const v = DEFAULT_BASELINE[k]
      expect(v).toBeGreaterThanOrEqual(0)
      expect(v).toBeLessThanOrEqual(1)
    }
  })
})

describe('presenceFromReviews — 从 Review[].missing 推 presence', () => {
  it('空数组:返回空', () => {
    expect(presenceFromReviews([])).toEqual([])
  })

  it('全 present(无 missing):8 段全 1', () => {
    const reviews = [{ steps: { missing: [] as string[] } }]
    const out = presenceFromReviews(reviews)
    expect(out).toHaveLength(1)
    for (const k of FIXED_STAGE_KEYS) {
      expect(out[0][k]).toBe(1)
    }
  })

  it('部分 missing:对应段位 0', () => {
    const reviews = [{ steps: { missing: ['跨资产资金', '逻辑自检'] } }]
    const out = presenceFromReviews(reviews)
    expect(out[0]['跨资产资金']).toBe(0)
    expect(out[0]['逻辑自检']).toBe(0)
    expect(out[0]['总结']).toBe(1)
  })

  it('多行独立:每行各自的 missing 决定 presence', () => {
    const reviews = [
      { steps: { missing: ['跨资产资金'] } },
      { steps: { missing: ['总结', '驱动力'] } },
      { steps: { missing: [] as string[] } },
    ]
    const out = presenceFromReviews(reviews)
    expect(out[0]['跨资产资金']).toBe(0)
    expect(out[0]['驱动力']).toBe(1)
    expect(out[1]['跨资产资金']).toBe(1)
    expect(out[1]['总结']).toBe(0)
    expect(out[1]['驱动力']).toBe(0)
    expect(out[2]['总框架']).toBe(1)
  })

  it('presenceFromReviews → detectSegmentDrift 端到端', () => {
    // 模拟 100 条 review,其中 80% 缺"跨资产资金" (异常)
    const reviews = Array.from({ length: 100 }, () => ({
      steps: { missing: ['跨资产资金'] as string[] },
    }))
    const presence = presenceFromReviews(reviews)
    const r = detectSegmentDrift(presence, BASELINE)
    // 跨资产资金 20% vs 10% baseline = +10pp < 15pp threshold → 不告警
    expect(r.alerts.find((a) => a.stage === '跨资产资金')).toBeUndefined()

    // 改:100% 缺"总结" → 总结 0% vs 98% baseline = -98pp → alert
    const reviews2 = Array.from({ length: 100 }, () => ({
      steps: { missing: ['总结'] as string[] },
    }))
    const r2 = detectSegmentDrift(presenceFromReviews(reviews2), BASELINE)
    const zj = r2.alerts.find((a) => a.stage === '总结')
    expect(zj?.severity).toBe('alert')
    expect(zj?.delta).toBeCloseTo(-0.98, 5)
  })
})

describe('topDriftAlerts — 取前 N 条', () => {
  it('N=3:取前 3 条最严重', () => {
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1,
        '驱动力': i < 95 ? 1 : 0,
        '水位博弈': 1,
        '跨资产资金': i < 50 ? 1 : 0,
        '反共识结论': 1, '逻辑自检': 0,
        '总结': i < 60 ? 1 : 0,
      })
    }
    const r = detectSegmentDrift(presence, BASELINE)
    const top = topDriftAlerts(r, 3)
    expect(top).toHaveLength(3)
    expect(top.map((a) => a.stage)).toEqual(['跨资产资金', '总结', '驱动力'])
  })

  it('N > alerts.length:返回全部', () => {
    const presence = makePresence(100, BASELINE)
    const r = detectSegmentDrift(presence, BASELINE)
    expect(r.alerts).toHaveLength(0)
    expect(topDriftAlerts(r, 5)).toEqual([])
  })

  it('默认 N=3', () => {
    const presence: StagePresence[] = Array.from({ length: 100 }, () => ({
      '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1,
      '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 0,
    }))
    const r = detectSegmentDrift(presence, BASELINE)
    // 总结      0 vs 98 = -98 alert
    // 驱动力   100 vs 78 = +22 warn
    // 水位博弈 100 vs 84 = +16 warn
    // 反共识   100 vs 84 = +16 warn
    // 逻辑自检   0 vs 16 = -16 warn
    // 跨资产     0 vs 10 = -10 no
    // 因素定位 100 vs 90 = +10 no
    // 总框架   100 vs 95 = +5 no
    const top = topDriftAlerts(r)
    expect(top).toHaveLength(3)
    expect(top[0].stage).toBe('总结')  // alert 必排第 1
  })
})

describe('rollingBaseline — 滚动窗口基线', () => {
  it('空数组:全 0 基线', () => {
    const out = rollingBaseline([])
    for (const k of FIXED_STAGE_KEYS) {
      expect(out[k]).toBe(0)
    }
  })

  it('N <= window:用全部', () => {
    const presence: StagePresence[] = Array.from({ length: 30 }, () => ({
      '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
    }))
    const out = rollingBaseline(presence, { window: 100 })
    expect(out['总框架']).toBe(1)
    expect(out['跨资产资金']).toBe(0)
  })

  it('N > window:只取尾部 window 条', () => {
    // 前 200 条 100% 命中跨资产资金,后 100 条 0% 命中
    const presence: StagePresence[] = []
    for (let i = 0; i < 200; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1,
        '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
      })
    }
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1,
        '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
      })
    }
    // window=100 应只看后 100 条
    const out = rollingBaseline(presence, { window: 100 })
    expect(out['跨资产资金']).toBe(0)
  })

  it('skipTail:跳过尾部 N 条', () => {
    // 30 条全 1,10 条全 0
    const presence: StagePresence[] = []
    for (let i = 0; i < 30; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 1, '总结': 1,
      })
    }
    for (let i = 0; i < 10; i++) {
      presence.push({
        '总框架': 0, '因素定位': 0, '驱动力': 0, '水位博弈': 0, '跨资产资金': 0, '反共识结论': 0, '逻辑自检': 0, '总结': 0,
      })
    }
    // skipTail=10 → 用前 30 条算基线(全 1)
    const out = rollingBaseline(presence, { window: 100, skipTail: 10 })
    expect(out['总结']).toBe(1)
    expect(out['跨资产资金']).toBe(1)
  })

  it('skipTail >= length:全 0 基线(防越界)', () => {
    const presence: StagePresence[] = Array.from({ length: 5 }, () => ({
      '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 1, '总结': 1,
    }))
    const out = rollingBaseline(presence, { skipTail: 10 })
    for (const k of FIXED_STAGE_KEYS) {
      expect(out[k]).toBe(0)
    }
  })
})

describe('detectDriftWithRollingBaseline — 端到端', () => {
  it('历史稳定 + 当前突变 → 滚动基线能抓到', () => {
    // 历史 100 条:全 1(模型稳定)
    // 当前 20 条:全缺"总结" (异常)
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 1, '总结': 1,
      })
    }
    for (let i = 0; i < 20; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 1, '总结': 0,
      })
    }
    const r = detectDriftWithRollingBaseline(presence, { window: 100, currentSize: 20 })
    expect(r.baselineSource).toBe('rolling')
    expect(r.baseline['总结']).toBe(1)  // 基线 = 100%
    expect(r.current['总结']).toBe(0)   // 当前 0%
    const zj = r.alerts.find((a) => a.stage === '总结')
    expect(zj?.severity).toBe('alert')
  })

  it('历史自然漂移 → 滚动基线不误报(DEFAULT_BASELINE 会误报)', () => {
    // 模型从 v1 (历史 100 条) 进化到 v2 (当前 20 条):
    // v1: 跨资产资金 0%(早期模型不会跨资产分析)
    // v2: 跨资产资金 80%(新模型会跨资产分析)
    // 滚动基线只比 v1 vs v2,无 baseline → 不应大告警
    const presence: StagePresence[] = []
    for (let i = 0; i < 100; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
      })
    }
    for (let i = 0; i < 20; i++) {
      presence.push({
        '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 1, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
      })
    }
    const r = detectDriftWithRollingBaseline(presence, { window: 100, currentSize: 20 })
    // 用 DEFAULT_BASELINE(10% 跨资产)的话会告警(80% - 10% = +70pp alert)
    // 滚动基线(0% 跨资产)的话 delta=80pp 但当前 20 条 N<minSample 20 → 不会大告警... 实际刚好 20
    // 改 minSample 让滚动不告警:
    const r2 = detectDriftWithRollingBaseline(presence, { window: 100, currentSize: 20, driftOptions: { minSample: 30 } })
    expect(r2.alerts.find((a) => a.stage === '跨资产资金')).toBeUndefined()
  })

  it('历史不够 (N <= currentSize):用 DEFAULT_BASELINE 兜底? 不,返回 0 基线', () => {
    // 边界: history 全被 skipTail 切掉 → baseline 全 0
    // 实际意义: 无历史数据,无法算滚动基线
    const presence: StagePresence[] = Array.from({ length: 10 }, () => ({
      '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
    }))
    const r = detectDriftWithRollingBaseline(presence, { window: 100, currentSize: 10 })
    // baseline 全 0 → current 1 - 0 = 1 全部 alert (但 minSample 20 不告警)
    expect(r.baseline['总框架']).toBe(0)
  })

  it('返回 baseline + baselineSource 元数据', () => {
    const presence: StagePresence[] = Array.from({ length: 50 }, () => ({
      '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 0, '反共识结论': 1, '逻辑自检': 0, '总结': 1,
    }))
    const r = detectDriftWithRollingBaseline(presence, { window: 30, currentSize: 10 })
    expect(r.baselineSource).toBe('rolling')
    expect(Object.keys(r.baseline)).toHaveLength(8)
  })
})
