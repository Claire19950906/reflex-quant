import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { AttributionCard } from './abnormal-detail-panel'

// v317: 跨平台注意力证据（crowd_attention）的前端渲染回归。
//
// 这条证据是**独立于 AI 归因**的客观事实来源（真实抓取、带发布时间戳）。
// 三条必须成立的行为：
//   1. 有 crowd_attention → 渲染出来（领先分钟 / 平台数 / 真实条目 / 原文链接）
//   2. AI 归因还在 pending → 注意力证据**照样**渲染（不能被 pending 分支吞掉）
//   3. 没有 crowd_attention → 不渲染任何注意力卡片（不造证据）
// 另外必须带上「单条证据 ≠ 因果」的诚实性提示。

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

const crowdEvidence = {
  symbol: 'BTC',
  attention_ts: 1793350000000,
  attention_at: '2026-10-30 10:00:00',
  lead_minutes: 20,
  platform_count: 2,
  platform_list: ['gnews_crypto', 'gnews'],
  item_count: 2,
  kind: 'crowd_attention',
  explain: '行情异动前 20 分钟，BTC 已被 2 个独立平台（gnews_crypto/gnews）提及 2 条',
  items: [
    { title: 'Bitcoin price surges past $120K', source: 'gnews_crypto',
      url: 'https://news.test/a', published_at: '2026-10-30 10:00:00' },
    { title: 'Bitcoin rally draws retail buyers', source: 'gnews',
      url: 'https://news.test/b', published_at: '2026-10-30 10:02:00' },
  ],
}

const aiAttribution = {
  summary: '测试归因摘要',
  drivers: [{ cause: '驱动因子', confidence: 'high' }],
  cross_asset_likely: [],
  actionable: 'yes',
  severity_hint: 2,
  source_model: 'test-model',
  used_news_n: 3,
}

function mockAttribution(body: unknown) {
  global.fetch = vi.fn(() => Promise.resolve(jsonResponse(body))) as unknown as typeof fetch
}

describe('AbnormalDetailPanel · v317 跨平台注意力证据', () => {
  beforeEach(() => {
    mockAttribution({
      signal_id: 7, asset: 'BTC', signal_kind: 'multi_factor', status: 'active',
      attribution: aiAttribution, crowd_attention: crowdEvidence,
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders crowd attention evidence with lead minutes, platforms and raw links', async () => {
    render(<AttributionCard signalId={7} />)
    await waitFor(() => {
      expect(screen.getByText('跨平台注意力（v317）')).toBeInTheDocument()
    })
    expect(screen.getByText(/领先 20 分钟/)).toBeInTheDocument()
    expect(screen.getByText(/2 个平台 · 2 条/)).toBeInTheDocument()
    // 平台名逐个列出（平台 chip 与条目来源标签都会出现同名，用 getAllByText）
    expect(screen.getAllByText('gnews_crypto').length).toBeGreaterThan(0)
    expect(screen.getAllByText('gnews').length).toBeGreaterThan(0)
    // 真实条目 + 可点的原文链接
    expect(screen.getByText('Bitcoin price surges past $120K')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Bitcoin price surges past $120K' })
    expect(link).toHaveAttribute('href', 'https://news.test/a')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('keeps honesty caveat: one event with attention is not proof of causation', async () => {
    render(<AttributionCard signalId={7} />)
    await waitFor(() => {
      expect(screen.getByText('跨平台注意力（v317）')).toBeInTheDocument()
    })
    // 必须明确说明它不参与/不改写 AI 归因，且全局结论是 attention_lags_price
    expect(screen.getByText(/不参与也不改写/)).toBeInTheDocument()
    expect(screen.getByText(/attention_lags_price/)).toBeInTheDocument()
  })

  it('still renders crowd attention while AI attribution is pending', async () => {
    mockAttribution({
      signal_id: 7, asset: 'BTC', signal_kind: 'multi_factor', status: 'open',
      attribution: null, crowd_attention: crowdEvidence,
    })
    render(<AttributionCard signalId={7} />)
    await waitFor(() => {
      expect(screen.getByText('跨平台注意力（v317）')).toBeInTheDocument()
    })
    // pending 提示与注意力证据同时在场
    expect(screen.getByText('pending')).toBeInTheDocument()
    expect(screen.getByText(/领先 20 分钟/)).toBeInTheDocument()
  })

  it('renders no attention card when backend returns null evidence', async () => {
    mockAttribution({
      signal_id: 7, asset: 'BTC', signal_kind: 'multi_factor', status: 'active',
      attribution: aiAttribution, crowd_attention: null,
    })
    render(<AttributionCard signalId={7} />)
    await waitFor(() => {
      expect(screen.getByText('测试归因摘要')).toBeInTheDocument()
    })
    expect(screen.queryByText('跨平台注意力（v317）')).toBeNull()
  })

  it('shows signal-missing state on bridge fallback instead of faking evidence', async () => {
    mockAttribution({
      ok: true, _bridge_fallback: true, signal_id: 7,
    })
    render(<AttributionCard signalId={7} />)
    await waitFor(() => {
      expect(screen.getByText('信号不存在')).toBeInTheDocument()
    })
    // 兜底壳下绝不能渲染注意力卡片
    expect(screen.queryByText('跨平台注意力（v317）')).toBeNull()
  })
})
