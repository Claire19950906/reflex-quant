import { render, screen, waitFor, within, cleanup } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { MissedSignalsPanel } from './MissedSignalsPanel'

const mockMissed = {
  ok: true,
  count: 2,
  rows: [
    { id: 1, asset: 'BTC', decision_date: '2025-10-15', decision_id: 100,
      signal_id: 5, pattern_type: 'vol_regime', signal: 'CAUTION',
      ignored_reason: 'low vol context', confidence_at_time: 0.65,
      actual_outcome_5d: 0.03, actual_outcome_20d: 0.07, hit: 1,
      created_at: '2025-10-15T10:00:00' },
    { id: 2, asset: 'ETH', decision_date: '2025-10-14', decision_id: 101,
      signal_id: 6, pattern_type: 'mean_revert', signal: 'SUPPORT',
      ignored_reason: '', confidence_at_time: 0.55,
      actual_outcome_5d: -0.02, actual_outcome_20d: -0.01, hit: 0,
      created_at: '2025-10-14T10:00:00' },
  ],
}
const mockCalibration = {
  ok: true,
  count: 1,
  rows: [
    { id: 1, week_start: '2025-10-13', asset: 'BTC', pattern_type: 'vol_regime',
      n_signals: 8, n_hit: 5, hit_rate: 0.625, avg_edge_5d: 0.012,
      avg_confidence: 0.7, confidence_calibration: 0.075,
      recommendation: 'hold', notes: '', created_at: '2025-10-13T08:00:00' },
  ],
}
const mockQuality = {
  ok: true,
  by_pattern: [
    { pattern_type: 'vol_regime', n: 8, n_hit: 5, hit_rate: 0.625,
      avg_confidence: 0.7, confidence_calibration: 0.075, avg_edge_5d: 0.012 },
  ],
  overall: { n: 8, n_hit: 5, hit_rate: 0.625, avg_confidence: 0.7,
    overconfidence_gap: 0.075 },
  trend: [
    { week_start: '2025-08-04', n: 6, n_hit: 4, hit_rate: 0.667, avg_confidence: 0.65, overconfidence_gap: -0.017 },
    { week_start: '2025-08-11', n: 8, n_hit: 5, hit_rate: 0.625, avg_confidence: 0.7, overconfidence_gap: 0.075 },
    { week_start: '2025-08-18', n: 5, n_hit: 2, hit_rate: 0.4, avg_confidence: 0.7, overconfidence_gap: 0.3 },
  ],
}

beforeEach(() => {
  global.fetch = vi.fn((url: string) => {
    if (url.includes('missed-signals')) {
      return Promise.resolve({ json: () => Promise.resolve(mockMissed) })
    }
    if (url.includes('calibration')) {
      return Promise.resolve({ json: () => Promise.resolve(mockCalibration) })
    }
    if (url.includes('judgment-quality')) {
      return Promise.resolve({ json: () => Promise.resolve(mockQuality) })
    }
    return Promise.reject(new Error('unknown url: ' + url))
  }) as unknown as typeof fetch
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MissedSignalsPanel', () => {
  it('renders 3 tabs and shows missed list by default', async () => {
    render(<MissedSignalsPanel />)
    expect(screen.getByText('Missed Signal Reflection (v237)')).toBeInTheDocument()
    expect(screen.getByText('漏检信号')).toBeInTheDocument()
    expect(screen.getByText('校准曲线')).toBeInTheDocument()
    expect(screen.getByText('判断力评分')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getAllByText('BTC').length).toBeGreaterThan(0)
    })
    const date = screen.getAllByText('2025-10-15')[0]
    expect(date).toBeInTheDocument()
  })

  it('switches to calibration tab and shows week row', async () => {
    render(<MissedSignalsPanel />)
    await waitFor(() => {
      expect(screen.getAllByText('BTC').length).toBeGreaterThan(0)
    })
    const calTab = screen.getByRole('button', { name: '校准曲线' })
    calTab.click()
    await waitFor(() => {
      expect(screen.getByText('2025-10-13')).toBeInTheDocument()
    })
    expect(screen.getByText('hold')).toBeInTheDocument()
  })

  it('switches to quality tab and shows calibration gap', async () => {
    render(<MissedSignalsPanel />)
    await waitFor(() => {
      expect(screen.getAllByText('BTC').length).toBeGreaterThan(0)
    })
    const qTab = screen.getByRole('button', { name: '判断力评分' })
    qTab.click()
    await waitFor(() => {
      expect(screen.getByText('Confidence Calibration Gap')).toBeInTheDocument()
    })
    // overconfidence_gap = 0.075 < 0.1, so should be muted; 0.075 * 100 = 7.5 → "+8%"
    // 跟 chart legend 都会出现 "过度自信" — 用 getAllByText
    expect(screen.getAllByText(/过度自信|谨慎/).length).toBeGreaterThan(0)
  })

  it('renders 12-week trend chart on quality tab', async () => {
    const { container } = render(<MissedSignalsPanel />)
    await waitFor(() => {
      expect(screen.getAllByText('BTC').length).toBeGreaterThan(0)
    })
    const qTab = screen.getByRole('button', { name: '判断力评分' })
    qTab.click()
    await waitFor(() => {
      expect(screen.getByText(/Overconfidence Gap 趋势/)).toBeInTheDocument()
    })
    // SVG path with 3 points (M + 2 L)
    const svg = container.querySelector('svg')!
    const path = svg.querySelector('path')!
    expect(path).not.toBeNull()
    expect(path.getAttribute('d')).toMatch(/^M [\d.]+ [\d.]+ L [\d.]+ [\d.]+ L [\d.]+ [\d.]+$/)
  })

  it('shows empty state when no missed signals', async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({ json: () => Promise.resolve({ ok: true, rows: [], count: 0 }) })
    ) as unknown as typeof fetch
    render(<MissedSignalsPanel />)
    await waitFor(() => {
      expect(screen.getByText(/No missed signals in window/)).toBeInTheDocument()
    })
  })

  it('shows error state when API fails', async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({ json: () => Promise.resolve({ ok: false, error: 'table not migrated' }) })
    ) as unknown as typeof fetch
    render(<MissedSignalsPanel />)
    // click quality tab
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '判断力评分' })).toBeInTheDocument()
    })
    ;(screen.getByRole('button', { name: '判断力评分' }) as HTMLButtonElement).click()
    await waitFor(() => {
      expect(screen.getByText(/Error: table not migrated/)).toBeInTheDocument()
    })
  })
})
