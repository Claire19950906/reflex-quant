// lib/risk-plan.ts — v412 风控档位的前端口径
//
// 后端 `/api/technical-state/risk` 返回的是**执行建议**（仓位系数 + 止损 ATR 倍数），
// 不是方向信号。状态层的方向读数已回测证否（v408~v411：命中率与"无脑做多"无可分离
// 差异），所以前端显示它时必须保持同样的克制：
//   - 档位只决定"仓位多大、止损多远"，不决定"做多还是做空"；
//   - 档位是**当前**读数（asof = 最后一根 K 线），不是决策当时的历史档位；
//   - 读数缺失时显示"没有读数"，不许补一个中性档位假装有数据。
import { apiGet } from './api'

export type RiskLevel = '低' | '中' | '高'

export type RiskProfile = {
  level: RiskLevel | null
  size_factor: number | null
  stop_atr_multiple: number | null
  atr_pct: number | null
  expected_adverse_pct: number | null
  drivers: string[]
  note: string | null
  score?: number | null
  /** v414：仓位口径来源 —— atr_band_daily（日线已验证）/ uniform_intraday（日内不缩仓） */
  size_basis?: string | null
  validated_timeframes?: string[]
  validation_note?: string | null
  /** v415：当前 ATR 相对近 60 根中位的倍数（波动贵不贵） */
  atr_vs_median?: number | null
  /** v415：等风险仓位参考（只对齐资金风险，不预测方向；未验证收益侧） */
  equal_risk_size?: number | null
  equal_risk_note?: string | null
  /** v417：止损距离（%），以及按单笔风险预算反推的名义敞口（%） */
  stop_pct?: number | null
  position_pct?: number | null
  position_pct_equal_risk?: number | null
  max_risk_pct?: number | null
  /** v418：该周期实测赔率（顺风中位÷逆风中位）与据此得到的最小预期空间 */
  odds_typical?: number | null
  require_space_pct?: number | null
  odds_sample?: string | null
  odds_note?: string | null
  /** v419：该周期赔率转正所需的最短持有根数，以及持有期的结论 */
  min_hold_bars?: number | null
  holding_note?: string | null
  /** v420：来回成本与该周期要求的最小空间，以及门槛依据（odds/adverse/cost） */
  round_trip_cost_pct?: number | null
  require_basis?: string | null
}

export type RiskSnapshot = {
  available: boolean
  asset?: string
  timeframe?: string
  /** 最后一根 K 线时间戳（毫秒），用于标注"读数时间" */
  asof?: number | null
  bars?: number | null
  /** 最新收盘价（后端给的最新 K 线 close），用于把止损倍数换成具体价格 */
  last_close?: number | null
  readout?: string
  bias?: number | null
  coverage?: number | null
  usable?: boolean
  risk?: RiskProfile | null
  error?: string
}

/** 档位 → 显示色：高=警示，中=注意，低=正常。 */
export function riskTone(level: RiskLevel | null | undefined): 'long' | 'warn' | 'short' | 'muted' {
  if (level === '高') return 'short'
  if (level === '中') return 'warn'
  if (level === '低') return 'long'
  return 'muted'
}

/** 仓位系数 → 人类可读的"建议仓位"（基准仓位 × 系数）。 */
export function describeSizeFactor(sizeFactor: number | null | undefined): string {
  if (sizeFactor === null || sizeFactor === undefined || Number.isNaN(sizeFactor)) return '—'
  if (sizeFactor >= 1) return `满仓（基准仓位的 ${sizeFactor.toFixed(1)}×）`
  const pct = Math.round(sizeFactor * 100)
  return `基准仓位的 ${pct}%`
}

/** 止损距离（%）＝ ATR% × 倍数。缺任何一项就返回 null，不猜。 */
export function stopDistancePct(
  atrPct: number | null | undefined,
  multiple: number | null | undefined,
): number | null {
  if (atrPct === null || atrPct === undefined) return null
  if (multiple === null || multiple === undefined) return null
  const value = Number(atrPct) * Number(multiple)
  if (!Number.isFinite(value)) return null
  return Math.round(value * 100) / 100
}

/**
 * 止损价：做多 = 入场 − 距离，做空 = 入场 + 距离。
 * 只在**给了入场价**时才算——没有价格就显示百分比，绝不假装算出一个绝对价。
 */
export function stopPriceFor(
  entryPrice: number | null | undefined,
  side: 'BUY' | 'SELL' | null | undefined,
  distancePct: number | null | undefined,
): number | null {
  if (!entryPrice || !Number.isFinite(Number(entryPrice))) return null
  if (!distancePct || !Number.isFinite(Number(distancePct))) return null
  if (!side || side === null) return null
  const move = (Number(entryPrice) * Number(distancePct)) / 100
  return side === 'BUY' ? Number(entryPrice) - move : Number(entryPrice) + move
}

export type RiskPlanView = {
  levelLabel: string
  sizeLabel: string
  stopLabel: string
  adverseLabel: string
  drivers: string[]
  note: string
  tone: 'long' | 'warn' | 'short' | 'muted'
  /** 是否可以直接当执行口径用（要有档位 + 止损倍数） */
  actionable: boolean
  /** 止损价：只有同时给了入场价与方向才算，否则 null（只显示百分比） */
  stopPrice: number | null
  /** 口径说明：日线已验证 / 日内只调止损不缩仓 */
  basisNote: string
  /** 等风险仓位参考（无数据时空串） */
  equalRiskNote: string
  /** 敞口说明：按 1% 单笔风险反推的下单比例（无数据时空串） */
  positionNote: string
  /** 赔率说明：这周期要赚多少才够本（无数据时空串） */
  oddsNote: string
  /** 持有期说明：这周期要不要拿久一点（无数据时空串） */
  holdingNote: string
  /** 门槛说明：这笔至少要走出多少才够本、依据是赔率还是成本（无数据时空串） */
  requireNote: string
}

/**
 * 把接口返回的档位翻译成一句能直接照着做的执行口径。
 * 缺数据时返回 null —— 调用方负责显示"没有读数"，不要在这里编默认档位。
 */
export function describeRiskPlan(
  snapshot: RiskSnapshot | null | undefined,
  entryPrice?: number | null,
  side?: 'BUY' | 'SELL' | null,
): RiskPlanView | null {
  const risk = snapshot?.risk
  if (!snapshot?.available || !risk || !risk.level) return null
  const stopPct = stopDistancePct(risk.atr_pct, risk.stop_atr_multiple)
  const stopLabel = risk.stop_atr_multiple
    ? `${risk.stop_atr_multiple} 倍 ATR${stopPct !== null ? `（≈${stopPct}%）` : ''}`
    : '—'
  const adverseLabel =
    risk.expected_adverse_pct !== null && risk.expected_adverse_pct !== undefined
      ? `≈${risk.expected_adverse_pct}%`
      : '—'
  const tf = snapshot.timeframe ? `${snapshot.timeframe} ` : ''
  return {
    levelLabel: `${risk.level}风险`,
    sizeLabel: describeSizeFactor(risk.size_factor),
    stopLabel,
    adverseLabel,
    drivers: risk.drivers ?? [],
    note:
      risk.note ??
      `${tf}档位：仓位 ${describeSizeFactor(risk.size_factor)}、止损 ${stopLabel}`,
    tone: riskTone(risk.level),
    actionable: Boolean(risk.stop_atr_multiple),
    stopPrice: stopPriceFor(entryPrice ?? snapshot.last_close, side, stopPct),
    basisNote: basisNoteOf(risk.size_basis, risk.validation_note),
    equalRiskNote: risk.equal_risk_note ?? '',
    positionNote: positionNoteOf(risk),
    oddsNote: risk.odds_note ?? '',
    holdingNote: risk.holding_note ?? '',
    requireNote: requireNoteOf(risk), 
  }
}

/** 把"这个口径在哪验证过"翻译成一句人话——不能让日内档位看起来也像验过。 */
export function basisNoteOf(sizeBasis?: string | null, validationNote?: string | null): string {
  if (sizeBasis === 'atr_band_daily') {
    return '口径：日线 ATR 绝对档位（已验证：XAU 3.54× / XAG 2.35× / BTC 1.72×）'
  }
  if (sizeBasis === 'uniform_intraday') {
    return '口径：日内只按 ATR 调止损、不缩仓位（4h 分档与逆风无关系，缩仓无依据）'
  }
  if (validationNote) return `口径：${validationNote}`
  return '口径：未标注验证范围'
}

/** 把"止损距离 → 名义敞口"翻成一句能直接照着下单的话。 */
export function positionNoteOf(risk: {
  stop_pct?: number | null
  position_pct?: number | null
  max_risk_pct?: number | null
  position_pct_equal_risk?: number | null
}): string {
  if (risk.stop_pct == null || risk.position_pct == null) return ''
  const budget = risk.max_risk_pct ?? 1
  let text = `敞口：止损 ${risk.stop_pct}%，按单笔亏 ${budget}% 本金反推 → 名义敞口约 ${risk.position_pct}%`
  if (risk.position_pct_equal_risk != null) {
    text += `（等风险口径 ${risk.position_pct_equal_risk}%）`
  }
  return text
}

const BASIS_LABEL: Record<string, string> = {
  odds: "赔率线",
  adverse: "逆风线",
  cost: "成本地板",
}

/** 把「要求空间 + 依据」拼成一句：这笔至少要走多远才不算白做。 */
export function requireNoteOf(risk: {
  require_space_pct?: number | null
  require_basis?: string | null
  round_trip_cost_pct?: number | null
}): string {
  if (risk.require_space_pct == null) return ""
  const basis = risk.require_basis ? BASIS_LABEL[risk.require_basis] : ""
  let text = `门槛：至少要走出 ${risk.require_space_pct}% 才够本`
  if (basis) text += `（${basis}`
  if (risk.round_trip_cost_pct != null) {
    text += basis ? `；来回成本 ${risk.round_trip_cost_pct}%）` : `（来回成本 ${risk.round_trip_cost_pct}%）`
  } else if (basis) {
    text += "）"
  }
  return text
}

export function fetchRiskSnapshot(asset: string, timeframe = '4h'): Promise<RiskSnapshot> {
  const qs = new URLSearchParams({ asset, timeframe })
  return apiGet<RiskSnapshot>(`/technical-state/risk?${qs.toString()}`)
}
