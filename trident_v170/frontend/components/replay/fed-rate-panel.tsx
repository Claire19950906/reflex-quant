'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/utils'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'

// v226+v275+v252.1: Fed 利率预期面板 — 以「市场对未来 24 月 Fed 路径定价」为 hero
//
// "美联储涨跌值" = (2Y UST yield - EFFR) * 100  bp
//   · DGS2: FRED 2 年期美国国债收益率(美国国债市场每日定价)
//   · EFFR: FRED 有效联邦基金利率(美联储设定)
//   · 一阶原理: 无套利 -> 2Y yield 约等接下来 24 月短期利率的几何平均预期
//   · 因此 spread_bp = 市场对 Fed 累计加减息的定价(单位 bp)
//   · 标准 Fed 操作步长 25bp -> n_steps = spread_bp / 25
//
// 权威性 3 级(后端回落顺序, v252.1 起禁用硬编码兜底):
//   🟢  FRED DGS2 + DFF          美联储官方(需 FRED_API_KEY)
//   🟡 yfinance ^FVX (5Y) + ^IRX (13W)  非官方代理,精度 ±30bp
//   ⚫ unavailable                所有源失败,前端显示"接口不可用"

type FedExpectationResponse = {
  ok: boolean
  ts: number
  error?: string                       // v252.1: 数据源全部失败时
  data_source: 'fred' | 'fred_dgs2_yfinance_irx_proxy' | 'fred_dff_yfinance_5y_proxy' | 'yfinance_5y_proxy' | 'yfinance_5y_irx_proxy' | 'yfinance_5y_proxy_fastinfo' | 'unavailable'
  current: { dgs2_pct: number | null; effr_pct: number | null }
  expectation: {
    current_effr_pct: number
    dgs2_pct: number
    spread_pct: number
    spread_bp: number
    expected_24m_change_bp: number
    expected_step_count: number
    direction: 'hawkish' | 'dovish' | 'neutral'
    narrative: string
    email_subject?: string
    market_impact_simple?: string
    first_principles_chain: string[]
    data_quality_notes?: string[]       // v252.1: 代理数据源质量警告
  } | null
  history_24h: { dgs2_change_bp: number; samples: number }
  anomaly: {
    is_anomaly: boolean
    type: string
    severity: 'high' | 'med' | 'low'
    message: string
  }
}

function sourceMeta(src: FedExpectationResponse['data_source']) {
  if (src === 'fred')
    return {
      badge: '🟢',
      label: 'FRED 官方 · DGS2 + DFF',
      tone: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
      explain: '美联储圣路易斯分行官方日度序列 — 最权威',
    }
  if (src === 'fred_dgs2_yfinance_irx_proxy')
    return {
      badge: '🟡',
      label: 'FRED DGS2 + yfinance ^IRX',
      tone: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
      explain: '2Y 用 FRED 官方,EFFR 用 13W 国债代理(高 5-25bp)',
    }
  if (src === 'fred_dff_yfinance_5y_proxy')
    return {
      badge: '🟡',
      label: 'FRED DFF + yfinance ^FVX',
      tone: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
      explain: 'EFFR 用 FRED 官方,2Y 用 5Y 代理(高 30-50bp)',
    }
  if (src === 'yfinance_5y_proxy' || src === 'yfinance_5y_proxy_fastinfo')
    return {
      badge: '🟡',
      label: 'yfinance ^FVX · 5Y 代理 2Y',
      tone: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
      explain: '5Y 国债做 2Y 替身,实时但非官方 — 精度 ±30-50bp',
    }
  if (src === 'yfinance_5y_irx_proxy')
    return {
      badge: '🟠',
      label: 'yfinance 全代理 · ^FVX + ^IRX',
      tone: 'border-orange-500/40 bg-orange-500/10 text-orange-700 dark:text-orange-300',
      explain: '2Y 用 5Y 代理,EFFR 用 13W 代理 — 误差 ±30bp,仅作参考',
    }
  // unavailable / fallback_hardcode (旧版,兼容)
  return {
    badge: '⚫',
    label: '数据源不可用',
    tone: 'border-slate-500/40 bg-slate-500/10 text-slate-700 dark:text-slate-300',
    explain: '外部数据源全部失败 — 不显示任何数据,不假装',
  }
}

function directionMeta(d: 'hawkish' | 'dovish' | 'neutral') {
  if (d === 'hawkish')
    return {
      emoji: '↑', label: '鹰派', sub: '加息预期',
      bar: 'bg-rose-500',
      ink: 'text-rose-600 dark:text-rose-400',
      ring: 'ring-rose-500/40',
      soft: 'bg-rose-500/10',
    }
  if (d === 'dovish')
    return {
      emoji: '↓', label: '鸽派', sub: '降息预期',
      bar: 'bg-emerald-500',
      ink: 'text-emerald-600 dark:text-emerald-400',
      ring: 'ring-emerald-500/40',
      soft: 'bg-emerald-500/10',
    }
  return {
    emoji: '⚖️', label: '中性', sub: '持稳预期',
    bar: 'bg-slate-500',
    ink: 'text-slate-600 dark:text-slate-300',
    ring: 'ring-slate-500/40',
    soft: 'bg-slate-500/10',
  }
}

function severityBadge(s: string) {
  if (s === 'high') return { emoji: '🆘', label: '强异动', tone: 'text-rose-700 dark:text-rose-300' }
  if (s === 'med')  return { emoji: '⚠️',  label: '中等异动', tone: 'text-amber-700 dark:text-amber-300' }
  return { emoji: 'ℹ️', label: '轻微异动', tone: 'text-slate-600 dark:text-slate-300' }
}

export function FedRatePanel() {
  const [data, setData] = useState<FedExpectationResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showChain, setShowChain] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const d = await apiGet<FedExpectationResponse>('/fed/expectation')
      if (!d.ok) throw new Error('fed/expectation returned ok=false')
      setData(d)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Fed 预期接口不可用')
    } finally { setLoading(false) }
  }, [])

  // v302 P0+：手写 setInterval → usePollRefresh（统一 alive flag + cleanup）
  usePollRefresh(reload, 300_000)

  const dir = useMemo(() => (data && data.expectation) ? directionMeta(data.expectation.direction) : null, [data])
  const src = useMemo(() => data ? sourceMeta(data.data_source) : null, [data])
  const ladder = useMemo(() => {
    if (!data || !data.expectation) return null
    const effr = data.expectation.current_effr_pct
    const expected = effr + data.expectation.expected_24m_change_bp / 100
    const span = 1.5
    const expectedPct = 50 + ((expected - effr) / span) * 50
    return { effr, expected, expectedPct, span }
  }, [data])

  if (error) {
    return (
      <div className="rounded border border-rose-300 bg-rose-50 p-3 text-sm dark:border-rose-700 dark:bg-rose-950">
        <div className="font-semibold text-rose-700 dark:text-rose-300">Fed 利率预期 · 接口异常</div>
        <div className="mt-1 text-rose-600 dark:text-rose-400">{error}</div>
      </div>
    )
  }

  // v252.1: 后端 ok=False 时(数据源全部失败),显示明确错误,不显示假数据
  if (data && data.ok === false) {
    return (
      <div className="rounded border border-slate-400 bg-slate-50 p-3 text-sm dark:border-slate-600 dark:bg-slate-900">
        <div className="flex items-center justify-between gap-2">
          <div className="font-semibold flex items-center gap-1.5">
            <span aria-hidden>🇫🇷</span> Fed 利率预期 · 数据源不可用
          </div>
          <button
            onClick={reload} disabled={loading}
            className="rounded bg-slate-100 px-2 py-0.5 text-xs hover:bg-slate-200 disabled:opacity-50 dark:bg-slate-800 dark:hover:bg-slate-700"
          >
            {loading ? '…' : '🔄 重试'}
          </button>
        </div>
        <div className="mt-2 text-slate-700 dark:text-slate-300">
          {data.error || '所有外部数据源失败(FRED / yfinance 均无数据)。'}
        </div>
        <div className="mt-2 text-[11px] text-slate-500 dark:text-slate-400 leading-relaxed">
          💡 不会显示任何伪造的 5.33% 等数字。请稍后重试,或在 backend/config.py 配置 FRED_API_KEY 启用官方数据源。
        </div>
      </div>
    )
  }

  if (!data || !dir || !src || !ladder || !data.expectation || !data.current) {
    return (
      <div className="rounded border border-slate-200 bg-white p-3 text-sm dark:border-slate-700 dark:bg-slate-900">
        <div className="font-semibold">Fed 利率预期</div>
        <div className="mt-1 text-slate-500">{loading ? '加载中…' : '等待数据'}</div>
      </div>
    )
  }

  // v252.1: guard 已确保 data.expectation / data.current 非 null;
  // 这里 dgs2/effr 仍是 nullable 类型(后端 ok=false 时为 null),
  // 用 ?? 0 兜底显示 — 实际上执行到此处时不会为 null。
  const dgs2 = data.current.dgs2_pct ?? 0
  const effr = data.current.effr_pct ?? 0
  const spreadBp = data.expectation.spread_bp
  const expectedBp = data.expectation.expected_24m_change_bp
  const steps = data.expectation.expected_step_count
  const d24h = data.history_24h.dgs2_change_bp
  const expectedPctClamped = Math.max(0, Math.min(100, ladder.expectedPct))

  return (
    <div className="rounded border border-slate-200 bg-white p-3 text-sm dark:border-slate-700 dark:bg-slate-900">

      {/* 顶栏: 标题 + 刷新 + 权威度徽章 */}
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="font-semibold flex items-center gap-1.5">
          <span aria-hidden>{"🇫🇷"}</span> Fed 利率预期 · 市场对 24 月路径定价
        </div>
        <div className="flex items-center gap-1.5">
          <span
            className={cn('rounded border px-1.5 py-0.5 text-[10px] font-mono', src.tone)}
            title={src.explain}
          >
            {src.badge} {src.label}
          </span>
          <button
            onClick={reload} disabled={loading}
            className="rounded bg-slate-100 px-2 py-0.5 text-xs hover:bg-slate-200 disabled:opacity-50 dark:bg-slate-800 dark:hover:bg-slate-700"
          >
            {loading ? '…' : '🔄'}
          </button>
        </div>
      </div>

      {/* HERO: 方向 + 累计 bp */}
      <div className={cn('rounded-lg border-2 p-3 mb-3', dir.ring, dir.soft, 'border-current/30')}>
        <div className="flex items-center gap-3">
          <div className="text-5xl leading-none" aria-hidden>{dir.emoji}</div>
          <div className="flex-1 min-w-0">
            <div className={cn('text-xs uppercase tracking-widest font-mono', dir.ink)}>
              {dir.label} · {dir.sub}
            </div>
            <div className="mt-0.5 flex items-baseline gap-2">
              <span className={cn('font-mono font-bold text-3xl leading-none tabular-nums', dir.ink)}>
                {expectedBp >= 0 ? '+' : ''}{expectedBp.toFixed(0)}
              </span>
              <span className={cn('text-base font-semibold', dir.ink)}>bp</span>
              <span className="text-[11px] text-muted-foreground ml-1">/ 24 月累计</span>
            </div>
            <div className="mt-0.5 text-[11px] text-muted-foreground">
              ≈ <span className="font-mono font-semibold text-foreground">{Math.abs(steps).toFixed(1)}</span> 次
              {' '}{expectedBp >= 0 ? '加息' : '降息'}25bp
              {' · '}<span className="font-mono">{ladder.expected.toFixed(2)}%</span> 预期 24 月后利率
            </div>
          </div>
        </div>
        {data.expectation.narrative && (
          <div className="mt-2 border-t border-current/10 pt-2">
            {/* v277: 📧 通知箱 — 把后端学术版 narrative 翻译成人话版
                (email_subject + market_impact_simple)。fallback 旧 narrative。 */}
            <div className="text-[10px] font-mono uppercase tracking-widest text-muted-foreground">
              📧 Fed 发件箱 · {data.expectation.email_subject ? '白话解读' : '原始 narrative'}
            </div>
            {data.expectation.email_subject && (
              <div className={cn('mt-1 text-xs font-bold', dir.ink)}>
                {data.expectation.email_subject}
              </div>
            )}
            {data.expectation.market_impact_simple && (
              <div className="mt-1 text-[11px] leading-5 text-foreground/90">
                <strong className="text-foreground">📌 对市场影响:</strong>{' '}
                {data.expectation.market_impact_simple}
              </div>
            )}
            {/* 折叠在底部的"原始学术版"narrative,给极客用户/audit 用 */}
            <details className="mt-1.5">
              <summary className="cursor-pointer text-[10px] text-muted-foreground hover:text-foreground">
                ▶ 原始 narrative(技术版)
              </summary>
              <div className="mt-1 text-[10px] leading-4 text-muted-foreground/80">
                {data.expectation.narrative}
              </div>
            </details>
          </div>
        )}
      </div>

      {/* 阶梯可视化: 当前利率 -> 24m 预期利率 */}
      <div className="rounded border border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-800/40 p-2 mb-3">
        <div className="flex items-center justify-between text-[10px] font-mono text-muted-foreground mb-1">
          <span>← 当前 {effr.toFixed(2)}%</span>
          <span>±{ladder.span.toFixed(1)}% 范围</span>
          <span>24m 预期 →</span>
        </div>
        <div className="relative h-6 rounded bg-slate-200/60 dark:bg-slate-700/60 overflow-hidden">
          <div className="absolute top-0 bottom-0 left-1/2 w-px bg-foreground/40" aria-hidden />
          <div
            className={cn('absolute top-1 bottom-1 rounded', dir.bar)}
            style={{
              left: expectedBp >= 0 ? '50%' : `${expectedPctClamped}%`,
              width: `${Math.abs(expectedPctClamped - 50)}%`,
            }}
            aria-hidden
          />
          <div className="absolute top-0 bottom-0 left-1/2 -translate-x-1/2 flex items-center justify-center">
            <div className="h-3 w-3 rounded-full bg-foreground border-2 border-background" />
          </div>
          {expectedPctClamped !== 50 && (
            <div
              className="absolute top-0 bottom-0 -translate-x-1/2 flex items-center justify-center"
              style={{ left: `${expectedPctClamped}%` }}
            >
              <div className={cn('h-3 w-3 rounded-full border-2 border-background', dir.bar)} />
            </div>
          )}
        </div>
        <div className="flex items-center justify-between text-[9px] font-mono text-muted-foreground mt-1">
          <span>{(effr - ladder.span/2).toFixed(2)}%</span>
          <span className="font-semibold text-foreground">当前 {effr.toFixed(2)}%</span>
          <span>{(effr + ladder.span/2).toFixed(2)}%</span>
        </div>
      </div>

      {/* 24h 异动 */}
      {data.anomaly.is_anomaly ? (
        <div className={cn('rounded border p-2 mb-3',
          data.anomaly.severity === 'high'
            ? 'border-rose-400 bg-rose-50 dark:border-rose-700 dark:bg-rose-950'
            : 'border-amber-400 bg-amber-50 dark:border-amber-700 dark:bg-amber-950'
        )}>
          <div className="flex items-center justify-between">
            <span className={cn('text-xs font-bold', severityBadge(data.anomaly.severity).tone)}>
              {severityBadge(data.anomaly.severity).emoji} {severityBadge(data.anomaly.severity).label}
              · 24h 2Y 变动
            </span>
            <span className={cn('font-mono font-bold text-lg tabular-nums',
              d24h > 0 ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400'
            )}>
              {d24h >= 0 ? '+' : ''}{d24h.toFixed(1)} bp
            </span>
          </div>
          <div className="mt-1 text-[11px] text-foreground/70">{data.anomaly.message}</div>
        </div>
      ) : (
        <div className="rounded border border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-800/40 p-2 mb-3 flex items-center justify-between">
          <span className="text-[11px] text-muted-foreground">24h 2Y 变动</span>
          <span className={cn('font-mono font-semibold tabular-nums',
            d24h > 0 ? 'text-rose-600' : d24h < 0 ? 'text-emerald-600' : 'text-muted-foreground'
          )}>
            {d24h >= 0 ? '+' : ''}{d24h.toFixed(1)} bp · {data.history_24h.samples} 快照
          </span>
        </div>
      )}

      {/* 辅助数据: DGS2 / EFFR / Spread */}
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div className="rounded bg-slate-50 p-2 dark:bg-slate-800">
          <div className="text-slate-500 text-[10px]">2Y UST (DGS2)</div>
          <div className="font-mono font-semibold tabular-nums">{dgs2.toFixed(3)}%</div>
        </div>
        <div className="rounded bg-slate-50 p-2 dark:bg-slate-800">
          <div className="text-slate-500 text-[10px]">EFFR (DFF)</div>
          <div className="font-mono font-semibold tabular-nums">{effr.toFixed(3)}%</div>
        </div>
        <div className="rounded bg-slate-50 p-2 dark:bg-slate-800">
          <div className="text-slate-500 text-[10px]">Spread = 2Y - EFFR</div>
          <div className={cn('font-mono font-semibold tabular-nums',
            spreadBp > 0 ? 'text-rose-600' : spreadBp < 0 ? 'text-emerald-600' : ''
          )}>
            {spreadBp >= 0 ? '+' : ''}{spreadBp.toFixed(1)} bp
          </div>
        </div>
      </div>

      {/* 推导链(可折叠) */}
      <div className="mt-3">
        <button
          onClick={() => setShowChain(v => !v)}
          className="text-xs text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
        >
          {showChain ? '▼' : '▶'} 推导链(无套利 → EFFR → 折现回 2Y)
        </button>
        {showChain && (
          <ol className="mt-1 space-y-0.5 text-xs text-slate-600 dark:text-slate-300">
            {data.expectation.first_principles_chain.map((line, idx) => (
              <li key={idx}>{line}</li>
            ))}
          </ol>
        )}
      </div>

      <div className="mt-2 text-[10px] text-slate-400 dark:text-slate-500">
        观测 {new Date(data.ts * 1000).toLocaleString('zh-CN')}
        {' · '}数据源说明:{src.explain}
      </div>
    </div>
  )
}
