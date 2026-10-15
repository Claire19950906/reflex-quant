'use client'
import { useEffect, useMemo, useState } from 'react'
import { cn } from '@/lib/utils'

type TabKey = 'missed' | 'calibration' | 'quality'
const TABS: ReadonlyArray<{ key: TabKey; label: string }> = [
  { key: 'missed', label: '漏检信号' },
  { key: 'calibration', label: '校准曲线' },
  { key: 'quality', label: '判断力评分' },
]
const PATTERN_LABELS: Record<string, string> = {
  vol_regime: '波动率体制', trend_persist: '趋势持续', mean_revert: '均值回归',
  pattern_match: '形态匹配', drawdown_recovery: '回撤恢复',
  volatility_breakout: '波动挤压', seasonality: '季节性',
}
const RECO_COLORS: Record<string, string> = {
  raise_weight: 'text-long bg-long/10 border-long/40',
  lower_weight: 'text-short bg-short/10 border-short/40',
  stop: 'text-short bg-short/10 border-short/40',
  hold: 'text-muted-foreground bg-muted/30 border-muted-foreground/30',
  insufficient_data: 'text-muted-foreground bg-muted/20 border-muted-foreground/20',
}
type MissedRow = {
  id: number; asset: string; decision_date: string; decision_id: number | null
  signal_id: number | null; pattern_type: string; signal: string
  ignored_reason: string; confidence_at_time: number | null
  actual_outcome_5d: number | null; actual_outcome_20d: number | null
  hit: number | null; created_at: string
}
type CalibrationRow = {
  id: number; week_start: string; asset: string; pattern_type: string
  n_signals: number; n_hit: number; hit_rate: number
  avg_edge_5d: number; avg_confidence: number
  confidence_calibration: number; recommendation: string
  notes: string; created_at: string
}
type QualityByPattern = {
  pattern_type: string; n: number; n_hit: number
  hit_rate: number; avg_confidence: number
  confidence_calibration: number; avg_edge_5d: number
}
type QualityOverall = {
  n?: number; n_hit?: number; hit_rate?: number
  avg_confidence?: number; overconfidence_gap?: number
}
type QualityResp = {
  ok: boolean; by_pattern?: QualityByPattern[]
  overall?: QualityOverall
  trend?: Array<{ week_start: string; n: number; n_hit: number; hit_rate: number; avg_confidence: number; overconfidence_gap: number }>
  error?: string
}
type TrendPoint = { week_start: string; n: number; n_hit: number; hit_rate: number; avg_confidence: number; overconfidence_gap: number }
function TrendChart({ points }: { points: TrendPoint[] }) {
  if (points.length < 2) return (
    <div className="rounded border border-muted-foreground/20 bg-muted/10 px-3 py-3 font-mono text-[11px] text-muted-foreground">
      需要至少 2 周 calibration 数据才能画趋势. 当前 {points.length} 周.
    </div>
  )
  const W = 320
  const H = 80
  const PAD = 6
  const xs = points.map((_, i) => PAD + (i * (W - 2 * PAD)) / Math.max(1, points.length - 1))
  // y 范围: gap ∈ [-0.5, 0.5]
  const Y_MIN = -0.5
  const Y_MAX = 0.5
  const yFor = (v: number) => H - PAD - ((v - Y_MIN) / (Y_MAX - Y_MIN)) * (H - 2 * PAD)
  const zeroY = yFor(0)
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"} ${xs[i].toFixed(1)} ${yFor(p.overconfidence_gap).toFixed(1)}`).join(" ")
  return (
    <div className="rounded border border-muted-foreground/30 bg-card p-2">
      <div className="mb-1 flex items-center justify-between font-mono text-[9px] text-muted-foreground/80">
        <span>Overconfidence Gap 趋势 (12 周)</span>
        <span className="flex items-center gap-2">
          <span className="text-short">过度自信 +</span>
          <span>0</span>
          <span className="text-long">谨慎 -</span>
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
        <line x1={PAD} x2={W - PAD} y1={zeroY} y2={zeroY} stroke="currentColor" strokeOpacity={0.2} strokeDasharray="2 2" />
        <path d={path} fill="none" stroke="currentColor" strokeWidth={1.4} className="text-foreground" />
        {points.map((p, i) => {
          const y = yFor(p.overconfidence_gap)
          const color = p.overconfidence_gap > 0.1 ? "text-short" : p.overconfidence_gap < -0.1 ? "text-long" : "text-muted-foreground"
          return (
            <g key={p.week_start}>
              <circle cx={xs[i]} cy={y} r={2.2} className={color} fill="currentColor" />
            </g>
          )
        })}
      </svg>
      <div className="mt-1 flex items-center justify-between font-mono text-[8px] text-muted-foreground/60">
        <span>{points[0].week_start.slice(5)}</span>
        <span>{points[points.length - 1].week_start.slice(5)}</span>
      </div>
    </div>
  )
}
function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined) return '—'
  return `${(n * 100).toFixed(digits)}%`
}
function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return '—'
  return n.toFixed(digits)
}
function MissedList({ rows, loading }: { rows: MissedRow[]; loading: boolean }) {
  if (loading) return <div className="px-2 py-3 font-mono text-[11px] text-muted-foreground">Loading...</div>
  if (rows.length === 0) return (
    <div className="rounded border border-muted-foreground/20 bg-muted/10 px-3 py-4 font-mono text-[11px] text-muted-foreground">
      No missed signals in window. 归隐 K 线没给过非 NEUTRAL 信号, 或全部被采纳。
    </div>
  )
  return (
    <div className="overflow-x-auto">
      <table className="w-full font-mono text-[11px]">
        <thead>
          <tr className="border-b border-muted-foreground/30 text-left text-[10px] uppercase text-muted-foreground/80">
            <th className="px-2 py-1">Date</th>
            <th className="px-2 py-1">Asset</th>
            <th className="px-2 py-1">Pattern</th>
            <th className="px-2 py-1">Signal</th>
            <th className="px-2 py-1 text-right">Conf</th>
            <th className="px-2 py-1 text-right">5d Edge</th>
            <th className="px-2 py-1 text-right">20d Edge</th>
            <th className="px-2 py-1 text-center">Hit?</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const hit = r.hit
            const hitColor = hit === 1 ? "text-long" : hit === 0 ? "text-short" : "text-muted-foreground/60"
            return (
              <tr key={r.id} className="border-b border-muted-foreground/10 hover:bg-muted/20">
                <td className="px-2 py-1">{r.decision_date}</td>
                <td className="px-2 py-1 font-semibold">{r.asset}</td>
                <td className="px-2 py-1">{PATTERN_LABELS[r.pattern_type] ?? r.pattern_type}</td>
                <td className={cn("px-2 py-1", r.signal === "CAUTION" ? "text-short" : "text-long")}>
                  {r.signal}
                </td>
                <td className="px-2 py-1 text-right">{fmtNum(r.confidence_at_time)}</td>
                <td className="px-2 py-1 text-right">{fmtPct(r.actual_outcome_5d, 2)}</td>
                <td className="px-2 py-1 text-right">{fmtPct(r.actual_outcome_20d, 2)}</td>
                <td className={cn("px-2 py-1 text-center font-semibold", hitColor)}>
                  {hit === 1 ? "OK" : hit === 0 ? "X" : "-"}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function CalibrationTable({ rows, loading }: { rows: CalibrationRow[]; loading: boolean }) {
  if (loading) return <div className="px-2 py-3 font-mono text-[11px] text-muted-foreground">Loading...</div>
  if (rows.length === 0) return (
    <div className="rounded border border-muted-foreground/20 bg-muted/10 px-3 py-4 font-mono text-[11px] text-muted-foreground">
      No calibration data yet. judgment_review 周一跑一次, 需要至少 1 周 missed_signals 结算后才会有数据。
    </div>
  )
  return (
    <div className="overflow-x-auto">
      <table className="w-full font-mono text-[11px]">
        <thead>
          <tr className="border-b border-muted-foreground/30 text-left text-[10px] uppercase text-muted-foreground/80">
            <th className="px-2 py-1">Week</th>
            <th className="px-2 py-1">Asset</th>
            <th className="px-2 py-1">Pattern</th>
            <th className="px-2 py-1 text-right">N</th>
            <th className="px-2 py-1 text-right">Hit%</th>
            <th className="px-2 py-1 text-right">Conf</th>
            <th className="px-2 py-1 text-right">Calib Gap</th>
            <th className="px-2 py-1">Recommendation</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const recCls = RECO_COLORS[r.recommendation] ?? RECO_COLORS.hold
            return (
              <tr key={r.id} className="border-b border-muted-foreground/10 hover:bg-muted/20">
                <td className="px-2 py-1">{r.week_start}</td>
                <td className="px-2 py-1 font-semibold">{r.asset}</td>
                <td className="px-2 py-1">{PATTERN_LABELS[r.pattern_type] ?? r.pattern_type}</td>
                <td className="px-2 py-1 text-right">{r.n_signals}</td>
                <td className="px-2 py-1 text-right">{fmtPct(r.hit_rate, 0)}</td>
                <td className="px-2 py-1 text-right">{fmtNum(r.avg_confidence)}</td>
                <td className={cn("px-2 py-1 text-right",
                  r.confidence_calibration > 0.1 ? "text-short"
                  : r.confidence_calibration < -0.1 ? "text-long" : "text-muted-foreground")}>
                  {r.confidence_calibration >= 0 ? "+" : ""}
                  {(r.confidence_calibration * 100).toFixed(0)}%
                </td>
                <td className="px-2 py-1">
                  <span className={cn("inline-block rounded border px-1.5 py-0.5 text-[10px]", recCls)}>
                    {r.recommendation}
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function QualityView({ data, loading, byDim }: { data: QualityResp | null; loading: boolean; byDim?: any }) {
  if (loading) return <div className="px-2 py-3 font-mono text-[11px] text-muted-foreground">Loading...</div>
  if (!data || data.ok === false) return (
    <div className="rounded border border-short/40 bg-short/10 px-3 py-4 font-mono text-[11px] text-short">
      Error: {data?.error ?? "no data"}
    </div>
  )
  const overall = data.overall ?? {}
  const gap = overall.overconfidence_gap
  const gapColor = gap === undefined ? "text-muted-foreground"
    : gap > 0.1 ? "text-short" : gap < -0.1 ? "text-long" : "text-muted-foreground"
  const gapText = gap === undefined ? "-"
    : gap > 0 ? `+${(gap * 100).toFixed(0)}% 过度自信`
    : `${(gap * 100).toFixed(0)}% 谨慎`
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="rounded border border-muted-foreground/30 bg-card px-3 py-2">
          <div className="text-[10px] uppercase text-muted-foreground/80">总样本</div>
          <div className="font-mono text-lg font-semibold">{overall.n ?? 0}</div>
        </div>
        <div className="rounded border border-muted-foreground/30 bg-card px-3 py-2">
          <div className="text-[10px] uppercase text-muted-foreground/80">总命中</div>
          <div className="font-mono text-lg font-semibold text-long">{overall.n_hit ?? 0}</div>
        </div>
        <div className="rounded border border-muted-foreground/30 bg-card px-3 py-2">
          <div className="text-[10px] uppercase text-muted-foreground/80">命中率</div>
          <div className="font-mono text-lg font-semibold">{fmtPct(overall.hit_rate, 1)}</div>
        </div>
        <div className="rounded border border-muted-foreground/30 bg-card px-3 py-2">
          <div className="text-[10px] uppercase text-muted-foreground/80">平均置信</div>
          <div className={cn("font-mono text-lg font-semibold", gapColor)}>
            {fmtPct(overall.avg_confidence, 1)}
          </div>
        </div>
      </div>
      <div className={cn("rounded border bg-card px-3 py-2", gapColor)}>
        <div className="text-[10px] uppercase opacity-80">Confidence Calibration Gap</div>
        <div className="font-mono text-base font-semibold">{gapText}</div>
        <div className="mt-1 text-[10px] text-muted-foreground/80">
          gap = avg_confidence − hit_rate. 正值说明我们比实际命中的更自信, 应降低 signal weight.
        </div>
      </div>
      <TrendChart points={data.trend ?? []} />
      <div className="rounded border border-muted-foreground/30 bg-card px-3 py-2">
        <div className="mb-2 text-[11px] font-semibold uppercase text-muted-foreground">By Pattern</div>
        {(data.by_pattern ?? []).length === 0 ? (
          <div className="font-mono text-[11px] text-muted-foreground">
            还没有已结算的 missed_signals.
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {data.by_pattern!.map((p) => (
              <div key={p.pattern_type} className="flex items-center justify-between rounded bg-muted/20 px-2 py-1 font-mono text-[11px]">
                <span className="font-semibold">{PATTERN_LABELS[p.pattern_type] ?? p.pattern_type}</span>
                <span className="flex items-center gap-3">
                  <span className="text-muted-foreground">n={p.n}</span>
                  <span className="text-long">hit={fmtPct(p.hit_rate, 0)}</span>
                  <span className={cn(
                    p.confidence_calibration > 0.1 ? "text-short"
                    : p.confidence_calibration < -0.1 ? "text-long" : "text-muted-foreground")}>
                    cal={p.confidence_calibration >= 0 ? "+" : ""}
                    {(p.confidence_calibration * 100).toFixed(0)}%
                  </span>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
      {/* v2026.10.15: by-dimension (3 维度: A 买卖信号 / B 信息方向 / C 技术错误)
          数据源 trade_reflections_v2, 即使 by_pattern=0 也能展示真实样本 */}
      {byDim && byDim.ok && (() => {
        const dims = byDim.dimensions || {}
        const totals = byDim.totals || {}
        // 显式标注 A/B/C 联合 — `keyof typeof dims` 推成 string|number|symbol,
        // 与 React.key 不兼容(symbol 被拒)
        const order: Array<'A' | 'B' | 'C'> = ['A', 'B', 'C']
        return (
          <div className="rounded border border-muted-foreground/30 bg-card px-3 py-2">
            <div className="mb-2 flex items-baseline justify-between">
              <div className="text-[11px] font-semibold uppercase text-muted-foreground">
                By Dimension (trade_reflections_v2)
              </div>
              <div className="font-mono text-[10px] text-muted-foreground">
                总样本 {totals.grand_total ?? 0}
              </div>
            </div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {order.map((k) => {
                const d = dims[k]
                if (!d) return null
                const n = d.total ?? 0
                const subs = d.by_subcategory || {}
                return (
                  <div key={k} className="rounded border border-muted-foreground/20 bg-muted/10 p-2">
                    <div className="flex items-center justify-between font-mono text-[10px]">
                      <span className={cn("font-semibold",
                        k === 'A' ? "text-long" : k === 'B' ? "text-short" : "text-foreground")}>
                        {k}. {d.label}
                      </span>
                      <span className="text-muted-foreground">n={n}</span>
                    </div>
                    <div className="mt-1 flex flex-col gap-0.5 font-mono text-[10px]">
                      {Object.entries(subs).map(([sub, info]: [string, any]) => (
                        <div key={sub} className="flex items-center justify-between">
                          <span className="truncate text-foreground/80">{sub}</span>
                          <span className="text-muted-foreground">{info?.n ?? 0}</span>
                        </div>
                      ))}
                      {Object.keys(subs).length === 0 && (
                        <div className="text-muted-foreground/60">无样本</div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )
      })()}
    </div>
  )
}

export type MissedSignalsPanelProps = {
  refreshMs?: number
  className?: string
  // v2026.10.15: page 层统一轮询时点 (number 改变时强制 fetchAll 一次)
  externalRefreshToken?: number
}
export function MissedSignalsPanel({ refreshMs = 120000, className, externalRefreshToken }: MissedSignalsPanelProps) {
  // 默认 tab = "missed" (漏检信号):
  //  1. panel 名字就是 "Missed Signal Reflection", 与主内容对齐
  //  2. 与 test(components/v237/MissedSignalsPanel.test.tsx:"renders 3 tabs and
  //     shows missed list by default") 一致 — 之前 default = "quality" 时 5 个测试全挂
  //  3. quality tab 用 judgment-quality 端点(无 by-dim 时 N=0),数据更稀疏,
  //     让用户先看 missed 列表再点 quality 更直观
  const [tab, setTab] = useState<TabKey>("missed")
  const [missed, setMissed] = useState<MissedRow[]>([])
  const [calibration, setCalibration] = useState<CalibrationRow[]>([])
  const [quality, setQuality] = useState<QualityResp | null>(null)
  const [byDim, setByDim] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const fetchAll = useMemo(
    () => async () => {
      setLoading(true)
      try {
        const [m, c, q, bd] = await Promise.all([
          fetch("/api/v237/missed-signals?limit=200&days=90").then((r) => r.json()),
          fetch("/api/v237/calibration?weeks=12").then((r) => r.json()),
          fetch("/api/v237/judgment-quality").then((r) => r.json()),
          // v2026.10.15 bugfix: 也拉 by-dimension (trade_reflections_v2 表有真实 163 条数据,
          // 比 missed_signals 表的 0 数据有用). quality 卡片展示.
          fetch("/api/v237/missed-signals/by-dimension?days=90").then((r) => r.json()),
        ])
        if (m && m.ok) setMissed(m.rows ?? [])
        if (c && c.ok) setCalibration(c.rows ?? [])
        setQuality(q ?? { ok: false, error: "no response" })
        if (bd && bd.ok) setByDim(bd)
        setLastUpdated(Date.now())
      } catch (e) {
        setQuality({ ok: false, error: e instanceof Error ? e.message : String(e) })
      } finally {
        setLoading(false)
      }
    }, [])
  useEffect(() => {
    // v302+ P0 优化: 如果有 externalRefreshToken (page 在推统一 tick),
    // 不再启动内部 setInterval, 避免与 page 推的 tick 重复 poll (60s + 60s = ~30s 平均一次)
    void fetchAll()
    if (externalRefreshToken !== undefined) {
      // page 推时由下面的 externalRefreshToken useEffect 负责拉取
      return
    }
    const id = setInterval(() => void fetchAll(), refreshMs)
    return () => clearInterval(id)
    // fetchAll 是 useMemo([]) 稳定的; externalRefreshToken 仅用来决定要不要建 setInterval,
    // 不进 deps 避免每次 page tick 重新挂载定时器(fetch 仍由下面的 useEffect 负责)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchAll, refreshMs])
  // v2026.10.15: 响应 page 层统一 tick. externalRefreshToken 改变时立即拉取,
  // 让 4 个 panel 错峰问题变同点拉取. 用 > 0 过滤初始 0 不触发.
  useEffect(() => {
    if (externalRefreshToken && externalRefreshToken > 0) void fetchAll()
    // fetchAll 在 useMemo([]) 已稳定, 不会因它再触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externalRefreshToken])
  return (
    <div className={cn("flex flex-col gap-2 rounded border border-muted-foreground/30 bg-card p-3", className)}>
      <div className="flex items-center justify-between">
        <h3 className="font-mono text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
          Missed Signal Reflection (v237)
        </h3>
        <div className="flex items-center gap-1">
          {TABS.map((t) => (
            <button key={t.key} type="button" onClick={() => setTab(t.key)}
              className={cn("rounded border px-2 py-0.5 font-mono text-[10px] transition",
                tab === t.key
                  ? "border-foreground/60 bg-foreground/10 text-foreground"
                  : "border-muted-foreground/30 bg-transparent text-muted-foreground hover:bg-muted/30")}>
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-[120px]">
        {tab === "missed" && <MissedList rows={missed} loading={loading} />}
        {tab === "calibration" && <CalibrationTable rows={calibration} loading={loading} />}
        {tab === "quality" && <QualityView data={quality} loading={loading} byDim={byDim} />}
      </div>
      <div className="flex items-center justify-between font-mono text-[9px] text-muted-foreground/60">
        <span>auto-refresh {Math.round(refreshMs / 1000)}s</span>
        {lastUpdated !== null && (
          <span>Updated {new Date(lastUpdated).toISOString().slice(11, 19)} UTC</span>
        )}
      </div>
    </div>
  )
}
