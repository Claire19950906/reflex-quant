'use client'

import { useEffect, useRef, useState } from 'react'
import { API_BASE } from '@/lib/api'
import {
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useIsDark } from '@/hooks/use-is-dark'

// 资金流动页图表 —— 用快照数据画「流向 / 集中 / 杠杆占比」动态仪表盘。
// 数据形态与 flows-panel 的 MacroItem 兼容（category / source / metric_key / value /
// unit / status / payload{label,longAccount,shortAccount,...}）。
export type FlowMetric = {
  ts?: number
  category: string
  source: string
  metric_key: string
  value: number | null
  unit: string
  status: string
  payload: { label?: string; note?: string; [k: string]: unknown }
}

const ASSET_ORDER = ['BTC', 'ETH', 'SOL', 'XAU', 'WTI']

function assetRank(label: string): number {
  const i = ASSET_ORDER.indexOf(label)
  return i === -1 ? 99 : i
}

function assetOf(it: FlowMetric): string {
  const label = (it.payload?.label as string | undefined)?.trim()
  if (label) return label
  const k = it.metric_key
  return k.includes('.') ? k.slice(0, k.lastIndexOf('.')) : k
}

// 提取某分类下、以 `.suffix` 结尾的生效指标（value 非空、status ok），按资产顺序排序。
function pickSuffix(items: FlowMetric[], category: string, suffix: string): FlowMetric[] {
  return items
    .filter((it) => it.category === category && it.status === 'ok' && it.value != null)
    .filter((it) => it.metric_key.endsWith(`.${suffix}`))
    .sort((a, b) => assetRank(assetOf(a)) - assetRank(assetOf(b)))
}

function fmtPct(v: number): string {
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

function fmtRatioPct(frac: number): string {
  return `${frac.toFixed(0)}%`
}

type Palette = { up: string; down: string; flat: string; bg: string; grid: string }

function theme(isDark: boolean): Palette {
  return {
    up: isDark ? '#2ebd85' : '#00b061',
    down: isDark ? '#f6465d' : '#ef4444',
    flat: isDark ? '#9aa3af' : '#64748b',
    bg: isDark ? '#334155' : '#e2e8f0',
    grid: isDark ? '#475569' : '#cbd5e1',
  }
}

export function ChartCard(props: {
  title: string
  subtitle?: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={`surface-card p-3 ${props.className ?? ''}`}>
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">{props.title}</h3>
      </div>
      {props.subtitle && (
        <p className="mb-2 font-mono text-[10px] text-muted-foreground/80">{props.subtitle}</p>
      )}
      {props.children}
    </section>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="py-6 text-center font-mono text-xs text-muted-foreground">{text}</div>
}

// ── 1) 资金流向 · CVD 净买盘（横向正负柱，中心为 0）──────────────────────────
export function CvdFlowChart({ items }: { items: FlowMetric[] }) {
  const isDark = useIsDark()
  const p = theme(isDark)
  const rows = pickSuffix(items, 'flow', 'cvd')
  if (rows.length === 0) return <Empty text="暂无 CVD 数据（XAU/WTI 无免费 taker 源）" />
  const maxAbs = Math.max(1e-9, ...rows.map((r) => Math.abs(r.value as number)))

  return (
    <div className="space-y-2">
      {rows.map((r) => {
        const v = r.value as number
        const color = v >= 0 ? p.up : p.down
        const widthPct = (Math.abs(v) / maxAbs) * 46
        const left = v >= 0 ? '50%' : `${50 - widthPct}%`
        return (
          <div key={r.metric_key} className="grid grid-cols-[40px_1fr_64px] items-center gap-2">
            <span className="truncate font-mono text-[10px] font-semibold text-foreground/80">{assetOf(r)}</span>
            <div
              className="relative h-4 overflow-hidden rounded-sm"
              style={{ backgroundColor: p.bg }}
              title={`${assetOf(r)} CVD ${fmtPct(v)}`}
            >
              <div className="absolute inset-y-0 w-px" style={{ left: '50%', backgroundColor: p.grid }} />
              <div
                className="absolute inset-y-0 rounded-sm"
                style={{ left, width: `${widthPct}%`, backgroundColor: color }}
              />
            </div>
            <span className="text-right font-mono text-[10px]" style={{ color }}>
              {fmtPct(v)}
            </span>
          </div>
        )
      })}
      <p className="pt-1 font-mono text-[9px] leading-relaxed text-muted-foreground/70">
        正=净流入/多方主动买入 · 负=净流出/空方主动卖出（近 1h）
      </p>
    </div>
  )
}

// ── 2) 资金费率 · 杠杆成本（横向正负柱）──────────────────────────────────────
export function FundingStrip({ items }: { items: FlowMetric[] }) {
  const isDark = useIsDark()
  const p = theme(isDark)
  const rows = pickSuffix(items, 'structure', 'funding')
  if (rows.length === 0) return <Empty text="暂无资金费率数据" />
  const maxAbs = Math.max(1e-9, ...rows.map((r) => Math.abs(r.value as number)))

  return (
    <div className="space-y-2">
      {rows.map((r) => {
        const v = r.value as number
        const color = v >= 0 ? p.up : p.down
        const widthPct = (Math.abs(v) / maxAbs) * 46
        const left = v >= 0 ? '50%' : `${50 - widthPct}%`
        return (
          <div key={r.metric_key} className="grid grid-cols-[40px_1fr_64px] items-center gap-2">
            <span className="truncate font-mono text-[10px] font-semibold text-foreground/80">{assetOf(r)}</span>
            <div
              className="relative h-4 overflow-hidden rounded-sm"
              style={{ backgroundColor: p.bg }}
              title={`${assetOf(r)} funding ${fmtPct(v)}`}
            >
              <div className="absolute inset-y-0 w-px" style={{ left: '50%', backgroundColor: p.grid }} />
              <div
                className="absolute inset-y-0 rounded-sm"
                style={{ left, width: `${widthPct}%`, backgroundColor: color }}
              />
            </div>
            <span className="text-right font-mono text-[10px]" style={{ color }}>
              {fmtPct(v)}
            </span>
          </div>
        )
      })}
      <p className="pt-1 font-mono text-[9px] leading-relaxed text-muted-foreground/70">
        &gt;0 多头付空头（多头拥挤）· &lt;0 空头付多头（空头拥挤）；跨所分歧=资金面信号
      </p>
    </div>
  )
}

// ── 3) 杠杆多空占比 · 账户情绪（堆叠条）─────────────────────────────────────
export function LeverageStack({ items }: { items: FlowMetric[] }) {
  const isDark = useIsDark()
  const p = theme(isDark)
  const rows = pickSuffix(items, 'structure', 'ls_ratio')
  if (rows.length === 0) return <Empty text="暂无多空账户比数据" />

  return (
    <div className="space-y-2.5">
      {rows.map((r) => {
        const long = r.payload?.longAccount as number | undefined | null
        const short = r.payload?.shortAccount as number | undefined | null
        let longPct = 50
        if (long != null && short != null && long + short > 0) {
          longPct = (long / (long + short)) * 100
        } else {
          const ratio = r.value as number
          longPct = (ratio / (1 + ratio)) * 100
        }
        const shortPct = 100 - longPct
        return (
          <div key={r.metric_key}>
            <div className="mb-0.5 flex items-center justify-between gap-2">
              <span className="font-mono text-[10px] font-semibold text-foreground/80">{assetOf(r)}</span>
              <span className="font-mono text-[10px] text-muted-foreground">
                多 <span className="text-long">{fmtRatioPct(longPct)}</span> · 空{' '}
                <span className="text-short">{fmtRatioPct(shortPct)}</span>
              </span>
            </div>
            <div
              className="flex h-2.5 w-full overflow-hidden rounded-full"
              style={{ backgroundColor: p.down }}
              title={`${assetOf(r)} 多头 ${longPct.toFixed(1)}% / 空头 ${shortPct.toFixed(1)}%`}
            >
              <div style={{ width: `${longPct}%`, backgroundColor: p.up }} />
            </div>
          </div>
        )
      })}
      <p className="pt-1 font-mono text-[9px] leading-relaxed text-muted-foreground/70">
        币安全体账户多空比（1h）；黄金为 CFTC 投机多空比替代
      </p>
    </div>
  )
}

function fmtBig(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return String(Math.round(v))
}

const ASSET_COLORS: Record<string, string> = {
  BTC: '#f7931a',
  ETH: '#627eea',
  SOL: '#14f195',
  XAU: '#f59e0b',
  WTI: '#38bdf8',
}

function colorOf(label: string): string {
  return ASSET_COLORS[label] ?? '#94a3b8'
}

// ── 4) 持仓集中度 · OI 分布（环图）──────────────────────────────────────────
export function OiComposition({ items }: { items: FlowMetric[] }) {
  const isDark = useIsDark()
  const p = theme(isDark)
  const rows = pickSuffix(items, 'structure', 'oi')
  if (rows.length === 0) return <Empty text="暂无持仓量数据" />
  const total = rows.reduce((a, r) => a + Math.max(0, r.value as number), 0)

  const R = 40
  const C = 2 * Math.PI * R
  let acc = 0
  const segs = rows.map((r) => {
    const v = Math.max(0, r.value as number)
    const frac = total > 0 ? v / total : 0
    const seg = { label: assetOf(r), value: v, frac, color: colorOf(assetOf(r)) }
    acc += frac
    return seg
  })

  return (
    <div className="flex items-center gap-4">
      <div className="relative h-28 w-28 shrink-0">
        <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
          <circle cx={50} cy={50} r={R} fill="none" stroke={p.bg} strokeWidth={15} />
          {segs.map((s, i) => {
            const dash = s.frac * C
            const prev = segs.slice(0, i).reduce((a, x) => a + x.frac, 0)
            const offset = -(prev * C)
            return (
              <circle
                key={s.label}
                cx={50}
                cy={50}
                r={R}
                fill="none"
                stroke={s.color}
                strokeWidth={15}
                strokeDasharray={`${dash} ${C - dash}`}
                strokeDashoffset={offset}
              />
            )
          })}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="font-mono text-xs font-bold text-foreground">{rows.length} 类</span>
          <span className="font-mono text-[8px] text-muted-foreground">持仓分布</span>
        </div>
      </div>
      <ul className="min-w-0 flex-1 space-y-1">
        {segs.map((s) => (
          <li key={s.label} className="flex items-center gap-2 font-mono text-[10px]">
            <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: s.color }} />
            <span className="text-foreground/80">{s.label}</span>
            <span className="ml-auto shrink-0 text-muted-foreground">
              {fmtBig(s.value)} · {(s.frac * 100).toFixed(0)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ── 5) 各金融品类资金构成 · COT 投机净头寸（环图，按品类聚合，净多绿/净空红）──
const COT_GROUP_ORDER = ['贵金属', '能源', '股指', '债券利率', '外汇', '加密']

export function CotComposition({ items }: { items: FlowMetric[] }) {
  const isDark = useIsDark()
  const p = theme(isDark)
  const netRows = pickSuffix(items, 'cot', 'cot_noncomm_net')
  if (netRows.length === 0) return <Empty text="暂无 CFTC 各品类机构持仓数据" />

  const chgMap = new Map<string, number>()
  for (const r of pickSuffix(items, 'cot', 'cot_noncomm_chg')) {
    const g = (r.payload?.group as string) || '其它'
    chgMap.set(g, (chgMap.get(g) ?? 0) + (r.value as number))
  }
  const groups = new Map<string, { long: number; short: number; net: number }>()
  for (const r of netRows) {
    const g = (r.payload?.group as string) || '其它'
    const v = r.value as number
    const e = groups.get(g) ?? { long: 0, short: 0, net: 0 }
    if (v >= 0) e.long += v
    else e.short += -v
    e.net += v
    groups.set(g, e)
  }
  const segs = Array.from(groups.entries())
    .map(([label, e]) => ({ ...e, label, chg: chgMap.get(label) ?? 0 }))
    .sort((a, b) => {
      const ia = COT_GROUP_ORDER.indexOf(a.label)
      const ib = COT_GROUP_ORDER.indexOf(b.label)
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
    })
  const total = segs.reduce((a, s) => a + s.long + s.short, 0)

  const R = 40
  const C = 2 * Math.PI * R
  const arcs = segs.map((s) => {
    const abs = s.long + s.short
    return { ...s, abs, frac: total > 0 ? abs / total : 0, color: s.net >= 0 ? p.up : p.down }
  })

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-4">
        <div className="relative h-28 w-28 shrink-0">
          <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
            <circle cx={50} cy={50} r={R} fill="none" stroke={p.bg} strokeWidth={15} />
            {arcs.map((a, i) => {
              const dash = a.frac * C
              const prev = arcs.slice(0, i).reduce((x, y) => x + y.frac, 0)
              const offset = -(prev * C)
              return (
                <circle
                  key={a.label}
                  cx={50}
                  cy={50}
                  r={R}
                  fill="none"
                  stroke={a.color}
                  strokeWidth={15}
                  strokeDasharray={`${dash} ${C - dash}`}
                  strokeDashoffset={offset}
                />
              )
            })}
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className="font-mono text-xs font-bold text-foreground">{arcs.length} 类</span>
            <span className="font-mono text-[8px] text-muted-foreground">品类规模</span>
          </div>
        </div>
        <ul className="min-w-0 flex-1 space-y-1">
          {arcs.map((a) => (
            <li key={a.label} className="flex items-center gap-2 font-mono text-[10px]">
              <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: a.color }} />
              <span className="text-foreground/80">{a.label}</span>
              <span className="ml-auto shrink-0 text-muted-foreground">
                {fmtBig(a.abs)} · {(a.frac * 100).toFixed(0)}%
                {a.chg !== 0 && (
                  <span className={a.chg >= 0 ? 'text-long' : 'text-short'}>
                    {' '}{a.chg >= 0 ? '▲' : '▼'}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      </div>
      <p className="pt-1 font-mono text-[9px] leading-relaxed text-muted-foreground/70">
        绿=投机净多 · 红=投机净空；▲/▼=本周净头寸变化（机构资金轮动方向）
      </p>
    </div>
  )
}

// ── 6) 杠杆资金 · 美股保证金债务（margin debt，季度滞后）+ 加密合约 OI ──
function fmtUsdBig(v: number): string {
  const a = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (a >= 1e12) return `${sign}$${(a / 1e12).toFixed(2)}T`
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`
  return `${sign}$${Math.round(a).toLocaleString()}`
}

export function MarginDebtPanel({ items }: { items: FlowMetric[] }) {
  const isDark = useIsDark()
  const p = theme(isDark)
  const debt = items.find((it) => it.metric_key === 'margin_debt')
  const oiRows = pickSuffix(items, 'structure', 'oi').slice(0, 5)

  const value = debt?.status === 'ok' && debt.value != null ? (debt.value as number) : null
  const qoq = (debt?.payload?.qoq as number | undefined | null) ?? null
  const yoy = (debt?.payload?.yoy as number | undefined | null) ?? null
  const date = (debt?.payload?.observation_date as string | undefined) || ''
  const tone = (v: number | null | undefined): string =>
    v == null ? 'text-muted-foreground' : v >= 0 ? 'text-long' : 'text-short'
  const maxOi = Math.max(...oiRows.map((x) => Math.max(0, x.value as number)), 1e-9)

  return (
    <div className="space-y-3">
      {value != null ? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-mono text-2xl font-bold text-foreground">{fmtUsdBig(value * 1e6)}</span>
            {date && <span className="font-mono text-[10px] text-muted-foreground">截至 {date}</span>}
          </div>
          <div className="flex gap-4 font-mono text-[11px]">
            <span className={tone(qoq)}>
              环比 {qoq != null ? `${qoq >= 0 ? '+' : ''}${qoq.toFixed(1)}%` : 'N/A'}
            </span>
            <span className={tone(yoy)}>
              同比 {yoy != null ? `${yoy >= 0 ? '+' : ''}${yoy.toFixed(1)}%` : 'N/A'}
            </span>
          </div>
        </>
      ) : (
        <Empty text="暂无美股保证金债务数据" />
      )}
      <div className="border-t border-border/50 pt-2">
        <p className="mb-1.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-primary/80">
          加密合约杠杆敞口（OI）
        </p>
        <div className="space-y-1.5">
          {oiRows.length === 0 && <div className="font-mono text-[10px] text-muted-foreground">暂无 OI 数据</div>}
          {oiRows.map((r) => {
            const v = Math.max(0, r.value as number)
            return (
              <div key={r.metric_key} className="grid grid-cols-[40px_1fr_64px] items-center gap-2">
                <span className="font-mono text-[10px] font-semibold text-foreground/80">{assetOf(r)}</span>
                <div className="relative h-3 overflow-hidden rounded-sm" style={{ backgroundColor: p.bg }}>
                  <div
                    className="absolute inset-y-0 rounded-sm"
                    style={{ width: `${(v / maxOi) * 100}%`, backgroundColor: colorOf(assetOf(r)) }}
                  />
                </div>
                <span className="text-right font-mono text-[10px] text-muted-foreground">{fmtBig(v)}</span>
              </div>
            )
          })}
        </div>
      </div>
      <p className="pt-1 font-mono text-[9px] leading-relaxed text-muted-foreground/70">
        美股保证金债务=杠杆资金总量（季度滞后·官方）；加密 OI=永续未平仓合约（近似杠杆敞口）
      </p>
    </div>
  )
}

// ── 7) 加密现货资金构成：BTC / ETH / 山寨+稳定币（环图 + 绝对市值）────────
export function CryptoSpotMap({ items }: { items: FlowMetric[] }) {
  const isDark = useIsDark()
  const p = theme(isDark)
  const mcap = items.find((it) => it.metric_key === 'crypto.total_mcap' && it.status === 'ok')
  const btc = items.find((it) => it.metric_key === 'crypto.btc_dominance' && it.status === 'ok')
  const eth = items.find((it) => it.metric_key === 'crypto.eth_dominance' && it.status === 'ok')
  const total = mcap?.value as number | null ?? null
  if (total == null) return <Empty text="暂无加密总市值数据（CoinPaprika）" />
  const b = (btc?.value as number | null) ?? 0
  const e = (eth?.value as number | null) ?? 0
  const alt = Math.max(0, 100 - b - e)
  const segs = [
    { label: 'BTC', pct: b, color: colorOf('BTC') },
    { label: 'ETH', pct: e, color: colorOf('ETH') },
    { label: '山寨+稳定币', pct: alt, color: '#a78bfa' },
  ].filter((s) => s.pct > 0.01)

  const R = 40
  const C = 2 * Math.PI * R
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-4">
        <div className="relative h-28 w-28 shrink-0">
          <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
            <circle cx={50} cy={50} r={R} fill="none" stroke={p.bg} strokeWidth={15} />
            {segs.map((s, i) => {
              const frac = s.pct / 100
              const dash = frac * C
              const prev = segs.slice(0, i).reduce((x, y) => x + y.pct, 0) / 100
              return (
                <circle
                  key={s.label}
                  cx={50}
                  cy={50}
                  r={R}
                  fill="none"
                  stroke={s.color}
                  strokeWidth={15}
                  strokeDasharray={`${dash} ${C - dash}`}
                  strokeDashoffset={-(prev * C)}
                />
              )
            })}
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className="font-mono text-[11px] font-bold text-foreground">{fmtUsdBig(total)}</span>
            <span className="font-mono text-[8px] text-muted-foreground">加密总市值</span>
          </div>
        </div>
        <ul className="min-w-0 flex-1 space-y-1">
          {segs.map((s) => (
            <li key={s.label} className="flex items-center gap-2 font-mono text-[10px]">
              <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: s.color }} />
              <span className="text-foreground/80">{s.label}</span>
              <span className="ml-auto shrink-0 text-muted-foreground">
                {fmtUsdBig((total * s.pct) / 100)} · {s.pct.toFixed(1)}%
              </span>
            </li>
          ))}
        </ul>
      </div>
      <p className="pt-1 font-mono text-[9px] leading-relaxed text-muted-foreground/70">
        BTC 市占率(CoinPaprika) · ETH(CoinGecko)；「山寨+稳定币」= 余量（含 USDT/USDC 等稳定币与其余小币）
      </p>
    </div>
  )
}

// ── 8) 杠杆 vs 现货占比（美股、加密两市场分开，各自杠杆资金 / 现货市值）──
function LeverageBar(props: {
  title: string
  lev: number | null
  spot: number | null
  levLabel: string
  spotLabel: string
  levColor: string
  spotColor: string
}) {
  const { title, lev, spot, levLabel, spotLabel, levColor, spotColor } = props
  const lv = lev ?? 0
  const sp = spot ?? 0
  if (sp <= 0 && lv <= 0) {
    return <div className="rounded border border-border/60 p-2 font-mono text-[10px] text-muted-foreground">{title}：暂无数据</div>
  }
  const total = lv + sp
  const ratio = sp > 0 ? (lv / sp) * 100 : null
  return (
    <div className="rounded border border-border bg-card p-2">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="font-mono text-[11px] font-semibold text-foreground">{title}</span>
        <span className="font-mono text-[10px] text-foreground/80">
          {ratio != null ? `杠杆/现货 ${ratio.toFixed(2)}%` : '—'}
        </span>
      </div>
      <div className="flex h-5 w-full overflow-hidden rounded">
        {sp > 0 && <div style={{ width: `${(sp / total) * 100}%`, backgroundColor: spotColor }} title={`${spotLabel}: ${fmtUsdBig(sp)}`} />}
        {lv > 0 && <div style={{ width: `${(lv / total) * 100}%`, backgroundColor: levColor }} title={`${levLabel}: ${fmtUsdBig(lv)}`} />}
      </div>
      <ul className="mt-1.5 space-y-0.5">
        <li className="flex items-center gap-2 font-mono text-[9px]">
          <span className="h-1.5 w-1.5 shrink-0 rounded-sm" style={{ backgroundColor: spotColor }} />
          <span className="text-foreground/70">{spotLabel}</span>
          <span className="ml-auto shrink-0 text-muted-foreground">
            {sp > 0 ? `${fmtUsdBig(sp)} · ${((sp / total) * 100).toFixed(1)}%` : 'N/A'}
          </span>
        </li>
        <li className="flex items-center gap-2 font-mono text-[9px]">
          <span className="h-1.5 w-1.5 shrink-0 rounded-sm" style={{ backgroundColor: levColor }} />
          <span className="text-foreground/70">{levLabel}</span>
          <span className="ml-auto shrink-0 text-muted-foreground">
            {lv > 0 ? `${fmtUsdBig(lv)} · ${((lv / total) * 100).toFixed(1)}%` : 'N/A'}
          </span>
        </li>
      </ul>
    </div>
  )
}

export function LeverageSplit({ items }: { items: FlowMetric[] }) {
  const mcap = items.find((it) => it.metric_key === 'crypto.total_mcap' && it.status === 'ok')
  const debt = items.find((it) => it.metric_key === 'margin_debt' && it.status === 'ok')
  const eqMcap = items.find((it) => it.metric_key === 'us_equity_mcap' && it.status === 'ok')
  const oiRows = pickSuffix(items, 'structure', 'oi_usd_bybit')

  const cryptoSpot = (mcap?.value as number | null) ?? null
  const cryptoLev = oiRows.reduce((a, r) => a + Math.max(0, r.value as number), 0)
  const usLev = debt?.status === 'ok' && debt.value != null ? (debt.value as number) * 1e6 : null
  const usSpot = eqMcap?.status === 'ok' && eqMcap.value != null ? (eqMcap.value as number) * 1e6 : null

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <LeverageBar
        title="美股"
        lev={usLev}
        spot={usSpot}
        levLabel="保证金债务(杠杆)"
        spotLabel="公开股权市值(现货)"
        levColor="#f59e0b"
        spotColor="#3b82f6"
      />
      <LeverageBar
        title="加密"
        lev={cryptoLev}
        spot={cryptoSpot}
        levLabel="永续 OI(杠杆·Bybit样本)"
        spotLabel="加密总市值(现货)"
        levColor="#f6465d"
        spotColor="#14f195"
      />
      <p className="sm:col-span-2 font-mono text-[9px] leading-relaxed text-muted-foreground/70">
        美股杠杆=保证金债务(官方季度值)、美股现货=公开股权总市值(FRED Z.1 季度)；加密杠杆=Bybit 单所永续 OI(USD 样本)、加密现货=CoinPaprika 总市值
      </p>
    </div>
  )
}

// ── 组合：资金流图表区（快照动态仪表盘，30s 轮询自动刷新）───────────────
export function FlowOverviewCharts({ items }: { items: FlowMetric[] }) {
  return (
    <>
      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-4">
        <ChartCard title="资金流向 · 净买盘" subtitle="CVD 主动买−卖（近1h）">
          <CvdFlowChart items={items} />
        </ChartCard>
        <ChartCard title="持仓集中度 · OI" subtitle="未平仓量构成（近似）">
          <OiComposition items={items} />
        </ChartCard>
        <ChartCard title="杠杆多空占比" subtitle="账户情绪（1h）">
          <LeverageStack items={items} />
        </ChartCard>
        <ChartCard title="资金费率 · 成本" subtitle="8h 资金费率拥挤方向">
          <FundingStrip items={items} />
        </ChartCard>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title="各金融品类资金构成 · COT" subtitle="投机净头寸按品类聚合（净多绿 / 净空红）">
          <CotComposition items={items} />
        </ChartCard>
        <ChartCard title="加密内部资金 · BTC / ETH / 山寨" subtitle="加密现货市值构成（单看加密与山寨）">
          <CryptoSpotMap items={items} />
        </ChartCard>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title="杠杆 vs 现货占比" subtitle="美股 · 加密 两个市场，杠杆资金 / 现货市值">
          <LeverageSplit items={items} />
        </ChartCard>
        <ChartCard title="杠杆资金 · 保证金债务" subtitle="美股杠杆总量（季度）+ 加密合约 OI">
          <MarginDebtPanel items={items} />
        </ChartCard>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title="资金费率趋势 · 48h" subtitle="各资产 funding% 随时间变化（动态）">
          <FundingTrend />
        </ChartCard>
        <ChartCard title="资金流向 · CVD 净买盘趋势 · 48h" subtitle="主动买−卖净占比（正=流入 / 负=流出，动态）">
          <CvdTrend />
        </ChartCard>
      </div>
    </>
  )
}

// ── 动态趋势图（lightweight-charts，后端 /api/macro/history 时间序列）────────────
type SeriesPoint = { ts: number; value: number }
type HistoryResp = { status?: string; series?: Record<string, SeriesPoint[]> }

const TREND_COLORS: Record<string, string> = {
  BTC: '#f7931a',
  ETH: '#627eea',
  SOL: '#14f195',
  XAU: '#f59e0b',
}

function assetOfKey(key: string): string {
  return key.includes('.') ? key.slice(0, key.lastIndexOf('.')) : key
}

function trendTheme(isDark: boolean) {
  return isDark
    ? { bg: '#16171b', text: '#9aa3af', grid: '#26282e' }
    : { bg: '#ffffff', text: '#5b6472', grid: '#eceff3' }
}

function TrendWrap({
  error,
  innerRef,
}: {
  error: string | null
  innerRef: React.RefObject<HTMLDivElement>
}) {
  return (
    <div className="relative h-48 w-full">
      <div ref={innerRef} className="h-48 w-full" />
      {error && (
        <div className="absolute inset-0 flex items-center justify-center rounded border border-short/30 bg-short/10 font-mono text-xs text-short">
          {error}
        </div>
      )}
    </div>
  )
}

// 资金费率趋势：多资产折线（%），48h 变化看资金面升温/退潮
export function FundingTrend({ hours = 48 }: { hours?: number }) {
  const isDark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  const [ready, setReady] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const t = trendTheme(isDark)
    const chart = createChart(el, {
      autoSize: true,
      layout: { background: { color: t.bg }, textColor: t.text },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: t.grid },
      rightPriceScale: { borderColor: t.grid },
    })
    chartRef.current = chart
    seriesRef.current = new Map()
    setReady(true)
    return () => {
      setReady(false)
      chart.remove()
      chartRef.current = null
      seriesRef.current = new Map()
    }
  }, [isDark])

  useEffect(() => {
    if (!ready) return
    const keys = ['BTC.funding', 'ETH.funding', 'XAU.funding']
    let alive = true
    const load = () => {
      fetch(`${API_BASE}/macro/history?hours=${hours}&keys=${keys.join(',')}`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d: HistoryResp) => {
          if (!alive || !chartRef.current) return
          const series = d.series ?? {}
          setError(null)
          for (const key of keys) {
            const pts = series[key] ?? []
            let line = seriesRef.current.get(key)
            if (!line) {
              line = chartRef.current.addSeries(LineSeries, {
                color: TREND_COLORS[assetOfKey(key)] ?? '#94a3b8',
                lineWidth: 2,
                priceFormat: {
                  type: 'custom',
                  formatter: (v: number) => `${v.toFixed(4)}%`,
                },
              })
              seriesRef.current.set(key, line)
            }
            line.setData(pts.map((p) => ({ time: p.ts as UTCTimestamp, value: p.value })))
          }
        })
        .catch((e) => {
          if (alive) setError(e instanceof Error ? e.message : String(e))
        })
    }
    void load()
    const timer = window.setInterval(load, 60_000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [ready, hours])

  return <TrendWrap error={error} innerRef={containerRef} />
}
// 资金流向 · CVD 净买盘趋势：多资产折线（%），正=净流入 / 负=净流出，看资金方向切换
export function CvdTrend({ hours = 48 }: { hours?: number }) {
  const isDark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  const [ready, setReady] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const t = trendTheme(isDark)
    const chart = createChart(el, {
      autoSize: true,
      layout: { background: { color: t.bg }, textColor: t.text },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: t.grid },
      rightPriceScale: { borderColor: t.grid },
    })
    chartRef.current = chart
    seriesRef.current = new Map()
    setReady(true)
    return () => {
      setReady(false)
      chart.remove()
      chartRef.current = null
      seriesRef.current = new Map()
    }
  }, [isDark])

  useEffect(() => {
    if (!ready) return
    const keys = ['BTC.cvd', 'ETH.cvd', 'XAU.cvd']
    let alive = true
    const load = () => {
      fetch(`${API_BASE}/macro/history?hours=${hours}&keys=${keys.join(',')}`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d: HistoryResp) => {
          if (!alive || !chartRef.current) return
          const series = d.series ?? {}
          setError(null)
          for (const key of keys) {
            const pts = series[key] ?? []
            let line = seriesRef.current.get(key)
            if (!line) {
              line = chartRef.current.addSeries(LineSeries, {
                color: TREND_COLORS[assetOfKey(key)] ?? '#94a3b8',
                lineWidth: 2,
                priceFormat: {
                  type: 'custom',
                  formatter: (v: number) => `${v.toFixed(2)}%`,
                },
              })
              seriesRef.current.set(key, line)
              if (key === 'BTC.cvd') {
                line.createPriceLine({
                  price: 0,
                  color: isDark ? '#475569' : '#94a3b8',
                  lineWidth: 1,
                  lineStyle: LineStyle.Dashed,
                  axisLabelVisible: false,
                  title: '',
                })
              }
            }
            line.setData(pts.map((p) => ({ time: p.ts as UTCTimestamp, value: p.value })))
          }
        })
        .catch((e) => {
          if (alive) setError(e instanceof Error ? e.message : String(e))
        })
    }
    void load()
    const timer = window.setInterval(load, 60_000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [ready, hours, isDark])

  return <TrendWrap error={error} innerRef={containerRef} />
}