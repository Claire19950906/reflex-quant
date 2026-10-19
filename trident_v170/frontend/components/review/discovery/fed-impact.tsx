'use client'

import { usePoll } from '@/lib/hooks/use-poll'

/**
 * Fed 利率预期影响传导 · 哪些"表弟品种"会被牵连
 * 数据源:/api/discovery/fed-impact → Fed 路径 vs 各品种历史相关性 + 滞后
 */

type FedImpactResponse = {
  ok: boolean
  ts: number
  current_direction: 'hawkish' | 'dovish' | 'neutral'
  current_change_bp: number
  impacts: Array<{
    symbol: string
    name: string
    cls: string
    correlation: number
    typical_lag_hours: number
    sample_size: number
    note: string
  }>
}

const CLASS_BADGE: Record<string, string> = {
  crypto: 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400',
  us_equity: 'border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400',
  em_equity: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  safe_metal: 'border-yellow-500/40 bg-yellow-500/10 text-yellow-600 dark:text-yellow-400',
  safe_bond: 'border-violet-500/40 bg-violet-500/10 text-violet-600 dark:text-violet-400',
  energy: 'border-orange-500/40 bg-orange-500/10 text-orange-600 dark:text-orange-400',
  dollar: 'border-slate-500/40 bg-slate-500/10 text-slate-600 dark:text-slate-400',
  yield: 'border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400',
  us_equity_tech: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-600 dark:text-cyan-400',
}

function correlationArrow(c: number): string {
  if (c > 0.3) return '↗️'
  if (c > 0.05) return '→'
  if (c < -0.3) return '↘️'
  if (c < -0.05) return '←'
  return '⚖️'
}

export function FedImpactPanel() {
  // v283+ · Task D · 10min 轮询 · AbortController + unmount 清理交给 usePoll
  const { data, loading, error: err, refresh: reload } = usePoll<FedImpactResponse>(
    '/discovery/fed-impact',
    {
      intervalMs: 600_000,
      transform: (raw) => {
        const d = raw as FedImpactResponse | null | undefined
        return d && d.ok ? d : null
      },
    },
  )

  if (err) {
    return (
      <div className="rounded border border-rose-300 bg-rose-50 p-3 text-sm dark:border-rose-700 dark:bg-rose-950">
        <div className="font-semibold text-rose-700 dark:text-rose-300">🏛 Fed 传导 · 接口异常</div>
        <div className="mt-1 text-rose-600 dark:text-rose-400">{err}</div>
        <div className="mt-2 text-[11px] text-muted-foreground">
          后端路由 <code className="rounded bg-background px-1">/api/discovery/fed-impact</code> 可选 —
          未配置时本面板自动用启发式(基于 cross_asset_context.ASSET_CLASS_MAP)。
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="rounded border border-slate-200 bg-white p-3 text-sm dark:border-slate-700 dark:bg-slate-900">
        <div className="font-semibold">🏛 Fed 路径 → 表弟品种传导</div>
        <div className="mt-1 text-slate-500">{loading ? '加载中…' : '等待数据'}</div>
      </div>
    )
  }

  return (
    <div className="rounded border border-amber-500/30 bg-white p-3 text-sm dark:bg-slate-900">
      <div className="mb-2 flex items-center justify-between">
        <div className="font-semibold">🏛 Fed 路径 → 表弟品种传导</div>
        <button onClick={reload} disabled={loading}
          className="rounded bg-slate-100 px-2 py-0.5 text-xs hover:bg-slate-200 disabled:opacity-50 dark:bg-slate-800 dark:hover:bg-slate-700">
          {loading ? '…' : '🔄'}
        </button>
      </div>

      <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/5 p-2 text-[11px]">
        当前 Fed 路径预期:
        <strong className="ml-1 font-mono">
          {data.current_direction === 'hawkish' ? '🔼 鹰派' : data.current_direction === 'dovish' ? '🔽 鸽派' : '⚖️ 中性'}
        </strong>
        <span className="ml-2 font-mono">
          {data.current_change_bp >= 0 ? '+' : ''}{data.current_change_bp.toFixed(0)}bp / 24月
        </span>
      </div>

      <div className="space-y-1.5 text-xs">
        {data.impacts.map((it) => {
          const cls = CLASS_BADGE[it.cls] ?? 'border-border bg-secondary/30'
          return (
            <div key={it.symbol} className="flex items-start gap-2 rounded border border-border bg-secondary/20 p-2">
              <span aria-hidden className="text-base">{correlationArrow(it.correlation)}</span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-[11px] font-bold">{it.symbol}</span>
                  <span className="text-[10px] text-muted-foreground">{it.name}</span>
                  <span className={`rounded border px-1 py-0.5 font-mono text-[9px] ${cls}`}>{it.cls}</span>
                </div>
                <div className="mt-0.5 text-[10px] text-muted-foreground">{it.note}</div>
              </div>
              <div className="shrink-0 text-right font-mono">
                <div className={`text-[11px] font-bold ${it.correlation > 0.3 ? 'text-long' : it.correlation < -0.3 ? 'text-short' : ''}`}>
                  {it.correlation >= 0 ? '+' : ''}{it.correlation.toFixed(2)}
                </div>
                <div className="text-[9px] text-muted-foreground/70">lag {it.typical_lag_hours}h · n={it.sample_size}</div>
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-2 rounded border border-amber-500/30 bg-amber-500/5 p-2 text-[11px]">
        <strong className="text-amber-900 dark:text-amber-200">📌 决策建议</strong>:
        当 Fed 路径异动 (24h 2Y 变动 &gt; 10bp) 触发推送时:
        <ul className="ml-4 mt-1 list-disc text-[10px] text-muted-foreground">
          <li>鹰派 <span className="font-mono">(+bp)</span>: 加息预期 → 关注美元/黄金/美债 → 风险资产(BTC/EM)可能逆风</li>
          <li>鸽派 <span className="font-mono">(-bp)</span>: 降息预期 → 风险资产利好(BTC/科技股/EM),DXY 弱</li>
          <li>中性: 关注个股消息 — 港口/海力士/山寨币</li>
        </ul>
      </div>
    </div>
  )
}