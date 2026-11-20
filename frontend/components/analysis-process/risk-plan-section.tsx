'use client'

/**
 * v412: 分区 ④ 前置 —— 风控档位（仓位系数 + 止损）。
 *
 * 为什么单独一层：`decision_guard` 的 `risk_plan` 已经给出建议仓位与止损 ATR 倍数，
 * 但交易执行区只显示"模拟持仓有没有"，没人看得到"该用多大仓位"。
 * 这里把状态层的**执行口径**单独摆出来，且明确标注它来自当前快照（asof）。
 *
 * 边界（必须显示，不许省略）：
 *   - 档位不决定方向，只决定仓位与止损；
 *   - 读数是当前快照，不是决策当时的历史档位；
 *   - 读不到就写"没有读数"，不补中性档位。
 */

import { useEffect, useState } from 'react'
import { describeRiskPlan, fetchRiskSnapshot, type RiskSnapshot } from '@/lib/risk-plan'
import { KeyValueGrid, SectionCard } from './sections-shared'

export function RiskPlanSection({
  asset,
  action,
}: {
  asset: string | null
  action?: string | null
}) {
  const [snapshot, setSnapshot] = useState<RiskSnapshot | null>(null)
  const [state, setState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle')
  const [error, setError] = useState<string>('')

  useEffect(() => {
    if (!asset) return
    let cancelled = false
    setState('loading')
    setError('')
    fetchRiskSnapshot(asset, '4h')
      .then((snap) => {
        if (cancelled) return
        setSnapshot(snap)
        setState('done')
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : String(e))
        setState('error')
      })
    return () => {
      cancelled = true
    }
  }, [asset])

  if (!asset) {
    return (
      <SectionCard tone="blue" step="④" title="风控档位" subtitle="仓位系数 + 止损 ATR 倍数">
        <div className="font-mono text-xs text-muted-foreground">
          这条新闻没有明确标的，读不到风控档位 —— 不是"中性档位"，是没标的。
        </div>
      </SectionCard>
    )
  }

  if (state === 'loading') {
    return (
      <SectionCard tone="blue" step="④" title="风控档位" subtitle={`${asset} 4h`}>
        <div className="font-mono text-xs text-muted-foreground">正在读 {asset} 4h 状态…</div>
      </SectionCard>
    )
  }

  if (state === 'error' || !snapshot?.available) {
    const reason = error || snapshot?.error || '接口没有返回档位'
    return (
      <SectionCard
        tone="gold"
        step="④"
        title="风控档位"
        subtitle={`${asset} 4h · 没有读数`}
        badge="无读数"
      >
        <div className="font-mono text-xs text-muted-foreground">
          读不到风控档位（{reason}）。没有读数时不要假设"风险适中" ——
          该资产这一档可能是缺 K 线、缺 OI 或缺成交量数据。
        </div>
      </SectionCard>
    )
  }

  const side = action === 'BUY' || action === 'SELL' ? action : null
  const view = describeRiskPlan(snapshot, snapshot.last_close ?? null, side)
  if (!view) {
    return (
      <SectionCard tone="gold" step="④" title="风控档位" subtitle="没有档位数据" badge="无读数">
        <div className="font-mono text-xs text-muted-foreground">
          接口可用但没有给出档位字段，不做推测。
        </div>
      </SectionCard>
    )
  }

  const tone = view.tone === 'short' ? 'red' : view.tone === 'warn' ? 'gold' : 'green'
  const asof = snapshot.asof
    ? new Date(snapshot.asof).toLocaleString('zh-CN', { hour12: false })
    : '—'

  return (
    <SectionCard
      tone={tone}
      step="④"
      title="风控档位"
      subtitle={`${snapshot.asset ?? asset} ${snapshot.timeframe ?? '4h'} · 仓位系数 + 止损`}
      badge={view.levelLabel}
    >
      <KeyValueGrid
        items={[
          { label: '档位', value: view.levelLabel },
          { label: '建议仓位', value: view.sizeLabel },
          { label: '止损', value: view.stopLabel },
          { label: '5 根内常见逆风', value: view.adverseLabel },
          {
            label: `止损价（按最新收盘 ${snapshot.last_close ?? '—'}）`,
            value:
              view.stopPrice !== null && side
                ? `${view.stopPrice.toFixed(2)}（${side} 方向）`
                : '—（方向未定或无价格，只给百分比）',
          },
          { label: '读数时间（最后一根 K）', value: asof },
        ]}
      />

      {view.drivers.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {view.drivers.map((d) => (
            <span
              key={d}
              className="rounded border border-border/70 bg-background/60 px-1.5 py-0.5 font-mono text-[11px] text-foreground/80"
            >
              {d}
            </span>
          ))}
        </div>
      ) : null}

      <p className="mt-3 font-mono text-[11px] leading-relaxed text-muted-foreground">
        {view.note}
      </p>
      <p className="mt-1 font-mono text-[11px] leading-relaxed text-foreground/75">{view.basisNote}</p>
      {view.requireNote ? (
        <p className="mt-1 font-mono text-[11px] leading-relaxed text-foreground/80">
          {view.requireNote}
        </p>
      ) : null}
      {view.holdingNote ? (
        <p className="mt-1 font-mono text-[11px] leading-relaxed text-foreground/70">
          {view.holdingNote}
        </p>
      ) : null}
      {view.oddsNote ? (
        <p className="mt-1 font-mono text-[11px] leading-relaxed text-foreground/70">
          {view.oddsNote}
        </p>
      ) : null}
      {view.positionNote ? (
        <p className="mt-1 font-mono text-[11px] leading-relaxed text-foreground/80">
          {view.positionNote}
        </p>
      ) : null}
      {view.equalRiskNote ? (
        <p className="mt-1 font-mono text-[11px] leading-relaxed text-muted-foreground">
          {view.equalRiskNote}
        </p>
      ) : null}
      <p className="mt-1.5 font-mono text-[11px] leading-relaxed text-foreground/75">
        边界：档位只回答&quot;仓位多大、止损多远&quot;，<span className="font-bold">不回答&quot;做多还是做空&quot;</span>。
        方向由新闻/基本面链路决定；档位是<b>当前快照</b>读数，不是决策当时的历史值。
      </p>
      {action === 'HOLD' ? (
        <p className="mt-1.5 font-mono text-[11px] leading-relaxed text-hold">
          本次决策是 HOLD：档位仅用于复盘口径（如果要做，该用多大仓位、止损放哪），不构成开仓依据。
        </p>
      ) : null}
    </SectionCard>
  )
}

export type { RiskSnapshot }
