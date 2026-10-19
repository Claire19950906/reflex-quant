'use client'

import { useCallback, useEffect, useState } from 'react'
import { Brain, TrendingUp, TrendingDown } from 'lucide-react'
import { apiGet, apiSend } from '@/lib/api'
import { useToast } from '@/components/ui/toast'
import type {
  Adjustment,
  BuySellKpi,
  StrategyReflection,
} from '@/lib/types-reflection-improve'
import {
  PatternCard,
  TriggerCard,
  TopAssetAction,
  StrategyReflectionCard,
} from './reflection-improve-parts'

/** 主面板 — KPI 事件、买卖胜率、LLM 策略反思审批 */
export function ReflectionImprovePanel() {
  const { toast } = useToast()
  const [open, setOpen] = useState(false)
  const [adjustments, setAdjustments] = useState<Adjustment[]>([])
  const [kpi, setKpi] = useState<BuySellKpi | null>(null)
  const [stratReflections, setStratReflections] = useState<StrategyReflection[]>([])
  const [loadingAdj, setLoadingAdj] = useState(false)
  const [loadingKpi, setLoadingKpi] = useState(false)
  const [loadingStrat, setLoadingStrat] = useState(false)
  const [actingId, setActingId] = useState<number | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setErr(null)
    setLoadingAdj(true)
    setLoadingKpi(true)
    setLoadingStrat(true)
    try {
      const [adjRes, kpiRes, stratRes] = await Promise.all([
        apiGet<Adjustment[] | Adjustment>('/paper/adjustments?status=pending&limit=20'),
        apiGet<BuySellKpi>('/paper/buy-sell-kpi?days=60'),
        apiGet<StrategyReflection[]>('/paper/strategy-reflections?status=pending&limit=20'),
      ])
      setAdjustments(Array.isArray(adjRes) ? adjRes : [])
      setKpi(kpiRes && typeof kpiRes === 'object' && 'buy_pattern' in kpiRes ? kpiRes : null)
      setStratReflections(Array.isArray(stratRes) ? stratRes : [])
    } catch (e) {
      setErr(`拉取失败：${(e as Error).message}`)
    } finally {
      setLoadingAdj(false)
      setLoadingKpi(false)
      setLoadingStrat(false)
    }
  }, [])

  useEffect(() => {
    if (open) void refresh()
  }, [open, refresh])

  const actOnReflection = async (id: number, action: 'approve' | 'reject') => {
    setActingId(id)
    try {
      await apiSend(`/paper/strategy-reflections/${id}/review?action=${action}`, 'POST', { note: '' })
      if (action === 'approve') toast.success('已批准反思')
      else toast.info('已驳回反思')
      await refresh()
    } catch (e) {
      toast.error('操作失败：' + (e as Error).message)
    } finally {
      setActingId(null)
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex w-full items-center gap-2 rounded border border-primary/40 bg-primary/5 px-3 py-2 text-left font-mono text-[11px] text-primary hover:bg-primary/10"
      >
        <Brain className="h-3.5 w-3.5" />
        打开反思与改进面板（KPI + 策略级 LLM 反思审批）
      </button>
    )
  }

  return (
    <div className="space-y-3 rounded border border-primary/30 bg-background/40 p-3">
      <div className="flex items-center gap-2">
        <Brain className="h-4 w-4 text-primary" />
        <h3 className="font-mono text-[12px] font-semibold uppercase tracking-widest text-foreground">
          反思与改进
        </h3>
        <button
          type="button"
          onClick={() => void refresh()}
          className="ml-auto rounded border border-border bg-secondary/30 px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-secondary/50"
          disabled={loadingAdj || loadingKpi || loadingStrat}
        >
          {loadingAdj || loadingKpi || loadingStrat ? '刷新中…' : '刷新'}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded border border-border bg-secondary/30 px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-secondary/50"
        >
          收起
        </button>
      </div>

      {err && (
        <div className="rounded border border-short/40 bg-short/10 p-2 font-mono text-[10px] text-short">
          {err}
        </div>
      )}

      {/* Buy / Sell Patterns */}
      {kpi && (
        <div className="grid grid-cols-2 gap-2">
          <PatternCard
            icon={<TrendingUp className="h-3 w-3 text-long" />}
            title="买入 触发后胜率"
            pattern={kpi.buy_pattern}
          />
          <PatternCard
            icon={<TrendingDown className="h-3 w-3 text-short" />}
            title="卖出 触发后胜率"
            pattern={kpi.sell_pattern}
          />
          <TriggerCard win={kpi.win_trigger_pattern} loss={kpi.loss_trigger_pattern} />
          <TopAssetAction rows={kpi.per_asset_action.slice(0, 5)} />
        </div>
      )}

      {loadingKpi && !kpi && (
        <div className="font-mono text-[10px] text-muted-foreground">加载 KPI…</div>
      )}

      {/* Strategy-level reflections (LLM) */}
      <div className="space-y-2">
        <h4 className="font-mono text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          策略级 LLM 反思（{stratReflections.length} 待审）
        </h4>
        {loadingStrat && stratReflections.length === 0 ? (
          <div className="font-mono text-[10px] text-muted-foreground">加载中…</div>
        ) : stratReflections.length === 0 ? (
          <div className="font-mono text-[10px] text-muted-foreground">暂无待审反思。</div>
        ) : (
          stratReflections.map((r) => (
            <StrategyReflectionCard
              key={r.id}
              reflection={r}
              acting={actingId === r.id}
              onApprove={() => void actOnReflection(r.id, 'approve')}
              onReject={() => void actOnReflection(r.id, 'reject')}
            />
          ))
        )}
      </div>
    </div>
  )
}