'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { FlowOverviewCharts, type FlowMetric } from './flows-charts'

// 资金流动页 — 数据来自 /api/macro/context（macro_context 的免费公开源聚合）：
//   币安/OKX/Bybit/Bitget 永续（多空比·持仓量·资金费率·主动买卖比）
//   CFTC COT 持仓（COMEX 黄金 / CME 日元 投机·商业净头寸，周度）
//   SPDR GLD 黄金 ETF 持仓（日度）· NY Fed EFFR · FRED 美元流动性 · Fear&Greed 等
// 全部走公开接口，失败标 unavailable（不造数），无需外部付费 key。

type MacroItem = {
  ts?: number
  category: string
  source: string
  metric_key: string
  value: number | null
  unit: string
  status: string
  payload: {
    label?: string
    note?: string
    observation_date?: string
    report_date?: string
    as_of?: string
    freshness?: string
    [key: string]: unknown
  }
}

// refresh=0 返回 { status, items }；refresh=1 返回 build_context 的 { status, layers:{...}, ok, total, ts }
function extractItems(data: unknown): MacroItem[] {
  if (!data || typeof data !== 'object') return []
  const d = data as Record<string, unknown>
  if (Array.isArray(d.items)) return d.items as MacroItem[]
  if (d.layers && typeof d.layers === 'object') {
    const out: MacroItem[] = []
    for (const rows of Object.values(d.layers as Record<string, unknown>)) {
      if (Array.isArray(rows)) out.push(...(rows as MacroItem[]))
    }
    return out
  }
  return []
}

// 快照按 id desc 含历史记录，同 metric_key 只保留最新一条
function dedupe(items: MacroItem[]): MacroItem[] {
  const seen = new Set<string>()
  const out: MacroItem[] = []
  for (const it of items) {
    if (seen.has(it.metric_key)) continue
    seen.add(it.metric_key)
    out.push(it)
  }
  return out
}

const SECTIONS: { key: string; title: string; desc: string }[] = [
  { key: 'flow', title: '资金流向', desc: '永续主动买卖比 / CVD 净买盘 / BTC ETF 净流入' },
  { key: 'cot', title: 'CFTC 全品类机构持仓', desc: '贵金属/能源/股指/债券利率/外汇/加密 投机·商业净头寸（周度）' },
  { key: 'gold_etf', title: '黄金 ETF 持仓', desc: 'SPDR GLD 实物持仓与日变化（日度）' },
  { key: 'structure', title: '盘面结构', desc: '资金费率 / 持仓量 OI / 多空比' },
  { key: 'liquidation', title: '爆仓清算', desc: '多空爆仓力量对比' },
  { key: 'coinglass', title: 'Coinglass 机构资金', desc: 'Coinglass 付费墙 → 免费单所口径替代（币安资金费率/OI + OKX 爆仓）' },
  { key: 'fed', title: '美联储预期', desc: 'NY Fed EFFR + 联邦基金期货隐含利率' },
  { key: 'fred', title: '全网宏观基准 (FRED)', desc: '美债全期限收益率 / 信用利差 / 美股指数 / 美元汇率 / 商品现货（官方免费）' },
  { key: 'sentiment', title: '市场情绪', desc: 'Crypto Fear & Greed' },
  { key: 'options', title: '期权市场', desc: 'Deribit 期权偏斜' },
  { key: 'onchain', title: '链上资金', desc: 'BTC 哈希率 / 难度（日度）' },
  { key: 'rotation', title: '资金轮动', desc: 'BTC 市占率 / 全市场市值' },
  { key: 'breadth', title: '市场宽度', desc: '涨跌家数 / ETH 市占率' },
  { key: 'equity', title: '权益资金', desc: '美股 ETF 资金流（需 FMP key）' },
  { key: 'eia', title: 'EIA 能源', desc: '原油 / 汽油周度库存（需 EIA key）' },
]

const METRIC_LABELS: Record<string, string> = {
  'btc_etf_net': 'BTC ETF 净流入',
  'gold.etf_holdings': 'GLD 持仓',
  'gold.etf_holdings_chg': 'GLD 持仓变化',
  'margin_debt': '美股保证金债务(杠杆资金)',
  'us_equity_mcap': '美股总市值(公开股权)',
}

const SUFFIX_LABELS: Record<string, string> = {
  taker_buy_sell: '主动买卖比',
  cvd: 'CVD 净买盘',
  funding: '资金费率',
  funding_bybit: '资金费率 (Bybit)',
  funding_bitget: '资金费率 (Bitget)',
  oi: '持仓量',
  oi_bybit: '持仓量 (Bybit)',
  ls_ratio: '多空比',
  top_ls_ratio: '大户多空比',
  basis: '基差',
  cot_noncomm_net: '投机净头寸',
  cot_noncomm_chg: '投机净头寸变化',
  cot_comm_net: '商业净头寸',
  cot_spec_ratio: '投机多空比',
}

function titleFor(item: MacroItem): string {
  const key = item.metric_key
  const exact = METRIC_LABELS[key]
  if (exact) return exact
  const label = item.payload?.label?.trim()
  const suffix = key.includes('.') ? key.slice(key.lastIndexOf('.') + 1) : key
  const mapped = SUFFIX_LABELS[suffix]
  if (mapped) return label ? `${label} ${mapped}` : mapped
  return label ? `${label} ${key}` : key
}

function fmtUsd(v: number): string {
  const a = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(1)}K`
  return `${sign}$${a.toFixed(0)}`
}

function fmtNum(v: number): string {
  if (Number.isInteger(v)) {
    return Math.abs(v) >= 10000 ? v.toLocaleString() : String(v)
  }
  const a = Math.abs(v)
  if (a < 1) return v.toFixed(4)
  if (a < 100) return v.toFixed(2)
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function fmtValue(item: MacroItem): string {
  if (item.status !== 'ok' || item.value === null || item.value === undefined) return 'N/A'
  const v = item.value
  switch (item.unit || '') {
    case '%':
      return `${v.toFixed(2)}%`
    case 'x':
      return `${v.toFixed(2)}×`
    case '吨':
      return `${v.toFixed(1)} 吨`
    case '张':
      return `${Math.round(v).toLocaleString()} 张`
    case 'usd':
      return fmtUsd(v)
    case '千人':
      return `${Math.round(v).toLocaleString()} 千人`
    case 'bp':
      return `${v.toFixed(1)} bp`
    case '百万美元':
      return fmtUsd(v * 1e6)
    case '':
      return fmtNum(v)
    default:
      return `${fmtNum(v)}${item.unit}`
  }
}

// 涨跌/多空语义着色：资金流维度值 >0 偏流入看多（绿），<0 偏流出看空（红）
type Tone = 'long' | 'short' | 'neutral'

function toneOf(item: MacroItem): Tone {
  if (item.status !== 'ok' || item.value === null || item.value === undefined) return 'neutral'
  const k = item.metric_key.toLowerCase()
  const v = item.value
  if (k.includes('ratio')) return v > 1 ? 'long' : v < 1 ? 'short' : 'neutral'
  if (k.includes('funding')) return v > 0 ? 'long' : v < 0 ? 'short' : 'neutral'
  if (/net|cvd|_chg|inflow|flow/.test(k)) return v > 0 ? 'long' : v < 0 ? 'short' : 'neutral'
  return 'neutral'
}

function metaText(item: MacroItem): string {
  const p = item.payload ?? {}
  const parts: string[] = []
  if (p.freshness) parts.push(String(p.freshness))
  if (p.report_date) parts.push(String(p.report_date))
  if (p.as_of) parts.push(String(p.as_of))
  if (p.observation_date) parts.push(String(p.observation_date))
  if (item.status !== 'ok' && p.note) parts.push(String(p.note))
  return parts.join(' · ')
}

function fmtTs(ts: number | null): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

export function FlowsPanel() {
  const [items, setItems] = useState<MacroItem[]>([])
  const [meta, setMeta] = useState<{ status: string; ok?: number; total?: number } | null>(null)
  const [lastTs, setLastTs] = useState<number | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  // refresh=0 读沉淀快照（轻量）
  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/macro/context`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`macro/context ${r.status}`)
      const data = (await r.json()) as { status?: string; ok?: number; total?: number; ts?: number }
      const its = extractItems(data)
      if (its.length) {
        setItems(its)
        setMeta({ status: data.status ?? 'ok', ok: data.ok, total: data.total })
        const maxTs = its.reduce((acc, it) => Math.max(acc, it.ts ?? 0), 0)
        setLastTs(maxTs || (data.ts ?? null))
        setNote(null)
      } else {
        setItems([])
        setMeta(null)
        setNote('快照为空：引擎尚未沉淀资金流数据，可点击右上角「拉取真实资金流」实时聚合（免费公开源）。')
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  // refresh=1 实时打公开源并落库（免费：币安/OKX/CFTC/SPDR/NY Fed/Fear&Greed；需 key 的源自动标 unavailable）
  const refresh = useCallback(async () => {
    setRefreshing(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/macro/context?refresh=1`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`macro/context?refresh=1 ${r.status}`)
      const data = (await r.json()) as {
        status?: string
        ok?: number
        total?: number
        ts?: number
      }
      const its = extractItems(data)
      if (its.length) {
        setItems(its)
        setMeta({ status: data.status ?? 'ok', ok: data.ok, total: data.total })
        setLastTs(data.ts ?? null)
        setNote(null)
      } else {
        await load()
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setRefreshing(false)
    }
  }, [load])

  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(), 30_000)
    return () => clearInterval(timer)
  }, [load])

  const grouped = useMemo(() => {
    const deduped = dedupe(items)
    const map = new Map<string, MacroItem[]>()
    for (const it of deduped) {
      const arr = map.get(it.category) ?? []
      arr.push(it)
      map.set(it.category, arr)
    }
    return SECTIONS.map((s) => {
      const sItems = map.get(s.key) ?? []
      // cot 按品种品类（payload.group）做二级分组；其余 section 保持平铺
      const groups = new Map<string, MacroItem[]>()
      if (s.key === 'cot') {
        for (const it of sItems) {
          const g = (it.payload?.group as string) || '其它'
          const arr = groups.get(g) ?? []
          arr.push(it)
          groups.set(g, arr)
        }
      }
      return { ...s, items: sItems, groups }
    }).filter((s) => s.items.length > 0)
  }, [items])

  const okCount = items.filter((it) => it.status === 'ok' && it.value != null).length

  // 图表用去重后的最新快照（MacroItem 结构兼容 FlowMetric）
  const flowItems = useMemo<FlowMetric[]>(() => dedupe(items), [items])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-mono text-sm uppercase tracking-widest text-muted-foreground">
            资金流动
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            跨资产资金流向与持仓结构：<span className="text-long">绿=净流入/多方主导</span>、
            <span className="text-short">红=净流出/空方主导</span>。数据来自公开接口聚合（币安/OKX 永续、CFTC COT、SPDR
            GLD、NY Fed、FRED 等），失败标 N/A 不造数。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {meta && (
            <span className="font-mono text-[11px] text-muted-foreground">
              可用 {meta.ok ?? okCount}/{meta.total ?? items.length} · 数据时间 {fmtTs(lastTs)}
            </span>
          )}
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={refreshing}
            className="rounded border border-primary/40 bg-primary/10 px-3 py-1.5 font-mono text-xs text-primary disabled:opacity-60"
          >
            {refreshing ? '聚合中…' : '拉取真实资金流'}
          </button>
        </div>
      </div>

      {loading && items.length === 0 && (
        <div className="font-mono text-xs text-muted-foreground">加载中…</div>
      )}
      {error && (
        <div className="mb-4 rounded border border-short/40 bg-short/10 px-3 py-2 font-mono text-xs text-short">
          {error}
        </div>
      )}
      {note && items.length === 0 && (
        <div className="mb-4 rounded border border-border bg-card px-3 py-2 font-mono text-xs text-muted-foreground">
          {note}
        </div>
      )}

      <div className="mb-4">
        <FlowOverviewCharts items={flowItems} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {grouped.map((section) => {
          const renderCard = (it: MacroItem) => {
            const tone = toneOf(it)
            const cls =
              tone === 'long' ? 'text-long' : tone === 'short' ? 'text-short' : 'text-foreground'
            const ok = it.status === 'ok' && it.value != null
            return (
              <div
                key={it.metric_key}
                className={`rounded border p-2 ${
                  ok ? 'border-border bg-card' : 'border-border/60 bg-card/40'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-[10px] text-muted-foreground">
                    {titleFor(it)}
                  </span>
                  <span className="shrink-0 font-mono text-[9px] uppercase text-muted-foreground/60">
                    {it.source}
                  </span>
                </div>
                <div className={`mt-1 font-mono text-base ${cls}`}>
                  <b>{fmtValue(it)}</b>
                </div>
                {metaText(it) && (
                  <div
                    className="mt-0.5 truncate font-mono text-[9px] text-muted-foreground/80"
                    title={metaText(it)}
                  >
                    {metaText(it)}
                  </div>
                )}
              </div>
            )
          }
          return (
            <section key={section.key} className="surface-card p-3">
              <div className="mb-2 flex items-baseline justify-between gap-2">
                <h3 className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
                  {section.title}
                </h3>
                <span className="shrink-0 font-mono text-[9px] text-muted-foreground/70">
                  {section.key}
                </span>
              </div>
              <p className="mb-2 font-mono text-[10px] text-muted-foreground/80">{section.desc}</p>
              {section.groups && section.groups.size > 0 ? (
                Array.from(section.groups.entries()).map(([g, gItems]) => (
                  <div key={g} className="mb-3 last:mb-0">
                    <h4 className="mb-1.5 mt-1 border-b border-border/50 pb-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-primary/80">
                      {g}
                    </h4>
                    <div className="grid gap-1.5 sm:grid-cols-2 xl:grid-cols-3">
                      {gItems.map(renderCard)}
                    </div>
                  </div>
                ))
              ) : (
                <div className="grid gap-1.5 sm:grid-cols-2 xl:grid-cols-3">
                  {section.items.map(renderCard)}
                </div>
              )}
            </section>
          )
        })}
      </div>

      {grouped.length === 0 && !loading && !note && !error && (
        <div className="font-mono text-xs text-muted-foreground">暂无资金流数据。</div>
      )}
    </main>
  )
}