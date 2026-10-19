'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { AlertTriangle, CalendarClock, CheckCircle2 } from 'lucide-react'

interface CalendarEvent {
  time: string
  star: number
  title: string
  forecast: string
  previous: string
}

interface CalendarStatus {
  ok: boolean
  count: number
  error: string
  sample_event: string
  events: CalendarEvent[]
  quota_exhausted: boolean
  star_threshold: number
  horizon_hours: number
  checked_at?: number
}

/**
 * 重要宏观数据日历提示条（与 ai_worker 注入到主分析 prompt 的同一份数据）
 *
 * 用途：让用户一眼看见
 * - 未来 24h 有多少 ★2+ 重要财经事件（CPI/非农/利率决议等）
 * - 日历通道是否正常（ok=true）
 * - 配额是否耗尽（quota_exhausted=true → 提示会失败）
 *
 * 数据源: /api/calendar/status（自探式，不打 MCP 配额）
 */
export function CalendarHintBanner() {
  const [status, setStatus] = useState<CalendarStatus | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    const load = () =>
      fetch(`${API_BASE}/calendar/status`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
        .then((d: CalendarStatus) => {
          if (alive) {
            setStatus(d)
            setLoading(false)
          }
        })
        .catch(() => {
          if (alive) setLoading(false)
        })
    void load()
    // 5 分钟轮询（日历数据变化慢）
    const t = window.setInterval(load, 5 * 60_000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])

  if (loading) {
    return (
      <div className="surface-card flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground">
        <CalendarClock className="h-3.5 w-3.5" />
        <span className="font-mono">日历通道加载中…</span>
      </div>
    )
  }
  if (!status) {
    return (
      <div className="surface-card flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground">
        <AlertTriangle className="h-3.5 w-3.5 text-short" />
        <span className="font-mono">日历通道无响应</span>
      </div>
    )
  }

  // 配额耗尽：显眼警告
  if (status.quota_exhausted) {
    return (
      <div className="surface-card border-short/40 bg-short/5 flex items-start gap-2 px-3 py-2">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-short" />
        <div className="min-w-0 flex-1">
          <p className="font-mono text-xs text-short">
            日历通道配额耗尽（所有 JIN10_MCP_TOKEN 今日 1500/1500 用完）
          </p>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            提示：AI 主分析 prompt 当前 <b>无</b> 宏观日历上下文，明日 0 点重置后恢复
          </p>
        </div>
      </div>
    )
  }

  // 通道异常
  if (!status.ok && status.error) {
    return (
      <div className="surface-card border-short/40 bg-short/5 flex items-start gap-2 px-3 py-2">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-short" />
        <div className="min-w-0 flex-1">
          <p className="font-mono text-xs text-short">日历通道异常</p>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground truncate" title={status.error}>
            {status.error.slice(0, 160)}
          </p>
        </div>
      </div>
    )
  }

  // 正常：展示 ★2+ 事件数和首条示例
  // 布局：
  //   宽屏 (xl+ = 1280px+) — 一行：图标 / 统计 / 首条 / "↔ AI 同源"
  //   中屏 (lg+ = 1024px+)  — 两行：统计 + 首条；"↔ AI" 单独一行
  //   窄屏 (< lg)          — 保持原 vertical 多行
  return (
    <details
      className="surface-card border-long/30 bg-long/5 px-3 py-2"
      open={status.count <= 4}
    >
      <summary className="flex cursor-pointer list-none items-start gap-2">
        <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-long" />
        <div className="min-w-0 flex-1">
          {/* 宽屏单行布局 (xl+) */}
          <div className="hidden xl:flex items-center gap-2 font-mono whitespace-nowrap">
            <span className="text-xs text-foreground">
              未来 {status.horizon_hours}h 内有 <b className="tabular-nums">{status.count}</b> 个重要财经事件（★{status.star_threshold}+）
            </span>
            {status.sample_event && (
              <>
                <span className="text-muted-foreground/50">|</span>
                <span className="min-w-0 truncate text-[10px] text-muted-foreground" title={status.sample_event}>
                  首条: {status.sample_event}
                </span>
              </>
            )}
            <span className="ml-auto shrink-0 text-[9px] text-muted-foreground/60">
              ↔ AI 主分析 prompt 同源注入
            </span>
          </div>
          {/* 中屏两行布局 (lg-xl) */}
          <div className="hidden lg:flex xl:hidden flex-col gap-0.5">
            <p className="font-mono text-xs text-foreground whitespace-nowrap">
              未来 {status.horizon_hours}h 内有 <b className="tabular-nums">{status.count}</b> 个重要财经事件（★{status.star_threshold}+）
              {status.sample_event && (
                <span className="ml-2 text-[10px] text-muted-foreground" title={status.sample_event}>
                  · 首条: {status.sample_event}
                </span>
              )}
            </p>
            <p className="font-mono text-[9px] text-muted-foreground/60">
              ↔ AI 主分析 prompt 同源注入
            </p>
          </div>
          {/* 窄屏多行布局 (<lg) — 保持原版 */}
          <div className="lg:hidden">
            <p className="font-mono text-xs text-foreground">
              未来 {status.horizon_hours}h 内有 <b className="tabular-nums">{status.count}</b> 个重要财经事件（★{status.star_threshold}+）
            </p>
            {status.sample_event && (
              <p className="mt-0.5 font-mono text-[10px] text-muted-foreground truncate" title={status.sample_event}>
                首条: {status.sample_event}
              </p>
            )}
            <p className="mt-0.5 font-mono text-[9px] text-muted-foreground/60">
              ↔ AI 主分析 prompt 同源注入
            </p>
          </div>
        </div>
      </summary>
      {status.events && status.events.length > 0 && (
        <ul className="mt-2 border-t border-long/20 pt-2">
          {status.events.map((e, i) => (
            <li key={`${e.time}-${i}`} className="flex items-baseline gap-2 font-mono text-[10px]">
              <span className="shrink-0 text-muted-foreground">{e.time}</span>
              <span className="shrink-0 text-long">★{e.star}</span>
              <span className="min-w-0 flex-1 truncate text-foreground" title={e.title}>
                {e.title}
              </span>
              {e.forecast && (
                <span className="shrink-0 text-muted-foreground" title={`预期值：${e.forecast}`}>
                  预期 {e.forecast}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </details>
  )
}