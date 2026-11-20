'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { CapabilityPanel } from '@/components/quant/capability-panel'

// v449 该不该开火 — 把两件事放在一页：
//   1) 13 笔仓位按相关性簇合并，看到底是几注独立下注；
//   2) 扣成本后的净期望矩阵，没有正期望格子就明确"停单"。
// 所有口径都在后端，页面不重算。

type Stat = {
  n: number
  avg: number | null
  median: number | null
  win_rate: number | null
  profit_factor: number | null
  t_stat: number | null
}

type Cell = {
  horizon: string
  net: Stat
  gross: Stat
  cost_to_move: number | null
  verdict: string
}

type Cohort = {
  entries: number
  allow: boolean
  weak?: boolean
  action?: string
  reason: string
  recommended_hold: string | null
  allowed_instruments: string[]
  allowed_cells: Cell[]
  weak_cells: Cell[]
  cells: Cell[]
  best_cell: Cell | null
  stop_assets: { asset: string; net: Stat; verdict: string }[]
  gate_stats?: { input: number; kept: number; asset_filtered: number; no_bars: number }
}

type Policy = {
  cost_bps: number
  allow: boolean
  action: string
  cohort: string
  headline: string
  recommended_hold: string | null
  allowed_instruments: string[]
  criteria: { min_n: number; min_t: number }
  raw: Cohort
  gated: Cohort
  note: string
}

type Fold = {
  fold: number
  start: number
  end: number
  stats: Stat
}

type Candidate = {
  cohort: string
  horizon: string
  pooled: Stat
  skipped: number
  folds: Fold[]
  promotion: {
    pass: boolean
    positive_folds: number
    nonempty_folds: number
    need_positive: number
    worst_fold: number | null
    reasons: string[]
  }
}

type FactoryRep = {
  asset: string
  source: string
  cost_bps: number
  verdict: string
  headline: string
  coverage: {
    bar_from: number
    bar_to: number
    span_days: number
    bars: number
    expected_bars: number
    coverage_ratio: number
    entries: number
    gated: number
  }
  best: Candidate | null
  criteria: { min_n: number; min_t: number; worst_fold_floor: number; exit_grace_s: number }
  note: string
}

type Bet = {
  cluster: string
  direction: string
  raw_positions: number
  assets: string[]
  total_size_usdt: number
  avg_entry: number
  avg_pnl_pct: number
  ids: number[]
}

type Position = {
  id: number
  asset: string
  action: string
  entry_price: number
  size_usdt: number
  pnl_pct: number
}

type Scan = {
  headline: string
  raw_positions: number
  effective_bets: number
  total_exposure_usdt: number
  correlation: { assets: string[]; matrix: Record<string, Record<string, number>>; common_days: number }
  clusters: { cluster: string; assets: string[] }[]
  positions: Position[]
  bets: Bet[]
  warnings: string[]
}

const fmt = (v: number | null | undefined, d = 3) =>
  v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`

const COSTS = [30, 10, 5, 2, 0]

export default function BetsPage() {
  const [cost, setCost] = useState(30)
  const [scan, setScan] = useState<Scan | null>(null)
  const [pol, setPol] = useState<Policy | null>(null)
  const [fac, setFac] = useState<FactoryRep | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback((bps: number) => {
    setBusy(true)
    setErr('')
    // 两个块分开取：期望矩阵要扫全样本，慢的时候不能把"几注"一起卡住
    fetch(`${API_BASE}/bets/scan?window_days=60`)
      .then((r) => r.json())
      .then((s) => { if (s?.ok) setScan(s.data); else setErr(s?.error || '仓位读取失败') })
      .catch((e) => setErr(e instanceof Error ? e.message : '仓位读取失败'))

    fetch(`${API_BASE}/policy/expectancy?days=60&cost=${bps}`, {
      signal: AbortSignal.timeout(45000),
    })
      .then((r) => r.json())
      .then((p) => { if (p?.ok) setPol(p.data); else setErr(p?.error || '期望读取失败') })
      .catch((e) => setErr(e instanceof Error ? e.message : '期望读取失败（稍后刷新）'))

    fetch(`${API_BASE}/factory/report?asset=BTC&days=60&folds=4&cost=${bps}`, {
      signal: AbortSignal.timeout(30000),
    })
      .then((r) => r.json())
      .then((f) => { if (f?.ok) setFac(f.data); })
      .catch(() => setFac(null))
      .finally(() => setBusy(false))
  }, [])

  useEffect(() => { load(cost) }, [cost, load])

  const tone = pol?.allow
    ? 'border-emerald-500/60 text-emerald-400'
    : pol?.action?.includes('观察')
      ? 'border-amber-400/60 text-amber-300'
      : 'border-red-500/60 text-red-400'

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">该不该开火 · 单押注 × 正期望</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            13 笔仓位到底是几注？扣掉成本还有没有正期望？两个问题一起回答 · 只读
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1 font-mono text-[11px]">
          <span className="text-muted-foreground">单边成本</span>
          {COSTS.map((bps) => (
            <button
              key={bps}
              onClick={() => setCost(bps)}
              className={`rounded-sm border px-2 py-1 ${
                cost === bps ? 'border-primary text-primary' : 'border-border text-muted-foreground'
              }`}
            >
              {bps} bps
            </button>
          ))}
          <button
            onClick={() => load(cost)}
            className="rounded-sm border border-border px-2 py-1 text-muted-foreground"
          >
            {busy ? '读取中' : '刷新'}
          </button>
        </div>
      </header>

      {err && <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">{err}</div>}

      {pol && (
        <div className={`surface-card mb-3 border-l-2 p-3 ${tone}`}>
          <div className="flex flex-wrap items-baseline gap-3">
            <span className="font-mono text-lg font-semibold">
              {pol.allow
                ? '可以开火'
                : pol.action === '仅观察仓'
                  ? '停单 · 只观察'
                  : pol.action === '样本不足'
                    ? '样本不足 · 暂停'
                    : '停单'}
            </span>
            <span className="font-mono text-[11px] text-muted-foreground">
              成本 {pol.cost_bps}bps · cohort={pol.cohort} · 门槛 n≥{pol.criteria.min_n}、t≥{pol.criteria.min_t}
            </span>
          </div>
          <div className="mt-1 text-sm">{pol.headline}</div>
          {pol.allow && (
            <div className="mt-1 font-mono text-xs">
              建议持有 {pol.recommended_hold} · 品种 {pol.allowed_instruments.join(' / ') || '—'}
            </div>
          )}
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        {/* 左：几注独立方向 */}
        <section className="surface-card p-3">
          <div className="mb-2 flex items-baseline justify-between">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              仓位合并 · 相关性聚簇
            </span>
            {scan && (
              <span className="font-mono text-[11px] text-muted-foreground">
                {scan.correlation.common_days} 个共同交易日
              </span>
            )}
          </div>

          {scan && (
            <>
              <div className="mb-3 flex flex-wrap gap-2 font-mono text-xs">
                <span className="rounded-sm border border-border px-2 py-1">
                  {scan.raw_positions} 笔仓位
                </span>
                <span className="rounded-sm border border-primary/50 px-2 py-1 text-primary">
                  = {scan.effective_bets} 注独立方向
                </span>
                <span className="rounded-sm border border-border px-2 py-1">
                  总敞口 {scan.total_exposure_usdt.toFixed(0)} USDT
                </span>
              </div>

              <div className="space-y-1.5">
                {scan.bets.map((b) => (
                  <div key={`${b.cluster}-${b.direction}`} className="rounded-sm border border-border/70 p-2">
                    <div className="flex items-center justify-between font-mono text-xs">
                      <span>
                        <span className="text-muted-foreground">{b.cluster}</span>
                        <span className={b.direction === 'BUY' ? 'ml-2 text-long' : 'ml-2 text-short'}>
                          {b.direction}
                        </span>
                        <span className="ml-2 text-muted-foreground">
                          {b.raw_positions} 笔 / {b.total_size_usdt.toFixed(0)}U
                        </span>
                      </span>
                      <span className={b.avg_pnl_pct >= 0 ? 'text-long' : 'text-short'}>
                        {fmt(b.avg_pnl_pct, 2)}
                      </span>
                    </div>
                    <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                      均价 {b.avg_entry} · 单号 {b.ids.join(', ')}
                    </div>
                  </div>
                ))}
              </div>

              {scan.warnings.length > 0 && (
                <ul className="mt-3 space-y-1 text-[11px] text-amber-300">
                  {scan.warnings.map((w) => <li key={w}>· {w}</li>)}
                </ul>
              )}

              <div className="mt-3 overflow-x-auto">
                <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                  60 日相关性
                </div>
                <table className="w-full font-mono text-[10px]">
                  <thead className="text-muted-foreground">
                    <tr className="border-b border-border/60">
                      <th className="py-1 text-left font-normal" />
                      {scan.correlation.assets.map((a) => (
                        <th key={a} className="py-1 text-right font-normal">{a}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {scan.correlation.assets.map((row) => (
                      <tr key={row} className="border-b border-border/30">
                        <td className="py-1 text-muted-foreground">{row}</td>
                        {scan.correlation.assets.map((col) => {
                          const v = scan.correlation.matrix[row]?.[col]
                          const strong = row !== col && Math.abs(v ?? 0) >= 0.7
                          return (
                            <td
                              key={col}
                              className={`py-1 text-right ${strong ? 'text-primary' : 'text-muted-foreground'}`}
                            >
                              {v === undefined ? '—' : v.toFixed(2)}
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>

        {/* 右：正期望矩阵 */}
        <section className="surface-card p-3">
          <div className="mb-2 flex items-baseline justify-between">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              扣成本后的净期望
            </span>
            {pol && (
              <span className="font-mono text-[11px] text-muted-foreground">
                全样本 {pol.raw.entries} 笔
                {pol.gated.entries > 0 ? ` · 同向闸后 ${pol.gated.entries} 笔` : ''}
              </span>
            )}
          </div>

          {pol && (
            <>
              <table className="w-full font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    <th className="py-1.5 text-left font-normal">持有期</th>
                    <th className="py-1.5 text-right font-normal">全样本净</th>
                    <th className="py-1.5 text-right font-normal">同向闸后</th>
                    <th className="py-1.5 text-right font-normal">t</th>
                    <th className="py-1.5 text-right font-normal">胜率</th>
                  </tr>
                </thead>
                <tbody>
                  {(pol.raw.cells || []).map((c) => {
                    const g = (pol.gated.cells || []).find((x) => x.horizon === c.horizon)
                    const ok = (c.net.avg ?? -1) > 0 && (c.net.t_stat ?? 0) >= pol.criteria.min_t
                    return (
                      <tr key={c.horizon} className="border-b border-border/30">
                        <td className="py-1.5">{c.horizon}</td>
                        <td className={`py-1.5 text-right ${(c.net.avg ?? 0) > 0 ? 'text-long' : 'text-short'}`}>
                          {fmt(c.net.avg)}
                        </td>
                        <td className={`py-1.5 text-right ${(g?.net.avg ?? 0) > 0 ? 'text-long' : 'text-short'}`}>
                          {g ? fmt(g.net.avg) : '—'}
                        </td>
                        <td className="py-1.5 text-right text-muted-foreground">
                          {g?.net.t_stat === null || g?.net.t_stat === undefined ? '—' : g.net.t_stat.toFixed(2)}
                        </td>
                        <td className="py-1.5 text-right text-muted-foreground">
                          {g?.net.win_rate === null || g?.net.win_rate === undefined
                            ? '—'
                            : `${(g.net.win_rate * 100).toFixed(0)}%`}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>

              <div className="mt-3 space-y-1 text-[11px]">
                <div className="text-muted-foreground">
                  全样本最好的格子：
                  {pol.raw.best_cell
                    ? ` ${pol.raw.best_cell.horizon} ${fmt(pol.raw.best_cell.net.avg)}/笔（t=${pol.raw.best_cell.net.t_stat?.toFixed(2) ?? '—'}）`
                    : ' —'}
                </div>
                {pol.gated.gate_stats && (
                  <div className="text-muted-foreground">
                    同向闸：输入 {pol.gated.gate_stats.input} → 留 {pol.gated.gate_stats.kept}
                    （非 BTC 过滤 {pol.gated.gate_stats.asset_filtered}、缺 5m 数据 {pol.gated.gate_stats.no_bars}）
                  </div>
                )}
                {pol.raw.stop_assets.length > 0 && (
                  <div className="text-amber-300">
                    显著亏钱的品种：{pol.raw.stop_assets.map((a) => `${a.asset}（t=${a.net.t_stat?.toFixed(2)}）`).join('、')}
                  </div>
                )}
              </div>

              <p className="mt-3 border-t border-border/40 pt-2 text-[10px] leading-relaxed text-muted-foreground">
                {pol.note}
              </p>
            </>
          )}
        </section>
      </div>

      {fac && (
        <section className="surface-card mt-3 p-3">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <div className="font-mono text-xs font-semibold tracking-wider">样本工厂 · 分段验证</div>
            <span className={`font-mono text-[11px] ${
              fac.verdict === '可开火' ? 'text-emerald-400'
                : fac.verdict === '停单' ? 'text-red-400' : 'text-amber-300'
            }`}>
              {fac.asset} · {fac.cost_bps}bps · {fac.verdict}
            </span>
          </div>

          <div className="text-sm">{fac.headline}</div>

          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            来源 {fac.source === 'shadow' ? '真实影子账本' : fac.source === 'live' ? '实盘已结算'
              : fac.source === 'sim' ? '回放模拟（只观察）' : '无'} ·
            5m 覆盖 {fac.coverage.span_days} 天 / {fac.coverage.bars} 根
            （理论 {fac.coverage.expected_bars}，{Math.round(fac.coverage.coverage_ratio * 100)}%）·
            样本 {fac.coverage.entries} 笔 / 同向闸后 {fac.coverage.gated} 笔
          </div>

          {fac.best && fac.best.pooled.n > 0 && (
            <>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full font-mono text-[11px]">
                  <thead className="text-muted-foreground">
                    <tr className="border-b border-border/60 text-left">
                      <th className="py-1 pr-3">段</th>
                      <th className="py-1 pr-3">时间</th>
                      <th className="py-1 pr-3 text-right">n</th>
                      <th className="py-1 pr-3 text-right">净/笔</th>
                      <th className="py-1 pr-3 text-right">胜率</th>
                      <th className="py-1 text-right">t</th>
                    </tr>
                  </thead>
                  <tbody>
                    {fac.best.folds.map((f) => (
                      <tr key={f.fold} className="border-b border-border/30">
                        <td className="py-1 pr-3">F{f.fold}</td>
                        <td className="py-1 pr-3 text-muted-foreground">
                          {new Date(f.start * 1000).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })}
                          –{new Date(f.end * 1000).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })}
                        </td>
                        <td className="py-1 pr-3 text-right">{f.stats.n}</td>
                        <td className={`py-1 pr-3 text-right ${(f.stats.avg ?? 0) > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {fmt(f.stats.avg, 3)}
                        </td>
                        <td className="py-1 pr-3 text-right">{f.stats.win_rate === null ? '—' : `${Math.round(f.stats.win_rate * 100)}%`}</td>
                        <td className="py-1 text-right">{f.stats.t_stat?.toFixed(2) ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-muted-foreground">
                <span>
                  {fac.best.cohort === 'gated' ? '同向闸后' : '全样本'} · {fac.best.horizon} ·
                  合并 {fac.best.pooled.n} 笔 {fmt(fac.best.pooled.avg)}/笔 · t={fac.best.pooled.t_stat?.toFixed(2) ?? '—'} ·
                  胜率 {fac.best.pooled.win_rate === null ? '—' : `${Math.round(fac.best.pooled.win_rate * 100)}%`}
                </span>
                <span className={fac.best.promotion.pass ? 'text-emerald-400' : 'text-amber-300'}>
                  正分段 {fac.best.promotion.positive_folds}/{fac.best.promotion.nonempty_folds}
                  （需 ≥{fac.best.promotion.need_positive}）·
                  最差段 {fmt(fac.best.promotion.worst_fold)}
                </span>
              </div>
              {!fac.best.promotion.pass && fac.best.promotion.reasons.length > 0 && (
                <div className="mt-1 text-[10px] text-amber-300">
                  没过分段：{fac.best.promotion.reasons.join('；')}
                </div>
              )}
            </>
          )}

          <p className="mt-3 border-t border-border/40 pt-2 text-[10px] leading-relaxed text-muted-foreground">
            {fac.note}
          </p>
        </section>
      )}

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="人与行为能力"
        hint="干预效果追踪 / 人机对比 / 纪律监控 / 决策清单"
        featureIds={['F50','F51','F52','F53']}
      />
      </section>
    </main>
  )
}
