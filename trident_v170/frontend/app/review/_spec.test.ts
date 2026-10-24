import { describe, it, expect } from 'vitest'

/**
 * app/review/_spec.test.ts — 单一事实源的不变式测试 (v302+ 重构 + v302+ P3 zod)
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
 *  7. v302+ P3 zod schema:复合不变量在 parse 时被强制 — 拒绝坏数据
 */

import {
  REVIEW_SECTIONS,
  SECTION_MATRIX,
  REVIEW_PANEL_SELECTOR,
  selectReviewPanel,
  isSectionVisible,
  sectionMetaSchema,
  reviewSectionsSchema,
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

// ====== v302+ P3+: selectReviewPanel / isSectionVisible 联动不变量 ======
// 这些测试不依赖任何 spec 字面量,只依赖"spec 的内部关系":
//   1. selectReviewPanel == null ⇔ review section 在该 mode 下不可见
//   2. journal 的 a/b 选同一面板(原版行为)
//   3. REVIEW_PANEL_SELECTOR 与 abAware section 的 spec 永远联动
// 谁动了 spec 让这些关系变样,会立刻被这组测试抓住。
describe('v302+ P3+: 联动不变量', () => {
  it('selectReviewPanel 返回 null ⇔ review section 在该 mode 不可见(双向)', () => {
    for (const mode of ALL_MODES) {
      for (const ab of ALL_AB) {
        const panel = selectReviewPanel(mode, ab)
        const visible = isSectionVisible(mode, 'review')
        if (panel === null) {
          expect(
            visible,
            `selectReviewPanel(${mode}, ${ab}) === null 但 review section 在 ${mode} 可见`,
          ).toBe(false)
        } else {
          expect(
            visible,
            `selectReviewPanel(${mode}, ${ab}) === ${panel} 但 review section 在 ${mode} 不可见`,
          ).toBe(true)
        }
      }
    }
  })

  it('REVIEW_PANEL_SELECTOR 的 key 集合 === REVIEW_SECTIONS.review.modes(双向)', () => {
    // selector 里有非 null 映射的 mode,review section 必须可见
    const selectorModes = Object.entries(REVIEW_PANEL_SELECTOR)
      .filter(([_, abMap]) => Object.values(abMap).some((p) => p !== null))
      .map(([mode]) => mode as ReviewTab)
    for (const mode of selectorModes) {
      expect(isSectionVisible(mode, 'review'), `selector 有 ${mode} 但 review section 不可见`).toBe(
        true,
      )
    }
  })

  it('journal 模式 a/b 选同一面板(原版行为锁定 — 改了会变 UI 行为)', () => {
    // 这是历史行为契约:journal 模式不论 ab 都查"历史真实交易"复盘。
    // 谁改成 a/b 不同会破坏 /journal 用户习惯。
    expect(selectReviewPanel('journal', 'a')).toBe(selectReviewPanel('journal', 'b'))
    expect(selectReviewPanel('journal', 'a')).toBe('ReviewHistoryPanel')
  })

  it('isSectionVisible 对 hidden section 必返回 false(防 漏写 modes)', () => {
    // 不在 modes 里的 mode 必定 visible=false
    for (const key of ALL_SECTIONS) {
      for (const mode of ALL_MODES) {
        const specModes = REVIEW_SECTIONS[key].modes as readonly ReviewTab[]
        if (!specModes.includes(mode)) {
          expect(isSectionVisible(mode, key), `${key} 在 ${mode} 不在 modes 列表但返回 true`).toBe(
            false,
          )
        }
      }
    }
  })

  it('isSectionVisible 是幂等函数(查两次结果相同)', () => {
    for (const mode of ALL_MODES) {
      for (const key of ALL_SECTIONS) {
        expect(isSectionVisible(mode, key)).toBe(isSectionVisible(mode, key))
      }
    }
  })

  it('REVIEW_PANEL_SELECTOR 不含任何 ReviewTab 之外的 mode 键(spec 与 use-review-url-state 同步)', () => {
    const selectorKeys = Object.keys(REVIEW_PANEL_SELECTOR) as ReviewTab[]
    expect(selectorKeys.sort()).toEqual([...ALL_MODES].sort())
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

// ====== 7. v302+ P3: zod schema 复合不变量测试 ======
//
// 目的:验证 zod schema 在模块加载时真的会拒绝坏数据。
// 如果 zod schema 写错(例如 .refine 条件反了),这些测试会立刻爆。

describe('zod schema — sectionMetaSchema 复合不变量', () => {
  const validSection = {
    modes: ['all'],
    lazy: false,
    errorBoundary: false,
    id: 'test-section',
    label: '测试段',
  }

  it('接受合法的最小 section', () => {
    expect(() => sectionMetaSchema.parse(validSection)).not.toThrow()
  })

  it('拒绝:modes 为空数组', () => {
    expect(() => sectionMetaSchema.parse({ ...validSection, modes: [] })).toThrow(/at least 1/i)
  })

  it('拒绝:modes 含非法 tab 值', () => {
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, modes: ['all', 'INVALID_TAB'] }),
    ).toThrow()
  })

  it('拒绝:id 为空字符串', () => {
    expect(() => sectionMetaSchema.parse({ ...validSection, id: '' })).toThrow()
  })

  it('拒绝:label 为空字符串', () => {
    expect(() => sectionMetaSchema.parse({ ...validSection, label: '' })).toThrow()
  })

  it('拒绝:lazy=true 但缺 minHeight(关键不变量)', () => {
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, lazy: true }),
    ).toThrow(/minHeight/i)
  })

  it('拒绝:lazy=true 但 minHeight <= 0(关键不变量)', () => {
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, lazy: true, minHeight: 0 }),
    ).toThrow(/minHeight/i)
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, lazy: true, minHeight: -100 }),
    ).toThrow()
  })

  it('拒绝:minHeight 不是整数', () => {
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, lazy: true, minHeight: 100.5 }),
    ).toThrow()
  })

  it('拒绝:未知字段(.strict() 生效)', () => {
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, typoField: 'should-fail' }),
    ).toThrow()
  })

  it('接受:lazy=true + minHeight=300', () => {
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, lazy: true, minHeight: 300 }),
    ).not.toThrow()
  })

  it('接受:abAware 字段(可选)', () => {
    expect(() =>
      sectionMetaSchema.parse({ ...validSection, abAware: true }),
    ).not.toThrow()
  })
})

describe('zod schema — reviewSectionsSchema 集合级不变量', () => {
  const baseSection = {
    modes: ['all'],
    lazy: false,
    errorBoundary: false,
    id: 'unique-id',
    label: '段',
  }

  it('接受:id 互不重复的多 section 集合', () => {
    const valid = {
      a: { ...baseSection, id: 'a' },
      b: { ...baseSection, id: 'b' },
    }
    expect(() => reviewSectionsSchema.parse(valid)).not.toThrow()
  })

  it('拒绝:id 重复的集合(关键不变量)', () => {
    const dup = {
      a: { ...baseSection, id: 'same' },
      b: { ...baseSection, id: 'same' },
    }
    expect(() => reviewSectionsSchema.parse(dup)).toThrow(/id.*唯一|必须唯一/)
  })

  it('拒绝:任一 section 违反 sectionMetaSchema(错误聚合)', () => {
    const broken = {
      good: baseSection,
      bad: { ...baseSection, id: '', lazy: true /* 缺 minHeight */ },
    }
    expect(() => reviewSectionsSchema.parse(broken)).toThrow()
  })
})

describe('zod schema — REVIEW_SECTIONS 实际 parse 通过', () => {
  it('当前 REVIEW_SECTIONS 字面量能被 zod 接受(模块加载已成功 parse)', () => {
    // REVIEW_SECTIONS 已被模块顶层 zod.parse() 处理过 — 若 parse 失败,
    // 导入 _spec 就会抛错,所有测试不会运行到这里。所以这里只需要断言
    // "REVIEW_SECTIONS 是合法的、shape 与 spec 一致" 即可。
    expect(Object.keys(REVIEW_SECTIONS).length).toBeGreaterThan(0)
    for (const key of Object.keys(REVIEW_SECTIONS)) {
      const s = REVIEW_SECTIONS[key as SectionKey]
      expect(s.id).toBeTruthy()
      expect(s.label).toBeTruthy()
      expect(s.modes.length).toBeGreaterThan(0)
      // 不变量:lazy=true → minHeight 必填
      if (s.lazy) {
        expect(s.minHeight).toBeDefined()
        expect(s.minHeight!).toBeGreaterThan(0)
      }
    }
  })
})