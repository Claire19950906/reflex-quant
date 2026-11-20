'use client'

import { useMemo } from 'react'
import { Wallet } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ReplayPositions } from '@/lib/replay-data'

// v351-UI: 首页右栏「纸面账户 + 权益曲线」。
//
// 数据全部来自已有的 /replay/positions，不新增后端接口：
//   · 权益/已实现/浮盈亏/可用 直接读 account 四个字段（与模拟盘页同一口径）
//   · 曲线 = 已结算持仓按 entry_time 升序累加 current_pnl_usdt，起点 = initial_equity
//     实测 sum(current_pnl_usdt) 与 account.realized_pnl_usdt 完全相等（误差 0.004），
//     所以这条线不是"另算一套"，就是账户已实现盈亏的时间投影。
//
// 为什么不做成假空态：结算笔数少于 2 时画不出一条线，此时只显示数字 +
// 「已结算 N 笔，样本不足以成线 —— 不做预测」。宁可不画，也不给一条编出来的斜线。

type Point = { x: number; y: number; at: string; pnl: number }

function buildCurve(data: ReplayPositions | null): Point[] {
  if (!data) return []
  const settled = data.positions.filter(
    (p) => p.settled && p.entry_time && p.current_pnl_usdt != null,
  )
  if (settled.length < 2) return []
  settled.sort((a, b) => (a.entry_time ?? '').localeCompare(b.entry_time ?? ''))
  let equity = data.account.initial_equity_usdt
  return settled.map((p) => {
    equity += p.current_pnl_usdt ?? 0
    return { x: 0, y: equity, at: p.entry_time ?? '', pnl: p.current_pnl_usdt ?? 0 }
  })
}

function fmtUsdt(v: number | null | undefined, digits = 2) {
  if (v == null) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

// 曲线横轴刻度：entry_time 是 "2026-11-15 02:40:07"，图上只需要"月-日 时:分"
function fmtAt(at: string | undefined) {
  if (!at) return '—'
  const m = at.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/)
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : at
}

export function PaperEquityCard({
  hideValue = false,
  snapshot,
  err = false,
  loading = false,
}: {
  hideValue?: boolean
  snapshot: ReplayPositions | null
  err?: boolean
  loading?: boolean
}) {
  const data = snapshot

  const curve = useMemo(() => buildCurve(data), [data])
  const acct = data?.account
  // 接口有「无快照快版」：DB 持仓先返回，account 可能为空。此时不是"没有账户"，
  // 而是"快照还没算完"，必须单独成态，否则会显示成 起始 — / 已结算 0 的假空值。
  const snapshotLoading = loading
  const openCount = useMemo(
    () => (data ? data.positions.filter((p) => !p.settled).length : 0),
    [data],
  )

  // 曲线几何：SVG 1000×160，上下留 12px 内边距
  // v352: 把「起始权益」也塞进值域 —— 没有参照线时一条单向下滑的线读者无从判断
  // 是"跌了"还是"一直在高位"，加一条 10,000 虚线基线才有语义。
  const geo = useMemo(() => {
    if (curve.length < 2) return null
    const base = data!.account.initial_equity_usdt
    const ys = [...curve.map((p) => p.y), base]
    const min = Math.min(...ys)
    const max = Math.max(...ys)
    const span = Math.max(max - min, 1)
    const H = 160
    const pad = 12
    const toY = (v: number) => H - pad - ((v - min) / span) * (H - pad * 2)
    const pts = curve.map((p, i) => ({
      ...p,
      x: (i / (curve.length - 1)) * 1000,
      y: toY(p.y),
    }))
    const curveMax = Math.max(...curve.map((p) => p.y))
    const curveMin = Math.min(...curve.map((p) => p.y))
    return { pts, min, max, curveMin, curveMax, baseY: toY(base) }
  }, [curve, data])

  const last = curve.length > 0 ? curve[curve.length - 1] : null
  const first = curve.length > 0 ? curve[0] : null
  const chg = last && first ? last.y - data!.account.initial_equity_usdt : null
  const up = (chg ?? 0) >= 0

  return (
    <div className="surface-card px-4 py-3">
      <div className="flex items-center gap-2">
        <Wallet className="h-4 w-4 text-primary" aria-hidden="true" />
        <span className="text-xs uppercase tracking-wider text-muted-foreground">纸面账户</span>
        <span className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[11px] text-primary">
          PAPER
        </span>
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">
          起始 {fmtUsdt(acct?.initial_equity_usdt, 0)} USDT
        </span>
      </div>

      {err ? (
        <p className="mt-2 font-mono text-[11px] text-muted-foreground">
          模拟盘接口暂时不可用，稍后自动重试
        </p>
      ) : snapshotLoading ? (
        <p className="mt-2 font-mono text-[11px] text-muted-foreground">
          账户快照计算中 —— 15s 内自动刷新（后端先回持仓，再补权益/浮盈亏）
        </p>
      ) : (
        <>
          {!hideValue && (
          <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-mono text-2xl font-bold tabular-nums text-foreground">
              {fmtUsdt(acct?.current_equity_usdt)}
            </span>
            <span className="font-mono text-[11px] text-muted-foreground">USDT</span>
            {chg != null ? (
              <span
                className={cn('font-mono text-[11px] tabular-nums', up ? 'text-long' : 'text-short')}
                title="相对起始权益的累计变化"
              >
                {up ? '+' : ''}
                {fmtUsdt(chg)} ({up ? '+' : ''}
                {acct?.initial_equity_usdt ? ((chg / acct.initial_equity_usdt) * 100).toFixed(2) : '—'}%)
              </span>
            ) : null}
            <span className="ml-auto font-mono text-[11px] text-muted-foreground">
              持仓 {openCount} · 已结算 {curve.length}
            </span>
          </div>
          )}

          {geo ? (
            <>
              <svg
                viewBox="0 0 1000 160"
                preserveAspectRatio="none"
                className="mt-1 h-[86px] w-full"
                role="img"
                aria-label={`权益曲线，${curve.length} 个结算点，从 ${geo.curveMin.toFixed(0)} 到 ${geo.curveMax.toFixed(0)} USDT，起点 ${fmtUsdt(acct?.initial_equity_usdt, 0)}`}
              >
              <defs>
                <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor={up ? 'var(--long)' : 'var(--short)'} stopOpacity="0.22" />
                  <stop offset="1" stopColor={up ? 'var(--long)' : 'var(--short)'} stopOpacity="0" />
                </linearGradient>
              </defs>
              {[0.25, 0.5, 0.75].map((f) => (
                <line
                  key={f}
                  x1="0"
                  x2="1000"
                  y1={160 * f}
                  y2={160 * f}
                  stroke="currentColor"
                  strokeOpacity="0.06"
                />
              ))}
              {/* 起始权益基线：让"这条线相对起点是赚是亏"一眼可读 */}
              <line
                x1="0"
                x2="1000"
                y1={geo.baseY}
                y2={geo.baseY}
                stroke="currentColor"
                strokeOpacity="0.3"
                strokeDasharray="6 6"
                vectorEffect="non-scaling-stroke"
              />
              <path
                d={`M${geo.pts.map((p) => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' L')}`}
                fill="none"
                stroke={up ? 'var(--long)' : 'var(--short)'}
                strokeWidth="2"
                vectorEffect="non-scaling-stroke"
              />
              <path
                d={`M${geo.pts[0].x} 160 L${geo.pts.map((p) => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' L')} L${geo.pts[geo.pts.length - 1].x} 160 Z`}
                fill="url(#equity-fill)"
              />
              <circle
                cx={geo.pts[0].x}
                cy={geo.pts[0].y}
                r="2.5"
                fill="none"
                stroke={up ? 'var(--long)' : 'var(--short)'}
                strokeWidth="1.5"
                vectorEffect="non-scaling-stroke"
              />
              <circle
                cx={geo.pts[geo.pts.length - 1].x}
                cy={geo.pts[geo.pts.length - 1].y}
                r="3"
                fill={up ? 'var(--long)' : 'var(--short)'}
              />
            </svg>
              {/* 横轴：首笔 → 末笔结算时间；中间给基线与区间峰谷，替代没有刻度的裸线 */}
              <div className="mt-0.5 flex items-center justify-between gap-2 font-mono text-[11px] text-muted-foreground/75">
                <span title={first?.at}>{fmtAt(first?.at)}</span>
                <span className="flex items-center gap-2">
                  <span className="inline-flex items-center gap-1">
                    <i
                      aria-hidden
                      className="inline-block h-0 w-4 border-t border-dashed border-muted-foreground/50"
                    />
                    起始 {fmtUsdt(acct?.initial_equity_usdt, 0)}
                  </span>
                  <span>
                    结算区间 {fmtUsdt(geo.curveMin, 0)} – {fmtUsdt(geo.curveMax, 0)}
                  </span>
                </span>
                <span title={last?.at}>{fmtAt(last?.at)}</span>
              </div>
            </>
          ) : (
            <p className="mt-2 rounded border border-dashed border-border px-2 py-1.5 font-mono text-[11px] text-muted-foreground">
              已结算 {curve.length} 笔 —— 样本不足以成线，不做趋势外推。每结算一笔，这里会多一个点。
            </p>
          )}

          <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 border-t border-border/60 pt-1.5 font-mono text-[11px] text-muted-foreground sm:grid-cols-4">
            <span>
              已实现{' '}
              <b className={cn('tabular-nums', (acct?.realized_pnl_usdt ?? 0) >= 0 ? 'text-long' : 'text-short')}>
                {(acct?.realized_pnl_usdt ?? 0) >= 0 ? '+' : ''}
                {fmtUsdt(acct?.realized_pnl_usdt)}
              </b>
            </span>
            <span>
              浮盈亏{' '}
              <b className={cn('tabular-nums', (acct?.unrealized_pnl_usdt ?? 0) >= 0 ? 'text-long' : 'text-short')}>
                {acct?.unrealized_pnl_usdt == null
                  ? '待行情'
                  : `${(acct.unrealized_pnl_usdt >= 0 ? '+' : '')}${fmtUsdt(acct.unrealized_pnl_usdt)}`}
              </b>
            </span>
            <span>
              可用 <b className="tabular-nums text-foreground">{fmtUsdt(acct?.available_equity_usdt)}</b>
            </span>
            <span>
              占用 <b className="tabular-nums text-foreground">{fmtUsdt(acct?.used_margin_usdt)}</b>
            </span>
          </div>
        </>
      )}
    </div>
  )
}
