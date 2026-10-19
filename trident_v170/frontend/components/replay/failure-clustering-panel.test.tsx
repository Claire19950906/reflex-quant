/**
 * FailureClusteringPanel — 失败模式聚类组件 (v302+ P2 PR-17)。
 * 覆盖: 渲染/排序/3 趋势色/code/severity/top_assets/example按钮/空态/错误态/refresh。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { FailureClusteringPanel } from './failure-clustering-panel'
import type { ReflectionClustering } from '@/lib/hooks/use-reflection-clustering'

const useClusteringMock = vi.fn()
vi.mock('@/lib/hooks/use-reflection-clustering', () => ({
  useReflectionClustering: (opts: unknown) => useClusteringMock(opts),
}))

const sample: ReflectionClustering = {
  days: 30, min_count: 2, total_errors: 100, total_decisions: 45,
  patterns: [
    {
      code: 'E014', count: 25, pct_of_total: 0.25, severity_avg: 2.8,
      trend: 'up', trend_delta: 0.5, last_7d: 15, prior_7d: 10,
      top_assets: [{ asset: 'BTC', count: 8 }, { asset: 'ETH', count: 5 }],
      examples: [
        { decision_id: 100, detail: '推理链断裂示例文本', created_at: '2026-01-09 10:00:00' },
        { decision_id: 200, detail: '另一例', created_at: '2026-01-08 10:00:00' },
      ],
    },
    {
      code: 'E015', count: 12, pct_of_total: 0.12, severity_avg: 1.8,
      trend: 'down', trend_delta: -0.3, last_7d: 3, prior_7d: 5,
      top_assets: [{ asset: 'SOL', count: 4 }],
      examples: [{ decision_id: 300, detail: '概念错位', created_at: '2026-01-07 10:00:00' }],
    },
    {
      code: 'E001', count: 8, pct_of_total: 0.08, severity_avg: 1.2,
      trend: 'flat', trend_delta: 0.0, last_7d: 4, prior_7d: 4,
      top_assets: [],
      examples: [],
    },
  ],
}

describe('FailureClusteringPanel', () => {
  beforeEach(() => {
    useClusteringMock.mockReset()
  })

  // 1. 基础渲染
  it('renders title + summary', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    render(<FailureClusteringPanel />)
    expect(screen.getByText('失败模式聚类')).toBeTruthy()
    expect(screen.getByText(/30 天/)).toBeTruthy()
    expect(screen.getByText(/100 条错误/)).toBeTruthy()
    expect(screen.getByText(/45 笔决策/)).toBeTruthy()
    expect(screen.getByText(/3 个聚类/)).toBeTruthy()
  })

  // 2. 渲染 N 行 pattern
  it('renders pattern rows', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    const rows = container.querySelectorAll('[data-testid="clustering-row"]')
    expect(rows).toHaveLength(3)
    expect(rows[0].getAttribute('data-code')).toBe('E014')
    expect(rows[1].getAttribute('data-code')).toBe('E015')
    expect(rows[2].getAttribute('data-code')).toBe('E001')
  })

  // 3. 趋势 3 档
  it('shows 3 trend variants', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    const trends = container.querySelectorAll('[data-testid="trend-badge"]')
    expect(trends).toHaveLength(3)
    expect(trends[0].textContent).toContain('+50%')
    expect(trends[1].textContent).toContain('-30%')
    expect(trends[2].textContent).toContain('平稳')
  })

  // 4. severity 3 档
  it('shows severity label per pattern', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    expect(container.textContent).toContain('CRIT')
    expect(container.textContent).toContain('WARN')
    expect(container.textContent).toContain('INFO')
  })

  // 5. top_assets chip
  it('renders top asset chips', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    expect(container.textContent).toContain('BTC×8')
    expect(container.textContent).toContain('ETH×5')
    expect(container.textContent).toContain('SOL×4')
  })

  // 6. example 按钮 + decision_id
  it('renders example buttons with decision_id', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    const buttons = container.querySelectorAll('[data-testid="examples-list"] button')
    expect(buttons.length).toBeGreaterThanOrEqual(3)
    expect(container.textContent).toContain('#100')
    expect(container.textContent).toContain('#300')
  })

  // 7. 点击 example 触发 onSelectDecision
  it('clicking example calls onSelectDecision', () => {
    const onSelect = vi.fn()
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel onSelectDecision={onSelect} />)
    const btn = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.includes('#100'))
    expect(btn).toBeTruthy()
    fireEvent.click(btn!)
    expect(onSelect).toHaveBeenCalledWith(100)
  })

  // 8. 空 patterns
  it('shows empty state when no patterns', () => {
    useClusteringMock.mockReturnValue({
      data: { days: 30, min_count: 2, total_errors: 0, total_decisions: 0, patterns: [] },
      loading: false, error: null, refresh: vi.fn(),
    })
    const { container } = render(<FailureClusteringPanel />)
    expect(container.textContent).toContain('窗口内无')
    expect(container.querySelectorAll('[data-testid="clustering-row"]')).toHaveLength(0)
  })

  // 9. 错误态
  it('shows error message', () => {
    useClusteringMock.mockReturnValue({
      data: { days: 30, min_count: 2, total_errors: 0, total_decisions: 0, patterns: [] },
      loading: false, error: 'boom', refresh: vi.fn(),
    })
    const { container } = render(<FailureClusteringPanel />)
    expect(container.textContent).toContain('加载失败')
    expect(container.textContent).toContain('boom')
  })

  // 10. topN 截断
  it('topN truncates display', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel topN={2} />)
    const rows = container.querySelectorAll('[data-testid="clustering-row"]')
    expect(rows).toHaveLength(2)
    expect(container.textContent).toContain('展示前 2 / 共 3')
  })

  // 11. refresh 按钮
  it('refresh button triggers refresh', async () => {
    const refresh = vi.fn()
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh })
    const { container } = render(<FailureClusteringPanel />)
    const btn = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.trim() === '刷新')
    expect(btn).toBeTruthy()
    fireEvent.click(btn!)
    await waitFor(() => expect(refresh).toHaveBeenCalled())
  })

  // 12. onSelect 未传 → example 按钮 disabled
  it('example buttons disabled when onSelectDecision not provided', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    const buttons = container.querySelectorAll('[data-testid="examples-list"] button')
    buttons.forEach(b => expect((b as HTMLButtonElement).disabled).toBe(true))
  })

  // 13. count + pct
  it('renders count and pct', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    expect(container.textContent).toContain('25')
    expect(container.textContent).toContain('(25.0%)')
  })

  // 14. last/prior 7d 数字
  it('renders last_7d / prior_7d numbers', () => {
    useClusteringMock.mockReturnValue({ data: sample, loading: false, error: null, refresh: vi.fn() })
    const { container } = render(<FailureClusteringPanel />)
    expect(container.textContent).toContain('7d=15')
    expect(container.textContent).toContain('prior=10')
  })
})
