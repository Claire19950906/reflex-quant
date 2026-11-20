'use client'

import { Fragment, useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

/**
 * v476 · 输入层体检面板
 *
 * 解决什么问题：v459–v475 这批工作把美债、估值、TIC、链上、分歧度、可信度门槛
 * 一路接进了决策链，但页面上**只有结论**（BUY/HOLD），看不到"这些输入长什么样、
 * 是不是过期了"。分数被打了折、门槛被抬了，用户无从核对。
 *
 * 数据来源（只读，不触发抓取）：
 *   - `decision_fundamental_snapshot.components`：每个品种的分量分数/权重/明细
 *   - `macro_snapshots`：valuation / auction / tic / onchain / defi_tvl / chain_fees /
 *     etf_flow / index_price / fred / equity 各类最近一次读数
 *   - 分歧度按 v472 公式在**后端**重算（库里 dispersion 列尚未落值），前端只展示
 *
 * 三条硬约束：
 *  1. 过期不隐藏：每条读数带 `stale`/`stale_days`，超 7 天直接标「旧」；
 *  2. 缺就是缺：`status != ok` 或 value=null 显示「无数据」，不补 0；
 *  3. 前端不算第二套口径：分歧度/可信度/多空权重全部由后端给。
 */

type Component = {
  name: string
  score: number | null
  weight: number | null
  detail: string
  contrib: number | null
}

type AssetRow = {
  asset: string
  ticker?: string | null
  observed_at?: number | null
  fund_score: number | null
  sizing_mult?: number | null
  dispersion: number | null
  dispersion_level: string | null
  confidence: number | null
  bull_weight: number | null
  bear_weight: number | null
  components_n: number
  source?: string
  available?: boolean
  reason?: string | null
  gaps?: string[]
  components: Component[]
}

type MacroRow = {
  metric_key: string
  value: number | null
  unit: string
  status: string
  observed_at: number | null
  stale_days: number | null
  stale: boolean
  label: string
}

type Payload = {
  ok: boolean
  ts?: number
  assets?: AssetRow[]
  macro?: Record<string, MacroRow[]>
  notes?: string[]
  detail?: string
}

const LEVEL_TONE: Record<string, string> = {
  高: 'text-short',
  中: 'text-amber-400',
  低: 'text-long',
}

const GROUP_LABEL: Record<string, string> = {
  valuation: '股指估值',
  index_price: '指数点位',
  auction: '美债拍卖',
  tic: 'TIC 外资持仓',
  fred: '利率与通胀',
  onchain: 'BTC 链上',
  defi_tvl: 'TVL',
  chain_fees: '链上手续费',
  etf_flow: 'ETF 净流入',
  equity: '公司基本面',
}

function fmtDate(ts?: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)}M`
  return v.toFixed(digits)
}

export function InputLayerPanel({ title = '输入层体检' }: { title?: string }) {
  const [data, setData] = useState<Payload | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const ac = new AbortController()
    setLoading(true)
    setErr(null)
    const timer = setTimeout(() => ac.abort(), 15_000)
    fetch(`${API_BASE}/input-layer/snapshot`, { signal: ac.signal, cache: 'no-store' })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return (await res.json()) as Payload
      })
      .then((d) => {
        if (!d?.ok) throw new Error(d?.detail || '接口返回 ok=false')
        setData(d)
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return
        setErr(e instanceof Error ? e.message : String(e))
        setData(null)
      })
      .finally(() => {
        clearTimeout(timer)
        if (!ac.signal.aborted) setLoading(false)
      })
    return () => {
      clearTimeout(timer)
      ac.abort()
    }
  }, [tick])

  const refresh = useCallback(() => setTick((t) => t + 1), [])

  const assets = (data?.assets || []).filter((a) => a.dispersion !== null)
  const noDisp = (data?.assets || []).filter((a) => a.dispersion === null)
  const macro = data?.macro || {}
  const groups = Object.keys(GROUP_LABEL).filter((k) => (macro[k] || []).length > 0)

  return (
    <section className="rounded-lg border border-border bg-card/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/70 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-2 font-mono text-[12px] font-semibold text-foreground hover:text-primary"
        >
          <span aria-hidden>{open ? '▼' : '▶'}</span>
          <span>{title}</span>
        </button>
        <span className="text-[11px] text-muted-foreground">
          分歧度（v472 口径重算）· 美债 / 估值 / TIC / 链上原始读数 · 只读
        </span>
        <div className="ml-auto flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
          {err ? <span className="text-short">接口未响应</span> : loading ? <span>载入中…</span> : <span>{fmtDate(data?.ts)}</span>}
          <button
            type="button"
            onClick={refresh}
            className="rounded border border-muted-foreground/30 bg-card px-1.5 py-0.5 text-foreground hover:bg-muted/40"
          >
            刷新
          </button>
        </div>
      </div>

      {open ? (
        <div className="px-3 py-2">
          {err ? (
            <div className="rounded border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
              <span className="font-mono">/api/input-layer/snapshot</span> 当前没有响应（{err}）。这里不显示占位数据。
            </div>
          ) : loading ? (
            <div className="py-2 text-[11px] text-muted-foreground">载入中…</div>
          ) : (
            <>
              {/* ① 分歧度 —— 回答"这个分数能不能直接用" */}
              <div className="mb-3">
                <div className="mb-1 flex items-baseline gap-2">
                  <h3 className="font-mono text-[12px] font-semibold text-foreground">分数可信度</h3>
                  <span className="text-[10px] text-muted-foreground">
                    disp = 加权标准差；conf = max(0, 1−disp/1.5)；高分歧的分数不能当方向用
                  </span>
                </div>
                {assets.length === 0 ? (
                  <div className="rounded border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
                    近期决策快照里没有可用分量（全为占位/无数据）。
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[640px] text-[11px]">
                      <thead>
                        <tr className="border-b border-border/60 text-left font-mono text-[10px] text-muted-foreground">
                          <th className="py-1 pr-2 font-normal">品种</th>
                          <th className="py-1 pr-2 text-right font-normal">分数</th>
                          <th className="py-1 pr-2 text-right font-normal">分歧</th>
                          <th className="py-1 pr-2 text-right font-normal">等级</th>
                          <th className="py-1 pr-2 text-right font-normal">可信度</th>
                          <th className="py-1 pr-2 text-right font-normal">多/空权重</th>
                          <th className="py-1 pr-2 text-right font-normal">分量</th>
                          <th className="py-1 pr-2 text-right font-normal">仓位乘数</th>
                          <th className="py-1 pl-1 font-normal">观测</th>
                        </tr>
                      </thead>
                      <tbody>
                        {assets.map((a) => {
                          const isOpen = expanded === a.asset
                          return (
                            <Fragment key={a.asset}>
                              <tr
                                className="cursor-pointer border-b border-border/40 hover:bg-muted/20"
                                onClick={() => setExpanded(isOpen ? null : a.asset)}
                              >
                                <td className="py-1 pr-2 font-medium text-foreground">
                                  <span aria-hidden className="mr-1 text-muted-foreground">
                                    {isOpen ? '▾' : '▸'}
                                  </span>
                                  {a.asset}
                                </td>
                                <td className="py-1 pr-2 text-right font-mono text-foreground/80">{fmtNum(a.fund_score, 3)}</td>
                                <td className="py-1 pr-2 text-right font-mono">{fmtNum(a.dispersion, 3)}</td>
                                <td className={`py-1 pr-2 text-right font-mono ${LEVEL_TONE[a.dispersion_level || ''] || ''}`}>
                                  {a.dispersion_level || '—'}
                                  <span className="ml-0.5 text-[9px] text-muted-foreground/70">
                                    {a.source === 'stored' ? '库' : a.source === 'recomputed' ? '算' : ''}
                                  </span>
                                </td>
                                <td className="py-1 pr-2 text-right font-mono">{fmtNum(a.confidence, 2)}</td>
                                <td className="py-1 pr-2 text-right font-mono text-muted-foreground">
                                  {fmtNum(a.bull_weight, 2)} / {fmtNum(a.bear_weight, 2)}
                                </td>
                                <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{a.components_n}</td>
                                <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{fmtNum(a.sizing_mult, 2)}</td>
                                <td className="py-1 pl-1 font-mono text-muted-foreground">{fmtDate(a.observed_at)}</td>
                              </tr>
                              {isOpen && (
                                <tr key={`${a.asset}-detail`} className="border-b border-border/40">
                                  <td colSpan={9} className="bg-secondary/20 px-2 py-1.5">
                                    {(a.components || []).length === 0 ? (
                                      <span className="text-[11px] text-muted-foreground">没有分量明细。</span>
                                    ) : (
                                      <div className="flex flex-col gap-1">
                                        {a.components.map((c) => (
                                          <div key={c.name} className="grid grid-cols-[110px_52px_52px_1fr] gap-2 text-[11px]">
                                            <span className="font-mono text-foreground/80">{c.name}</span>
                                            <span className="text-right font-mono">{fmtNum(c.score, 2)}</span>
                                            <span className="text-right font-mono text-muted-foreground">w={fmtNum(c.weight, 2)}</span>
                                            <span className="truncate text-muted-foreground" title={c.detail}>
                                              {c.detail || '—'}
                                            </span>
                                          </div>
                                        ))}
                                        {(a.gaps || []).length > 0 && (
                                          <div className="mt-0.5 font-mono text-[10px] text-amber-400/80">
                                            缺口：{(a.gaps || []).join('；')}
                                          </div>
                                        )}
                                      </div>
                                    )}
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
                {noDisp.length > 0 && (
                  <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                    另有 {noDisp.length} 个标签无分量、无法算分歧（{noDisp.map((a) => a.asset).join(' / ')}）
                  </div>
                )}
              </div>

              {/* ② 宏观/估值/链上 —— 回答"结论背后的输入是什么、新不新" */}
              <div className="grid gap-2 md:grid-cols-2">
                {groups.map((g) => (
                  <div key={g} className="rounded border border-border/60 p-2">
                    <div className="mb-1 flex items-baseline justify-between">
                      <span className="font-mono text-[11px] font-semibold text-foreground">{GROUP_LABEL[g]}</span>
                      <span className="font-mono text-[10px] text-muted-foreground">{macro[g].length} 项</span>
                    </div>
                    <div className="flex flex-col gap-0.5">
                      {macro[g].map((m) => (
                        <div key={m.metric_key} className="flex items-baseline gap-2 text-[11px]">
                          <span className="min-w-0 flex-1 truncate font-mono text-muted-foreground" title={m.label || m.metric_key}>
                            {m.metric_key}
                          </span>
                          <span
                            className={`font-mono ${
                              m.status !== 'ok' || m.value === null ? 'text-muted-foreground/60' : 'text-foreground'
                            }`}
                          >
                            {m.status !== 'ok' || m.value === null ? '无数据' : `${fmtNum(m.value)} ${m.unit}`}
                          </span>
                          {m.stale ? (
                            <span className="rounded border border-amber-400/40 px-1 font-mono text-[10px] text-amber-400">
                              旧 {m.stale_days}d
                            </span>
                          ) : (
                            <span className="font-mono text-[10px] text-muted-foreground/60">{fmtDate(m.observed_at)}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              {(data?.notes || []).length > 0 && (
                <div className="mt-2 font-mono text-[10px] leading-relaxed text-muted-foreground/70">
                  {(data?.notes || []).map((n) => (
                    <div key={n}>· {n}</div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      ) : null}
    </section>
  )
}
