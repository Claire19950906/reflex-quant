/**
 * SegmentDriftBadge 组件测试 (v302+ P1)。
 *
 * 守护:
 *  - 无告警 → 绿勾 + "段位稳定"
 *  - 有告警 → AlertTriangle + 计数 + 前 N 条段名徽标
 *  - alert 数量 > 0 → 红色,否则黄色
 *  - max=N 限制显示条数
 *  - compact 模式不展开段名
 *  - 颜色区分 (alert 红色, warn 黄色)
 *  - delta 正负号正确显示 (+/-)
 */
import { describe, it, expect, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SegmentDriftBadge } from './segment-drift-badge'
import { FIXED_STAGE_KEYS, type DriftResult, type DriftAlert } from '@/lib/segment-drift'

afterEach(() => cleanup())

function makeAlert(over: Partial<DriftAlert> = {}): DriftAlert {
  return {
    stage: '跨资产资金',
    current: 0.5,
    baseline: 0.1,
    delta: 0.4,
    severity: 'alert',
    ...over,
  }
}

// 用真实 stage name 绕开 StageKey 类型(测试时为了简短用 a/b/c 不行,
// 改用真实段名 + 不同 delta 凑 5 条)
function makeResultForOrder(): DriftResult {
  const deltas: Array<[typeof FIXED_STAGE_KEYS[number], number, 'alert' | 'warn']> = [
    ['跨资产资金', 0.5, 'alert'],
    ['逻辑自检', 0.4, 'alert'],
    ['驱动力', 0.3, 'alert'],
    ['反共识结论', 0.2, 'warn'],
    ['水位博弈', 0.17, 'warn'],
  ]
  return {
    total: 100,
    current: Object.fromEntries(FIXED_STAGE_KEYS.map((k) => [k, 1])) as DriftResult['current'],
    deltas: Object.fromEntries(deltas.map(([k, d]) => [k, d])) as DriftResult['deltas'],
    alerts: deltas.map(([stage, delta, severity]) => makeAlert({ stage, delta, severity })),
  }
}

function makeResult(alerts: DriftAlert[]): DriftResult {
  return {
    total: 100,
    current: { '总框架': 1, '因素定位': 1, '驱动力': 1, '水位博弈': 1, '跨资产资金': 0.5, '反共识结论': 1, '逻辑自检': 0, '总结': 1 },
    deltas: { '总框架': 0, '因素定位': 0, '驱动力': 0, '水位博弈': 0, '跨资产资金': 0.4, '反共识结论': 0, '逻辑自检': 0, '总结': 0 },
    alerts,
  }
}

describe('SegmentDriftBadge — UI 渲染', () => {
  it('无告警:显示绿勾 + "段位稳定"', () => {
    render(<SegmentDriftBadge result={makeResult([])} />)
    expect(screen.getByText('段位稳定')).toBeTruthy()
    expect(screen.getByTitle('8 段命中率与历史基线一致')).toBeTruthy()
  })

  it('有 alert:显示红数 + 段名徽标', () => {
    render(
      <SegmentDriftBadge
        result={makeResult([
          makeAlert({ stage: '跨资产资金', delta: 0.4, severity: 'alert' }),
        ])}
      />,
    )
    expect(screen.getByText((_, el) => el?.textContent === '跨资产资金 +40pp')).toBeTruthy()
    expect(screen.getByText('段位漂移')).toBeTruthy()
  })

  it('alert + warn 混合:数字按 severity 分色', () => {
    render(
      <SegmentDriftBadge
        result={makeResult([
          makeAlert({ stage: '跨资产资金', delta: 0.4, severity: 'alert' }),
          makeAlert({ stage: '驱动力', delta: 0.17, severity: 'warn' }),
        ])}
      />,
    )
    // 红色 alert 数 1, 黄色 warn 数 1
    const red = document.querySelector('span.text-red-500')
    expect(red?.textContent).toBe('1')
    const yellow = document.querySelector('span.font-bold.text-yellow-600, span.font-bold.text-yellow-300')
    expect(yellow?.textContent).toBe('1')
  })

  it('负向漂移:delta 前缀 "-"', () => {
    render(
      <SegmentDriftBadge
        result={makeResult([
          makeAlert({ stage: '总结', current: 0, baseline: 0.98, delta: -0.98, severity: 'alert' }),
        ])}
      />,
    )
    expect(screen.getByText((_, el) => el?.textContent === '总结 -98pp')).toBeTruthy()
  })

  it('max=N:只显示前 N 条', () => {
    render(<SegmentDriftBadge result={makeResultForOrder()} max={2} />)
    expect(screen.getByText((_, el) => el?.textContent === '跨资产资金 +50pp')).toBeTruthy()
    expect(screen.getByText((_, el) => el?.textContent === '逻辑自检 +40pp')).toBeTruthy()
    expect(screen.queryByText((_, el) => el?.textContent === '驱动力 +30pp')).toBeNull()
    expect(screen.queryByText((_, el) => el?.textContent === '反共识结论 +20pp')).toBeNull()
  })

  it('默认 max=3', () => {
    render(<SegmentDriftBadge result={makeResultForOrder()} />)
    expect(screen.getByText((_, el) => el?.textContent === '跨资产资金 +50pp')).toBeTruthy()
    expect(screen.getByText((_, el) => el?.textContent === '逻辑自检 +40pp')).toBeTruthy()
    expect(screen.getByText((_, el) => el?.textContent === '驱动力 +30pp')).toBeTruthy()
    expect(screen.queryByText((_, el) => el?.textContent === '反共识结论 +20pp')).toBeNull()
  })

  it('compact=true:不显示段名徽标', () => {
    render(
      <SegmentDriftBadge
        result={makeResult([makeAlert({ stage: '跨资产资金', delta: 0.4 })])}
        compact
      />,
    )
    expect(screen.getByText('段位漂移')).toBeTruthy()
    // 段名徽标不再渲染
    expect(screen.queryByText((_, el) => el?.textContent === '跨资产资金 +40pp')).toBeNull()
  })

  it('title 属性含全部 alerts 摘要(不只显示的 max 条)', () => {
    render(<SegmentDriftBadge result={makeResultForOrder()} max={2} />)
    // title 是 span.title 属性; 用 querySelector 找
    const span = document.querySelector('span[title]')!
    const title = span.getAttribute('title')!
    expect(title).toContain('跨资产资金')
    expect(title).toContain('逻辑自检')
    expect(title).toContain('驱动力')
    expect(title).toContain('反共识结论')
  })

  it('0 alert + 多 warn:整体黄色', () => {
    render(
      <SegmentDriftBadge
        result={makeResult([
          makeAlert({ stage: '驱动力', delta: 0.17, severity: 'warn' }),
          makeAlert({ stage: '水位博弈', delta: 0.16, severity: 'warn' }),
        ])}
      />,
    )
    const warnSpan = document.querySelector('span.text-yellow-600, span.text-yellow-300')
    expect(warnSpan).toBeTruthy()
  })

  it('className 透传', () => {
    const { container } = render(
      <SegmentDriftBadge result={makeResult([])} className="ml-2" />,
    )
    const span = container.querySelector('span')!
    expect(span.className).toContain('ml-2')
  })
})
