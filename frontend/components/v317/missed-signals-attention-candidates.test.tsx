import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { MissedSignalsAttentionCandidates } from './missed-signals-attention-candidates'

// v317 页面级回归：漏检信号页的「注意力先行候选」区块。
//
// 关键行为：
//   · 四态齐全：loading / data / empty / error
//   · **绝不出现命中率口径** —— 先行候选按定义尚未兑现，
//     给命中率就是拿还没发生的事编预测
//   · 全局判定是 attention_lags_price 时必须给出警示，
//     否则用户会把"媒体追价格"读成"领先信号"
//   · 原始条目链接可点回原页面核验

vi.mock('@/lib/crowd-data', () => ({
  fetchMissedSignalAttentionCandidates: vi.fn(),
}))

import { fetchMissedSignalAttentionCandidates } from '@/lib/crowd-data'

const mock = vi.mocked(fetchMissedSignalAttentionCandidates)

const summary = {
  events_total: 45,
  events_with_price: 36,
  events_with_move: 35,
  hit_rate: 0.9722,
  avg_lead_minutes: 48.3,
  median_lead_minutes: 37,
  lead_leads_count: 35,
  lead_lags_count: 0,
  events_with_prior_move: 31,
  prior_move_ratio: 0.8611,
  reverse_median_lead_minutes: 138,
  clean_events: 14,
  clean_hit_rate: 0.2857,
  clean_avg_lead_minutes: 49.5,
  verdict: 'attention_lags_price',
  symbols: ['XAU'],
  generated_at: '2026-10-30 13:44:14',
}

const candidate = {
  symbol: 'XAU',
  attention_ts: 1793334939000,
  attention_at: '2026-10-30 04:35:39',
  platform_count: 1,
  platform_list: ['gnews_crypto'],
  item_count: 1,
  items: [
    { source: 'gnews_crypto', title: 'Gold price in India: Rates on October 30',
      url: 'https://news.google.com/rss/articles/abc',
      media_channel: 'media', published_at: '2026-10-30 04:35:39',
      heat: 0.42 },
  ],
  max_abs_move_pct: 0.1,
  move_pct_threshold: 0.35,
  horizon_minutes: 180,
  system_signal: { ts: 1793334000, suggested_action: 'HOLD',
                   target_asset: 'XAU', created_at: '2026-10-30 04:20:00' },
  already_acted: false,
  explain: '注意力已起、价格未动，属于先行漏检候选',
}

describe('MissedSignalsAttentionCandidates · v317', () => {
  beforeEach(() => {
    mock.mockResolvedValue({
      ok: true, count: 1, candidates: [candidate], note: '1 个注意力事件尚未兑现', summary,
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows loading state', () => {
    mock.mockReturnValue(new Promise(() => {}))
    render(<MissedSignalsAttentionCandidates />)
    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('renders candidate facts, raw link and the lags-price warning', async () => {
    render(<MissedSignalsAttentionCandidates />)
    await waitFor(() => {
      expect(screen.getByText('XAU')).toBeInTheDocument()
    })
    expect(screen.getByText(/1 平台（gnews_crypto）/)).toBeInTheDocument()
    expect(screen.getByText(candidate.explain)).toBeInTheDocument()
    // 全局判定是 attention_lags_price → 必须有警示
    expect(screen.getByText(/价格先动、注意力后知/)).toBeInTheDocument()
    // 原始条目链接
    const link = screen.getByRole('link', { name: /Gold price in India/ })
    expect(link).toHaveAttribute('href', 'https://news.google.com/rss/articles/abc')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('never presents a hit-rate for candidates that have not materialized', async () => {
    render(<MissedSignalsAttentionCandidates />)
    await waitFor(() => {
      expect(screen.getByText('XAU')).toBeInTheDocument()
    })
    // 候选区块里不能出现任何命中率数值（96.1% 这类数字）
    expect(screen.queryByText(/9[0-9]\.[0-9]%/)).toBeNull()
    expect(screen.queryByText(/2[0-9]\.[0-9]%/)).toBeNull()
    // 且必须显式声明"不给命中率口径"
    expect(screen.getByText(/不给命中率口径/)).toBeInTheDocument()
  })

  it('shows empty state without placeholder rows', async () => {
    mock.mockResolvedValue({
      ok: true, count: 0, candidates: [], note: '0 个注意力事件尚未兑现', summary,
    })
    render(<MissedSignalsAttentionCandidates />)
    await waitFor(() => {
      expect(screen.getByText(/当前没有先行候选/)).toBeInTheDocument()
    })
    expect(screen.queryByText('XAU')).toBeNull()
  })

  it('shows error state when the endpoint fails', async () => {
    mock.mockResolvedValue({
      ok: false, count: 0, candidates: [], note: '',
      summary, error: '后端未实现该接口',
    })
    render(<MissedSignalsAttentionCandidates />)
    await waitFor(() => {
      expect(screen.getByText(/读取失败：/)).toBeInTheDocument()
    })
    expect(screen.queryByText('加载中…')).toBeNull()
  })
})
