'use client'

import { useMemo, useState } from 'react'
import type { SimSignal } from '@/lib/replay-data'
import {
  actionColor,
  fmtPct,
  fmtPrice,
  fmtTime,
  verdictColor,
} from '@/lib/replay-data'

type Props = {
  signals: SimSignal[]
  loading: boolean
  selectedId: number | null
  onSelect: (signal: SimSignal) => void
  onReload: () => void
}

const ASSET_OPTIONS = ['', 'BTC', 'ETH', 'SOL', 'XAU', 'WTI']
const ACTION_OPTIONS = ['', 'BUY', 'SELL', 'HOLD']

export function ReplayList({ signals, loading, selectedId, onSelect, onReload }: Props) {
  const [asset, setAsset] = useState('')
  const [action, setAction] = useState('')

  const filtered = useMemo(() => {
    return signals.filter((s) => {
      if (asset && s.asset.toUpperCase() !== asset.toUpperCase()) return false
      if (action && s.action.toUpperCase() !== action.toUpperCase()) return false
      return true
    })
  }, [signals, asset, action])
  const pendingCount = filtered.filter((signal) => !signal.settled).length
  const settledCount = filtered.length - pendingCount

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-mono text-xs uppercase tracking-widest text-muted-foreground">持仓与盈亏记录</h3>
        <span className="rounded-md border border-hold/40 bg-hold/10 px-2 py-0.5 font-mono text-[10px] text-hold">
          持仓中 {pendingCount}
        </span>
        <span className="rounded-md border border-border bg-secondary px-2 py-0.5 font-mono text-[10px] text-foreground">
          盈亏记录 {settledCount}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Filter label="品种" value={asset} options={ASSET_OPTIONS} onChange={setAsset} />
          <Filter label="方向" value={action} options={ACTION_OPTIONS} onChange={setAction} />
          <button
            onClick={onReload}
            className="rounded-md border border-border bg-card px-2 py-1 font-mono text-[10px] text-foreground transition hover:border-primary/40"
          >
            ⟳ 刷新
          </button>
        </div>
      </div>

      <div className="thin-scroll min-h-0 flex-1 overflow-y-auto rounded-md border border-border bg-card">
        {loading && signals.length === 0 ? (
          <div className="p-3 font-mono text-xs text-muted-foreground">加载中…</div>
        ) : filtered.length === 0 ? (
          <EmptyState />
        ) : (
          <ul className="divide-y divide-border">
            {filtered.map((s) => {
              const selected = s.id === selectedId
              return (
                <li
                  key={s.id}
                  onClick={() => onSelect(s)}
                  className={`cursor-pointer px-3 py-2 transition hover:bg-accent/60 ${
                    selected ? 'bg-accent ring-1 ring-primary/40' : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[10px] text-muted-foreground">#{s.id}</span>
                      <span className="font-mono text-sm font-bold text-foreground">{s.asset}</span>
                      <span
                        className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold ${actionColor(
                          s.action
                        )}`}
                      >
                        {s.action}
                      </span>
                      <span
                        className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${verdictColor(
                          s.is_correct
                        )}`}
                      >
                        {s.settled ? (s.is_correct || '已结算') : '持仓中'}
                      </span>
                      <ImpactBadge horizon={s.expected_horizon} />
                    </div>
                    <span className="font-mono text-[10px] text-muted-foreground">{fmtTime(s.entry_time)}</span>
                  </div>
                  <div className="mt-1 grid grid-cols-3 gap-2 font-mono text-[10px] text-muted-foreground">
                    <span>
                      入场{' '}
                      <span className="text-foreground">{fmtPrice(s.entry_price, s.asset)}</span>
                    </span>
                    <span>
                      {s.settled ? '出场' : '当前'}{' '}
                      {s.settled && s.partial_exit_price != null && s.partial_exit_price > 0 ? (
                        <span className="text-foreground" title={`分级止盈：已平 ${(s.partial_exit_ratio ?? 0) * 100}% @ ${fmtPrice(s.partial_exit_price, s.asset)}，剩余 ${(1 - (s.partial_exit_ratio ?? 0)) * 100}% @ ${fmtPrice(s.exit_price, s.asset)}`}>
                          <span className="text-hold">{fmtPrice(s.partial_exit_price, s.asset)}</span>
                          {' + '}
                          {fmtPrice(s.exit_price, s.asset)}
                        </span>
                      ) : (
                        <span className="text-foreground">{fmtPrice(s.settled ? s.exit_price : s.current_price, s.asset)}</span>
                      )}
                    </span>
                    <span
                      className={
                        ((s.settled ? s.forward_pnl : s.current_pnl_pct) || 0) >= 0 ? 'text-long' : 'text-short'
                      }
                    >
                      PnL {fmtPct(s.settled ? s.forward_pnl : s.current_pnl_pct)}
                    </span>
                  </div>
                  <div className="mt-1 line-clamp-1 text-[11px] text-muted-foreground">
                    {/fred|stlouisfed/i.test(s.source || '') && (
                      <span className="mr-1 rounded bg-primary/15 px-1 py-px font-mono text-[9px] font-bold uppercase tracking-wider text-primary">
                        FRED
                      </span>
                    )}
                    {s.news_text || '(无新闻文本)'}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}

// 影响类型标注：按 AI 的 expected_horizon（intraday=即时 / 1-3d、1w+=中长期）
function ImpactBadge({ horizon }: { horizon: string | null }) {
  const h = (horizon || '').toLowerCase()
  if (h === 'intraday') {
    return (
      <span
        className="rounded border border-short/40 bg-short/10 px-1.5 py-0.5 font-mono text-[10px] text-short"
        title="影响集中在事件当下，短周期交易"
      >
        即时影响
      </span>
    )
  }
  if (h === '1-3d' || h === '1w+') {
    return (
      <span
        className="rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-primary"
        title="对行情有中长期影响，可考虑趋势/波段周期"
      >
        中长期影响
      </span>
    )
  }
  return null
}

function Filter({  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: string[]
  onChange: (v: string) => void
}) {
  return (
    <label className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
      <span>{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-border bg-card px-1.5 py-1 font-mono text-[10px] text-foreground focus:border-primary"
      >
        {options.map((o) => (
          <option key={o || 'all'} value={o}>
            {o || '全部'}
          </option>
        ))}
      </select>
    </label>
  )
}

function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
      <div className="font-mono text-xs text-muted-foreground">暂无持仓或盈亏记录</div>
      <div className="max-w-[280px] font-mono text-[10px] leading-relaxed text-muted-foreground">
        新闻通过 AI 分析和当前策略过滤并取得真实入场价后，会自动开仓为持仓（2 小时追踪）；到期按真实行情结算后自动保留为盈亏记录。
      </div>
    </div>
  )
}
