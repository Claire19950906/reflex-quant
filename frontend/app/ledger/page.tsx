'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { CapabilityPanel } from '@/components/quant/capability-panel'

// v462 账本 · 模拟盘 — 只做三件事：按最新行情标记、按账户算敞口、把对不上的列出来。
// 不写库、不下单、不补历史（重置前数据不回溯）。

type Position = {
  id: number
  asset: string
  side: string
  qty: number
  entry_price: number
  entry_ts: number | null
  age_min: number | null
  mark: number | null
  mark_ts: number | null
  mark_source: string
  mark_age_s: number | null
  mark_stale: boolean
  unrealized_pct: number | null
  unrealized_usdt: number | null
  notional_entry: number
  notional_mark: number | null
  stop_price: number | null
  target_price: number | null
  decision_id: number | null
  strategy_id: string | null
  note: string | null
}

type Order = {
  id: number
  ts: number
  asset: string
  side: string
  qty: number
  notional_usdt: number | null
  filled_price: number | null
  status: string
  mode: string
  decision_id: number | null
  note: string | null
}

type Closed = {
  id: number
  asset: string
  side: string
  qty: number
  entry_price: number
  exit_price: number | null
  closed_ts: number | null
  realized_pnl: number | null
}

type Report = {
  account: {
    mode: string
    cash: number
    equity_stored: number
    equity_mtm: number
    equity_gap: number
    realized_pnl: number
    fee_bps: number
    updated_at: number | null
    age_s: number | null
  }
  positions: Position[]
  orders: Order[]
  closed: Closed[]
  costs: {
    rows: number
    fee_rows: number
    fee_total: number
    token_rows: number
    token_total: number
    filled_notional: number
    expected_fee: number
  }
  risk: {
    open_count: number
    gross_exposure: number
    net_exposure: number
    concentration_pct: number
    realized_closed: number
  }
  reconciliation: { level: string; key: string; text: string }[]
  headline: string
  note: string
}

const money = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined ? '—' : v.toLocaleString('en-US', { maximumFractionDigits: d })
const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`

export default function LedgerPage() {
  const [rep, setRep] = useState<Report | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    setBusy(true)
    fetch(`${API_BASE}/ledger/overview?order_limit=30`, { signal: AbortSignal.timeout(20000) })
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) { setRep(j.data); setErr('') }
        else setErr(j?.error || '读取失败')
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'))
      .finally(() => setBusy(false))
  }, [])

  useEffect(() => { load() }, [load])

  const a = rep?.account
  const r = rep?.risk
  const isPaper = a?.mode === 'paper'

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">账本 · 模拟盘</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            账户、持仓、成交、对账——按最新行情标记，不用估算冒充实盘 · 只读
          </p>
        </div>
        <button
          onClick={load}
          className="rounded-sm border border-border px-2 py-1 font-mono text-[11px] text-muted-foreground"
        >
          {busy ? '读取中' : '刷新'}
        </button>
      </header>

      {err && <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">{err}</div>}

      {isPaper && (
        <div className="surface-card mb-3 border-l-2 border-amber-400/70 p-3 text-xs text-amber-300">
          这是 <span className="font-mono">paper</span> 模拟盘账户，所有成交都是模拟单，不代表真实资金。
          真实交易未启用时，本页只用于对账与风险检查。
        </div>
      )}

      {rep && a && r && (
        <>
          <div className="surface-card mb-3 border-l-2 border-primary/60 p-3">
            <div className="text-sm">{rep.headline}</div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              账户模式 {a.mode} · 费率 {a.fee_bps}bps · 快照
              {a.age_s === null ? '—' : ` ${Math.round(a.age_s / 60)} 分钟前`}
            </div>
          </div>

          <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
            {[
              ['权益（标记后）', `${money(a.equity_mtm)} U`],
              ['现金', `${money(a.cash)} U`],
              ['已实现', `${money(a.realized_pnl)} U`],
              ['浮盈亏', `${money(rep.positions.reduce((s, p) => s + (p.unrealized_usdt || 0), 0))} U`],
            ].map(([k, v]) => (
              <div key={k} className="surface-card p-3">
                <div className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{k}</div>
                <div className="mt-1 font-mono text-lg">{v}</div>
              </div>
            ))}
          </div>

          <div className="mb-3 grid gap-3 lg:grid-cols-3">
            <div className="surface-card p-3 lg:col-span-2">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                持仓（{r.open_count} 笔）
              </div>
              {rep.positions.length === 0 ? (
                <div className="py-6 text-center text-xs text-muted-foreground">空仓</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full font-mono text-[11px]">
                    <thead className="text-muted-foreground">
                      <tr className="border-b border-border/60">
                        <th className="py-1.5 text-left font-normal">品种</th>
                        <th className="py-1.5 text-left font-normal">方向</th>
                        <th className="py-1.5 text-right font-normal">数量</th>
                        <th className="py-1.5 text-right font-normal">入场</th>
                        <th className="py-1.5 text-right font-normal">标记</th>
                        <th className="py-1.5 text-right font-normal">浮盈亏</th>
                        <th className="py-1.5 text-right font-normal">名义</th>
                        <th className="py-1.5 text-right font-normal">止损</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rep.positions.map((p) => (
                        <tr key={p.id} className="border-b border-border/30">
                          <td className="py-1.5">{p.asset}</td>                          <td className={p.side.toLowerCase().includes('short') || p.side.toLowerCase() === 'sell'
                            ? 'py-1.5 text-short' : 'py-1.5 text-long'}>
                            {p.side}
                          </td>
                          <td className="py-1.5 text-right">{p.qty}</td>
                          <td className="py-1.5 text-right">{money(p.entry_price)}</td>
                          <td className="py-1.5 text-right">
                            {money(p.mark)}
                            {p.mark_stale && <span className="ml-1 text-amber-300">旧</span>}
                          </td>
                          <td className={`py-1.5 text-right ${(p.unrealized_pct || 0) >= 0 ? 'text-long' : 'text-short'}`}>
                            {pct(p.unrealized_pct)}
                          </td>
                          <td className="py-1.5 text-right">{money(p.notional_entry, 0)}</td>
                          <td className={`py-1.5 text-right ${p.stop_price ? '' : 'text-amber-300'}`}>
                            {p.stop_price ? money(p.stop_price) : '无'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] md:grid-cols-4">
                <div className="rounded-sm border border-border/60 p-2">
                  <div className="text-muted-foreground">总名义敞口</div>
                  <div className="font-mono">{money(r.gross_exposure, 0)} U</div>
                </div>
                <div className="rounded-sm border border-border/60 p-2">
                  <div className="text-muted-foreground">净敞口</div>
                  <div className="font-mono">{money(r.net_exposure, 0)} U</div>
                </div>
                <div className="rounded-sm border border-border/60 p-2">
                  <div className="text-muted-foreground">最大单品种占权益</div>
                  <div className="font-mono">{r.concentration_pct}%</div>
                </div>
                <div className="rounded-sm border border-border/60 p-2">
                  <div className="text-muted-foreground">已平仓</div>
                  <div className="font-mono">{rep.closed.length} 笔</div>
                </div>
              </div>
            </div>

            <div className="surface-card p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                对账提醒
              </div>
              <ul className="space-y-2 text-[11px]">
                {rep.reconciliation.map((w) => (
                  <li
                    key={w.key}
                    className={
                      w.level === 'error' ? 'text-short' : w.level === 'warn' ? 'text-amber-300' : 'text-muted-foreground'
                    }
                  >
                    · {w.text}
                  </li>
                ))}
                {rep.reconciliation.length === 0 && (
                  <li className="text-muted-foreground">没有发现对不上的地方</li>
                )}
              </ul>

              <div className="mt-3 border-t border-border/40 pt-2 font-mono text-[10px] text-muted-foreground">
                成本账本 {rep.costs.rows} 行 · 手续费 {rep.costs.fee_rows} 行 / {money(rep.costs.fee_total)} U ·
                Token {rep.costs.token_rows} 行 / {money(rep.costs.token_total, 0)} · 成交名义{" "}
                {money(rep.costs.filled_notional, 0)} U · 应记手续费约 {money(rep.costs.expected_fee)} U
              </div>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <section className="surface-card p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                最近成交
              </div>
              {rep.orders.length === 0 ? (
                <div className="py-4 text-center text-xs text-muted-foreground">重置后还没有成交</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full font-mono text-[10px]">
                    <thead className="text-muted-foreground">
                      <tr className="border-b border-border/60">
                        <th className="py-1 text-left font-normal">#</th>
                        <th className="py-1 text-left font-normal">品种</th>
                        <th className="py-1 text-left font-normal">方向</th>
                        <th className="py-1 text-right font-normal">数量</th>
                        <th className="py-1 text-right font-normal">成交价</th>
                        <th className="py-1 text-right font-normal">名义</th>
                        <th className="py-1 text-left font-normal">状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rep.orders.slice(0, 15).map((o) => (
                        <tr key={o.id} className="border-b border-border/30">
                          <td className="py-1 text-muted-foreground">{o.id}</td>
                          <td className="py-1">{o.asset}</td>
                          <td className="py-1">{o.side}</td>
                          <td className="py-1 text-right">{o.qty}</td>
                          <td className="py-1 text-right">{money(o.filled_price)}</td>
                          <td className="py-1 text-right">{money(o.notional_usdt, 0)}</td>
                          <td className="py-1">{o.status}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="surface-card p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                已平仓（重置后）
              </div>
              {rep.closed.length === 0 ? (
                <div className="py-6 text-center text-xs text-muted-foreground">
                  0 笔。没有已平仓样本，胜率 / 盈亏比 / 最大回撤都不该算。
                </div>
              ) : (
                <div className="space-y-1.5">
                  {rep.closed.map((c) => (
                    <div key={c.id} className="flex justify-between font-mono text-[11px]">
                      <span>
                        {c.asset} {c.side} {c.qty}
                      </span>
                      <span className={(c.realized_pnl || 0) >= 0 ? 'text-long' : 'text-short'}>
                        {money(c.realized_pnl)} U
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>

          <p className="mt-3 border-t border-border/40 pt-2 text-[10px] leading-relaxed text-muted-foreground">
            {rep.note}
          </p>
        </>
      )}

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="账本与资金管理能力"
        hint="持仓 thesis / 成本账本 / 自动 sizing / 再平衡 / 现金也是仓位 / 成本批次 / 子账户隔离"
        featureIds={['F04','F18','F54','F55','F56','F57','F58']}
      />
      </section>
    </main>
  )
}
