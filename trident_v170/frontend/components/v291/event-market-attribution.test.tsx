import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { EventMarketAttribution } from './event-market-attribution'

// v302+ P0 测试: 验证 a11y 升级后键盘可访问
//   - moves 列表是 role=listbox / role=option
//   - Enter / Space 选择
//   - 选中状态用 aria-selected 暴露
//   - 取消按钮有 aria-label + type=button

const sampleMoves = [
  {
    id: 1,
    asset: 'BTC',
    ts: 1700000000,
    start_price: 100, peak_price: 110, end_price: 105,
    move_pct: 5.0, move_kind: 'fast_rise', duration_min: 12,
    algo_score: 0.82, context: {}, created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 2,
    asset: 'ETH',
    ts: 1700001000,
    start_price: 200, peak_price: 195, end_price: 198,
    move_pct: -1.0, move_kind: 'slow_fall', duration_min: 30,
    algo_score: 0.55, context: {}, created_at: '2024-01-01T00:30:00Z',
  },
]

const sampleStats = {
  ok: true,
  by_asset: {
    BTC: { moves: 1, attributions: 3, max_score: 0.82, ai_explained: 1 },
    ETH: { moves: 1, attributions: 0, max_score: 0.55, ai_explained: 0 },
  },
}

// v303: 改用真实 Response（带 .ok=true + .json()），让 lib/api.fetchWithFallback 的
// `if (!r.ok)` 分支通过 — 见 MissedSignalsPanel.test.tsx 注释。
function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

describe('EventMarketAttribution v291 a11y', () => {
  beforeEach(() => {
    global.fetch = vi.fn((url: string) => {
      if (url.includes('/api/v291/critical-moves')) {
        return Promise.resolve(jsonResponse({ ok: true, rows: sampleMoves, items: sampleMoves })) as never
      }
      if (url.includes('/api/v291/stats')) {
        return Promise.resolve(jsonResponse(sampleStats)) as never
      }
      if (url.includes('/api/v291/move-attributions')) {
        return Promise.resolve(jsonResponse({ ok: true, rows: [], items: [] })) as never
      }
      return Promise.reject(new Error(`unexpected url: ${url}`))
    }) as unknown as typeof fetch
  })
  afterEach(() => cleanup())

  it('moves list exposes listbox + option roles', async () => {
    render(<EventMarketAttribution />)
    const listbox = await screen.findByRole('listbox', { name: /临界点列表/ })
    expect(listbox).toBeInTheDocument()
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(2)
  })

  it('each option has aria-selected reflecting current selection', async () => {
    render(<EventMarketAttribution />)
    const options = await screen.findAllByRole('option')
    expect(options[0]).toHaveAttribute('aria-selected', 'false')
    fireEvent.click(options[0])
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('Enter key on an option selects that move', async () => {
    render(<EventMarketAttribution />)
    const options = await screen.findAllByRole('option')
    options[1].focus()
    fireEvent.keyDown(options[1], { key: 'Enter' })
    expect(options[1]).toHaveAttribute('aria-selected', 'true')
  })

  it('Space key on an option also selects', async () => {
    render(<EventMarketAttribution />)
    const options = await screen.findAllByRole('option')
    options[0].focus()
    fireEvent.keyDown(options[0], { key: ' ' })
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('cancel button has type=button and aria-label mentioning move id', async () => {
    render(<EventMarketAttribution />)
    const options = await screen.findAllByRole('option')
    fireEvent.click(options[0])
    const cancel = await screen.findByRole('button', { name: /取消选中临界点 #1/ })
    expect(cancel).toBeInTheDocument()
    expect(cancel.getAttribute('type')).toBe('button')
  })
})
