'use client'

import { useEffect, useMemo, useState } from 'react'
import { fetchTechSignals, type TechSignal } from '@/lib/api-gub'

/**
 * v457 · 技术信号面板
 *
 * 为什么要这块：后端 `tech_signals` 一直在产信号、台账也一直在验证，
 * 但前端从没调用 `/api/tech/signals`（`lib/api-gub.ts` 里封装了函数、0 个调用方）
 * —— 用户看到的界面只有价格和 AI 结论，"系统在算技术信号"这件事在 UI 上是不可见的。
 *
 * 三条硬约束（对齐项目口径）：
 *  1. **不造数**：接口返回空数组就显示"这段时间没有信号"，不补占位行；
 *  2. **口径一致**：结论(方向/周期)、评级、波动档全部由后端算完透传，
 *     前端不自己实现一套白名单或打分，避免界面和真实下单对不上；
 *  3. **拒绝也要说明**：不出方向的信号照样列出来，并写明"按当前口径仅观察"，
 *     否则用户会以为信号被漏采。
 */

const ASSET_LABEL: Record<string, string> = {
  BTC: 'BTC 比特币',
  ETH: 'ETH 以太坊',
  SOL: 'SOL',
  XAU: 'XAU 黄金',
  WTI: 'WTI 原油',
}

const HOURS = 24 * 60 * 60 * 1000

function fmtTime(ts: number): string {
  // 后端 ts 是秒级 unix；统一按本地时间展示，不写死时区
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function GradeBadge({ grade }: { grade?: string | null }) {
  if (!grade) return <span className="text-muted-foreground/40">—</span>
  const cls =
    grade === 'A'
      ? 'border-long/40 text-long'
      : grade === 'B'
        ? 'border-border text-foreground/80'
        : 'border-border text-muted-foreground'
  return (
    <span className={`inline-flex min-w-[16px] justify-center rounded border px-1 text-[10px] ${cls}`}>
      {grade}
    </span>
  )
}

function ActionCell({ sig }: { sig: TechSignal }) {
  if (sig.actionable && sig.action && sig.action !== 'HOLD') {
    const isBuy = sig.action === 'BUY'
    return (
      <span className={`text-[11px] font-medium ${isBuy ? 'text-long' : 'text-short'}`}>
        {isBuy ? '看涨 ↑' : '看跌 ↓'}
        {sig.horizon ? <span className="text-muted-foreground"> · {sig.horizon}日</span> : null}
      </span>
    )
  }
  return <span className="text-[11px] text-muted-foreground/60">仅观察</span>
}

export function TechSignalsPanel({
  asset,
  days = 30,
  limit = 8,
}: {
  asset: string
  days?: number
  limit?: number
}) {
  const [signals, setSignals] = useState<TechSignal[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setErr(null)
    setSignals(null)
    fetchTechSignals(asset, days)
      .then((res) => {
        if (cancelled) return
        if (!res?.ok) {
          setErr(String((res as { reason?: string })?.reason || '接口返回 ok=false'))
          setSignals([])
          return
        }
        setSignals(res.signals || [])
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setErr(e instanceof Error ? e.message : '请求失败')
        setSignals([])
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [asset, days])

  const rows = useMemo(() => {
    if (!signals?.length) return []
    // 接口按 ts 升序返回；面板按"最近在上"展示
    return [...signals].reverse().slice(0, limit)
  }, [signals, limit])

  const actionableCount = useMemo(
    () => (signals || []).filter((s) => s.actionable && s.action && s.action !== 'HOLD').length,
    [signals],
  )

  return (
    <section className="min-h-0 shrink-0 rounded border border-border bg-background/40">
      <header className="flex items-center justify-between border-b border-border px-2.5 py-1.5">
        <div className="flex items-center gap-2 text-[11px] font-medium text-foreground">
          技术信号
          <span className="text-muted-foreground/70">{ASSET_LABEL[asset] ?? asset}</span>
          <span className="text-muted-foreground/50">近 {days} 天</span>
        </div>
        <div className="text-[10px] text-muted-foreground">
          {loading
            ? '读取中…'
            : signals == null
              ? '—'
              : `${signals.length} 条 · 可执行 ${actionableCount} 条`}
        </div>
      </header>

      {loading ? (
        <div className="px-2.5 py-3 text-[11px] text-muted-foreground">正在读取后端信号…</div>
      ) : err ? (
        <div className="px-2.5 py-3 text-[11px] text-short">
          信号读取失败：{err}
          <div className="mt-1 text-muted-foreground">
            接口异常时不展示任何推测信号，避免把"取不到"看成"没发生"。
          </div>
        </div>
      ) : rows.length === 0 ? (
        <div className="px-2.5 py-3 text-[11px] text-muted-foreground">
          近 {days} 天没有达到入库门槛的技术信号。这是真实结果，不是占位数据。
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead>
              <tr className="text-muted-foreground">
                <th className="px-2.5 py-1 text-left font-normal">时间</th>
                <th className="px-1.5 py-1 text-left font-normal">触发</th>
                <th className="px-1.5 py-1 text-left font-normal">评分</th>
                <th className="px-1.5 py-1 text-left font-normal">结论</th>
                <th className="px-1.5 py-1 text-left font-normal">波动</th>
                <th className="px-2.5 py-1 text-right font-normal">现价</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={`${s.ts}-${s.trigger_type}`} className="border-t border-border/60">
                  <td className="whitespace-nowrap px-2.5 py-1 text-muted-foreground">
                    {fmtTime(s.ts)}
                    {s.uses_future ? (
                      <span className="ml-1 rounded border border-short/40 px-1 text-[9px] text-short">
                        未来函数
                      </span>
                    ) : null}
                  </td>
                  <td className="whitespace-nowrap px-1.5 py-1">{s.trigger_type || '—'}</td>
                  <td className="whitespace-nowrap px-1.5 py-1">
                    <span className="tabular-nums">{Number(s.score).toFixed(2)}</span>
                    <span className="ml-1">
                      <GradeBadge grade={s.grade} />
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-1.5 py-1">
                    <ActionCell sig={s} />
                  </td>
                  <td className="whitespace-nowrap px-1.5 py-1 text-muted-foreground">
                    {s.vol_bucket ?? '—'}
                    {s.asset_class ? (
                      <span className="ml-1 text-muted-foreground/60">{s.asset_class}</span>
                    ) : null}
                  </td>
                  <td className="whitespace-nowrap px-2.5 py-1 text-right tabular-nums">
                    {Number(s.close).toLocaleString(undefined, { maximumFractionDigits: 4 })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <footer className="border-t border-border/60 px-2.5 py-1 text-[10px] text-muted-foreground/60">
        结论/周期由后端按白名单与分层口径算出；「仅观察」= 会记账但不出方向。
      </footer>
    </section>
  )
}

/** 供外部做"多久没更新"判断，避免把历史信号看成实时 */
export function staleDays(signals: TechSignal[] | null): number | null {
  if (!signals?.length) return null
  const maxTs = Math.max(...signals.map((s) => s.ts))
  return Math.floor((Date.now() - maxTs * 1000) / HOURS / 24)
}
