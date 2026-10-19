/**
 * FailureStatsPanel — 段位漂移接入 (v302+ P1 PR-14)。
 *
 * 测试目标:
 *  - 头部 FAILURE STATS badge 与"失败原因统一汇总"标题始终渲染
 *  - 有 drift alerts → header 出现 compact badge + "📊 段位漂移" 小节
 *  - 无 drift alerts → header 不出现 badge + 不渲染小节
 *  - 空 reviews → 无 drift
 *  - "出现次数最多" 框始终渲染
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { ToastProvider } from '@/components/ui/toast'
import { FailureStatsPanel } from './failure-stats-panel'
import { FIXED_STAGE_KEYS } from '@/lib/segment-drift'
import type { Review, ReviewListResp } from '@/lib/types-reasoning-review'

const apiGetMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiGet: (...args: unknown[]) => (apiGetMock as any)(...args),
  apiSend: (...args: unknown[]) => (apiGetMock as any)(...args),
}))

const STATS_FIXTURE = {
  total: 100,
  sources: { news: 40, review: 60 },
  statuses: { pending: 5, agree: 80, corrected: 15 },
  reasons: [
    { code: 101, name: '逻辑自检失败', count: 35, pct: 35 },
    { code: 202, name: '反共识缺失', count: 20, pct: 20 },
  ],
  news_count: 40,
  review_count: 60,
  reset_count: 0,
}

function mkReview(over: Partial<Review> = {}): Review {
  return {
    decision_id: 1000,
    asset: 'BTC',
    action: 'long',
    reasoning_length: 200,
    is_correct: 0,
    forward_pnl: 0,
    steps: { present: [...FIXED_STAGE_KEYS], missing: [], pct: 1 },
    counter_consensus: { present: true, keywords_hit: [] },
    causal_mechanism: { present: true, keywords_hit: [] },
    factor_concepts: { count: 0, concepts: [] },
    cross_asset_direction: {
      present: true, target_asset: 'ETH', items: [],
      aligned_count: 0, conflict_count: 0, detail: '',
    },
    assumption: { present: true, keywords_hit: [] },
    historical_pattern: { present: true, keywords_hit: [] },
    calibration: { confidence: 0.5, pnl_realized: 0, is_correct: 0 },
    notes: [],
    completion_score: 8,
    ...over,
  }
}

// 20 条 reviews,全部缺测 总框架 → current 0% vs baseline 95% → -95pp → alert
const DRIFT_REVIEWS: Review[] = Array.from({ length: 20 }, (_, i) =>
  mkReview({
    decision_id: 1000 + i,
    steps: {
      present: FIXED_STAGE_KEYS.filter((k) => k !== '总框架'),
      missing: ['总框架'],
      pct: 7 / 8,
    },
    completion_score: 7,
  }),
)

const REVIEW_LIST_FIXTURE: ReviewListResp = {
  limit: 20, days: 7, only_failed: false,
  count: DRIFT_REVIEWS.length, items: DRIFT_REVIEWS,
}

const NO_DRIFT_REVIEWS: Review[] = (() => {
  // 100 条 reviews,每段命中率贴近基线(DEFAULT_BASELINE):
  //   总框架 95% / 因素定位 90% / 驱动力 78% / 水位博弈 84% /
  //   跨资产资金 10% / 反共识结论 84% / 逻辑自检 16% / 总结 98%
  // 用确定性分布 + 末尾补到 100(避免 ±2pp 抖动触发 warn)
  const N = 100
  const presence: Record<string, number> = {
    '总框架': 95, '因素定位': 90, '驱动力': 78, '水位博弈': 84,
    '跨资产资金': 10, '反共识结论': 84, '逻辑自检': 16, '总结': 98,
  }
  return Array.from({ length: N }, (_, i) => {
    const missing: string[] = []
    for (const k of FIXED_STAGE_KEYS) {
      if (i >= presence[k]) missing.push(k)
    }
    return mkReview({
      decision_id: 2000 + i,
      steps: {
        present: FIXED_STAGE_KEYS.filter((k) => !missing.includes(k)),
        missing,
        pct: (FIXED_STAGE_KEYS.length - missing.length) / FIXED_STAGE_KEYS.length,
      },
      completion_score: FIXED_STAGE_KEYS.length - missing.length,
    })
  })
})()

const NO_DRIFT_LIST_FIXTURE: ReviewListResp = {
  limit: 100, days: 7, only_failed: false,
  count: NO_DRIFT_REVIEWS.length, items: NO_DRIFT_REVIEWS,
}

const EMPTY_LIST_FIXTURE: ReviewListResp = {
  limit: 20, days: 7, only_failed: false, count: 0, items: [],
}

function renderWithToast(ui: React.ReactNode) {
  return render(<ToastProvider>{ui}</ToastProvider>)
}

function stubApi(opts: { reviews?: ReviewListResp; reviewsFail?: boolean }) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.includes('/review/attributions/stats')) return Promise.resolve(STATS_FIXTURE)
    if (url.includes('/reasoning/review/list')) {
      if (opts.reviewsFail) return Promise.reject(new Error('reviews API down'))
      return Promise.resolve(opts.reviews ?? NO_DRIFT_LIST_FIXTURE)
    }
    return Promise.reject(new Error(`unexpected url: ${url}`))
  })
}

beforeEach(() => {
  apiGetMock.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('FailureStatsPanel — 段位漂移接入', () => {
  it('always 渲染标题 + FAILURE STATS badge', async () => {
    stubApi({ reviews: NO_DRIFT_LIST_FIXTURE })
    renderWithToast(<FailureStatsPanel />)
    expect(screen.getByText('失败原因统一汇总')).toBeInTheDocument()
    expect(screen.getByText('FAILURE STATS')).toBeInTheDocument()
  })

  it('reviews 贴近基线(无漂移)→ 不渲染 drift 小节', async () => {
    stubApi({ reviews: NO_DRIFT_LIST_FIXTURE })
    renderWithToast(<FailureStatsPanel />)
    await waitFor(() => {
      expect(apiGetMock.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
    // "📊 段位漂移" 是子段唯一标识(头部 badge 不带 emoji)
    expect(screen.queryByText(/📊 段位漂移/)).not.toBeInTheDocument()
  })

  it('reviews 缺测 总框架(95pp 偏差)→ header 出 drift badge + 出"📊 段位漂移"小节', async () => {
    stubApi({ reviews: REVIEW_LIST_FIXTURE })
    renderWithToast(<FailureStatsPanel />)
    // 头部 badge + 子段 + 脚注都有"段位漂移" → 用 getAllByText 至少 2 个
    const all = await waitFor(() => screen.getAllByText(/段位漂移/))
    expect(all.length).toBeGreaterThanOrEqual(2)
    // 小节列出 总框架 段名(总框架 -95pp 是 |delta| 最大的)
    expect(screen.getByText('总框架')).toBeInTheDocument()
    // 段位偏差子段: -95pp 偏差值显示
    expect(screen.getByText(/-95pp/)).toBeInTheDocument()
    // 等 stats fetch 完成,失败原因(35 笔 · 35%)出现在 top reason 框
    expect(await screen.findByText('35 笔 · 35%')).toBeInTheDocument()
  })

  it('reviews 拉取失败 → 不渲染 drift,主面板仍可用', async () => {
    stubApi({ reviewsFail: true })
    renderWithToast(<FailureStatsPanel />)
    // 头部 + 标题始终存在
    expect(screen.getByText('失败原因统一汇总')).toBeInTheDocument()
    expect(screen.getByText('FAILURE STATS')).toBeInTheDocument()
    // 等 stats fetch 完成,失败原因渲染
    expect(await screen.findByText('35 笔 · 35%')).toBeInTheDocument()
    // 等一段时间确保 reviews fetch 也跑完(已 reject)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText(/📊 段位漂移/)).not.toBeInTheDocument()
  })

  it('空 reviews 数组 → 无 drift', async () => {
    stubApi({ reviews: EMPTY_LIST_FIXTURE })
    renderWithToast(<FailureStatsPanel />)
    await waitFor(() => {
      expect(apiGetMock.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
    expect(screen.queryByText(/📊 段位漂移/)).not.toBeInTheDocument()
  })
})
