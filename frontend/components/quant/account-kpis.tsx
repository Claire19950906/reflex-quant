'use client'

import { useMemo } from 'react'
import { Activity, ArrowDownRight, ArrowUpRight, Percent, Wallet } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ApiEvent } from '@/lib/quant-data'
import type { ReplayPositions } from '@/lib/replay-data'
import { useTodaySignalCount } from './use-today-signal-count'

// v353-UI P1: 首页顶部「结论区」—— 4 张 KPI。
//
// 为什么加：原来的账户数字埋在右栏 400px 的卡片里（权益 9,683.15 与旁边 10px 灰字同权重），
// 打开首页要先滚两屏才知道"今天赚没赚、有没有持仓"。
//
// 数据全部来自已有的 /replay/positions（与模拟盘页同一口径），不新增接口：
//   总权益  = account.current_equity_usdt
//   已实现  = account.realized_pnl_usdt（相对 initial_equity_usdt 的收益率）
//   胜率    = positions 里 settled 且 is_correct 为 WIN/LOSS 的比例
//   活跃信号 = 传入的 events 里 BUY/SELL 条数（多空分开计数）
//
// 任意一项拿不到就显示「—」+ 说明，不编 0。

function fmtUsdt(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function fmtPct(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

function Spark({ up }: { up: boolean }) {
  const d = up
    ? 'M0 26 L14 22 L28 25 L42 17 L56 20 L70 13 L84 16 L100 9'
    : 'M0 10 L14 15 L28 12 L42 20 L56 17 L70 24 L84 21 L100 28'
  return (
    <svg
      className="pointer-events-none absolute bottom-0 right-0 h-11 w-32 opacity-50"
      viewBox="0 0 100 34"
      preserveAspectRatio="none"
      aria-hidden
    >
      <path d={d} fill="none" stroke={up ? 'var(--long)' : 'var(--short)'} strokeWidth="1.2" />
    </svg>
  )
}

function Kpi({
  label,
  value,
  unit,
  sub,
  tone = 'default',
  icon,
  badge,
}: {
  label: string
  value: string
  unit?: string
  sub?: React.ReactNode
  tone?: 'default' | 'up' | 'down'
  icon?: React.ReactNode
  badge?: string
}) {
  return (
    <div className="surface-card relative overflow-hidden px-4 py-3">
      <div className="flex items-center gap-1.5">
        {icon}
        <span className="text-[11px] text-muted-foreground">{label}</span>
        {badge && (
          <span className="ml-auto rounded border border-border px-1 py-0.5 font-mono text-[11px] text-muted-foreground">
            {badge}
          </span>
        )}
      </div>
      <div
        className={cn(
          'mt-2 font-mono text-[26px] font-semibold leading-none tracking-tight',
          tone === 'up' && 'text-long',
          tone === 'down' && 'text-short',
        )}
      >
        {value}
        {unit && <span className="ml-1.5 text-[11px] font-normal text-muted-foreground">{unit}</span>}
      </div>
      <div className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">{sub}</div>
      {tone !== 'default' && <Spark up={tone === 'up'} />}
    </div>
  )
}

export function AccountKpis({
  events,
  snapshot,
  err = false,
}: {
  events: ApiEvent[]
  snapshot: ReplayPositions | null
  err?: boolean
}) {
  const data = snapshot
  const acct = data?.account
  const loading = !err && (!acct || acct.current_equity_usdt == null)

  const settled = useMemo(() => {
    if (!data) return { wins: 0, losses: 0, count: 0 }
    const arr = data.positions.filter((p) => Number(p.settled) === 1)
    const wins = arr.filter((p) => p.is_correct === 'WIN').length
    const losses = arr.filter((p) => p.is_correct === 'LOSS').length
    return { wins, losses, count: arr.length }
  }, [data])

  const openCount = useMemo(
    () => (data ? data.positions.filter((p) => Number(p.settled) !== 1).length : 0),
    [data],
  )

  // 只数今天的 BUY/SELL —— 事件流里带着历史全量，全量数字当"活跃信号"会虚高。
  // v470 fix：后端 timestamp 有两种形态 —— 'YYYY-MM-DD HH:MM:SS'（空格分隔，主路径）
  // 和 'YYYY-MM-DDTHH:MM:SS'（T 分隔）。旧代码 startsWith(today) 只认 T 分隔，
  // 空格格式前 10 位仍是日期字面量但第 11 位是空格 → 前缀不匹配，今日决策行被全部滤掉，
  // 活跃信号恒显 0。统一取前 10 位日期串比较，两种格式都兼容。
  const localLive = useMemo(() => {
    const d = new Date()
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    const actionable = events.filter(
      (e) =>
        (e.action === 'BUY' || e.action === 'SELL') &&
        typeof e.timestamp === 'string' &&
        e.timestamp.slice(0, 10) === today,
    )
    return {
      total: actionable.length,
      long: actionable.filter((e) => e.action === 'BUY').length,
      short: actionable.filter((e) => e.action === 'SELL').length,
    }
  }, [events])

  // v471：优先直连后端 /events/today 的权威多空拆分（不受 events 列表 limit 截断影响）；
  // 接口未就绪或旧后端无拆分字段时，回退上面的本地列表计数兜底，不显示假 0。
  const todaySignal = useTodaySignalCount()
  const live = todaySignal ?? localLive

  const retPct =
    acct && acct.initial_equity_usdt
      ? (acct.realized_pnl_usdt / acct.initial_equity_usdt) * 100
      : null
  const pnlUp = (acct?.realized_pnl_usdt ?? 0) >= 0
  const winRate = settled.wins + settled.losses > 0
    ? (settled.wins / (settled.wins + settled.losses)) * 100
    : null

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Kpi
        label="总权益"
        badge="PAPER"
        icon={<Wallet className="h-3.5 w-3.5 text-primary" aria-hidden />}
        value={fmtUsdt(acct?.current_equity_usdt)}
        unit="USDT"
        sub={
          <>
            <span className="font-mono">起始 {fmtUsdt(acct?.initial_equity_usdt)}</span>
            <span>·</span>
            <span>持仓 {openCount} 笔</span>
          </>
        }
      />

      <Kpi
        label="已实现盈亏"
        icon={
          pnlUp ? (
            <ArrowUpRight className="h-3.5 w-3.5 text-long" aria-hidden />
          ) : (
            <ArrowDownRight className="h-3.5 w-3.5 text-short" aria-hidden />
          )
        }
        value={fmtUsdt(acct?.realized_pnl_usdt)}
        unit="USDT"
        tone={acct ? (pnlUp ? 'up' : 'down') : 'default'}
        sub={
          <>
            <span className={cn('font-mono', pnlUp ? 'text-long' : 'text-short')}>
              {fmtPct(retPct)}
            </span>
            <span className="font-mono">浮盈亏 {fmtUsdt(acct?.unrealized_pnl_usdt)}</span>
          </>
        }
      />

      <Kpi
        label="胜率"
        badge={`已结算 ${settled.count} 笔`}
        icon={<Percent className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />}
        value={winRate == null ? '—' : winRate.toFixed(1)}
        unit={winRate == null ? undefined : '%'}
        sub={
          <>
            <span className="font-mono text-long">{settled.wins} 胜</span>
            <span className="font-mono text-short">{settled.losses} 负</span>
            {winRate == null && <span>样本不足，不做估算</span>}
          </>
        }
      />

      <Kpi
        label="活跃信号"
        icon={<Activity className="h-3.5 w-3.5 text-primary" aria-hidden />}
        value={String(live.total)}
        unit="条"
        sub={
          <>
            <span className="rounded bg-long/10 px-1 font-mono text-[11px] text-long">{live.long} 多</span>
            <span className="rounded bg-short/10 px-1 font-mono text-[11px] text-short">{live.short} 空</span>
          </>
        }
      />

      {loading && (
        <div className="col-span-full -mt-1 text-[11px] text-muted-foreground">
          账户快照计算中 —— 15s 内自动刷新，先回持仓、后补权益。
        </div>
      )}
      {err && (
        <div className="col-span-full -mt-1 text-[11px] text-muted-foreground">
          模拟盘接口暂不可用，KPI 保留上一次值，不显示假 0。
        </div>
      )}
    </div>
  )
}
