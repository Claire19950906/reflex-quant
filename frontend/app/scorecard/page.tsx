'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { CapabilityPanel } from '@/components/quant/capability-panel'

// v386 成绩单页 — 与 scorecard_share.py 同一口径（同一 collect()），
// 页面看到的数字 = 导出的 HTML 数字 = 对外发布的数字

type Row = Record<string, string | number | null>

type Data = {
  generated_at: string
  news_total: number
  news_24h: number
  decisions: number
  settled: number
  wins: number
  avg_pnl: number
  sum_pnl: number
  winrate: number | null
  funnel: [string, number][]
  gate_reasons: { reason: string; n: number }[]
  errors: { code: string; n: number; avg_w: number }[]
  missed: { kind: string; n: number }[]
  dimensions: { dim: string; n: number }[]
  sources: { name: string; n: number }[]
  muted_now?: number
  muted_total?: number
  ignored?: number
  reversed_shadow?: number
  daily?: { date: string; n: number; avg_pnl: number }[]
  week?: { n: number; wins: number; winrate: number | null; avg_pnl: number }
  prev_week?: { n: number; wins: number; winrate: number | null; avg_pnl: number }
}

function pct(v: number | null, signed = false) {
  if (v === null || v === undefined) return '—'
  return `${signed && v > 0 ? '+' : ''}${signed ? v.toFixed(2) : v.toFixed(1)}%`
}

export default function ScorecardPage() {
  const [data, setData] = useState<Data | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch(`${API_BASE}/v1/scorecard`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) setData(j.data)
        else setError(j?.error || '读取失败')
      })
      .catch((e) => setError(e instanceof Error ? e.message : '读取失败'))
  }, [])

  const funnelMax = data ? Math.max(1, ...data.funnel.map(([, n]) => n)) : 1

  const table = (headers: string[], rows: Row[]) => (
    <table className="w-full font-mono text-[11px]">
      <thead className="text-muted-foreground">
        <tr className="border-b border-border/60">
          {headers.map((h) => <th key={h} className="py-1.5 text-left font-normal">{h}</th>)}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-b border-border/30">
            {headers.map((h, j) => (
              <td key={h} className={`py-1.5 ${j > 0 ? 'text-right' : ''}`}>{r[h] ?? '—'}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4">
        <h1 className="font-mono text-sm font-semibold tracking-wider">信号成绩单</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          数据截止 {data?.generated_at || '…'} · 只读快照 · 不含未来收益承诺
        </p>
      </header>

      {error && (
        <div className="surface-card border border-red-500/30 p-3 text-xs text-red-400">{error}</div>
      )}

      {data && (
        <>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {[
              ['新闻入库', data.news_total.toLocaleString(), `最近 24h ${data.news_24h.toLocaleString()} 条`],
              ['已结算信号', String(data.settled), `共产出 ${data.decisions} 条判断`],
              ['胜率', pct(data.winrate), `${data.wins} 笔盈利`],
              ['平均每笔', pct(data.avg_pnl, true), `累计 ${data.sum_pnl > 0 ? '+' : ''}${data.sum_pnl.toFixed(2)}%`],
            ].map(([k, v, n]) => (
              <div key={k} className="surface-card p-3">
                <div className="font-mono text-[11px] text-muted-foreground">{k}</div>
                <div className="mt-0.5 font-mono text-xl font-semibold">{v}</div>
                <div className="font-mono text-[11px] text-muted-foreground">{n}</div>
              </div>
            ))}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
            <span className="rounded bg-secondary px-2 py-1">当前静默品种 {data.muted_now ?? 0}</span>
            <span className="rounded bg-secondary px-2 py-1">静默累计 {data.muted_total ?? 0} 次</span>
            <span className="rounded bg-secondary px-2 py-1">已忽略 {data.ignored ?? 0} 笔</span>
            <span className="rounded bg-secondary px-2 py-1">反手影子 {data.reversed_shadow ?? 0} 笔</span>
            <span className="text-muted-foreground/60">处置只影响之后写入的信号，不追溯已结算记录</span>
          </div>

          <div className="surface-card mt-3 p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">成交漏斗</div>
            <div className="space-y-2">
              {data.funnel.map(([name, n]) => (
                <div key={name} className="grid grid-cols-[72px_1fr_56px] items-center gap-2 font-mono text-[11px]">
                  <span>{name}</span>
                  <div className="h-1.5 overflow-hidden rounded bg-secondary">
                    <div className="h-full rounded bg-primary" style={{ width: `${(n / funnelMax) * 100}%` }} />
                  </div>
                  <span className="text-right">{n.toLocaleString()}</span>
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
              各层为独立计数：历史写入路径与口径差异会让个别层不严格包含；转化率只在相邻层之间成立。
            </p>
          </div>

          <div className="surface-card mt-3 overflow-x-auto p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              闸门拦截原因（Top 8）
            </div>
            {table(['拦截原因', '条数'], data.gate_reasons.map((g) => ({ 拦截原因: g.reason.slice(0, 90), 条数: g.n })))}
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div className="surface-card overflow-x-auto p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">错误码</div>
              {table(['错误码', '次数', '权重'], data.errors.map((e) => ({ 错误码: e.code, 次数: e.n, 权重: Math.round(e.avg_w) })))}
            </div>
            <div className="surface-card overflow-x-auto p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">漏检分布</div>
              {table(['漏检类型', '条数'], data.missed.map((m) => ({ 漏检类型: m.kind, 条数: m.n })))}
            </div>
            <div className="surface-card overflow-x-auto p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">反思维度</div>
              {table(['维度', '条数'], data.dimensions.map((d) => ({ 维度: d.dim, 条数: d.n })))}
            </div>
            <div className="surface-card overflow-x-auto p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">信息源</div>
              {table(['信息源', '条数'], data.sources.map((s) => ({ 信息源: s.name, 条数: s.n })))}
            </div>
          </div>
        </>
      )}

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="结论可信能力"
        hint="置信度校准 / 事件研究 / 输入快照 / Prompt 回归集"
        featureIds={['F59','F60','F61','F62']}
      />
      </section>
    </main>
  )
}
