/**
 * reflection schema 测试。
 *
 * 目标:验证 zod schema 在"字段缺失 / 类型错误 / 额外字段"下的容错与报错能力,
 * 以及 parseReflectionRecent / parseReflectionStats 返回的 ParseResult 形态。
 *
 * 这是 reflection-errors-panel.tsx 的"运行时数据合约",
 * 后端字段调整时能立即被测试捕获。
 */

import { describe, it, expect } from 'vitest'
import {
  parseReflectionRecent,
  parseReflectionStats,
  ReflectionRecentSchema,
  ReflectionStatsSchema,
  DecisionErrorSchema,
} from './reflection'

describe('parseReflectionRecent — happy path', () => {
  it('完整字段都通过', () => {
    const raw = {
      total: 12,
      errors: [
        {
          id: 1,
          decision_id: 100,
          error_code: 'E001',
          origin: 'checker',
          severity: 2,
          detail: '数据缺失',
          source: 'paper',
          created_at: '2026-01-01 09:00:00',
        },
      ],
      code_stats: { E001: 5, E002: 7 },
      origin_stats: { checker: 8, llm: 4 },
      severity_stats: { '2': 12 },
      prompt_preview: '...',
      code_subkinds: { 'E001a': 3, 'E001b': 2 },
      cross_asset_snapshot: {
        anchor_ts: '2026-01-01 09:00:00',
        asset: 'XAU',
        macro_regime: 'risk_on',
        pct_only: { XAU: 100 },
      },
      code_pnl_impact: [
        {
          code: 'E001',
          n_errors: 5,
          n_settled: 4,
          avg_pnl: -0.012,
          total_pnl: -0.05,
          win_rate_when_coded: 0.2,
          worst_pnl: -0.08,
          best_pnl: 0.02,
        },
      ],
    }
    const r = parseReflectionRecent(raw)
    expect(r.ok).toBe(true)
    if (r.ok) {
      expect(r.data.total).toBe(12)
      expect(r.data.errors).toHaveLength(1)
      expect(r.data.code_pnl_impact?.[0]?.total_pnl).toBe(-0.05)
    }
  })

  it('缺可选字段仍通过', () => {
    const raw = {
      total: 0,
      errors: [],
      code_stats: {},
      origin_stats: {},
      severity_stats: {},
      prompt_preview: '',
      // 没有 code_subkinds / cross_asset_snapshot / code_pnl_impact
    }
    const r = parseReflectionRecent(raw)
    expect(r.ok).toBe(true)
  })

  it('空字符串 prompt_preview 合法', () => {
    const raw = {
      total: 0,
      errors: [],
      code_stats: {},
      origin_stats: {},
      severity_stats: {},
      prompt_preview: '',
    }
    const r = parseReflectionRecent(raw)
    expect(r.ok).toBe(true)
  })
})

describe('parseReflectionRecent — 错误情况', () => {
  it('缺 total 字段 → ok:false + 路径提示', () => {
    const raw = {
      // total 缺
      errors: [],
      code_stats: {},
      origin_stats: {},
      severity_stats: {},
      prompt_preview: '',
    }
    const r = parseReflectionRecent(raw)
    expect(r.ok).toBe(false)
    if (r.ok === false) {
      expect(r.error).toContain('total')
    }
  })

  it('errors 不是数组 → ok:false', () => {
    const raw = {
      total: 0,
      errors: 'not array',
      code_stats: {},
      origin_stats: {},
      severity_stats: {},
      prompt_preview: '',
    }
    const r = parseReflectionRecent(raw)
    expect(r.ok).toBe(false)
  })

  it('DecisionError 缺 detail → ok:false + 路径含 errors.0.detail', () => {
    const raw = {
      total: 1,
      errors: [
        {
          id: 1,
          decision_id: 1,
          error_code: 'E001',
          origin: 'checker',
          severity: 2,
          // detail 缺
          created_at: '2026-01-01',
        },
      ],
      code_stats: {},
      origin_stats: {},
      severity_stats: {},
      prompt_preview: '',
    }
    const r = parseReflectionRecent(raw)
    expect(r.ok).toBe(false)
    if (r.ok === false) {
      expect(r.error).toContain('detail')
    }
  })

  it('null 输入 → ok:false', () => {
    const r = parseReflectionRecent(null)
    expect(r.ok).toBe(false)
  })

  it('undefined 输入 → ok:false', () => {
    const r = parseReflectionRecent(undefined)
    expect(r.ok).toBe(false)
  })

  it('顶层是数组 → ok:false', () => {
    const r = parseReflectionRecent([])
    expect(r.ok).toBe(false)
  })
})

describe('parseReflectionStats', () => {
  it('完整字段通过', () => {
    const raw = {
      days: 30,
      total: 100,
      by_code: { E001: 50 },
      by_origin: { checker: 80, llm: 20 },
      by_severity: { '1': 10, '2': 80, '3': 10 },
      rows: [{ error_code: 'E001', origin: 'checker', severity: 2, cnt: 50 }],
    }
    const r = parseReflectionStats(raw)
    expect(r.ok).toBe(true)
  })

  it('rows 缺一个字段 → ok:false', () => {
    const raw = {
      days: 30,
      total: 100,
      by_code: {},
      by_origin: {},
      by_severity: {},
      rows: [{ error_code: 'E001', origin: 'checker', severity: 2 }], // 缺 cnt
    }
    const r = parseReflectionStats(raw)
    expect(r.ok).toBe(false)
  })
})

describe('Schema — 错误 issue 数截断', () => {
  it('多个错误时 formatZodError 只显示前 5 条', () => {
    // 故意构造多处错误
    const raw = {} // 缺全部必填字段
    const r = parseReflectionRecent(raw)
    expect(r.ok).toBe(false)
    if (r.ok === false) {
      // 总共应该 ≥ 5 条 issue,但格式只显示前 5 条
      expect(r.error.split(';').length).toBeLessThanOrEqual(5)
    }
  })
})

describe('Schema — 直通验证(schema 本身)', () => {
  it('DecisionError 允许 evidence_refs 是任意类型', () => {
    const raw = {
      decision_id: 1,
      error_code: 'E001',
      origin: 'checker',
      severity: 2,
      detail: 'x',
      created_at: '2026-01-01',
      evidence_refs: { weird: { deeply: { nested: [1, 2, 3] } } },
    }
    expect(DecisionErrorSchema.safeParse(raw).success).toBe(true)
  })

  it('DecisionError severity 字符串(而非数字)会拒', () => {
    const raw = {
      decision_id: 1,
      error_code: 'E001',
      origin: 'checker',
      severity: '2', // string,不是 number
      detail: 'x',
      created_at: '2026-01-01',
    }
    expect(DecisionErrorSchema.safeParse(raw).success).toBe(false)
  })

  it('ReflectionStatsSchema total 缺 → ok:false', () => {
    const raw = {
      // total 缺
      days: 30,
      by_code: {},
      by_origin: {},
      by_severity: {},
      rows: [],
    }
    expect(ReflectionStatsSchema.safeParse(raw).success).toBe(false)
  })
})