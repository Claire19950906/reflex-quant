import { describe, it, expect } from 'vitest'
import { formatViewSnapshotAsMarkdown } from './review-view-snapshot'
import type { ReviewUrlState } from './use-review-url-state'

function makeState(over: Partial<ReviewUrlState> = {}): ReviewUrlState {
  return {
    tab: 'all',
    ab: 'a',
    err: null,
    period: 'today',
    customRange: { fromDate: '2024-01-01', toDate: '2024-01-07' },
    setTab: () => {},
    setAbTab: () => {},
    setErrorCode: () => {},
    setPeriod: () => {},
    setCustomDateRange: () => {},
    ...over,
  } as unknown as ReviewUrlState
}

describe('formatViewSnapshotAsMarkdown — 状态块', () => {
  it('tab=all 渲染"全部"', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState({ tab: 'all' }),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review?tab=all',
      sections: [],
    })
    expect(out).toContain('- **模式**: 全部')
  })

  it('tab=journal 渲染"交易日记"', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState({ tab: 'journal' }),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review?tab=journal',
      sections: [],
    })
    expect(out).toContain('- **模式**: 交易日记')
  })

  it('ab=a 渲染"历史真实交易复盘"', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState({ ab: 'a' }),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toContain('- **复盘面板**: A · 历史真实交易复盘')
  })

  it('ab=b 渲染"快速评测复盘"', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState({ ab: 'b' }),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toContain('- **复盘面板**: B · 快速评测复盘')
  })

  it('period=custom 展开自定义范围', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState({
        period: 'custom',
        customRange: { fromDate: '2024-03-01', toDate: '2024-03-15' },
      }),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toContain('- **时段**: custom')
    expect(out).toContain('- 自定义范围: 2024-03-01 → 2024-03-15')
  })

  it('err 渲染当前错误码', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState({ err: 'E_RATE_LIMIT' }),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toContain('- **当前错误码**: E_RATE_LIMIT')
  })

  it('err=null 渲染"(无)"', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState({ err: null }),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toContain('- **当前错误码**: (无)')
  })

  it('展开 N/总 M 段位', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState(),
      totalSectionCount: 7,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toContain('- **段位展开**: 5 / 7')
  })
})

describe('formatViewSnapshotAsMarkdown — section 清单', () => {
  it('只列 visible=true 的段位', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState(),
      totalSectionCount: 3,
      expandedSectionCount: 3,
      fullUrl: 'http://localhost/review',
      sections: [
        { id: 'diagnose', label: '诊断', visible: true },
        { id: 'hidden', label: '隐藏段', visible: false },
        { id: 'review', label: '复盘', visible: true },
      ],
    })
    expect(out).toContain('- ✅ 诊断')
    expect(out).toContain('- ✅ 复盘')
    expect(out).not.toContain('隐藏段')
  })
})

describe('formatViewSnapshotAsMarkdown — 链接 / footer', () => {
  it('包含完整 URL', () => {
    const url = 'http://localhost/review?tab=journal&ab=b'
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState(),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: url,
      sections: [],
    })
    expect(out).toContain(`<${url}>`)
  })

  it('footer 提示单条决策走决策卡按钮', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState(),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toContain('不包含具体归因/推理内容')
    expect(out).toContain('决策卡')
  })

  it('包含时间戳头', () => {
    const out = formatViewSnapshotAsMarkdown({
      urlState: makeState(),
      totalSectionCount: 5,
      expandedSectionCount: 5,
      fullUrl: 'http://localhost/review',
      sections: [],
    })
    expect(out).toMatch(/生成于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}/)
  })
})
