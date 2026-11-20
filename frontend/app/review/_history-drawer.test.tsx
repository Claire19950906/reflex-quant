import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { HistoryDrawer } from './_history-drawer'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const mockFetch = (status: number, body: unknown) => {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as never) as never
}

describe('HistoryDrawer — 渲染', () => {
  it('open=false → 不渲染', () => {
    render(<HistoryDrawer open={false} onClose={() => {}} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('open=true → 渲染 dialog(aria-modal="true")', () => {
    mockFetch(200, { positions: [] })
    render(<HistoryDrawer open={true} onClose={() => {}} />)
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'true')
  })

  it('标题"复盘历史"', () => {
    mockFetch(200, { positions: [] })
    render(<HistoryDrawer open={true} onClose={() => {}} />)
    expect(screen.getByRole('heading', { name: '复盘历史' })).toBeInTheDocument()
  })
})

describe('HistoryDrawer — 数据加载', () => {
  it('请求 /replay/positions?limit=50', async () => {
    mockFetch(200, { positions: [] })
    render(<HistoryDrawer open={true} onClose={() => {}} />)
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled()
    })
    const url = vi.mocked(globalThis.fetch).mock.calls[0]?.[0] as string
    expect(url).toContain('/replay/positions')
    expect(url).toContain('limit=50')
  })

  it('有数据 → 渲染列表', async () => {
    mockFetch(200, {
      positions: [
        {
          id: 1,
          news_time: '2024-03-15T10:30:00',
          source: 'reuters',
          asset: 'BTC',
          action: 'BUY',
          score: 0.85,
          settled: true,
          forward_pnl_usdt: 12.5,
          current_pnl_usdt: null,
        },
        {
          id: 2,
          news_time: '2024-03-15T11:00:00',
          source: 'bloomberg',
          asset: 'ETH',
          action: 'SELL',
          score: 0.62,
          settled: false,
          forward_pnl_usdt: null,
          current_pnl_usdt: -3.2,
        },
      ],
    })
    render(<HistoryDrawer open={true} onClose={() => {}} />)
    await waitFor(() => {
      expect(screen.getByTestId('history-list')).toBeInTheDocument()
    })
    expect(screen.getByText('BTC')).toBeInTheDocument()
    expect(screen.getByText('ETH')).toBeInTheDocument()
    expect(screen.getByText('BUY')).toBeInTheDocument()
    expect(screen.getByText('SELL')).toBeInTheDocument()
  })

  it('无数据 → 显示"暂无数据"', async () => {
    mockFetch(200, { positions: [] })
    render(<HistoryDrawer open={true} onClose={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText('暂无数据')).toBeInTheDocument()
    })
  })

  it('请求失败 → 显示错误', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({}),
    } as never) as never
    render(<HistoryDrawer open={true} onClose={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument()
    })
  })

  it('fetch 抛异常 → 显示错误信息', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('network down')) as never
    render(<HistoryDrawer open={true} onClose={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument()
    })
  })
})

describe('HistoryDrawer — 关闭', () => {
  it('点关闭按钮 → onClose', () => {
    mockFetch(200, { positions: [] })
    const onClose = vi.fn()
    render(<HistoryDrawer open={true} onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: '关闭抽屉' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('按 Esc → onClose', () => {
    mockFetch(200, { positions: [] })
    const onClose = vi.fn()
    render(<HistoryDrawer open={true} onClose={onClose} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
