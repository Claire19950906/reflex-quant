'use client'

import { useRef, useState } from 'react'
import type { NewsSourceSettings, PaperTrack, PaperTradingSettings, ReplayPositions, ReplayResetResult, SimSignal } from '@/lib/replay-data'
import { backupDownloadUrl, fmtPct, fmtPrice, fmtTime } from '@/lib/replay-data'
import { API_BASE } from '@/lib/api'
import { activateStrategy, fetchStrategies, type Strategy } from '@/lib/strategy-data'
import { ReplayStats } from './replay-stats'
import { ReplayList } from './replay-list'
import { KlineChart } from './kline-chart'
import { SignalDetail } from './signal-detail'

import { llmShortModel, TRACKS, NEWS_SOURCES } from './replay-constants'
import { useBackupManager } from './use-backup-manager'
import { useReplayPersistence } from './use-replay-persistence'
import { useReplayDataLoader } from './use-replay-data-loader'
import { useReplayControls } from './use-replay-controls'

// v202: ReflectionImprovePanel 重新迁回 /replay。
// 该面板改的是 paper-trading 参数（买/卖门槛、止盈、新闻时效等），
// 模拟盘专属。与 /review 反思系统里的 AnalysisImprovePanel（改因果图谱）
// 严格分开，避免职责重叠 / 在反思系统混为一谈。
import { ReflectionImprovePanel } from './reflection-improve-panel'
// v232.6 P1: 反思 -> policy 闭环 - 候选补丁面板 (闭环仪表盘)
import PolicyPatchesPanel from './policy-patches-panel'
// v226: Fed 利率预期 + 跨资产异常两个新面板
import { FedRatePanel } from './fed-rate-panel'
import { CrossAssetAnomalyPanel } from './cross-asset-anomaly-panel'



export function ReplayPanel() {
  const [signals, setSignals] = useState<SimSignal[]>([])
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [currentStrategyId, setCurrentStrategyId] = useState<number | null>(null)
  const [currentStrategyName, setCurrentStrategyName] = useState('加载中')
  const [currentVersion, setCurrentVersion] = useState<number | null>(null)
  const [account, setAccount] = useState<ReplayPositions['account'] | null>(null)
  const [tradingSettings, setTradingSettings] = useState<PaperTradingSettings | null>(null)
  const [market, setMarket] = useState<Pick<ReplayPositions, 'pairs' | 'market_status' | 'market_sources'> | null>(null)
  const [strategyFeedback, setStrategyFeedback] = useState<ReplayPositions['strategy_feedback'] | null>(null)
  const [llmPerformance, setLlmPerformance] = useState<ReplayPositions['llm_performance'] | null>(null)
  const [quickSim, setQuickSim] = useState<ReplayPositions['quick_sim'] | null>(null)
  const [tracks, setTracks] = useState<PaperTrack[]>([])
  const [newsSettings, setNewsSettings] = useState<NewsSourceSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [resetResult, setResetResult] = useState<ReplayResetResult | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [loading, setLoading] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [selected, setSelected] = useState<SimSignal | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [showStats, setShowStats] = useState(false)
  const [showParamHelp, setShowParamHelp] = useState(false)

  // 备份目录信息（路径 + 最近备份文件），供"打开备份文件夹"按钮使用
  const { backupInfo, openBackupDir } = useBackupManager()

  // reload 的反向引用：persistence hook 需要 reload() 回滚，但 reload 由
  // useReplayDataLoader 生成，必须在 persistence 之后才能调用，
  // 所以用 ref 持有避免循环依赖。
  const reloadRef = useRef<(() => Promise<void>) | null>(null)

  // 模拟操盘 + 新闻设置 的持久化层（带版本号锁 + changingTrading/changingNews 标志）
  const { persistTrading, persistNews, changingTrading, changingNews, tradingLock, newsLock } =
    useReplayPersistence({
      reloadRef,
      setTradingSettings,
      setTracks,
      setNewsSettings,
      setError,
    })

  // 数据加载层（reload + 首次拉取 + 10s 轮询）
  const { reload } = useReplayDataLoader({
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
  })

  // 把 reload 暴露给 persistence（用于失败回滚）
  reloadRef.current = reload

  // UI 控件层（所有 toggle/change 函数 + handleReset）
  const {
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
  } = useReplayControls({
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
  })

  const tracking = signals.filter((signal) => !signal.settled).length
  const livePnl = signals
    .filter((signal) => !signal.settled)
    .reduce((sum, signal) => sum + (signal.current_pnl_usdt ?? 0), 0)
  const gateOn = tradingSettings?.gate_enabled ?? true
  const newsOn = newsSettings?.enabled !== false
  // 快速评测持仓时长（分钟）：取后端下发的 quick_sim.horizon_minutes，拿不到时退回 config.py 默认值 20
  const qsHorizon = quickSim?.horizon_minutes ?? 20

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-hidden">
      <div className="surface-card shrink-0 space-y-2 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-[160px]">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">当前自动模拟策略</div>
            <div className="mt-0.5 text-sm text-foreground">{currentStrategyName} · v{currentVersion ?? '—'}</div>
          </div>
          {/* v180 P0+：数据通道统一口径 §6 — 标明源表 + 时区 */}
          <span className="rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[9px] text-primary">
            数据 = [实时] paper_trading 实时快照 · 北京时区
          </span>
          <select
            value={currentStrategyId ?? ''}
            disabled={tradingSettings?.is_running}
            onChange={(event) => void changeStrategy(Number(event.target.value))}
            className="rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground disabled:cursor-not-allowed disabled:opacity-60"
          >
            {strategies.map((strategy) => (
              <option key={strategy.id} value={strategy.id}>{strategy.name} · v{strategy.latest_version?.version ?? 0}</option>
            ))}
          </select>
          <div className="flex items-center gap-1">
            {TRACKS.map(([track, label]) => (
              <button
                key={track}
                type="button"
                disabled={changingTrading}
                onClick={() => toggleTrack(track)}
                className={`rounded border px-2 py-1 font-mono text-[10px] disabled:cursor-wait disabled:opacity-70 ${tracks.includes(track) ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted-foreground'}`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={changingTrading}
            onClick={() => void toggleTrading()}
            className={`rounded border px-3 py-1.5 font-mono text-xs disabled:cursor-wait disabled:opacity-60 ${tradingSettings?.is_running ? 'border-short/50 bg-short/10 text-short' : 'border-primary bg-primary text-primary-foreground'}`}
          >
            {changingTrading ? '状态切换中…' : tradingSettings?.is_running ? '停止模拟操盘' : '开始模拟操盘'}
          </button>
          <button
            type="button"
            disabled={changingTrading || !tradingSettings}
            onClick={toggleGate}
            className={`rounded border px-3 py-1.5 font-mono text-xs disabled:cursor-wait disabled:opacity-60 ${gateOn ? 'border-hold/50 bg-hold/10 text-hold' : 'border-long/50 bg-long/10 text-long'}`}
          >
            {gateOn ? '闸门开启 · 严格' : '闸门关闭 · 宽松'}
          </button>
          <button
            type="button"
            disabled={!newsSettings || changingNews}
            onClick={toggleNewsMaster}
            className={`rounded border px-3 py-1.5 font-mono text-xs disabled:cursor-wait disabled:opacity-60 ${newsOn ? 'border-long/50 bg-long/10 text-long' : 'border-short/50 bg-short/10 text-short'}`}
          >
            {newsOn ? '新闻开启' : '新闻关闭'}
          </button>
          <div className="flex items-center gap-1">
            {NEWS_SOURCES.map(([key, label]) => (
              <button
                key={key}
                type="button"
                disabled={!newsSettings || changingNews || !newsOn}
                onClick={() => toggleNewsSource(key)}
                className={`rounded border px-2 py-1 font-mono text-[10px] disabled:opacity-50 ${newsSettings?.sources[key] ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted-foreground'}`}
              >
                {label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
            每日
            <input
              type="number"
              min={1}
              defaultValue={newsSettings?.daily_target ?? 300}
              key={newsSettings?.daily_target ?? 300}
              onBlur={(event) => void changeDailyTarget(Number(event.target.value) || 300)}
              className="w-16 rounded border border-border bg-card px-1 py-0.5 text-foreground"
            />
            <span>{newsSettings ? `${newsSettings.today_count}/${newsSettings.daily_target}` : '—'}</span>
          </label>
          <label className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground" title="新闻发布超过该时长后，BUY/SELL 信号不再建仓（引擎 ≤5s 生效）">
            新闻时效
            <input
              type="number"
              min={30}
              max={86400}
              defaultValue={tradingSettings?.news_trade_max_age_seconds ?? 2700}
              key={tradingSettings?.news_trade_max_age_seconds ?? 2700}
              onBlur={(event) => void changeMaxAge(Number(event.target.value) || 2700)}
              className="w-20 rounded border border-border bg-card px-1 py-0.5 text-foreground"
            />
            秒
          </label>
          <label className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground" title="盘面结构分析开关：开=AI 分析时附带多周期(1d/4h/1h/15m)盘面结构判断（价位/波动/周期共振/新闻×结构）；关=只做筹码情绪判断。即时新闻交易可关闭，避免结构判断影响追单（引擎 ≤5s 生效）">
            盘面结构
            <button
              type="button"
              onClick={() => void changeMarketStructure(tradingSettings?.market_structure_enabled === false)}
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${tradingSettings?.market_structure_enabled === false ? 'border-border text-muted-foreground' : 'border-long/50 bg-long/10 text-long'}`}
            >
              {tradingSettings?.market_structure_enabled === false ? '关' : '开'}
            </button>
          </label>
          <label className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground" title="分级止盈：目标价=入场±系数×历史MFE，到位按比例部分平仓（剩余仓位继续跟踪）">
            止盈
            <button
              type="button"
              onClick={() => void changeTp({ tp_enabled: tradingSettings?.tp_enabled === false })}
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${tradingSettings?.tp_enabled === false ? 'border-border text-muted-foreground' : 'border-long/50 bg-long/10 text-long'}`}
            >
              {tradingSettings?.tp_enabled === false ? '关' : '开'}
            </button>
            {tradingSettings?.tp_enabled !== false && (
              <>
                <input
                  type="number"
                  step="0.1"
                  min={0.1}
                  max={3}
                  defaultValue={tradingSettings?.tp1_mult ?? 0.6}
                  key={`m1-${tradingSettings?.tp1_mult ?? 0.6}`}
                  onBlur={(event) => void changeTp({ tp1_mult: Number(event.target.value) || 0.6 })}
                  className="w-12 rounded border border-border bg-card px-1 py-0.5 text-foreground"
                />
                <input
                  type="number"
                  step="0.1"
                  min={0.1}
                  max={3}
                  defaultValue={tradingSettings?.tp2_mult ?? 1.0}
                  key={`m2-${tradingSettings?.tp2_mult ?? 1.0}`}
                  onBlur={(event) => void changeTp({ tp2_mult: Number(event.target.value) || 1.0 })}
                  className="w-12 rounded border border-border bg-card px-1 py-0.5 text-foreground"
                />
                <input
                  type="number"
                  step="0.1"
                  min={0}
                  max={0.9}
                  defaultValue={tradingSettings?.tp1_ratio ?? 0.5}
                  key={`r1-${tradingSettings?.tp1_ratio ?? 0.5}`}
                  onBlur={(event) => void changeTp({ tp1_ratio: Number(event.target.value) || 0.5 })}
                  className="w-12 rounded border border-border bg-card px-1 py-0.5 text-foreground"
                />
                <input
                  type="number"
                  step="0.1"
                  min={0}
                  max={0.9}
                  defaultValue={tradingSettings?.tp2_ratio ?? 0.3}
                  key={`r2-${tradingSettings?.tp2_ratio ?? 0.3}`}
                  onBlur={(event) => void changeTp({ tp2_ratio: Number(event.target.value) || 0.3 })}
                  className="w-12 rounded border border-border bg-card px-1 py-0.5 text-foreground"
                />
              </>
            )}
          </label>
          <label className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground" title="买卖方向分数门槛：BUY 需更高分（做多胜率偏低），SELL 门槛较低（更倾向做空）">
            买/卖门槛
            <input
              type="number"
              step="0.05"
              min={0.1}
              max={0.9}
              defaultValue={tradingSettings?.buy_min_score ?? 0.4}
              key={`b-${tradingSettings?.buy_min_score ?? 0.4}`}
              onBlur={(event) => void changeScores({ buy_min_score: Number(event.target.value) || 0.4 })}
              className="w-12 rounded border border-border bg-card px-1 py-0.5 text-foreground"
            />
            /
            <input
              type="number"
              step="0.05"
              min={0.1}
              max={0.9}
              defaultValue={tradingSettings?.sell_min_score ?? 0.3}
              key={`s-${tradingSettings?.sell_min_score ?? 0.3}`}
              onBlur={(event) => void changeScores({ sell_min_score: Number(event.target.value) || 0.3 })}
              className="w-12 rounded border border-border bg-card px-1 py-0.5 text-foreground"
            />
          </label>
          <span className={`rounded border px-2 py-1 font-mono text-[10px] ${tradingSettings?.is_running ? 'border-long/40 bg-long/10 text-long' : 'border-border text-muted-foreground'}`}>
            {tradingSettings?.is_running ? 'AUTO PAPER ON' : 'AUTO PAPER OFF'}
          </span>
          <span className="font-mono text-xs text-muted-foreground">持仓 {tracking}</span>
          <span className="font-mono text-xs text-foreground">权益 {account?.current_equity_usdt?.toFixed(2) ?? '—'} USDT</span>
          <span className={`font-mono text-xs ${(account?.realized_pnl_usdt ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>已实现 {(account?.realized_pnl_usdt ?? 0) >= 0 ? '+' : ''}{account?.realized_pnl_usdt.toFixed(2) ?? '—'}</span>
          <span className={`font-mono text-xs ${livePnl >= 0 ? 'text-long' : 'text-short'}`}>浮盈亏 {account?.unrealized_pnl_usdt == null ? '待行情' : `${livePnl >= 0 ? '+' : ''}${livePnl.toFixed(2)} USDT`}</span>
          <span className="font-mono text-xs text-muted-foreground">可用 {account?.available_equity_usdt?.toFixed(2) ?? '—'} · 占用 {account?.used_margin_usdt.toFixed(2) ?? '—'}</span>
          <div className="ml-auto flex gap-2">
            <a href={`${API_BASE}/export/signals`} className="rounded border border-border px-2 py-1.5 text-xs text-foreground">下载八列数据</a>
            <button
              type="button"
              disabled={resetting}
              onClick={() => void handleReset()}
              className="rounded border border-short/40 bg-short/10 px-2 py-1.5 font-mono text-xs text-short disabled:cursor-wait disabled:opacity-60"
            >
              {resetting ? '重置中…' : '重置模拟盘'}
            </button>
            <a href="http://127.0.0.1:8501" target="_blank" rel="noreferrer" className="rounded border border-primary/40 bg-primary/10 px-2 py-1.5 text-xs text-primary">Streamlit :8501</a>
          </div>
        </div>
      </div>

      {/* 参数说明（新闻时效 / 止盈 / 买卖门槛） */}
      <button
        type="button"
        onClick={() => setShowParamHelp((v) => !v)}
        className="flex w-full items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1 font-mono text-[10px] text-muted-foreground transition hover:border-primary/40 hover:text-foreground"
      >
        <span>{showParamHelp ? '▾' : '▸'}</span>
        <span>{showParamHelp ? '收起参数说明' : '参数说明（新闻时效 / 止盈 / 买卖门槛 — 如何调）'}</span>
      </button>
      {showParamHelp && (
        <div className="thin-scroll grid shrink-0 grid-cols-1 gap-2 overflow-x-auto lg:grid-cols-3">
          {/* 新闻时效 */}
          <div className="rounded-md border border-border bg-card p-2.5">
            <div className="font-mono text-[10px] uppercase tracking-widest text-foreground">① 新闻时效（秒）</div>
            <div className="mt-1 space-y-1 text-[11px] leading-relaxed text-muted-foreground">
              <p><b className="text-foreground">含义：</b>新闻发布超过该时长后，这条新闻产生的 BUY/SELL 信号<b>不再建仓</b>（防追旧闻）。过期信号仍会分析入库统计，只是不交易。</p>
              <p><b className="text-foreground">默认：</b>2700 秒 = 45 分钟</p>
              <p><b className="text-foreground">怎么调：</b></p>
              <ul className="list-disc space-y-0.5 pl-4">
                <li>想只做"刚出炉"的新闻 → 调小，如 <b>900</b>（15 分钟）</li>
                <li>新闻源较慢 / 想放宽 → 调大，如 <b>5400</b>（90 分钟）</li>
              </ul>
              <p className="text-[10px]">引擎 ≤5 秒生效，失焦即保存。</p>
            </div>
          </div>

          {/* 止盈 */}
          <div className="rounded-md border border-border bg-card p-2.5">
            <div className="font-mono text-[10px] uppercase tracking-widest text-foreground">② 止盈（分级部分止盈）</div>
            <div className="mt-1 space-y-1 text-[11px] leading-relaxed text-muted-foreground">
              <p><b className="text-foreground">含义：</b>持仓有利方向到<b>目标价</b>时<b>分批止盈</b>：先落袋一部分，剩余仓位继续吃趋势尾巴。</p>
              <p><b className="text-foreground">目标价公式：</b>目标价 = 入场价 ×（1 ± 系数 × 历史平均 MFE%）<br />
                <span className="text-[10px]">MFE = 同品种×同事件强度已结算交易的平均最大有利波动（样本 ≥5 才启用）</span></p>
              <ul className="list-disc space-y-0.5 pl-4">
                <li><b>系数1</b>（默认 0.6）：目标1 = 入场 ±0.6×MFE → 触及时<b>止盈 50%</b></li>
                <li><b>系数2</b>（默认 1.0）：目标2 = 入场 ±1.0×MFE → <b>再止盈 30%</b>（累计 80%）</li>
                <li><b>比例1/2</b>（默认 0.5 / 0.3）：两档止盈比例，剩余 20% 交移动止损/到期</li>
              </ul>
              <p><b className="text-foreground">例子：</b>XAU 历史强催化平均 MFE 1.2%，入场 4590 做空 → 目标1 ≈ 4557（平 50%）、目标2 ≈ 4535（再平 30%）</p>
              <p><b className="text-foreground">怎么调：</b>想更快落袋 → 系数调小（0.4）；想拿更远 → 调大（1.2）；想平更多 → 比例调大（合计 ≤95%）。不想用止盈 → 点"关"。</p>
            </div>
          </div>

          {/* 买卖门槛 */}
          <div className="rounded-md border border-border bg-card p-2.5">
            <div className="font-mono text-[10px] uppercase tracking-widest text-foreground">③ 买/卖门槛（信号分数）</div>
            <div className="mt-1 space-y-1 text-[11px] leading-relaxed text-muted-foreground">
              <p><b className="text-foreground">含义：</b>AI 信号分数（多证据闸门加权分）达到多少才开仓。买/卖不对称是有意设计：<b>做多历史胜率 53% &lt; 做空 56%</b>，所以买入要求更高分。</p>
              <ul className="list-disc space-y-0.5 pl-4">
                <li><b>买</b>（默认 0.4）：做多信号分数 ≥0.4 才买入（更严格）</li>
                <li><b>卖</b>（默认 0.3）：做空信号分数 ≤-0.3 才卖出（更积极）</li>
              </ul>
              <p><b className="text-foreground">怎么调：</b></p>
              <ul className="list-disc space-y-0.5 pl-4">
                <li>想更保守（少交易、信号更精）→ 调大，如买 0.5 / 卖 0.4</li>
                <li>想更活跃（多交易）→ 调小，如买 0.3 / 卖 0.25</li>
                <li>想让买卖一致 → 设成相同值（如 0.35 / 0.35）</li>
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* 反思改进计划（v202 起重新迁回 /replay）。
          改的是模拟盘参数（买/卖门槛、止盈、新闻时效等），
          与 /review 反思系统的 AnalysisImprovePanel（改因果图谱）严格分开，
          避免在反思界面混为一谈。 */}
      <ReflectionImprovePanel />

      {/* v226: Fed 利率预期 + 跨资产同步异常 — 两个新宏观监控面板 */}
      <div className="grid shrink-0 grid-cols-1 gap-2 lg:grid-cols-2">
        <FedRatePanel />
        <CrossAssetAnomalyPanel />
      </div>

      {/* v232.6 P1: 反思 -> policy 闭环候选补丁面板 */}
      <div className="shrink-0">
        <PolicyPatchesPanel />
      </div>

      {resetResult && (
        <div className="shrink-0 rounded border border-long/40 bg-long/10 px-3 py-1.5 text-left text-xs text-long">
          <div className="flex items-center gap-2">
            <span>✓ {resetResult.message}</span>
            <button
              type="button"
              onClick={() => setResetResult(null)}
              className="ml-auto rounded border border-long/40 px-1.5 py-0.5 font-mono text-[10px] text-long hover:bg-long/10"
            >
              关闭
            </button>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="font-mono text-[10px] text-muted-foreground">备份文件：</span>
            <a href={backupDownloadUrl(resetResult.backup.json)} className="font-mono text-[10px] text-long underline" download>下载 JSON 完整备份</a>
            <a href={backupDownloadUrl(resetResult.backup.excel)} className="font-mono text-[10px] text-long underline" download>下载 Excel 表格</a>
            <span className="font-mono text-[10px] text-muted-foreground">完整路径：{resetResult.backup.json}</span>
          </div>
        </div>
      )}

      {error && (
        <button type="button" onClick={() => void reload()} className="shrink-0 rounded border border-hold/40 bg-hold/10 px-3 py-1.5 text-left text-xs text-hold">
          后端暂时不可用：{error}。点击重试，现有数据已保留。
        </button>
      )}

      {/* 运行状态与统计（默认收起，三列占满整页；展开时最多 40% 视口） */}
      <div className="shrink-0">
        <button
          type="button"
          onClick={() => setShowStats((value) => !value)}
          className="flex w-full items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1 font-mono text-[10px] text-muted-foreground transition hover:border-primary/40 hover:text-foreground"
        >
          <span>{showStats ? '▾' : '▸'}</span>
          <span>{showStats ? '收起运行状态与统计' : '运行状态与统计（点开查看）'}</span>
          {tradingSettings?.is_running && (
            <span className="ml-auto flex items-center gap-1">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-long" />
              模拟盘运行中
            </span>
          )}
        </button>
      </div>
      {showStats && (
        <div className="thin-scroll min-h-0 max-h-[40vh] shrink-0 overflow-y-auto pr-0.5">
          <div className="flex flex-col gap-2">
            <div className="thin-scroll grid shrink-0 grid-cols-1 gap-2 overflow-x-auto xl:grid-cols-4">
              <div className="surface-card min-w-[220px] p-2.5">
                <div className="flex items-center justify-between">
                  <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">实时交易对</h3>
                  <span className={`font-mono text-[10px] ${market?.market_status === 'ok' ? 'text-long' : 'text-hold'}`}>{market?.market_status?.toUpperCase() ?? 'LOADING'}</span>
                </div>
                <div className="thin-scroll mt-2 max-h-24 space-y-1 overflow-y-auto">
                  {(market?.pairs ?? []).length === 0 ? (
                    <div className="font-mono text-[10px] text-muted-foreground">未运行：启动模拟盘后显示所选赛道的交易对</div>
                  ) : (
                    (market?.pairs ?? []).map((pair) => (
                      <div key={pair.symbol} className="flex items-center justify-between rounded border border-border bg-secondary/40 px-2 py-1 font-mono text-[10px]">
                        <span className="text-foreground">{pair.symbol}</span>
                        <span>{pair.price == null ? '待行情' : pair.price.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                        <span className={pair.status === 'LIVE' ? 'text-long' : 'text-short'}>{pair.status}</span>
                      </div>
                    ))
                  )}
                </div>
                <div className="mt-1 font-mono text-[9px] leading-tight text-muted-foreground/70">所选赛道交易对的实时价格与行情状态（LIVE=有行情，UNAVAILABLE=暂无）</div>
              </div>
              <div className="surface-card min-w-[220px] p-2.5">
                <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">策略实时反馈</h3>
                <div className="mt-1 font-mono text-xs text-foreground">{strategyFeedback?.message ?? '正在初始化运行状态'}</div>
                <div className="mt-1 flex gap-3 font-mono text-[10px] text-muted-foreground">
                  <span>检测 {strategyFeedback?.evaluated ?? 0}</span>
                  <span className="text-long">通过 {strategyFeedback?.passed ?? 0}</span>
                  <span className="text-short">拒绝 {strategyFeedback?.rejected ?? 0}</span>
                </div>
                <div className="mt-0.5 font-mono text-[9px] leading-tight text-muted-foreground/70">检测=运行以来累计收到的信号数 · 通过=闸门放行累计 · 拒绝=被拦截累计；未运行显示"未启动"</div>
                <div className="mt-1 rounded border border-border/50 bg-secondary/20 px-1.5 py-1 font-mono text-[9px] leading-relaxed text-muted-foreground/80">
                  通过判定（满足其一）：① AI 置信度 ≥ 55（evidence_confidence）
                  <br />② 闸门裁决"证据充分，允许输出方向性结论"（trade_gate_reason）
                  <br />被拒原因：置信度不足 / LLM 方向与分数符号矛盾 / 闸门方向与信号方向不一致
                  <br />注意：通过 ≠ 一定建仓——还需真实行情价可取才会自动建仓
                </div>
              </div>
              <div className="surface-card min-w-[220px] p-2.5">
                <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">LLM 准确与盈利</h3>
                {llmPerformance?.settled ? (
                  <div className="mt-1 grid grid-cols-2 gap-1 font-mono text-[11px]">
                    <span title="该模型自己（agent_model_id）的真实持仓战绩">
                      <b>{llmShortModel(llmPerformance.model_id)}</b> 准确率{' '}
                      <b>{llmPerformance.accuracy_pct == null ? '样本不足' : `${llmPerformance.accuracy_pct.toFixed(1)}%`}</b>
                    </span>
                    <span>结算 <b>{llmPerformance.settled}</b></span>
                    {llmPerformance.all_models && llmPerformance.all_models.settled > 0 && (
                      <>
                        <span className="col-span-2 mt-1 border-t border-border/40 pt-1 text-[9px] text-muted-foreground/70">
                          全部模型累计：{llmPerformance.all_models.settled} 笔
                          {llmPerformance.all_models.accuracy_pct != null
                            ? ` · 准确率 ${llmPerformance.all_models.accuracy_pct.toFixed(1)}%`
                            : ' · 暂无胜负样本'}
                        </span>
                      </>
                    )}
                  </div>
                ) : (
                  <div className="mt-1 font-mono text-[11px] text-muted-foreground">暂无结算数据</div>
                )}
                <div className="mt-1 rounded border border-border/50 bg-secondary/20 px-1.5 py-1 font-mono text-[9px] leading-relaxed text-muted-foreground/80">
                  <div>
                    · <b className="text-foreground">上方准确率</b> 只算「{llmShortModel(llmPerformance?.model_id)}」这一个模型自己的真实持仓
                    ——模拟盘<b className="text-foreground">运行时固定为该次启动配置的模型</b>，未运行时跟随你当前选择的模型
                  </div>
                  <div>
                    · <b className="text-foreground">下方全部模型</b> = 模型池所有模型 + 历史记录加总
                  </div>
                  <div>
                    · 每条新闻由模型池（13 槽）<b className="text-foreground">随机分片</b>给其中一个模型分析，只有分到它的新闻才计入它的统计——所以上方样本通常比下方少
                  </div>
                </div>
              </div>
              <div className="surface-card min-w-[220px] p-2.5">
                <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">快速评测</h3>
                {quickSim?.overall.settled ? (
                  <div className="mt-1 grid grid-cols-2 gap-1 font-mono text-[11px]">
                    <span>胜率 <b>{quickSim.overall.winrate_pct == null ? '—' : `${quickSim.overall.winrate_pct.toFixed(1)}%`}</b></span>
                    <span>结算 <b>{quickSim.overall.settled}</b></span>
                  </div>
                ) : (
                  <div className="mt-1 font-mono text-[11px] text-muted-foreground">暂无结算数据</div>
                )}
                <div className="mt-0.5 font-mono text-[9px] leading-tight text-muted-foreground/70">胜率=全部信号（不过闸门）{qsHorizon} 分钟结算的胜负比 · 结算=已结算笔数，看模型判断力</div>
                {quickSim?.by_model && quickSim.by_model.length > 1 && (
                  <div className="mt-1 border-t border-border/40 pt-1">
                    <div className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground/60">按模型（{qsHorizon} 分钟胜率）</div>
                    <div className="mt-0.5 space-y-0.5">
                      {quickSim.by_model.slice(0, 6).map((m) => (
                        <div key={m.model} className="flex items-center gap-1.5 font-mono text-[9px]">
                          <span className="min-w-0 flex-1 truncate text-muted-foreground" title={m.model}>{m.model}</span>
                          <span className={m.winrate_pct != null && m.winrate_pct >= 50 ? 'text-long' : 'text-short'}>
                            {m.winrate_pct == null ? '—' : `${m.winrate_pct.toFixed(0)}%`}
                          </span>
                          <span className="w-10 shrink-0 text-right text-muted-foreground/60">{m.settled} 笔</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>

            <div className="shrink-0"><ReplayStats refreshKey={refreshKey} /></div>

            {/* 两种统计口径说明：快速评测（全信号快评）vs 全部交易统计（真实模拟持仓） */}
            <div className="rounded-md border border-border bg-card p-3">
              <div className="flex items-center justify-between">
                <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                  两种统计口径说明
                </div>
                <span className="font-mono text-[9px] text-muted-foreground/60">数字对不上？先看这里</span>
              </div>
              <div className="mt-2 grid grid-cols-1 gap-2 lg:grid-cols-2">
                <div className="rounded border border-border bg-secondary/30 p-2.5">
                  <div className="font-mono text-[10px] font-bold text-foreground">快速评测</div>
                  <div className="mt-1 space-y-0.5 font-mono text-[9px] leading-relaxed text-muted-foreground">
                    <div>· 数据源：全部 BUY/SELL 信号（quick_sim_trades 表）</div>
                    <div>· 准入：不过闸门，闸门结论只作分组标记</div>
                    <div>· 周期：{qsHorizon} 分钟（建仓时写入，调整后新仓生效），到期按真实行情结算</div>
                    <div>· 测什么：模型判断力——大量信号快速检验胜率</div>
                    <div>· 不参与真实持仓，不写 ai_decisions</div>
                  </div>
                </div>
                <div className="rounded border border-border bg-secondary/30 p-2.5">
                  <div className="font-mono text-[10px] font-bold text-foreground">全部交易统计</div>
                  <div className="mt-1 space-y-0.5 font-mono text-[9px] leading-relaxed text-muted-foreground">
                    <div>· 数据源：ai_decisions 已结算的真实模拟持仓</div>
                    <div>· 准入：通过多证据闸门 + 策略闸门才建仓</div>
                    <div>· 周期：按 expected_horizon（intraday / 1-3d / 1w+）</div>
                    <div>· 测什么：模拟盘真实战绩（胜率 / 盈亏 / 过程质量）</div>
                    <div>· forward_pnl 已按分级止盈加权</div>
                  </div>
                </div>
              </div>
              <div className="mt-2 font-mono text-[9px] leading-relaxed text-muted-foreground/60">
                简单说：快速评测≈"所有信号都试一下 {qsHorizon} 分钟"（检验 AI 判断力）；全部交易统计≈"闸门同意才真建仓"的模拟盘战绩。
                两者口径不同，数字不一致是正常的——快速评测的数字通常更大、更快增长。
              </div>
            </div>

            {/* 重置备份：路径 + 打开文件夹 + 最近备份文件 */}
            <div className="rounded-md border border-border bg-card p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">重置备份</span>
                <button
                  type="button"
                  onClick={() => void openBackupDir()}
                  title="在服务器本机打开 backend/backups/ 文件夹（仅本机/同机部署有效）"
                  className="rounded border border-primary/50 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary hover:bg-primary/20"
                >
                  📁 打开备份文件夹
                </button>
                <span className="ml-auto truncate font-mono text-[9px] text-muted-foreground/60" title={backupInfo?.backup_dir}>
                  {backupInfo?.backup_dir ?? 'backend/backups/'}
                </span>
              </div>
              {backupInfo && backupInfo.files.length > 0 ? (
                <div className="thin-scroll mt-1.5 max-h-24 space-y-0.5 overflow-y-auto pr-0.5">
                  <div className="font-mono text-[9px] text-muted-foreground/60">最近备份（点击下载）：</div>
                  {backupInfo.files.slice(0, 10).map((f) => (
                    <a
                      key={f.name}
                      href={`${API_BASE}/backups/${encodeURIComponent(f.name)}`}
                      download
                      className="flex items-center gap-2 font-mono text-[9px] text-long/90 underline hover:text-long"
                    >
                      <span className="min-w-0 flex-1 truncate">{f.name}</span>
                      <span className="shrink-0 text-muted-foreground/60">{(f.size / 1024).toFixed(1)} KB</span>
                    </a>
                  ))}
                </div>
              ) : (
                <div className="mt-1.5 font-mono text-[9px] text-muted-foreground/60">
                  暂无备份文件——点"重置模拟盘"时自动备份到此目录
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 三列固定区域：持仓记录 / K线 / 实时跟踪 各自独立滚动，互不带动 */}
      <div className="min-h-0 flex-1 overflow-hidden">
        <div className="grid h-full min-h-0 grid-cols-1 gap-2 overflow-y-auto xl:grid-cols-[minmax(260px,300px)_minmax(0,1fr)_minmax(300px,360px)] xl:overflow-hidden">
          <div className="surface-card flex min-h-0 flex-col overflow-hidden p-2.5">
            <ReplayList signals={signals} loading={loading} selectedId={selected?.id ?? null} onSelect={setSelected} onReload={reload} />
          </div>

          <div className="flex min-h-0 flex-col overflow-hidden">
              <div className="mb-1 flex items-center gap-2">
                <h3 className="font-mono text-xs uppercase tracking-widest text-muted-foreground">K 线 · 实时行情</h3>
                <label className="ml-auto flex cursor-pointer items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} className="h-3 w-3 accent-primary" />
                  <span>每 10s 实时刷新</span>
                </label>
              </div>
              {/* 进出场位置信息条（TradingView 图上无法画标记，用信息条清晰显示） */}
              {selected && (
                <div className="mb-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded-md border border-border bg-card px-2 py-1 font-mono text-[10px] text-muted-foreground">
                  <span className="font-semibold text-foreground">
                    {selected.asset} · <span className={selected.action === 'BUY' ? 'text-long' : selected.action === 'SELL' ? 'text-short' : ''}>{selected.action}</span>
                  </span>
                  <span>
                    入场 <b className="text-foreground">{fmtPrice(selected.entry_price, selected.asset)}</b>
                    {selected.entry_time ? ` @ ${fmtTime(selected.entry_time)}` : ''}
                  </span>
                  <span>
                    出场{' '}
                    {selected.settled ? (
                      selected.partial_exit_price != null && selected.partial_exit_price > 0 ? (
                        <span title={`分级止盈：已平 ${(selected.partial_exit_ratio ?? 0) * 100}% @ ${fmtPrice(selected.partial_exit_price, selected.asset)}，剩余 ${(1 - (selected.partial_exit_ratio ?? 0)) * 100}% @ ${fmtPrice(selected.exit_price, selected.asset)}`}>
                          <b className="text-hold">{fmtPrice(selected.partial_exit_price, selected.asset)}</b>
                          <span className="text-muted-foreground"> ({(selected.partial_exit_ratio ?? 0) * 100}%)</span>
                          {' + '}
                          <b className="text-foreground">{fmtPrice(selected.exit_price, selected.asset)}</b>
                        </span>
                      ) : (
                        <b className="text-foreground">{fmtPrice(selected.exit_price, selected.asset)}</b>
                      )
                    ) : '持仓中'}
                  </span>
                  {selected.settled ? (
                    <span className={(selected.forward_pnl ?? 0) >= 0 ? 'text-long' : 'text-short'}>
                      PnL {fmtPct(selected.forward_pnl)}
                    </span>
                  ) : null}
                  {selected.is_correct && (
                    <span className={selected.is_correct === 'WIN' ? 'text-long' : selected.is_correct === 'LOSS' ? 'text-short' : ''}>
                      {selected.is_correct}
                    </span>
                  )}
                </div>
              )}
              <div className="min-h-0 flex-1">
                <KlineChart
                  signalId={selected?.id ?? null}
                  asset={selected?.asset ?? ''}
                  action={selected?.action ?? 'HOLD'}
                  entryPrice={selected?.entry_price ?? null}
                  exitPrice={selected?.exit_price ?? null}
                  defaultSubIndicator="macd"
                />
              </div>
            </div>

            <div className="min-h-0 overflow-hidden"><SignalDetail signal={selected} /></div>
        </div>
      </div>
    </div>
  )
}
