import { describe, it, expect } from 'vitest'
import {
  fmtBeijing,
  fmtBeijingShort,
  fmtBeijingDate,
  inferDataSource,
  DATA_SOURCE_META,
} from './data-channel-standards'

/**
 * 这些测试是 v302+ P0+ 【修复】的回归用例 —— 后端 raw_news / timestamp 字符串
 * 代表"北京时间",无 tz 那部分应补 +08:00 而不是 +Z(UTC)。
 * 旧逻辑补 Z 会把北京时间当 UTC,显示时漂移 8h。
 *
 * 测试目的:确保修复不退化。
 */

describe('fmtBeijing — 8h 漂移 bug 回归', () => {
  it('无 tz 的字符串应解释为北京时间,不漂移到 UTC', () => {
    // "2026-01-01 09:00:00" 这个字符串代表北京时间 9:00
    // 旧代码补 +Z 会显示成 17:00:00 (UTC+8 倒推)
    const out = fmtBeijing('2026-01-01 09:00:00')
    expect(out).toMatch(/09:00:00/)
    expect(out).not.toMatch(/17:00:00/)
  })

  it('带 Z 的 ISO 字符串按 UTC 解析,转 +8 后应显示正确', () => {
    // 2026-01-01T01:00:00Z = UTC 1:00 = 北京时间 9:00
    const out = fmtBeijing('2026-01-01T01:00:00Z')
    expect(out).toMatch(/09:00:00/)
  })

  it('已带 +08:00 偏移的字符串不二次偏移', () => {
    const out = fmtBeijing('2026-01-01T09:00:00+08:00')
    expect(out).toMatch(/09:00:00/)
  })

  it('T 分隔符兼容', () => {
    // "2026-01-01T09:00:00" (无 tz) → 应视为北京时间
    const out = fmtBeijing('2026-01-01T09:00:00')
    expect(out).toMatch(/09:00:00/)
  })

  it('epoch 自动归一(ms → s)', () => {
    // 1735689600000 ms = 2025-01-01T00:00:00Z = 北京时间 08:00
    const out = fmtBeijing(1735689600000)
    expect(out).toMatch(/08:00:00/)
  })

  it('epoch 秒级 (1e10-1e12) 不会被错当 ms', () => {
    // 1735689600 s = 2025-01-01T00:00:00Z = 北京时间 08:00
    const out = fmtBeijing(1735689600)
    expect(out).toMatch(/08:00:00/)
  })

  it('空值返回 — 兜底', () => {
    expect(fmtBeijing(null)).toBe('—')
    expect(fmtBeijing(undefined)).toBe('—')
    expect(fmtBeijing('')).toBe('—')
  })

  it('无效字符串返回 — 不抛', () => {
    expect(() => fmtBeijing('not a date')).not.toThrow()
    expect(fmtBeijing('not a date')).toBe('—')
  })
})

describe('fmtBeijingShort / fmtBeijingDate', () => {
  it('fmtBeijingShort 截断秒后为空值返回 —', () => {
    expect(fmtBeijingShort(null)).toBe('—')
  })

  it('fmtBeijingDate 只取日期部分', () => {
    expect(fmtBeijingDate('2026-01-01 09:00:00')).toBe('2026-01-01')
  })

  it('fmtBeijingDate 空值返回 —', () => {
    expect(fmtBeijingDate(null)).toBe('—')
  })
})

describe('inferDataSource — 4 维度数据源映射', () => {
  it('paper_trading 表 → real', () => {
    expect(inferDataSource({ table: 'paper_trading' })).toBe('real')
    expect(inferDataSource({ table: 'trade_reflections_v2' })).toBe('real')
  })

  it('quick_sim_trades + settled → simulated', () => {
    expect(inferDataSource({ table: 'quick_sim_trades', settled: true })).toBe('simulated')
  })

  it('quick_sim_trades + 未 settled → live', () => {
    expect(inferDataSource({ table: 'quick_sim_trades', settled: false })).toBe('live')
  })

  it('ai_decisions 默认 live', () => {
    expect(inferDataSource({ table: 'ai_decisions' })).toBe('live')
  })

  it('backtest / experience_rules → backtest', () => {
    expect(inferDataSource({ table: 'experience_rules' })).toBe('backtest')
  })

  it('source 字段优先级高于 table', () => {
    expect(inferDataSource({ source: 'paper', table: 'ai_decisions' })).toBe('real')
    expect(inferDataSource({ source: 'quick_sim_settled', table: 'ai_decisions' })).toBe(
      'simulated',
    )
  })

  it('DATA_SOURCE_META 4 个键齐全且互斥', () => {
    const keys = Object.keys(DATA_SOURCE_META).sort()
    expect(keys).toEqual(['backtest', 'live', 'real', 'simulated'])
  })
})
