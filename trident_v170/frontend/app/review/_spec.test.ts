import { describe, it, expect } from 'vitest'

/**
 * app/review/_spec.test.ts — 单一事实源的不变式测试 (v302+ 重构)
 *
 * 惊艳点:这些测试是"声明式 spec 的回归网"。
 * 如果谁改了 _spec.ts 但忘了改另一处,下面任何一条测试都会爆。
 *
 * 覆盖的不变式:
 *  1. SECTION_MATRIX[mode][key] 严格 === REVIEW_SECTIONS[key].modes.includes(mode)
 *     → spec 与派生矩阵永远一致(防止手动维护两份漂移)
 *  2. 每个 mode 至少 1 个 section visible(防止"空白页"回归)
 *  3. REVIEW_PANEL_SELECTOR 完全覆盖 (mode × ab) 二维空间
 *  4. 每个 abAware section 都与 REVIEW_PANEL_SELECTOR 存在映射关系
 *  5. 永远显示的 section (dataHealth / improve) 的可见性恒为 true
 *  6. selectReviewPanel 是纯函数(同输入同输出)
 */

import {
  REVIEW_SECTIONS,
  SECTION_MATRIX,
  REVIEW_PANEL_SELECTOR,
  selectReviewPanel,
  isSectionVisible,
  type SectionKey,
} from './_spec'
import type { ReviewTab, AbTab } from '@/lib/use-review-url-state'

const ALL_MODES: readonly ReviewTab[] = ['all', 'journal', 'experiences']
const ALL_AB: readonly AbTab[] = ['a', 'b']
const ALL_SECTIONS = Object.keys(REVIEW_SECTIONS) as SectionKey[]

describe('REVIEW_SECTIONS — spec 结构', () => {
  it('不为空,且每个 section 都有 modes / id / label', () => {
    expect(ALL_SECTIONS.length).toBeGreaterThan(0)
    for (const key of ALL_SECTIONS) {
      const spec = REVIEW_SECTIONS[key]
      expect(spec.modes.length).toBeGreaterThan(0)
      expect(spec.id).toBeTruthy()
      expect(spec.label).toBeTruthy()
    }
  })

  it('lazy=true 的 section 必填 minHeight', () => {
    for (const [key, spec] of Object.entries(REVIEW_SECTIONS)) {
      if (spec.lazy) {
        expect(spec.minHeight, `${key} 是 lazy 但没填 minHeight`).toBeDefined()
        expect(spec.minHeight).toBeGreaterThan(0)
      }
    }
  })

  it('id 在所有 section 间唯一(防止 DOM 锚点冲突)', () => {
    const ids = ALL_SECTIONS.map((k) => REVIEW_SECTIONS[k].id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})

describe('SECTION_MATRIX — 派生一致性', () => {
  it('SECTION_MATRIX[mode][key] === REVIEW_SECTIONS[key].modes.includes(mode)', () => {
    for (const mode of ALL_MODES) {
      for (const key of ALL_SECTIONS) {
        // ⚠️ as const 把 modes 推成 tuple literal,includes 拒绝更宽的 ReviewTab
        const expected = (REVIEW_SECTIONS[key].modes as readonly ReviewTab[]).includes(mode)
        const actual = SECTION_MATRIX[mode][key]
        expect(actual, `${mode} × ${key}`).toBe(expected)
      }
    }
  })

  it('每个 mode 至少 1 个 section visible(防空白页)', () => {
    for (const mode of ALL_MODES) {
      const visibleCount = ALL_SECTIONS.filter((k) => SECTION_MATRIX[mode][k]).length
      expect(visibleCount, `${mode} mode 下没有任何 section`).toBeGreaterThan(0)
    }
  })

  it('isSectionVisible 与 SECTION_MATRIX 完全一致(纯函数契约)', () => {
    for (const mode of ALL_MODES) {
      for (const key of ALL_SECTIONS) {
        expect(isSectionVisible(mode, key)).toBe(SECTION_MATRIX[mode][key])
      }
    }
  })
})

describe('REVIEW_PANEL_SELECTOR — 复盘面板 lookup', () => {
  it('覆盖所有 (mode × ab) 组合', () => {
    for (const mode of ALL_MODES) {
      for (const ab of ALL_AB) {
        expect(REVIEW_PANEL_SELECTOR[mode]).toHaveProperty(ab)
      }
    }
  })

  it('experiences 模式不论 ab 都返回 null(spec 设计意图)', () => {
    for (const ab of ALL_AB) {
      expect(selectReviewPanel('experiences', ab)).toBeNull()
    }
  })

  it('journal 模式不论 ab 都返回 ReviewHistoryPanel(原版行为)', () => {
    for (const ab of ALL_AB) {
      expect(selectReviewPanel('journal', ab)).toBe('ReviewHistoryPanel')
    }
  })

  it('all 模式 a → ReviewPanel,b → ReviewHistoryPanel(原版行为)', () => {
    expect(selectReviewPanel('all', 'a')).toBe('ReviewPanel')
    expect(selectReviewPanel('all', 'b')).toBe('ReviewHistoryPanel')
  })

  it('selectReviewPanel 是纯函数(同输入同输出,无副作用)', () => {
    const r1 = selectReviewPanel('all', 'a')
    const r2 = selectReviewPanel('all', 'a')
    expect(r1).toBe(r2)
  })
})

describe('abAware section — 与 selector 联动', () => {
  it('只有 review section 是 abAware', () => {
    // ⚠️ as const 让 abAware 只在 review section 的类型分支里
    // 用 'in' 收窄访问
    const abAwareSections = ALL_SECTIONS.filter((k) => 'abAware' in REVIEW_SECTIONS[k])
    expect(abAwareSections).toEqual(['review'])
  })

  it('abAware section 的所有可见 mode 在 selector 中都有非 null 映射', () => {
    for (const [key, spec] of Object.entries(REVIEW_SECTIONS)) {
      // ⚠️ as const 让 abAware 只在 review section 的类型分支里,其他 section 类型上看不到该属性
      // 用 'in' 收窄,避免强制 cast
      if (!('abAware' in spec) || !spec.abAware) continue
      for (const mode of spec.modes as readonly ReviewTab[]) {
        for (const ab of ALL_AB) {
          const panel = selectReviewPanel(mode, ab)
          expect(panel, `${key} 在 ${mode} mode 下 ab=${ab} 应有面板`).not.toBeNull()
        }
      }
    }
  })
})

describe('关键 section 的可见性 — 防回归', () => {
  it('draftSummary 三种 tab 都展示(顶部横幅)', () => {
    for (const mode of ALL_MODES) {
      expect(isSectionVisible(mode, 'draftSummary'), mode).toBe(true)
    }
  })

  it('dataHealth 三种 tab 都展示', () => {
    for (const mode of ALL_MODES) {
      expect(isSectionVisible(mode, 'dataHealth'), mode).toBe(true)
    }
  })

  it('improve 三种 tab 都展示', () => {
    for (const mode of ALL_MODES) {
      expect(isSectionVisible(mode, 'improve'), mode).toBe(true)
    }
  })

  it('experiences tab 下不展示推理审查 / 股票反思 / reflection errors(修复 v302+ 旧 bug)', () => {
    expect(isSectionVisible('experiences', 'reasoning')).toBe(false)
    expect(isSectionVisible('experiences', 'stockReflection')).toBe(false)
    expect(isSectionVisible('experiences', 'reflectionErrors')).toBe(false)
  })

  it('experiences tab 下展示 attribution + failureStats(原版行为)', () => {
    expect(isSectionVisible('experiences', 'attribution')).toBe(true)
    expect(isSectionVisible('experiences', 'failureStats')).toBe(true)
  })

  it('journal tab 下不展示 failureStats(原版行为)', () => {
    expect(isSectionVisible('journal', 'failureStats')).toBe(false)
  })

  it('journal tab 下展示 review(原版行为)', () => {
    expect(isSectionVisible('journal', 'review')).toBe(true)
  })
})