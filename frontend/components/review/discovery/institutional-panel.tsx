'use client'

/* ════════════════════════════════════════════════════════════════════════
 * 🏛 知名投资人 13F 持仓（/discover 段）
 *
 * 为什么跟「聪明钱」分开放：
 *   聪明钱看的是 Hyperliquid 链上钱包的分钟级成交；巴菲特、阿克曼、Burry
 *   这些人根本不上链、没有 0x 地址。他们唯一可核验的公开口径是 SEC 的
 *   13F-HR 季度申报（季末后 45 天内提交），所以这里单独做一个模块，
 *   金额、持仓数、季环比全部来自 SEC 原始 XML，不做任何推算。
 *
 * 展示口径：
 *   · 只显示最近两期，环比按 CUSIP 对齐（同一 CUSIP 的增减才是真加/减仓）；
 *   · 申报日期超过 7 个月打「已过期」标记 —— 别把旧季报当实时信号；
 *   · 期权（put/call）单独标注，不与正股混算。
 * ════════════════════════════════════════════════════════════════════════ */

import { useEffect, useMemo, useState } from 'react'
import { Building2, ChevronDown, ChevronUp, ExternalLink, RefreshCw, Users } from 'lucide-react'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/utils'

const POLL_MS = 10 * 60_000

type TopHolding = {
  issuer: string
  value_usd: number
  weight: number
  qoq_usd: number
  is_new: boolean
}

type Manager = {
  manager: string
  person: string
  cik: string
  report_date: string
  filed_date: string
  total_usd: number
  positions: number
  top10_weight: number
  top_holdings: TopHolding[]
  prev_report_date: string | null
  new_positions: string[]
  exited_positions: string[]
  stale: boolean
  has_prior: boolean
}

type Resp = {
  managers: Manager[]
  count: number
  as_of: string | null
  source: string
}

type Consensus = {
  issuer: string
  holder_count: number
  value_usd: number
  holders: string[]
}

const fmtUsd = (v: number): string => {
  if (!v) return '—'
  if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(2)}B`
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(1)}M`
  if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(0)}K`
  return `$${v.toFixed(0)}`
}

const fmtPct = (v: number): string => `${(v * 100).toFixed(1)}%`

export function InstitutionalPanel() {
  const [data, setData] = useState<Resp | null>(null)
  const [consensus, setConsensus] = useState<Consensus[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<'size' | 'date'>('size')

  const load = async () => {
    setLoading(true)
    try {
      const [r, c] = await Promise.all([
        apiGet<Resp>('/api/institutional/13f/managers'),
        apiGet<{ consensus: Consensus[] }>('/api/institutional/13f/consensus?limit=10'),
      ])
      if (r?.managers) {
        setData(r)
        setErr(null)
      } else {
        setErr('接口无数据')
      }
      if (c?.consensus) setConsensus(c.consensus)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '拉取失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    const t = setInterval(() => void load(), POLL_MS)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const sorted = useMemo(() => {
    const list = data?.managers ?? []
    return [...list].sort((a, b) =>
      sortKey === 'size' ? b.total_usd - a.total_usd : b.report_date.localeCompare(a.report_date),
    )
  }, [data, sortKey])

  return (
    <div className="space-y-3">
      {/* 口径说明 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border/60 bg-secondary/20 px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground/80">
        <span className="flex items-center gap-1 text-foreground/80">
          <Building2 className="h-3 w-3" /> SEC EDGAR 13F-HR
        </span>
        <span>季度申报 · 季末后 45 天内 · 非实时</span>
        <span>金额取 SEC XML 原始 value；个别机构按千美元申报，已按每股隐含价自动识别换算</span>
        {data?.as_of && <span className="text-foreground/70">最新季末 {data.as_of}</span>}
        <span className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setSortKey(sortKey === 'size' ? 'date' : 'size')}
            className="rounded border border-border px-1.5 py-0.5 hover:bg-secondary"
          >
            按{sortKey === 'size' ? '规模' : '申报日'}排序
          </button>
          <button
            onClick={() => void load()}
            className="flex items-center gap-1 rounded border border-border px-1.5 py-0.5 hover:bg-secondary"
          >
            <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} /> 刷新
          </button>
        </span>
      </div>

      {err && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 text-[12px] text-amber-300">
          {err}
        </div>
      )}

      {/* 共识持仓 */}
      {consensus.length > 0 && (
        <div className="rounded-md border border-border/60 bg-secondary/10 p-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 text-[12px] font-medium">
            <Users className="h-3.5 w-3.5" /> 跨经理共识持仓
            <span className="font-mono text-[11px] font-normal text-muted-foreground">
              按持有家数排序 · 共 {data?.count ?? 0} 位
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {consensus.map((c) => (
              <span
                key={c.issuer + c.value_usd}
                className="rounded border border-border/60 bg-background/60 px-2 py-0.5 font-mono text-[11px]"
                title={c.holders.join(' / ')}
              >
                {c.issuer}
                <span className="ml-1.5 text-primary">{c.holder_count}家</span>
                <span className="ml-1 text-muted-foreground">{fmtUsd(c.value_usd)}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 经理卡片 */}
      {loading && !data ? (
        <div className="py-6 text-center text-[12px] text-muted-foreground">拉取 SEC 13F…</div>
      ) : (
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {sorted.map((m) => {
            const open = expanded === m.manager
            return (
              <div key={m.manager} className="rounded-md border border-border/60 bg-background/40 p-2.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-[13px] font-medium">{m.manager}</div>
                    <div className="font-mono text-[11px] text-muted-foreground">
                      {m.report_date} 季末 · {m.filed_date} 申报
                    </div>
                  </div>
                  {m.stale && (
                    <span className="shrink-0 rounded border border-amber-500/50 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] text-amber-300">
                      旧季报
                    </span>
                  )}
                </div>

                <div className="mt-2 flex items-baseline gap-3 font-mono">
                  <span className="text-[18px] font-semibold text-foreground">{fmtUsd(m.total_usd)}</span>
                  <span className="text-[11px] text-muted-foreground">{m.positions} 只</span>
                  <span className="text-[11px] text-muted-foreground">前10占 {fmtPct(m.top10_weight)}</span>
                </div>

                {/* 前五大 + 权重条 */}
                <div className="mt-2 space-y-1">
                  {m.top_holdings.slice(0, 4).map((h) => (
                    <div key={h.issuer} className="text-[11px]">
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate">
                          {h.issuer}
                          {h.is_new && <span className="ml-1 text-emerald-400">新进</span>}
                        </span>
                        <span className="shrink-0 font-mono text-muted-foreground">
                          {fmtPct(h.weight)}
                          {m.has_prior && h.qoq_usd !== 0 && (
                            <span className={h.qoq_usd > 0 ? 'ml-1 text-emerald-400' : 'ml-1 text-rose-400'}>
                              {h.qoq_usd > 0 ? '+' : '−'}{fmtUsd(Math.abs(h.qoq_usd)).replace('$', '$')}
                            </span>
                          )}
                        </span>
                      </div>
                      <div className="mt-0.5 h-0.5 w-full rounded bg-secondary/60">
                        <div
                          className="h-0.5 rounded bg-primary/70"
                          style={{ width: `${Math.min(100, h.weight * 100)}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>

                {(m.new_positions.length > 0 || m.exited_positions.length > 0) && (
                  <button
                    onClick={() => setExpanded(open ? null : m.manager)}
                    className="mt-2 flex items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                    本季新进 {m.new_positions.length} · 清仓 {m.exited_positions.length}
                  </button>
                )}

                {open && (
                  <div className="mt-1.5 space-y-1 rounded border border-border/40 bg-secondary/20 p-1.5 text-[11px]">
                    {m.new_positions.length > 0 && (
                      <div>
                        <span className="text-emerald-400">新进：</span>
                        {m.new_positions.join('、')}
                      </div>
                    )}
                    {m.exited_positions.length > 0 && (
                      <div>
                        <span className="text-rose-400">清仓：</span>
                        {m.exited_positions.join('、')}
                      </div>
                    )}
                    <a
                      href={`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${m.cik}&type=13F-HR&dateb=&owner=include&count=40`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-primary hover:underline"
                    >
                      SEC 原始申报 <ExternalLink className="h-3 w-3" />
                    </a>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {!loading && data && data.managers.length === 0 && (
        <div className="py-6 text-center text-[12px] text-muted-foreground">
          13F 表为空，先跑 <span className="font-mono">python3 -m feeds.sec_13f</span>
        </div>
      )}
    </div>
  )
}
