'use client'

/**
 * v302+ P1-B 市场基本面检测面板
 * 需求 14: 7 类市场 regime 实时检测 (基于 CFTC COT 数据)
 *
 * 数据流:
 *   1. 拉取 /api/market-regime?days=N
 *   2. 渲染: 顶部 6 个 regime 计数 + 全局风险卡片 + 各资产 spec/oi 详情
 */

import { useCallback, useEffect, useState } from 'react'
import {
  Activity, AlertTriangle, ArrowDown, ArrowUp, BarChart3,
  Layers, RefreshCcw, TrendingDown, TrendingUp, Zap,
} from 'lucide-react'
import { apiGet } from '@/lib/api'
import { useToast } from '@/components/ui/toast'

type Regime =
  | 'leverage_buildup'
  | 'deleveraging'
  | 'large_spec_long'
  | 'large_spec_short'
  | 'commercial_hedging'
  | 'risk_off_rotation'
  | 'spec_extreme'
  // v308 P1-bugfix: 后端还有第 8/9 类 (基于 Binance 永续资金费率)。
  // 之前前端只认 7 类, RegimeCard 里 `if (!meta) return null` 会把这两类
  // **静默丢弃** —— 面板显示"无活跃 regime", 但后端其实判定出了极端资金费率。
  | 'funding_extreme_long'
  | 'funding_extreme_short'

type RegimeEvent = {
  asset?: string
  regime: Regime
  report_date: string
  oi_chg_pct?: number
  spec_net_pct?: number
  spec_delta_pct?: number
  comm_delta_pct?: number
  comm_now_pct?: number
  percentile?: number
  direction?: 'increasing' | 'decreasing'
  /**
   * v308 P1-bugfix: 后端资金费率类 regime 的 side 取值是 'long_pay'/'short_pay',
   * 与 COT 类的 'long'/'short' 不同。之前类型只写了 'long'|'short',
   * 导致 `ev.side === 'long_pay'` 在 TS 里是"类型无交集"的编译错误。
   */
  side?: 'long' | 'short' | 'long_pay' | 'short_pay'
  crypto_oi_chg_pct?: number
  safe_oi_chg_pct?: number
  assets_crypto?: string[]
  assets_safe?: string[]
  intensity: 'strong' | 'moderate' | 'extreme'
  score: number
  /** v308 P1: 资金费率类 regime 专有字段 */
  current_rate?: number
  annualized_pct?: number
  data_source?: string
}

type AssetSummary = {
  report_date: string
  open_interest: number
  oi_chg_pct: number
  spec_net_pct: number
  spec_delta_pct: number
}

type RegimeResponse = {
  ok: boolean
  n_regimes: number
  n_assets: number
  regime_counts: Record<string, number>
  regimes: RegimeEvent[]
  by_asset: Record<string, AssetSummary>
  since_date: string
  /** v308 P1: 后端明确报错时的说明 (ok=false 时有值) */
  error?: string
  /** 后端降级返回 (例如外部行情源不可达导致部分数据缺失) */
  degraded?: boolean
  /** 资金面 (Binance 永续费率) 取数状态: 哪些币种取到了, 哪些没取到 */
  funding?: {
    ok?: boolean
    /** 本轮取到实时数据、参与判定的币种 */
    checked?: string[]
    /** 因超时预算/取数失败完全没取到的币种 */
    skipped?: string[]
    /** 只拿到历史缓存 (外部源不可达)、本轮未参与判定的币种 */
    stale?: string[]
    note?: string
  }
}

const REGIME_META: Record<Regime, { label: string; desc: string; color: string; icon: any }> = {
  leverage_buildup: {
    label: '机构开杠杆',
    desc: 'OI 单周↑5%+ 机构多头大增',
    color: 'border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-300',
    icon: Zap,
  },
  deleveraging: {
    label: '套利离场',
    desc: 'OI 单周↓5%+ 多头萎缩',
    color: 'border-orange-500/50 bg-orange-500/10 text-orange-600 dark:text-orange-300',
    icon: TrendingDown,
  },
  large_spec_long: {
    label: '大资金做多',
    desc: '非商业净多 > 历史 80% 分位',
    color: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
    icon: TrendingUp,
  },
  large_spec_short: {
    label: '大资金做空',
    desc: '非商业净空 < 历史 20% 分位',
    color: 'border-rose-500/50 bg-rose-500/10 text-rose-600 dark:text-rose-300',
    icon: TrendingDown,
  },
  commercial_hedging: {
    label: '产业套保激增',
    desc: 'commercial_net 周变 > 5% OI',
    color: 'border-violet-500/50 bg-violet-500/10 text-violet-600 dark:text-violet-300',
    icon: Layers,
  },
  risk_off_rotation: {
    label: '集体撤资',
    desc: 'crypto OI↓ & 贵金属 OI↑',
    color: 'border-rose-600/50 bg-rose-600/10 text-rose-700 dark:text-rose-300',
    icon: AlertTriangle,
  },
  spec_extreme: {
    label: '极端投机 (反转预警)',
    desc: '净持仓 > 历史 95% 分位',
    color: 'border-fuchsia-500/50 bg-fuchsia-500/10 text-fuchsia-600 dark:text-fuchsia-300',
    icon: Activity,
  },
  // v308 P1-bugfix: 补齐后端第 8/9 类。没有这两条, 资金面极端信号会被
  // `if (!meta) return null` 静默吃掉。
  funding_extreme_long: {
    label: '资金费率极端偏多',
    desc: '永续费率 ≥ 0.03%/8h (≈33% APR), 多头付费',
    color: 'border-orange-500/50 bg-orange-500/10 text-orange-600 dark:text-orange-300',
    icon: TrendingUp,
  },
  funding_extreme_short: {
    label: '资金费率极端偏空',
    desc: '永续费率 ≤ -0.03%/8h, 空头付费',
    color: 'border-teal-500/50 bg-teal-500/10 text-teal-600 dark:text-teal-300',
    icon: TrendingDown,
  },
}

function pctFmt(n: number | null | undefined, digits = 1) {
  if (n === null || n === undefined) return '—'
  const s = n >= 0 ? '+' : ''
  return `${s}${n.toFixed(digits)}%`
}

function intensityColor(intensity: string) {
  if (intensity === 'extreme') return 'text-fuchsia-600 dark:text-fuchsia-300'
  if (intensity === 'strong') return 'text-rose-500'
  return 'text-muted-foreground'
}

function RegimeCard({ ev }: { ev: RegimeEvent }) {
  const meta = REGIME_META[ev.regime as Regime]
  if (!meta) return null
  const Icon = meta.icon
  // 描述细节
  let detail = ''
  if (ev.regime === 'leverage_buildup' || ev.regime === 'deleveraging') {
    detail = `OI ${pctFmt(ev.oi_chg_pct)} · 净持仓 ${pctFmt(ev.spec_net_pct)} (Δ${pctFmt(ev.spec_delta_pct)})`
  } else if (ev.regime === 'large_spec_long' || ev.regime === 'large_spec_short') {
    detail = `净持仓 ${pctFmt(ev.spec_net_pct)} · 历史分位 ${ev.percentile?.toFixed(0)}%`
  } else if (ev.regime === 'commercial_hedging') {
    detail = `产业净持仓 Δ${pctFmt(ev.comm_delta_pct)} (现 ${pctFmt(ev.comm_now_pct)}) · ${ev.direction === 'increasing' ? '↑ 套保增' : '↓ 套保减'}`
  } else if (ev.regime === 'risk_off_rotation') {
    detail = `crypto OI ${pctFmt(ev.crypto_oi_chg_pct)} · 贵金属 OI ${pctFmt(ev.safe_oi_chg_pct)}`
  } else if (ev.regime === 'spec_extreme' || ev.regime === 'funding_extreme_long' || ev.regime === 'funding_extreme_short') {
    // v308 P1: 资金费率类显示费率本身 (8h 费率 + 年化), 不是持仓分位
    if (ev.regime === 'funding_extreme_long' || ev.regime === 'funding_extreme_short') {
      const ratePct = ev.current_rate != null ? (ev.current_rate * 100).toFixed(4) : '—'
      const ann = ev.annualized_pct != null ? `${ev.annualized_pct.toFixed(1)}% APR` : '—'
      detail = `8h 费率 ${ratePct}% · 年化 ${ann} · ${ev.side === 'long_pay' ? '多头付费' : '空头付费'}`
    } else {
      detail = `净持仓 ${pctFmt(ev.spec_net_pct)} · ${ev.side === 'long' ? '极端多' : '极端空'} · 分位 ${ev.percentile?.toFixed(0)}%`
    }
  }
  return (
    <div className={`rounded-md border p-3 ${meta.color}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4" />
          <div>
            <div className="font-mono text-xs font-semibold">
              {meta.label}
              {ev.asset && <span className="ml-2 text-[11px] text-muted-foreground">@ {ev.asset}</span>}
              {!ev.asset && ev.assets_crypto && (
                <span className="ml-2 text-[11px] text-muted-foreground">@ {ev.assets_crypto.join('+')}/{ev.assets_safe?.join('+')}</span>
              )}
            </div>
            <div className="text-[11px] text-muted-foreground">{meta.desc}</div>
          </div>
        </div>
        <div className="flex flex-col items-end gap-0.5">
          <span className={`font-mono text-[11px] uppercase ${intensityColor(ev.intensity)}`}>
            {ev.intensity}
          </span>
          <span className="font-mono text-[11px] text-muted-foreground">
            score {(ev.score * 100).toFixed(0)}
          </span>
        </div>
      </div>
      <div className="mt-2 font-mono text-[11px] text-foreground/80">{detail}</div>
      <div className="mt-1 font-mono text-[11px] text-muted-foreground">
        as of {ev.report_date}
      </div>
    </div>
  )
}

function AssetRow({ asset, data }: { asset: string; data: AssetSummary }) {
  const oi = data.oi_chg_pct
  const spec = data.spec_net_pct
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border/40 px-3 py-2 text-xs last:border-0">
      <div className="flex w-20 items-center gap-1.5 font-mono font-semibold">
        <BarChart3 className="h-3 w-3 text-muted-foreground" />
        {asset}
      </div>
      <div className="flex flex-1 items-center justify-end gap-4 font-mono text-[11px]">
        <span className="text-muted-foreground">OI {pctFmt(oi)}</span>
        <span className={spec >= 0 ? 'text-emerald-500' : 'text-rose-500'}>
          {spec >= 0 ? <ArrowUp className="inline h-3 w-3" /> : <ArrowDown className="inline h-3 w-3" />}
          {pctFmt(spec)}
        </span>
        <span className="text-muted-foreground">Δ {pctFmt(data.spec_delta_pct)}</span>
      </div>
    </div>
  )
}

export function MarketRegimePanel() {
  const { toast } = useToast()
  const [data, setData] = useState<RegimeResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [windowDays, setWindowDays] = useState(365)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      // 走 apiGet —— 主服务 stale 时 (端点 404), apiGet 会自动
      // fallback 到 8100 桥接。之前这里裸 fetch API_BASE, 面板永远拿不到真实数据。
      const json = await apiGet<RegimeResponse>(`/market-regime?days=${windowDays}`)
      if (!json?.ok) {
        // 后端明确说"没算出来" —— 如实报错, 不要渲染成"0 个信号 / 风险 LOW"
        setData(null)
        setError(json?.error ?? 'market-regime 未返回有效数据')
        return
      }
      setData(json)
      setError(null)
    } catch (e: any) {
      setData(null)
      setError(e?.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }, [windowDays])

  useEffect(() => { load() }, [load])

  const counts = data?.regime_counts ?? {}
  const regimes = data?.regimes ?? []
  const byAsset = data?.by_asset ?? {}

  const totalScore = regimes.reduce((s, r) => s + r.score, 0)
  const riskLevel = totalScore > 4 ? 'HIGH' : totalScore > 2 ? 'MEDIUM' : 'LOW'
  const riskColor = riskLevel === 'HIGH' ? 'text-rose-500' : riskLevel === 'MEDIUM' ? 'text-amber-500' : 'text-emerald-500'

  return (
    <div className="mx-auto w-full max-w-[1600px] p-3 md:p-6">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold tracking-tight">市场基本面状态</h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
            基于 CFTC COT 周度持仓数据检测 7 类市场 regime (大资金/杠杆/产业套保/集体撤资/极端投机).
            数据源: 13 个资产 × 周度 COT 报告.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={windowDays}
            onChange={e => setWindowDays(parseInt(e.target.value, 10))}
            className="rounded-md border border-border bg-card px-2 py-1 font-mono text-xs"
          >
            <option value={90}>近 90 天</option>
            <option value={180}>近 180 天</option>
            <option value={365}>近 365 天</option>
            <option value={730}>近 2 年</option>
          </select>
          <button
            onClick={() => { load(); toast.success('已刷新') }}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 font-mono text-xs hover:bg-secondary"
          >
            <RefreshCcw className="h-3 w-3" /> 刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 rounded-md border border-rose-500/50 bg-rose-500/10 p-3 text-xs text-rose-600">
          错误: {error}
        </div>
      )}
      {data?.funding?.skipped?.length ? (
        <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-600 dark:text-amber-300">
          资金面数据未取全 (外部行情源超时): {data.funding.skipped.join(', ')} —— 这几个币种本轮未参与资金费率判定, 其余 regime 仍然有效。
        </div>
      ) : null}
      {data?.funding?.stale?.length ? (
        <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-600 dark:text-amber-300">
          资金面数据为历史快照 (外部行情源当前不可达): {data.funding.stale.join(', ')} —— 这几个币种本轮**未参与**资金费率判定, 面板不会基于旧数据下极端结论。
        </div>
      ) : null}
      {loading && !data && (
        <div className="p-6 text-center font-mono text-xs text-muted-foreground">加载市场状态…</div>
      )}

      {data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-8">
            <div className="rounded-md border border-border bg-card p-3">
              <div className="font-mono text-[11px] text-muted-foreground">总风险</div>
              <div className={`mt-1 font-mono text-lg font-semibold ${riskColor}`}>{riskLevel}</div>
              <div className="font-mono text-[11px] text-muted-foreground">
                score {totalScore.toFixed(1)} · {regimes.length} signals
              </div>
            </div>
            {(['leverage_buildup', 'deleveraging', 'large_spec_long', 'large_spec_short',
               'commercial_hedging', 'risk_off_rotation', 'spec_extreme',
               'funding_extreme_long', 'funding_extreme_short'] as Regime[]).map(r => {
              const meta = REGIME_META[r]
              const n = counts[r] ?? 0
              return (
                <div key={r} className={`rounded-md border p-3 ${n > 0 ? meta.color : 'border-border bg-card opacity-50'}`}>
                  <div className="font-mono text-[11px] uppercase">{meta.label}</div>
                  <div className="mt-1 font-mono text-lg font-semibold">{n}</div>
                  <div className="font-mono text-[11px] text-muted-foreground">{meta.desc}</div>
                </div>
              )
            })}
          </div>

          {regimes.length === 0 ? (
            <div className="rounded-md border border-border bg-card p-6 text-center text-xs text-muted-foreground">
              当前时间窗口内无活跃 regime (所有 7 类指标都在中性区间).
            </div>
          ) : (
            <div className="mb-4">
              <h3 className="mb-2 font-mono text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                活跃信号 ({regimes.length}) — 按 score 排序
              </h3>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
                {regimes.map((r, i) => <RegimeCard key={i} ev={r} />)}
              </div>
            </div>
          )}

          <div className="rounded-md border border-border bg-card">
            <div className="border-b border-border px-3 py-2">
              <h3 className="font-mono text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                13 资产最新 COT ({data.since_date} 起) — OI / 机构净持仓 %
              </h3>
            </div>
            <div className="divide-y divide-border/40">
              {Object.entries(byAsset).map(([asset, a]) => (
                <AssetRow key={asset} asset={asset} data={a} />
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}


