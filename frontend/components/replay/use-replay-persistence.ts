// v170 P0+：replay-panel 持久化层 hook
//
// 职责：
//   - 包装 updatePaperTrading / updateNewsSourceSettings 调用 + 版本号锁
//   - 暴露 changingTrading / changingNews 状态（按钮 disable 用）
//   - 调用方负责传入 reload + 各个 setter
//
// 设计要点：
//   - 用 ref 自增计数做"过期回包"防护（tradingLock / newsLock），
//     避免用户连点多个切换时旧响应覆盖新状态。
//   - 失败时 trading 路径会自动 reload() 回滚；news 路径只显示错误。
//   - reload 用 ref 持有，允许 hook 调用顺序晚于 useReplayDataLoader。

import { useCallback, useRef, useState } from 'react'
import type { Dispatch, MutableRefObject, SetStateAction } from 'react'
import type { NewsSourceSettings, PaperTrack, PaperTradingSettings } from '@/lib/replay-data'
import { updateNewsSourceSettings, updatePaperTrading } from '@/lib/replay-data'

type ReloadFn = () => Promise<void>

type UseReplayPersistenceArgs = {
  /**
   * 持有 reload 的 ref。persistence hook 需要 reload() 回滚，但 reload 由
   * useReplayDataLoader 生成且必须在 persistence 之后才能调用，
   * 所以用 ref 持有避免循环依赖。
   */
  reloadRef: MutableRefObject<ReloadFn | null>
  setTradingSettings: Dispatch<SetStateAction<PaperTradingSettings | null>>
  setTracks: Dispatch<SetStateAction<PaperTrack[]>>
  setNewsSettings: Dispatch<SetStateAction<NewsSourceSettings | null>>
  setError: Dispatch<SetStateAction<string | null>>
}

/**
 * 模拟盘 + 新闻设置 的持久化层。
 * 返回两个 persist 函数与 changingTrading/changingNews 标志位 + lock refs。
 */
export function useReplayPersistence({
  reloadRef,
  setTradingSettings,
  setTracks,
  setNewsSettings,
  setError,
}: UseReplayPersistenceArgs) {
  const tradingLock = useRef(0)
  const newsLock = useRef(0)
  const [changingTrading, setChangingTrading] = useState(false)
  const [changingNews, setChangingNews] = useState(false)

  const persistTrading = useCallback(
    async (next: { is_running: boolean; tracks: PaperTrack[]; gate_enabled: boolean }) => {
      tradingLock.current += 1
      const lock = tradingLock.current
      setChangingTrading(true)
      try {
        const saved = await updatePaperTrading(next)
        if (lock !== tradingLock.current) return
        setTradingSettings(saved)
        setTracks(saved.tracks)
        setError(null)
      } catch (cause) {
        if (lock === tradingLock.current) {
          tradingLock.current = 0
          setError(cause instanceof Error ? cause.message : '模拟操盘状态更新失败')
          await reloadRef.current?.()
        }
      } finally {
        if (lock === tradingLock.current) {
          tradingLock.current = 0
          setChangingTrading(false)
        }
      }
    },
    [reloadRef, setTradingSettings, setTracks, setError],
  )

  const persistNews = useCallback(
    async (
      body: Parameters<typeof updateNewsSourceSettings>[0],
      optimistic: NewsSourceSettings,
    ) => {
      newsLock.current += 1
      const lock = newsLock.current
      setChangingNews(true)
      setNewsSettings(optimistic)
      try {
        const saved = await updateNewsSourceSettings(body)
        if (lock !== newsLock.current) return
        setNewsSettings(saved)
        setError(null)
      } catch (cause) {
        if (lock === newsLock.current) {
          setError(cause instanceof Error ? cause.message : '新闻开关更新失败')
        }
      } finally {
        if (lock === newsLock.current) {
          newsLock.current = 0
          setChangingNews(false)
        }
      }
    },
    [setNewsSettings, setError],
  )

  return { persistTrading, persistNews, changingTrading, changingNews, tradingLock, newsLock }
}