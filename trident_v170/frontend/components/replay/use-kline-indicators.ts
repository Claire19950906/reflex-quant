// v170 P0+：K 线指标渲染 hook
//
// 职责：
//   - 把主图叠加（MA / EMA / BOLL）渲染到 pane0
//   - 把副图指标（MACD / RSI / KDJ）渲染到独立 pane1
//   - 不拉数据、不创建 chart、不改 candle/volume
//   - 数据更新（barsRef）后会自动重新调用主图 + 副图渲染
//
// 与 KlineChart 的契约：
//   - chartRef.current     — 已创建的 chart 实例
//   - candleRef.current    — pane0 主图 series
//   - barsRef.current      — 最近一次成功的 bars 数组（指标计算用）
//   - overlayRef.current   — 主图叠加 series 数组（hook 内部 push）
//   - subPaneRef.current   — 副图 pane + series 句柄（hook 内部 set）
//   - mainInd / subInd     — 用户选择的指标

import { useCallback, useEffect } from 'react'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import { HistogramSeries, LineSeries, type Time } from 'lightweight-charts'
import type { ReplayKlineBar } from '@/lib/replay-data'
import { bollinger, ema, kdj, macd, rsi, sma } from '@/lib/technical-indicators'
import { IND_COLORS } from './kline-utils'

export type MainIndicator = 'none' | 'ma' | 'ema' | 'boll'
export type SubIndicator = 'none' | 'macd' | 'rsi' | 'kdj'

type ChartRefLike = { current: IChartApi | null }
type CandleRefLike = { current: ISeriesApi<'Line'> | null }
type BarsRefLike = { current: ReplayKlineBar[] }
type OverlayRefLike = { current: ISeriesApi<'Line'>[] }
type SubPaneRefLike = {
  current: {
    paneIndex: number
    series: ISeriesApi<'Line'>[]
    hist?: ISeriesApi<'Histogram'>
  } | null
}

type UseKlineIndicatorsArgs = {
  chartRef: ChartRefLike
  candleRef: CandleRefLike
  barsRef: BarsRefLike
  overlayRef: OverlayRefLike
  subPaneRef: SubPaneRefLike
  chartReady: boolean
  mainInd: MainIndicator
  subInd: SubIndicator
  maPeriod: number
}

/**
 * 主图叠加 + 副图指标。
 * 选择指标后自动渲染；切回 none 时清掉对应 series。
 * 数据来源是 barsRef.current（由 useKlineData 写入）。
 */
export function useKlineIndicators({
  chartRef,
  candleRef,
  barsRef,
  overlayRef,
  subPaneRef,
  chartReady,
  mainInd,
  subInd,
  maPeriod,
}: UseKlineIndicatorsArgs) {
  // ── 主图叠加指标（BOLL 三线 / EMA / MA）渲染到 pane0 ──
  const renderOverlay = useCallback(() => {
    const chart = chartRef.current
    const candle = candleRef.current
    if (!chart || !candle) return
    overlayRef.current.forEach((s) => chart.removeSeries(s))
    overlayRef.current = []
    const bars = barsRef.current
    if (!bars.length || mainInd === 'none') return
    const mk = (data: { time: number; value: number }[], color: string, title: string) => {
      const series = chart.addSeries(LineSeries, {
        color,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title,
        priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
      })
      series.setData(data.map((p) => ({ time: p.time as Time, value: p.value })))
      overlayRef.current.push(series)
    }
    if (mainInd === 'ma') {
      mk(
        sma(bars, maPeriod).filter((p): p is { time: number; value: number } => p.value !== null),
        IND_COLORS.ma,
        `MA${maPeriod}`,
      )
    } else if (mainInd === 'ema') {
      mk(
        ema(bars, 20).filter((p): p is { time: number; value: number } => p.value !== null),
        IND_COLORS.ema,
        'EMA20',
      )
    } else if (mainInd === 'boll') {
      const bb = bollinger(bars, 20, 2)
      const clean = (s: { time: number; value: number | null }[]) =>
        s.filter((p): p is { time: number; value: number } => p.value !== null)
      mk(clean(bb.upper), IND_COLORS.bollUp, 'BOLL上')
      mk(clean(bb.middle), IND_COLORS.bollMid, 'BOLL中')
      mk(clean(bb.lower), IND_COLORS.bollLow, 'BOLL下')
    }
  }, [chartRef, candleRef, barsRef, overlayRef, mainInd, maPeriod])

  // ── 副图指标（MACD/RSI/KDJ 独立 pane）──
  const renderSub = useCallback(() => {
    const chart = chartRef.current
    if (!chart) return
    if (subPaneRef.current) {
      try {
        chart.removePane(subPaneRef.current.paneIndex)
      } catch {
        /* 已删 */
      }
      subPaneRef.current = null
    }
    const bars = barsRef.current
    if (!bars.length || subInd === 'none') return
    const pane = chart.addPane(true)
    const paneIndex = pane.paneIndex()
    const mkLine = (data: { time: number; value: number }[], color: string, title: string) => {
      const series = chart.addSeries(
        LineSeries,
        {
          color,
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          title,
        },
        paneIndex,
      )
      series.setData(data.map((p) => ({ time: p.time as Time, value: p.value })))
      return series
    }
    const clean = (s: { time: number; value: number | null }[]) =>
      s.filter((p): p is { time: number; value: number } => p.value !== null)
    const series: ISeriesApi<'Line'>[] = []
    if (subInd === 'rsi') {
      const r = rsi(bars, 14)
      series.push(mkLine(clean(r), IND_COLORS.rsiK, 'RSI14'))
    } else if (subInd === 'kdj') {
      const kd = kdj(bars, 9)
      series.push(mkLine(clean(kd.k), IND_COLORS.kdjK, 'K'))
      series.push(mkLine(clean(kd.d), IND_COLORS.kdjD, 'D'))
      series.push(mkLine(clean(kd.j), IND_COLORS.kdjJ, 'J'))
    } else if (subInd === 'macd') {
      const m = macd(bars)
      series.push(mkLine(clean(m.dif), IND_COLORS.dif, 'DIF'))
      series.push(mkLine(clean(m.dea), IND_COLORS.dea, 'DEA'))
      const histSeries = chart.addSeries(
        HistogramSeries,
        {
          priceFormat: { type: 'price', precision: 6, minMove: 0.000001 },
          priceLineVisible: false,
          lastValueVisible: false,
        },
        paneIndex,
      )
      histSeries.setData(
        m.hist
          .filter((p): p is { time: number; value: number } => p.value !== null)
          .map((p) => ({
            time: p.time as Time,
            value: p.value,
            color: p.value >= 0 ? IND_COLORS.histUp : IND_COLORS.histDown,
          })),
      )
      subPaneRef.current = { paneIndex, series, hist: histSeries }
      return
    }
    subPaneRef.current = { paneIndex, series }
  }, [chartRef, barsRef, subPaneRef, subInd])

  // 指标切换 → 重渲染（不重拉数据）
  useEffect(() => {
    if (!chartReady) return
    renderOverlay()
    renderSub()
  }, [chartReady, mainInd, maPeriod, subInd, renderOverlay, renderSub])

  return { renderOverlay, renderSub }
}