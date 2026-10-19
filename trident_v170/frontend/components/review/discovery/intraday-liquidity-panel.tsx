'use client'

import { useMemo } from 'react'
import { Activity, AlertTriangle, ArrowDown, ArrowUp, Zap, RefreshCw } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { cn } from '@/lib/utils'

/* ════════════════════════════════════════════════════════════════════════
 * ⚡ 日内流动性情绪仪表盘(v170+ P2 · 第 2 项)
 *
 * 设计动机:用户原话——
 *   「发现页增加日内/实时数据(开盘就能看到,观察资金情绪,
 *    今天这种大跌看的指标)—— 10 期美债收益率、美元指数 DXY、
 *    VIX 恐慌指数、CME 美联储观察(FedWatch)」。
 *
 * 实现要点:
 *  ① 数据源:
 *     · UST10Y / UST5Y / DXY / ES / NQ / yield_curve_spread / funding_rate
 *       ←  /api/v189/market/all  (yfinance + OKX, 60s 缓存)
 *     · VIX ←  /api/external/indicators?indicator=VIX  (yfinance ^VIX)
 *     · 拉取失败单独占位,不影响其它卡片。
 *  ② 视觉: 5 张大卡片(UST10Y · DXY · VIX · ES · NQ),大字数值 + 涨跌箭头 + 涨跌幅,
 *     涨跌色与图标颜色联动;顶部一行「今日情绪」文字汇总。
 *  ③ 24h 跳动: 每 60s 拉一次(后端 60s 缓存,前端不浪费 API 配额)。
 *  ④ 周末/夜间安全:数据陈旧(>2h)→ 显示「⏸ 非交易时段」+ 暗色徽章。
 * ════════════════════════════════════════════════════════════════════════ */

type AllSource = {
  ok: boolean
  data?: Record<string, any> | null
  error?: string
}

type AllResp = {
  ok: boolean
  ts?: number
  sources?: Record<string, AllSource>
}

type VixRow = {
  ts: number
  value: number
  meta?: Record<string, any>
  ts_iso?: string
}

type CardDef = {
  key: 'us10y' | 'dxy' | 'es' | 'nq' | 'us5y'
  title: string
  unit: string
  higherIs: 'hawkish' | 'dovish' | 'risk_on' | 'risk_off' | 'neutral'
  icon: string
  path: string[]
  changePath: string[]
  hint: string
}

const CARDS: CardDef[] = [
  {
    key: 'us10y', title: 'UST10Y · 10Y 美债', unit: '%',
    higherIs: 'hawkish', icon: '📈',
    path: ['sources', 'us10y', 'data'],
    changePath: ['sources', 'us10y', 'data', 'change_bp'],
    hint: '全球资产定价锚 · 上行压制风险资产估值',
  },
  {
    key: 'us5y', title: 'UST5Y · 5Y 美债', unit: '%',
    higherIs: 'hawkish', icon: '📉',
    path: ['sources', 'us5y', 'data'],
    changePath: ['sources', 'us5y', 'data', 'change_bp'],
    hint: '短端利率敏感 · 衰退期常被前瞻交易',
  },
  {
    key: 'dxy', title: 'DXY · 美元指数', unit: '',
    higherIs: 'risk_off', icon: '💵',
    path: ['sources', 'dxy', 'data'],
    changePath: ['sources', 'dxy', 'data', 'change_pct'],
    hint: '加息 → 美元强 · 降息 → 美元弱',
  },
  {
    key: 'es', title: 'ES · 标普期货', unit: '',
    higherIs: 'risk_on', icon: '🟢',
    path: ['sources', 'es', 'data'],
    changePath: ['sources', 'es', 'data', 'change_pct'],
    hint: '美股盘前/盘中实时 · 风险情绪先行',
  },
  {
    key: 'nq', title: 'NQ · 纳指期货', unit: '',
    higherIs: 'risk_on', icon: '🟣',
    path: ['sources', 'nq', 'data'],
    changePath: ['sources', 'nq', 'data', 'change_pct'],
    hint: '科技股 · 贴现率敏感 · 降息利好更强',
  },
]

function pick(obj: any, path: string[]): any {
  let cur: any = obj
  for (const k of path) {
    if (cur == null) return null
    cur = cur[k]
  }
  return cur
}

function fmtValue(v: any, digits: number, unit: string): string {
  if (v == null || (typeof v === 'number' && !Number.isFinite(v))) return '—'
  return `${(v as number).toFixed(digits)}${unit}`
}
function fmtSigned(v: any, digits: number, unit: string): string {
  if (v == null || (typeof v === 'number' && !Number.isFinite(v))) return '—'
  const n = v as number
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}${unit}`
}

/** 计算情绪文字 + tone. */
function regimeOf(payload: {
  us10y?: Record<string, any> | null
  dxy?: Record<string, any> | null
  vix?: { value?: number; change_pct?: number | null } | null
  es?: Record<string, any> | null
}): { text: string; tone: 'bear' | 'bull' | 'neutral' } {
  const us10yChg = payload.us10y?.change_bp
  const dxyChg = payload.dxy?.change_pct
  const vix = payload.vix?.value
  const vixChg = payload.vix?.change_pct
  const esChg = payload.es?.change_pct

  let hawkScore = 0
  let riskOffScore = 0
  let bullScore = 0

  if (typeof us10yChg === 'number') {
    if (us10yChg > 3) hawkScore += 2
    else if (us10yChg > 0) hawkScore += 1
    else if (us10yChg < -3) hawkScore -= 1
  }
  if (typeof dxyChg === 'number') {
    if (dxyChg > 0.3) riskOffScore += 1
    else if (dxyChg < -0.3) riskOffScore -= 1
  }
  if (typeof vix === 'number') {
    if (vix > 30) riskOffScore += 2
    else if (vix > 22) riskOffScore += 1
    else if (vix < 14) riskOffScore -= 1
  }
  if (typeof vixChg === 'number' && vixChg > 5) riskOffScore += 1
  if (typeof esChg === 'number') {
    if (esChg > 0.5) bullScore += 1
    else if (esChg < -0.5) bullScore -= 1
  }

  const score = hawkScore + riskOffScore - bullScore
  if (score >= 2) {
    return { text: '避险情绪强 · 流动性紧 · 美债收益率/美元/VIX 同步走高', tone: 'bear' }
  }
  if (score <= -2) {
    return { text: '风险偏好回升 · 流动性宽松 · 利率/美元回落', tone: 'bull' }
  }
  return { text: '中性震荡 · 关注美债异常重定价 + VIX 跳升信号', tone: 'neutral' }
}

export function IntradayLiquidityPanel() {
  // v283+ · 改用两个独立 usePoll 替代手写双 fetch。
  //   /v189/market/all        - 主行情(US10Y/DXY/ES/NQ)
  //   /external/indicators... - VIX
  // 两路独立轮询、互不耦合;若后端日后把 VIX 合入 market/all(解决 #8),
  // 把第二个 usePoll 删掉即可,组件渲染逻辑不需要改。
  const allPoll = usePoll<AllResp>('/v189/market/all', {
    intervalMs: 60_000,
    // market/all 没有 ok 字段,直接保留 raw 即可
    transform: (raw) => raw as AllResp,
  })
  const vixPoll = usePoll<VixRow[] | { ok: boolean; rows?: VixRow[] }>(
    '/external/indicators?indicator=VIX&limit=10',
    {
      intervalMs: 60_000,
      transform: (raw) => {
        // 兼容两种返回:直接 array, 或 { ok, rows: [] }
        if (Array.isArray(raw)) return raw as VixRow[]
        const r = raw as { ok?: boolean; rows?: VixRow[] }
        return Array.isArray(r.rows) ? r.rows : []
      },
    },
  )
  const data = allPoll.data
  const vixRows = (Array.isArray(vixPoll.data) ? vixPoll.data : []) as VixRow[]
  const loading = allPoll.loading || vixPoll.loading
  // updatedAt:取两路最新一次成功时间戳(ms);任一路失败也不影响渲染
  const updatedAt = Math.max(allPoll.lastFetchedAt ?? 0, vixPoll.lastFetchedAt ?? 0) || null
  // 手动刷新按钮(原 onClick={reload})
  const reload = () => { void allPoll.refresh(); void vixPoll.refresh() }

  const summary = useMemo(() => {
    const us10y = data ? pick(data, ['sources', 'us10y', 'data']) : null
    const dxy = data ? pick(data, ['sources', 'dxy', 'data']) : null
    const es = data ? pick(data, ['sources', 'es', 'data']) : null
    const nq = data ? pick(data, ['sources', 'nq', 'data']) : null
    const vixLatest = vixRows[0]
    const vixValue = vixLatest?.value
    const vixPrev = vixRows[1]?.value
    const vixChangePct = (vixValue != null && vixPrev != null && vixPrev !== 0)
      ? ((vixValue - vixPrev) / vixPrev * 100)
      : null
    return {
      us10y, dxy, es, nq,
      vix: vixValue != null ? { value: vixValue, change_pct: vixChangePct } : null,
    }
  }, [data, vixRows])

  const regime = useMemo(() => regimeOf(summary), [summary])

  // 单卡渲染
  const renderCard = (def: CardDef) => {
    const v = data ? pick(data, def.path) : null
    const chg = data ? pick(data, def.changePath) : null
    const num = (v && typeof v === 'object') ? (v as any).current ?? (v as any).price ?? (v as any).last ?? (v as any).value : v
    const isUp = typeof chg === 'number' ? chg > 0 : null
    const isDown = typeof chg === 'number' ? chg < 0 : null
    let badUp = false
    if (def.higherIs === 'hawkish' || def.higherIs === 'risk_off') {
      badUp = isUp === true
    } else if (def.higherIs === 'dovish' || def.higherIs === 'risk_on') {
      badUp = isDown === true
    }
    const ok = data?.sources?.[def.key]?.ok
    const errMsg = data?.sources?.[def.key]?.error

    const toneCls = isUp == null
      ? 'text-muted-foreground border-border'
      : badUp
        ? 'border-rose-500/40 bg-rose-500/5 text-rose-600 dark:text-rose-300'
        : 'border-emerald-500/40 bg-emerald-500/5 text-emerald-600 dark:text-emerald-300'

    return (
      <div key={def.key} className={cn('rounded border bg-background/40 p-2.5', toneCls)}>
        <div className="flex items-center gap-1 font-mono text-[10px]">
          <span aria-hidden>{def.icon}</span>
          <span className="font-semibold uppercase tracking-wider">{def.title}</span>
        </div>
        <div className="mt-1 flex items-baseline gap-1.5">
          <span className="font-mono text-xl font-bold tabular-nums">
            {ok && num != null ? fmtValue(num, def.unit === '%' ? 3 : 2, def.unit) : '—'}
          </span>
          {typeof chg === 'number' ? (
            <span className={cn(
              'inline-flex items-center gap-0.5 font-mono text-[10px] tabular-nums',
              (badUp ? 'text-rose-500' : 'text-emerald-500'),
            )}>
              {isUp ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />}
              {def.changePath.includes('change_bp')
                ? `${chg >= 0 ? '+' : ''}${chg.toFixed(1)}bp`
                : `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`}
            </span>
          ) : null}
        </div>
        <div className="mt-1 font-mono text-[9px] text-muted-foreground/80 leading-tight">{def.hint}</div>
        {!ok && errMsg && (
          <div className="mt-1 font-mono text-[9px] text-rose-500/80" title={errMsg}>
            ⚠ feed 失败
          </div>
        )}
      </div>
    )
  }

  const ycs = data ? pick(data, ['sources', 'yield_curve_spread', 'data']) : null
  const ycsBp = (ycs && typeof ycs === 'object') ? (ycs as any).spread_bp : null

  const fundingSrc = data ? pick(data, ['sources', 'funding_rate', 'data']) : null
  const fundingOk = data?.sources?.funding_rate?.ok
  const fundingItems: Array<{ name: string; value: any }> = []
  if (fundingOk && Array.isArray(fundingSrc)) {
    for (const it of fundingSrc as any[]) {
      const k = String(it?.metric_key ?? '')
      if (k.endsWith('.funding')) {
        fundingItems.push({ name: k.split('.')[0], value: it?.value })
      }
    }
  }

  const dataFreshness = updatedAt
    ? (Date.now() / 1000 - updatedAt > 2 * 3600 ? '⏸ 离线/休市' : '🟢 实时')
    : '—'

  return (
    <div className="rounded border border-cyan-500/40 bg-cyan-500/5 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs">
        <div className="flex items-center gap-2">
          <Zap className="h-4 w-4 text-cyan-500" aria-hidden />
          <span className="font-bold uppercase tracking-widest text-cyan-700 dark:text-cyan-300">
            ⚡ 日内流动性情绪仪表盘
          </span>
          <span className="rounded border border-cyan-500/30 bg-cyan-500/10 px-1.5 py-0.5 text-[9px] font-bold text-cyan-600 dark:text-cyan-400">
            24h 实时跳动 · 美债 + DXY + VIX + ES/NQ
          </span>
        </div>
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <span className={dataFreshness.startsWith('🟢') ? 'text-emerald-500' : 'text-muted-foreground'}>{dataFreshness}</span>
          </span>
          {updatedAt && (
            <span>更新 {new Date(updatedAt * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</span>
          )}
          <button
            type="button"
            onClick={reload}
            disabled={loading}
            className="inline-flex items-center gap-0.5 rounded border border-border px-1.5 py-0.5 hover:bg-secondary/50 disabled:opacity-50"
          >
            <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
            <span>刷新</span>
          </button>
        </div>
      </div>

      {/* 顶部情绪横幅 */}
      <div className={cn(
        'mt-2 rounded border p-2 font-mono text-[11px]',
        regime.tone === 'bear'
          ? 'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300'
          : regime.tone === 'bull'
            ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
            : 'border-border bg-secondary/30 text-muted-foreground',
      )}>
        <div className="flex items-center gap-1.5">
          {regime.tone === 'bear' ? <AlertTriangle className="h-3.5 w-3.5" /> : <Activity className="h-3.5 w-3.5" />}
          <strong>今日情绪:</strong>
          <span>{regime.text}</span>
        </div>
      </div>

      {/* 5 大卡片栅格 */}
      <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
        {CARDS.map(renderCard)}
      </div>

      {/* 利差 + 资金费率 mini 条 */}
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
        <div className="flex items-center justify-between rounded border border-border bg-secondary/20 px-2 py-1 font-mono text-[10px]">
          <span className="text-muted-foreground">10Y-5Y 利差</span>
          <span className={cn(
            'tabular-nums font-semibold',
            (ycsBp ?? 0) < 0 ? 'text-rose-500' : 'text-emerald-500',
          )}>
            {ycsBp != null ? `${ycsBp >= 0 ? '+' : ''}${ycsBp.toFixed(1)} bp` : '—'}
          </span>
          <span className="text-muted-foreground/70">负值=倒挂(衰退信号)</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-border bg-secondary/20 px-2 py-1 font-mono text-[10px]">
          <span className="text-muted-foreground">永续资金费率</span>
          {fundingItems.length === 0 && <span className="text-muted-foreground/70">—</span>}
          {fundingItems.slice(0, 4).map((it) => (
            <span key={it.name} className={cn(
              'tabular-nums',
              (it.value ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500',
            )}>
              {it.name}: {fmtSigned(it.value, 4, '%')}
            </span>
          ))}
        </div>
      </div>

      <div className="mt-2 rounded border border-cyan-500/20 bg-cyan-500/5 p-1.5 text-[10px] text-muted-foreground">
        💡 解读:<strong className="text-foreground">10Y 美债 ↑ = 全球资产贴现率上行</strong>,
        DXY ↑ = 美元强 + 风险资产承压,VIX 急升 = 恐慌踩踏。
        今天这种大跌看这套仪表盘最直接;若日内信号与月/季度信号
        (CPI/PCE/非农/PMI/存储) 背离,以<strong className="text-foreground">月/季度为基准</strong>,
        日内只用来判读流动性与超短期节奏。
      </div>
    </div>
  )
}