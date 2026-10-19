'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v281 全局联通中心 — 4 维度聚合 + 单通道测试
//   1. data_sources   — 行情 / 宏观 / 新闻 数据源
//   2. llm_channels   — LLM 通道（OpenRouter / DeepSeek / Grok / Doubao）
//   3. notify_channels — 出站告警（飞书 / Slack / Discord / 邮件 / 微信）
//   4. asset_edges    — 跨资产传导边

type Status = 'ok' | 'degraded' | 'down' | 'online' | 'error' | 'stale' | 'unknown' | 'disabled'

type SourceItem = {
  source_id: string
  category: string
  label: string
  status: Status
  consecutive_failures: number
  secs_since_ok: number | null
  secs_since_fail: number | null
  last_error: string
  last_latency_ms: number | null
}

type LlmChannel = {
  name: string
  available: boolean
  status?: string
  latency_ms?: number | null
  status_code?: number | null
  base?: string
  model?: string
  error?: string
}

type NotifyChannel = {
  channel_id: string
  kind: string
  label: string
  description: string
  configured: boolean
  enabled: boolean
  status: Status
  last_ok_ts: number | null
  last_fail_ts: number | null
  secs_since_ok: number | null
  secs_since_fail: number | null
  last_error: string
  last_latency_ms: number | null
  consecutive_failures: number
  total_ok: number
  total_fail: number
}

type AssetEdge = {
  source: string
  target: string
  lag_s?: number
}

type ConnectivityPayload = {
  ts: number
  overall_score: number
  data_sources: {
    status: Status
    health_pct: number
    tally: Record<string, number>
    total: number
    items: SourceItem[]
  }
  llm_channels: {
    status: Status
    health_pct: number
    ok: number
    err: number
    total: number
    items: LlmChannel[]
    engine_openrouter?: { ok?: boolean } | null
    engine_model_pool?: string[] | null
  }
  notify_channels: {
    status: Status
    configured: number
    online: number
    tally: Record<string, number>
    total: number
    items: NotifyChannel[]
  }
  asset_edges: {
    total: number
    items: AssetEdge[]
  }
}

const STATUS_META: Record<string, { label: string; dot: string; text: string; ring: string }> = {
  ok:       { label: '正常', dot: 'bg-emerald-500', text: 'text-long',    ring: 'ring-emerald-500/40' },
  degraded: { label: '降级', dot: 'bg-amber-400',   text: 'text-amber-500', ring: 'ring-amber-500/30' },
  down:     { label: '不可用', dot: 'bg-red-500',    text: 'text-short',   ring: 'ring-red-500/40' },
  online:   { label: '在线', dot: 'bg-emerald-500', text: 'text-long',    ring: 'ring-emerald-500/40' },
  error:    { label: '报错', dot: 'bg-red-500',    text: 'text-short',   ring: 'ring-red-500/40' },
  stale:    { label: '过期', dot: 'bg-amber-400',   text: 'text-amber-500', ring: 'ring-amber-500/30' },
  unknown:  { label: '未配置', dot: 'bg-slate-400', text: 'text-muted-foreground', ring: 'ring-slate-500/30' },
  disabled: { label: '禁用', dot: 'bg-slate-500',  text: 'text-muted-foreground', ring: 'ring-slate-500/30' },
}

function scoreMeta(score: number) {
  if (score >= 85) return { label: '优秀', ring: 'ring-emerald-500/50', bar: 'bg-emerald-500', text: 'text-long' }
  if (score >= 60) return { label: '良好', ring: 'ring-blue-500/40',    bar: 'bg-blue-500',   text: 'text-blue-500' }
  if (score >= 30) return { label: '降级', ring: 'ring-amber-500/40',   bar: 'bg-amber-500',  text: 'text-amber-500' }
  return            { label: '不可用', ring: 'ring-red-500/50',    bar: 'bg-red-500',    text: 'text-short' }
}

function fmtAgo(secs: number | null | undefined): string {
  if (secs == null) return '—'
  if (secs < 60) return `${Math.round(secs)}s 前`
  if (secs < 3600) return `${Math.round(secs / 60)}m 前`
  return `${(secs / 3600).toFixed(1)}h 前`
}

function fmtClock(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export default function ConnectivityPage() {
  const [data, setData] = useState<ConnectivityPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [lastTestResult, setLastTestResult] = useState<Record<string, { ok: boolean; info: string; latency_ms?: number }>>({})

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/connectivity/status`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const d = (await r.json()) as ConnectivityPayload
      setData(d)
      setError(null)
    } catch (err) {
      setError(String(err))
    }
  }, [])

  useEffect(() => {
    void load()
    const t = window.setInterval(load, 15_000)
    return () => window.clearInterval(t)
  }, [load])

  const handleTestChannel = useCallback(async (channelId: string) => {
    setTesting(channelId)
    try {
      const r = await fetch(`${API_BASE}/connectivity/test-channel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel_id: channelId }),
      })
      const d = (await r.json()) as { ok: boolean; info: string; latency_ms?: number }
      setLastTestResult((prev) => ({ ...prev, [channelId]: { ok: !!d.ok, info: d.info || '', latency_ms: d.latency_ms } }))
      void load()
    } catch (err) {
      setLastTestResult((prev) => ({ ...prev, [channelId]: { ok: false, info: String(err) } }))
    } finally {
      setTesting(null)
    }
  }, [load])

  const handleDispatchAll = useCallback(async () => {
    setTesting('__dispatch_all__')
    await fetch(`${API_BASE}/connectivity/dispatch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '[Trident 全联通] 健康度测试', body: '这是一条来自 /connectivity 派发的全通道测试消息。' }),
    })
    setTesting(null)
    void load()
  }, [load])

  const handleReset = useCallback(async (channelId: string) => {
    await fetch(`${API_BASE}/connectivity/reset-channel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel_id: channelId }),
    })
    void load()
  }, [load])

  const scoreInfo = useMemo(() => data ? scoreMeta(data.overall_score) : null, [data])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-mono text-sm uppercase tracking-widest text-muted-foreground">全局联通中心 · Global Connectivity</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            数据源 / LLM 通道 / 通知通道 / 跨资产传导 — 四大维度实时健康度。每 15s 自动刷新。
          </p>
        </div>
        {data && scoreInfo && (
          <div className={`surface-card flex items-center gap-4 px-4 py-2 ring-1 ${scoreInfo.ring}`}>
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">综合评分</div>
              <div className={`font-mono text-3xl font-bold tabular-nums ${scoreInfo.text}`}>{data.overall_score.toFixed(1)}</div>
            </div>
            <div className="border-l border-border pl-4">
              <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">等级</div>
              <div className={`font-mono text-lg font-semibold ${scoreInfo.text}`}>{scoreInfo.label}</div>
            </div>
            <div className="border-l border-border pl-4">
              <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">更新时间</div>
              <div className="font-mono text-xs text-muted-foreground">{fmtClock(data.ts)}</div>
            </div>
          </div>
        )}
      </div>

      {error && (
        <div className="mb-4 rounded border border-short/40 bg-short/10 px-3 py-2 font-mono text-xs text-short">
          加载失败: {error}
        </div>
      )}

      {!data ? (
        <div className="font-mono text-xs text-muted-foreground">加载中…</div>
      ) : (
        <>
          {/* 总览四个维度 */}
          <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <DimensionCard
              title="数据源"
              subtitle={`${data.data_sources.total} 个源 · 健康度 ${data.data_sources.health_pct}%`}
              status={data.data_sources.status}
              tally={data.data_sources.tally}
            />
            <DimensionCard
              title="LLM 通道"
              subtitle={`${data.llm_channels.ok}/${data.llm_channels.total} 可用 · 健康度 ${data.llm_channels.health_pct}%`}
              status={data.llm_channels.status}
              tally={{ online: data.llm_channels.ok, error: data.llm_channels.err }}
            />
            <DimensionCard
              title="通知通道"
              subtitle={`${data.notify_channels.configured} 已配置 · ${data.notify_channels.online} 在线`}
              status={data.notify_channels.status}
              tally={data.notify_channels.tally}
            />
            <DimensionCard
              title="跨资产传导"
              subtitle={`${data.asset_edges.total} 条传导边`}
              status="ok"
              tally={{ online: data.asset_edges.total }}
            />
          </div>

          {/* 详情 4 区 */}
          <div className="grid gap-4 xl:grid-cols-2">
            {/* LLM 通道 */}
            <section className="surface-card p-4">
              <SectionTitle status={data.llm_channels.status} title="LLM 通道" hint={`${data.llm_channels.ok}/${data.llm_channels.total} 可用`} />
              <ul className="space-y-2">
                {data.llm_channels.items.map((c) => (
                  <li key={c.name} className="rounded border border-border bg-card p-2">
                    <div className="flex items-center gap-2">
                      <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${c.available ? 'bg-emerald-500' : 'bg-red-500'}`} />
                      <span className="font-mono text-xs font-semibold">{c.name}</span>
                      {c.model && <span className="truncate font-mono text-[10px] text-muted-foreground">· {c.model}</span>}
                      <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                        {typeof c.latency_ms === 'number' ? `${c.latency_ms} ms` : '—'}
                      </span>
                    </div>
                    {c.error && (
                      <div className="mt-1 line-clamp-2 font-mono text-[10px] text-short/80">{c.error}</div>
                    )}
                  </li>
                ))}
                {data.llm_channels.items.length === 0 && (
                  <li className="font-mono text-[11px] text-muted-foreground">尚未探测到 LLM 通道。</li>
                )}
              </ul>
              {data.llm_channels.engine_model_pool && data.llm_channels.engine_model_pool.length > 0 && (
                <details className="mt-2">
                  <summary className="cursor-pointer font-mono text-[10px] text-muted-foreground hover:text-foreground">
                    引擎模型池 ({data.llm_channels.engine_model_pool.length})
                  </summary>
                  <div className="mt-1 break-all font-mono text-[10px] text-muted-foreground">
                    {data.llm_channels.engine_model_pool.join(', ')}
                  </div>
                </details>
              )}
            </section>

            {/* 通知通道 */}
            <section className="surface-card p-4">
              <div className="mb-3 flex items-center justify-between">
                <SectionTitle status={data.notify_channels.status} title="通知通道" hint={`${data.notify_channels.online}/${data.notify_channels.configured} 在线 · ${data.notify_channels.total} 总数`} />
                <button
                  type="button"
                  onClick={handleDispatchAll}
                  disabled={testing === '__dispatch_all__'}
                  className="rounded bg-primary px-2 py-1 font-mono text-[10px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  data-testid="dispatch-all"
                >
                  {testing === '__dispatch_all__' ? '派发中…' : '📨 全通道测试'}
                </button>
              </div>
              <ul className="space-y-2">
                {data.notify_channels.items.map((c) => {
                  const meta = STATUS_META[c.status] ?? STATUS_META.unknown
                  const testR = lastTestResult[c.channel_id]
                  return (
                    <li key={c.channel_id} className={`rounded border border-border bg-card p-2 ring-1 ${meta.ring}`}>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${meta.dot}`} />
                        <span className="font-mono text-xs font-semibold">{c.label}</span>
                        <span className="font-mono text-[10px] text-muted-foreground">· {c.kind}</span>
                        {!c.configured && (
                          <span className="rounded bg-slate-500/15 px-1 py-0.5 font-mono text-[9px] text-muted-foreground">未配置</span>
                        )}
                        <span className={`ml-auto font-mono text-[10px] ${meta.text}`}>{meta.label}</span>
                      </div>
                      <div className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={c.description}>
                        {c.description}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px] text-muted-foreground">
                        {c.secs_since_ok !== null && <span>最近成功 {fmtAgo(c.secs_since_ok)}</span>}
                        {c.last_latency_ms !== null && <span>· {c.last_latency_ms} ms</span>}
                        {c.consecutive_failures > 0 && <span className="text-short">· 连续失败 {c.consecutive_failures}</span>}
                      </div>
                      {c.last_error && (
                        <div className="mt-1 line-clamp-2 font-mono text-[10px] text-short/80">{c.last_error}</div>
                      )}
                      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border/40 pt-2">
                        <button
                          type="button"
                          onClick={() => handleTestChannel(c.channel_id)}
                          disabled={testing === c.channel_id || !c.configured}
                          className="rounded bg-emerald-600 px-2 py-0.5 font-mono text-[10px] font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                          data-testid={`test-${c.channel_id}`}
                        >
                          {testing === c.channel_id ? '测试中…' : '🧪 测试此通道'}
                        </button>
                        {c.consecutive_failures > 0 && (
                          <button
                            type="button"
                            onClick={() => handleReset(c.channel_id)}
                            className="rounded bg-slate-600 px-2 py-0.5 font-mono text-[10px] font-medium text-white hover:bg-slate-700"
                          >
                            🔄 重置健康
                          </button>
                        )}
                        {testR && (
                          <span className={`font-mono text-[10px] ${testR.ok ? 'text-long' : 'text-short'}`}>
                            {testR.ok ? '✅' : '❌'} {testR.info}{testR.latency_ms !== undefined ? ` (${testR.latency_ms} ms)` : ''}
                          </span>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            </section>

            {/* 数据源（精简版，详情见 /sources） */}
            <section className="surface-card p-4">
              <SectionTitle status={data.data_sources.status} title="数据源" hint={`${data.data_sources.total} 个 · 在线 ${data.data_sources.tally.online || 0} · 错误 ${data.data_sources.tally.error || 0}`} />
              <ul className="thin-scroll grid max-h-[420px] gap-1.5 overflow-y-auto pr-1">
                {data.data_sources.items.slice(0, 30).map((s) => {
                  const meta = STATUS_META[s.status] ?? STATUS_META.unknown
                  return (
                    <li key={s.source_id} className="flex items-center gap-2 rounded border border-border/60 bg-card/50 px-2 py-1">
                      <span className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} />
                      <span className="truncate font-mono text-[10px] font-semibold">{s.label || s.source_id}</span>
                      <span className="ml-auto font-mono text-[9px] text-muted-foreground">{s.category}</span>
                      <span className={`shrink-0 font-mono text-[9px] ${meta.text}`}>{meta.label}</span>
                    </li>
                  )
                })}
              </ul>
              {data.data_sources.total > 30 && (
                <div className="mt-2 text-center">
                  <a href="/sources" className="font-mono text-[10px] text-muted-foreground underline-offset-2 hover:underline">
                    查看全部 {data.data_sources.total} 个源 →
                  </a>
                </div>
              )}
            </section>

            {/* 跨资产传导 */}
            <section className="surface-card p-4">
              <SectionTitle status="ok" title="跨资产传导" hint={`${data.asset_edges.total} 条传导边`} />
              <div className="mb-2 font-mono text-[10px] text-muted-foreground">
                来自 config.ASSET_DRIVEN_EDGES — 描述资产间因果与领先/滞后关系，用于跨资产信号补充。
              </div>
              <ul className="thin-scroll grid max-h-[280px] grid-cols-2 gap-1 overflow-y-auto pr-1">
                {data.asset_edges.items.map((e, i) => (
                  <li key={i} className="flex items-center gap-2 rounded border border-border/60 bg-card/50 px-2 py-1 font-mono text-[10px]">
                    <span className="font-semibold">{e.source}</span>
                    <span className="text-muted-foreground">→</span>
                    <span className="font-semibold">{e.target}</span>
                    {typeof e.lag_s === 'number' && (
                      <span className="ml-auto text-muted-foreground">{e.lag_s}s</span>
                    )}
                  </li>
                ))}
                {data.asset_edges.items.length === 0 && (
                  <li className="col-span-2 font-mono text-[10px] text-muted-foreground">
                    暂无传导边。运行 review / attribution 可自动构建。
                  </li>
                )}
              </ul>
            </section>
          </div>
        </>
      )}
    </main>
  )
}

// ── 子组件 ────────────────────────────────────────────────────────

function DimensionCard({ title, subtitle, status, tally }: {
  title: string
  subtitle: string
  status: Status
  tally: Record<string, number>
}) {
  const meta = STATUS_META[status] ?? STATUS_META.unknown
  const counts = [
    { key: 'online',   color: 'bg-emerald-500', label: '在线' },
    { key: 'error',    color: 'bg-red-500',    label: '报错' },
    { key: 'stale',    color: 'bg-amber-400',   label: '过期' },
    { key: 'unknown',  color: 'bg-slate-400',  label: '未配置' },
    { key: 'disabled', color: 'bg-slate-500',  label: '禁用' },
  ]
  return (
    <div className={`surface-card p-3 ring-1 ${meta.ring}`}>
      <div className="flex items-center gap-2">
        <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${meta.dot}`} />
        <span className="font-mono text-xs font-semibold">{title}</span>
        <span className={`ml-auto font-mono text-[10px] ${meta.text}`}>{meta.label}</span>
      </div>
      <div className="mt-1 font-mono text-[10px] text-muted-foreground">{subtitle}</div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {counts.map((c) => {
          const v = tally[c.key] || 0
          if (!v) return null
          return (
            <span key={c.key} className="flex items-center gap-1 rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px]">
              <span className={`inline-block h-1.5 w-1.5 rounded-full ${c.color}`} />
              {c.label} {v}
            </span>
          )
        })}
      </div>
    </div>
  )
}

function SectionTitle({ status, title, hint }: { status: Status; title: string; hint?: string }) {
  const meta = STATUS_META[status] ?? STATUS_META.unknown
  return (
    <div className="mb-3 flex items-center gap-2">
      <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${meta.dot}`} />
      <h3 className="font-mono text-xs font-semibold uppercase tracking-wider">{title}</h3>
      {hint && <span className="font-mono text-[10px] text-muted-foreground">· {hint}</span>}
    </div>
  )
}