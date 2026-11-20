/**
 * draft-storage 单元测试 —— LS 持久化层(v275+, v302+ PR-15)。
 *
 * 覆盖:
 *  - SSR 安全 / 解析失败 / saveDraft 强制 open=false / quota 静默吞 / lsKey 格式
 *  - v302+ PR-15: listAllDrafts / removeDraft / parseLsKey
 */

import { describe, it, expect, beforeEach } from 'vitest'
import {
  loadDraft,
  saveDraft,
  removeDraft,
  listAllDrafts,
  parseLsKey,
  lsKey,
  emptyDraft,
  REFLECTION_DRAFT_LS_PREFIX,
} from './draft-storage'

describe('lsKey', () => {
  it('格式 = `${prefix}${decisionId}:${errorCode}`', () => {
    expect(lsKey(123, 'E001')).toBe(`${REFLECTION_DRAFT_LS_PREFIX}123:E001`)
  })
  it('不同 decisionId 区分', () => {
    expect(lsKey(1, 'E001')).not.toBe(lsKey(2, 'E001'))
  })
})

describe('emptyDraft', () => {
  it('默认 open=true,text 空,coreMarked=false', () => {
    const d = emptyDraft()
    expect(d.open).toBe(true)
    expect(d.text).toBe('')
    expect(d.coreMarked).toBe(false)
    expect(d.improvementText).toBe('')
    expect(d.aiQuestions).toEqual([])
    expect(d.loadingFollowups).toBe(false)
  })
})

describe('loadDraft', () => {
  beforeEach(() => {
    localStorage.clear()
  })
  it('无数据 → null', () => {
    expect(loadDraft(1, 'E001')).toBeNull()
  })
  it('解析失败(JSON 损坏)→ null,不抛', () => {
    localStorage.setItem(lsKey(1, 'E001'), '{not valid json')
    expect(() => loadDraft(1, 'E001')).not.toThrow()
    expect(loadDraft(1, 'E001')).toBeNull()
  })
  it('非对象值 → null', () => {
    localStorage.setItem(lsKey(1, 'E001'), '"abc"')
    expect(loadDraft(1, 'E001')).toBeNull()
    localStorage.setItem(lsKey(1, 'E001'), '123')
    expect(loadDraft(1, 'E001')).toBeNull()
    localStorage.setItem(lsKey(1, 'E001'), 'null')
    expect(loadDraft(1, 'E001')).toBeNull()
  })
  it('合法对象原样返回', () => {
    const draft = { open: false, text: 'hello', coreMarked: true, improvementText: '', aiQuestions: ['q1'], loadingFollowups: false }
    localStorage.setItem(lsKey(1, 'E001'), JSON.stringify(draft))
    expect(loadDraft(1, 'E001')).toEqual(draft)
  })
})

describe('saveDraft', () => {
  beforeEach(() => {
    localStorage.clear()
  })
  it('写入后 localStorage 能读出', () => {
    const draft = emptyDraft()
    draft.text = '保存测试'
    saveDraft(1, 'E001', draft)
    const stored = localStorage.getItem(lsKey(1, 'E001'))
    expect(stored).toBeTruthy()
    expect(JSON.parse(stored!).text).toBe('保存测试')
  })
  it('强制 open=false 持久化(刷新默认收起)', () => {
    const draft = emptyDraft()
    expect(draft.open).toBe(true)
    saveDraft(1, 'E001', draft)
    expect(JSON.parse(localStorage.getItem(lsKey(1, 'E001'))!).open).toBe(false)
  })
  it('写入 quota 超限 → 静默吞', () => {
    const orig = localStorage.setItem
    localStorage.setItem = () => {
      throw new DOMException('quota exceeded', 'QuotaExceededError')
    }
    expect(() => saveDraft(1, 'E001', emptyDraft())).not.toThrow()
    localStorage.setItem = orig
  })
})

describe('parseLsKey', () => {
  it('合法 key → { decisionId, errorCode }', () => {
    expect(parseLsKey(`${REFLECTION_DRAFT_LS_PREFIX}123:E001`)).toEqual({
      decisionId: 123,
      errorCode: 'E001',
    })
  })
  it('非 reflection 草稿 key → null', () => {
    expect(parseLsKey('other-prefix:1:E001')).toBeNull()
    expect(parseLsKey('reflection-draft')).toBeNull()
  })
  it('格式错误(无冒号)→ null', () => {
    expect(parseLsKey(`${REFLECTION_DRAFT_LS_PREFIX}123`)).toBeNull()
  })
  it('decisionId 非数字 → null', () => {
    expect(parseLsKey(`${REFLECTION_DRAFT_LS_PREFIX}abc:E001`)).toBeNull()
    expect(parseLsKey(`${REFLECTION_DRAFT_LS_PREFIX}1.5:E001`)).toBeNull()
  })
  it('冒号出现在 errorCode 内部 → 正确切分(只切第一个冒号)', () => {
    expect(parseLsKey(`${REFLECTION_DRAFT_LS_PREFIX}7:E001:sub`)).toEqual({
      decisionId: 7,
      errorCode: 'E001:sub',
    })
  })
})

describe('removeDraft', () => {
  beforeEach(() => {
    localStorage.clear()
  })
  it('删除存在 key → 后续 loadDraft 返回 null', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'to delete' })
    expect(loadDraft(1, 'E001')).not.toBeNull()
    removeDraft(1, 'E001')
    expect(loadDraft(1, 'E001')).toBeNull()
  })
  it('删除不存在 key → 不抛', () => {
    expect(() => removeDraft(999, 'E999')).not.toThrow()
  })
  it('removeItem 抛错(隐私模式)→ 静默吞', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'x' })
    const orig = localStorage.removeItem
    localStorage.removeItem = () => {
      throw new DOMException('disabled', 'SecurityError')
    }
    expect(() => removeDraft(1, 'E001')).not.toThrow()
    localStorage.removeItem = orig
  })
})

describe('listAllDrafts', () => {
  beforeEach(() => {
    localStorage.clear()
  })
  it('空 LS → 空数组', () => {
    expect(listAllDrafts()).toEqual([])
  })
  it('有非 reflection LS key → 忽略', () => {
    localStorage.setItem('other:key', '{"text":"x"}')
    localStorage.setItem('reflection-draft-broken', '{"text":"x"}')
    expect(listAllDrafts()).toEqual([])
  })
  it('多个 reflection 草稿 → 全部返回,按 decisionId + errorCode 排序', () => {
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'A' })
    saveDraft(1, 'E002', { ...emptyDraft(), text: 'B' })
    saveDraft(1, 'E001', { ...emptyDraft(), text: 'C' })
    saveDraft(2, 'E002', { ...emptyDraft(), text: 'D' })
    const all = listAllDrafts()
    expect(all.map((e) => [e.decisionId, e.errorCode])).toEqual([
      [1, 'E001'],
      [1, 'E002'],
      [2, 'E001'],
      [2, 'E002'],
    ])
  })
  it('空草稿(text 空 + coreMarked=false + improvementText 空 + 无 aiQuestions)→ 过滤', () => {
    saveDraft(1, 'E001', emptyDraft())
    expect(listAllDrafts()).toEqual([])
  })
  it('任意字段非空 → 不过滤', () => {
    saveDraft(1, 'E001', { ...emptyDraft(), coreMarked: true })
    saveDraft(2, 'E001', { ...emptyDraft(), aiQuestions: ['q'] })
    saveDraft(3, 'E001', { ...emptyDraft(), improvementText: '改' })
    expect(listAllDrafts()).toHaveLength(3)
  })
  it('JSON 损坏 → 跳过该 key', () => {
    localStorage.setItem(lsKey(1, 'E001'), '{not valid')
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'good' })
    const all = listAllDrafts()
    expect(all).toHaveLength(1)
    expect(all[0].decisionId).toBe(2)
  })
  it('非对象(JSON 字符串/数字/null)→ 跳过', () => {
    localStorage.setItem(lsKey(1, 'E001'), '"abc"')
    localStorage.setItem(lsKey(1, 'E002'), '123')
    localStorage.setItem(lsKey(1, 'E003'), 'null')
    saveDraft(2, 'E001', { ...emptyDraft(), text: 'good' })
    const all = listAllDrafts()
    expect(all).toHaveLength(1)
    expect(all[0].decisionId).toBe(2)
  })
})
