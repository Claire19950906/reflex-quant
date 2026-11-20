'use client'

import { useEffect, useState } from 'react'

import { fetchWithFallback } from '@/lib/api'
import { useElapsed } from '@/lib/hooks/use-elapsed'

// 三类错误仪表盘（v344）
//
// 用户口径（2026-11-14）：
//   1. 买卖信号判断失误 —— 对市场还不够了解，心理预期 / 形态预期没达标
//   2. 信息分析方向错误 —— 逻辑框架本身错了（事件 → 方向的推理链不成立）
//   3. 技术错误 —— 数据缺失、信号慢，工程问题（用户自己处理）
//
// 设计原则：每一类都要给出**小类计数**，这样能一眼锁定"是哪一种错"，而不是只看一个总数。

type Sub = { label: string; n: number }
type Sample = Record<string, any>
type ErrorClass = {
  key: 'signal' | 'direction' | 'technical' | string
  label: string
  total: number
  bad: number
  rate: number | null
  subs: Sub[]
  samples: Sample[]
}
type TaxonomyResp = {
  ok: boolean
  window_hours: number
  classes: ErrorClass[]
}

const CLASS_STYLE: Record<string, { accent: string; icon: string; hint: string }> = {
  signal: {
    accent: 'border-l-amber-500',
    icon: '①',
    hint: '对市场理解 / 心理预期 / 形态预期的偏差',
  },
  direction: {
    accent: 'border-l-rose-500',
    icon: '②',
    hint: '事件 → 方向的推理链不成立',
  },
  technical: {
    accent: 'border-l-slate-500',
    icon: '③',
    hint: '数据缺失、接口慢（工程问题）',
  },
}

function sampleLine(cls: string, s: Sample): string {
  if (cls === 'signal') {
    const pnl = typeof s.pnl_pct === 'number' ? `${s.pnl_pct > 0 ? '+' : ''}${s.pnl_pct}%` : '—'
    return `${s.asset ?? '?'} · ${s.action ?? '?'} · ${pnl}`
  }
  if (cls === 'direction') {
    return `${s.asset ?? '?'} · 异动 ${s.move_pct ?? '—'}% · ${(s.reason ?? '').slice(0, 46)}`
  }
  const lat = s.latency_ms ? `${Math.round(Number(s.latency_ms) / 1000)}s` : ''
  return `${s.source ?? '?'} · ${s.asset ?? '?'} · ${s.feed_kind ?? ''} ${lat}`.trim()
}

export function ErrorTaxonomyPanel() {
  const [data, setData] = useState<TaxonomyResp | null>(null)
  const [error, setError] = useState<string | null>(null)
  const elapsed = useElapsed(!data && !error)

  useEffect(() => {
    let alive = true
    fetchWithFallback<TaxonomyResp>('/analysis/error-taxonomy?hours=168')
      .then((d) => {
        if (alive) setData(d)
      })
      .catch((e) => {
        if (alive) setError(String(e?.message ?? e))
      })
    return () => {
      alive = false
    }
  }, [])

  if (error) {
    return (
      <div className="surface-card border-l-4 border-l-rose-500 p-3 font-mono text-[11px] text-muted-foreground">
        三类错误统计读取失败（{error.slice(0, 80)}）—— 不代表没有错误，只代表这一格没取到数。
      </div>
    )
  }

  if (!data) {
    return (
      <div className="surface-card p-3" data-testid="error-taxonomy-loading">
        <div className="text-xs font-semibold text-foreground">三类错误仪表盘</div>
        <div className="mt-1 font-mono text-[11px] text-muted-foreground">
          读取中… {elapsed}s{elapsed >= 6 ? '（接口慢，后端在算）' : ''}
        </div>
      </div>
    )
  }

  const classes = data.classes ?? []

  return (
    <div className="surface-card p-3" data-testid="error-taxonomy">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-[13px] font-semibold text-foreground">三类错误仪表盘</h3>
          <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
            近 {Math.round((data.window_hours ?? 168) / 24)} 天 · 大类下面继续拆小类，方便快速锁定是哪一种错
          </p>
        </div>
      </div>

      <div className="grid gap-2 md:grid-cols-3">
        {classes.map((cls) => {
          const style = CLASS_STYLE[cls.key] ?? CLASS_STYLE.technical
          return (
            <div key={cls.key} className={`rounded border border-border border-l-4 ${style.accent} p-2.5`}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[12px] font-semibold text-foreground">
                  {style.icon} {cls.label}
                </span>
                {cls.rate != null ? (
                  <span className="font-mono text-[11px] text-rose-600 dark:text-rose-400">
                    {cls.rate}% 出错
                  </span>
                ) : (
                  <span className="font-mono text-[11px] text-muted-foreground">故障计数</span>
                )}
              </div>
              <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">{style.hint}</p>

              <div className="mt-1.5 space-y-0.5">
                {cls.subs.map((s, i) => (
                  <div key={`${cls.key}-${i}`} className="flex items-baseline justify-between gap-2">
                    <span className="truncate font-mono text-[11px] text-muted-foreground" title={s.label}>
                      {s.label}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] font-semibold text-foreground">
                      {s.n}
                    </span>
                  </div>
                ))}
                {cls.subs.length === 0 && (
                  <div className="font-mono text-[11px] text-muted-foreground">窗口内没有样本</div>
                )}
              </div>

              {cls.samples.length > 0 && (
                <div className="mt-1.5 border-t border-border/60 pt-1.5">
                  <div className="font-mono text-[11px] uppercase tracking-wide text-muted-foreground">
                    最近案例
                  </div>
                  {cls.samples.slice(0, 3).map((s, i) => (
                    <div key={`${cls.key}-s-${i}`} className="truncate font-mono text-[11px] text-muted-foreground">
                      · {sampleLine(cls.key, s)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default ErrorTaxonomyPanel
