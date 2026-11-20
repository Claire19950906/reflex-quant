'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v497 两套账本对账 — 页面上经常同时出现「t2 亏 80」和「快测亏 0.6」两个数字，
// 看起来像系统算错了。真相是两套账本测的东西不一样、样本都不够。
// 这里把两者并列摊开，并明确标出哪一套是官方绩效口径。

type Ledger = {
  key: string
  name: string
  official: boolean
  table: string
  n: number
  closed: number
  open: number
  wins: number
  net: number
  avg: number
  note: string
}

type Resp = {
  ok: boolean
  ledgers: Ledger[]
  official: string
  coverage: {
    decisions_7d: number
    not_in_official_ledger_7d: number
    t2_rows_traceable_to_decision: number
    note: string
  }
  verdict: string
  disclaimer: string
}

export function LedgerReconcileCard() {
  const [data, setData] = useState<Resp | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const r = await fetch(`${API_BASE}/ledger/reconcile`, { cache: 'no-store' })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const j = (await r.json()) as Resp
        if (alive) {
          setData(j)
          setErr('')
        }
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : '加载失败')
      }
    }
    load()
    const t = setInterval(load, 60_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  if (err) {
    return (
      <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">
        两套账本对账接口未接上：{err}
      </div>
    )
  }
  if (!data) return null

  return (
    <div className="surface-card mb-3 p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
          两套模拟账本对账
        </span>
        <span className="font-mono text-[10px] text-muted-foreground">
          官方口径 = {data.official}
        </span>
      </div>

      <table className="w-full table-fixed border-collapse text-xs">
        <colgroup>
          <col style={{ width: '34%' }} />
          <col style={{ width: '9%' }} />
          <col style={{ width: '9%' }} />
          <col style={{ width: '9%' }} />
          <col style={{ width: '14%' }} />
          <col style={{ width: '25%' }} />
        </colgroup>
        <thead>
          <tr className="border-b border-border text-muted-foreground">
            <th className="py-1 text-left font-normal">账本</th>
            <th className="py-1 text-right font-normal">笔数</th>
            <th className="py-1 text-right font-normal">已结算</th>
            <th className="py-1 text-right font-normal">胜</th>
            <th className="py-1 text-right font-normal">净额</th>
            <th className="py-1 text-left font-normal pl-2">它测的是什么</th>
          </tr>
        </thead>
        <tbody>
          {data.ledgers.map((L) => (
            <tr key={L.key} className="border-b border-border/50 align-top">
              <td className="py-1 text-foreground">
                {L.name}
                {L.official ? (
                  <span className="ml-1 rounded-sm border border-primary/50 px-1 font-mono text-[9px] text-primary">
                    官方
                  </span>
                ) : null}
                <div className="font-mono text-[10px] text-muted-foreground">{L.table}</div>
              </td>
              <td className="py-1 text-right text-foreground">{L.n}</td>
              <td className="py-1 text-right text-foreground">{L.closed}</td>
              <td className="py-1 text-right text-foreground">{L.wins}</td>
              <td className={`py-1 text-right ${L.net >= 0 ? 'text-long' : 'text-short'}`}>
                {L.net >= 0 ? '+' : ''}
                {L.net.toFixed(2)}
              </td>
              <td className="py-1 pl-2 text-[10px] leading-relaxed text-muted-foreground">{L.note}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="mt-2 text-xs leading-relaxed text-foreground/80">{data.verdict}</p>
      <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
        近 7 天决策 {data.coverage.decisions_7d} 条，其中 {data.coverage.not_in_official_ledger_7d} 条没进官方账本
        —— 这些是「分析过但没成交」，不是漏账。{data.disclaimer}
      </p>
    </div>
  )
}
