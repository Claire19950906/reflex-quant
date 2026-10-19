/**
 * SegmentHistoryHeatmap — 8xN 段位热力图 (v302+ P2 PR-16)。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { SegmentHistoryHeatmap } from './segment-history-heatmap'
import { FIXED_STAGE_KEYS } from '@/lib/segment-drift'

const apiGetMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiGet: (...args: unknown[]) => (apiGetMock as any)(...args),
}))

beforeEach(() => {
  apiGetMock.mockReset()
})

afterEach(() => {
  cleanup()
})

const sample = {
  days: 7,
  baseline: {
    总框架: 0.95, 因素定位: 0.88, 驱动力: 0.72, 水位博弈: 0.65,
    跨资产资金: 0.45, 反共识结论: 0.5, 逻辑自检: 0.6, 总结: 0.8,
  },
  items: [
    { date: '2026-10-06', count: 12, stages: {
      总框架: 1.0, 因素定位: 0.9, 驱动力: 0.7, 水位博弈: 0.6,
      跨资产资金: 0.9, 反共识结论: 0.5, 逻辑自检: 0.6, 总结: 0.8,
    }},
    { date: '2026-10-07', count: 8, stages: {
      总框架: 0.4, 因素定位: 0.85, 驱动力: 0.75, 水位博弈: 0.7,
      跨资产资金: 0.5, 反共识结论: 0.5, 逻辑自检: 0.6, 总结: 0.2,
    }},
    { date: '2026-10-08', count: 0, stages: {
      总框架: 0, 因素定位: 0, 驱动力: 0, 水位博弈: 0,
      跨资产资金: 0, 反共识结论: 0, 逻辑自检: 0, 总结: 0,
    }},
  ],
}

describe('SegmentHistoryHeatmap — 渲染边界', () => {
  it('items=[] → 不渲染', async () => {
    apiGetMock.mockResolvedValue({ days: 7, baseline: {}, items: [] })
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    expect(container.querySelector('[data-testid="segment-history-heatmap"]')).toBeNull()
  })

  it('失败 → 显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('boom'))
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    expect(container.textContent).toContain('段位快照加载失败')
  })
})

describe('SegmentHistoryHeatmap — 8 行 × N 列', () => {
  beforeEach(() => {
    apiGetMock.mockResolvedValue(sample)
  })

  it('渲染 8 个段名行', async () => {
    render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    for (const k of FIXED_STAGE_KEYS) {
      expect(screen.getAllByText(k).length).toBeGreaterThan(0)
    }
  })

  it('渲染 3 个日期列头(MM-DD)', async () => {
    render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    expect(screen.getByText('10-06')).toBeTruthy()
    expect(screen.getByText('10-07')).toBeTruthy()
    expect(screen.getByText('10-08')).toBeTruthy()
  })

  it('总 cell 数 = 8 × 3 = 24', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const cells = container.querySelectorAll('[data-testid^="heatmap-cell-"]')
    expect(cells.length).toBe(24)
  })

  it('data-delta / data-count 调试属性齐全', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const cell = container.querySelector('[data-stage="跨资产资金"][data-date="2026-10-06"]')
    expect(cell?.getAttribute('data-delta')).toBe('0.450')
    expect(cell?.getAttribute('data-count')).toBe('12')
  })
})

describe('SegmentHistoryHeatmap — 颜色 / 文字', () => {
  beforeEach(() => {
    apiGetMock.mockResolvedValue(sample)
  })

  it('delta >= +0.30 → 深绿', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const cell = container.querySelector('[data-stage="跨资产资金"][data-date="2026-10-06"]')
    expect(cell?.className).toContain('green')
    expect(cell?.textContent).toBe('+45pp')
  })

  it('delta <= -0.30 → 深红', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const cell = container.querySelector('[data-stage="总框架"][data-date="2026-10-07"]')
    expect(cell?.className).toContain('red')
    expect(cell?.textContent).toBe('-55pp')
  })

  it('|delta| < 0.15 → ±0', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const cell = container.querySelector('[data-stage="反共识结论"][data-date="2026-10-06"]')
    expect(cell?.textContent).toBe('±0')
  })

  it('count=0 → — + 灰色', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const cell = container.querySelector('[data-stage="总框架"][data-date="2026-10-08"]')
    expect(cell?.textContent).toBe('—')
    expect(cell?.className).toContain('gray')
  })
})

describe('SegmentHistoryHeatmap — tooltip + 图例', () => {
  beforeEach(() => {
    apiGetMock.mockResolvedValue(sample)
  })

  it('cell title 含段名 + 日期 + count + delta 文字', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const cell = container.querySelector('[data-stage="跨资产资金"][data-date="2026-10-06"]')
    const title = cell?.getAttribute('title') ?? ''
    expect(title).toContain('跨资产资金')
    expect(title).toContain('2026-10-06')
    expect(title).toContain('12')
    expect(title).toContain('+45pp')
  })

  it('图例文字含"基线样本"+ 总条数 + 天数', async () => {
    const { container } = render(<SegmentHistoryHeatmap days={7} />)
    await new Promise((r) => setTimeout(r, 10))
    const text = container.textContent ?? ''
    expect(text).toContain('时段基线')
    expect(text).toContain('20')
    expect(text).toContain('3')
  })
})
