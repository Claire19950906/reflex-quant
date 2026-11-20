import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { CrowdPulsePanel } from './crowd-pulse-panel'
import { CrowdRankingTable } from './crowd-ranking-table'

// v317 页面级回归：/crowd 的「注意力领先」视图。
//
// 验证的是**状态机**（loading / data / empty / error）而不是某一次的真实数字：
//   · loading 时不能空着（要有明确的"加载中…"）
//   · 判定必须显式给出，不让用户自己猜
//   · 三个关键口径（表面命中率 / 反向对照 / 干净子集）必须同时在场 ——
//     只显示"命中率 97%"就是那个会骗人的假象
//   · 原始条目链接必须可点回原页面
//   · 后端挂了要显示"读取失败"，不能静默变成空表

vi.mock('@/lib/crowd-data', () => ({
  fetchCrowdRanking: vi.fn(),
  fetchCrowdAudience: vi.fn(),
  fetchCrowdSources: vi.fn(),
  fetchCrowdItems: vi.fn(),
  fetchCrowdAttentionLead: vi.fn(),
  fetchCrowdPendingAttention: vi.fn(),
  fetchCrowdTopics: vi.fn(),
}))

import {
  fetchCrowdRanking,
  fetchCrowdAudience,
  fetchCrowdSources,
  fetchCrowdItems,
  fetchCrowdAttentionLead,
  fetchCrowdPendingAttention,
  fetchCrowdTopics,
} from '@/lib/crowd-data'

// v318 话题 × 观点：一条有方向观点的话题 + 一条无方向的话题
const topicsResp = {
  month: '2026-11',
  window: { start: '2026-10-30', end: '2026-11-03', days: 5, label: '近 5 天（2026-10-30 ~ 2026-11-03）' },
  sort: 'authority',
  kind: 'topic',
  count: 2,
  topics: [
    {
      rank_no: 1,
      topic_key: 'theme:科技/AI',
      display_name: '科技/AI',
      topic_kind: 'theme',
      heat_total: 12.4,
      authority_score: 61.5,
      source_count: 3,
      days_active: 5,
      mentions: 19,
      momentum: 0.13,
      media_weight: 0.5,
      last_seen: '2026-11-03',
      platforms: ['gnews', 'baidu'],
      sources: ['gnews', 'baidu_finance'],
      platform_audiences: ['财经媒体/机构', '大众财经网民'],
      markets: ['美股', 'A股'],
      asset_classes: ['美股'],
      themes: ['科技/AI'],
      explain: '热度总量 12.4 ×8.0 + 跨平台共现 3 个平台 ×10.0',
      sentiment: {
        bull: 8, bear: 2, neutral: 9, total: 19,
        stance: '看多主导', bull_ratio: 0.42, bear_ratio: 0.1, net: 6,
      },
      items: [
        {
          title: '美股三大指数高开 科技股普遍反弹',
          url: 'https://news.test/ai-rally',
          source: 'gnews',
          media_channel: '华尔街见闻',
          heat: 1,
          published_at: '2026-11-03 10:00:00',
          collected_at: '2026-11-03T10:05:00',
        },
      ],
      item_total: 1,
    },
    {
      rank_no: 2,
      topic_key: 'text:加油新规来了',
      display_name: '加油新规来了！11月起全国推行',
      topic_kind: 'news',
      heat_total: 2.0,
      authority_score: 24.8,
      source_count: 1,
      days_active: 2,
      mentions: 4,
      momentum: 0,
      media_weight: 0.5,
      last_seen: '2026-11-02',
      platforms: ['baidu'],
      sources: ['baidu_hot'],
      platform_audiences: ['大众财经网民'],
      markets: ['泛财经/跨市场'],
      asset_classes: ['泛财经/跨市场'],
      themes: [],
      explain: '热度总量 2.0 ×8.0',
      sentiment: {
        bull: 0, bear: 0, neutral: 4, total: 4,
        stance: '无明确方向', bull_ratio: 0, bear_ratio: 0, net: 0,
      },
      items: [],
      item_total: 0,
    },
  ],
  summary: {
    mentions: 23,
    topic_count: 2,
    platform_count: 2,
    platforms: ['baidu', 'gnews'],
    sentiment: {
      bull: 8, bear: 2, neutral: 13, total: 23,
      stance: '看多主导', bull_ratio: 0.35, bear_ratio: 0.09, net: 6,
    },
    top_markets: [{ name: '美股', mentions: 19 }],
    top_themes: [{ name: '科技/AI', mentions: 19 }],
    platform_mentions: [{ platform: 'gnews', audience: '财经媒体/机构', mentions: 19 }],
  },
  note: '观点由标题/正文关键词判定，不是语义模型。',
}

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
  symbols: ['BTC', 'ETH'],
  generated_at: '2026-10-30 13:44:14',
}

const attentionResp = {
  params: {
    lookback_hours: 48, min_platforms: 1, move_pct: 0.35,
    horizon_minutes: 180, timeframe: '1m',
  },
  summary,
  events: [
    {
      symbol: 'BTC',
      attention_ts: 1793350000000,
      attention_at: '2026-10-30 10:00:00',
      platform_count: 2,
      platform_list: ['gnews_crypto', 'gnews'],
      item_count: 2,
      items: [
        { source: 'gnews_crypto', title: 'Bitcoin price surges past $120K',
          url: 'https://news.test/a', media_channel: 'media',
          published_at: '2026-10-30 10:00:00' },
      ],
      price: { has_move: true, lead_minutes: 12, move_pct: 0.8,
               horizon_minutes: 180, move_pct_threshold: 0.35 },
      reverse: { prior_move: true, reverse_lead_minutes: 45 },
      explain: '注意力后价格移动',
    },
  ],
  note: '45 个注意力事件中有行情对照 36 个，其中 31 个（86%）在注意力之前价格就已经动过。',
}

function mockAll(overrides: Record<string, unknown> = {}) {
  const base: Record<string, unknown> = {
    fetchCrowdRanking: { ranking: [], month: '2026-10' },
    fetchCrowdAudience: null,
    fetchCrowdSources: { sources: [] },
    fetchCrowdItems: { items: [] },
    fetchCrowdAttentionLead: attentionResp,
    fetchCrowdPendingAttention: { pending: [], pending_note: '' },
    fetchCrowdTopics: topicsResp,
  }
  const merged = { ...base, ...overrides }
  ;(fetchCrowdRanking as ReturnType<typeof vi.fn>).mockResolvedValue(merged.fetchCrowdRanking)
  ;(fetchCrowdAudience as ReturnType<typeof vi.fn>).mockResolvedValue(merged.fetchCrowdAudience)
  ;(fetchCrowdSources as ReturnType<typeof vi.fn>).mockResolvedValue(merged.fetchCrowdSources)
  ;(fetchCrowdItems as ReturnType<typeof vi.fn>).mockResolvedValue(merged.fetchCrowdItems)
  ;(fetchCrowdAttentionLead as ReturnType<typeof vi.fn>).mockResolvedValue(merged.fetchCrowdAttentionLead)
  ;(fetchCrowdPendingAttention as ReturnType<typeof vi.fn>).mockResolvedValue(merged.fetchCrowdPendingAttention)
  ;(fetchCrowdTopics as ReturnType<typeof vi.fn>).mockResolvedValue(merged.fetchCrowdTopics)
}

describe('CrowdPulsePanel · v317 注意力领先视图', () => {
  beforeEach(() => {
    mockAll()
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows loading state before data arrives', () => {
    ;(fetchCrowdRanking as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    render(<CrowdPulsePanel />)
    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('renders explicit verdict plus surface/reverse/clean numbers on attention tab', async () => {
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect((fetchCrowdAttentionLead as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0)
    })
    fireEvent.click(screen.getByRole('button', { name: '注意力领先' }))

    // 判定必须显式给出（而不是让用户自己看数字猜）
    await waitFor(() => {
      expect(screen.getByText(/价格先动、注意力后知/)).toBeInTheDocument()
    })
    // 三个口径必须同时在场：只看"命中率 97.2%"就是会骗人的那个假象
    expect(screen.getByText('表面命中率')).toBeInTheDocument()
    expect(screen.getByText('97.2%')).toBeInTheDocument()
    expect(screen.getByText('干净子集命中率')).toBeInTheDocument()
    expect(screen.getByText('28.6%')).toBeInTheDocument()
    expect(screen.getByText('注意力前价格已动')).toBeInTheDocument()
    expect(screen.getByText('86.1%')).toBeInTheDocument()
  })

  it('renders raw source links for attention events', async () => {
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect((fetchCrowdAttentionLead as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0)
    })
    fireEvent.click(screen.getByRole('button', { name: '注意力领先' }))
    await waitFor(() => {
      // 条目文本带 "[来源] 标题" 前缀，用正则匹配
      expect(screen.getByText(/Bitcoin price surges past \$120K/)).toBeInTheDocument()
    })
    const link = screen.getByRole('link', { name: /Bitcoin price surges past \$120K/ })
    expect(link).toHaveAttribute('href', 'https://news.test/a')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('shows empty state instead of placeholder rows', async () => {
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect((fetchCrowdRanking as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0)
    })
    fireEvent.click(screen.getByRole('button', { name: '权威度榜单' }))
    await waitFor(() => {
      expect(screen.getByText('暂无榜单数据（采集尚未产出）')).toBeInTheDocument()
    })
  })

  it('shows error state when backend fails', async () => {
    ;(fetchCrowdRanking as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect(screen.getByText(/读取失败：/)).toBeInTheDocument()
    })
    // 出错时不能再显示"加载中…"
    expect(screen.queryByText('加载中…')).toBeNull()
  })
})

// v318 话题 × 观点：默认视图。验证的是"能不能一眼看出关注什么 + 持什么观点"：
//   · 话题名、多空计数、立场结论、谁在说，必须同时在场
//   · "无方向词"必须如实说成"无明确方向"，不能包装成"看平"
//   · 原话链接必须能点回原页面
//   · 接口挂了要显示"话题视图读取失败"，不能静默空白
describe('CrowdPulsePanel · v318 话题 × 观点视图', () => {
  beforeEach(() => {
    mockAll()
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows topic + multi-air sentiment + who is talking by default', async () => {
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect((fetchCrowdTopics as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0)
    })
    await waitFor(() => {
      expect(screen.getAllByText('科技/AI').length).toBeGreaterThan(0)
    })
    // 多空计数 + 立场结论必须显式给出
    expect(screen.getAllByText(/看多 8/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/看空 2/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/看多主导/).length).toBeGreaterThan(0)
    // 谁在说（人群圈层）
    expect(screen.getAllByText(/财经媒体\/机构/).length).toBeGreaterThan(0)
    // 概览：热门题材 / 市场分布
    expect(screen.getAllByText(/热门题材/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/市场分布/).length).toBeGreaterThan(0)
  })

  it('labels directionless topics honestly instead of calling them flat', async () => {
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('加油新规来了！11月起全国推行').length).toBeGreaterThan(0)
    })
    expect(screen.getAllByText(/标题里没有方向词/).length).toBeGreaterThan(0)
  })

  it('expands raw quotes and links back to the original page', async () => {
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('科技/AI').length).toBeGreaterThan(0)
    })
    fireEvent.click(screen.getAllByText(/展开原话/)[0])
    const link = screen.getByRole('link', { name: /美股三大指数高开/ })
    expect(link).toHaveAttribute('href', 'https://news.test/ai-rally')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('shows an explicit error when the topics endpoint fails', async () => {
    ;(fetchCrowdTopics as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    render(<CrowdPulsePanel />)
    await waitFor(() => {
      expect(screen.getByText(/话题视图读取失败：/)).toBeInTheDocument()
    })
  })
})

// v318 附带修复：月榜的「话题/标的」列之前直接打印 GROUP_CONCAT 逗号串
// （"题材:科技/AI,美股,加密资产,A股" 重复两遍），现在拆成可读标签 chip，
// 并且 themes 真实填充（后端 themes_json 之前写死 "[]"）。
describe('CrowdRankingTable · v318 标签可读性', () => {
  afterEach(() => {
    cleanup()
  })

  it('renders themes / asset_class / market as separate chips without duplicates', () => {
    const { container } = render(
      <CrowdRankingTable
        ranking={[
          {
            month: '2026-11',
            topic_key: 'theme:科技/AI',
            display_name: '科技/AI',
            topic_kind: 'theme',
            rank_no: 1,
            authority_score: 61.5,
            heat_total: 0.4,
            source_count: 5,
            days_active: 3,
            momentum: 0.13,
            media_weight: 0.5,
            sources: ['baidu', 'gnews'],
            explain: 'x',
            asset_class: '题材:科技/AI,美股,加密资产,A股',
            themes: ['科技/AI', '宏观政策'],
            market: '题材:科技/AI,美股,加密资产,A股',
          },
        ]}
        sort="authority"
        onSortChange={() => {}}
      />,
    )
    // 题材 chip
    expect(screen.getByText('宏观政策')).toBeInTheDocument()
    // 拼接串被拆开，不再整串打印
    expect(screen.queryByText(/题材:科技\/AI,美股,加密资产,A股/)).toBeNull()
    // asset_class 与 market 里重复出现的标签只渲染一个 chip
    const chips = Array.from(container.querySelectorAll('span')).filter(
      (s) => s.textContent === '科技/AI',
    )
    expect(chips).toHaveLength(1)
  })
})
