'use client'

import { useCallback, useRef, useState } from 'react'
import { API_BASE, BRIDGE_BASE, SAME_BACKEND } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'
import { sharedJson } from '@/lib/shared-json'

// 今日权威信号计数（多/空拆分）。
//
// 背景：AccountKpis 原来靠传入的 events 列表本地重数 BUY/SELL，但 /api/events
//   受 limit 截断（HOLD 占大头），今日窗口内决策行可能被切掉一部分，本地数偏少；
//   且早期日期前缀匹配只认 T 分隔、不认空格分隔，决策行被整体滤掉（恒显 0）。
//
// 口径：直连 /api/events/today 的 signal_count / signal_long / signal_short
//   （后端按 ai_decisions.ts 整数窗口聚合，不受列表 limit 影响）。接口不可用或
//   旧后端未返回多空拆分时返回 null，由卡片回退到 events 本地计数兜底，不编 0。
export type TodaySignalCount = {
  total: number
  long: number
  short: number
}

type TodayPayload = {
  signal_count?: number
  signal_long?: number
  signal_short?: number
}

async function fetchTodaySignal(): Promise<TodaySignalCount | null> {
  const path = '/events/today'
  const bases = SAME_BACKEND ? [API_BASE] : [API_BASE, BRIDGE_BASE]
  let lastErr = ''
  for (const base of bases) {
    try {
      // v499: 与 StatCards/EventBus 同参数 → 4s TTL 合并，避免 3 倍读放大
      const j = await sharedJson<TodayPayload>(`${base}${path}`, 4000, { cache: 'no-store' })
      if (j == null || typeof j !== 'object') { lastErr = 'empty payload'; continue }
      const total = Number(j.signal_count ?? 0)
      if (!Number.isFinite(total)) { lastErr = 'bad total'; continue }
      // 旧后端可能只返回 signal_count、没有 long/short 拆分 → 返回 null 让前端兜底。
      if (j.signal_long == null || j.signal_short == null) return null
      return {
        total,
        long: Number(j.signal_long ?? 0),
        short: Number(j.signal_short ?? 0),
      }
    } catch (e) {
      lastErr = e instanceof Error ? e.message : String(e)
    }
  }
  throw new Error(`fetchTodaySignal ${lastErr}`)
}

export function useTodaySignalCount() {
  const [data, setData] = useState<TodaySignalCount | null>(null)
  const readyRef = useRef(false)

  const load = useCallback(async () => {
    try {
      const d = await fetchTodaySignal()
      setData(d)
      if (d) readyRef.current = true
    } catch {
      // 失败保留上一次值；ready 前为 null，卡片回退本地计数。
    }
  }, [])

  // 15s 快轮询追首轮，就绪后 60s 慢刷新；in-flight 守卫跳过未完成的拍。
  usePollRefresh(() => void load(), 60_000, [load], true)
  usePollRefresh(() => { if (!readyRef.current) void load() }, 15_000, [load], true)

  return data
}
