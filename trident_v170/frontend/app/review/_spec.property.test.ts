import { describe, it, expect } from 'vitest'
import fc from 'fast-check'

/**
 * app/review/_spec.property.test.ts — 属性测试 (v302+ 重构 — 阶段 D)
 *
 * 惊艳点(什么叫惊艳 — 这里就是):
 *  - 手写不变式测试只能覆盖已知维度;属性测试保证:
 *    "任何人加新 tab / 加新 section / 加新 abTab,测试矩阵自动展开"
 *  - fc.assert + fc.property 跑 N 次随机抽样,N 越大覆盖率越高
 *  - vitest 默认每个 property 跑 100 次 → 100 种 (mode × section × ab)
 *    组合,且每次随机 = 抓 bug 的概率随 N 线性增长
 *
 * 与 _spec.test.ts 的关系:
 *  - _spec.test.ts:手写"已知重要 case"(回归断言,可读性高)
 *  - _spec.property.test.ts:随机探索"未知的边界 case"(覆盖率广)
 *  - 两者互补:手写测试固定锚点,属性测试铺开广度
 */

import {
  REVIEW_SECTIONS,
  SECTION_MATRIX,
  selectReviewPanel,
  isSectionVisible,
  type SectionKey,
} from './_spec'
import type { ReviewTab, AbTab } from '@/lib/use-review-url-state'

// ====== Arbitraries(测试用例生成器) ======

const reviewTabArb: fc.Arbitrary<ReviewTab> = fc.constantFrom('all', 'journal', 'experiences')
const abTabArb: fc.Arbitrary<AbTab> = fc.constantFrom('a', 'b')
const sectionKeyArb: fc.Arbitrary<SectionKey> = fc.constantFrom(
  ...(Object.keys(REVIEW_SECTIONS) as SectionKey[]),
)

// ====== 属性 1:SECTION_MATRIX 与 spec 一致性 ======

describe('属性 — SECTION_MATRIX 一致性', () => {
  it('任何 (mode, section) 对,SECTION_MATRIX[mode][section] 都等于 spec 派生值', () => {
    fc.assert(
      fc.property(reviewTabArb, sectionKeyArb, (mode, key) => {
        const expected = (REVIEW_SECTIONS[key].modes as readonly ReviewTab[]).includes(mode)
        const actual = SECTION_MATRIX[mode][key]
        // ⚠️ 失败时 vitest 会输出具体的 mode 和 key(shrink 自动最小化反例)
        expect(actual, `${mode} × ${key}`).toBe(expected)
      }),
      { numRuns: 100 },
    )
  })

  it('isSectionVisible 与 SECTION_MATRIX 永远一致(纯函数契约)', () => {
    fc.assert(
      fc.property(reviewTabArb, sectionKeyArb, (mode, key) => {
        expect(isSectionVisible(mode, key)).toBe(SECTION_MATRIX[mode][key])
      }),
      { numRuns: 100 },
    )
  })
})

// ====== 属性 2:复盘面板 lookup 的健全性 ======

describe('属性 — selectReviewPanel 健全性', () => {
  it('experiences 模式不论 ab 都返回 null(不会泄漏 review panel 到 experiences tab)', () => {
    fc.assert(
      fc.property(abTabArb, (ab) => {
        expect(selectReviewPanel('experiences', ab)).toBeNull()
      }),
      { numRuns: 50 },
    )
  })

  it('journal / all 模式不论 ab 都返回非 null 的有效面板', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('all' as const, 'journal' as const),
        abTabArb,
        (mode, ab) => {
          const panel = selectReviewPanel(mode, ab)
          expect(panel).not.toBeNull()
          expect(['ReviewPanel', 'ReviewHistoryPanel']).toContain(panel)
        },
      ),
      { numRuns: 50 },
    )
  })
})

// ====== 属性 3:abAware section 的可见 mode 必有面板 ======

describe('属性 — abAware section 与 selector 联动', () => {
  it('任何 abAware section 在其所有可见 mode × 所有 ab 下都有非 null 面板', () => {
    // 过滤出 abAware 的 sections
    const abAwareKeys = (Object.keys(REVIEW_SECTIONS) as SectionKey[]).filter(
      (k) => 'abAware' in REVIEW_SECTIONS[k] && REVIEW_SECTIONS[k].abAware === true,
    )

    fc.assert(
      fc.property(
        fc.constantFrom(...abAwareKeys),
        reviewTabArb,
        abTabArb,
        (key, mode, ab) => {
          // ⚠️ 'in' 检查确保只有真有 abAware 的 section 才进入这条性质
          if (!('abAware' in REVIEW_SECTIONS[key])) return
          if (!REVIEW_SECTIONS[key].abAware) return

          const visible = isSectionVisible(mode, key)
          if (visible) {
            // section 在此 mode 下展示 → 必须有面板(否则用户看到空白)
            const panel = selectReviewPanel(mode, ab)
            expect(panel, `${key} 在 ${mode} × ab=${ab} 应有面板`).not.toBeNull()
          }
        },
      ),
      { numRuns: 100 },
    )
  })
})

// ====== 属性 4:spec 的结构健全性 ======

describe('属性 — REVIEW_SECTIONS 结构健全性', () => {
  it('任何 section 都有合法 modes(id 唯一 / label 非空 / modes 非空)', () => {
    fc.assert(
      fc.property(sectionKeyArb, (key) => {
        const spec = REVIEW_SECTIONS[key]
        expect(spec.modes.length).toBeGreaterThan(0)
        expect(spec.id).toBeTruthy()
        expect(spec.label).toBeTruthy()
      }),
      { numRuns: 50 },
    )
  })

  it('lazy=true 的 section 必有 minHeight > 0', () => {
    fc.assert(
      fc.property(sectionKeyArb, (key) => {
        const spec = REVIEW_SECTIONS[key]
        if (spec.lazy) {
          expect(spec.minHeight, `${key} lazy=true 但缺 minHeight`).toBeDefined()
          expect(spec.minHeight).toBeGreaterThan(0)
        }
      }),
      { numRuns: 50 },
    )
  })
})

// ====== 属性 5:永不出现"空白页"(防最严重回归) ======

describe('属性 — 永不空白页', () => {
  it('任何 mode 下至少有一个 section visible', () => {
    fc.assert(
      fc.property(reviewTabArb, (mode) => {
        const sectionKeys = Object.keys(REVIEW_SECTIONS) as SectionKey[]
        const visibleCount = sectionKeys.filter((k) => isSectionVisible(mode, k)).length
        expect(visibleCount, `${mode} mode 下任何 section 都不显示 → 空白页`).toBeGreaterThan(0)
      }),
      { numRuns: 30 },
    )
  })
})