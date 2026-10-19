'use client'

import { Calendar, Activity, BarChart3, Crosshair, AlertTriangle, Radio, RefreshCw, Mail } from 'lucide-react'
import { useFedWatch } from '@/lib/contexts/fed-watch-context'
import { cn } from '@/lib/utils'

/**
 * v276 · Fed 全景监控面板
 *
 * 数据源:/api/discovery/fed-watch
 *
 * 7 块合一:
 *   ① 当前路径(2Y-EFFR + 推导预期 + 异动状态)
 *   ② 下次 FOMC 会议倒计时
 *   ③ 未来 7d Fed 事件日历(FOMC/CPI/NFP/Powell讲话)
 *   ④ 自适应异动阈值(VIX 联动)
 *   ⑤ 收益率曲线(13W/5Y/10Y/30Y)
 *   ⑥ 24h Fed 路径时序(2Y/EFFR/spread)
 *   ⑦ 历史相关性回测(2Y yield 移动 vs BTC/SPX/DXY/GLD)
 */

type FedWatchResponse = {
  ok: boolean
  ts: number
  current: {
    ok: boolean
    data_source?: string
    current?: { dgs2_pct?: number; effr_pct?: number }
    expectation?: {
      spread_bp?: number
      expected_24m_change_bp?: number
      direction?: 'hawkish' | 'dovish' | 'neutral'
      narrative?: string
      first_principles_chain?: string[]
    }
    anomaly?: {
      is_anomaly: boolean
      severity?: 'low' | 'med' | 'high'
      message?: string
      yield_move_bp?: number
      expect_move_bp?: number
    }
  }
  next_fomc: {
    title: string
    event_time: number
    minutes_to: number
    impact: string
    star: number
    forecast?: string
    previous?: string
  } | null
  events_7d: Array<{
    title: string
    event_time: number
    minutes_to: number
    impact: string
    star: number
    forecast?: string
    previous?: string
    actual?: string
    source?: string
  }>
  adaptive_threshold: {
    regime: 'low_vol' | 'normal' | 'high_vol' | 'panic' | 'unknown'
    vix: number | null
    yield_move_bp: number
    expect_move_bp: number
    note: string
  }
  yield_curve: {
    ok: boolean
    yields: Array<{
      name: string
      yield_pct: number | null
      previous_close: number | null
      change_pct: number | null
    }>
  }
  history_24h: {
    ts: number[]
    dgs2_pct: number[]
    effr_pct: number[]
    spread_bp: number[]
    samples: number
  }
  correlation: {
    ok: boolean
    samples: number
    correlations: Record<string, { pearson: number | null; samples: number; note?: string }>
    note: string
  }
  last_push: { last_push_ts: number | null; severity?: string }
  explain: string
}

const DIRECTION_BADGE = {
  hawkish: 'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  dovish: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  neutral: 'border-slate-500/40 bg-slate-500/10 text-slate-700 dark:text-slate-300',
} as const

const REGIME_BADGE = {
  low_vol: 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  normal: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  high_vol: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  panic: 'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  unknown: 'border-slate-500/40 bg-slate-500/10 text-slate-700 dark:text-slate-300',
} as const

const REGIME_CN = {
  low_vol: '低波动',
  normal: '常态',
  high_vol: '高波动',
  panic: '恐慌',
  unknown: '未知',
} as const

// v277: 5★ 事件市场影响速查表(给 events_7d 行末 + 解读箱用)
const TITLE_TO_IMPACT_CN: Record<string, string> = {
  'FOMC Meeting Minutes': '鹰/鸽信号在纪要文本里 — 关注措辞。',
  'FOMC Press Conference': '鲍威尔讲话 — 极易引发 2Y yield 大幅移动。',
  'FOMC Statement': '利率决议声明 — 政策口径变化首发。',
  'FOMC Meeting': '利率决议会议 — 决定 Fed Funds 目标。',
  'FOMC Member': 'FOMC 委员讲话 — 释放政策风向(关注措辞)。',
  'CPI': '通胀数据 — 直接改写 2Y 利率预期。',
  'PCE': 'Fed 偏好的通胀指标 — 影响加息/降息预期。',
  'NFP': '非农就业 — 强劲 = 鹰,疲软 = 鸽。',
  'Powell': '鲍威尔讲话 — 市场最大单一波动源。',
}

function impactCnFor(title: string): string {
  for (const k of Object.keys(TITLE_TO_IMPACT_CN)) {
    if (title.includes(k)) return TITLE_TO_IMPACT_CN[k]
  }
  return 'Fed 相关事件 — 关注措辞 vs 市场预期。'
}

function formatMinutes(min: number): string {
  if (min < 0) return `${-min}m 前`
  if (min < 60) return `${min}m 后`
  if (min < 1440) return `${Math.floor(min / 60)}h${min % 60}m 后`
  return `${Math.floor(min / 1440)}d${Math.floor((min % 1440) / 60)}h 后`
}

function fmtTs(ts: number): string {
  const d = new Date(ts * 1000)
  const mo = String(d.getMonth() + 1).padStart(2, '0')
  const da = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${mo}-${da} ${hh}:${mm}`
}

function pearsonColor(p: number | null): string {
  if (p === null) return 'text-muted-foreground'
  if (p >= 0.5) return 'text-rose-600 dark:text-rose-400'
  if (p >= 0.2) return 'text-amber-600 dark:text-amber-400'
  if (p <= -0.5) return 'text-emerald-600 dark:text-emerald-400'
  if (p <= -0.2) return 'text-sky-600 dark:text-sky-400'
  return 'text-muted-foreground'
}

function MiniSparkline({ values, w = 80, h = 24 }: { values: number[]; w?: number; h?: number }) {
  if (!values || values.length < 2) {
    return <span className="text-[10px] text-muted-foreground/50">no data</span>
  }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const step = w / (values.length - 1)
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / range) * h).toFixed(1)}`)
    .join(' ')
  return (
    <svg width={w} height={h} className="inline-block">
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  )
}

export function FedWatchPanel() {
  // v283+ · 改用共享 FedWatchContext,与 fed-mailbox-panel 共用同一份 60s 轮询。
  // 解决 #2「双组件同接口重复请求」(从 2 req/60s 合并为 1 req/60s)。
  const fedWatch = useFedWatch<FedWatchResponse>()
  const data = fedWatch.data
  const loading = fedWatch.loading
  // Provider 内部 usePoll 默认 transform 已把 ok=false 视为「失败」(data=null + error 设),
  // 此处直接读 error 即可。
  const err = fedWatch.error

  if (!data) {
    return (
      <section
        id="disc-fedwatch"
        aria-label="Fed 全景监控"
        className="scroll-mt-3 surface-card border-amber-500/30 bg-amber-500/5 p-3"
      >
        <header className="flex items-center gap-2">
          <Radio className="h-4 w-4 text-amber-600" aria-hidden />
          <h3 className="font-mono text-sm font-bold text-foreground">🏛️ Fed 全景监控</h3>
        </header>
        <div className="mt-2 text-[11px] text-muted-foreground">
          {loading ? '加载中…' : err ? `⚠️ ${err}` : '等待数据…'}
        </div>
      </section>
    )
  }

  const exp = data.current.expectation || {}
  // v277: anomaly 给 is_anomaly/severity/message 默认值,避免旧代码读 undefined 触发 TS2339
  const anomaly = data.current.anomaly || { is_anomaly: false as const, severity: 'low' as const, message: '' }
  const direction = exp.direction || 'neutral'
  const dirCn = { hawkish: '偏加息', dovish: '偏降息', neutral: '中性' }[direction]
  const threshold = data.adaptive_threshold
  const corr = data.correlation.correlations || {}
  const cur = data.current.current || {}

  return (
    <section
      id="disc-fedwatch"
      aria-label="Fed 全景监控"
      className="scroll-mt-3 surface-card border-amber-500/30 bg-amber-500/5 p-3"
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Radio className="h-4 w-4 text-amber-600" aria-hidden />
          <h3 className="font-mono text-sm font-bold text-foreground">🏛️ Fed 全景监控</h3>
          {anomaly.is_anomaly && (
            <span className="rounded border border-rose-500/40 bg-rose-500/15 px-1.5 py-0.5 text-[10px] font-bold text-rose-700 dark:text-rose-300">
              ⚠️ 异动 {anomaly.severity}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={() => void fedWatch.refresh()}
          disabled={loading}
          className="rounded border border-border bg-secondary/30 px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground"
        >
          <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} /> 刷新
        </button>
      </header>

      <p className="mt-1 text-[11px] leading-5 text-muted-foreground">{data.explain}</p>

      {/* 块 1: 当前路径 + 异动状态 + 自适应阈值 */}
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
        <div className="rounded border border-border bg-background/50 p-2">
          <div className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground">当前路径</div>
          <div className="mt-1 flex items-center gap-1.5">
            <span className={cn('rounded border px-1.5 py-0.5 text-[10px] font-bold', DIRECTION_BADGE[direction])}>
              {dirCn}
            </span>
            {exp.spread_bp !== undefined && (
              <span className="font-mono text-[11px]">
                spread <span className="font-bold">{exp.spread_bp >= 0 ? '+' : ''}{exp.spread_bp}bp</span>
              </span>
            )}
          </div>
          {exp.expected_24m_change_bp !== undefined && (
            <div className="mt-1 font-mono text-[10px] text-muted-foreground">
              24m 累计预期:{' '}
              <span className="text-foreground">
                {exp.expected_24m_change_bp >= 0 ? '+' : ''}{exp.expected_24m_change_bp}bp
              </span>
            </div>
          )}
          {exp.narrative && (
            <div className="mt-1 text-[10px] leading-4 text-muted-foreground/90">{exp.narrative}</div>
          )}
        </div>

        <div className="rounded border border-border bg-background/50 p-2">
          <div className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground">2Y / EFFR / 价差</div>
          <div className="mt-1 font-mono text-[11px]">
            <div>2Y UST = <span className="font-bold">{(cur.dgs2_pct ?? 0).toFixed(3)}%</span></div>
            <div>EFFR = <span className="font-bold">{(cur.effr_pct ?? 0).toFixed(3)}%</span></div>
            <div className="mt-1 text-[10px] text-muted-foreground">数据源:{data.current.data_source || 'n/a'}</div>
          </div>
        </div>

        <div className="rounded border border-border bg-background/50 p-2">
          <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
            <Crosshair className="h-2.5 w-2.5" /> 自适应阈值(VIX)
          </div>
          <div className="mt-1 flex items-center gap-1.5">
            <span className={cn('rounded border px-1.5 py-0.5 text-[10px] font-bold', REGIME_BADGE[threshold.regime])}>
              {REGIME_CN[threshold.regime]}
            </span>
            <span className="font-mono text-[11px]">
              VIX = <span className="font-bold">{threshold.vix !== null ? threshold.vix.toFixed(1) : '—'}</span>
            </span>
          </div>
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">
            2Y: <span className="text-foreground">{threshold.yield_move_bp}bp</span> · 预期:{' '}
            <span className="text-foreground">{threshold.expect_move_bp}bp</span>
          </div>
        </div>
      </div>

      {/* 块 2: 下次 FOMC + 7d 事件 */}
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
        <div className="rounded border border-border bg-background/50 p-2">
          <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
            <Calendar className="h-2.5 w-2.5" /> 下次 FOMC
          </div>
          {data.next_fomc ? (
            <div className="mt-1">
              <div className="text-[11px] font-bold text-foreground">{data.next_fomc.title}</div>
              <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                {fmtTs(data.next_fomc.event_time)} · {formatMinutes(data.next_fomc.minutes_to)}
              </div>
              {data.next_fomc.forecast && (
                <div className="mt-1 text-[10px] leading-4 text-muted-foreground/90">
                  预测:{data.next_fomc.forecast} · 前值:{data.next_fomc.previous || '—'}
                </div>
              )}
            </div>
          ) : (
            <div className="mt-1 text-[10px] text-muted-foreground/70">macro_calendar 暂无 FOMC 记录</div>
          )}
        </div>

        <div className="rounded border border-border bg-background/50 p-2">
          <div className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
            未来 7d Fed 事件({data.events_7d.length})
          </div>
          <div className="mt-1 max-h-40 space-y-1 overflow-y-auto text-[10px] thin-scroll">
            {data.events_7d.length === 0 ? (
              <div className="text-muted-foreground/70">无</div>
            ) : (
              data.events_7d.slice(0, 6).map((e, i) => {
                // v277: 5★ → 红边 + "🌟 极高" 徽章 + 影响速查
                const isFive = e.star >= 5
                const starCls = isFive
                  ? 'border-rose-500 bg-rose-500/25 text-rose-700 dark:text-rose-200'
                  : e.impact === 'high'
                    ? 'border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300'
                    : 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300'
                return (
                  <div
                    key={i}
                    className={cn(
                      'rounded border p-1 leading-4',
                      isFive
                        ? 'border-rose-500/70 bg-rose-500/[0.07] ring-1 ring-rose-500/30'
                        : 'border-border bg-background/30'
                    )}
                  >
                    <div className="flex items-center gap-1.5">
                      <span className={cn('shrink-0 rounded border px-1 font-mono text-[9px]', starCls)}>
                        {'★'.repeat(e.star)}
                      </span>
                      <span className="font-mono text-muted-foreground">{fmtTs(e.event_time)}</span>
                      <span className={cn('flex-1 truncate', isFive && 'font-bold text-foreground')}>
                        {e.title}
                      </span>
                      {isFive && (
                        <span className="shrink-0 rounded border border-rose-500/60 bg-rose-500/20 px-1 py-px text-[9px] font-bold text-rose-700 dark:text-rose-200">
                          🌟 极高
                        </span>
                      )}
                      <span className="shrink-0 font-mono text-[9px] text-muted-foreground/70">
                        {formatMinutes(e.minutes_to)}
                      </span>
                    </div>
                    <div className="mt-0.5 pl-1 text-[9px] leading-3.5 text-muted-foreground/90">
                      📌 {impactCnFor(e.title)}
                    </div>
                  </div>
                )
              })
            )}
            {data.events_7d.length > 6 && (
              <div className="text-[9px] text-muted-foreground/70">+{data.events_7d.length - 6} 个未显示</div>
            )}
          </div>
        </div>
      </div>

      {/* v277 块 2.5: 📧 Fed 解读箱 — 把"路径异动 + 临近事件 + 数据源"包成 3 条人话邮件 */}
      <FedMailbox
        direction={direction}
        exp={exp}
        anomaly={anomaly}
        events={data.events_7d.slice(0, 3)}
      />

      {/* 块 3: 收益率曲线 + 24h 历史 */}
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
        <div className="rounded border border-border bg-background/50 p-2">
          <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
            <BarChart3 className="h-2.5 w-2.5" /> 美债收益率曲线
          </div>
          <div className="mt-1 space-y-0.5 font-mono text-[10px]">
            {(data.yield_curve.yields || []).map((y) => (
              <div key={y.name} className="flex items-center gap-2">
                <span className="w-16 text-muted-foreground">{y.name}</span>
                <span className="w-16 font-bold">
                  {y.yield_pct !== null ? `${y.yield_pct.toFixed(3)}%` : '—'}
                </span>
                <span
                  className={cn(
                    'w-16',
                    y.change_pct !== null && y.change_pct > 0
                      ? 'text-rose-600 dark:text-rose-400'
                      : y.change_pct !== null && y.change_pct < 0
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : 'text-muted-foreground',
                  )}
                >
                  {y.change_pct !== null
                    ? `${y.change_pct >= 0 ? '+' : ''}${y.change_pct.toFixed(3)}`
                    : '—'}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded border border-border bg-background/50 p-2">
          <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
            <Activity className="h-2.5 w-2.5" /> 24h Fed 路径时序
            <span className="ml-1 text-[9px] text-muted-foreground/60">n={data.history_24h.samples}</span>
          </div>
          {data.history_24h.samples > 1 ? (
            <div className="mt-1 space-y-1 font-mono text-[10px]">
              <div className="flex items-center gap-2">
                <span className="w-16 text-muted-foreground">2Y UST</span>
                <span className="text-amber-600 dark:text-amber-400">
                  <MiniSparkline values={data.history_24h.dgs2_pct} w={120} h={20} />
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span className="w-16 text-muted-foreground">EFFR</span>
                <span className="text-sky-600 dark:text-sky-400">
                  <MiniSparkline values={data.history_24h.effr_pct} w={120} h={20} />
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span className="w-16 text-muted-foreground">价差(bp)</span>
                <span className="text-rose-600 dark:text-rose-400">
                  <MiniSparkline values={data.history_24h.spread_bp} w={120} h={20} />
                </span>
              </div>
            </div>
          ) : (
            <div className="mt-1 text-[10px] text-muted-foreground/70">fed_rate_history 样本不足</div>
          )}
        </div>
      </div>

      {/* 块 4: 历史相关性回测 */}
      <div className="mt-2 rounded border border-border bg-background/50 p-2">
        <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
          <AlertTriangle className="h-2.5 w-2.5" /> 2Y yield 移动 vs 资产 24h 反应(Pearson,n={data.correlation.samples}天)
        </div>
        {!data.correlation.ok ? (
          <div className="mt-1 text-[10px] text-muted-foreground/70">
            {data.correlation.note || '相关性回测暂不可用'}
          </div>
        ) : (
          <div className="mt-1 grid grid-cols-2 gap-1.5 font-mono text-[10px] md:grid-cols-4">
            {Object.entries(corr).map(([asset, c]) => (
              <div key={asset} className="rounded border border-border bg-background/30 px-2 py-1">
                <div className="text-muted-foreground">{asset}</div>
                <div className="flex items-center gap-1">
                  <span className={cn('text-base font-bold', pearsonColor(c.pearson))}>
                    {c.pearson !== null ? (c.pearson >= 0 ? '+' : '') + c.pearson.toFixed(2) : '—'}
                  </span>
                  <span className="text-[9px] text-muted-foreground/70">n={c.samples}</span>
                </div>
              </div>
            ))}
          </div>
        )}
        <p className="mt-1 text-[9px] leading-4 text-muted-foreground/70">
          💡 解读:正相关 = 加息预期利空该资产;负相关 = 加息预期利好(如美债/黄金)。样本数 ≥ 30 天才有意义。
        </p>
      </div>

      <footer className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[9px] text-muted-foreground/70">
        <span>数据源: FRED + macro_calendar + treasury_yield_feed + kline_bars</span>
        {data.last_push.last_push_ts && (
          <span>
            上次推送:{fmtTs(data.last_push.last_push_ts)}
            {data.last_push.severity && (
              <span className="ml-1 text-rose-500">({data.last_push.severity})</span>
            )}
          </span>
        )}
        <span>缓存 60s · 自动刷新</span>
      </footer>
    </section>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// v277: 📧 Fed 解读箱 — 把"路径异动 / 临近事件 / 数据源"包成 3 条人话邮件
// ──────────────────────────────────────────────────────────────────────────────

type FedMailboxEvent = {
  title: string
  event_time: number
  minutes_to: number
  star: number
}

type FedMailboxProps = {
  direction: 'hawkish' | 'dovish' | 'neutral'
  exp: { spread_bp?: number; expected_24m_change_bp?: number; narrative?: string }
  // v277: anomaly 设为全 optional,允许传空对象(原始类型用 || {} 兜底)
  anomaly: { is_anomaly?: boolean; severity?: 'low' | 'med' | 'high'; message?: string }
  events: FedMailboxEvent[]
}

const DIR_MAIL = {
  hawkish: {
    tone: 'border-rose-500/40 bg-rose-500/5',
    from: '美联储 <no-reply@fed.gov>',
    directionWord: '鹰派(加息预期)',
    icon: '🔴',
  },
  dovish: {
    tone: 'border-emerald-500/40 bg-emerald-500/5',
    from: '美联储 <no-reply@fed.gov>',
    directionWord: '鸽派(降息预期)',
    icon: '🟢',
  },
  neutral: {
    tone: 'border-slate-500/40 bg-slate-500/5',
    from: '美联储 <no-reply@fed.gov>',
    directionWord: '中性(按兵不动)',
    icon: '⚖️',
  },
} as const

function FedMailbox({ direction, exp, anomaly, events }: FedMailboxProps) {
  const meta = DIR_MAIL[direction]
  const spreadBp = exp.spread_bp ?? 0
  const change24m = exp.expected_24m_change_bp ?? 0
  const steps = Math.abs(change24m / 25)
  const isAnomaly = anomaly?.is_anomaly ?? false

  // 邮件 1: 当前路径状态
  const mail1Subject =
    direction === 'hawkish'
      ? `市场认为 Fed 还要加息 ${steps.toFixed(0)} 次,共 +${change24m.toFixed(0)}bp`
      : direction === 'dovish'
        ? `市场认为 Fed 会降息 ${steps.toFixed(0)} 次,共 ${change24m.toFixed(0)}bp`
        : `市场认为 Fed 未来 24 月按兵不动`

  const mail1Body =
    direction === 'hawkish'
      ? '加息预期 → 一般会推高美元、美债收益率(债价跌),压低股票、黄金、加密币。注意风险资产回调。'
      : direction === 'dovish'
        ? '降息预期 → 一般会推低美元/美债收益率(债价涨),利好股票(尤其科技)、黄金、加密币。新兴市场也受益。'
        : '按兵不动 → 关注个股/行业消息,宏观层面波动小,看财报、地缘、油价等微观事件。'

  // 邮件 2: 异动预警
  const mail2Subject = isAnomaly
    ? `⚠️ 24h 2Y 国债异常变动 ${anomaly.message ? '·' + anomaly.severity + '级' : ''}`
    : '✅ 24h 内 2Y 国债价格平稳,Fed 路径无显著偏移'

  const mail2Body = isAnomaly
    ? `当前 2Y 国债收益率 24h 变动幅度触发预警阈值 — Fed 路径定价正在重置,密切留意下一根 K 线。${
        anomaly.message ? ' 原因:' + anomaly.message : ''
      }`
    : '无显著波动,适合静观其变。可关注下方邮件(临近事件)寻找下一波预期重定价机会。'

  // 邮件 3: 临近事件
  const nearestEvent = events[0]
  const mail3Subject = nearestEvent
    ? `🗓 临近: ${nearestEvent.title} · ${nearestEvent.star >= 5 ? '🌟 5★ 极高' : nearestEvent.star + '★'}(约 ${Math.max(0, Math.round(nearestEvent.minutes_to / 60))}h 后)`
    : '🗓 未来 7d 无重大 Fed 事件,可继续持仓'

  const mail3Body = nearestEvent
    ? `影响速查:${impactCnFor(nearestEvent.title)} 建议在事件前 30min 关闭短线仓位或收紧止损。`
    : '本周 Fed 真空期,可重点关注个股/行业逻辑或外部宏观(欧央行/油价等)。'

  const mails = [
    { tag: '邮件 1', subject: mail1Subject, body: mail1Body, urgent: direction === 'hawkish' || direction === 'dovish' },
    { tag: '邮件 2', subject: mail2Subject, body: mail2Body, urgent: isAnomaly },
    { tag: '邮件 3', subject: mail3Subject, body: mail3Body, urgent: nearestEvent?.star >= 5 },
  ]

  return (
    <div className={`mt-2 rounded-lg border-2 ${meta.tone} p-2`}>
      <div className="flex items-center gap-1.5">
        <Mail className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
        <span className="text-xs font-bold">📧 Fed 解读箱</span>
        <span className="rounded border border-current/30 px-1.5 py-0.5 font-mono text-[9px]">
          {meta.icon} {meta.directionWord}
        </span>
        <span className="ml-auto font-mono text-[9px] text-muted-foreground/70">
          共 3 封 · 60s 缓存
        </span>
      </div>
      <p className="mt-1 text-[10px] leading-4 text-muted-foreground">
        📌 这是把后端 <span className="font-mono">spread / 异动 / 临近事件</span> 自动翻译成的"人话邮件"。
      </p>

      <ul className="mt-1.5 space-y-1.5">
        {mails.map((m, i) => (
          <li
            key={i}
            className={`rounded border bg-background/60 p-1.5 ${
              m.urgent ? 'border-rose-500/50' : 'border-border'
            }`}
          >
            <div className="flex items-start gap-1.5">
              <span
                className={`mt-0.5 shrink-0 rounded border px-1 font-mono text-[9px] ${
                  m.urgent
                    ? 'border-rose-500/50 bg-rose-500/15 text-rose-700 dark:text-rose-300'
                    : 'border-border bg-secondary/40 text-muted-foreground'
                }`}
              >
                {m.tag}
              </span>
              <div className="min-w-0 flex-1">
                <div className="font-mono text-[9px] text-muted-foreground/80">
                  发件人:{meta.from}
                </div>
                <div className={`mt-0.5 text-[11px] font-bold ${m.urgent ? 'text-rose-700 dark:text-rose-300' : 'text-foreground'}`}>
                  {m.subject}
                </div>
                <div className="mt-0.5 text-[10px] leading-4 text-foreground/85">
                  {m.body}
                </div>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}




