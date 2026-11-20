// v170 P0+：replay-panel 数据加载 hook
//
// 职责：
//   - 封装 fetchReplayPositions + fetchStrategies 调用
//   - 把响应映射到 signals / strategies / settings / market 等十几个 useState
//   - 挂载时拉一次 + autoRefresh=true 时每 10s 轮询
//   - 与 useReplayPersistence 的 tradingLock / newsLock 协作：
//     持久化进行中时不覆盖本地状态，避免回滚竞态
//
// 失败：setError('模拟盘接口暂时不可用' / 原始 message)

import { useCallback, useEffect } from 'react'
import type { Dispatch, MutableRefObject as RTMutableRefObject, SetStateAction } from 'react'
import type {
  NewsSourceSettings,
  PaperTrack,
  PaperTradingSettings,
  ReplayPositions,
  SimSignal,
} from '@/lib/replay-data'
import { fetchReplayPositions } from '@/lib/replay-data'
import { fetchStrategies, type Strategy } from '@/lib/strategy-data'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'

type UseReplayDataLoaderArgs = {
  autoRefresh: boolean
  tradingLock: RTMutableRefObject<number>
  newsLock: RTMutableRefObject<number>

  setSignals: Dispatch<SetStateAction<SimSignal[]>>
  setCurrentStrategyId: Dispatch<SetStateAction<number | null>>
  setCurrentStrategyName: Dispatch<SetStateAction<string>>
  setCurrentVersion: Dispatch<SetStateAction<number | null>>
  setAccount: Dispatch<SetStateAction<ReplayPositions['account'] | null>>
  setTradingSettings: Dispatch<SetStateAction<PaperTradingSettings | null>>
  setTracks: Dispatch<SetStateAction<PaperTrack[]>>
  setMarket: Dispatch<
    SetStateAction<Pick<ReplayPositions, 'pairs' | 'market_status' | 'market_sources'> | null>
  >
  setStrategyFeedback: Dispatch<SetStateAction<ReplayPositions['strategy_feedback'] | null>>
  setLlmPerformance: Dispatch<SetStateAction<ReplayPositions['llm_performance'] | null>>
  setQuickSim: Dispatch<SetStateAction<ReplayPositions['quick_sim'] | null>>
  setNewsSettings: Dispatch<SetStateAction<NewsSourceSettings | null>>
  setError: Dispatch<SetStateAction<string | null>>
  setRefreshKey: Dispatch<SetStateAction<number>>
  setSelected: Dispatch<SetStateAction<SimSignal | null>>
  setLoading: Dispatch<SetStateAction<boolean>>
  setStrategies: Dispatch<SetStateAction<Strategy[]>>
}

/**
 * 拉一次 /replay/positions + 拉一次 /strategies；
 * 同时挂载定时轮询（autoRefresh=true 时每 10s）。
 * 返回 reload 给上层用（例如 persistTrading 失败回滚）。
 */
export function useReplayDataLoader(args: UseReplayDataLoaderArgs) {
  const {
    autoRefresh,
    tradingLock,
    newsLock,
    setSignals,
    setCurrentStrategyId,
    setCurrentStrategyName,
    setCurrentVersion,
    setAccount,
    setTradingSettings,
    setTracks,
    setMarket,
    setStrategyFeedback,
    setLlmPerformance,
    setQuickSim,
    setNewsSettings,
    setError,
    setRefreshKey,
    setSelected,
    setLoading,
    setStrategies,
  } = args

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchReplayPositions()
      setSignals(data.positions)
      setCurrentStrategyId(data.current_strategy.id)
      setCurrentStrategyName(data.current_strategy.name)
      setCurrentVersion(data.current_strategy.active_version.version)
      setAccount(data.account)
      if (tradingLock.current === 0) {
        setTradingSettings((prev) => ({
          ...data.trading_settings,
          gate_enabled: data.trading_settings.gate_enabled ?? prev?.gate_enabled ?? true,
        }))
        setTracks(data.trading_settings.tracks)
      }
      setMarket({
        pairs: data.pairs,
        market_status: data.market_status,
        market_sources: data.market_sources,
      })
      setStrategyFeedback(data.strategy_feedback)
      setLlmPerformance(data.llm_performance)
      setQuickSim(data.quick_sim ?? null)
      if (newsLock.current === 0) {
        if (data.news_settings) {
          setNewsSettings(data.news_settings)
        }
      }
      setError(null)
      setRefreshKey((value) => value + 1)
      setSelected((prev) => {
        if (!prev) return data.positions[0] ?? null
        return data.positions.find((item) => item.id === prev.id) ?? data.positions[0] ?? null
      })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模拟盘接口暂时不可用')
    } finally {
      setLoading(false)
    }
  }, [
    tradingLock,
    newsLock,
    setSignals,
    setCurrentStrategyId,
    setCurrentStrategyName,
    setCurrentVersion,
    setAccount,
    setTradingSettings,
    setTracks,
    setMarket,
    setStrategyFeedback,
    setLlmPerformance,
    setQuickSim,
    setNewsSettings,
    setError,
    setRefreshKey,
    setSelected,
    setLoading,
  ])

  // 初次拉取 + 策略列表
  useEffect(() => {
    void reload()
    void fetchStrategies().then(setStrategies).catch(() => setStrategies([]))
  }, [reload, setStrategies])

  // autoRefresh=true 时每 10s 轮询（3s 全量拉取会导致明显卡顿）
  // v302 P0+：手写 setInterval → usePollRefresh（统一 alive flag + cleanup）
  usePollRefresh(reload, 10_000, [reload], autoRefresh)

  return { reload }
}