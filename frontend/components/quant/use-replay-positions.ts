'use client'

import { useCallback, useRef, useState } from 'react'
import { fetchReplayPositions, type ReplayPositions } from '@/lib/replay-data'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'

// P3-UI: 首页「账户快照」统一取数 hook。
//
// 背景：原来 AccountKpis / PositionsTable / PaperEquityCard 三个组件各自
//   useEffect + setInterval 打同一个 GET /replay/positions，首页一开就是 3 份
//   并行轮询（60s / 60s / 120s + 15s 快轮询），同一份持仓与账户快照被重复拉取。
//
// 做法：在 LiveDesk 顶层挂一份这个 hook，把 { data, err, loading } 下发给三个卡片；
//   卡片只做派生计算与渲染，不再各自发请求。轮询策略合并为：
//     · 初始 + 快照没就绪时 15s 追一次，避免盯着「—」干等
//     · 就绪后回落 60s 慢轮询；上一次未回来的拍自动跳过（usePollRefresh in-flight 守卫）
//
// 返回值与三个旧组件原本的 state 口径一致：
//   data   上一次成功的快照；err 时保留旧值，不清空，不显示假 0
//   err    本轮请求失败（保留旧数据渲染）
//   loading account 尚未就绪且未报错
export function useReplayPositions() {
  const [data, setData] = useState<ReplayPositions | null>(null)
  const [err, setErr] = useState(false)
  const readyRef = useRef(false)

  const load = useCallback(async () => {
    try {
      const d = await fetchReplayPositions()
      setData(d)
      setErr(false)
      if (d?.account?.current_equity_usdt != null) readyRef.current = true
    } catch {
      setErr(true)
    }
  }, [])

  // 单份轮询：就绪前 15s 追快照，就绪后 60s 慢刷新。
  // usePollRefresh 的 in-flight 守卫会跳过未完成的拍，慢接口不会一轮叠一轮。
  usePollRefresh(
    () => {
      void load()
    },
    60_000,
    [load],
    true,
  )
  // 快照未就绪阶段用更密的 15s 追一次；就绪后由上面的 60s 兜底。
  usePollRefresh(
    () => {
      if (!readyRef.current) void load()
    },
    15_000,
    [load],
    true,
  )

  const loading = !err && (!data?.account || data.account.current_equity_usdt == null)

  return { data, err, loading }
}
