'use client'

/**
 * app/divergence/page.tsx — 背离雷达 (2026-11-16)
 *
 * 回答三个问题：
 *   1. 现在哪些标的出现了顶/底背离，强度多少（A/B/C 分级 + 多腿证据）
 *   2. 这个信号怎么交易（回踩入场 / 失效价 / 目标 / 盈亏比）
 *   3. 这类信号在该标的、该周期、该趋势环境下历史准不准（按上下文分组的真实胜率）
 *
 * 数据来自 /api/divergence/*，引擎 divergence_engine.py：
 *   公开指标（RSI/MACD/OBV/MFI/CCI/Stoch/ADX/ATR）+ pivot 确认 + 无未来函数。
 * 本页只展示，不接自动下单；下单仍走原有链路。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { CapabilityPanel } from '@/components/quant/capability-panel'
import { DivergenceScoreboard } from '@/components/divergence/scoreboard'

type Pivot = { idx: number; ts: number; price: number; at?: string }
type Evidence = { indicator: string; leg: string; a: number; b: number; delta: number; weight: number }
type Signal = {
  type: 'bearish' | 'bullish'
  kind: 'regular' | 'hidden'
  score: number
  grade: 'A' | 'B' | 'C' | 'D'
  agree_legs: number
  evidence: Evidence[]
  pivot_a: Pivot
  pivot_b: Pivot
  confirm_idx: number
  confirm_ts: number
  confirm_at: string
  invalidation: number
  trigger: number
  target: number
  atr: number
  adx: number | null
  trend: 'up' | 'down' | 'unknown'
  context: string
  ema200: number | null
  bars_gap: number
  bars_since_confirm: number
  status: '待触发' | '已触发' | '已失效'
  last_close: number
  risk_reward: number
}
type ScanResp = {
  ok: boolean
  asset: string
  interval: string
  bars: number
  last_close: number
  last_ts: number
  count: number
  items: Signal[]
}
type StatsBucket = { n: number; hit_rate: number | null; avg_pnl: number | null; median_pnl?: number | null; lift?: number | null }
type StatsResp = {
  ok: boolean
  asset: string
  interval: string
  horizon: number
  bars: number
  signals: number
  baseline: { up_rate: number; down_rate: number; n: number }
  by_grade: Record<string, StatsBucket>
  by_type: Record<string, StatsBucket>
  by_context: Record<string, StatsBucket>
  all: StatsBucket
}
type Bar = { ts: number; open: number; high: number; low: number; close: number; volume: number }
type PivotMark = { idx: number; ts: number; price: number; type: string; kind: string; grade: string; tag: 'a' | 'b' }
type BarsResp = { ok: boolean; asset: string; interval: string; bars: Bar[]; pivots: PivotMark[] }
type ConfluenceItem = {
  interval: string
  bias: 'bearish' | 'bullish' | 'neutral'
  latest: { type: string; kind: string; grade: string; score: number; bars_since_confirm: number } | null
}
type ConfluenceResp = {
  ok: boolean
  asset: string
  items: ConfluenceItem[]
  resonance: 'bearish' | 'bullish' | 'none'
  bear_count: number
  bull_count: number
}
type VBucket = {
  n: number
  hit_rate: number | null
  avg_pnl: number | null
  profit_factor?: number | null
  verdict?: '有效' | '观察' | '未通过' | '样本不足'
  avg_win?: number | null
  avg_loss?: number | null
}
type ValidationResp = {
  ok: boolean
  horizon: number
  train_ratio: number
  cost_bps: number
  baseline_rate: number
  tests: number
  assets: number
  overall: VBucket
  by_rule: { id: string; label: string; horizon: number; note: string; train: VBucket; test: VBucket }[]
  by_grade: { grade: string; train: VBucket; test: VBucket }[]
  by_context: { context: string; train: VBucket; test: VBucket }[]
}
type LedgerItem = {
  id: number
  asset: string
  interval: string
  type: 'bearish' | 'bullish'
  kind: string
  grade: string
  score: number
  context: string
  confirm_ts: number
  confirm_at: string
  entry: number
  exit_price: number | null
  pnl_pct: number | null
  hit: number | null
  mfe_pct: number | null
  mae_pct: number | null
  status: string
  executable: number
  rule_id: string | null
  horizon: number
}
type LedgerResp = {
  ok: boolean
  items: LedgerItem[]
  stats: {
    total: number
    open: number
    hit_rate: number | null
    avg_pnl: number | null
    by_context: { context: string; grade: string; n: number; hit_rate: number; avg_pnl: number }[]
  }
}

const INTERVALS: { key: string; label: string }[] = [
  { key: '1d', label: '日线' },
  { key: '4h', label: '4 小时' },
  { key: '1h', label: '1 小时' },
  { key: '15m', label: '15 分钟' },
]

const GRADE_ORDER = ['A', 'B', 'C', 'D']

function fmt(v: number | null | undefined, digits?: number): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const d = digits ?? (Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 10 ? 2 : 4)
  return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d })
}

function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return `${v.toFixed(digits)}%`
}

function dateLabel(ts: number, interval: string): string {
  const d = new Date(ts * 1000)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  if (interval === '1d') return `${mm}-${dd}`
  const hh = String(d.getHours()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${String(d.getMinutes()).padStart(2, '0')}`
}

function contextLabel(key: string): string {
  const [kind, type, trend] = key.split('/')
  const k = kind === 'regular' ? '常规背离' : '隐藏背离'
  const t = type === 'bearish' ? '顶' : '底'
  const r = trend === 'up' ? '上升趋势' : trend === 'down' ? '下降趋势' : '趋势未知'
  return `${t}·${k} / ${r}`
}

function sideLabel(sig: Pick<Signal, 'type' | 'kind'>): string {
  const t = sig.type === 'bearish' ? '顶背离' : '底背离'
  return sig.kind === 'regular' ? t : `${t}（隐藏）`
}

function gradeClass(g: string): string {
  if (g === 'A') return 'border-amber-500/50 bg-amber-500/10 text-amber-300'
  if (g === 'B') return 'border-sky-500/40 bg-sky-500/10 text-sky-300'
  if (g === 'C') return 'border-border bg-muted/40 text-muted-foreground'
  return 'border-border bg-transparent text-muted-foreground/70'
}

function statusClass(s: string): string {
  if (s === '已触发') return 'border-long/40 text-long'
  if (s === '已失效') return 'border-short/40 text-short'
  return 'border-border text-muted-foreground'
}

/* ===== K 线图：蜡烛 + pivot 标注 + 交易计划线 ===== */
function verdictClass(v?: string): string {
  if (v === '有效') return 'border-long/50 bg-long/10 text-long'
  if (v === '观察') return 'border-amber-500/50 bg-amber-500/10 text-amber-500'
  if (v === '未通过') return 'border-short/50 bg-short/10 text-short'
  return 'border-border text-muted-foreground/70'
}

function signedPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function CandleChart({ bars, pivots, signal, interval }: {
  bars: Bar[]
  pivots: PivotMark[]
  signal: Signal | null
  interval: string
}) {
  const W = Math.max(720, bars.length * 6)
  const H = 340
  const padL = 8
  const padR = 88
  const padT = 14
  const padB = 34
  const plotW = W - padL - padR
  const plotH = H - padT - padB
  const cw = plotW / Math.max(1, bars.length)

  const { min, max } = useMemo(() => {
    const lows = bars.map((b) => b.low)
    const highs = bars.map((b) => b.high)
    const extra: number[] = []
    if (signal) extra.push(signal.invalidation, signal.trigger, signal.target)
    const lo = Math.min(...lows, ...(extra.length ? extra : []))
    const hi = Math.max(...highs, ...(extra.length ? extra : []))
    const pad = (hi - lo) * 0.06 || hi * 0.01
    return { min: lo - pad, max: hi + pad }
  }, [bars, signal])

  const y = (p: number) => padT + (max - p) / (max - min || 1) * plotH
  const x = (i: number) => padL + i * cw + cw / 2

  const selectedPivots = signal
    ? pivots.filter((p) => p.ts === signal.pivot_a.ts || p.ts === signal.pivot_b.ts)
    : []

  const ticks = useMemo(() => {
    const out: number[] = []
    const step = Math.max(1, Math.floor(bars.length / 6))
    for (let i = 0; i < bars.length; i += step) out.push(i)
    return out
  }, [bars.length])

  return (
    <div className="thin-scroll overflow-x-auto">
      <svg width={W} height={H} className="block">
        {/* 网格 */}
        {[0.25, 0.5, 0.75].map((r) => (
          <line key={r} x1={padL} x2={padL + plotW} y1={padT + plotH * r} y2={padT + plotH * r}
            stroke="var(--border)" strokeWidth={0.5} opacity={0.5} />
        ))}
        {bars.map((b, i) => {
          const up = b.close >= b.open
          const color = up ? 'var(--long)' : 'var(--short)'
          const bodyTop = y(Math.max(b.open, b.close))
          const bodyH = Math.max(1, Math.abs(y(b.open) - y(b.close)))
          return (
            <g key={b.ts}>
              <line x1={x(i)} x2={x(i)} y1={y(b.high)} y2={y(b.low)} stroke={color} strokeWidth={0.8} opacity={0.8} />
              <rect x={x(i) - cw * 0.32} y={bodyTop} width={cw * 0.64} height={bodyH}
                fill={color} opacity={0.85} />
            </g>
          )
        })}

        {/* 计划线 */}
        {signal && ([
          ['入场', signal.pivot_b.price, 'var(--foreground)'],
          ['失效', signal.invalidation, 'var(--short)'],
          ['目标', signal.target, 'var(--long)'],
        ] as [string, number, string][]).map(([label, price, color]) => (
          <g key={label}>
            <line x1={padL} x2={padL + plotW} y1={y(price)} y2={y(price)}
              stroke={color} strokeWidth={1} strokeDasharray="4 4" opacity={0.7} />
            <text x={padL + plotW + 6} y={y(price) + 3} fontSize={10} fill={color} fontFamily="var(--font-mono)">
              {label} {fmt(price)}
            </text>
          </g>
        ))}

        {/* pivot 标注 */}
        {(selectedPivots.length ? selectedPivots : pivots).map((p, i) => {
          const on = selectedPivots.length > 0
          const color = p.type === 'bearish' ? 'var(--short)' : 'var(--long)'
          return (
            <g key={`${p.ts}-${p.tag}-${i}`}>
              <circle cx={x(p.idx)} cy={y(p.price)} r={on ? 4 : 2.5}
                fill={color} stroke="var(--background)" strokeWidth={1} opacity={on ? 1 : 0.55} />
              {on && (
                <text x={x(p.idx) + 6} y={y(p.price) - 6} fontSize={10} fill={color} fontFamily="var(--font-mono)">
                  {p.tag.toUpperCase()} {p.grade}
                </text>
              )}
            </g>
          )
        })}

        {/* 时间轴 */}
        {ticks.map((i, k) => {
          // v499：首/末刻度居中对齐会被画布边缘裁掉（首标签 "10-16" 曾只剩 "0-16"）。
          // 首刻度改用 start 从 0 起排，末刻度改 end 贴右边界，中间仍居中。
          const first = k === 0
          const last = k === ticks.length - 1 && ticks.length > 1
          const anchor = first ? 'start' : last ? 'end' : 'middle'
          const tx = first ? 0 : last ? padL + plotW : x(i)
          return (
            <text key={i} x={tx} y={H - 10} fontSize={9} fill="var(--muted-foreground)"
              textAnchor={anchor} fontFamily="var(--font-mono)">
              {dateLabel(bars[i].ts, interval)}
            </text>
          )
        })}
      </svg>
    </div>
  )
}

/* ===== 证据条 ===== */
function EvidenceRow({ e }: { e: Evidence }) {
  const maxDelta = 40
  const ratio = Math.min(1, Math.abs(e.delta) / maxDelta)
  const positive = e.delta >= 0
  return (
    <div className="grid grid-cols-[86px_1fr_88px] items-center gap-2 py-1">
      <span className="truncate font-mono text-[11px] text-muted-foreground">{e.indicator}</span>
      <div className="relative h-[6px] rounded-sm bg-muted/50">
        <div className="absolute top-0 h-full rounded-sm"
          style={{ width: `${ratio * 50}%`, left: positive ? '50%' : `${50 - ratio * 50}%`,
            background: positive ? 'var(--long)' : 'var(--short)', opacity: 0.75 }} />
        <div className="absolute left-1/2 top-0 h-full w-px bg-border" />
      </div>
      <span className="text-right font-mono text-[11px] text-foreground/80">
        {e.a.toFixed(1)} → {e.b.toFixed(1)}
      </span>
    </div>
  )
}

export default function DivergencePage() {
  const [asset, setAsset] = useState('BTC')
  const [interval, setInterval] = useState('1d')
  const [assets, setAssets] = useState<string[]>([])
  const [scan, setScan] = useState<ScanResp | null>(null)
  const [stats, setStats] = useState<StatsResp | null>(null)
  const [bars, setBars] = useState<BarsResp | null>(null)
  const [conf, setConf] = useState<ConfluenceResp | null>(null)
  const [selected, setSelected] = useState(0)
  const [tab, setTab] = useState<'signals' | 'calibration' | 'validation' | 'ledger'>('signals')
  const [horizon, setHorizon] = useState(10)
  const [minGrade, setMinGrade] = useState('C')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [validation, setValidation] = useState<ValidationResp | null>(null)
  const [ledger, setLedger] = useState<LedgerResp | null>(null)
  const [extraLoading, setExtraLoading] = useState(false)

  useEffect(() => {
    setErr('')
    fetch(`${API_BASE}/divergence/assets?interval=${interval}`)
      .then((r) => r.json())
      .then((d) => {
        const list: string[] = (d.items || []).map((x: { asset: string }) => x.asset)
        setAssets(list)
        if (list.length && !list.includes(asset)) setAsset(list[0])
      })
      .catch((e) => setErr(String(e)))
  }, [interval, asset])

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const qs = new URLSearchParams({ asset, interval, within: '160', min_grade: minGrade })
      const [s, b, st, cf] = await Promise.all([
        fetch(`${API_BASE}/divergence/scan?${qs}`).then((r) => { if (!r.ok) throw new Error(`scan HTTP ${r.status}`); return r.json() }),
        fetch(`${API_BASE}/divergence/bars?asset=${asset}&interval=${interval}&limit=420&within=200`).then((r) => { if (!r.ok) throw new Error(`bars HTTP ${r.status}`); return r.json() }),
        fetch(`${API_BASE}/divergence/stats?asset=${asset}&interval=${interval}&horizon=${horizon}`).then((r) => { if (!r.ok) throw new Error(`stats HTTP ${r.status}`); return r.json() }),
        fetch(`${API_BASE}/divergence/confluence?asset=${asset}`).then((r) => { if (!r.ok) throw new Error(`confluence HTTP ${r.status}`); return r.json() }),
      ])
      if (!s.ok) throw new Error(s.detail || 'scan failed')
      setScan(s)
      setBars(b)
      setStats(st)
      setConf(cf)
      setSelected(0)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [asset, interval, minGrade, horizon])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (tab === 'validation' && !validation) {
      setExtraLoading(true)
      fetch(`${API_BASE}/divergence/validation?horizon=20&interval=1d`)
        .then((r) => r.json())
        .then(setValidation)
        .catch(() => {})
        .finally(() => setExtraLoading(false))
    }
    if (tab === 'ledger' && !ledger) {
      setExtraLoading(true)
      fetch(`${API_BASE}/divergence/ledger?status=all&limit=200`)
        .then((r) => r.json())
        .then(setLedger)
        .catch(() => {})
        .finally(() => setExtraLoading(false))
    }
  }, [tab, validation, ledger])

  const current = scan?.items?.[selected] || null
  const counts = useMemo(() => {
    const items = scan?.items || []
    return {
      total: items.length,
      a: items.filter((x) => x.grade === 'A').length,
      triggerable: items.filter((x) => x.status === '待触发').length,
      triggered: items.filter((x) => x.status === '已触发').length,
    }
  }, [scan])

  const gradeRows = useMemo(() => (stats ? GRADE_ORDER.map((g) => ({ g, ...(stats.by_grade[g] || {}) })) : []), [stats])
  const contextRows = useMemo(() => {
    if (!stats || !stats.by_context) return [] as { key: string; b: StatsBucket }[]
    return Object.entries(stats.by_context)
      .filter(([, b]) => b && b.n > 0)
      .sort((a, b) => (b[1].n || 0) - (a[1].n || 0))
      .map(([key, b]) => ({ key, b }))
  }, [stats])

  // v498: 页眉日期此前写死 2026-11-16，看起来像数据还停在三天前；改成按本机日期显示。
  const [today, setToday] = useState('')
  useEffect(() => { setToday(new Date().toLocaleDateString('sv-SE')) }, [])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1400px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-border pb-3">
        <div>
          <h1 className="font-mono text-base font-semibold tracking-wider text-foreground">背离雷达</h1>
          <p className="mt-1 font-mono text-[11px] text-muted-foreground">
            公开指标 5 腿投票 · pivot 确认不重绘 · 按趋势上下文给出历史校准
          </p>
        </div>
        <div className="flex items-center gap-2 font-mono text-[11px]">
         {today ? `${today} · ` : ''}只读行情，不接下单
        </div>
      </header>

      {/* 控制条 */}
      <section className="mb-3 flex flex-wrap items-center gap-2">
        <select value={asset} onChange={(e) => setAsset(e.target.value)}
          className="h-8 rounded-sm border border-border bg-card px-2 font-mono text-xs text-foreground outline-none focus:border-ring">
          {assets.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <div className="flex rounded-sm border border-border">
          {INTERVALS.map((it) => (
            <button key={it.key} onClick={() => setInterval(it.key)}
              className={`px-2.5 py-1.5 font-mono text-[11px] ${interval === it.key ? 'bg-foreground text-background' : 'text-muted-foreground hover:text-foreground'}`}>
              {it.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1 font-mono text-[11px] text-muted-foreground">
          最低等级
          {['A', 'B', 'C'].map((g) => (
            <button key={g} onClick={() => setMinGrade(g)}
              className={`h-7 w-7 rounded-sm border ${minGrade === g ? gradeClass(g) : 'border-border text-muted-foreground'}`}>{g}</button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
          {loading ? '扫描中…' : scan ? `${scan.bars} 根 · 最新 ${dateLabel(scan.last_ts, interval)} · 收盘 ${fmt(scan.last_close)}` : ''}
          <button onClick={load} className="h-7 rounded-sm border border-border px-2 text-foreground hover:bg-muted/40">刷新</button>
        </div>
      </section>

      {err && <div className="mb-3 rounded-sm border border-short/40 bg-short/5 px-3 py-2 font-mono text-[11px] text-short">{err}</div>}

      {/* 概览卡 */}
      <section className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        {[
          ['当前信号', String(counts.total), `窗口内 C 级以上`],
          ['A 级', String(counts.a), '至少 3 条腿一致'],
          ['待触发', String(counts.triggerable), '价格未越过触发价'],
          ['已触发', String(counts.triggered), '已按计划方向运行'],
        ].map(([label, value, note]) => (
          <div key={label} className="surface-card px-3 py-2">
            <div className="font-mono text-[11px] tracking-wider text-muted-foreground">{label}</div>
            <div className="mt-0.5 font-mono text-xl text-foreground">{value}</div>
            <div className="font-mono text-[11px] text-muted-foreground/80">{note}</div>
          </div>
        ))}
      </section>

      {/* 跨周期共振 */}
      {conf && Array.isArray(conf.items) && (
        <section className="surface-card mb-3 flex flex-wrap items-center gap-2 px-3 py-2 font-mono text-[11px]">
          <span className="text-muted-foreground">跨周期共振</span>
          {conf.items.map((it) => (
            <span key={it.interval} className="flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5">
              <span className="text-muted-foreground">{it.interval === '1d' ? '日线' : it.interval}</span>
              {it.latest ? (
                <>
                  <span className={it.latest.type === 'bearish' ? 'text-short' : 'text-long'}>
                    {it.latest.type === 'bearish' ? '顶' : '底'}
                  </span>
                  <span className={`rounded-sm border px-1 text-[11px] ${gradeClass(it.latest.grade)}`}>{it.latest.grade}</span>
                </>
              ) : <span className="text-muted-foreground/60">无</span>}
            </span>
          ))}
          {conf.resonance === 'none' ? (
            <span className="ml-auto text-muted-foreground">未形成 2 周期以上同向</span>
          ) : (
            <span className={`ml-auto rounded-sm border px-2 py-0.5 ${conf.resonance === 'bearish' ? 'border-short/40 text-short' : 'border-long/40 text-long'}`}>
              {conf.resonance === 'bearish' ? '顶部共振' : '底部共振'} · {conf.resonance === 'bearish' ? conf.bear_count : conf.bull_count} 个周期同向
            </span>
          )}
        </section>
      )}

      {/* 图表 */}
      <section className="surface-card mb-3 overflow-hidden">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div className="font-mono text-[11px] text-muted-foreground">
            {asset} · {INTERVALS.find((x) => x.key === interval)?.label} · 蜡烛 + 背离 pivot
          </div>
          {current && (
            <div className="flex items-center gap-2 font-mono text-[11px]">
              <span className={`rounded-sm border px-1.5 py-0.5 ${gradeClass(current.grade)}`}>{current.grade}</span>
              <span className={current.type === 'bearish' ? 'text-short' : 'text-long'}>{sideLabel(current)}</span>
              <span className="text-muted-foreground">评分 {current.score.toFixed(1)}</span>
            </div>
          )}
        </div>
        {bars && bars.bars?.length ? (
          <CandleChart bars={bars.bars} pivots={bars.pivots || []} signal={current} interval={interval} />
        ) : (
          <div className="px-3 py-8 text-center font-mono text-[11px] text-muted-foreground">
            {err ? '行情图表暂不可用（接口异常，稍后自动重试）' : '图表数据加载中'}
          </div>
        )}
      </section>

      {/* 主体两栏 */}
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        {/* 左：信号列表 / 校准 */}
        <section className="surface-card flex min-h-[420px] flex-col overflow-hidden">
          <nav className="flex border-b border-border">
            {([['signals', '信号列表'], ['calibration', '历史校准'], ['validation', '样本外验证'], ['ledger', '信号台账']] as const).map(([k, label]) => (
              <button key={k} onClick={() => setTab(k)}
                className={`px-3 py-2 font-mono text-[11px] ${tab === k ? 'border-b-2 border-foreground text-foreground' : 'text-muted-foreground'}`}>
                {label}
              </button>
            ))}
          </nav>

          {tab === 'signals' ? (
            <div className="thin-scroll max-h-[560px] overflow-y-auto">
              {(scan?.items || []).length === 0 && (
                <div className="px-3 py-10 text-center font-mono text-[11px] text-muted-foreground">
                  该窗口内没有达到 {minGrade} 级的背离信号。可放宽等级或切换周期。
                </div>
              )}
              {(scan?.items || []).map((s, i) => (
                <button key={`${s.confirm_ts}-${i}`} onClick={() => setSelected(i)}
                  className={`block w-full border-b border-border/60 px-3 py-2 text-left hover:bg-muted/30 ${selected === i ? 'bg-muted/40' : ''}`}>
                  <div className="flex items-center gap-2">
                    <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[11px] ${gradeClass(s.grade)}`}>{s.grade}</span>
                    <span className={`font-mono text-xs ${s.type === 'bearish' ? 'text-short' : 'text-long'}`}>{sideLabel(s)}</span>
                    <span className="font-mono text-[11px] text-muted-foreground">{s.bars_since_confirm} 根前确认</span>
                    {/* v499：133 根前的确认还排在「当前信号」首位，会被读成"刚出现"。
                        超 60 根（约 60 天日线）标陈旧，让人一眼分清新老信号。 */}
                    {s.bars_since_confirm > 60 && (
                      <span className="rounded-sm border border-amber-500/40 px-1.5 py-0.5 font-mono text-[10px] text-amber-500">
                        陈旧
                      </span>
                    )}
                    <span className={`ml-auto rounded-sm border px-1.5 py-0.5 font-mono text-[11px] ${statusClass(s.status)}`}>{s.status}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
                    <span className="text-foreground">评分 {s.score.toFixed(1)}</span>
                    <span>{s.agree_legs} 腿一致</span>
                    <span>盈亏比 {s.risk_reward.toFixed(1)}</span>
                    <span>{s.trend === 'up' ? '均线上方' : s.trend === 'down' ? '均线下方' : '趋势未知'}</span>
                    <span className="text-foreground/70">失效 {fmt(s.invalidation)}</span>
                  </div>
                </button>
              ))}
            </div>
          ) : tab === 'calibration' ? (
            <div className="thin-scroll max-h-[560px] overflow-y-auto p-3">
              <div className="mb-3 flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
                持有
                {[5, 10, 20].map((h) => (
                  <button key={h} onClick={() => setHorizon(h)}
                    className={`rounded-sm border px-1.5 py-0.5 ${horizon === h ? 'border-foreground text-foreground' : 'border-border'}`}>{h} 根</button>
                ))}
                <span className="ml-auto">基准涨跌 {pct(stats?.baseline.up_rate)} / {pct(stats?.baseline.down_rate)}</span>
              </div>

              <div className="mb-4">
                <div className="mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">按识别强度</div>
                <table className="w-full table-fixed border-collapse font-mono text-[11px]">
                  <colgroup><col style={{ width: '18%' }} /><col style={{ width: '18%' }} /><col style={{ width: '22%' }} /><col style={{ width: '22%' }} /><col style={{ width: '20%' }} /></colgroup>
                  <thead>
                    <tr className="border-b border-border text-muted-foreground">
                      <th className="py-1 text-left font-normal">等级</th>
                      <th className="py-1 text-right font-normal">样本</th>
                      <th className="py-1 text-right font-normal">命中率</th>
                      <th className="py-1 text-right font-normal">平均盈亏</th>
                      <th className="py-1 text-right font-normal">超额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gradeRows.map((r) => (
                      <tr key={r.g} className="border-b border-border/50">
                        <td className="py-1"><span className={`rounded-sm border px-1 py-0.5 text-[11px] ${gradeClass(r.g)}`}>{r.g}</span></td>
                        <td className="py-1 text-right text-muted-foreground">{r.n ?? 0}</td>
                        <td className="py-1 text-right text-foreground">{pct(r.hit_rate)}</td>
                        <td className={`py-1 text-right ${(r.avg_pnl ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>{r.avg_pnl === null || r.avg_pnl === undefined ? '—' : `${r.avg_pnl > 0 ? '+' : ''}${r.avg_pnl.toFixed(2)}%`}</td>
                        <td className="py-1 text-right text-muted-foreground">{r.lift === null || r.lift === undefined ? '—' : `${r.lift > 0 ? '+' : ''}${r.lift.toFixed(1)}pt`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-1 font-mono text-[11px] leading-relaxed text-muted-foreground/80">
                  命中率 = 确认后第 {horizon} 根收盘方向与信号一致的比例；超额为相对全样本基准的百分点。
                </p>
              </div>

              <div>
                <div className="mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">按趋势上下文（同标的、同周期）</div>
                <table className="w-full table-fixed border-collapse font-mono text-[11px]">
                  <colgroup><col style={{ width: '44%' }} /><col style={{ width: '14%' }} /><col style={{ width: '18%' }} /><col style={{ width: '24%' }} /></colgroup>
                  <thead>
                    <tr className="border-b border-border text-muted-foreground">
                      <th className="py-1 text-left font-normal">场景</th>
                      <th className="py-1 text-right font-normal">样本</th>
                      <th className="py-1 text-right font-normal">命中率</th>
                      <th className="py-1 text-right font-normal">平均盈亏</th>
                    </tr>
                  </thead>
                  <tbody>
                    {contextRows.map(({ key, b }) => (
                      <tr key={key} className="border-b border-border/50">
                        <td className="py-1 pr-1 text-foreground/80" title={contextLabel(key)}>{contextLabel(key)}</td>
                        <td className="py-1 text-right text-muted-foreground">{b.n}</td>
                        <td className={`py-1 text-right ${b.n < 20 ? 'text-muted-foreground/60' : ((b.hit_rate ?? 0) >= (stats?.baseline.up_rate ?? 50) ? 'text-long' : 'text-short')}`}>
                          {pct(b.hit_rate)}
                        </td>
                        <td className={`py-1 text-right ${(b.avg_pnl ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>
                          {b.avg_pnl === null || b.avg_pnl === undefined ? '—' : `${b.avg_pnl > 0 ? '+' : ''}${b.avg_pnl.toFixed(2)}%`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-1 font-mono text-[11px] leading-relaxed text-muted-foreground/80">
                  样本少于 20 条的格子不做结论；同一场景在不同标的上的胜率会明显不同，以当前标的为准。
                </p>
              </div>
            </div>
          ) : tab === 'validation' ? (
            <div className="thin-scroll max-h-[560px] overflow-y-auto p-3">
              {!validation ? (
                <div className="py-10 text-center font-mono text-[11px] text-muted-foreground">
                  {extraLoading ? '正在跑样本外验证…' : '验证数据加载失败，稍后再试'}
                </div>
              ) : (
                <>
                  <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
                    <span>时间外样本 · 前 {Math.round(validation.train_ratio * 100)}% 观察，后 {100 - Math.round(validation.train_ratio * 100)}% 检验</span>
                    <span>扣往返 {validation.cost_bps}bp</span>
                    <span className="ml-auto">全样本基准 {pct(validation.baseline_rate)}</span>
                  </div>

                  {validation.by_rule.map((r) => (
                    <div key={r.id} className="mb-3 rounded-sm border border-border p-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-foreground">{r.label}</span>
                        <span className={`ml-auto rounded-sm border px-1.5 py-0.5 font-mono text-[11px] ${verdictClass(r.test.verdict)}`}>{r.test.verdict}</span>
                      </div>
                      <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-[11px]">
                        <div>
                          <div className="text-muted-foreground">样本外样本</div>
                          <div className="text-foreground">{r.test.n}</div>
                        </div>
                        <div>
                          <div className="text-muted-foreground">命中率</div>
                          <div className={((r.test.hit_rate ?? 0) >= validation.baseline_rate ? 'text-long' : 'text-short')}>{pct(r.test.hit_rate)}</div>
                        </div>
                        <div>
                          <div className="text-muted-foreground">平均盈亏 / 笔</div>
                          <div className={((r.test.avg_pnl ?? 0) >= 0 ? 'text-long' : 'text-short')}>{signedPct(r.test.avg_pnl)}</div>
                        </div>
                      </div>
                      <p className="mt-1.5 font-mono text-[11px] leading-relaxed text-muted-foreground/80">
                        观察段 n={r.train.n} · 命中 {pct(r.train.hit_rate)} · 盈亏 {signedPct(r.train.avg_pnl)}；{r.note}
                      </p>
                    </div>
                  ))}

                  <div className="mb-2 flex items-center gap-2 font-mono text-[11px]">
                    <span className="text-muted-foreground">全部信号（不筛选）</span>
                    <span className={`rounded-sm border px-1.5 py-0.5 text-[11px] ${verdictClass(validation.overall.verdict)}`}>{validation.overall.verdict}</span>
                    <span className="ml-auto text-muted-foreground">n={validation.overall.n} · {pct(validation.overall.hit_rate)} · {signedPct(validation.overall.avg_pnl)}</span>
                  </div>

                  <div className="mt-3 mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">按趋势上下文 · 观察段 vs 检验段</div>
                  <table className="w-full table-fixed border-collapse font-mono text-[11px]">
                    <colgroup><col style={{ width: '30%' }} /><col style={{ width: '12%' }} /><col style={{ width: '14%' }} /><col style={{ width: '12%' }} /><col style={{ width: '14%' }} /><col style={{ width: '18%' }} /></colgroup>
                    <thead>
                      <tr className="border-b border-border text-muted-foreground">
                        <th className="py-1 text-left font-normal">场景</th>
                        <th className="py-1 text-right font-normal">观察 n</th>
                        <th className="py-1 text-right font-normal">观察盈亏</th>
                        <th className="py-1 text-right font-normal">检验 n</th>
                        <th className="py-1 text-right font-normal">检验命中</th>
                        <th className="py-1 text-right font-normal">结论</th>
                      </tr>
                    </thead>
                    <tbody>
                      {validation.by_context
                        .filter((r) => r.train.n + r.test.n >= 10)
                        .sort((a, b) => b.test.n - a.test.n)
                        .map((r) => (
                          <tr key={r.context} className="border-b border-border/50">
                            <td className="py-1 pr-1 text-foreground/80" title={contextLabel(r.context)}>{contextLabel(r.context)}</td>
                            <td className="py-1 text-right text-muted-foreground">{r.train.n}</td>
                            <td className={`py-1 text-right ${(r.train.avg_pnl ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>{signedPct(r.train.avg_pnl)}</td>
                            <td className="py-1 text-right text-muted-foreground">{r.test.n}</td>
                            <td className={`py-1 text-right ${(r.test.hit_rate ?? 0) >= validation.baseline_rate ? 'text-long' : 'text-short'}`}>{pct(r.test.hit_rate)}</td>
                            <td className="py-1 text-right">
                              <span className={`rounded-sm border px-1 py-0.5 text-[11px] ${verdictClass(r.test.verdict)}`}>{r.test.verdict}</span>
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                  <p className="mt-2 font-mono text-[11px] leading-relaxed text-muted-foreground/80">
                    入场 = 确认当根收盘；出场 = 第 N 根收盘；成本按往返 {validation.cost_bps}bp 扣。样本少于 15 条不给结论。
                  </p>
                </>
              )}
            </div>
          ) : (
            <div className="thin-scroll max-h-[560px] overflow-y-auto p-3">
              {!ledger ? (
                <div className="py-10 text-center font-mono text-[11px] text-muted-foreground">
                  {extraLoading ? '正在同步台账…' : '台账加载失败，稍后再试'}
                </div>
              ) : (
                <>
                  <DivergenceScoreboard />
                  <div className="mb-3 grid grid-cols-4 gap-2 font-mono text-[11px]">
                    <div className="rounded-sm border border-border px-2 py-1.5">
                      <div className="text-muted-foreground">累计信号</div>
                      <div className="text-foreground">{ledger.stats.total}</div>
                    </div>
                    <div className="rounded-sm border border-border px-2 py-1.5">
                      <div className="text-muted-foreground">未结算</div>
                      <div className="text-foreground">{ledger.stats.open}</div>
                    </div>
                    <div className="rounded-sm border border-border px-2 py-1.5">
                      <div className="text-muted-foreground">已结算命中</div>
                      <div className="text-foreground">{pct(ledger.stats.hit_rate)}</div>
                    </div>
                    <div className="rounded-sm border border-border px-2 py-1.5">
                      <div className="text-muted-foreground">平均盈亏</div>
                      <div className={(ledger.stats.avg_pnl ?? 0) >= 0 ? 'text-long' : 'text-short'}>{signedPct(ledger.stats.avg_pnl)}</div>
                    </div>
                  </div>

                  {(ledger.items || []).length === 0 && (
                    <div className="py-8 text-center font-mono text-[11px] text-muted-foreground">
                      台账为空。点击下方按钮同步最近 90 根内的信号。
                    </div>
                  )}

                  <table className="w-full table-fixed border-collapse font-mono text-[11px]">
                    <colgroup><col style={{ width: '20%' }} /><col style={{ width: '26%' }} /><col style={{ width: '8%' }} /><col style={{ width: '14%' }} /><col style={{ width: '16%' }} /><col style={{ width: '16%' }} /></colgroup>
                    <thead>
                      <tr className="border-b border-border text-muted-foreground">
                        <th className="py-1 text-left font-normal">确认</th>
                        <th className="py-1 text-left font-normal">标的 / 场景</th>
                        <th className="py-1 text-left font-normal">等级</th>
                        <th className="py-1 text-left font-normal">状态</th>
                        <th className="py-1 text-right font-normal">盈亏</th>
                        <th className="py-1 text-right font-normal">最大有利/不利</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(ledger.items || []).slice(0, 80).map((x) => (
                        <tr key={x.id} className="border-b border-border/50">
                          <td className="py-1 text-muted-foreground">{x.confirm_at?.slice(0, 10)}</td>
                          <td className="py-1 pr-1">
                            <span className="text-foreground">{x.asset}</span>
                            {x.executable ? <span className="ml-1 rounded-sm border border-long/50 bg-long/10 px-1 text-[11px] text-long">可执行</span> : null}
                            <div className="truncate text-[11px] text-muted-foreground" title={contextLabel(x.context)}>{contextLabel(x.context)}</div>
                          </td>
                          <td className="py-1"><span className={`rounded-sm border px-1 py-0.5 text-[11px] ${gradeClass(x.grade)}`}>{x.grade}</span></td>
                          <td className={`py-1 text-[11px] ${x.status === 'settled' ? 'text-muted-foreground' : 'text-amber-500'}`}>{x.status === 'settled' ? `已结算 ${x.horizon}根` : '待结算'}</td>
                          <td className={`py-1 text-right ${(x.pnl_pct ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>{x.status === 'settled' ? signedPct(x.pnl_pct) : '—'}</td>
                          <td className="py-1 text-right text-[11px] text-muted-foreground">
                            {x.status === 'settled' ? `${signedPct(x.mfe_pct)} / ${signedPct(-(x.mae_pct ?? 0))}` : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>

                  <button
                    onClick={async () => {
                      setExtraLoading(true)
                      try {
                        await fetch(`${API_BASE}/divergence/ledger/sync`, { method: 'POST' })
                        const d = await fetch(`${API_BASE}/divergence/ledger?status=all&limit=200`).then((r) => r.json())
                        setLedger(d)
                      } finally {
                        setExtraLoading(false)
                      }
                    }}
                    className="mt-3 rounded-sm border border-border px-2 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground">
                    {extraLoading ? '同步中…' : '同步 / 结算台账'}
                  </button>
                  <p className="mt-2 font-mono text-[11px] leading-relaxed text-muted-foreground/80">
                    每条信号确认后自动入库，按各自 horizon 到期结算；“可执行”仅限命中样本外有效规则的信号。盈亏已扣 {8}bp 往返成本。
                  </p>
                </>
              )}
            </div>
          )}
        </section>

        {/* 右：详情 */}
        <section className="surface-card flex min-h-[420px] flex-col overflow-hidden">
          <div className="border-b border-border px-3 py-2 font-mono text-[11px] text-muted-foreground">信号详情</div>
          {!current ? (
            <div className="flex flex-1 items-center justify-center px-4 text-center font-mono text-[11px] text-muted-foreground">
              左侧选择一条信号，查看多腿证据与交易计划
            </div>
          ) : (
            <div className="thin-scroll max-h-[600px] overflow-y-auto p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`rounded-sm border px-2 py-1 font-mono text-xs ${gradeClass(current.grade)}`}>{current.grade} 级</span>
                <span className={`font-mono text-sm ${current.type === 'bearish' ? 'text-short' : 'text-long'}`}>{sideLabel(current)}</span>
                <span className="ml-auto font-mono text-lg text-foreground">{current.score.toFixed(1)}<span className="text-[11px] text-muted-foreground"> 分</span></span>
              </div>

              <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-[11px]">
                <span className="text-muted-foreground">确认时间</span><span className="text-right text-foreground">{current.confirm_at}</span>
                <span className="text-muted-foreground">间隔根数</span><span className="text-right text-foreground">{current.bars_gap}</span>
                <span className="text-muted-foreground">腿一致</span><span className="text-right text-foreground">{current.agree_legs} / 5</span>
                <span className="text-muted-foreground">ADX</span><span className="text-right text-foreground">{current.adx === null ? '—' : current.adx.toFixed(1)}</span>
                <span className="text-muted-foreground">ATR</span><span className="text-right text-foreground">{fmt(current.atr)}</span>
              </div>

              <div className="mt-3 border-t border-border pt-2">
                <div className="mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">多腿证据（左值 A 点 → 右值 B 点）</div>
                {current.evidence.map((e) => <EvidenceRow key={e.leg} e={e} />)}
              </div>

              <div className="mt-3 border-t border-border pt-2">
                <div className="mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">交易计划（价格触发，不追单）</div>
                <div className="grid grid-cols-[64px_1fr] gap-y-1 font-mono text-[11px]">
                  <span className="text-muted-foreground">入场</span>
                  <span className="text-foreground">{fmt(current.pivot_b.price)} <span className="text-muted-foreground">（回踩 B 点）</span></span>
                  <span className="text-muted-foreground">触发</span>
                  <span className="text-foreground">{fmt(current.trigger)} <span className="text-muted-foreground">（越过即确认）</span></span>
                  <span className="text-muted-foreground">失效</span>
                  <span className="text-short">{fmt(current.invalidation)}</span>
                  <span className="text-muted-foreground">目标</span>
                  <span className="text-long">{fmt(current.target)}</span>
                  <span className="text-muted-foreground">盈亏比</span>
                  <span className="text-foreground">{current.risk_reward.toFixed(2)} : 1</span>
                </div>
              </div>

              {stats && current.context && stats.by_context[current.context] && (
                <div className="mt-3 border-t border-border pt-2">
                  <div className="mb-1 font-mono text-[11px] tracking-wider text-muted-foreground">该上下文的历史表现</div>
                  <div className="flex items-baseline gap-2 font-mono text-[11px]">
                    <span className="text-foreground">{contextLabel(current.context)}</span>
                    <span className="text-muted-foreground">样本 {stats.by_context[current.context].n}</span>
                    <span className={`ml-auto ${(stats.by_context[current.context].hit_rate ?? 0) >= 50 ? 'text-long' : 'text-short'}`}>
                      命中 {pct(stats.by_context[current.context].hit_rate)}
                    </span>
                  </div>
                  {stats.by_context[current.context].n < 20 && (
                    <div className="mt-1 font-mono text-[11px] text-amber-400/80">样本不足 20 条，仅作参考，不构成统计结论。</div>
                  )}
                </div>
              )}
            </div>
          )}
        </section>
      </div>


      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="背离相关能力"
        hint="量价背离 / 波动率突变 / 相关性突变 / lead-lag"
        featureIds={['F22','F24','F32','F33']}
      />
      </section>
      <footer className="mt-4 border-t border-border pt-2 font-mono text-[11px] leading-relaxed text-muted-foreground/70">
        识别口径：同向 pivot（左右各 3 根）确认后，比较 RSI / MACD 柱 / OBV / MFI / CCI / Stoch 在 A、B 两点的变化；
        至少 2 条腿同向才算背离，A 级另要求 3 条腿以上。pivot 使用右侧 3 根确认，因此信号会滞后 3 根，但不重绘。
      </footer>
    </main>
  )
}
