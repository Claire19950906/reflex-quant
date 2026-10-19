'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'
import type { ReplayKlineBar, ReplayKlineMarker } from '@/lib/replay-data'
import { fetchLivePrice, fetchReplaySignalKline } from '@/lib/replay-data'
import { useIsDark } from '@/hooks/use-is-dark'
import {
  alignMarkerTime,
  CHART_THEME,
  shTickFormatter,
} from './kline-utils'
import {
  type MainIndicator,
  type SubIndicator,
  useKlineIndicators,
} from './use-kline-indicators'

// 主图叠加指标：MA / EMA / BOLL；副图指标：MACD / RSI / KDJ（独立 pane 显示）
//   详见 use-kline-indicators.ts

type Props = {
  signalId: number | null
  asset: string
  action?: string
  entryPrice: number | null
  exitPrice?: number | null
  /** 初始副图指标。模拟盘复盘默认 MACD；其他场景默认 none。 */
  defaultSubIndicator?: SubIndicator
}

export function KlineChart({ signalId, asset, action, entryPrice, exitPrice, defaultSubIndicator }: Props) {
  const isDark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Line'> | null>(null)
  const extraLinesRef = useRef<IPriceLine[]>([])
  const markersRef = useRef<{ detach: () => void } | null>(null)
  const [chartReady, setChartReady] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<{
    action: string
    stop_loss: number | null
    take_profit: number | null
    trailing: number
    invalidation: string
    is_paper_trading: boolean
    data_source?: string
    market_type?: string
  } | null>(null)
  const [ohlc, setOhlc] = useState<{ o: number; h: number; l: number; c: number; changePct: number } | null>(null)
  const [interval, setInterval] = useState<'1m' | '15m' | '1h' | '4h' | '1d'>('1h')
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // ── 指标状态：主图叠加(MA/EMA/BOLL) + 副图(MACD/RSI/KDJ) ──
  const [mainInd, setMainInd] = useState<MainIndicator>('none')
  const [subInd, setSubInd] = useState<SubIndicator>(defaultSubIndicator ?? 'none')
  const [maPeriod, setMaPeriod] = useState(20)
  // 最近一次成功加载的 bars（指标重算用）
  const barsRef = useRef<ReplayKlineBar[]>([])
  // 主图叠加指标 series（BOLL 三线 / EMA / MA）——随选择动态创建到 pane0
  const overlayRef = useRef<ISeriesApi<'Line'>[]>([])
  // 副图指标 pane + series（MACD/RSI/KDJ 独立 pane）
  const subPaneRef = useRef<{ paneIndex: number; series: ISeriesApi<'Line'>[]; hist?: ISeriesApi<'Histogram'> } | null>(null)
  // 进出场竖线（时间点 → 像素 x 坐标）
  const markTimesRef = useRef<{ entry: number | null; exit: number | null; entryPrice: number | null; exitPrice: number | null }>(
    { entry: null, exit: null, entryPrice: null, exitPrice: null }
  )
  const [markLines, setMarkLines] = useState<{ x: number; color: string; label: string; kind: 'entry' | 'exit' }[]>([])
  const updateMarkLinesRef = useRef<() => void>(() => {})

  // 根据入场/出场时间点计算图表 x 像素坐标（随缩放/移动重算）
  const updateMarkLines = useCallback(() => {
    const chart = chartRef.current
    if (!chart) return
    const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
    const { entry, exit, entryPrice: ep, exitPrice: xp } = markTimesRef.current
    const lines: { x: number; color: string; label: string; kind: 'entry' | 'exit' }[] = []
    if (entry != null) {
      const x = chart.timeScale().timeToCoordinate(entry as Time)
      if (x != null) lines.push({ x, color: palette.entry, label: `入场 ${ep ?? '—'}`, kind: 'entry' })
    }
    if (exit != null) {
      const x = chart.timeScale().timeToCoordinate(exit as Time)
      if (x != null) lines.push({ x, color: palette.exit, label: `出场 ${xp ?? '—'}`, kind: 'exit' })
    }
    setMarkLines(lines)
  }, [isDark])
  updateMarkLinesRef.current = updateMarkLines

  // ── 主图叠加指标（BOLL 三线 / EMA / MA）渲染到 pane0 ──
  // 指标渲染层：主图叠加 + 副图（hook 内部 useEffect 自动响应 mainInd/subInd/maPeriod 变化）
  const { renderOverlay, renderSub } = useKlineIndicators({
    chartRef,
    candleRef,
    barsRef,
    overlayRef,
    subPaneRef,
    chartReady,
    mainInd,
    subInd,
    maPeriod,
  })

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
    // v5 autoSize：自动跟随容器尺寸，无需手动调宽高，也不随窗口跳动
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { color: palette.background },
        textColor: palette.text,
        fontFamily: 'var(--font-stack)',
        fontSize: 11,
        attributionLogo: false,
      },
      localization: {
        // crosshair 悬浮/OHLC 读数的时间也统一上海时区（tickMarkFormatter 只管轴刻度）
        timeFormatter: shTickFormatter,
        locale: 'zh-CN',
      },
      grid: {
        vertLines: { color: palette.grid },
        horzLines: { color: palette.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: palette.border,
        rightOffset: 2,
        barSpacing: 7,
        minBarSpacing: 4,
        tickMarkFormatter: shTickFormatter,  // 统一上海时区显示，与交易时间一致
      },
      rightPriceScale: {
        borderColor: palette.border,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      crosshair: { mode: 1 },
    })

    const candle = chart.addSeries(LineSeries, {
      color: palette.up,
      lineWidth: 1,
      priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    })
    // 成交量副图（交易所风格，底部独立 pane）
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
      priceLineVisible: false,
      lastValueVisible: false,
    })
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    })

    chartRef.current = chart
    candleRef.current = candle
    volumeRef.current = volume
    extraLinesRef.current = []
    // 缩放/移动时重算进出场竖线位置
    const onRangeChange = () => updateMarkLinesRef.current()
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange)
    setChartReady(true)

    return () => {
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange)
      setChartReady(false)
      extraLinesRef.current = []
      markersRef.current?.detach()
      markersRef.current = null
      overlayRef.current = []
      subPaneRef.current = null
      volumeRef.current = null
      chart.remove()
      chartRef.current = null
      candleRef.current = null
    }
  }, [isDark])

  const loadIdRef = useRef(0)
  // 最后一根 bar（实时价原地更新用）
  const lastBarRef = useRef<{ time: number; open: number; high: number; low: number } | null>(null)
  // 区分"切换信号"（显示 loading）与"切换周期"（保留旧图，不闪烁）
  const prevSignalRef = useRef<number | null>(null)

  // 加载 K 线并渲染；fit=true 时重置视图（切换信号/周期），false 时仅更新数据（轮询，保持视图）
  // showLoading 仅在切换信号时显示（切换周期保留旧图不闪烁）
  const loadKline = useCallback(async (fit: boolean, showLoading: boolean = true) => {
    if (!signalId || !chartReady || !candleRef.current) return
    const myId = ++loadIdRef.current
    if (fit && showLoading) { setLoading(true); setError(null) }
    try {
      const resp = await fetchReplaySignalKline(signalId, interval)
      if (myId !== loadIdRef.current || !candleRef.current) return
      const bars: ReplayKlineBar[] = [...resp.klines]
      if (!bars.length) {
        if (fit) setError('K 线数据为空')
        return
      }
      bars.sort((a, b) => a.time - b.time)
      const lastBar = bars[bars.length - 1]
      lastBarRef.current = { time: lastBar.time, open: lastBar.open, high: lastBar.high, low: lastBar.low }
      barsRef.current = bars
      candleRef.current.setData(
        bars.map((bar) => ({
          time: bar.time as Time,
          value: bar.close,
        })),
      )
      // 成交量副图（涨绿跌红）
      const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
      volumeRef.current?.setData(
        bars.map((bar) => ({
          time: bar.time as Time,
          value: bar.volume,
          color: bar.close >= bar.open ? `${palette.up}55` : `${palette.down}55`,
        })),
      )
      // 交易所风格 OHLC 读数：取区间首根 open 与最后一根 close 计算涨跌
      const last = bars[bars.length - 1]
      const first = bars[0]
      setOhlc({
        o: first.open,
        h: last.high,
        l: last.low,
        c: last.close,
        changePct: first && first.open ? ((last.close - first.open) / first.open) * 100 : 0,
      })
      extraLinesRef.current.forEach((line) => {
        try { candleRef.current?.removePriceLine(line) } catch { /* already removed */ }
      })
      extraLinesRef.current = []
      // 止损/追踪线保留横线（价格提示）
      if (resp.stop_loss) {
        extraLinesRef.current.push(candleRef.current.createPriceLine({
          price: resp.stop_loss,
          color: palette.stop,
          lineWidth: 2,
          lineStyle: 1,
          axisLabelVisible: true,
          title: `止损 ${resp.stop_loss}`,
        }))
      }
      if (resp.take_profit) {
        extraLinesRef.current.push(candleRef.current.createPriceLine({
          price: resp.take_profit,
          color: palette.take,
          lineWidth: 1,
          lineStyle: 3,
          axisLabelVisible: true,
          title: `追踪高/低 ${resp.take_profit}`,
        }))
      }
      // 进出场竖线标注：记录时间点，由 updateMarkLines 计算 x 坐标
      const markers: ReplayKlineMarker[] = resp.markers?.length
        ? resp.markers
        : [{
            time: resp.entry_time,
            position: (resp.action || action) === 'SELL' ? 'aboveBar' : 'belowBar',
            color: (resp.action || action) === 'SELL' ? palette.stop : palette.take,
            shape: (resp.action || action) === 'SELL' ? 'arrowDown' : 'arrowUp',
            text: `${resp.action || action || 'ENTRY'}`,
            kind: 'entry',
          }]
      const entryAligned = alignMarkerTime(resp.entry_time, bars)
      const exitMarker = markers.find((m) => m.kind === 'exit')
      markTimesRef.current = {
        entry: entryAligned,
        exit: exitMarker ? alignMarkerTime(exitMarker.time, bars) : null,
        entryPrice: resp.entry_price,
        exitPrice: resp.exit_price ?? null,
      }
      updateMarkLinesRef.current()
      markersRef.current?.detach()
      markersRef.current = createSeriesMarkers(candleRef.current, markers.map((marker) => ({
        time: alignMarkerTime(marker.time, bars) as Time,
        position: marker.position,
        color: marker.color,
        shape: marker.shape,
        text: marker.text,
      })))
      // 数据更新后重算指标（若已选）
      renderOverlay()
      renderSub()
      if (fit) {
        // 延迟一帧 fitContent：先让 setData 渲染，避免同帧双重重绘卡顿
        requestAnimationFrame(() => chartRef.current?.timeScale().fitContent())
      }
      setMeta({
        action: resp.action || action || '',
        stop_loss: resp.stop_loss ?? null,
        take_profit: resp.take_profit ?? null,
        trailing: resp.trailing_callback_rate ?? 0,
        invalidation: resp.invalidation_condition || '',
        is_paper_trading: resp.is_paper_trading,
        data_source: resp.data_source,
        market_type: resp.market_type,
      })
    } catch (err) {
      if (myId === loadIdRef.current && fit) setError(String(err))
    } finally {
      if (myId === loadIdRef.current && fit) setLoading(false)
    }
  }, [signalId, interval, isDark, chartReady, action, renderOverlay, renderSub])

  // 实时价：原地更新最后一根 bar 的 close/high/low/volume + OHLC 读数（1s 一次）
  const refreshLive = useCallback(async () => {
    const lb = lastBarRef.current
    const chart = chartRef.current
    if (!signalId || !lb || !chart || !candleRef.current) return
    const res = await fetchLivePrice(asset, interval)
    if (res.price == null) return
    const intervalSec = { '1m': 60, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400 }[interval]
    const nowSec = Math.floor(Date.now() / 1000)
    // 跨入新 bar 周期：全量刷新拉取新 bar
    if (nowSec >= lb.time + intervalSec) {
      void loadKline(false)
      return
    }
    const price = res.price
    const high = Math.max(lb.high, price)
    const low = Math.min(lb.low, price)
    candleRef.current.update({ time: lb.time as Time, value: price })
    lastBarRef.current = { ...lb, high, low }
    // 成交量同步更新（加密走 Binance 实时量；黄金/原油返回 null 则保持）
    if (res.volume != null) {
      const palette = isDark ? CHART_THEME.dark : CHART_THEME.light
      volumeRef.current?.update({
        time: lb.time as Time,
        value: res.volume,
        color: price >= lb.open ? `${palette.up}55` : `${palette.down}55`,
      })
    }
    setOhlc((prev) => (prev ? { ...prev, h: high, l: low, c: price } : prev))
  }, [signalId, asset, interval, isDark, loadKline])

  // 信号/周期切换：加载并重置视图；切换信号显示 loading，切换周期保留旧图不闪烁
  useEffect(() => {
    const switchingSignal = prevSignalRef.current !== signalId
    prevSignalRef.current = signalId
    void loadKline(true, switchingSignal)
  }, [loadKline])

  // 实时刷新：最新 bar / OHLC 跟随行情（保持视图，不 fitContent）
  // 1m 受 Yahoo 限流，间隔自动拉长
  useEffect(() => {
    if (!signalId || !chartReady) return
    // 1m 保持 60s（Yahoo 限流）；1H/4H/1D 加快刷新，周期越长间隔稍长
    const ms = ({ '1m': 15_000, '15m': 12_000, '1h': 10_000, '4h': 15_000, '1d': 20_000 })[interval] ?? 15_000
    const timer = window.setInterval(() => { void loadKline(false) }, ms)
    return () => window.clearInterval(timer)
  }, [signalId, chartReady, interval, loadKline])

  // 实时价轮询：1 秒一次，原地更新最后一根 bar（轻量接口，后端 2s 缓存）
  useEffect(() => {
    if (!signalId || !asset) return
    const timer = window.setInterval(() => { void refreshLive() }, 1000)
    return () => window.clearInterval(timer)
  }, [signalId, asset, refreshLive])

  const indicatorBtn = (active: boolean) =>
    `rounded px-1.5 py-0.5 transition ${
      active ? 'bg-primary/20 text-primary' : 'text-muted-foreground hover:text-foreground'
    }`

  return (
    <div className="relative h-full w-full overflow-hidden rounded-md border border-border bg-card">
      <div className="absolute left-2 top-2 z-10 flex max-w-[92%] flex-wrap items-center gap-2 font-mono text-[10px] text-muted-foreground">
        <span className="rounded border border-hold/50 bg-hold/10 px-2 py-0.5 text-hold">
          PAPER · 模拟盘
        </span>
        {meta?.data_source === 'real' ? (
          <span className="rounded border border-long/50 bg-long/10 px-2 py-0.5 text-long">
            实时行情
          </span>
        ) : meta?.data_source === 'simulated' ? (
          <span className="rounded border border-hold/40 bg-hold/10 px-2 py-0.5 text-hold">
            模拟反推（行情源不可用）
          </span>
        ) : null}
        {meta?.market_type === 'binance_perp' && (
          <span
            className="rounded border border-sky-500/40 bg-sky-500/10 px-2 py-0.5 text-sky-500"
            title="K线与成交价同源：币安永续合约（fapi）。与现货/国际期货价可能有基差，交易以本价为准"
          >
            币安永续
          </span>
        )}
        {meta?.market_type === 'simulated' && (
          <span
            className="rounded border border-hold/40 bg-hold/10 px-2 py-0.5 text-hold"
            title="行情源不可用，K线按 entry/max/min 模拟反推，非真实盘口"
          >
            模拟
          </span>
        )}
        {meta?.market_type === 'yahoo' && (
          <span
            className="rounded border border-blue-400/40 bg-blue-400/10 px-2 py-0.5 text-blue-500 dark:text-blue-300"
            title="股票 / 海力士 / 美股 — Yahoo Finance v8 chart，yfinance / asia_prices 多源兜底（multi_source_fetcher）"
          >
            Yahoo Finance
          </span>
        )}
        <span className="text-foreground">
          {asset || '—'} · {meta?.action || action || '—'} · entry {entryPrice ?? '—'}
          {exitPrice != null ? ` · exit ${exitPrice}` : ''}
        </span>
        {meta?.stop_loss != null && (
          <span className="text-short">止损 {meta.stop_loss}{meta.trailing ? ` · 回撤 ${meta.trailing}%` : ''}</span>
        )}
        {meta?.invalidation && (
          <span className="max-w-[240px] truncate" title={meta.invalidation}>失效：{meta.invalidation}</span>
        )}
        {meta?.is_paper_trading && (
          <span className="text-muted-foreground">（基于 entry/max/min 反推，非真实盘口）</span>
        )}
      </div>
      {/* 周期切换 + 指标按钮 + 交易所风格 OHLC 读数条 */}
      <div className="absolute right-2 top-2 z-10 flex flex-col items-end gap-1.5">
        <div className="flex items-center gap-1">
          {/* 周期切换 */}
          <div className="flex items-center gap-0.5 rounded border border-border bg-card/80 px-1 py-0.5 font-mono text-[10px] backdrop-blur-sm">
            {(['1m', '15m', '1h', '4h', '1d'] as const).map((iv) => (
              <button
                key={iv}
                type="button"
                onClick={() => setInterval(iv)}
                className={`rounded px-1.5 py-0.5 transition ${
                  interval === iv
                    ? 'bg-primary/15 text-primary'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {iv === '1m' ? '1m' : iv === '15m' ? '15m' : iv === '1h' ? '1H' : iv === '4h' ? '4H' : '1D'}
              </button>
            ))}
          </div>
          {/* 指标按钮组 */}
          <div className="flex items-center gap-0.5 rounded border border-border bg-card/80 px-1 py-0.5 font-mono text-[10px] backdrop-blur-sm">
            <button type="button" className={indicatorBtn(mainInd === 'none' && subInd === 'none')} onClick={() => { setMainInd('none'); setSubInd('none') }}>
              关
            </button>
            <button type="button" className={indicatorBtn(mainInd === 'ma')} onClick={() => setMainInd(mainInd === 'ma' ? 'none' : 'ma')}>
              MA
            </button>
            <button type="button" className={indicatorBtn(mainInd === 'ema')} onClick={() => setMainInd(mainInd === 'ema' ? 'none' : 'ema')}>
              EMA
            </button>
            <button type="button" className={indicatorBtn(mainInd === 'boll')} onClick={() => setMainInd(mainInd === 'boll' ? 'none' : 'boll')}>
              BOLL
            </button>
            <span className="mx-0.5 h-3 w-px bg-border" />
            <button type="button" className={indicatorBtn(subInd === 'macd')} onClick={() => setSubInd(subInd === 'macd' ? 'none' : 'macd')}>
              MACD
            </button>
            <button type="button" className={indicatorBtn(subInd === 'rsi')} onClick={() => setSubInd(subInd === 'rsi' ? 'none' : 'rsi')}>
              RSI
            </button>
            <button type="button" className={indicatorBtn(subInd === 'kdj')} onClick={() => setSubInd(subInd === 'kdj' ? 'none' : 'kdj')}>
              KDJ
            </button>
            {mainInd === 'ma' && (
              <select
                value={maPeriod}
                onChange={(e) => setMaPeriod(Number(e.target.value))}
                className="ml-0.5 rounded border border-border bg-card px-1 py-0.5 font-mono text-[9px] text-foreground focus:border-primary focus:outline-none"
                aria-label="MA 周期"
              >
                {[5, 10, 20, 60].map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            )}
          </div>
        </div>
        {ohlc && (
          <div className="flex items-center gap-2 rounded border border-border bg-card/80 px-2 py-1 font-mono text-[10px] text-muted-foreground backdrop-blur-sm">
            <span>O <b className="text-foreground">{ohlc.o.toFixed(4)}</b></span>
            <span>H <b className="text-long">{ohlc.h.toFixed(4)}</b></span>
            <span>L <b className="text-short">{ohlc.l.toFixed(4)}</b></span>
            <span>C <b className="text-foreground">{ohlc.c.toFixed(4)}</b></span>
            <span className={ohlc.changePct >= 0 ? 'text-long' : 'text-short'}>
              {ohlc.changePct >= 0 ? '▲' : '▼'} {ohlc.changePct >= 0 ? '+' : ''}{ohlc.changePct.toFixed(2)}%
            </span>
          </div>
        )}
      </div>
      {loading && (
        <div className="absolute right-2 bottom-2 z-10 font-mono text-[10px] text-muted-foreground">
          加载 K 线…
        </div>
      )}
      {error && (
        <div className="absolute inset-x-2 bottom-2 z-10 rounded border border-short/50 bg-short/10 px-2 py-1 font-mono text-[10px] text-short">
          {error}
        </div>
      )}
      {/* 进出场竖线标注（时间点 + 旁边价格标签） */}
      {markLines.map((line, i) => (
        <div
          key={i}
          className="pointer-events-none absolute inset-y-0 z-[5]"
          style={{ left: line.x }}
        >
          <div
            className="absolute inset-y-0 w-0 border-l-2 border-dashed"
            style={{ borderColor: line.color, opacity: 0.9 }}
          />
          <span
            className="absolute top-1 left-1.5 whitespace-nowrap rounded px-1.5 py-0.5 font-mono text-[10px] font-bold backdrop-blur-sm"
            style={{ color: line.color, backgroundColor: `${line.color}1a`, border: `1px solid ${line.color}66` }}
          >
            {line.label}
          </span>
        </div>
      ))}
      {!signalId && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <span className="font-mono text-xs text-muted-foreground">← 在左侧选择一个信号查看 K 线</span>
        </div>
      )}
      <div ref={containerRef} className="h-full w-full" />
    </div>
  )
}
