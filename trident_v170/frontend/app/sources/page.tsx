'use client'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

type SourceItem = {
  source_id: string
  category: string
  label: string
  description: string
  status: 'online' | 'error' | 'stale' | 'unknown'
  last_result: string | null
  last_ok_ts: number | null
  last_fail_ts: number | null
  last_error: string
  consecutive_failures: number
  total_ok: number
  total_fail: number
  secs_since_ok: number | null
  secs_since_fail: number | null
  detail: string
  // v283+ 任务 5：接入状态，把"故意不接/等付费/等 key"和"真坏了"区分开
  access_state?: 'active' | 'existing' | 'free' | 'needs_key' | 'needs_registration' | 'needs_login'
    | 'premium_tier' | 'no_public_api' | 'duplicate' | 'untracked'
}

const STATUS_META: Record<string, { label: string; dot: string; text: string }> = {
  online: { label: '在线', dot: 'bg-emerald-500', text: 'text-long' },
  error: { label: '报错', dot: 'bg-red-500', text: 'text-short' },
  stale: { label: '离线/过期', dot: 'bg-amber-400', text: 'text-amber-500' },
  unknown: { label: '未采集', dot: 'bg-slate-400', text: 'text-muted-foreground' },
}

// v283+ 任务 5：接入状态徽章（颜色编码：绿=可观察 / 蓝=未启用免费 / 黄=需 key / 红=永久不可用）
const ACCESS_STATE_META: Record<string, { label: string; cls: string }> = {
  active:             { label: '已接入',    cls: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400' },
  existing:           { label: '已接入',    cls: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400' },
  free:               { label: '免费可启用', cls: 'border-blue-500/50 bg-blue-500/10 text-blue-400' },
  needs_key:          { label: '需 API key', cls: 'border-amber-500/50 bg-amber-500/10 text-amber-400' },
  needs_registration: { label: '需注册账户', cls: 'border-amber-500/50 bg-amber-500/10 text-amber-400' },
  needs_login:        { label: '需登录',    cls: 'border-amber-500/50 bg-amber-500/10 text-amber-400' },
  premium_tier:       { label: '仅付费',    cls: 'border-rose-500/50 bg-rose-500/10 text-rose-400' },
  no_public_api:      { label: '无 API',   cls: 'border-slate-500/50 bg-slate-500/10 text-slate-400' },
  duplicate:          { label: '已停用·重复', cls: 'border-slate-500/50 bg-slate-500/10 text-slate-400' },
  untracked:          { label: '主动停用',  cls: 'border-slate-500/50 bg-slate-500/10 text-slate-400' },
}

const CATEGORY_ORDER = ['行情', '宏观', '新闻']

function fmtAgo(secs: number | null): string {
  if (secs == null) return '—'
  if (secs < 60) return `${Math.round(secs)}s 前`
  if (secs < 3600) return `${Math.round(secs / 60)}m 前`
  return `${Math.round(secs / 3600)}h 前`
}

export default function SourcesPage() {
  const [sources, setSources] = useState<SourceItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => {
      fetch(`${API_BASE}/sources/status`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => { if (alive) setSources(Array.isArray(d.sources) ? d.sources : []) })
        .catch((err) => { if (alive) setError(String(err)) })
    }
    void load()
    const timer = window.setInterval(load, 10_000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])

  const groups = CATEGORY_ORDER
    .map((cat) => ({ cat, items: (sources ?? []).filter((s) => s.category === cat) }))
    .filter((g) => g.items.length > 0)

  const tally: Record<string, number> = { online: 0, error: 0, stale: 0, unknown: 0 }
  ;(sources ?? []).forEach((s) => { tally[s.status] = (tally[s.status] ?? 0) + 1 })

  // v283+ 任务 5：按接入状态 + status 重新聚合，区分"真坏"和"故意不接"
  const healthTally: Record<string, Record<string, number>> = {}
  ;(sources ?? []).forEach((s) => {
    const st = s.access_state || 'active'
    healthTally[st] ??= {}
    healthTally[st][s.status] = (healthTally[st][s.status] ?? 0) + 1
  })
  // 健康度评分：active/existing + online 视为"正常运行"
  const observableTotal = (sources ?? []).filter((s) => (s.access_state === 'active' || s.access_state === 'existing')).length
  const observableOk = (sources ?? []).filter((s) => (s.access_state === 'active' || s.access_state === 'existing') && s.status === 'online').length
  const observableError = (sources ?? []).filter((s) => (s.access_state === 'active' || s.access_state === 'existing') && s.status === 'error').length
  const healthScore = observableTotal > 0 ? Math.round((observableOk / observableTotal) * 100) : 0
  const intentionalCount = (sources ?? []).filter((s) =>
    ['free', 'needs_key', 'needs_registration', 'needs_login', 'premium_tier', 'no_public_api', 'duplicate', 'untracked'].includes(s.access_state ?? ''),
  ).length

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mb-4">
        <h2 className="font-mono text-sm uppercase tracking-widest text-muted-foreground">信息源清单与分布</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          每个数据源实时标色：<span className="text-long">在线</span> / <span className="text-short">报错</span> /{' '}
          <span className="text-amber-500">离线/过期</span> / <span className="text-muted-foreground">未采集</span>。
          右侧徽章为接入状态，把"故意不接/等付费/等 key"和"真坏了"明确区分。
        </p>
      </div>

      {error && <div className="mb-4 rounded border border-short/40 bg-short/10 px-3 py-2 text-xs text-short">{error}</div>}

      {/* v283+ 任务 5：顶部健康度总览（区分"可观察"vs"故意不接"） */}
      {sources && (
        <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="surface-card p-3">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">健康度</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className={`font-mono text-2xl font-bold ${
                healthScore >= 80 ? 'text-long' : healthScore >= 50 ? 'text-amber-500' : 'text-short'
              }`}>{healthScore}%</span>
              <span className="font-mono text-[10px] text-muted-foreground">{observableOk}/{observableTotal} 在线</span>
            </div>
            <div className="mt-1 font-mono text-[10px] text-muted-foreground">
              可观察源 = active + existing 状态
            </div>
          </div>
          <div className="surface-card p-3">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">真坏</div>
            <div className="mt-1 font-mono text-2xl font-bold text-short">{observableError}</div>
            <div className="mt-1 font-mono text-[10px] text-muted-foreground">
              已接入但当前报错（看右侧徽章定位）
            </div>
          </div>
          <div className="surface-card p-3">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">故意不接</div>
            <div className="mt-1 font-mono text-2xl font-bold text-slate-400">{intentionalCount}</div>
            <div className="mt-1 font-mono text-[10px] text-muted-foreground">
              需 key / 付费 / 无 API / 主动停用 等
            </div>
          </div>
          <div className="surface-card p-3">
            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">合计</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold text-foreground">{sources.length}</span>
              <span className="font-mono text-[10px] text-muted-foreground">条</span>
            </div>
            <div className="mt-1 font-mono text-[10px] text-muted-foreground">
              按 status: 在线{tally.online} · 报错{tally.error} · 过期{tally.stale} · 未采集{tally.unknown}
            </div>
          </div>
        </div>
      )}

      {/* 旧式一行 tally 保留（精简） */}
      {sources && (
        <div className="mb-4 flex flex-wrap gap-4">
          <span className="font-mono text-[11px] text-long">● 在线 {tally.online}</span>
          <span className="font-mono text-[11px] text-short">● 报错 {tally.error}</span>
          <span className="font-mono text-[11px] text-amber-500">● 离线/过期 {tally.stale}</span>
          <span className="font-mono text-[11px] text-muted-foreground">● 未采集 {tally.unknown}</span>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        {groups.map((g) => (
          <section key={g.cat} className="surface-card p-4">
            <h3 className="mb-3 font-mono text-xs uppercase tracking-wider text-muted-foreground">
              {g.cat}（{g.items.length}）
            </h3>
            <ul className="space-y-2">
              {g.items.map((s) => {
                const meta = STATUS_META[s.status] ?? STATUS_META.unknown
                const access = ACCESS_STATE_META[s.access_state ?? 'active'] ?? ACCESS_STATE_META.active
                return (
                  <li key={s.source_id} className="rounded border border-border bg-card p-2">
                    <div className="flex items-center gap-2">
                      <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${meta.dot}`} />
                      <span className="truncate font-mono text-xs font-semibold">{s.label || s.source_id}</span>
                      {/* v283+ 任务 5：接入状态徽章（颜色编码） */}
                      <span
                        className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] ${access.cls}`}
                        title={`接入状态: ${access.label}`}
                      >
                        {access.label}
                      </span>
                      <span className={`ml-auto shrink-0 font-mono text-[10px] ${meta.text}`}>{meta.label}</span>
                    </div>
                    <div className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={s.source_id}>
                      {s.source_id}
                    </div>
                    {s.status === 'error' && s.last_error ? (
                      <div className="mt-1 line-clamp-2 font-mono text-[10px] text-short/80">{s.last_error}</div>
                    ) : null}
                    {s.status !== 'unknown' ? (
                      <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                        最近成功 {fmtAgo(s.secs_since_ok)} · 连续失败 {s.consecutive_failures}
                      </div>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          </section>
        ))}
      </div>

      {sources && sources.length === 0 && (
        <div className="font-mono text-xs text-muted-foreground">暂无数据源（后端尚未上报状态）</div>
      )}
    </main>
  )
}