'use client'

import { useEffect, useState, useCallback } from 'react'
import { formatDual, getDisplayTzName, setDisplayTz, listDisplayTzOptions, type TzName, type DualTime } from '@/lib/tz-utils'

interface TridentTimeProps {
  /** epoch sec（默认 now）；也可传自定义 epoch 如某个事件的 ts */
  epoch?: number
  /** 是否同时显示 4 个时区（默认 true）；false 时仅展示 display */
  showAll?: boolean
  /** 紧凑单行模式：一行内联展示 4 时区，用于顶栏/右上角 */
  compact?: boolean
  className?: string
}

const LABELS: Record<TzName, string> = {
  'Asia/Shanghai': '北京',
  'UTC': 'UTC',
  'US/Eastern': '美东',
  'US/Pacific': '美西',
}

export function TridentTime({ epoch, showAll = true, compact = false, className }: TridentTimeProps) {
  const [tz, setTz] = useState<TzName>(getDisplayTzName())
  const [dual, setDual] = useState<DualTime>(() =>
    formatDual(epoch ?? Math.floor(Date.now() / 1000), tz)
  )

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as TzName
      setTz(detail)
      setDual(formatDual(epoch ?? Math.floor(Date.now() / 1000), detail))
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('trident:tz-changed', handler)
    }
    return () => {
      if (typeof window !== 'undefined') {
        window.removeEventListener('trident:tz-changed', handler)
      }
    }
  }, [epoch])

  useEffect(() => {
    setDual(formatDual(epoch ?? Math.floor(Date.now() / 1000), tz))
  }, [epoch, tz])

  // 时钟跳动（仅在 epoch 缺省时）
  useEffect(() => {
    if (epoch !== undefined) return
    const t = setInterval(() => {
      setDual(formatDual(Math.floor(Date.now() / 1000), tz))
    }, 1000)
    return () => clearInterval(t)
  }, [epoch, tz])

  const onSelect = useCallback((name: TzName) => {
    setDisplayTz(name)
    setTz(name)
  }, [])

  const zones: Array<{ key: TzName; label: string; value: string | undefined }> = [
    { key: 'Asia/Shanghai', label: '北京', value: dual.cn },
    { key: 'UTC', label: 'UTC', value: dual.utc },
    { key: 'US/Eastern', label: '美东', value: dual.us_eastern },
    { key: 'US/Pacific', label: '美西', value: dual.us_pacific },
  ]

  if (compact) {
    return (
      <div className="flex shrink-0 items-center gap-1 font-mono text-[10px] text-muted-foreground">
        {zones.map((z, i) => (
          <span key={z.key} className="flex items-center gap-1">
            {i > 0 && <span className="text-muted-foreground/30">·</span>}
            <button
              onClick={() => onSelect(z.key)}
              className={`tabular-nums transition-colors ${
                tz === z.key ? 'font-semibold text-foreground' : 'hover:text-foreground'
              }`}
              title={`切换显示时区到 ${z.label} (${z.key})`}
            >
              {z.label} {z.value || '—'}
            </button>
          </span>
        ))}
      </div>
    )
  }

  return (
    <div className={className ?? 'flex flex-col gap-2 rounded border border-border bg-card/40 p-2 text-xs'}>
      <div className="flex flex-wrap gap-1">
        {listDisplayTzOptions().map((opt) => (
          <button
            key={opt}
            onClick={() => onSelect(opt)}
            className={`rounded px-2 py-1 font-mono text-[10px] transition-colors ${
              tz === opt
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/70'
            }`}
            title={`切到 ${LABELS[opt]} (${opt})`}
          >
            {LABELS[opt]}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[11px]">
        <div className="flex justify-between">
          <span className="text-muted-foreground">UTC</span>
          <span>{dual.utc || '—'}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">北京</span>
          <span>{dual.cn || '—'}</span>
        </div>
        {showAll && (
          <>
            <div className="flex justify-between">
              <span className="text-muted-foreground">美东</span>
              <span>{dual.us_eastern || '—'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">美西</span>
              <span>{dual.us_pacific || '—'}</span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}