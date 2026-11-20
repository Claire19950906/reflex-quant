'use client'
/**
 * components/v237/MissedSignalsDimensionSummary.tsx — v302+ P0
 *
 * 复用后端 /api/missed-signals/by-dimension (v2026.10.15 已存在, 数据源 trade_reflections_v2,
 * 真实样本 ~163 条). 把"三维度错因"提到主视图:
 *   A 买卖信号判断  (long  色)  — 形态预期/心理预期未达标
 *   B 信息分析方向  (short 色)  — 逻辑框架错 / 因果链倒置
 *   C 技术错误      (默认)     — 数据缺失 / 信号慢 / 接口超时
 *
 * 设计:
 *   - compact: 3 卡片无子分类, 给 LiveDesk 顶部 strip 用
 *   - full:    3 卡片 + top-4 子分类 + desc, 给 attribution 顶部主视图用
 *   - 内部轮询 refreshMs, 不依赖父组件.
 *   - 失败态独立 (不抛错) — 不影响 v237 旧面板渲染.
 */

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { cn } from '@/lib/utils'
import { fetchWithFallback } from '@/lib/api'

type SubInfo = {
  n: number
  hit_rate?: number
  avg_confidence?: number
  avg_outcome_pct?: number
  /** 已结算样本占比（有真实未来涨跌的行 / 总行），<90% 时不该把均值当真 */
  outcome_coverage?: number
  /** v326 起后端按"这类错该怎么修"打的标:
   *  judgment=认知/判断问题 · rule_block=规则拦错 · unverified=历史标签无证据 · technical=数据/延迟 */
  kind?: 'judgment' | 'rule_block' | 'unverified' | 'technical' | string
}
type DimInfo = {
  label: string
  total: number
  hit: number
  hit_rate: number
  by_subcategory: Record<string, SubInfo>
  /** 漏检信号数（critical_price_moves 实时算），与 total（反思样本数）口径不同 */
  missed_total?: number
  /** 判断失误条数（认知/逻辑错） */
  judgment_total?: number
  /** 规则拦截条数（额度/闸门/连坐主动拦下，不是判断错） */
  rule_block_total?: number
  /** 未验证条数（历史贴的标签，但没有方向证据支撑） */
  unverified_total?: number
  /** 技术错误条数（数据缺失/信号慢/接口失败），不并入判断失误 */
  technical_total?: number
}
/** by_dimension 块里的一条 item (子分类挂在 subcategory 上) */
type DimItem = {
  asset?: string
  ts?: number
  subcategory?: string
  signal?: string
  pattern?: string
  ignored_reason?: string
}
type ByDimBlock = {
  label?: string
  n?: number
  items?: DimItem[]
  note?: string | null
}
type ByDimResp = {
  ok: boolean
  /** 旧结构 (trade_reflections_v2 派生) — 本 DB 为空, total=0 */
  dimensions?: { A?: DimInfo; B?: DimInfo; C?: DimInfo }
  /** 桥接真实结构 (critical_price_moves / fed_rate / feed_health 推导) — 有数据 */
  by_dimension?: { A_signal?: ByDimBlock; B_direction?: ByDimBlock; C_technical?: ByDimBlock }
  totals?: { A?: number; B?: number; C?: number; sum?: number; grand_total?: number }
  error?: string
}

// 小类中文名：后端返回的是 code（A1_strong_buy_missed…），
// 直接显示 code 让人没法快速锁定问题，这里统一映射成能读的话。
const SUB_LABELS: Record<string, string> = {
  A1_strong_buy_missed: '强买入信号漏检',
  A2_strong_sell_missed: '强卖出信号漏检',
  A3_continuation_missed: '持续性信号漏检',
  B1_macro_conflict: '宏观锚（利率/央行/通胀）判反',
  B2_event_misdirection: '事件方向解读反了',
  B3_chain_misuse: '因果链选错',
  B1_rate_conflict: '利率与行情反向',
  B2_rate_reversal_alone: '利率反转未传导',
  C1_feed_latency: '行情延迟',
  C2_feed_failure: '行情断流',
  // 后端 by-dimension 接口实际返回的代码（2026-11-13 实测）
  market_regime_misread: '市场状态误读',
  // v326: A 类细分 —— 把"该做的买卖被规则拦下"与"认知错"分开，便于直接定位
  pattern_recognition: '形态/信号识别错',
  psychological_anchor: '心理锚定(惯性/过度自信)',
  volume_price_divergence: '量价背离',
  risk_limit_blocked: '资产额度/集中度上限',
  batch_conflict_blocked: '同批次连坐拒单',
  strength_gate_blocked: '强度闸门拒单',
  news_direction_inverted: '消息方向判反',
  event_chain_break: '事件传导断链',
  cause_effect_reversed: '因果倒置',
  calibration_drift: '校准漂移',
  feed_missing: '数据缺失',
  signal_delay: '信号延迟',
  // v323: 反思表里历史贴的 B 标签，重算后无法用"真实方向 + 事后行情"证实，
  // 行本身保留，但统计上不再算作"信息方向判断错"。
  unverified_legacy_label: '历史标签·无方向证据',
}

const DIM_META: Record<'A' | 'B' | 'C', { label: string; color: string; desc: string }> = {
  A: {
    label: '买卖信号判断',
    color: 'text-long',
    desc: '对市场还不够了解 · 心理预期计算与形态预期未达标',
  },
  B: {
    label: '信息分析方向',
    color: 'text-short',
    desc: '方向错误 · 逻辑框架错了 · 因果链倒置',
  },
  C: {
    label: '技术错误',
    color: 'text-foreground',
    desc: '数据缺失 · 信号慢 · 接口超时 · 计算异常',
  },
}

const ORDER: Array<'A' | 'B' | 'C'> = ['A', 'B', 'C']

/** by_dimension 块的键名映射 */
const BLOCK_KEY: Record<'A' | 'B' | 'C', 'A_signal' | 'B_direction' | 'C_technical'> = {
  A: 'A_signal',
  B: 'B_direction',
  C: 'C_technical',
}

/**
 * 适配后端两种 shape:
 *   - dimensions.{A,B,C}      (trade_reflections_v2 派生) —— 本 DB 为空, total=0
 *   - by_dimension.A_signal 等 (critical_price_moves / fed_rate / feed_health 派生) —— 真实有数据
 *
 * v2026.10.24 fix: 之前只读 dimensions, 归因页与实时信号台的「三维度错因」卡片恒显示
 * n=0 / 无数据 (旁边 Banner 读 by_dimension 所以显示正常)。这里两套都兼容,
 * 优先用有真实数据的那套, 子分类计数从 items[].subcategory 聚合。
 */
function adaptDims(resp: ByDimResp | null): {
  dims: Record<'A' | 'B' | 'C', DimInfo>
  total: number
} {
  const out = {} as Record<'A' | 'B' | 'C', DimInfo>
  const blocks = resp?.by_dimension ?? {}
  for (const k of ORDER) {
    const legacy = resp?.dimensions?.[k]
    const blk0 = blocks[BLOCK_KEY[k]]
    if (legacy && legacy.total > 0) {
      // 漏检条数始终取 by_dimension（critical_price_moves 实时算），
      // 反思样本数与命中率取 legacy（trade_reflections_v2）。
      // 两个口径必须同时出现在同一张卡上 —— 否则"n=36"和"n=362"会被当成矛盾数据。
      out[k] = {
        ...legacy,
        missed_total: blk0?.n ?? legacy.missed_total ?? 0,
      }
      continue
    }
    const blk = blocks[BLOCK_KEY[k]]
    const items: DimItem[] = blk?.items ?? []
    const bySub: Record<string, SubInfo> = {}
    for (const it of items) {
      const sub = it.subcategory ?? 'unknown'
      bySub[sub] = { n: (bySub[sub]?.n ?? 0) + 1 }
    }
    out[k] = {
      label: blk?.label ?? DIM_META[k].label,
      total: blk?.n ?? items.length,
      hit: 0,
      hit_rate: 0,
      by_subcategory: bySub,
    }
  }
  const total = ORDER.reduce((s, k) => s + (out[k]?.total ?? 0), 0)
  return { dims: out, total }
}

type Variant = 'compact' | 'full'

export function MissedSignalsDimensionSummary({
  className,
  variant = 'full',
  refreshMs = 60_000,
  // v308 P1-fix3: 原来 days 硬编码在 fetch URL 里, 标题的 "90d" 又是另一个
  // 写死的字符串 —— 改窗口时数字和标签会脱节。提为 prop, 两者共用一个来源。
  days = 90,
}: {
  className?: string
  variant?: Variant
  refreshMs?: number
  days?: number
}) {
  const [data, setData] = useState<ByDimResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const fetchIt = async () => {
      try {
        // v2026.10.19 P0 fix: 去掉 /api 前缀 (fetchWithFallback base 已有 /api)
        const j: ByDimResp = await fetchWithFallback(
          `/missed-signals/by-dimension?days=${days}`,
        )
        if (cancelled) return
        setData(j)
        if (j?.ok) {
          setErr(null)
        } else {
          setErr(j?.error ?? 'unknown error')
        }
      } catch (e) {
        if (cancelled) return
        setErr(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void fetchIt()
    const id = setInterval(fetchIt, refreshMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [refreshMs, days])

  if (loading && !data) {
    return (
      <div
        className={cn(
          'inline-flex w-fit items-center gap-1 rounded border border-muted-foreground/25 bg-card/60 px-2 py-0.5 font-mono text-[11px] text-muted-foreground',
          className,
        )}
      >
        正在加载三维度反思数据
      </div>
    )
  }
  if (err && !data?.ok) {
    return (
      // v362 降噪：整块红底 → 一行灰点 + 一句结论 + 原始错误入 title，60s 自动重试
      <div
        title={`by-dimension 错误: ${err}`}
        className={cn(
          'flex items-center gap-2 rounded border border-muted-foreground/25 bg-card/60 px-3 py-1.5 font-mono text-[11px] text-muted-foreground',
          className,
        )}
      >
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500/70" aria-hidden />
        <span>
          三维度统计暂不可用
          <span className="text-muted-foreground/60"> · {days}d 内 A 漏检 / B 方向 / C 技术</span>
        </span>
        <span className="text-muted-foreground/50">· 60s 自动重试</span>
      </div>
    )
  }

  const { dims, total } = adaptDims(data)
  const isCompact = variant === 'compact'
  // 漏检口径合计 = 三维度 missed_total 之和（原来由 ByDimBanner 单独展示，
  // 合并后在这里统一算，页面上只留一个"数字来源"）。
  const missedSum = ORDER.reduce((s, k) => s + (dims[k]?.missed_total ?? 0), 0)
  const topMissed = ORDER
    .map((k) => ({ k, n: dims[k]?.missed_total ?? 0 }))
    .sort((a, b) => b.n - a.n)[0]

  return (
    <div className={cn('rounded border border-muted-foreground/30 bg-card p-3', className)}>
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-baseline gap-2">
          <h3 className="font-mono text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            三维度错因 (A 漏检 / B 方向 / C 技术)
          </h3>
          <span className="font-mono text-[11px] text-muted-foreground/60">
            漏检 {missedSum} · 反思样本 {total} · {days}d
          </span>
        </div>
        <Link
          href="/missed-signals"
          className="font-mono text-[11px] text-muted-foreground underline hover:text-foreground"
        >
          {isCompact ? '详细 →' : '打开完整漏检 →'}
        </Link>
      </div>
      {missedSum > 0 && topMissed ? (
        <p className="mb-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
          <span className={cn('font-semibold', DIM_META[topMissed.k].color)}>
            {topMissed.k} · {DIM_META[topMissed.k].label}
          </span>
          {' '}占漏检 {Math.round((topMissed.n / missedSum) * 100)}%（{topMissed.n}/{missedSum}）
          —— 两个口径同源：漏检=该抓没抓的行情条数，反思=策略自省样本数，命中率只在反思样本里算。
        </p>
      ) : null}
      <div className={cn('grid gap-2', isCompact ? 'grid-cols-3' : 'grid-cols-1 sm:grid-cols-3')}>
        {ORDER.map((k) => {
          const d = dims[k]
          const meta = DIM_META[k]
          if (!d || d.total === 0) {
            return (
              <div key={k} className="rounded border border-muted-foreground/20 bg-muted/10 p-2">
                <div className={cn('font-mono text-[11px] font-semibold', meta.color)}>
                  {k}. {meta.label}
                </div>
                <div className="mt-1 font-mono text-[11px] text-muted-foreground/60">无数据</div>
              </div>
            )
          }
          const subs = d.by_subcategory || {}
          // v335: 小类**全量**展示, 按样本量排序。
          // 原来固定截 4 个 (compact 甚至截到 0), 而 A 类实有 5 个小类 ——
          // "细分小类方便快速锁定问题"的要求被一个 slice(0,4) 卡住了。
          const allSubs = Object.entries(subs)
            .sort((a, b) => (b[1]?.n ?? 0) - (a[1]?.n ?? 0))
          const topSubs = isCompact ? allSubs.slice(0, 3) : allSubs
          return (
            <div key={k} className="rounded border border-muted-foreground/20 bg-muted/10 p-2">
              <div className="flex items-center justify-between">
                <div className={cn('font-mono text-[11px] font-semibold', meta.color)}>
                  {k}. {meta.label}
                </div>
                <div className="font-mono text-[11px] text-muted-foreground">
                  漏检 {d.missed_total ?? 0} · 反思 {d.total} · 命中{' '}
                  {d.hit_rate > 0 ? `${Math.round(d.hit_rate * 100)}%` : '—'}
                  {(d.rule_block_total ?? 0) > 0 ? (
                    <span className="ml-1 text-muted-foreground/70">
                      · 规则拦截{d.rule_block_total}
                    </span>
                  ) : null}
                  {(d.unverified_total ?? 0) > 0 ? (
                    <span className="ml-1 text-warning/70">· 未验证{d.unverified_total}</span>
                  ) : null}
                  {(d.technical_total ?? 0) > 0 ? (
                    <span className="ml-1 text-muted-foreground/70">
                      · 技术错误{d.technical_total}
                    </span>
                  ) : null}
                </div>
              </div>
              {!isCompact ? (
                <div className="mt-0.5 font-mono text-[11px] text-muted-foreground/70">{meta.desc}</div>
              ) : null}
              {topSubs.length > 0 ? (
                <div className="mt-1 flex flex-col gap-0.5 font-mono text-[11px]">
                  {topSubs.map(([sub, info]) => (
                    <div key={sub} className="flex items-center justify-between gap-2">
                      <span className="truncate text-foreground/80" title={sub}>
                        {SUB_LABELS[sub] ?? sub}
                        {/* kind 决定这条错因该怎么修:
                            judgment=认知/判断问题 · rule_block=规则拦错 ·
                            unverified=历史标签无方向证据 · technical=数据/延迟 */}
                        {info?.kind && info.kind !== 'judgment' ? (
                          <span
                            className={cn(
                              'ml-1 rounded px-1 text-[11px]',
                              info.kind === 'rule_block' && 'bg-warning/15 text-warning',
                              info.kind === 'unverified' && 'bg-muted text-muted-foreground',
                              info.kind === 'technical' && 'bg-short/15 text-short',
                            )}
                          >
                            {info.kind === 'rule_block'
                              ? '规则拦截'
                              : info.kind === 'unverified'
                                ? '未验证'
                                : '技术'}
                          </span>
                        ) : null}
                        {/* 覆盖率不足时明确标出来：均值/命中率建立在少量已结算样本上 */}
                        {typeof info.outcome_coverage === 'number' && info.outcome_coverage < 0.9 ? (
                          <span className="ml-1 text-warning/80">
                            · 仅{Math.round(info.outcome_coverage * 100)}%已验证
                          </span>
                        ) : null}
                      </span>
                      <span className="flex shrink-0 items-center gap-1 text-muted-foreground">
                        {/* v335: 小类命中率一起给 —— 只看条数看不出"这类错有多严重" */}
                        {typeof info?.hit_rate === 'number' ? (
                          <span
                            className={cn(
                              info.hit_rate >= 0.5 ? 'text-long/80' : 'text-short/80',
                            )}
                          >
                            {Math.round(info.hit_rate * 100)}%
                          </span>
                        ) : null}
                        <span>{info?.n ?? 0}</span>
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

