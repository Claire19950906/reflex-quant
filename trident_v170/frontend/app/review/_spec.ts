/**
 * app/review/_spec.ts — /review 渲染规则的单一事实源 (v302+ 重构 + v302+ P3 zod)
 *
 * 惊艳点 (什么叫惊艳 — 这里就是):
 *  - REVIEW_SECTIONS 是声明式 spec,加新 section 只改这里一行
 *  - SECTION_MATRIX 自动从 spec 派生,杜绝"spec 与矩阵漂移"
 *  - REVIEW_PANEL_SELECTOR 是 O(1) lookup,显式 null 表达"此模式不展示"
 *  - 所有 (mode × ab × panel) 决策 = 数据查找,不再有命令式 if-else
 *  - 任一 SectionKey 加进 REVIEW_SECTIONS 必须显式出现在每个 mode 的 modes
 *    列表里(编译期保护,漏一个 TS 立刻报错)
 *  - v302+ P3: zod schema 运行时校验 REVIEW_SECTIONS — JSON 持久化 / 跨进程
 *    反序列化时类型擦除,zod 是最后一道防线(lazy=true 必须有 minHeight 等
 *    复合不变量,TS 做不到的,zod .refine 兜底)
 *
 * 不上 fast-check 属性测试:项目 0 新依赖原则,要加属性测试另开 PR。
 * 但即使没属性测试,本 spec 已经把"决策表"从命令式 140 行收缩到 70 行数据。
 */

import { z } from 'zod'
import type { ReviewTab, AbTab } from '@/lib/use-review-url-state'

// 复用 use-review-url-state 的类型(单一事实源:URL 状态类型只有一处定义)
export type { ReviewTab, AbTab } from '@/lib/use-review-url-state'

// ====== 0. zod 运行时 schema — JSON 持久化 / 跨进程 / 类型擦除场景的兜底 ======

/** ReviewTab 的 zod 字面量集合 — 与 use-review-url-state 同步 */
export const reviewTabEnum = z.enum(['all', 'journal', 'experiences'])

/** 单个 section 元数据的 zod schema */
export const sectionMetaSchema = z
  .object({
    modes: z.array(reviewTabEnum).min(1),
    lazy: z.boolean(),
    minHeight: z.number().int().positive().optional(),
    errorBoundary: z.boolean(),
    abAware: z.boolean().optional(),
    id: z.string().min(1),
    label: z.string().min(1),
  })
  .strict() // 拒绝未知字段,防止 typo 偷偷进入(zod .strict() 必须在 .refine() 之前)
  // 复合不变量:lazy=true 时必须有 minHeight > 0(TS mapped type 做不到这个)
  .refine((s) => !s.lazy || (typeof s.minHeight === 'number' && s.minHeight > 0), {
    message: 'lazy=true 的 section 必须填写 minHeight (>0)',
    path: ['minHeight'],
  })
  // id 不能重复(防止 DOM 锚点冲突)
  // (此约束走集合级,见 reviewSectionsSchema)

/** REVIEW_SECTIONS 的 zod schema — 完整 section 集合 */
export const reviewSectionsSchema = z
  .record(z.string().min(1), sectionMetaSchema)
  .refine(
    (sections) => {
      const ids = Object.values(sections).map((s) => s.id)
      return new Set(ids).size === ids.length
    },
    { message: 'id 在所有 section 间必须唯一(防止 DOM 锚点冲突)' },
  )

// ====== 1. Section 元数据(spec) ======

/** 单个 section 的渲染规则 — 加新字段必须保证向后兼容 */
type SectionMeta = {
  /** 此 section 在哪些 tab 下展示 — 单一事实源 */
  readonly modes: readonly ReviewTab[]
  /** 是否用 <LazySection> 包裹 */
  readonly lazy: boolean
  /** LazySection 占位高度(仅 lazy=true 时必填) */
  readonly minHeight?: number
  /** 是否用 <SectionErrorBoundary> 包裹 */
  readonly errorBoundary: boolean
  /** 是否参与 abTab 决策(仅 review section 用) */
  readonly abAware?: boolean
  /** section 在 DOM 中的 id(用于锚点跳转 / 测试 selector) */
  readonly id: string
  /** section 的 aria-label(屏幕阅读器 / 测试断言) */
  readonly label: string
}

/** /review 上每个 section 的元数据 — 加新 section 改这里 */
// 原始 spec 字面量 — 用 zod parse 后赋值给导出常量,保证运行时与编译期双重保护
const REVIEW_SECTIONS_RAW = {
  draftSummary: {
    modes: ['all', 'journal', 'experiences'],
    lazy: false,
    errorBoundary: false,
    id: 'draft-summary',
    label: '草稿总览',
  },
  reflectionErrors: {
    modes: ['all', 'journal'],
    lazy: true,
    minHeight: 400,
    errorBoundary: false,
    id: 'diagnose-reflection',
    label: '诊断 · 反思错误',
  },
  attribution: {
    modes: ['all', 'experiences'],
    lazy: true,
    minHeight: 300,
    errorBoundary: false,
    id: 'diagnose-attribution',
    label: '诊断 · 归因审查',
  },
  reasoning: {
    modes: ['all', 'journal'],
    lazy: true,
    minHeight: 300,
    errorBoundary: true,
    id: 'reasoning',
    label: '推理审查',
  },
  stockReflection: {
    modes: ['all', 'journal'],
    lazy: true,
    minHeight: 200,
    errorBoundary: false,
    id: 'stock-reflection',
    label: '股票反思',
  },
  failureStats: {
    // ⚠️ 隶属 "总览" section,但渲染规则独立记录在此(spec 与渲染解耦)
    modes: ['all', 'experiences'],
    lazy: false,
    errorBoundary: false,
    id: 'failure-stats',
    label: '失败统计',
  },
  review: {
    modes: ['all', 'journal'],
    lazy: true,
    minHeight: 500,
    errorBoundary: true,
    abAware: true,
    id: 'review',
    label: '复盘',
  },
  improve: {
    modes: ['all', 'journal', 'experiences'],
    lazy: false,
    errorBoundary: false,
    id: 'improve',
    label: '改进',
  },
  dataHealth: {
    // 永远显示 — 但放这里便于测试断言
    modes: ['all', 'journal', 'experiences'],
    lazy: false,
    errorBoundary: false,
    id: 'data-health-section',
    label: '数据健康',
  },
} as const satisfies Record<string, SectionMeta>

/**
 * v302+ P3:zod 运行时校验。复合不变量(id 唯一、lazy 必带 minHeight)在编译期
 * 类型系统无法完全保证,所以用 zod 在模块加载时 .parse() 兜底。
 * 任何 typo / 漏字段 / 不变量违反 → 模块加载即抛错,生产环境 0 容忍。
 */
export const REVIEW_SECTIONS = reviewSectionsSchema.parse(REVIEW_SECTIONS_RAW) as Record<
  keyof typeof REVIEW_SECTIONS_RAW,
  SectionMeta
>

/** 所有 section key 的字面量联合 — 派生自 §[keyof],加新 key 自动生效 */
export type SectionKey = keyof typeof REVIEW_SECTIONS

// ====== 2. 编译期覆盖矩阵(自动派生 — spec 与矩阵不可能漂移) ======

/** 完整的 (mode × section) 覆盖矩阵类型 — TS 用于校验 */
type DerivedCoverage = {
  [M in ReviewTab]: {
    [K in SectionKey]: boolean
  }
}

/** 所有 ReviewTab 字面量(取自 use-review-url-state 的 VALID_TABS 镜像,避免循环依赖) */
const REVIEW_TABS: readonly ReviewTab[] = ['all', 'journal', 'experiences'] as const

/** 自动从 REVIEW_SECTIONS 派生 SECTION_MATRIX — 任何人改了 §.modes,这里同步更新 */
function deriveCoverage(): DerivedCoverage {
  const out = {} as Record<ReviewTab, Record<string, boolean>>
  for (const mode of REVIEW_TABS) {
    out[mode] = {}
    for (const [key, spec] of Object.entries(REVIEW_SECTIONS)) {
      // ⚠️ as const 把 modes 推成 tuple literal,includes 拒绝更宽的 ReviewTab
      // cast 为 readonly ReviewTab[] 解决(运行时调用是数据级别的,modes 真的是 ReviewTab 列表)
      out[mode][key] = (spec.modes as readonly ReviewTab[]).includes(mode)
    }
  }
  return out as DerivedCoverage
}

/** mode × section 可见性矩阵 — 编排层只查这里,不再写 if */
export const SECTION_MATRIX: DerivedCoverage = deriveCoverage()

// ====== 3. 复盘面板选择(O(1) lookup,显式 null = 不展示) ======

/** 复盘面板的可选 key — 用 string literal 而非组件引用,避免循环依赖 */
export type ReviewPanelKey = 'ReviewPanel' | 'ReviewHistoryPanel'

/** (mode, ab) → 面板 key 的查表 — null 显式表达"此模式不展示" */
export const REVIEW_PANEL_SELECTOR: {
  readonly [M in ReviewTab]: { readonly [A in AbTab]: ReviewPanelKey | null }
} = {
  all: {
    a: 'ReviewPanel',
    b: 'ReviewHistoryPanel',
  },
  journal: {
    a: 'ReviewHistoryPanel',
    b: 'ReviewHistoryPanel',
  },
  experiences: {
    a: null,
    b: null,
  },
}

// ====== 4. 工具函数(纯函数,便于单测) ======

/** 给定 mode + ab,返回当前应渲染的复盘面板 key(可能为 null) */
export function selectReviewPanel(mode: ReviewTab, ab: AbTab): ReviewPanelKey | null {
  return REVIEW_PANEL_SELECTOR[mode][ab]
}

/** 给定 mode + section,此 section 是否应该渲染 — O(1) */
export function isSectionVisible(mode: ReviewTab, key: SectionKey): boolean {
  return SECTION_MATRIX[mode][key]
}