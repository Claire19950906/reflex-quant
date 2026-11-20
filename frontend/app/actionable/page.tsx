'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v422 可执行信号卡 — 把「BUY/SELL + 一段理由」变成「止损 / 仓位 / 目标 / 什么情况下作废」。
// 口径全在后端（routers/actionable.py + signal_cards.py）：价格与 ATR 来自 kline_bars，
// 仓位是风险平价（单笔风险 risk_pct%），页面不重算，避免出现两个不一样的数字。

type Plan = {
  ok: boolean
  direction?: string
  stop?: number
  stop_pct?: number
  targets?: number[]
  size_pct?: number
  risk_pct?: number
  atr_mult?: number
  estimated?: boolean
  why?: string
}

type Market = {
  bars: number
  price: number | null
  atr: number | null
  atr_pct: number | null
  tf: string
  data_age_h: number | null
  stale: boolean
  source: string
}

type Card = {
  decision_id: number
  at: string
  asset: string
  action: 'BUY' | 'SELL'
  action_field: string
  confidence: number | null
  gate_reason: string
  degraded: string
  news_source: string
  news_head: string
  why: string
  market: Market
  plan: Plan
  execute_ready: boolean
  blocking: string[]
  same_direction_count: number
  proxy?: string
}

type Data = {
  window_days: number
  directional_total: number
  returned: number
  ready: number
  not_ready: number
  duplicates_collapsed: number
  by_asset: Record<string, number>
  params: { risk_pct: number; atr_mult: number }
  cards: Card[]
  disclaimer: string
}

const fmt = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(digits)

export default function ActionablePage() {
  const [days, setDays] = useState(3)
  const [data, setData] = useState<Data | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  const load = useCallback((d: number) => {
    setLoading(true)
    fetch(`${API_BASE}/signals/actionable?days=${d}&limit=40`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) { setData(j.data); setErr('') }
        else setErr(j?.error || '读取失败')
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(days) }, [days, load])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">可执行信号卡</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            方向性决策 → 止损 / 仓位 / 目标 · 只读 · 价格与波动来自 kline_bars
          </p>
        </div>
        <div className="flex gap-1 font-mono text-[11px]">
          {[1, 3, 7, 14].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`rounded-sm border px-2 py-1 ${
                days === d ? 'border-primary text-primary' : 'border-border text-muted-foreground'
              }`}
            >
              近 {d} 天
            </button>
          ))}
        </div>
      </header>

      {err && <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">{err}</div>}

      {data && (
        <>
          <div className="surface-card mb-3 grid grid-cols-2 gap-3 p-3 font-mono text-[11px] md:grid-cols-5">
            <div>
              <div className="text-muted-foreground">方向性决策</div>
              <div className="text-lg">{data.directional_total}</div>
            </div>
            <div>
              <div className="text-muted-foreground">去重后</div>
              <div className="text-lg">{data.cards.length}</div>
            </div>
            <div>
              <div className="text-muted-foreground">可执行</div>
              <div className="text-lg text-long">{data.ready}</div>
            </div>
            <div>
              <div className="text-muted-foreground">算不出 / 过期</div>
              <div className="text-lg text-amber-400">{data.not_ready}</div>
            </div>
            <div>
              <div className="text-muted-foreground">折叠同向重复</div>
              <div className="text-lg">{data.duplicates_collapsed}</div>
            </div>
          </div>

          {data.cards.length === 0 && (
            <div className="surface-card p-4 text-xs text-muted-foreground">
              这段时间没有方向性决策（全是 HOLD）。这是产出侧的问题，不是本页的问题——
              去「产能体检」看队列和门卫通道。
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2">
            {data.cards.map((c) => {
              const buy = c.action === 'BUY'
              return (
                <div key={`${c.decision_id}`} className="surface-card p-3">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <div className="flex items-baseline gap-2">
                      <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[11px] ${
                        buy ? 'border-emerald-500/50 text-long' : 'border-red-500/50 text-short'
                      }`}>
                        {c.action}
                      </span>
                      <span className="font-mono text-sm font-semibold">{c.asset}</span>
                      {c.proxy && (
                        <span className="font-mono text-[10px] text-muted-foreground">代理 {c.proxy}</span>
                      )}
                      {c.same_direction_count > 1 && (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          同向 {c.same_direction_count} 条
                        </span>
                      )}
                    </div>
                    <span className="font-mono text-[11px] text-muted-foreground">{c.at}</span>
                  </div>

                  <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-[11px]">
                    <div>
                      <div className="text-muted-foreground">现价</div>
                      <div>{fmt(c.market.price, 4)}</div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">ATR%</div>
                      <div>{fmt(c.market.atr_pct)}%</div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">K线</div>
                      <div>
                        {c.market.bars ? `${c.market.tf} / ${c.market.bars}根` : '无'}
                        {c.market.data_age_h !== null && (
                          <span className={c.market.stale ? ' text-amber-400' : ' text-muted-foreground'}>
                            {' '}· {fmt(c.market.data_age_h, 1)}h 前
                          </span>
                        )}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">止损</div>
                      <div className="text-amber-400">
                        {fmt(c.plan.stop, 4)}
                        <span className="text-muted-foreground"> ({fmt(c.plan.stop_pct)}%)</span>
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">仓位（风险 {fmt(c.plan.risk_pct)}%）</div>
                      <div>{fmt(c.plan.size_pct)}%</div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">目标 1R/2R/3R</div>
                      <div>
                        {(c.plan.targets || []).map((t) => fmt(t, 2)).join(' / ') || '—'}
                      </div>
                    </div>
                  </div>

                  {(c.blocking?.length > 0 || c.plan.estimated) && (
                    <div className="mt-2 border-t border-border/40 pt-2 font-mono text-[10px] text-amber-400">
                      {c.blocking?.length > 0 ? c.blocking.join('；') : ''}
                      {c.plan.estimated ? ` 止损按 2% 兜底（K 线不足）：${c.plan.why || ''}` : ''}
                    </div>
                  )}

                  <div className="mt-2 line-clamp-2 text-[11px] text-muted-foreground">
                    {c.news_head || c.why}
                  </div>
                  {c.gate_reason && (
                    <div className="mt-1 font-mono text-[10px] text-muted-foreground/80">
                      闸门：{c.gate_reason}
                      {c.confidence !== null && ` · 置信度 ${fmt(c.confidence, 1)}`}
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          <p className="mt-3 font-mono text-[11px] text-muted-foreground/70">{data.disclaimer}</p>
          <p className="mt-1 font-mono text-[11px] text-muted-foreground/70">
            参数：单笔风险 {data.params.risk_pct}% · ATR 倍数 {data.params.atr_mult} ·
            仓位 = 风险% ÷ 止损距离%。K 线不足时标「兜底」，不假装算过。
          </p>
        </>
      )}

      {loading && <div className="mt-2 font-mono text-[11px] text-muted-foreground">读取中…</div>}
    </main>
  )
}
