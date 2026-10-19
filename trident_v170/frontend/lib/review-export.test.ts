import { describe, it, expect } from 'vitest'
import { formatReviewAsMarkdown, summarizeReviewShort } from './review-export'
import type { Review, DecisionTextResp } from './types-reasoning-review'

/** 测试用 Review 工厂(默认值全命中,各测试改某字段触发断言) */
function mkReview(over: Partial<Review> = {}): Review {
  return {
    decision_id: 12345,
    asset: 'BTC',
    action: 'BUY',
    completion_score: 77.5,
    forward_pnl: 1.2,
    is_correct: 1,
    reasoning_length: 480,
    steps: { present: ['总框架', '因素定位', '驱动力', '水位博弈', '跨资产资金', '反共识结论', '逻辑自检', '总结'], missing: [], pct: 100 },
    counter_consensus: { present: true, keywords_hit: ['市场情绪'] },
    causal_mechanism: { present: true, keywords_hit: ['资金费'] },
    factor_concepts: { count: 2, concepts: ['资金费', '持仓量'] },
    cross_asset_direction: {
      present: true,
      target_asset: 'ETH',
      items: [
        { symbol: 'ETH', pct_24h: 2.3, aligned: true, conflicts: false, neutral: false },
        { symbol: 'SOL', pct_24h: 1.5, aligned: true, conflicts: false, neutral: false },
      ],
      aligned_count: 3,
      conflict_count: 0,
      detail: '3 一致 / 0 冲突',
    },
    assumption: { present: true, keywords_hit: ['假设美联储暂停'] },
    historical_pattern: { present: true, keywords_hit: ['历史同期'] },
    calibration: { confidence: 0.7, pnl_realized: 1.2, is_correct: 1 },
    notes: [],
    ...over,
  }
}

function mkText(): DecisionTextResp {
  return {
    decision_id: 12345,
    asset: 'BTC',
    action: 'BUY',
    reasoning_path: '/reasoning/BTC.md',
    reasoning: '总框架: BTC 短期反弹。\n因素定位: ...\n总结: BUY 仓位 5%。',
    asset_reasoning: 'BTC 过去 24h +1.2%',
    full_text: '总框架: BTC 短期反弹。\n因素定位: ...\n总结: BUY 仓位 5%。',
    full_text_length: 48,
    segments: { all: ['总框架', '总结'], present: ['总框架', '总结'], missing: [], positions: [] },
  }
}

describe('formatReviewAsMarkdown — 结构与内容', () => {
  it('头部含 decision_id / asset / action', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect(md).toMatch(/^# 决策 #12345 — BTC BUY/m)
  })

  it('完备度 77.5% 在头部表格里', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect(md).toContain('77.5%')
  })

  it('盈利 1.20 在头部表格里', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect(md).toContain('盈利 1.20')
  })

  it('8 段齐全全 ✅', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect((md.match(/✅/g) || []).length).toBeGreaterThanOrEqual(8)
    expect(md).not.toContain('❌')
  })

  it('missing 段 → ❌', () => {
    const md = formatReviewAsMarkdown(
      mkReview({ steps: { present: [], missing: ['跨资产资金', '总结'], pct: 0 } }),
      null,
    )
    expect(md).toContain('❌ 跨资产资金')
    expect(md).toContain('❌ 总结')
  })

  it('6 维完备度: 反共识/因果/假设/历史 命中 → ✅', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect(md).toContain('**反共识** ✅')
    expect(md).toContain('**因果机制** ✅')
    expect(md).toContain('**假设** ✅')
    expect(md).toContain('**历史样本** ✅')
  })

  it('6 维完备度: 缺关键词命中 → 不显示 [xxx]', () => {
    const md = formatReviewAsMarkdown(
      mkReview({
        counter_consensus: { present: true, keywords_hit: [] },
      }),
      null,
    )
    // 命中 [空数组] 不会渲染
    expect(md).not.toMatch(/命中 \[\]/)
  })

  it('跨资产明细表: 2 行(BTC→ETH/SOL)', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect(md).toContain('## 跨资产明细')
    expect(md).toContain('| ETH |')
    expect(md).toContain('| SOL |')
    expect(md).toContain('+2.30%')
  })

  it('跨资产缺 → 跨资产明细 段不出现', () => {
    const md = formatReviewAsMarkdown(
      mkReview({
        cross_asset_direction: {
          present: false,
          target_asset: '',
          aligned_count: 0,
          conflict_count: 0,
          items: [],
          detail: '',
        },
      }),
      null,
    )
    expect(md).not.toContain('## 跨资产明细')
  })

  it('推理原文: includeFullText=true(default) → 含 ```', () => {
    const md = formatReviewAsMarkdown(mkReview(), mkText())
    expect(md).toContain('## 推理原文')
    expect(md).toContain('```')
    expect(md).toContain('总框架: BTC 短期反弹。')
  })

  it('推理原文: includeFullText=false → 不含原文', () => {
    const md = formatReviewAsMarkdown(mkReview(), mkText(), {
      includeFullText: false,
    })
    expect(md).not.toContain('## 推理原文')
  })

  it('段位表: includeStepTable=false → 不含"## 段位齐全"', () => {
    const md = formatReviewAsMarkdown(mkReview(), null, { includeStepTable: false })
    expect(md).not.toContain('## 段位齐全')
  })

  it('元信息脚注含 formatReviewAsMarkdown', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect(md).toMatch(/导出于 \d{4}-\d{2}-\d{2}T.*formatReviewAsMarkdown/)
  })

  it('未结算: 头表格显示"未结算"', () => {
    const md = formatReviewAsMarkdown(
      mkReview({ forward_pnl: null, is_correct: null }),
      null,
    )
    expect(md).toContain('未结算')
    expect(md).not.toContain('盈利 null')
  })

  it('亏损: 头表格显示"亏损 1.20"', () => {
    const md = formatReviewAsMarkdown(
      mkReview({ forward_pnl: -1.2, is_correct: 0 }),
      null,
    )
    expect(md).toContain('亏损 -1.20')
  })

  it('空 text 不会让 markdown 崩溃', () => {
    const md = formatReviewAsMarkdown(mkReview(), null)
    expect(md).toBeTypeOf('string')
    expect(md.length).toBeGreaterThan(0)
  })

  it('SELL 大写化', () => {
    const md = formatReviewAsMarkdown(mkReview({ action: 'sell' }), null)
    expect(md).toContain('SELL')
  })
})

describe('summarizeReviewShort — 简短摘要', () => {
  it('标准 1 行摘要含所有关键字段', () => {
    const s = summarizeReviewShort(mkReview())
    expect(s).toBe('BTC BUY #12345 · 完备度 78% · 段位齐')
  })

  it('缺段 → "N 段缺"', () => {
    const s = summarizeReviewShort(
      mkReview({ steps: { present: [], missing: ['跨资产资金', '总结'], pct: 0 } }),
    )
    expect(s).toContain('2 段缺')
  })

  it('完备度四舍五入到整数', () => {
    const s = summarizeReviewShort(mkReview({ completion_score: 78.49 }))
    expect(s).toContain('78%')
  })

  it('空 action 显示 —', () => {
    const s = summarizeReviewShort(mkReview({ action: '' }))
    expect(s).toContain('—')
  })
})
