'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

/**
 * v478 · 跨市场读数（挂在 /attribution 的「市场环境」里）
 *
 * 为什么放这里：归因页回答"这次涨跌是什么造成的"，但原先只有 A 股/加密视角。
 * 美股期货（ES/NQ）、美债 5Y/10Y、以及曲线价差（5s10s）是"外部环境"的硬读数 ——
 * 尤其曲线倒挂与否，直接改变风险资产的贴现假设。
 *
 * 口径：
 *  - ES/NQ 来自 sina_hf（期货连续合约），给价格与日内涨跌；
 *  - 5Y/10Y 来自 treasury_gov（日频），给收益率与 bp 变动；
 *  - 价差由后端直接给 spread_bp / inverted，前端不再自己相减。
 * 任何一个源失败，对应格子显示「无数据」，不拿 0 顶替，也不整块消失。
 */

type Quote = {
  symbol?: string
  asset_name?: string
  price?: number | null
  previous_close?: number | null
  change_pct?: number | null
  yield_pct?: number | null
  change_bp?: number | null
  source?: string
  ts?: number
}

type Spread = { spread_bp?: number | null; inverted?: boolean; ten_y?: number; five_y?: number }

type Cell = {
  key: string
  label: string
  path: string
  kind: 'price' | 'yield'
}

const CELLS: Cell[] = [
  { key: 'es', label: 'ES 标普期货', path: '/market/es', kind: 'price' },
  { key: 'nq', label: 'NQ 纳指期货', path: '/market/nq', kind: 'price' },
  { key: 'us5y', label: '美债 5Y', path: '/market/us5y', kind: 'yield' },
  { key: 'us10y', label: '美债 10Y', path: '/market/us10y', kind: 'yield' },
]

export function CrossMarketStrip() {
  const [quotes, setQuotes] = useState<Record<string, Quote | null>>({})
  const [spread, setSpread] = useState<Spread | null>(null)
  const [err, setErr] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const ac = new AbortController()
    const get = async (path: string) => {
      const r = await fetch(`${API_BASE}${path}`, { signal: ac.signal, cache: 'no-store' })
      if (!r.ok) throw new Error(String(r.status))
      return r.json()
    }
    Promise.allSettled([
      ...CELLS.map((c) => get(c.path)),
      get('/market/yield-curve-spread'),
    ])
      .then((res) => {
        const q: Record<string, Quote | null> = {}
        CELLS.forEach((c, i) => {
          const r = res[i]
          q[c.key] = r.status === 'fulfilled' ? (r.value?.data ?? null) : null
        })
        setQuotes(q)
        const sp = res[CELLS.length]
        setSpread(sp.status === 'fulfilled' ? (sp.value?.data ?? null) : null)
        setErr(res.every((r) => r.status === 'rejected'))
      })
      .catch(() => setErr(true))
      .finally(() => {
        if (!ac.signal.aborted) setLoading(false)
      })
    return () => ac.abort()
  }, [])

  const spreadTone =
    spread?.inverted === true ? 'text-amber-400' : 'text-long'

  return (
    <div className="rounded-lg border border-border bg-card/40 px-3 py-2">
      <div className="mb-1 flex items-baseline gap-2">
        <h3 className="font-mono text-[12px] font-semibold text-foreground">跨市场读数</h3>
        <span className="text-[10px] text-muted-foreground">
          股指期货 / 美债收益率 / 曲线价差 —— 外部环境，不是本品种自己的事
        </span>
      </div>
      {err ? (
        <div className="text-[11px] text-muted-foreground">接口未响应，不显示占位数据。</div>
      ) : (
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          {CELLS.map((c) => {
            const q = quotes[c.key]
            const okQuote = q && (c.kind === 'price' ? q.price != null : q.yield_pct != null)
            return (
              <div key={c.key} className="min-w-[92px]">
                <div className="font-mono text-[10px] text-muted-foreground">{c.label}</div>
                {loading ? (
                  <div className="font-mono text-[12px] text-muted-foreground/60">…</div>
                ) : !okQuote ? (
                  <div className="font-mono text-[12px] text-muted-foreground/60">无数据</div>
                ) : c.kind === 'price' ? (
                  <div className="flex items-baseline gap-1">
                    <span className="font-mono text-[13px] text-foreground">
                      {(q!.price as number).toLocaleString(undefined, { maximumFractionDigits: 2 })}
                    </span>
                    {q!.change_pct != null && (
                      <span className={`font-mono text-[11px] ${q!.change_pct >= 0 ? 'text-long' : 'text-short'}`}>
                        {q!.change_pct >= 0 ? '+' : ''}{q!.change_pct.toFixed(2)}%
                      </span>
                    )}
                  </div>
                ) : (
                  <div className="flex items-baseline gap-1">
                    <span className="font-mono text-[13px] text-foreground">{(q!.yield_pct as number).toFixed(2)}%</span>
                    {q!.change_bp != null && (
                      <span className={`font-mono text-[11px] ${q!.change_bp >= 0 ? 'text-short' : 'text-long'}`}>
                        {q!.change_bp >= 0 ? '+' : ''}{q!.change_bp.toFixed(0)}bp
                      </span>
                    )}
                  </div>
                )}
              </div>
            )
          })}
          <div className="min-w-[110px]">
            <div className="font-mono text-[10px] text-muted-foreground">5Y–10Y 价差</div>
            {loading ? (
              <div className="font-mono text-[12px] text-muted-foreground/60">…</div>
            ) : spread?.spread_bp == null ? (
              <div className="font-mono text-[12px] text-muted-foreground/60">无数据</div>
            ) : (
              <div className="flex items-baseline gap-1">
                <span className={`font-mono text-[13px] ${spreadTone}`}>{spread.spread_bp.toFixed(0)}bp</span>
                <span className="font-mono text-[10px] text-muted-foreground">
                  {spread.inverted ? '倒挂' : '正常'}
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
