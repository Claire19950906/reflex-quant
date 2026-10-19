'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// 当前生效经验规则（区分来源）+ 停用经验的启用入口。
// origin: real=历史交易复盘 / quick_sim=快速评测提炼 / rule=归因规则
//
// 为什么要显示停用项：停用的经验不注入 prompt，但要能看到、能逐条恢复。
// （历史背景：早期自动停用是单向的——只评估 active=1 的经验，停用后永不重评，
//   曾一次连坐停用全部 110 条。现已改为按命中胜率双向启停 + 无样本误杀自动救回。）
// 曾出现 102 条经验因同一次复盘写入、判定窗口相同而"连坐"全部停用；若这里只显示
// 生效项，用户只会看到一块空白，既不知道原因、也没地方把经验重新启用。

type ActiveInsight = { id: number; rule: string; origin: string }
type AllInsight = {
  id: number
  rule: string
  origin: string
  active: number
  source_samples: number
}
type InsightData = {
  insights: string[]
  active_items?: ActiveInsight[]
  active_total?: number
  /** 已停用经验总数 —— 用于说明"0 条生效"的原因 */
  deactivated_total?: number
  /** 快速评测持仓时长（分钟）—— 后端配置下发，避免把时长写死在文案里 */
  quick_sim_horizon_minutes?: number
}

export function ActiveInsightsPanel() {
  const [data, setData] = useState<InsightData | null>(null)
  const [allInsights, setAllInsights] = useState<AllInsight[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showDeactivated, setShowDeactivated] = useState(false)

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/review/insights`, { cache: 'no-store' })
      if (r.ok) setData((await r.json()) as InsightData)
      // 全量列表（含停用）：供逐条启用；不受"注入窗口"条数限制
      const r2 = await fetch(`${API_BASE}/review/insights/all`, { cache: 'no-store' })
      if (r2.ok) setAllInsights((await r2.json()) as AllInsight[])
      setError(null)
    } catch {
      // 静默：复盘经验不可用不影响提示词管理
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    // 10s 轮询：复盘页启用/停用后这里自动同步
    const timer = window.setInterval(() => void load(), 10_000)
    return () => window.clearInterval(timer)
  }, [load])

  const toggle = async (id: number) => {
    setBusyId(id)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/review/insights/${id}/toggle`, { method: 'PATCH' })
      if (!r.ok) throw new Error(await r.text())
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '操作失败')
    } finally {
      setBusyId(null)
    }
  }

  // 生效列表优先用全量数据（不受注入窗口条数限制），取不到时退回接口给的 active_items
  const items: ActiveInsight[] = allInsights.length > 0
    ? allInsights.filter((i) => i.active === 1)
    : (data?.active_items ?? [])
  const deactivated = allInsights.filter((i) => i.active !== 1)
  const real = items.filter((i) => i.origin === 'real' || !i.origin)
  const qs = items.filter((i) => i.origin === 'quick_sim')
  const rule = items.filter((i) => i.origin === 'rule')
  // 未拿到配置时退回 config.py 默认值 20
  const qsHorizon = data?.quick_sim_horizon_minutes ?? 20
  const deactivatedTotal = Math.max(data?.deactivated_total ?? 0, deactivated.length)

  const originBadge = (origin: string) =>
    origin === 'quick_sim' ? (
      <span className="shrink-0 rounded border border-amber-500/40 bg-amber-500/10 px-1 py-px font-mono text-[8px] text-amber-500" title={`由快速评测（${qsHorizon} 分钟模拟）提炼`}>快速评测</span>
    ) : origin === 'rule' ? (
      <span className="shrink-0 rounded border border-secondary bg-secondary px-1 py-px font-mono text-[8px] text-muted-foreground" title="归因规则">归因</span>
    ) : (
      <span className="shrink-0 rounded border border-long/40 bg-long/10 px-1 py-px font-mono text-[8px] text-long" title="由历史交易复盘提炼">历史交易</span>
    )

  const renderGroup = (title: string, list: ActiveInsight[], tone: string) => {
    if (!list.length) return null
    return (
      <div>
        <div className={`font-mono text-[9px] uppercase tracking-widest ${tone}`}>{title}（{list.length}）</div>
        <ul className="mt-0.5 space-y-0.5">
          {list.map((ins) => (
            <li key={ins.id} className="flex items-start gap-1.5 rounded border border-border/60 bg-card/60 px-1.5 py-1 text-[11px] leading-relaxed text-foreground/85">
              <span className="min-w-0 flex-1">✦ {ins.rule}</span>
              {originBadge(ins.origin)}
              <button
                type="button"
                onClick={() => void toggle(ins.id)}
                disabled={busyId === ins.id}
                title="停用（不再注入 AI 分析，可随时重新启用）"
                className="shrink-0 rounded border border-border px-1 py-px font-mono text-[9px] text-muted-foreground hover:bg-secondary disabled:opacity-50"
              >
                {busyId === ins.id ? '…' : '停用'}
              </button>
            </li>
          ))}
        </ul>
      </div>
    )
  }

  return (
    <section className="surface-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">当前生效经验规则</h2>
        <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">LEARNING</span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {data
            ? `${items.length} 条已注入 prompt${deactivatedTotal > 0 ? ` · ${deactivatedTotal} 条停用` : ''} · ≤5s 生效`
            : '加载中…'}
        </span>
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
        这些规则由 <b className="text-long">AI 复盘自动沉淀</b>（历史交易 / 快速评测 / 归因），
        以 <b className="text-foreground">「═══ 复盘经验 ═══」分区</b>注入每次 AI 分析的 prompt（带样本数与来源）。
        <b className="text-foreground">启用多少注入多少</b>（不截断）；与你的 Prompt / 品种专项规则冲突时，以规则为准。
      </p>

      {error && (
        <div className="mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 text-[11px] text-short">{error}</div>
      )}

      {loading && !data ? (
        <div className="mt-2 font-mono text-[10px] text-muted-foreground">加载中…</div>
      ) : items.length === 0 ? (
        <div className="mt-2 rounded border border-border bg-secondary/20 px-2 py-1.5 text-[10px] leading-relaxed text-muted-foreground">
          {deactivatedTotal > 0 ? (
            <>
              <div className="font-mono">
                当前 0 条生效 —— 但有 <b className="text-foreground">{deactivatedTotal} 条经验处于「已停用」</b>。
              </div>
              <div className="mt-0.5">
                停用/恢复都由系统按<b className="text-foreground">命中该经验的交易胜率</b>自动判定：
                命中样本 ≥10 且胜率 &lt;40% → 停用；已停用且胜率回升 ≥55% → <b className="text-foreground">自动恢复</b>。
                另外「停用但没有命中样本」的经验（历史连坐停用遗留）会在复盘或引擎启动时自动救回。
                也可以展开下方列表手动启用。
              </div>
            </>
          ) : (
            <>暂无生效经验 —— 跑一次 AI 复盘后，有价值的规律会自动沉淀到这里并回灌分析</>
          )}
        </div>
      ) : (
        <div className="mt-2 space-y-1.5">
          {renderGroup('▍历史交易复盘', real, 'text-long/80')}
          {renderGroup(`▍快速评测提炼（${qsHorizon} 分钟模拟）`, qs, 'text-amber-500/80')}
          {renderGroup('▍归因规则', rule, 'text-muted-foreground/70')}
        </div>
      )}

      {/* 停用经验：逐条可启用（不受注入窗口条数限制） */}
      {deactivated.length > 0 && (
        <div className="mt-2 border-t border-border/50 pt-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setShowDeactivated((v) => !v)}
              className="rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground"
            >
              {showDeactivated ? '收起停用经验 ▾' : `展开停用经验（${deactivated.length}）▸`}
            </button>
            <span className="font-mono text-[9px] text-muted-foreground/60">
              点「启用」立即注入 prompt（≤5s 生效）；胜率回升时系统也会自动恢复
            </span>
          </div>
          {showDeactivated && (
            <ul className="mt-1 space-y-0.5">
              {deactivated.map((ins) => (
                <li
                  key={ins.id}
                  className="flex items-start gap-1.5 rounded border border-border/50 bg-secondary/20 px-1.5 py-1"
                >
                  <span className="min-w-0 flex-1 text-[10px] leading-relaxed text-muted-foreground/75">{ins.rule}</span>
                  {originBadge(ins.origin)}
                  <span className="shrink-0 font-mono text-[9px] text-muted-foreground/50">{ins.source_samples} 样本</span>
                  <button
                    type="button"
                    onClick={() => void toggle(ins.id)}
                    disabled={busyId === ins.id}
                    title="启用这条经验（立即注入 AI 分析）"
                    className="shrink-0 rounded border border-long/40 px-1 py-px font-mono text-[9px] text-long hover:bg-long/10 disabled:opacity-50"
                  >
                    {busyId === ins.id ? '…' : '启用'}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
