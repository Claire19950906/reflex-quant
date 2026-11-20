'use client'

import { friendlyError } from '@/lib/friendly-error'

import { useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import SourceHealthPanel from '@/components/source-health-panel'
import { CapabilityPanel } from '@/components/quant/capability-panel'

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
  // v376: 观测层（只读诊断）—— 源可用率 / 指标新鲜度 / LLM 通道 / CFTC 持仓
  const [obs, setObs] = useState<{ feeds: any[]; stale: any; llm: any[]; cot: any[] } | null>(null)

  useEffect(() => {
    let alive = true
    // v453: 后台标签页跳过轮询；间隔 10s → 30s（源状态按分钟级变化，10s 是白打）
    const load = () => {
      if (document.hidden) return
      fetch(`${API_BASE}/sources/status`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => { if (alive) setSources(Array.isArray(d.sources) ? d.sources : []) })
        .catch((err) => { if (alive) setError(String(err)) })
    }
    void load()
    const timer = window.setInterval(load, 30_000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    let alive = true
    const loadObs = async () => {
      try {
        const [s, st, ll, cot] = await Promise.all([
          fetch(`${API_BASE}/obs/sources?days=7`, { cache: 'no-store' }).then((r) => r.json()),
          fetch(`${API_BASE}/obs/staleness?limit=8`, { cache: 'no-store' }).then((r) => r.json()),
          fetch(`${API_BASE}/obs/llm?days=1`, { cache: 'no-store' }).then((r) => r.json()),
          fetch(`${API_BASE}/obs/cot`, { cache: 'no-store' }).then((r) => r.json()),
        ])
        if (alive) {
          setObs({
            feeds: Array.isArray(s?.by_feed_health) ? s.by_feed_health.slice(0, 10) : [],
            stale: st ?? null,
            llm: Array.isArray(ll?.channels) ? ll.channels : [],
            cot: Array.isArray(cot?.items) ? cot.items : [],
          })
        }
      } catch {
        /* 观测层失败不阻塞主列表 */
      }
    }
    void loadObs()
    const t = window.setInterval(loadObs, 60_000)
    return () => { alive = false; window.clearInterval(t) }
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
        <h2 className="font-mono text-sm tracking-widest text-muted-foreground">信息源清单与分布</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          每个数据源实时标色：<span className="text-long">在线</span> / <span className="text-short">报错</span> /{' '}
          <span className="text-amber-500">离线/过期</span> / <span className="text-muted-foreground">未采集</span>。
          右侧徽章为接入状态，把"故意不接/等付费/等 key"和"真坏了"明确区分。
        </p>
      </div>

      {error && <div className="mb-4 rounded border border-short/40 bg-short/10 px-3 py-2 text-xs text-short">{friendlyError(error)}</div>}

      {/* v422：源健康度（停更/半残/未启用分开算，按入库量取分） */}
      <SourceHealthPanel />

      {/* v283+ 任务 5：顶部健康度总览（区分"可观察"vs"故意不接"） */}
      {sources && (
        <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="surface-card p-3">
            <div className="font-mono text-[11px] tracking-widest text-muted-foreground">健康度</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className={`font-mono text-2xl font-bold ${
                healthScore >= 80 ? 'text-long' : healthScore >= 50 ? 'text-amber-500' : 'text-short'
              }`}>{healthScore}%</span>
              <span className="font-mono text-[11px] text-muted-foreground">{observableOk}/{observableTotal} 在线</span>
            </div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              可观察源 = active + existing 状态
            </div>
          </div>
          <div className="surface-card p-3">
            <div className="font-mono text-[11px] tracking-widest text-muted-foreground">真坏</div>
            <div className="mt-1 font-mono text-2xl font-bold text-short">{observableError}</div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              已接入但当前报错（看右侧徽章定位）
            </div>
          </div>
          <div className="surface-card p-3">
            <div className="font-mono text-[11px] tracking-widest text-muted-foreground">故意不接</div>
            <div className="mt-1 font-mono text-2xl font-bold text-slate-400">{intentionalCount}</div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              需 key / 付费 / 无 API / 主动停用 等
            </div>
          </div>
          <div className="surface-card p-3">
            <div className="font-mono text-[11px] tracking-widest text-muted-foreground">合计</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold text-foreground">{sources.length}</span>
              <span className="font-mono text-[11px] text-muted-foreground">条</span>
            </div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
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
                        className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[11px] ${access.cls}`}
                        title={`接入状态: ${access.label}`}
                      >
                        {access.label}
                      </span>
                      <span className={`ml-auto shrink-0 font-mono text-[11px] ${meta.text}`}>{meta.label}</span>
                    </div>
                    <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground" title={s.source_id}>
                      {s.source_id}
                    </div>
                    {s.status === 'error' && s.last_error ? (
                      <div className="mt-1 line-clamp-2 font-mono text-[11px] text-short/80">{s.last_error}</div>
                    ) : null}
                    {s.status !== 'unknown' ? (
                      <div className="mt-1 font-mono text-[11px] text-muted-foreground">
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

      {/* v376 观测层：库里已有但之前没露出来的细节 */}
      {obs && (
        <div className="mt-6 grid gap-4 lg:grid-cols-2">
          <section className="surface-card p-4">
            <h3 className="mb-3 font-mono text-xs uppercase tracking-wider text-muted-foreground">
              源真实可用率（7 天）
            </h3>
            <table className="w-full table-fixed border-collapse font-mono text-[11px]">
              <colgroup>
                <col style={{ width: '26%' }} /><col style={{ width: '14%' }} />
                <col style={{ width: '12%' }} /><col style={{ width: '12%' }} />
                <col style={{ width: '36%' }} />
              </colgroup>
              <thead>
                <tr className="text-muted-foreground">
                  <th className="pb-1 text-left font-normal">源</th>
                  <th className="pb-1 text-right font-normal">成功率</th>
                  <th className="pb-1 text-right font-normal">p50</th>
                  <th className="pb-1 text-right font-normal">p95</th>
                  <th className="pb-1 pl-2 text-left font-normal">主要报错</th>
                </tr>
              </thead>
              <tbody>
                {obs.feeds.map((f) => {
                  const rate = f.success_rate == null ? null : Math.round(f.success_rate * 100)
                  const bad = rate != null && rate < 50
                  return (
                    <tr key={f.source} className="border-t border-border/60">
                      <td className="truncate py-1">{f.source}</td>
                      <td className={`py-1 text-right ${bad ? 'text-short' : 'text-long'}`}>{rate == null ? '—' : `${rate}%`}</td>
                      <td className="py-1 text-right text-muted-foreground">{f.latency_p50_ms == null ? '—' : `${Math.round(f.latency_p50_ms)}ms`}</td>
                      <td className="py-1 text-right text-muted-foreground">{f.latency_p95_ms == null ? '—' : `${Math.round(f.latency_p95_ms)}ms`}</td>
                      <td className="truncate py-1 pl-2 text-muted-foreground" title={f.top_error || ''}>{f.top_error || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <div className="mt-2 font-mono text-[11px] text-muted-foreground">
              ok/fail 来自 feed_health 近 7 天真实探测记录；成功率 &lt;50% 标红
            </div>
          </section>

          <section className="surface-card p-4">
            <h3 className="mb-3 font-mono text-xs uppercase tracking-wider text-muted-foreground">
              LLM 通道真实画像（24h）
            </h3>
            <table className="w-full table-fixed border-collapse font-mono text-[11px]">
              <colgroup>
                <col style={{ width: '38%' }} /><col style={{ width: '14%' }} />
                <col style={{ width: '14%' }} /><col style={{ width: '16%' }} />
                <col style={{ width: '18%' }} />
              </colgroup>
              <thead>
                <tr className="text-muted-foreground">
                  <th className="pb-1 text-left font-normal">通道</th>
                  <th className="pb-1 text-right font-normal">调用</th>
                  <th className="pb-1 text-right font-normal">失败率</th>
                  <th className="pb-1 text-right font-normal">p50</th>
                  <th className="pb-1 text-right font-normal">p95</th>
                </tr>
              </thead>
              <tbody>
                {obs.llm.slice(0, 8).map((c) => {
                  const fr = c.fail_rate == null ? null : Math.round(c.fail_rate * 100)
                  return (
                    <tr key={c.channel} className="border-t border-border/60">
                      <td className="truncate py-1" title={c.channel}>{c.channel}</td>
                      <td className="py-1 text-right">{c.calls}</td>
                      <td className={`py-1 text-right ${fr != null && fr > 20 ? 'text-short' : 'text-muted-foreground'}`}>
                        {fr == null ? '—' : `${fr}%`}
                      </td>
                      <td className="py-1 text-right text-muted-foreground">{c.latency_p50_ms == null ? '—' : `${(c.latency_p50_ms / 1000).toFixed(1)}s`}</td>
                      <td className="py-1 text-right text-muted-foreground">{c.latency_p95_ms == null ? '—' : `${(c.latency_p95_ms / 1000).toFixed(1)}s`}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <div className="mt-2 font-mono text-[11px] text-muted-foreground">
              来自 ai_call_stats；失败率 &gt;20% 标红（限流/过载一目了然）
            </div>
          </section>

          <section className="surface-card p-4">
            <h3 className="mb-3 font-mono text-xs uppercase tracking-wider text-muted-foreground">
              指标新鲜度
            </h3>
            {obs.stale ? (
              <>
                <div className="mb-2 flex flex-wrap gap-3 font-mono text-[11px]">
                  <span className="text-foreground">共 {obs.stale.total}</span>
                  <span className="text-long">正常 {obs.stale.fresh}</span>
                  <span className="text-amber-500">落后 {obs.stale.stale}</span>
                  <span className="text-muted-foreground">按地板放宽 {obs.stale.relaxed_by_floor ?? 0}</span>
                </div>
                <ul className="space-y-1">
                  {(obs.stale.items ?? []).slice(0, 8).map((it: any) => (
                    <li key={`${it.source}-${it.indicator}`} className="flex items-baseline gap-2 font-mono text-[11px]">
                      <span className="w-20 shrink-0 truncate text-muted-foreground">{it.source}</span>
                      <span className="w-14 shrink-0 truncate">{it.asset}</span>
                      <span className="w-24 shrink-0 truncate text-muted-foreground" title={it.indicator}>{it.indicator}</span>
                      <span className="shrink-0 text-amber-500">{fmtAgo(it.age_s)}</span>
                      <span className="truncate text-muted-foreground">{it.note || it.status}</span>
                    </li>
                  ))}
                </ul>
                <div className="mt-2 font-mono text-[11px] text-muted-foreground">
                  日频/周频源（ECB、FRED、CFTC 等）用 36h 地板判定，避免把"周末不发布"误报成断流
                </div>
              </>
            ) : (
              <div className="font-mono text-[11px] text-muted-foreground">暂无数据</div>
            )}
          </section>

          <section className="surface-card p-4">
            <h3 className="mb-3 font-mono text-xs uppercase tracking-wider text-muted-foreground">
              CFTC 持仓（最新报告周）
            </h3>
            <table className="w-full table-fixed border-collapse font-mono text-[11px]">
              <colgroup>
                <col style={{ width: '18%' }} /><col style={{ width: '20%' }} />
                <col style={{ width: '20%' }} /><col style={{ width: '22%' }} />
                <col style={{ width: '20%' }} />
              </colgroup>
              <thead>
                <tr className="text-muted-foreground">
                  <th className="pb-1 text-left font-normal">品种</th>
                  <th className="pb-1 text-right font-normal">商业净</th>
                  <th className="pb-1 text-right font-normal">非商业净</th>
                  <th className="pb-1 text-right font-normal">多头周增减</th>
                  <th className="pb-1 text-right font-normal">投机指数</th>
                </tr>
              </thead>
              <tbody>
                {obs.cot.slice(0, 10).map((r, i) => (
                  <tr key={`${r.asset}-${i}`} className="border-t border-border/60">
                    <td className="truncate py-1" title={r.contract}>{r.asset}</td>
                    <td className="py-1 text-right text-muted-foreground">{r.comm_net == null ? '—' : r.comm_net.toLocaleString()}</td>
                    <td className="py-1 text-right text-muted-foreground">{r.noncomm_net == null ? '—' : r.noncomm_net.toLocaleString()}</td>
                    <td className={`py-1 text-right ${(r.chg_noncomm_long ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>
                      {r.chg_noncomm_long == null ? '—' : `${r.chg_noncomm_long > 0 ? '+' : ''}${r.chg_noncomm_long.toLocaleString()}`}
                    </td>
                    <td className="py-1 text-right text-muted-foreground">{r.spec_index == null ? '—' : r.spec_index.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-2 font-mono text-[11px] text-muted-foreground">
              周频数据（CFTC 每周五发布），报告日 {obs.cot[0]?.report_date ?? '—'}
            </div>
          </section>
        </div>
      )}

      {sources && sources.length === 0 && (
        <div className="font-mono text-xs text-muted-foreground">暂无数据源（后端尚未上报状态）</div>
      )}

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="信源质量能力"
        hint="新闻密度突增 / 信源分歧 / 信源记分卡"
        featureIds={['F29','F30','F49']}
      />
      </section>
    </main>
  )
}