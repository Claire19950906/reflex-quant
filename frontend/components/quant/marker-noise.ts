/**
 * marker-noise.ts — v318 归因页 K 线 marker 降噪 (纯函数, 可离线单测)
 * ============================================================================
 *
 * 为什么要这个文件:
 *   归因页 K 线上的 marker 之前是"有多少画多少" —— `/impact/by-asset`
 *   一律 limit=200, 前端拿到 151 条 impact 就画 151 个红点 (而且**涨跌一律红色**,
 *   涨的跌的都红)。异动圆点也没有幅度门槛和冷却, 量能激增时能连着出几十个点。
 *   用户反馈: "满屏红点, 看不清重点"。
 *
 *   降噪必须**同时**满足这几条, 少一条都会漏:
 *     1. 最小幅度门槛 (minAbsPct)      —— 0.1% 的波动不值得一个 marker
 *     2. 同 K 线数量上限 (maxPerBar)    —— 一根柱子上挂 8 个标签没法看
 *     3. 同桶去重                      —— worker 可能重复写同一条事件
 *     4. 全局总量上限 (maxTotal)        —— 按优先级(严重度→幅度)截断, 不是按时间截断
 *     5. 同类型冷却 (cooldownMs)        —— 同一类异动 15 分钟内只留第一个
 *     6. 方向着色                      —— 涨绿跌红, 不能一律红
 *
 * 设计约束: 全部是**纯函数**, 不依赖 React / DOM / 网络 ——
 *   这样能离线跑单测 (frontend/components/quant/marker-noise.test.ts),
 *   也能在 Python 侧用同一套阈值做后端预过滤, 两端口径不会漂移。
 */

/**
 * impact marker: 最小涨跌幅门槛 (%) —— 低于此值不画。
 *
 * v319: 1.0% → **0.3%**。
 *   后端现在统一把 impact_pct 输出成**有符号百分数** (event_kline_impact 原本是
 *   小数 0.0033 = 0.33%, 已 ×100), 而真实数据里主资产 (XAU) 的事件冲击
 *   30d 内: >=1% 只有 35 条, >=0.3% 有 337 条 —— 1% 门槛在 XAU 上几乎画不出点。
 *   0.3% 正是 worker 自己的 "medium" 分档线 (engine/event_kline_impact_worker.py
 *   _SEVERITY_THRESHOLDS = (0.003, 0.01)), 低于它的属于噪声, 不配一个 marker。
 */
export const IMPACT_MIN_ABS_PCT = 0.3
/** impact marker: 同一根 K 线最多画几个 */
export const IMPACT_MAX_PER_BAR = 2
/** impact marker: 全局最多画几个 (按严重度→幅度截断) */
export const IMPACT_MAX_TOTAL = 40
/** 异动圆点: 最小涨跌幅门槛 (%) */
export const ABNORMAL_MIN_ABS_PCT = 0.2
/** 异动圆点: 同类型冷却窗口 (ms) */
export const ABNORMAL_COOLDOWN_MS = 15 * 60 * 1000
/** 异动圆点: 同一根 K 线最多画几个 */
export const ABNORMAL_MAX_PER_BAR = 3
/** 异动圆点: 量能异动在无涨跌幅时的量比门槛 */
export const ABNORMAL_VOL_RATIO_MIN = 2.0

export type ImpactMarkerLike = {
  event_time: number
  event_title?: string | null
  /** 有符号涨跌幅 (百分数, 正=涨) —— v319 后端统一单位 */
  impact_pct?: number | null
  /** 幅度 (百分数, 恒 ≥ 0) —— 后端给的原始幅度, 优先用于门槛判定 */
  impact_abs_pct?: number | null
  move_pct?: number | null
  impact_severity?: string | null
  move_direction?: string | null
  event_backed?: boolean | null
  event_source?: string | null
  event_id?: string | null
}

export type AbnormalMarkerLike = {
  ts: number
  pct_change?: number | null
  vol_ratio?: number | null
  trigger_type?: string | null
}

export type MarkerFilterOptions = {
  /** ts → K 线桶序号 (用于"同柱"判定); 不传则按 ts 自身分桶 */
  align?: (ts: number) => number
  minAbsPct?: number
  maxPerBar?: number
  maxTotal?: number
  cooldownMs?: number
  window?: { from: number; to: number }
}

const SEVERITY_RANK: Record<string, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
}

/**
 * 取 impact 的有效涨跌幅 (幅度, 恒 ≥ 0)。
 *
 * v319: 优先用后端给的 **impact_abs_pct** (幅度百分数) —— impact_pct 现在是
 * *有符号* 的 (neutral 方向的事件 impact_pct=0, 但幅度未必是 0),
 * 直接取 |impact_pct| 会把这类"涨跌幅方向不明但确实动过"的事件判成"幅度不足"。
 */
export function impactAbsPct(it: ImpactMarkerLike): number {
  const v = it.impact_abs_pct ?? it.impact_pct ?? it.move_pct ?? 0
  const n = typeof v === 'number' && Number.isFinite(v) ? v : 0
  return Math.abs(n)
}

/**
 * impact 的**真实涨跌方向** (up/down/flat)。
 *
 * 关键: 方向来自**价格涨跌幅的符号**, 不是 ev_action ("利好/利空")。
 *   利空消息也可能被资金买在低点 → ev_action=SELL 但价格是涨的。
 *   一律按 ev_action 上色就会"方向反着标", 一律红色则"满屏红"。
 */
export function impactDirection(it: ImpactMarkerLike): 'up' | 'down' | 'flat' {
  const md = (it.move_direction || '').toLowerCase()
  if (md === 'up' || md === 'down' || md === 'flat') return md
  const v = it.impact_pct ?? it.move_pct ?? 0
  const n = typeof v === 'number' && Number.isFinite(v) ? v : 0
  if (n > 0) return 'up'
  if (n < 0) return 'down'
  return 'flat'
}

/** impact marker 颜色 (涨绿跌红, 与项目既有 "涨绿跌红" 口径一致) */
export function impactMarkerColor(it: ImpactMarkerLike): string {
  const d = impactDirection(it)
  if (d === 'up') return '#22c55e'
  if (d === 'down') return '#ef4444'
  return '#f59e0b'
}

/** impact marker 的图例分组 (绿=看涨, 红=看跌, 无方向=不归组) */
export function impactMarkerGroup(it: ImpactMarkerLike): 'green' | 'red' | undefined {
  const d = impactDirection(it)
  if (d === 'up') return 'green'
  if (d === 'down') return 'red'
  return undefined
}

/** impact marker 优先级: 严重度优先, 同严重度比幅度 */
export function impactPriority(it: ImpactMarkerLike): number {
  const sev = SEVERITY_RANK[(it.impact_severity || '').toLowerCase()] ?? 1
  return sev * 1000 + Math.min(999, impactAbsPct(it) * 10)
}



/**
 * 挑出要画的 impact marker。
 *
 * 步骤: 时间窗 → 幅度门槛 → 同桶去重 → 同柱上限 → 全局上限(按优先级) → 按时序返回。
 */
export function pickImpactMarkers(
  items: ImpactMarkerLike[] | null | undefined,
  opts: MarkerFilterOptions = {},
): ImpactMarkerLike[] {
  if (!Array.isArray(items) || items.length === 0) return []
  const {
    align,
    minAbsPct = IMPACT_MIN_ABS_PCT,
    maxPerBar = IMPACT_MAX_PER_BAR,
    maxTotal = IMPACT_MAX_TOTAL,
    window,
  } = opts

  const bucket = (ts: number) => (align ? align(ts) : ts)

  const inWindow = items.filter((it) => {
    const ts = Number(it?.event_time ?? 0)
    if (!ts || !Number.isFinite(ts)) return false
    if (window && (ts < window.from || ts > window.to)) return false
    return true
  })

  // 幅度门槛: 有事件的条目即使幅度为 0 也保留 (那是"有消息但没动", 本身是信息);
  // 没有事件来源的纯价格异动必须过幅度门槛。
  const byAmp = inWindow.filter((it) => {
    const pct = impactAbsPct(it)
    if (pct >= minAbsPct) return true
    if (it.event_backed && (SEVERITY_RANK[(it.impact_severity || '').toLowerCase()] ?? 0) >= 3) {
      return true
    }
    return false
  })

  // 同桶去重 (同一事件被 worker 重复写入)
  const seen = new Set<string>()
  const deduped: ImpactMarkerLike[] = []
  for (const it of byAmp) {
    const key = `${bucket(Number(it.event_time))}::${(it.event_title || '').slice(0, 40)}`
    if (seen.has(key)) continue
    seen.add(key)
    deduped.push(it)
  }

  // 同柱上限: 同一根 K 线上只留优先级最高的 N 个
  const perBar = new Map<number, number>()
  const capped: ImpactMarkerLike[] = []
  const ordered = [...deduped].sort((a, b) => impactPriority(b) - impactPriority(a))
  for (const it of ordered) {
    const b = bucket(Number(it.event_time))
    const used = perBar.get(b) ?? 0
    if (used >= maxPerBar) continue
    perBar.set(b, used + 1)
    capped.push(it)
  }

  const total = capped.slice(0, Math.max(0, maxTotal))
  return total.sort((a, b) => Number(a.event_time) - Number(b.event_time))
}

/**
 * 挑出要画的异动圆点。
 *
 * 步骤: 时间窗 → 幅度/量能门槛 → 同类型冷却 → 同柱上限 → 按时序返回。
 */
export function pickAbnormalMarkers(
  items: AbnormalMarkerLike[] | null | undefined,
  opts: MarkerFilterOptions = {},
): AbnormalMarkerLike[] {
  if (!Array.isArray(items) || items.length === 0) return []
  const {
    align,
    minAbsPct = ABNORMAL_MIN_ABS_PCT,
    maxPerBar = ABNORMAL_MAX_PER_BAR,
    cooldownMs = ABNORMAL_COOLDOWN_MS,
    window,
  } = opts

  const bucket = (ts: number) => (align ? align(ts) : ts)

  const inWindow = items.filter((it) => {
    const ts = Number(it?.ts ?? 0)
    if (!ts || !Number.isFinite(ts)) return false
    if (window && (ts < window.from || ts > window.to)) return false
    return true
  })

  // 幅度/量能门槛: 涨跌幅够大, 或者是无涨跌幅但量能异常的纯量能异动
  const byAmp = inWindow.filter((it) => {
    const pct = it.pct_change == null ? null : Number(it.pct_change)
    if (pct != null && Number.isFinite(pct)) {
      return Math.abs(pct) >= minAbsPct
    }
    const vr = it.vol_ratio == null ? null : Number(it.vol_ratio)
    return vr != null && Number.isFinite(vr) && vr >= ABNORMAL_VOL_RATIO_MIN
  })

  // 同类型冷却: 同一 trigger_type 在 cooldownMs 内只保留第一个
  //  先按时间升序 —— 冷却必须以"时间上最早的那条"为准, 否则结果依赖入参顺序
  const byTime = [...byAmp].sort((a, b) => Number(a.ts) - Number(b.ts))
  const lastSeen = new Map<string, number>()
  const cooled: AbnormalMarkerLike[] = []
  for (const it of byTime) {
    const ts = Number(it.ts)
    const kind = (it.trigger_type || 'any').toString()
    const prev = lastSeen.get(kind)
    if (prev != null && ts - prev < cooldownMs) continue
    lastSeen.set(kind, ts)
    cooled.push(it)
  }

  // 同柱上限
  const perBar = new Map<number, number>()
  const capped: AbnormalMarkerLike[] = []
  for (const it of cooled) {
    const b = bucket(Number(it.ts))
    const used = perBar.get(b) ?? 0
    if (used >= maxPerBar) continue
    perBar.set(b, used + 1)
    capped.push(it)
  }

  return capped.sort((a, b) => Number(a.ts) - Number(b.ts))
}
