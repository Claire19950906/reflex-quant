'use client'

import { useEffect, useRef, useState } from 'react'

/**
 * useElapsed — 计时器：active 为 true 时每秒返回从开始到现在的毫秒数，false 时归零。
 *
 * 用途（v337-UI）：慢接口（`/api/events/today` 4.6s、`v291/market-regime` 26s）会让面板
 * 长时间停在 "loading…"，用户无法判断是"在算"还是"已经卡死"。把秒数写出来，
 * 超过阈值再补一句"后端在算"，比无限转圈诚实。
 */
export function useElapsed(active: boolean, tickMs = 1000): number {
  const [ms, setMs] = useState(0)
  const startRef = useRef(0)

  useEffect(() => {
    if (!active) {
      setMs(0)
      return
    }
    startRef.current = Date.now()
    setMs(0)
    const id = window.setInterval(() => {
      setMs(Date.now() - startRef.current)
    }, tickMs)
    return () => window.clearInterval(id)
  }, [active, tickMs])

  return ms
}
