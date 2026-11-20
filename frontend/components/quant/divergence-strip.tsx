'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'

type Item = {
  asset: string
  type: 'bearish' | 'bullish'
  kind: string
  grade: string
  score: number
  context: string
  bars_since_confirm: number
  confirm_at: string
  status: string
}

type Resp = { ok: boolean; count: number; items: Item[] }

function contextText(ctx: string): string {
  const parts = (ctx || '').split('/')
  const kind = parts[0] === 'hidden' ? '隐藏' : '常规'
  const side = parts[1] === 'bearish' ? '顶' : '底'
  const trend = parts[2] === 'up' ? '上升趋势' : parts[2] === 'down' ? '下降趋势' : '趋势未知'
  return `${kind}${side}背离 · ${trend}`
}

export function DivergenceStrip() {
  const [data, setData] = useState<Resp | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    fetch('/api/divergence/effective?within=30')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((d) => { if (alive) setData(d) })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [])

  const items = (data?.items || []).slice(0, 6)

  return (
    <div className="surface-card overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border bg-card/60 px-3 py-2">
        <span className="font-mono text-[12px] font-semibold text-foreground">有效背离信号</span>
        <span className="font-mono text-[11px] text-muted-foreground">仅显示通过样本外检验的规则</span>
        <Link href="/divergence" className="ml-auto font-mono text-[11px] text-muted-foreground hover:text-foreground">
          背离雷达 →
        </Link>
      </div>
      {!data && !failed ? (
        <div className="px-3 py-4 font-mono text-[11px] text-muted-foreground">加载中…</div>
      ) : failed ? (
        <div className="px-3 py-4 font-mono text-[11px] text-muted-foreground/70">
          背离信号暂不可用（接口超时或服务异常，稍后自动重试）
        </div>
      ) : items.length === 0 ? (
        <div className="px-3 py-4 font-mono text-[11px] text-muted-foreground">
          最近 30 根内没有命中有效规则的背离信号。
        </div>
      ) : (
        <div className="divide-y divide-border/60">
          {items.map((x, i) => (
            <Link
              key={`${x.asset}-${x.confirm_at}-${i}`}
              href="/divergence"
              className="flex items-center gap-3 px-3 py-2 hover:bg-muted/30"
            >
              <span className="w-16 font-mono text-xs text-foreground">{x.asset}</span>
              <span className={`font-mono text-xs ${x.type === 'bearish' ? 'text-short' : 'text-long'}`}>
                {contextText(x.context)}
              </span>
              <span className="rounded-sm border border-border px-1 py-0.5 font-mono text-[11px] text-muted-foreground">
                {x.grade} · {x.score.toFixed(0)}分
              </span>
              <span className="ml-auto font-mono text-[11px] text-muted-foreground">
                {x.bars_since_confirm} 根前确认 · {x.status}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
