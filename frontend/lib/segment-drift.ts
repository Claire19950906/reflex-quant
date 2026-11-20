/**
 * v302+ P1 重构: 段位漂移检测。
 *
 * 业务背景:
 *  - 每条 AI 决策的 reasoning_path 被切成 8 个段: 总框架 / 因素定位 / 驱动力 /
 *    水位博弈 / 跨资产资金 / 反共识结论 / 逻辑自检 / 总结 (见 FIXED_STAGE_KEYS)
 *  - 历史上各段都有稳定的命中率(驱动力 77.9% / 水位博弈 83.7% / 反共识 83.5% /
 *    跨资产资金 10.0% / 逻辑自检 16.0% — 见 analysis-parse.ts 注释)
 *  - 漂移告警: 某段命中率突然偏离基线 X 个百分点 → 可能是 prompt 注入 / 模型降级
 *    / 数据源污染 / schema 失配 等问题
 *
 * 设计:
 *  - 纯函数 detectSegmentDrift(presence, baseline, opts) → result
 *  - presence: 二维数组 [decision][stage] = 0/1
 *  - baseline: 8 个段的历史命中率
 *  - threshold: 触发告警的偏差(默认 0.15 = ±15pp)
 *  - 严重程度分 warn (±15pp) / alert (±30pp) 两档
 *
 * 为什么不直接 chi-square: 简单 pp 差更易解释(给运营看),不需要样本量 > 30
 * 的统计严谨性。这里定位是"异常感知",不是"假设检验"。
 */

export const FIXED_STAGE_KEYS = [
  '总框架',
  '因素定位',
  '驱动力',
  '水位博弈',
  '跨资产资金',
  '反共识结论',
  '逻辑自检',
  '总结',
] as const

export type StageKey = (typeof FIXED_STAGE_KEYS)[number]
export type StagePresence = Record<StageKey, 0 | 1>
export type BaselineDist = Record<StageKey, number>

export type DriftAlert = {
  stage: StageKey
  current: number
  baseline: number
  /** current - baseline,范围 [-1, 1] */
  delta: number
  /** ±0.30 算 alert, ±0.15 算 warn */
  severity: 'warn' | 'alert'
}

export type DriftResult = {
  /** 输入的决策数 */
  total: number
  /** 各段当前命中率(0..1) */
  current: BaselineDist
  /** 各段偏差(current - baseline) */
  deltas: BaselineDist
  /** 触阈告警的段(按 |delta| 降序) */
  alerts: DriftAlert[]
}

export type DriftOptions = {
  /** 触发 warn 的偏差阈值(默认 0.15 = ±15pp) */
  warnThreshold?: number
  /** 触发 alert 的偏差阈值(默认 0.30 = ±30pp) */
  alertThreshold?: number
  /** 总决策数低于此值不告警(样本不足,默认 20) */
  minSample?: number
}

const DEFAULTS = {
  warnThreshold: 0.15,
  alertThreshold: 0.3,
  minSample: 20,
} as const

/** 把一维决策-段位矩阵汇总成各段命中率(纯函数,可独立测) */
export function computeCurrentDistribution(
  presence: ReadonlyArray<StagePresence>,
): BaselineDist {
  const out = Object.fromEntries(
    FIXED_STAGE_KEYS.map((k) => [k, 0]),
  ) as unknown as BaselineDist
  if (presence.length === 0) return out
  for (const row of presence) {
    for (const k of FIXED_STAGE_KEYS) {
      if (row[k]) out[k] += 1
    }
  }
  const n = presence.length
  for (const k of FIXED_STAGE_KEYS) {
    out[k] = out[k] / n
  }
  return out
}

/** 主入口: 检测段位分布漂移 */
export function detectSegmentDrift(
  presence: ReadonlyArray<StagePresence>,
  baseline: BaselineDist,
  options: DriftOptions = {},
): DriftResult {
  const opts = { ...DEFAULTS, ...options }
  const total = presence.length
  const current = computeCurrentDistribution(presence)
  const deltas = Object.fromEntries(
    FIXED_STAGE_KEYS.map((k) => [k, current[k] - baseline[k]]),
  ) as unknown as BaselineDist

  // 样本不足 → 不告警,但仍返回 current/deltas 供观察
  if (total < opts.minSample) {
    return { total, current, deltas, alerts: [] }
  }

  const alerts: DriftAlert[] = []
  for (const k of FIXED_STAGE_KEYS) {
    const d = Math.abs(deltas[k])
    if (d >= opts.alertThreshold) {
      alerts.push({
        stage: k,
        current: current[k],
        baseline: baseline[k],
        delta: deltas[k],
        severity: 'alert',
      })
    } else if (d >= opts.warnThreshold) {
      alerts.push({
        stage: k,
        current: current[k],
        baseline: baseline[k],
        delta: deltas[k],
        severity: 'warn',
      })
    }
  }
  // 按 |delta| 降序
  alerts.sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta))

  return { total, current, deltas, alerts }
}

/** 默认基线(取自 analysis-parse.ts 注释,400 条决策实测) */
export const DEFAULT_BASELINE: BaselineDist = {
  '总框架': 0.95,
  '因素定位': 0.9,
  '驱动力': 0.779,
  '水位博弈': 0.837,
  '跨资产资金': 0.1,
  '反共识结论': 0.835,
  '逻辑自检': 0.16,
  '总结': 0.98,
}

/**
 * 从 reviews 列表推导 presence 矩阵。
 *
 * 输入: /reasoning/review/list 返回的 Review[] (每条带 steps.missing: string[])
 * 输出: StagePresence[] — 每行一个决策,每列一个段位,1 = 该段非空,0 = 该段缺
 *
 * 推导规则: 段位 = stage_name, 如果该 stage 不在 review.steps.missing 中 → present
 *   (因为 Review API 把"有/无"用 missing 数组表达,不是用 8 个 boolean)
 */
export function presenceFromReviews<
  R extends { steps: { missing: string[] } },
>(reviews: ReadonlyArray<R>): StagePresence[] {
  return reviews.map((r) => {
    const missing = new Set(r.steps.missing)
    const row = {} as StagePresence
    for (const k of FIXED_STAGE_KEYS) {
      row[k] = missing.has(k) ? 0 : 1
    }
    return row
  })
}

/** 选最严重的 N 条告警(默认 3) */
export function topDriftAlerts(result: DriftResult, n = 3): DriftAlert[] {
  return result.alerts.slice(0, n)
}

export type RollingBaselineOptions = {
  /** 用最近 N 条决策(默认 100) */
  window?: number
  /** 跳过尾部 N 条(默认 0)— 拿历史段不算当前批 */
  skipTail?: number
}

/**
 * 滚动基线: 取历史最近 N 条决策,算各段命中率作为基线。
 *
 * 用法: 比如当前批 30 条 + 历史 200 条,窗口=100,skipTail=30 → 用历史 101-200 这 100 条算基线
 *   rollingBaseline(allHistory.slice(0, -30), { window: 100 })
 *
 * 为什么需要:
 *   DEFAULT_BASELINE 是 6 个月前的实测,模型改进后基线会偏移 → 假阳性告警。
 *   滚动基线随模型自然漂移,告警只反映"当前 vs 近期"的异常跳变。
 */
export function rollingBaseline(
  history: ReadonlyArray<StagePresence>,
  options: RollingBaselineOptions = {},
): BaselineDist {
  const { window = 100, skipTail = 0 } = options
  if (history.length <= skipTail) {
    return Object.fromEntries(FIXED_STAGE_KEYS.map((k) => [k, 0])) as BaselineDist
  }
  const usable = history.slice(0, history.length - skipTail)
  const slice = usable.length > window ? usable.slice(usable.length - window) : usable
  return computeCurrentDistribution(slice)
}

/**
 * 一步到位的滑动基线漂移检测:
 *   - 把历史 + 当前拼起来
 *   - 用历史的滚动窗口算基线
 *   - 用当前算实际分布
 *   - 比对
 *
 * 典型用法:
 *   const r = detectDriftWithRollingBaseline(allPresence, { window: 100, currentSize: 30 })
 */
export type DetectDriftRollingOpts = RollingBaselineOptions & {
  /** 尾部 N 条视为当前批(默认全用) */
  currentSize?: number
  /** 透传给 detectSegmentDrift */
  driftOptions?: DriftOptions
}

export function detectDriftWithRollingBaseline(
  presence: ReadonlyArray<StagePresence>,
  options: DetectDriftRollingOpts = {},
): DriftResult & { baseline: BaselineDist; baselineSource: 'static' | 'rolling' } {
  const { currentSize = presence.length, ...rest } = options
  const baseline = rollingBaseline(presence, { ...rest, skipTail: currentSize })
  const current = computeCurrentDistribution(presence.slice(presence.length - currentSize))
  const result = detectSegmentDrift(
    presence.slice(presence.length - currentSize),
    baseline,
    options.driftOptions,
  )
  return { ...result, baseline, baselineSource: 'rolling' }
}

/** 空 DriftResult(无数据时回退,符合 DriftResult 形状) */
export const EMPTY_DRIFT: DriftResult = {
  total: 0,
  current: Object.fromEntries(FIXED_STAGE_KEYS.map((k) => [k, 0])) as BaselineDist,
  deltas: Object.fromEntries(FIXED_STAGE_KEYS.map((k) => [k, 0])) as BaselineDist,
  alerts: [],
}

/**
 * 从 reviews 数组一步算 drift: presence + 静态基线比对。
 *
 * 用于: reasoning-review-panel / failure-stats-panel / 任何拿到 Review[] 的面板
 * 返回: 永远有效的 DriftResult(空数据返回 EMPTY_DRIFT,不需要 UI 端判空)
 *
 * 对外保留 hook 形态: 内部用 useMemo,避免无依赖重算。
 * 注意: 这个文件是 lib/ 不是 hooks/ 目录,因为它是纯函数不依赖 React 状态。
 *   React 包装见 use-segment-drift.ts
 */
