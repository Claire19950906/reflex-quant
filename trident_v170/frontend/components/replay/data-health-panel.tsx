'use client'

import { useEffect, useState } from 'react'
import { apiGet } from '@/lib/api'
import { Activity, ChevronDown, ChevronRight, Loader2, RefreshCcw, Check, X, Minus, AlertCircle } from 'lucide-react'

// P2 「数据健康（多源 fallback）」面板
// 展示每个 (asset, feed_kind) 的 fallback 链 + 健康率 + 最近失败流水
// 不引入 severity/level — 4 个状态 (success/degraded/failed/skipped) 一律平铺

type ByKind = {
  asset: string
  feed_kind: string
  attempts: number
  success: number
  degraded: number
  failed: number
  primary_source: string | null
  primary_success_rate: number
  last_attempt_ts: string
  last_status: string
  last_error: string | null
}

type Summary = {
  days: number
  asset: string | null
  by_kind: ByKind[]
  totals: { success: number; degraded: number; failed: number; skipped: number }
  health_rate_pct: number | null
}

type RecentRow = {
  id: number; asset: string; feed_kind: string; source: string;
  status: string; latency_ms: number; value: number | null;
  error: string | null; ts: string;
}

type PolicyResp = {
  policy: Record<string, string[]>
  assets: string[]
  timeout_ms: number
  max_drift_pct: number
}

const STATUS_META: Record<string, { label: string; Icon: any }> = {
  success: { label: '成功', Icon: Check },
  degraded: { label: '降级', Icon: AlertCircle },
  failed: { label: '失败', Icon: X },
  skipped: { label: '跳过', Icon: Minus },
}

function StatusPill({ status }: { status: string }) {
  const m = STATUS_META[status] || STATUS_META.failed
  const Icon = m.Icon
  return (
    <span className="inline-flex items-center gap-1 rounded border border-border bg-card px-1.5 py-0.5 font-mono text-[10px] text-foreground">
      <Icon className="h-2.5 w-2.5 text-muted-foreground" />
      {m.label}
    </span>
  )
}

function HealthBadge({ pct }: { pct: number | null }) {
  if (pct == null) {
    return <span className="font-mono text-[10px] text-muted-foreground">无数据</span>
  }
  return <span className="font-mono text-[12px] font-bold text-foreground">{pct.toFixed(1)}%</span>
}


function PolicyChain({ chain }: { chain: string[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1 font-mono text-[10px]">
      {chain.map((src, idx) => (
        <span key={src} className="inline-flex items-center gap-1">
          <span className={
            idx === 0
              ? 'rounded border border-border bg-card px-1.5 py-0.5 font-bold text-foreground'
              : 'rounded border border-border bg-background px-1.5 py-0.5 text-muted-foreground'
          }>
            {src}
          </span>
          {idx < chain.length - 1 && <span className="text-muted-foreground">→</span>}
        </span>
      ))}
    </div>
  )
}


function KindRow({ row }: { row: ByKind }) {
  const total = row.success + row.degraded + row.failed
  return (
    <div className="rounded border border-border bg-card p-2 font-mono text-[11px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-bold text-foreground">{row.asset}</span>
        <span className="rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
          {row.feed_kind}
        </span>
        <span className="font-mono text-[10px] text-muted-foreground">
          主源 <span className="font-bold text-foreground">{row.primary_source || '?'}</span>
          （健康率 {row.primary_success_rate.toFixed(1)}%）
        </span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {row.attempts} 次
        </span>
      </div>
      {total > 0 && (
        <div className="mt-1 flex flex-wrap items-center gap-1 font-mono text-[10px]">
          <span className="rounded border border-border bg-background/40 px-1.5 py-0.5 text-foreground">
            成功 {row.success}
          </span>
          <span className="rounded border border-border bg-background/40 px-1.5 py-0.5 text-foreground">
            降级 {row.degraded}
          </span>
          <span className="rounded border border-border bg-background/40 px-1.5 py-0.5 text-foreground">
            失败 {row.failed}
          </span>
        </div>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
        <span>最近：</span>
        <StatusPill status={row.last_status} />
        <span>{row.last_attempt_ts}</span>
        {row.last_error && (
          <span className="truncate font-mono text-[10px] text-muted-foreground">
            · {row.last_error.slice(0, 80)}
          </span>
        )}
      </div>
    </div>
  )
}


export function DataHealthPanel() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [recent, setRecent] = useState<RecentRow[]>([])
  const [policy, setPolicy] = useState<PolicyResp | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [days, setDays] = useState(7)
  const [asset, setAsset] = useState('')
  const [expandedAsset, setExpandedAsset] = useState<string | null>(null)

  const load = () => {
    setLoading(true)
    setError(null)
    const qs1 = new URLSearchParams({ days: String(days), ...(asset ? { asset } : {}) })
    const qs2 = new URLSearchParams({ limit: '50', ...(asset ? { asset } : {}) })
    Promise.all([
      apiGet<Summary>(`/data-health/summary?${qs1.toString()}`),
      apiGet<{ items: RecentRow[] }>(`/data-health/recent?${qs2.toString()}`),
      apiGet<PolicyResp>('/data-health/policy'),
    ])
      .then(([s, r, p]) => {
        setSummary(s)
        setRecent(r.items || [])
        setPolicy(p)
        setError(null)
      })
      .catch((e) => {
        // P0 fix: lib/api.ts 已 throw BRIDGE_FALLBACK — 当作"端点未实现"灰态
        // 不显示红字 error, 让下方空态分支渲染"暂无数据健康记录"提示
        if (e instanceof Error && e.message.startsWith('BRIDGE_FALLBACK:')) {
          return  // 静默
        }
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days, asset])

  const byAsset: Record<string, ByKind[]> = {}
  if (summary && Array.isArray((summary as any).by_kind)) {
    for (const k of (summary as any).by_kind) {
      const a = (k.asset || '?').toUpperCase()
      if (!byAsset[a]) byAsset[a] = []
      byAsset[a].push(k)
    }
  }
  // P0 fix: lib/api.ts 已统一 throw 当 _bridge_fallback, 此处不需要再防御 wrapper
  // summary 为 null (apiGet throw 后 .then 不会跑) 或 shape 不匹配时, 走下方空态渲染

  return (
    <section id="data-health" className="rounded-md border border-border bg-background/60 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Activity className="h-4 w-4 text-primary" />
          <h3 className="font-mono text-[13px] font-bold text-foreground">
            数据健康（P2 · 多源 fallback）
          </h3>
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-0.5 font-mono text-[11px] text-foreground hover:bg-secondary/40 disabled:opacity-50"
        >
          {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCcw className="h-3 w-3" />}
          刷新
        </button>
      </div>

      <p className="mb-2 font-mono text-[10px] text-muted-foreground">
        每个 (asset, feed_kind) 按 <code className="rounded bg-secondary/40 px-1">feed_redund_policy</code> 走多源 fallback。
        4 个状态 (成功/降级/失败/跳过) 一律平铺 — 不分级。
      </p>

      <div className="mb-2 flex flex-wrap items-center gap-3 font-mono text-[11px]">
        <label className="flex items-center gap-1">
          时间窗
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}
            className="rounded border border-border bg-card px-1 py-0.5 font-mono text-[11px] text-foreground">
            <option value={1}>1d</option><option value={3}>3d</option>
            <option value={7}>7d</option><option value={14}>14d</option><option value={30}>30d</option>
          </select>
        </label>
        <label className="flex items-center gap-1">
          品种
          <select value={asset} onChange={(e) => setAsset(e.target.value)}
            className="rounded border border-border bg-card px-1 py-0.5 font-mono text-[11px] text-foreground">
            <option value="">全部</option>
            {(policy?.assets || []).map((a) => (<option key={a} value={a}>{a}</option>))}
          </select>
        </label>
        {summary && (summary as any).totals && (
          <>
            <span className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[10px]">
              总体健康率 <HealthBadge pct={summary.health_rate_pct} />
            </span>
            <span className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[10px]">
              成功 {(summary as any).totals.success}
            </span>
            <span className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[10px]">
              降级 {(summary as any).totals.degraded}
            </span>
            <span className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[10px]">
              失败 {(summary as any).totals.failed}
            </span>
            <span className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[10px]">
              跳过 {(summary as any).totals.skipped}
            </span>
          </>
        )}
      </div>

      {error && (
        <div className="rounded border border-alert/40 bg-alert/10 p-2 font-mono text-[11px] text-alert">
          加载失败：{error}
        </div>
      )}

      {!error && summary && Array.isArray((summary as any).by_kind) && (summary as any).by_kind.length === 0 && (
        <div className="rounded border border-border bg-card p-2 font-mono text-[11px] text-muted-foreground">
          暂无数据健康记录。运行 ai_worker / ingest 后会自动写入；也可手动调用
          <code className="mx-1 rounded bg-secondary/40 px-1">POST /api/data-health/record</code>
          写一条尝试。
        </div>
      )}

      <div className="space-y-2">
        {Object.keys(byAsset).sort().map((a) => {
          const rows = byAsset[a]
          const expanded = expandedAsset === a
          return (
            <div key={a} className="rounded border border-border bg-background/40">
              <button
                type="button"
                onClick={() => setExpandedAsset(expanded ? null : a)}
                className="flex w-full items-center justify-between gap-2 rounded p-2 hover:bg-secondary/40"
              >
                <div className="flex items-center gap-2">
                  {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                  <span className="font-mono text-[12px] font-bold text-foreground">{a}</span>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {rows.length} 个 feed_kind
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-1 font-mono text-[10px] text-muted-foreground">
                  <span>尝试 {rows.reduce((s, r) => s + r.attempts, 0)} 次</span>
                  <span>· 失败 {rows.reduce((s, r) => s + r.failed, 0)}</span>
                </div>
              </button>
              {expanded && (
                <div className="space-y-1 border-t border-border p-2">
                  {policy?.policy && policy.policy[a + '_PRICE'] && (
                    <div className="mb-1 font-mono text-[10px]">
                      <span className="text-muted-foreground">PRICE 链：</span>
                      <PolicyChain chain={policy.policy[a + '_PRICE']} />
                    </div>
                  )}
                  {policy?.policy && policy.policy[a + '_NEWS_GENERAL'] && (
                    <div className="mb-1 font-mono text-[10px]">
                      <span className="text-muted-foreground">NEWS 链：</span>
                      <PolicyChain chain={policy.policy[a + '_NEWS_GENERAL']} />
                    </div>
                  )}
                  {rows.map((r) => (<KindRow key={r.feed_kind} row={r} />))}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {recent.length > 0 && (
        <details className="mt-3 rounded border border-border bg-background/40 p-2">
          <summary className="cursor-pointer font-mono text-[11px] text-foreground">
            最近 {recent.length} 条 attempt 流水
          </summary>
          <div className="mt-1 space-y-0.5">
            {recent.map((row) => (
              <div key={row.id} className="flex flex-wrap items-center gap-2 font-mono text-[10px]">
                <span className="text-muted-foreground">{row.ts}</span>
                <span className="font-bold text-foreground">{row.asset}</span>
                <span className="rounded border border-border bg-card px-1 py-0.5 text-muted-foreground">
                  {row.feed_kind}
                </span>
                <span className="text-foreground">{row.source}</span>
                <StatusPill status={row.status} />
                {row.latency_ms > 0 && <span className="text-muted-foreground">{row.latency_ms}ms</span>}
                {row.value != null && <span className="text-muted-foreground">val={row.value}</span>}
                {row.error && (
                  <span className="truncate text-muted-foreground">
                    err: {row.error.slice(0, 60)}
                  </span>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
    </section>
  )
}