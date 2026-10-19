import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { CopyMarkdownButton } from './copy-markdown-button'
import type { Review, DecisionTextResp } from '@/lib/types-reasoning-review'

function mkReview(): Review {
  return {
    decision_id: 1, asset: 'BTC', action: 'BUY', completion_score: 80,
    forward_pnl: null, is_correct: null, reasoning_length: 100,
    steps: { present: [], missing: [], pct: 0 },
    counter_consensus: { present: true, keywords_hit: [] },
    causal_mechanism: { present: true, keywords_hit: [] },
    cross_asset_direction: { present: false, target_asset: '', items: [], aligned_count: 0, conflict_count: 0, detail: '' },
    assumption: { present: false, keywords_hit: [] },
    historical_pattern: { present: false, keywords_hit: [] },
    factor_concepts: { count: 0, concepts: [] },
    calibration: { confidence: null, pnl_realized: null, is_correct: null },
    notes: [],
  }
}

function mkText(): DecisionTextResp {
  return {
    decision_id: 1, asset: 'BTC', action: 'BUY',
    reasoning_path: '/x', reasoning: 'BTC 反弹。', asset_reasoning: '',
    full_text: 'BTC 反弹。', full_text_length: 6,
    segments: { all: [], present: [], missing: [], positions: [] },
  }
}

describe('CopyMarkdownButton — 行为', () => {
  beforeEach(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    })
  })
  afterEach(() => {
    cleanup()
  })

  it('默认渲染 "复制 Markdown" 文案', () => {
    render(<CopyMarkdownButton review={mkReview()} />)
    const btn = screen.getByTestId('copy-md-btn')
    expect(btn).toHaveAttribute('data-status', 'idle')
    expect(btn).toHaveTextContent('复制 Markdown')
  })

  it('点击后调用 clipboard.writeText 且显示"已复制"', async () => {
    const wtn = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: wtn },
      writable: true, configurable: true,
    })
    render(<CopyMarkdownButton review={mkReview()} text={mkText()} />)
    const btn = screen.getByTestId('copy-md-btn')
    fireEvent.click(btn)
    await waitFor(() => expect(btn).toHaveAttribute('data-status', 'ok'))
    expect(btn).toHaveTextContent('已复制')
    expect(wtn).toHaveBeenCalledTimes(1)
    const written = wtn.mock.calls[0][0]
    expect(written).toMatch(/决策 #1/)
    expect(written).toContain('BTC')
  })

  it('2 秒后回到 idle', async () => {
    vi.useFakeTimers()
    const wtn = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: wtn },
      writable: true, configurable: true,
    })
    render(<CopyMarkdownButton review={mkReview()} />)
    const btn = screen.getByTestId('copy-md-btn')
    fireEvent.click(btn)
    await vi.waitFor(() => expect(btn).toHaveAttribute('data-status', 'ok'))
    // 推进 2100ms 触发 setTimeout(2000) 重置
    await vi.advanceTimersByTimeAsync(2100)
    expect(btn).toHaveAttribute('data-status', 'idle')
    expect(btn).toHaveTextContent('复制 Markdown')
    vi.useRealTimers()
  })

  it('clipboard 不存在 → "复制失败"', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      writable: true, configurable: true,
    })
    render(<CopyMarkdownButton review={mkReview()} />)
    const btn = screen.getByTestId('copy-md-btn')
    fireEvent.click(btn)
    await waitFor(() => expect(btn).toHaveAttribute('data-status', 'err'))
    expect(btn).toHaveTextContent('复制失败')
  })

  it('clipboard.writeText 抛错 → "复制失败"', async () => {
    const wtn = vi.fn().mockRejectedValue(new Error('权限不足'))
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: wtn },
      writable: true, configurable: true,
    })
    render(<CopyMarkdownButton review={mkReview()} />)
    const btn = screen.getByTestId('copy-md-btn')
    fireEvent.click(btn)
    await waitFor(() => expect(btn).toHaveAttribute('data-status', 'err'))
    expect(btn).toHaveTextContent('复制失败')
  })

  it('compact 模式不显示文案', () => {
    const { container } = render(<CopyMarkdownButton review={mkReview()} compact />)
    expect(container.querySelector('button')?.textContent).toBe('')
  })

  it('title 始终是当前状态文案', () => {
    render(<CopyMarkdownButton review={mkReview()} />)
    expect(screen.getByTestId('copy-md-btn')).toHaveAttribute('title', '复制 Markdown')
  })

  it('stopPropagation=true → 点击不冒泡', () => {
    const parentClick = vi.fn()
    render(
      <div onClick={parentClick}>
        <CopyMarkdownButton review={mkReview()} stopPropagation />
      </div>,
    )
    fireEvent.click(screen.getByTestId('copy-md-btn'))
    expect(parentClick).not.toHaveBeenCalled()
  })

  it('stopPropagation 默认 (false) → 点击冒泡', () => {
    const parentClick = vi.fn()
    render(
      <div onClick={parentClick}>
        <CopyMarkdownButton review={mkReview()} />
      </div>,
    )
    fireEvent.click(screen.getByTestId('copy-md-btn'))
    expect(parentClick).toHaveBeenCalled()
  })
})
