'use client'

/**
 * ShadowLedgerPanel — 分析页「影子样本」面板 (v335)
 *
 * 背景（用户原话："一天几乎都没有单，我没有样本我怎么测试"）：
 *   统一置信度闸门 20 把 94% 的方向决策降级成 HOLD，一天剩 0~1 单。
 *   但"没放行"不等于"没预测" —— 决策时点的方向、当时的价、4 小时后的价都在库里，
 *   全部可以结算。这个面板就是把那些"没开成的单"的真实成绩亮出来：
 *
 *   · 每天到底能攒多少样本（不再靠一天一单去猜准确率）
 *   · 整体命中率（50% = 抛硬币，低于 50% 说明方向判断不如随机）
 *   · 按品种拆（谁在拖后腿）
 *   · 门槛扫描：门槛从 20 降到 0，命中率会不会变好 —— 如果不变，那道闸门就是筛了个寂寞
 *
 * 数据流: /api/shadow-ledger-summary?days=40 (bridge 8100)
 */

import { useCallback, useEffect, useState } from 'react'
import { Activity, Scale, Target } from 'lucide-react'
import { fetchWithFallback } from '@/lib/api'
import { cn } from '@/lib/utils'

type Bucket = { n: number; hit_rate: number | null }
type Summary = {
  ok: boolean
  days?: number
  totals?: {
    samples: number
    settled: number
    hit_rate: number | null
    avg_move_pct: number | null
    avg_abs_move_pct: number | null
    samples_per_day: number
    days_covered?: number
    target_samples?: number
    remaining_to_target?: number
  }
  by_asset?: ({ asset: string; n: number; avg_abs_move_pct: number } & Bucket)[]
  by_direction?: ({ direction: string; n: number } & Bucket)[]
  gate_sweep?: ({ gate: number } & Bucket)[]
  sweep_verdict?: { code: string; delta: number | null; text: string }
  daily?: ({ date: string; n: number } & Bucket)[]
  note?: string
  error?: string
}

/** 命中率配色：50% 是分水岭，低于它还不如抛硬币。 */
function rateTone(rate: number | null | undefined) {
  if (rate == null) return 'text-muted-foreground'
  if (rate >= 55) return 'text-long'
  if (rate >= 50) return 'text-long/70'
  return 'text-short'
}

export function ShadowLedgerPanel({
  refreshMs = 120_000,
  days = 40,
}: {
  refreshMs?: number
  days?: number
}) {
  const [data, setData] = useState<Summary | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const json = await fetchWithFallback<Summary>(
        `/shadow-ledger-summary?days=${days}`,
      )
      if (!json.ok) {
        setErr(json.error || '影子账本暂无数据')
        setData(null)
      } else {
        setData(json)
        setErr(null)
      }
    } catch {
      setErr('影子账本端点不可用（bridge 8100）')
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => {
    void load()
    const t = setInterval(() => void load(), refreshMs)
    return () => clearInterval(t)
  }, [load, refreshMs])

  const t = data?.totals
  const target = t?.target_samples ?? 60
  const settledN = t?.settled ?? 0
  const pct = Math.min(100, Math.round((100 * settledN) / Math.max(target, 1)))
  const perDay = t?.samples_per_day ?? 0
  const etaDays = perDay > 0 ? Math.ceil((t?.remaining_to_target ?? 0) / perDay) : null
  const verdict = data?.sweep_verdict
  const verdictTone =
    verdict?.code === 'effective'
      ? 'text-long'
      : verdict?.code === 'inverted'
        ? 'text-short'
        : 'text-muted-foreground'
  return (
    <section className="rounded-lg border border-border/60 bg-card/40 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <Activity className="h-4 w-4" />
          影子样本（闸门没放行的单也在记账）
        </div>
        <span className="text-[11px] text-muted-foreground">
          近 {data?.days ?? days} 天
        </span>
      </div>

      {loading ? (
        <div className="inline-flex w-fit items-center gap-1 rounded border border-muted-foreground/25 bg-card/60 px-2 py-0.5 font-mono text-[11px] text-muted-foreground">读取中…</div>
      ) : err ? (
        <div className="py-4 text-center text-xs text-muted-foreground">{err}</div>
      ) : (
        <>
          {/* 三个大数字：样本够不够、准不准、稳不稳 */}
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded border border-border/40 p-2">
              <div className="text-[11px] text-muted-foreground">每天样本</div>
              <div className="text-lg font-semibold">
                {t?.samples_per_day ?? 0}
                <span className="ml-0.5 text-[11px] font-normal text-muted-foreground">条</span>
              </div>
            </div>
            <div className="rounded border border-border/40 p-2">
              <div className="text-[11px] text-muted-foreground">方向命中率</div>
              <div className={cn('text-lg font-semibold', rateTone(t?.hit_rate))}>
                {t?.hit_rate != null ? `${t.hit_rate}%` : '—'}
              </div>
            </div>
            <div className="rounded border border-border/40 p-2">
              <div className="text-[11px] text-muted-foreground">累计样本</div>
              <div className="text-lg font-semibold">
                {t?.settled ?? 0}
                <span className="ml-0.5 text-[11px] font-normal text-muted-foreground">
                  /{t?.samples ?? 0} 条
                </span>
              </div>
            </div>
          </div>

          {/* 样本进度：README 定的"能下结论"门槛是 ≥60 笔已结算 */}
          <div className="mt-2">
            <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
              <span>
                样本进度 {settledN}/{target}
                {(t?.remaining_to_target ?? 0) > 0 ? ` · 还差 ${t?.remaining_to_target} 条` : ' · 已到门槛'}
              </span>
              <span>
                {etaDays != null && etaDays > 0 ? `约 ${etaDays} 天` : '—'}
              </span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded bg-muted">
              <div
                className="h-full rounded bg-primary transition-all"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>

          {/* 品种拆解：谁在拖后腿 */}
          {data?.by_asset?.length ? (
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-1 text-[11px] text-muted-foreground">
                <Target className="h-3 w-3" /> 按品种
              </div>
              <div className="flex flex-col gap-0.5 text-xs">
                {data.by_asset.map((a) => (
                  <div key={a.asset} className="flex items-center justify-between">
                    <span>{a.asset}</span>
                    <span className={cn('tabular-nums', rateTone(a.hit_rate))}>
                      {a.hit_rate ?? '—'}%
                      <span className="ml-1 text-muted-foreground">· {a.n} 条</span>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {/* BUY / SELL 分开：看是不是系统性偏多 */}
          {data?.by_direction?.length ? (
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-1 text-[11px] text-muted-foreground">
                <Scale className="h-3 w-3" /> 按方向
              </div>
              <div className="flex flex-col gap-0.5 text-xs">
                {data.by_direction.map((d) => (
                  <div key={d.direction} className="flex items-center justify-between">
                    <span>{d.direction === 'BUY' ? '看涨' : '看跌'}</span>
                    <span className={cn('tabular-nums', rateTone(d.hit_rate))}>
                      {d.hit_rate ?? '—'}%
                      <span className="ml-1 text-muted-foreground">· {d.n} 条</span>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {/* 门槛扫描：同一批样本按不同门槛切，看门槛到底筛出了什么 */}
          {data?.gate_sweep?.length ? (
            <div className="mt-3">
              <div className="mb-1 text-[11px] text-muted-foreground">
                门槛扫描（门槛越高，留下的单有没有更准？）
              </div>
              {verdict ? (
                <div className={cn('mb-1 text-[11px] leading-relaxed', verdictTone)}>
                  结论：{verdict.text}
                </div>
              ) : null}
              <div className="grid grid-cols-6 gap-1 text-[11px] text-center">
                {data.gate_sweep.map((g) => (
                  <div key={g.gate} className="rounded border border-border/40 p-1">
                    <div className="text-muted-foreground">≥{g.gate}</div>
                    <div className={cn('font-medium', rateTone(g.hit_rate))}>
                      {g.hit_rate != null ? `${g.hit_rate}%` : '—'}
                    </div>
                    <div className="text-muted-foreground">{g.n} 条</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {data?.note ? (
            <div className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
              {data.note}
            </div>
          ) : null}
        </>
      )}
    </section>
  )
}
