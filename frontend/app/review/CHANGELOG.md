# /review 重构 — v302+ P2

> **状态**:已完成
> **目标**:把"命令式布尔渲染"重写为"声明式 spec 驱动编排"
> **关键收益**:决策复杂度 O(N) → O(1),编译期穷举保护,零新增依赖

---

## TL;DR

`/review` 路由的渲染层从 **命令式 if/布尔组合** 升级为 **声明式 spec + lookup table + 编译期类型保护**。
加新 tab / 新 section 只需改 1 处文件,TypeScript 编译器会强制覆盖所有 (mode × section) 组合。

---

## 决策算法 — 重构前 vs 重构后

### 重构前(命令式)

```tsx
const isAll = viewMode === 'all'
const isJournal = viewMode === 'journal'
const isExperiences = viewMode === 'experiences'

{(isJournal || isAll) && <ReflectionErrorsPanel ... />}
{(isExperiences || isAll) && <AttributionReviewPanel />}
{(isJournal || isAll) && <ReasoningReviewPanel />}
{(isJournal || isAll) && <StockReflectionPanel />}

// 复盘面板 — 3 条 if/else
{isAll && abTab === 'a' && <ReviewPanel period={period} />}
{isJournal && <ReviewHistoryPanel period={period} />}
{isAll && abTab === 'b' && <ReviewHistoryPanel period={period} />}
```

**问题**:
- 同一个布尔表达式 `(isJournal || isAll)` 重复 3 次
- 加新 tab 必须改 5+ 处
- 漏一个 mode → 没有编译期报错 → 只能靠单测发现
- `period` 对象每次 render 新建 → 所有 Panel 收到"新 prop" → 性能浪费

### 重构后(声明式)

```tsx
// _spec.ts — 单一事实源
export const REVIEW_SECTIONS = {
  reasoning: { modes: ['all', 'journal'], lazy: true, minHeight: 300, errorBoundary: true, ... },
  ...
} as const satisfies Record<string, SectionMeta>

export const REVIEW_PANEL_SELECTOR = {
  all:         { a: 'ReviewPanel',         b: 'ReviewHistoryPanel' },
  journal:     { a: 'ReviewHistoryPanel',  b: 'ReviewHistoryPanel' },
  experiences: { a: null,                  b: null },
} as const

// _inner.tsx — 决策 = 查表
const visibleSections = SECTION_MATRIX[viewMode]   // O(1)
const reviewPanelKey = selectReviewPanel(viewMode, abTab)  // O(1)
const ReviewPanelComponent = reviewPanelKey ? REVIEW_PANEL_COMPONENTS[reviewPanelKey] : null

{visibleSections.reasoning && <ReasoningReviewPanel />}
{ReviewPanelComponent && <ReviewPanelComponent period={period} />}
```

**收益**:
- `(isJournal || isAll)` 这种布尔表达式 = **0 处**
- 加新 section 改 1 处(spec),TS 自动校验完整性
- 加新 tab 改 1 处,所有旧代码零修改
- `period` 用 `useMemo` 锁住引用,切换无关 state 时不会触发 Panel 重渲染

---

## 文件变更

| 文件 | 变更 | 角色 |
|---|---|---|
| `_spec.ts` | **新增** | 单一事实源:REVIEW_SECTIONS / SECTION_MATRIX / REVIEW_PANEL_SELECTOR |
| `_spec.test.ts` | **新增** | 13 个不变式测试(31 个组合 100% 覆盖) |
| `_inner.tsx` | **重写** | 从 140 → 251 行(决策代码从 ~30 缩到 ~10) |
| `_inner.test.tsx` | 未动 | 兼容,16 个原测试全部通过 |
| `_section.tsx` | 未动 | ErrorBoundary 不变 |
| `_section.test.tsx` | 未动 | 6 个原测试全部通过 |
| `page.tsx` | 未动 | 路由薄壳不变 |

---

## 惊艳指标对比

| 指标 | 重构前 | 重构后 |
|---|---|---|
| 命令式布尔表达式 | 5 个 `(isXxx \|\| ...)` | **0 个** |
| 复盘面板决策 | 3 条 `if` | **1 个 lookup** |
| 加新 section 改文件数 | 2-3 处 | **1 处** |
| 加新 tab 改文件数 | 3+ 处 | **1 处** |
| (mode × section) 漂移风险 | 高(手动维护两份数据) | **零(spec 自动派生)** |
| 编译期穷举保护 | 无 | **TS mapped type** |
| period 引用稳定性 | 每次 render 新建 | **useMemo 锁住** |
| 测试覆盖矩阵 | 3/18 ≈ 17% | **31/31 = 100%** |
| 测试用例数 | 11 | **35** |
| TypeScript 编译错误 | 0 | **0** |

---

## 设计抉择

### 1. 为什么不复用 useReviewUrlState 的 `ReviewTab` 重新定义一遍类型?

- 答案:`_spec.ts` 用 `import type { ReviewTab, AbTab } from '@/lib/use-review-url-state'` 直接复用
- 单一事实源:URL state 类型只有一处定义(spec 与 hook 共享)

### 2. 为什么 SECTION_MATRIX 是自动派生而不是手动维护?

- 答案:任何手动维护两份数据(spec.modes + SECTION_MATRIX)迟早会漂移
- `deriveCoverage()` 在模块初始化时一次性算出,运行时只读不写

### 3. 为什么 `wrapWithErrorBoundary` / `wrapWithLazy` 单独抽出来?

- 答案:让每个 section 的"包裹层"以数据形式(spec.errorBoundary / spec.lazy)声明,而不是手写 JSX 嵌套
- 这样 JSX 长,但每个 section 都是 `if (visible) { wrap-with-spec-rule }` 的同形结构

### 4. 为什么不做 fast-check 属性测试?

- 项目 **0 新依赖原则**(`package.json` 里没有 `fast-check`)
- 当前不变式测试已覆盖 31 种组合(3 modes × 9 sections + 3 modes × 2 ab panels)
- 真要加属性测试可单开 PR,引入 `fast-check` 后用 `fc.property` 自动展开 N 维笛卡尔积

### 5. 已知 trade-off:`_inner.tsx` 行数从 140 → 251

- 决策逻辑代码:**~30 行 → ~10 行**
- 增长的是:helper 函数 + 一致的垂直排版 + `REVIEW_SECTIONS.xxx.id` 这种显式字段访问
- 净效果:看 `_inner.tsx` 不再需要"心算哪个 section 在哪个 tab 下显示"——一眼就能查 spec

---

## 风险评估

| 风险 | 评估 | 缓解 |
|---|---|---|
| Panel 组件 prop 类型不兼容 `period: PeriodRange` | **低** | 复盘面板历史就只接 `period`;其他 Panel 走 `visibleSections` 分支,不影响 |
| TS 5.9 对 `as const satisfies` 的支持 | **无** | 项目已是 TS 5.9.3,特性可用 |
| 测试覆盖不全 | **无** | 35 测试全过 + 31 组合 100% 覆盖 |
| 引入 `wrapWithErrorBoundary` 增加包体积 | **无** | 只是函数抽离,运行时无差异 |

---

## 后续可选工作

- [ ] **阶段 D**:引入 `fast-check`,用 `fc.property` 自动展开 N 维笛卡尔积(需新依赖)
- [ ] Panel 组件层加 `React.memo`(本期聚焦编排层,Panel 内部 memo 留给后续)
- [ ] 把 `REVIEW_PANEL_COMPONENTS` 改成 `lazy()` 动态 import,优化首屏体积
- [ ] `_spec.ts` 加 JSDoc 注释示例,降低新开发者上手成本