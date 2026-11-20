'use client'

import { useEffect, useMemo, useState } from 'react'

import { fetchWithFallback } from '@/lib/api'
import { useElapsed } from '@/lib/hooks/use-elapsed'

// 事件效力散点（v344）
//
// 回答一个问题：**哪一类事件，真的把价格推动了？**
//   X 轴 = 因果评分（事件 ↔ 异动的匹配强度，0~1）
//   Y 轴 = 异动幅度 |%|（越大越是"真的动了"）
//   · 右上角：高确信 + 大幅 → 值得进因果链、值得加权重
//   · 右下角：高确信 + 小幅 → 逻辑成立但市场没反应（噪音级）
//   · 左侧：低评分 → 大概率时间巧合（AI 已判"不成立"的点用空心圈表示）
// 按 move_kind 着色：快涨/快跌/慢涨/慢跌，能直接看出"慢涨是不是更容易被识别"。

type Point = {
  asset: string
  x: number
  y: number
  kind: string
  rejected?: number
  reason?: string
  lag_sec?: number
  ts?: number
}

type ScatterResp = {
  ok: boolean
  days: number
  points: Point[]
  n: number
  n_effective: number
  by_kind: Record<string, number>
  x_label: string
  y_label: string
}

const KIND_COLOR: Record<string, string> = {
  fast_rise: '#ef4444',
  fast_fall: '#22c55e',
  slow_rise: '#f59e0b',
  slow_fall: '#3b82f6',
}

const KIND_LABEL: Record<string, string> = {
  fast_rise: '快涨',
  fast_fall: '快跌',
  slow_rise: '慢涨',
  slow_fall: '慢跌',
}

const W = 640
const H = 260
const PAD = { l: 44, r: 12, t: 14, b: 30 }

export function EventScatterPanel() {
  const [data, setData] = useState<ScatterResp | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hover, setHover] = useState<Point | null>(null)
  const [days, setDays] = useState(30)
  const elapsed = useElapsed(!data && !error)

  useEffect(() => {
    let alive = true
    setData(null)
    fetchWithFallback<ScatterResp>(`/attribution/event-scatter?days=${days}&limit=400`)
      .then((d) => {
        if (alive) setData(d)
      })
      .catch((e) => {
        if (alive) setError(String(e?.message ?? e))
      })
    return () => {
      alive = false
    }
  }, [days])

  const pts = data?.points ?? []
  const yMax = useMemo(() => {
    const m = pts.reduce((a, p) => Math.max(a, p.y), 0)
    return Math.max(2, Math.ceil(m))
  }, [pts])

  const innerW = W - PAD.l - PAD.r
  const innerH = H - PAD.t - PAD.b
  const scaleX = (x: number) => PAD.l + Math.max(0, Math.min(1, x)) * innerW
  const scaleY = (y: number) => PAD.t + (1 - Math.min(1, y / yMax)) * innerH

  if (error) {
    return (
      <div className="surface-card border-l-4 border-l-rose-500 p-3 font-mono text-[11px] text-muted-foreground">
        散点读取失败（{error.slice(0, 70)}）—— 是接口问题，不代表没有数据。
      </div>
    )
  }

  return (
    <div className="surface-card p-3" data-testid="event-scatter">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-[13px] font-semibold text-foreground">事件效力散点</h3>
          <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
            横轴 因果评分 · 纵轴 异动幅度 —— 右上角才是"真能推动价格"的事件
          </p>
        </div>
        <div className="flex items-center gap-1">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDays(d)}
              className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${
                days === d
                  ? 'border-primary/50 bg-primary/10 text-primary'
                  : 'border-border text-muted-foreground hover:bg-secondary/40'
              }`}
            >
              {d}天
            </button>
          ))}
        </div>
      </div>

      {!data ? (
        <div className="py-6 text-center font-mono text-[11px] text-muted-foreground">
          读取中… {elapsed}s
          {elapsed >= 6 ? '（接口慢，后端在算）' : ''}
        </div>
      ) : pts.length === 0 ? (
        <div className="py-6 text-center font-mono text-[11px] text-muted-foreground">
          这 {days} 天没有"事件-异动"配对记录。
          <br />
          散点靠 `event_move_attribution` 喂数，只有「异动被 AI 归因到某条新闻」才会有点。
        </div>
      ) : (
        <>
          <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="事件效力散点图">
            {/* 网格 */}
            {[0, 0.25, 0.5, 0.75, 1].map((t) => (
              <line
                key={`vx-${t}`}
                x1={PAD.l + t * innerW}
                y1={PAD.t}
                x2={PAD.l + t * innerW}
                y2={PAD.t + innerH}
                stroke="currentColor"
                strokeOpacity={0.12}
                strokeWidth={1}
              />
            ))}
            {[0, 0.5, 1].map((t) => (
              <line
                key={`hy-${t}`}
                x1={PAD.l}
                y1={PAD.t + t * innerH}
                x2={PAD.l + innerW}
                y2={PAD.t + t * innerH}
                stroke="currentColor"
                strokeOpacity={0.12}
                strokeWidth={1}
              />
            ))}
            {/* 有效区高亮（x>=0.8 且 y>=1%） */}
            <rect
              x={scaleX(0.8)}
              y={scaleY(yMax)}
              width={scaleX(1) - scaleX(0.8)}
              height={scaleY(1) - scaleY(yMax)}
              fill="#22c55e"
              fillOpacity={0.08}
            />
            <text x={scaleX(0.8) + 4} y={PAD.t + 10} className="fill-emerald-600" fontSize={9}>
              有效区
            </text>

            {pts.map((p, i) => {
              const fill = KIND_COLOR[p.kind] ?? '#94a3b8'
              return (
                <circle
                  key={`${p.asset}-${i}`}
                  cx={scaleX(p.x)}
                  cy={scaleY(p.y)}
                  r={p.rejected ? 2.2 : 3}
                  fill={p.rejected ? 'none' : fill}
                  stroke={p.rejected ? '#ef4444' : fill}
                  strokeWidth={p.rejected ? 1 : 0.2}
                  fillOpacity={0.75}
                  onMouseEnter={() => setHover(p)}
                  onMouseLeave={() => setHover(null)}
                  style={{ cursor: 'pointer' }}
                />
              )
            })}

            {/* 轴 */}
            <text x={PAD.l} y={H - 8} fontSize={9} className="fill-current opacity-60">
              0
            </text>
            <text x={PAD.l + innerW - 8} y={H - 8} fontSize={9} className="fill-current opacity-60">
              1（高确信）
            </text>
            <text x={4} y={PAD.t + 8} fontSize={9} className="fill-current opacity-60">
              {yMax}%
            </text>
            <text x={4} y={PAD.t + innerH} fontSize={9} className="fill-current opacity-60">
              0%
            </text>
          </svg>

          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px]">
            {Object.keys(KIND_LABEL).map((k) => (
              <span key={k} className="flex items-center gap-1 text-muted-foreground">
                <span className="inline-block h-2 w-2 rounded-full" style={{ background: KIND_COLOR[k] }} />
                {KIND_LABEL[k]} {data.by_kind?.[k] ?? 0}
              </span>
            ))}
            <span className="flex items-center gap-1 text-muted-foreground">
              <span className="inline-block h-2 w-2 rounded-full border border-rose-500" />
              AI 判“不成立”
            </span>
            <span className="text-emerald-600 dark:text-emerald-400">
              有效事件 {data.n_effective} / 共 {data.n} 个点
            </span>
          </div>

          <div className="mt-1 min-h-[16px] font-mono text-[11px] text-muted-foreground">
            {hover
              ? `${hover.asset} · ${KIND_LABEL[hover.kind] ?? hover.kind} · 评分 ${hover.x} · 幅度 ${hover.y}%` +
                (hover.rejected ? ` · 已判不成立（${(hover.reason ?? '').slice(0, 30)}）` : '')
              : '把鼠标放到点上，这里会显示该笔的事件/资产/幅度'}
          </div>
        </>
      )}
    </div>
  )
}

export default EventScatterPanel
