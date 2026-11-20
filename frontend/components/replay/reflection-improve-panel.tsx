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
  // v339: 原来默认折叠。实测 /play 页 98 条待审参数调整 + 2 条策略反思
  // 在库里躺了 45 天没人看见 —— 「反思 → 参数落地」断在最后一步不是没人审，
  // 是入口默认收起、折叠条上也不显示待审数量，用户根本不知道有待办。
  const [open, setOpen] = useState(true)
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

  // v339: 挂载即拉一次（原来只在展开时才拉），让折叠状态下也能显示待审数量。
  useEffect(() => {
    void refresh()
  }, [refresh])

  const actOnReflection = async (id: number, action: 'approve' | 'reject') => {
    setActingId(id)
    try {
      // v2026.11.02 修复: 原调用 `/review?action=xxx` —— 后端并无该端点
      // (api_server.py 只注册了 /approve 与 /reject), 该调用必然 404,
      // 导致「策略级 LLM 反思」审批按钮恒定失败。
      const res = (await apiSend(`/paper/strategy-reflections/${id}/${action}`, 'POST', { note: '' })) as
        | { applied_count?: number; skipped_count?: number; skipped_reasons?: { key: string; reason: string }[] }
        | null
      if (action === 'approve') {
        // v339: 全部 recommendation 被跳过时后端会返回 applied_count=0，
        // 原实现照样弹「已批准反思」，用户以为生效了、参数却一点没动。
        if (res && (res.applied_count ?? 0) === 0) {
          const why = res.skipped_reasons?.[0]?.reason || '建议参数均无法自动应用'
          toast.warning(`已批准，但 0 条落地：${why}`)
        } else {
          toast.success(`已批准反思，落地 ${res?.applied_count ?? 0} 条`)
        }
      } else toast.info('已驳回反思')
      await refresh()
    } catch (e) {
      toast.error('操作失败：' + (e as Error).message)
    } finally {
      setActingId(null)
    }
  }

  /**
   * v2026.11.02 新增: 参数调整审批。
   * 后端 /api/paper/adjustments/{adj_id}/approve|reject 一直存在, 但本面板
   * 此前只 GET 了列表却从不渲染, 导致 119 条 pending 建议在界面上完全不可见、
   * 也无从审批 —— 「反思 → 参数落地」闭环断在最后一步。
   */
  const actOnAdjustment = async (id: number, action: 'approve' | 'reject') => {
    setActingId(id)
    try {
      const res = (await apiSend(
        `/paper/adjustments/${id}/${action}`,
        'POST',
        action === 'approve' ? { apply_to_settings: true, note: '' } : { note: '' },
      )) as { applied_to?: string; reason?: string; old_value?: unknown; new_value?: unknown } | null
      if (action === 'approve') {
        // v339: 后端折算后若与原值相同，会返回 applied_to='noop'。
        // 此时必须如实告诉用户「没变」，不能弹「已批准」让人以为参数动了。
        if (res?.applied_to === 'noop') {
          toast.warning(res.reason || '折算后与原值相同，未改变任何参数')
        } else if (res && res.new_value !== undefined) {
          toast.success(`已批准：${String(res.old_value)} → ${String(res.new_value)}`)
        } else {
          toast.success('已批准参数调整')
        }
      } else toast.info('已驳回参数调整')
      await refresh()
    } catch (e) {
      toast.error('操作失败：' + (e as Error).message)
    } finally {
      setActingId(null)
    }
  }

  if (!open) {
    const pendingTotal = adjustments.length + stratReflections.length
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex w-full items-center gap-2 rounded border border-primary/40 bg-primary/5 px-3 py-2 text-left font-mono text-[11px] text-primary hover:bg-primary/10"
      >
        <Brain className="h-3.5 w-3.5" />
        打开反思与改进面板（KPI + 策略级 LLM 反思审批）
        {pendingTotal > 0 && (
          <span className="ml-auto rounded bg-hold/15 px-1.5 py-0.5 font-mono text-[11px] text-hold">
            {pendingTotal} 待审
          </span>
        )}
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
          className="ml-auto rounded border border-border bg-secondary/30 px-2 py-0.5 font-mono text-[11px] text-muted-foreground hover:bg-secondary/50"
          disabled={loadingAdj || loadingKpi || loadingStrat}
        >
          {loadingAdj || loadingKpi || loadingStrat ? '刷新中…' : '刷新'}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded border border-border bg-secondary/30 px-2 py-0.5 font-mono text-[11px] text-muted-foreground hover:bg-secondary/50"
        >
          收起
        </button>
      </div>

      {err && (
        <div
          className="flex items-center gap-1.5 rounded border border-border bg-secondary/20 px-2 py-1 font-mono text-[11px] text-muted-foreground"
          title={err}
        >
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-muted-foreground/60" />
          建议/反思接口暂不可用（后端 5xx，非策略故障）· 点上方「刷新」重试
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
        <div className="font-mono text-[11px] text-muted-foreground">加载 KPI…</div>
      )}

      {/* v2026.11.02: 参数调整审批 —— 此前 adjustments 已拉取但从不渲染 */}
      <div className="space-y-2">
        <h4 className="font-mono text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          参数调整建议（{adjustments.length} 待审）
        </h4>
        {loadingAdj && adjustments.length === 0 ? (
          <div className="font-mono text-[11px] text-muted-foreground">加载中…</div>
        ) : adjustments.length === 0 ? (
          <div className="font-mono text-[11px] text-muted-foreground">暂无待审参数调整。</div>
        ) : (
          <ul className="space-y-1.5">
            {adjustments.map((a) => (
              <li
                key={a.id}
                className="rounded border border-border bg-secondary/20 px-2 py-1.5"
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-[11px] font-bold text-foreground">
                    {a.param_key}
                  </span>
                  <span
                    className={`rounded border px-1 py-0.5 font-mono text-[11px] ${
                      a.direction === 'up'
                        ? 'border-long/40 bg-long/10 text-long'
                        : 'border-short/40 bg-short/10 text-short'
                    }`}
                  >
                    {a.direction === 'up' ? '↑ 调高' : '↓ 调低'} {a.magnitude}
                  </span>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {a.asset === '*' ? '全资产' : a.asset} · {a.action}
                  </span>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    n={a.sample_size}
                    {a.avg_confidence != null ? ` · 置信 ${a.avg_confidence}` : ''}
                  </span>
                  <span className="ml-auto flex items-center gap-1">
                    <button
                      type="button"
                      disabled={actingId === a.id}
                      onClick={() => void actOnAdjustment(a.id, 'approve')}
                      className="rounded border border-long/40 bg-long/10 px-1.5 py-0.5 font-mono text-[11px] text-long hover:bg-long/20 disabled:opacity-50"
                    >
                      批准 · 写入参数
                    </button>
                    <button
                      type="button"
                      disabled={actingId === a.id}
                      onClick={() => void actOnAdjustment(a.id, 'reject')}
                      className="rounded border border-short/40 bg-short/10 px-1.5 py-0.5 font-mono text-[11px] text-short hover:bg-short/20 disabled:opacity-50"
                    >
                      驳回
                    </button>
                  </span>
                </div>
                {a.evidence_summary && (
                  <p className="mt-1 line-clamp-3 text-[11px] leading-relaxed text-muted-foreground">
                    {a.evidence_summary}
                  </p>
                )}
                {actingId === a.id && (
                  <div className="mt-1 font-mono text-[11px] text-muted-foreground">处理中…</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Strategy-level reflections (LLM) */}
      <div className="space-y-2">
        <h4 className="font-mono text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          策略级 LLM 反思（{stratReflections.length} 待审）
        </h4>
        {loadingStrat && stratReflections.length === 0 ? (
          <div className="font-mono text-[11px] text-muted-foreground">加载中…</div>
        ) : stratReflections.length === 0 ? (
          <div className="font-mono text-[11px] text-muted-foreground">暂无待审反思。</div>
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