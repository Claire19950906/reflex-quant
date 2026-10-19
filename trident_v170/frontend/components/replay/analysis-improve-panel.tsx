'use client'

import { useCallback, useEffect, useState, type ComponentType } from 'react'
import { useSearchParams } from 'next/navigation'
import { Network, CheckCircle2, XCircle, Sparkles, ArrowUpRight, ArrowDownRight, BookOpen, GitBranch, Filter } from 'lucide-react'
import { apiGet, apiSend } from '@/lib/api'
import { cn } from '@/lib/utils'
import { DataState } from '@/components/ui/data-state'

/**
 * 分析改进计划 · 因果图谱
 *
 * 用途：与「模拟盘的反思改进计划」（改 paper-trading 参数）严格区分，
 * 本面板专管分析层级的改进：因果图谱（5-why）/ 因果链库（人工硬规则）
 * 这两块因果图谱的「加边 / 删边 / 改权重 / 启停规则」。
 *
 * 任何被批准的改动都写回因果图谱（causal_graph.json / causal_chains.json），
 * 而不是 paper-trading params。
 */

type KgProposalKind = 'add_edge' | 'remove_edge' | 'reweight' | 'enable_rule' | 'disable_rule'
type KgProposalStatus = 'pending' | 'approved' | 'rejected'

type KgProposal = {
  id: string | number
  kind: KgProposalKind
  target: string
  reason: string
  confidence: number
  sample_size: number
  impact: string
  status: KgProposalStatus
  first_seen_at: string | null
  last_seen_at: string | null
  note?: string
}

type KgStats = {
  graph_edges: number
  graph_nodes: number
  chains_total: number
  chains_enabled: number
  chains_disabled: number
  last_update_at: string | null
}

export function AnalysisImprovePanel() {
  const [open, setOpen] = useState(false)
  const [proposals, setProposals] = useState<KgProposal[]>([])
  const [stats, setStats] = useState<KgStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [actingId, setActingId] = useState<string | number | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // v302+ 跨页 deep-link: /discover ⭐ 入链后跳到 /review#improve?prefilter=from-discover&cause=&effect=
  const searchParams = useSearchParams()
  const prefilter = searchParams?.get('prefilter') || ''
  const filterCause = (searchParams?.get('cause') || '').trim()
  const filterEffect = (searchParams?.get('effect') || '').trim()
  const isPrefiltered = prefilter === 'from-discover' && (filterCause || filterEffect)
  const [prefilterDismissed, setPrefilterDismissed] = useState(false)
  const showPrefilterBanner = isPrefiltered && !prefilterDismissed && open

  // v302+ deep-link: 携带 prefilter=from-discover 自动展开面板
  useEffect(() => {
    if (isPrefiltered) {
      setOpen(true)
      setPrefilterDismissed(false)
    }
  }, [isPrefiltered])

  const refresh = useCallback(async () => {
    setErr(null)
    setLoading(true)
    try {
      const [propRes, statRes] = await Promise.all([
        apiGet<KgProposal[]>('/analysis/kg-improvements?status=pending&limit=20')
          .catch(() => [] as KgProposal[]),
        apiGet<KgStats | null>('/analysis/kg-stats')
          .catch(() => null),
      ])
      setProposals(Array.isArray(propRes) ? propRes : [])
      setStats(statRes && typeof statRes === 'object' ? statRes : null)
    } catch (e) {
      setErr(`拉取失败：${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    void refresh()
  }, [open, refresh])

  const actOn = async (id: string | number, decision: 'approve' | 'reject') => {
    setActingId(id)
    try {
      await apiSend(`/analysis/kg-improvements/${id}/${decision}`, 'POST')
      void refresh()
    } catch {
      /* 后端未上线时静默 */
    } finally {
      setActingId(null)
    }
  }

  const pendingCount = proposals.filter((p) => p.status === 'pending').length

  // v302+ prefilter=from-discover 时, 在列表里只保留 target 或 reason 命中 cause/effect 的行;
  // 完全没命中则显示「没有相关候选」提示但仍展示面板（让人工自己决定补提 ad-hoc）。
  const filteredProposals = (() => {
    if (!showPrefilterBanner) return proposals
    if (!filterCause && !filterEffect) return proposals
    const c = filterCause.toLowerCase()
    const e = filterEffect.toLowerCase()
    return proposals.filter((p) => {
      const blob = (p.target + ' ' + p.reason + ' ' + (p.note || '')).toLowerCase()
      return (c && blob.includes(c)) || (e && blob.includes(e))
    })
  })()

  return (
    <div className="rounded border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-secondary/30"
      >
        <span className="font-mono text-[11px] text-muted-foreground">{open ? '▾' : '▸'}</span>
        <Network className="h-3.5 w-3.5 text-primary" />
        <span className="font-mono text-xs font-bold uppercase tracking-widest text-foreground">分析改进计划 · 因果图谱</span>
        <span className="ml-2 font-mono text-[10px] text-muted-foreground">
          改的是因果图谱（因果图谱 / 因果链库），不是模拟盘参数
        </span>
        {!open && pendingCount > 0 && (
          <span className="ml-auto rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-primary">
            {pendingCount} 项待审
          </span>
        )}
      </button>

      {open && (
        <div className="border-t border-border p-3 space-y-3">
          {err && (
            <div className="rounded border border-short/40 bg-short/10 px-3 py-1.5 text-xs text-short">{err}</div>
          )}
          {showPrefilterBanner && (
            <div className="flex flex-wrap items-center gap-2 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 font-mono text-[11px] text-amber-700 dark:text-amber-300">
              <Filter className="h-3.5 w-3.5" />
              <span className="font-bold">来自发现页 · 待关联</span>
              {filterCause && (
                <span className="rounded bg-amber-500/15 px-1.5 py-0.5">因：{filterCause}</span>
              )}
              {filterEffect && (
                <span className="rounded bg-amber-500/15 px-1.5 py-0.5">果：{filterEffect}</span>
              )}
              <button
                type="button"
                onClick={() => setPrefilterDismissed(true)}
                className="ml-auto rounded border border-amber-500/40 bg-amber-500/15 px-1.5 py-0.5 text-[10px] hover:bg-amber-500/25"
                title="清除预过滤，显示全部候选"
              >
                ✕ 清除预过滤
              </button>
            </div>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void refresh()}
              className="rounded border border-border bg-secondary px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
            <a
              href="/knowledge-graph"
              className="inline-flex items-center gap-1 rounded border border-border bg-secondary px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
            >
              <GitBranch className="h-3 w-3" />
              打开因果图谱
            </a>
            <span className="ml-1 font-mono text-[10px] text-muted-foreground">
              范围边界：本面板的「批准」只改因果图谱（causal_graph / causal_chains），不联动模拟盘 gate / take / stop / track。
            </span>
          </div>

          {stats && (
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <KgStatBlock label="证据图谱节点" value={stats.graph_nodes} icon={<BookOpen className="h-3.5 w-3.5" />} />
              <KgStatBlock label="证据图谱边" value={stats.graph_edges} icon={<Network className="h-3.5 w-3.5" />} />
              <KgStatBlock
                label="硬规则启用/总"
                value={`${stats.chains_enabled}/${stats.chains_total}`}
                icon={<Sparkles className="h-3.5 w-3.5" />}
                tone={stats.chains_disabled > 0 ? 'warn' : 'ok'}
              />
              <KgStatBlock
                label="最近更新"
                value={stats.last_update_at ? new Date(stats.last_update_at).toLocaleString('zh-CN', { hour12: false }) : '—'}
                icon={<GitBranch className="h-3.5 w-3.5" />}
              />
            </div>
          )}

          <div>
            <div className="mb-1 flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              候选改进 · 因果图谱待审
              <span className="font-mono normal-case text-muted-foreground/80">
                （批准 → 写 causal_graph.json / causal_chains.json；驳回 → 留作存档）
              </span>
            </div>
            <DataState
              loading={loading && proposals.length === 0}
              loadingLabel="加载候选改进…"
              empty={!loading && filteredProposals.length === 0}
              emptyLabel={
                showPrefilterBanner && proposals.length > 0
                  ? `已预过滤「${filterCause}${filterEffect ? ' → ' + filterEffect : ''}」但无相关候选（${proposals.length} 条全部候选中）。可点上方「清除预过滤」查看全部，或手动批准此 cause/effect 进入因果链库。`
                  : '暂无候选改进。系统会从归因复核 / 决策错误码持续挖出「图谱需要增删的边 / 启停的规则」，发现后会出现在这里。'
              }
              data={filteredProposals}
            >
              {(list) => <ul className="space-y-1.5">
                {list.map((p) => (
                  <li
                    key={p.id}
                    className={cn(
                      'rounded border bg-secondary/30 px-2 py-1.5',
                      p.status === 'approved' && 'border-long/40',
                      p.status === 'rejected' && 'border-border opacity-60',
                      p.status === 'pending' && 'border-border',
                    )}
                  >
                    <div className="flex flex-wrap items-center gap-2 text-[11px]">
                      <KindBadge kind={p.kind} />
                      <span className="font-mono font-bold text-foreground">{p.target}</span>
                      <span className="font-mono text-muted-foreground">置信 {(p.confidence * 100).toFixed(0)}%</span>
                      <span className="font-mono text-muted-foreground">样本 {p.sample_size}</span>
                      <span className="font-mono text-muted-foreground">{p.impact}</span>
                      {p.status === 'pending' && (
                        <span className="ml-auto flex items-center gap-1">
                          <button
                            type="button"
                            disabled={actingId === p.id}
                            onClick={() => void actOn(p.id, 'approve')}
                            className="inline-flex items-center gap-0.5 rounded border border-long/40 bg-long/10 px-1.5 py-0.5 font-mono text-[10px] text-long hover:bg-long/20 disabled:opacity-50"
                          >
                            <CheckCircle2 className="h-3 w-3" /> 批准 · 写图谱
                          </button>
                          <button
                            type="button"
                            disabled={actingId === p.id}
                            onClick={() => void actOn(p.id, 'reject')}
                            className="inline-flex items-center gap-0.5 rounded border border-short/40 bg-short/10 px-1.5 py-0.5 font-mono text-[10px] text-short hover:bg-short/20 disabled:opacity-50"
                          >
                            <XCircle className="h-3 w-3" /> 驳回
                          </button>
                        </span>
                      )}
                      {p.status === 'approved' && (
                        <span className="ml-auto inline-flex items-center gap-1 font-mono text-[10px] text-long">
                          <CheckCircle2 className="h-3 w-3" /> 已写图谱
                        </span>
                      )}
                      {p.status === 'rejected' && (
                        <span className="ml-auto inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                          <XCircle className="h-3 w-3" /> 已驳回
                        </span>
                      )}
                    </div>
                    {p.reason && (
                      <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">{p.reason}</p>
                    )}
                    {actingId === p.id && <div className="mt-1 font-mono text-[10px] text-muted-foreground">处理中…</div>}
                  </li>
                ))}
              </ul>}
            </DataState>
          </div>
        </div>
      )}
    </div>
  )
}

function KindBadge({ kind }: { kind: KgProposalKind }) {
  const map: Record<KgProposalKind, { label: string; cls: string; Icon: ComponentType<{ className?: string }> }> = {
    add_edge: { label: '加边', cls: 'text-long border-long/40 bg-long/10', Icon: ArrowUpRight },
    remove_edge: { label: '删边', cls: 'text-short border-short/40 bg-short/10', Icon: ArrowDownRight },
    reweight: { label: '改权重', cls: 'text-amber-500 border-amber-500/40 bg-amber-500/10', Icon: Network },
    enable_rule: { label: '启规则', cls: 'text-primary border-primary/40 bg-primary/10', Icon: CheckCircle2 },
    disable_rule: { label: '禁规则', cls: 'text-muted-foreground border-border bg-secondary/40', Icon: XCircle },
  }
  const it = map[kind]
  return (
    <span className={cn('inline-flex items-center gap-0.5 rounded px-1 py-0.5 font-mono text-[10px] border', it.cls)}>
      <it.Icon className="h-3 w-3" />
      {it.label}
    </span>
  )
}

function KgStatBlock({
  label,
  value,
  icon,
  tone = 'ok',
}: {
  label: string
  value: string | number
  icon: React.ReactNode
  tone?: 'ok' | 'warn'
}) {
  return (
    <div
      className={cn(
        'rounded border bg-secondary/30 px-2 py-1.5',
        tone === 'warn' ? 'border-amber-500/30' : 'border-border',
      )}
    >
      <div className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className={cn('mt-0.5 font-mono text-sm font-bold', tone === 'warn' ? 'text-amber-500' : 'text-foreground')}>
        {value}
      </div>
    </div>
  )
}