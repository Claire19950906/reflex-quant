'use client'

/**
 * _history-drawer.tsx — /review 复盘历史抽屉(v302+ P6)
 *
 * 设计:
 *  - 自包含:打开抽屉时才请求 /replay/positions,不打开不请求(节省流量)
 *  - 不与 /replay 页面强耦合:不复用 ReplayList(需要把整个 ReplayPanel 数据流搬过来)
 *  - 简化为"浏览模式":每行展示 时间/资产/动作/分数/状态,点击只高亮,不导航
 *  - a11y:role="dialog" + aria-modal + Esc 关闭 + 焦点移到关闭按钮
 */

import { useEffect, useRef, useState, useCallback } from 'react'
import { X, RefreshCcw, Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { API_BASE } from '@/lib/api'

type SimSignal = {
  id: number
  news_time: string
  source: string
  news_text?: string
  asset: string
  action: 'BUY' | 'SELL' | 'HOLD'
  score: number
  settled: boolean
  forward_pnl_usdt: number | null
  current_pnl_usdt: number | null
}

const ACTION_COLOR: Record<SimSignal['action'], string> = {
  BUY: 'text-green-600',
  SELL: 'text-red-600',
  HOLD: 'text-muted-foreground',
}

function fmtTime(s: string): string {
  const m = s.match(/(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/)
  return m ? `${m[1]}-${m[2]} ${m[3]}:${m[4]}` : s.slice(5, 16)
}

function fmtPnl(n: number | null): string {
  if (n == null) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}`
}

export function HistoryDrawer({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const [signals, setSignals] = useState<SimSignal[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/replay/positions?limit=50`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`${r.status}`)
      const data = (await r.json()) as { positions?: SimSignal[] }
      setSignals(data.positions ?? [])
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void reload()
  }, [open, reload])

  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  useEffect(() => {
    if (open) closeRef.current?.focus()
  }, [open])

  if (!open) return null

  const settledCount = signals.filter((s) => s.settled).length
  const pendingCount = signals.length - settledCount

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/30 transition-opacity"
        onClick={onClose}
        aria-hidden
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="history-drawer-title"
        className={cn(
          'fixed right-0 top-0 z-50 flex h-full w-[min(420px,90vw)] flex-col gap-2 border-l border-border bg-background p-3 shadow-xl',
          'animate-in slide-in-from-right duration-200',
        )}
      >
        <header className="flex items-center justify-between border-b border-border pb-2">
          <div>
            <h2
              id="history-drawer-title"
              className="font-mono text-sm font-bold tracking-wide text-foreground"
            >
              复盘历史
            </h2>
            <p className="font-mono text-[10px] text-muted-foreground">
              最近 50 笔 · 已结算 {settledCount} · 持仓中 {pendingCount}
            </p>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => void reload()}
              disabled={loading}
              aria-label="刷新列表"
              className="inline-flex h-7 w-7 items-center justify-center rounded text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
            >
              <RefreshCcw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
            </button>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              aria-label="关闭抽屉"
              className="inline-flex h-7 w-7 items-center justify-center rounded text-muted-foreground hover:bg-secondary hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {loading && signals.length === 0 ? (
            <div className="flex h-32 items-center justify-center gap-2 font-mono text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              加载中…
            </div>
          ) : error ? (
            <div className="rounded border border-destructive/40 bg-destructive/5 p-2 font-mono text-xs text-destructive">
              加载失败:{error}
            </div>
          ) : signals.length === 0 ? (
            <div className="flex h-32 items-center justify-center font-mono text-xs text-muted-foreground">
              暂无数据
            </div>
          ) : (
            <ul className="divide-y divide-border/60" data-testid="history-list">
              {signals.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(s.id === selectedId ? null : s.id)}
                    aria-pressed={selectedId === s.id}
                    className={cn(
                      'flex w-full flex-col gap-1 px-2 py-1.5 text-left font-mono text-[11px] transition-colors',
                      'hover:bg-secondary/50',
                      selectedId === s.id && 'bg-secondary',
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">{fmtTime(s.news_time)}</span>
                      <span className={cn('font-bold', ACTION_COLOR[s.action])}>{s.action}</span>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-bold text-foreground">{s.asset}</span>
                      <span className="text-muted-foreground">
                        分数 {s.score.toFixed(2)} ·{' '}
                        {s.settled ? (
                          <span
                            className={
                              (s.forward_pnl_usdt ?? 0) >= 0 ? 'text-green-600' : 'text-red-600'
                            }
                          >
                            {fmtPnl(s.forward_pnl_usdt)}
                          </span>
                        ) : (
                          <span>{fmtPnl(s.current_pnl_usdt)} (持仓中)</span>
                        )}
                      </span>
                    </div>
                    {s.news_text && (
                      <div className="line-clamp-1 text-muted-foreground">{s.news_text}</div>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <footer className="border-t border-border pt-2 text-[10px] text-muted-foreground">
          提示:复盘与结算请前往 <a href="/replay" className="text-primary hover:underline">/replay</a>。
        </footer>
      </div>
    </>
  )
}
