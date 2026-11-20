'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v499 · 全局「暂停态」提示条
//
// 背景（2026-11-20 全站巡检）：系统跑在 v482 暂停态（~/.trident/state/DATA_PAUSED，
//   engine 未加载、122 个采集/分析任务被卸），新闻仍在入库但不再分析。结果是全站
//   出现大量"暂无数据 / N/A / — / 0"，用户无法区分"停了"还是"坏了"。
// 做法：只读 /api/status 的 paused 字段（后端新增），暂停时在内容区顶部常驻一条，
//   明说"这是暂停造成的"并给出恢复命令；不暂停时组件返回 null，零影响。
// 边界：接口失败一律静默（提示条自己的故障不能变成新的红条）。

type StatusResp = {
  paused?: boolean
  paused_since?: number
  paused_note?: string
}

function fmtClock(ts?: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function PauseBanner() {
  const [status, setStatus] = useState<StatusResp | null>(null)
  const [hidden, setHidden] = useState(false)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const r = await fetch(`${API_BASE}/status`, { cache: 'no-store' })
        if (!r.ok) return
        const d = (await r.json()) as StatusResp
        if (alive) setStatus(d)
      } catch {
        // 静默：状态探测失败不代表系统暂停，不弹任何东西
      }
    }
    void load()
    const timer = setInterval(load, 60_000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  if (!status?.paused || hidden) return null

  const since = fmtClock(status.paused_since)

  return (
    <div
      data-testid="pause-banner"
      className="sticky top-0 z-30 flex items-start justify-between gap-3 border-b border-amber-300 bg-amber-50/95 px-4 py-2 text-[12px] leading-relaxed text-amber-900 backdrop-blur dark:border-amber-700 dark:bg-amber-950/60 dark:text-amber-200"
    >
      <span className="min-w-0">
        <b>⏸ 数据采集已暂停</b>
        {since ? <span className="font-mono">（自 {since}）</span> : null}
        ：新闻仍在入库，但分析 / 决策 / 信号不会产出 —— 页面上的「暂无 / N/A / — / 0」是暂停造成的，先别当成故障排查。
        恢复：<code className="rounded bg-amber-100 px-1 font-mono dark:bg-amber-900/60">bash ~/.trident/wrappers/resume_data.sh</code>
      </span>
      <button
        type="button"
        onClick={() => setHidden(true)}
        className="shrink-0 rounded border border-amber-400/60 px-2 py-0.5 font-mono text-[11px] text-amber-900 hover:bg-amber-100 dark:text-amber-200 dark:hover:bg-amber-900/40"
      >
        隐藏
      </button>
    </div>
  )
}
