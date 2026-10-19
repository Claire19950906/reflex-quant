'use client'

import { useCallback, useEffect, useState } from 'react'
import { apiGet } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'

// v226: 跨资产异常同步检测面板 — 检测"≥ N 个不同行业同步同向"异动
// 数据来自 /api/cross-asset/anomaly。

type CrossAssetAnomalyResponse = {
  ok: boolean
  ts: number
  is_anomaly: boolean
  direction: 'up' | 'down' | 'none'
  classes_aligned: number
  classes_total: number
  symbols_aligned: string[]
  classes_aligned_names: string[]
  shape_consistent: boolean
  severity: 'high' | 'med' | 'low'
  message: string
  snapshot_ts: string
  macro_regime: string
}

function severityColor(sev: string) {
  if (sev === 'high') return 'border-rose-400 bg-rose-50 dark:border-rose-700 dark:bg-rose-950'
  if (sev === 'med') return 'border-amber-400 bg-amber-50 dark:border-amber-700 dark:bg-amber-950'
  return 'border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900'
}

export function CrossAssetAnomalyPanel() {
  const [data, setData] = useState<CrossAssetAnomalyResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [minClasses, setMinClasses] = useState(4)
  const [minPct, setMinPct] = useState(0.5)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const d = await apiGet<CrossAssetAnomalyResponse>(
        `/cross-asset/anomaly?min_classes=${minClasses}&min_pct=${minPct}`,
      )
      if (!d.ok) throw new Error('cross-asset/anomaly returned ok=false')
      setData(d)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '跨资产接口不可用')
    } finally {
      setLoading(false)
    }
  }, [minClasses, minPct])

  // v302 P0+：手写 setInterval → usePollRefresh（统一 alive flag + cleanup + deps 触发）
  usePollRefresh(reload, 300_000, [minClasses, minPct])

  if (error) {
    return (
      <div className="rounded border border-rose-300 bg-rose-50 p-3 text-sm dark:border-rose-700 dark:bg-rose-950">
        <div className="font-semibold text-rose-700 dark:text-rose-300">跨资产同步异常 · 接口异常</div>
        <div className="mt-1 text-rose-600 dark:text-rose-400">{error}</div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="rounded border border-slate-200 bg-white p-3 text-sm dark:border-slate-700 dark:bg-slate-900">
        <div className="font-semibold">跨资产同步异常</div>
        <div className="mt-1 text-slate-500">{loading ? '加载中…' : '等待数据'}</div>
      </div>
    )
  }

  const directionArrow = data.direction === 'up' ? '📈' : data.direction === 'down' ? '📉' : '⚖️'

  return (
    <div className="rounded border border-slate-200 bg-white p-3 text-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2 flex items-center justify-between">
        <div className="font-semibold">
          {directionArrow} 跨资产同步异常 · {data.macro_regime}
        </div>
        <button onClick={reload} disabled={loading}
          className="rounded bg-slate-100 px-2 py-0.5 text-xs hover:bg-slate-200 disabled:opacity-50 dark:bg-slate-800 dark:hover:bg-slate-700">
          {loading ? '…' : '🔄'}
        </button>
      </div>

      <div className={`rounded border p-2 mb-2 ${severityColor(data.severity)}`}>
        <div className="font-semibold">
          {data.is_anomaly ? `🚨 触发异常 · severity=${data.severity}` : '✅ 无异常'}
        </div>
        <div className="mt-1">{data.message}</div>
        {data.shape_consistent && (
          <div className="mt-1 text-xs font-semibold text-amber-700 dark:text-amber-300">
            ⚠️ 形态高度一致 → 系统性传导信号
          </div>
        )}
      </div>

      {data.is_anomaly && (
        <>
          <div className="text-xs text-slate-600 dark:text-slate-300">
            同向行业：<span className="font-mono">{data.classes_aligned} / {data.classes_total}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-1 text-xs">
            {data.classes_aligned_names.map((cls) => (
              <span key={cls} className="rounded bg-slate-100 px-2 py-0.5 font-mono dark:bg-slate-800">
                {cls}
              </span>
            ))}
          </div>
          <div className="mt-2 text-xs text-slate-600 dark:text-slate-300">
            代表品种：{data.symbols_aligned.slice(0, 8).join(', ')}
            {data.symbols_aligned.length > 8 ? ' …' : ''}
          </div>
        </>
      )}

      <div className="mt-2 flex items-center gap-2 text-[10px] text-slate-400 dark:text-slate-500">
        <label>行业阈值:</label>
        <select value={minClasses} onChange={(e) => setMinClasses(Number(e.target.value))}
          className="rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-800">
          {[2, 3, 4, 5, 6].map(n => <option key={n} value={n}>{n}</option>)}
        </select>
        <label>幅度阈值:</label>
        <select value={minPct} onChange={(e) => setMinPct(Number(e.target.value))}
          className="rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-800">
          {[0.1, 0.3, 0.5, 1.0, 1.5, 2.0].map(n => <option key={n} value={n}>{n}%</option>)}
        </select>
      </div>

      <div className="mt-1 text-[10px] text-slate-400 dark:text-slate-500">
        观测 {new Date(data.ts * 1000).toLocaleString('zh-CN')}
      </div>
    </div>
  )
}
