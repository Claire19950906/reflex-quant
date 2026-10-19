'use client'

import { useEffect, useState, useCallback, useMemo } from 'react'
import {
  Brain,
  ChevronDown,
  ChevronRight,
  Loader2,
  RefreshCcw,
  CheckCircle2,
  XCircle,
  CircleDashed,
  Network,
  History,
  Sparkles,
  ArrowRightLeft,
  Lightbulb,
} from 'lucide-react'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/utils'
import {
  DIM_LABELS,
  type DecisionTextResp,
  type Review,
  type ReviewListResp,
  STEPS,
} from '@/lib/types-reasoning-review'
import { useSegmentDrift } from '@/lib/hooks/use-segment-drift'
import { useReviewStats } from '@/lib/hooks/use-review-stats'
import { SegmentDriftBadge } from './segment-drift-badge'
import { DecisionCard } from './decision-card'

// P1 「推理审查」主面板 — 拆看 LLM 当时推理 6 维完备度，无 severity/level
// 6 维：8 段推理链完整性 / 反共识 / 因果机制 / 跨资产方向 / 假设 / 历史样本
// 所有问题一律平铺返回，不分级别，仅作信息性诊断


// ─── 主面板 ──────────────────────────────────────────────────────────

export function ReasoningReviewPanel() {
  const [days, setDays] = useState(7)
  const [onlyFailed, setOnlyFailed] = useState(false)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [data, setData] = useState<ReviewListResp | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const qs = new URLSearchParams({
        limit: '30',
        days: String(days),
        only_failed: String(onlyFailed),
      })
      const d = await apiGet<ReviewListResp>(`/reasoning/review/list?${qs}`)
      setData(d)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [days, onlyFailed])

  useEffect(() => {
    void load()
  }, [load])

  const stats = useReviewStats(data?.items)
  // 段位漂移: 单独 hook(useSegmentDrift),自动 useMemo 缓存
  const driftResult = useSegmentDrift(data?.items)

  return (
    <div className="flex flex-col gap-2">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2 rounded border border-border bg-card p-2 font-mono text-[11px]">
        <span className="font-bold text-foreground">推理审查</span>
        <span className="font-mono text-[10px] text-muted-foreground">
          拆看 LLM 当时推理 6 维是否完备 · 所有问题一律平铺
        </span>
        <span className="ml-auto inline-flex items-center gap-1">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-foreground"
          >
            <option value={1}>1 天</option>
            <option value={3}>3 天</option>
            <option value={7}>7 天</option>
            <option value={14}>14 天</option>
            <option value={30}>30 天</option>
          </select>
          <label className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
            <input
              type="checkbox"
              checked={onlyFailed}
              onChange={(e) => setOnlyFailed(e.target.checked)}
              className="h-3 w-3 accent-foreground"
            />
            只看失败
          </label>
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-1 rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-foreground hover:bg-accent"
            title="重新加载"
          >
            <RefreshCcw className="h-3 w-3" />
            刷新
          </button>
        </span>
      </div>

      {/* 聚合 */}
      {data && data.items.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-border bg-background/40 p-2 font-mono text-[10px] text-muted-foreground">
          <span>平均完成度 <span className="font-bold text-foreground">{stats.avg.toFixed(1)}%</span></span>
          <span>·</span>
          <span>{stats.n} 条</span>
          <span>·</span>
          <span>8 段缺段 <span className="font-bold text-foreground">{stats.missingStep}</span></span>
          <span>·</span>
          <span>无反共识 <span className="font-bold text-foreground">{stats.noCounter}</span></span>
          <span>·</span>
          <span>无因果机制 <span className="font-bold text-foreground">{stats.noMech}</span></span>
          <span>·</span>
          <span>无跨资产 <span className="font-bold text-foreground">{stats.noCross}</span></span>
          <span>·</span>
          <span>无假设 <span className="font-bold text-foreground">{stats.noAssum}</span></span>
          <span>·</span>
          <span>无历史 <span className="font-bold text-foreground">{stats.noHist}</span></span>
          {/* P1 段位漂移告警: |delta|>=30% 红色,|delta|>=15% 黄色,无漂移绿色 */}
          <span>·</span>
          <SegmentDriftBadge result={driftResult} max={3} />
        </div>
      )}

      {/* 错误 */}
      {err && (
        <div className="rounded border border-border bg-card p-2 font-mono text-[11px] text-muted-foreground">
          加载失败：{err}
        </div>
      )}

      {/* loading */}
      {loading && (
        <div className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          加载中…
        </div>
      )}

      {/* 列表 */}
      {data && data.items.length === 0 && !loading && (
        <div className="rounded border border-border bg-card p-3 font-mono text-[11px] text-muted-foreground">
          {days} 天内无审查条目
        </div>
      )}

      {data && data.items.length > 0 && (
        <div className="flex flex-col gap-2">
          {data.items.map((r) => (
            <DecisionCard key={r.decision_id} review={r} />
          ))}
        </div>
      )}
    </div>
  )
}
