// v170 P0+：replay-panel UI 控件 hook
//
// 职责：
//   - 把所有交易 / 新闻 / 策略 的 UI 触发函数从 ReplayPanel 抽出来
//   - 单一来源：toggleTrack / toggleGate / toggleNewsMaster / toggleNewsSource
//   - changeStrategy / toggleTrading / changeDailyTarget / changeMaxAge
//   - changeMarketStructure / changeTp / changeScores / handleReset
//   - 调用方传入 trading/news 状态与 persistTrading/persistNews/reload

import { useCallback } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import type { NewsSourceKey, NewsSourceSettings, PaperTrack, PaperTradingSettings, ReplayResetResult } from '@/lib/replay-data'
import { resetPaperTrading, updatePaperTrading } from '@/lib/replay-data'
import { useToast } from '@/components/ui/toast'

type ReloadFn = () => Promise<void>

type UseReplayControlsArgs = {
  tradingSettings: PaperTradingSettings | null
  tracks: PaperTrack[]
  newsSettings: NewsSourceSettings | null
  setTradingSettings: Dispatch<SetStateAction<PaperTradingSettings | null>>
  setTracks: Dispatch<SetStateAction<PaperTrack[]>>
  setNewsSettings: Dispatch<SetStateAction<NewsSourceSettings | null>>
  setError: Dispatch<SetStateAction<string | null>>
  setResetting: Dispatch<SetStateAction<boolean>>
  setResetResult: Dispatch<SetStateAction<ReplayResetResult | null>>
  persistTrading: (next: { is_running: boolean; tracks: PaperTrack[]; gate_enabled: boolean }) => Promise<void>
  persistNews: (
    body: Parameters<typeof import('@/lib/replay-data').updateNewsSourceSettings>[0],
    optimistic: NewsSourceSettings,
  ) => Promise<void>
  reload: ReloadFn
}

export function useReplayControls({
  tradingSettings,
  tracks,
  newsSettings,
  setTradingSettings,
  setTracks,
  setNewsSettings,
  setError,
  setResetting,
  setResetResult,
  persistTrading,
  persistNews,
  reload,
}: UseReplayControlsArgs) {
  // v302+ P0: 替代两段连续 window.confirm — 改用 toast 异步确认
  const { toast } = useToast()
  const toggleTrack = useCallback(
    (track: PaperTrack) => {
      const next = tracks.includes(track)
        ? tracks.filter((item) => item !== track)
        : [...tracks, track]
      if (next.length === 0 && tradingSettings?.is_running) {
        setError('运行中至少保留一个投资赛道')
        return
      }
      setTracks(next)
      if (!tradingSettings) return
      void persistTrading({
        is_running: tradingSettings.is_running,
        tracks: next,
        gate_enabled: tradingSettings.gate_enabled ?? true,
      })
    },
    [tracks, tradingSettings, setError, setTracks, persistTrading],
  )

  const toggleGate = useCallback(() => {
    if (!tradingSettings) return
    const nextGate = !(tradingSettings.gate_enabled ?? true)
    setTradingSettings({ ...tradingSettings, gate_enabled: nextGate })
    void persistTrading({
      is_running: tradingSettings.is_running,
      tracks,
      gate_enabled: nextGate,
    })
  }, [tradingSettings, tracks, setTradingSettings, persistTrading])

  const toggleNewsMaster = useCallback(() => {
    if (!newsSettings) return
    void persistNews(
      { enabled: !newsSettings.enabled },
      { ...newsSettings, enabled: !newsSettings.enabled },
    )
  }, [newsSettings, persistNews])

  const toggleNewsSource = useCallback(
    (key: NewsSourceKey) => {
      if (!newsSettings) return
      const nextSources = { ...newsSettings.sources, [key]: !newsSettings.sources[key] }
      void persistNews(
        { sources: { [key]: nextSources[key] } },
        { ...newsSettings, sources: nextSources },
      )
    },
    [newsSettings, persistNews],
  )

  const changeDailyTarget = useCallback(
    (value: number) => {
      if (!newsSettings) return
      const daily_target = Math.max(1, Math.round(value))
      void persistNews(
        { daily_target },
        {
          ...newsSettings,
          daily_target,
          remaining: Math.max(0, daily_target - newsSettings.today_count),
        },
      )
    },
    [newsSettings, persistNews],
  )

  // 新闻时效门槛（秒）：保存后引擎 ≤5s 内按新值过滤（BUY/SELL 通用）
  const changeMaxAge = useCallback(
    async (seconds: number) => {
      if (!tradingSettings) return
      const v = Math.min(86400, Math.max(30, Math.round(seconds) || 2700))
      try {
        const saved = await updatePaperTrading({
          is_running: tradingSettings.is_running,
          tracks,
          news_trade_max_age_seconds: v,
        })
        setTradingSettings(saved)
        setTracks(saved.tracks)
        setError(null)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '新闻时效门槛更新失败')
      }
    },
    [tradingSettings, tracks, setTradingSettings, setTracks, setError],
  )

  // 盘面结构分析开关：开=AI 分析附带多周期结构判断；关=纯筹码情绪（引擎 ≤5s 生效）
  const changeMarketStructure = useCallback(
    async (enabled: boolean) => {
      if (!tradingSettings) return
      try {
        const saved = await updatePaperTrading({
          is_running: tradingSettings.is_running,
          tracks,
          market_structure_enabled: enabled,
        })
        setTradingSettings(saved)
        setTracks(saved.tracks)
        setError(null)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '盘面结构开关更新失败')
      }
    },
    [tradingSettings, tracks, setTradingSettings, setTracks, setError],
  )

  // 分级止盈参数：保存后引擎 ≤5s 生效（目标价=entry±系数×历史MFE；到位按比例部分平仓）
  const changeTp = useCallback(
    async (
      patch: Partial<
        Pick<PaperTradingSettings, 'tp_enabled' | 'tp1_mult' | 'tp2_mult' | 'tp1_ratio' | 'tp2_ratio'>
      >,
    ) => {
      if (!tradingSettings) return
      try {
        const saved = await updatePaperTrading({
          is_running: tradingSettings.is_running,
          tracks,
          ...patch,
        })
        setTradingSettings(saved)
        setTracks(saved.tracks)
        setError(null)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '止盈参数更新失败')
      }
    },
    [tradingSettings, tracks, setTradingSettings, setTracks, setError],
  )

  // 买卖方向分数门槛（BUY 加严 / SELL 宽松）：保存后引擎 ≤5s 生效
  const changeScores = useCallback(
    async (patch: Partial<Pick<PaperTradingSettings, 'buy_min_score' | 'sell_min_score'>>) => {
      if (!tradingSettings) return
      try {
        const saved = await updatePaperTrading({
          is_running: tradingSettings.is_running,
          tracks,
          ...patch,
        })
        setTradingSettings(saved)
        setTracks(saved.tracks)
        setError(null)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '买卖门槛更新失败')
      }
    },
    [tradingSettings, tracks, setTradingSettings, setTracks, setError],
  )

  const toggleTrading = useCallback(() => {
    if (!tradingSettings?.is_running && tracks.length === 0) {
      setError('请先选择至少一个投资赛道')
      return
    }
    void persistTrading({
      is_running: !tradingSettings?.is_running,
      tracks,
      gate_enabled: tradingSettings?.gate_enabled ?? true,
    }).then(() => reload())
  }, [tradingSettings, tracks, setError, persistTrading, reload])

  const changeStrategy = useCallback(
    async (id: number) => {
      try {
        const { activateStrategy } = await import('@/lib/strategy-data')
        await activateStrategy(id)
        await reload()
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '策略切换失败')
      }
    },
    [setError, reload],
  )

  const handleReset = useCallback(async () => {
    // v302+ P0: 用 toast.confirm 串联两段确认,避免连续阻塞
    const first = await new Promise<boolean>((resolve) => {
      toast.confirm(
        '重置模拟盘将（彻底归零）：\n' +
          '1. 停止模拟操盘\n' +
          '2. 自动备份全部历史数据到 backend/backups/\n' +
          '3. 彻底删除全部交易历史与历史新闻（信号/持仓/结算/反思/归因/新闻），统计与界面从 0 开始\n' +
          '4. 复盘经验保留\n\n' +
          '确定继续吗？',
        () => resolve(true),
        () => resolve(false),
      )
    })
    if (!first) return
    const second = await new Promise<boolean>((resolve) => {
      toast.confirm(
        '再次确认（不可撤销）：\n' +
          '· 全部交易历史与历史新闻将被彻底删除，统计从 0 开始\n' +
          '· 界面不再出现重置前的旧新闻\n' +
          '· 复盘经验保留，历史数据仅能通过 backend/backups/ 的备份文件查阅\n\n' +
          '继续重置？',
        () => resolve(true),
        () => resolve(false),
      )
    })
    if (!second) return
    setResetting(true)
    try {
      const result = await resetPaperTrading()
      setResetResult(result)
      await reload()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '模拟盘重置失败')
    } finally {
      setResetting(false)
    }
  }, [setResetting, setResetResult, setError, reload, toast])

  return {
    toggleTrack,
    toggleGate,
    toggleNewsMaster,
    toggleNewsSource,
    changeDailyTarget,
    changeMaxAge,
    changeMarketStructure,
    changeTp,
    changeScores,
    toggleTrading,
    changeStrategy,
    handleReset,
  }
}