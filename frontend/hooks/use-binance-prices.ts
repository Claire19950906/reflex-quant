'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v252.2: change24h nullable — 后端拿不到 prev_close 时返回 null,
// 前端按 "—" 显示,绝不假造 "+0.00%"。
export type PriceInfo = { price: number; change24h: number | null }
export type PriceMap = Record<string, PriceInfo>

type MarketResponse = {
  items: Array<{ asset: string; price: number; change24h: number | null; sourceCount: number; updated_at: string }>
  sources: Record<string, { status: string }>
  updated_at: string
}

const API_URL = `${API_BASE}/market/prices`

export function useBinancePrices() {
  const [prices, setPrices] = useState<PriceMap>({})
  const [connected, setConnected] = useState(false)
  const [sourceCount, setSourceCount] = useState(0)
  const [lastUpdated, setLastUpdated] = useState<string | null>(null)
  // v310 P0 fix: loaded —— 是否**完成过一次**成功拉取。
  //   之前前端无法区分「还没拉到」和「这个品种后端根本没给」, 两者都渲染成「加载中」,
  //   于是 XAU/WTI(只走 SSE price_update, /market/prices 不含这两个)永远停在「加载中」。
  //   有了 loaded 就能诚实区分: 未加载 → 加载中; 已加载但无该品种 → 「—」(真的没数据)。
  const [loaded, setLoaded] = useState(false)
  // v466: 报价年龄（秒）。后端 updated_at 是北京时间串；解析失败返回 null（不显示陈旧提示，不造假）。
  const [ageSec, setAgeSec] = useState<number | null>(null)
  // v310 P0 fix: in-flight 闸门 —— setInterval 每 3s 触发一次, 不等上一轮结束。
  //   后端一慢(本机 bridge 偶尔 >3s)请求会**堆积**, 越堆越多把 bridge 拖垮。
  const inFlightRef = useRef(false)

  function parseAge(updatedAt: string | null): number | null {
    if (!updatedAt) return null
    const iso = updatedAt.includes('T') ? updatedAt : updatedAt.replace(' ', 'T')
    // 无时区串按北京时间解释
    const withTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + '+08:00'
    const ms = Date.parse(withTz)
    if (Number.isNaN(ms)) return null
    return Math.max(0, Math.round((Date.now() - ms) / 1000))
  }

  useEffect(() => {
    let active = true
    async function poll() {
      // 上一轮还没回来 → 跳过这一轮, 不制造请求堆积
      if (inFlightRef.current) return
      inFlightRef.current = true
      try {
        const response = await fetch(API_URL, { cache: 'no-store' })
        if (!response.ok) throw new Error(String(response.status))
        const data = await response.json() as MarketResponse
        if (!active) return
        setPrices(Object.fromEntries(data.items.map((item) => [item.asset, { price: item.price, change24h: item.change24h }])))
        setSourceCount(Object.values(data.sources).filter((source) => source.status !== 'unavailable').length)
        setLastUpdated(data.updated_at)
        setAgeSec(parseAge(data.updated_at))
        setConnected(data.items.length > 0)
        setLoaded(true)
      } catch {
        if (active) setConnected(false)
      } finally {
        inFlightRef.current = false
      }
    }
    void poll()
    const timer = setInterval(() => void poll(), 3000)
    return () => { active = false; clearInterval(timer) }
  }, [])

  const pricesByPair = useMemo(() => Object.fromEntries(
    Object.entries(prices).map(([asset, info]) => [`${asset}USDT`, info]),
  ), [prices])

  return { prices, pricesByPair, connected, sourceCount, lastUpdated, ageSec, loaded }
}
