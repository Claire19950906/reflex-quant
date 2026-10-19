'use client'

import { useEffect, useState, useCallback } from 'react'
import { Loader2, RefreshCcw, Wrench, Check, X, AlertTriangle, Zap } from 'lucide-react'
import { apiGet, apiSend } from '@/lib/api'
import { cn } from '@/lib/utils'

// v232.6 P1: 反思 -> policy 闭环 - 候选补丁面板
// 数据源: /api/policy/patches + /api/policy/sibling-rules
// 流程: reflection worker 产出 pending 候选 -> 人审 -> approved -> 未来 P2 apply 到 ai_worker
// 当前 P1 阶段只展示 + approve/reject,不自动注入 ai_worker prompt (保守起见)

type PolicyPatch = {
  id: number
  patch_type: string
  target_module: string
  payload: Record<string, any>
  source_kind: string
  source_reflection_ids: number[]
  source_error_codes: string[]
  sample_size: number
  cumulative_pnl_impact: number
  confidence_score: number
  rationale: string
  status: 'pending' | 'approved' | 'rejected' | 'applied' | 'expired'
  created_at: number
  reviewed_at: number
  reviewer_note: string
  applied_count: number
}

type SiblingRule = {
  id: number
  rule_signature: string
  fingerprint: string
  asset: string
  shared_features: Record<string, [number, number]>
  winner_only_features: Record<string, number>
  loser_only_features: Record<string, number>
  n_pairs: number
  consistency_score: number
  confidence_score: number
  pnl_gap_avg: number
  status: string
}

const PATCH_TYPE_LABELS: Record<string, string> = {
  prompt_addition: 'AI prompt 补丁',
  threshold_change: '风控阈值调整',
  rule_addition: '新增 gate 规则',
  pattern_warning: '反思模板警告',
}
const PATCH_TYPE_COLORS: Record<string, string> = {
  prompt_addition: 'bg-purple-50 text-purple-700 border-purple-200',
  threshold_change: 'bg-amber-50 text-amber-700 border-amber-200',
  rule_addition: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  pattern_warning: 'bg-sky-50 text-sky-700 border-sky-200',
}
const STATUS_LABELS: Record<string, string> = {
  pending: '待审', approved: '已批准', rejected: '已驳回',
  applied: '已应用', expired: '已过期',
}
const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-emerald-100 text-emerald-800',
  rejected: 'bg-zinc-100 text-zinc-600',
  applied: 'bg-blue-100 text-blue-800',
  expired: 'bg-zinc-50 text-zinc-400',
}

export default function PolicyPatchesPanel() {
  const [patches, setPatches] = useState<PolicyPatch[]>([])
  const [siblings, setSiblings] = useState<SiblingRule[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<'all' | 'pending' | 'approved' | 'rejected' | 'expired'>('pending')
  const [running, setRunning] = useState(false)
  const [reviewing, setReviewing] = useState<number | null>(null)
  const [reviewNote, setReviewNote] = useState('')

  const load = useCallback(async () => {
    try {
      const [pRes, sRes] = await Promise.all([
        apiGet<PolicyPatch[] | PolicyPatch>('/policy/patches?limit=100'),
        apiGet<SiblingRule[] | SiblingRule>('/policy/sibling-rules?limit=20'),
      ])
      setPatches(Array.isArray(pRes) ? pRes : [])
      setSiblings(Array.isArray(sRes) ? sRes : [])
    } catch {
      // silent
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const triggerCycle = async () => {
    setRunning(true)
    try {
      await apiSend('/policy/patches/run?window_days=30&do_expire=false', 'POST')
      await load()
    } finally { setRunning(false) }
  }

  const review = async (id: number, action: 'approve' | 'reject') => {
    setReviewing(id)
    try {
      await apiSend(`/policy/patches/${id}/review?action=${action}`, 'POST', { note: reviewNote || '' })
      setReviewNote('')
      await load()
    } finally { setReviewing(null) }
  }

  const filtered = filter === 'all' ? patches : patches.filter(p => p.status === filter)

  const stats = {
    pending: patches.filter(p => p.status === 'pending').length,
    approved: patches.filter(p => p.status === 'approved').length,
    rejected: patches.filter(p => p.status === 'rejected').length,
    expired: patches.filter(p => p.status === 'expired').length,
    total_pnl: patches.filter(p => p.status === 'pending').reduce((s, p) => s + p.cumulative_pnl_impact, 0),
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-zinc-500 py-8 justify-center">
        <Loader2 className="h-4 w-4 animate-spin" />
        加载策略补丁候选…
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header + actions */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Wrench className="h-4 w-4 text-zinc-700" />
            <h3 className="font-medium text-sm text-zinc-800">反思 → 策略补丁候选 (P1 闭环)</h3>
          </div>
          <p className="text-xs text-zinc-500 mt-1">
            反思 worker 产出的可审核补丁。仅展示 + 人工审,本阶段不会自动注入 ai_worker prompt。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={triggerCycle}
            disabled={running}
            className="inline-flex items-center gap-1.5 rounded border border-zinc-300 px-2.5 py-1 text-xs hover:bg-zinc-50 disabled:opacity-50"
          >
            {running ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
            跑一轮
          </button>
          <button
            type="button"
            onClick={load}
            className="inline-flex items-center gap-1.5 rounded border border-zinc-300 px-2.5 py-1 text-xs hover:bg-zinc-50"
          >
            <RefreshCcw className="h-3 w-3" />
            刷新
          </button>
        </div>
      </div>

      {/* Stats bar */}
      <div className="grid grid-cols-5 gap-2">
        {[
          { key: 'pending', label: '待审', value: stats.pending, color: 'text-yellow-700' },
          { key: 'approved', label: '已批准', value: stats.approved, color: 'text-emerald-700' },
          { key: 'rejected', label: '已驳回', value: stats.rejected, color: 'text-zinc-600' },
          { key: 'expired', label: '已过期', value: stats.expired, color: 'text-zinc-400' },
          { key: 'pnl', label: '待审累计 pnl', value: `${(stats.total_pnl * 100).toFixed(1)}%`, color: stats.total_pnl < 0 ? 'text-red-600' : 'text-emerald-600' },
        ].map(s => (
          <button
            key={s.key}
            type="button"
            onClick={() => setFilter(s.key === 'pnl' ? 'pending' : (s.key as any))}
            className={cn(
              'rounded border px-3 py-2 text-left transition-colors',
              filter === s.key ? 'border-zinc-400 bg-zinc-50' : 'border-zinc-200 hover:bg-zinc-50'
            )}
          >
            <div className="text-[10px] uppercase text-zinc-500 tracking-wider">{s.label}</div>
            <div className={cn('text-lg font-medium', s.color)}>{s.value}</div>
          </button>
        ))}
      </div>
      {/* Filter row */}
      <div className="flex items-center gap-2 text-xs flex-wrap">
        <span className="text-zinc-500">筛选:</span>
        {(['all', 'pending', 'approved', 'rejected', 'expired'] as const).map(k => (
          <button
            key={k}
            type="button"
            onClick={() => setFilter(k)}
            className={cn(
              'rounded-full px-2.5 py-0.5 border transition-colors',
              filter === k ? 'bg-zinc-800 text-white border-zinc-800' : 'border-zinc-300 text-zinc-600 hover:bg-zinc-50'
            )}
          >
            {k === 'all' ? '全部' : STATUS_LABELS[k]}
          </button>
        ))}
        {filter === 'pending' && patches.some(p => p.status === 'pending') && (
          <input
            type="text"
            placeholder="审核备注 (可选)"
            value={reviewNote}
            onChange={e => setReviewNote(e.target.value)}
            className="ml-2 flex-1 rounded border border-zinc-300 px-2 py-0.5 text-xs min-w-[200px]"
          />
        )}
      </div>

      {/* Patches list */}
      <div className="space-y-2">
        {filtered.length === 0 && (
          <div className="text-sm text-zinc-500 py-8 text-center border border-dashed rounded">
            {filter === 'pending' ? '当前没有待审候选。点击「跑一轮」生成。' : '暂无该状态的补丁。'}
          </div>
        )}
        {filtered.map(p => (
          <div key={p.id} className="border border-zinc-200 rounded p-3 space-y-2">
            <div className="flex items-start justify-between gap-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className={cn('rounded px-1.5 py-0.5 text-[10px] border', PATCH_TYPE_COLORS[p.patch_type] || 'bg-zinc-50')}>
                  {PATCH_TYPE_LABELS[p.patch_type] || p.patch_type}
                </span>
                <span className="rounded px-1.5 py-0.5 text-[10px] bg-zinc-100 text-zinc-600 border border-zinc-200">
                  → {p.target_module}
                </span>
                <span className={cn('rounded px-1.5 py-0.5 text-[10px]', STATUS_COLORS[p.status] || 'bg-zinc-100')}>
                  {STATUS_LABELS[p.status] || p.status}
                </span>
                {p.source_error_codes.length > 0 && (
                  <span className="rounded px-1.5 py-0.5 text-[10px] bg-red-50 text-red-700 border border-red-200">
                    错误码: {p.source_error_codes.join(', ')}
                  </span>
                )}
              </div>
              <div className="text-[10px] text-zinc-500 text-right shrink-0">
                <div>样本 {p.sample_size} · 置信 {p.confidence_score.toFixed(3)}</div>
                <div className={cn(p.cumulative_pnl_impact < 0 ? 'text-red-600' : 'text-emerald-600')}>
                  累计 pnl {p.cumulative_pnl_impact >= 0 ? '+' : ''}{(p.cumulative_pnl_impact * 100).toFixed(2)}%
                </div>
              </div>
            </div>
            <div className="text-xs text-zinc-700 leading-relaxed">
              <span className="text-zinc-500">理由:</span> {p.rationale || '(空)'}
            </div>
            {p.payload.addition && (
              <div className="text-[11px] text-zinc-600 bg-zinc-50 rounded p-2 border border-zinc-100 whitespace-pre-wrap font-mono">
                {p.payload.addition}
              </div>
            )}
            {p.payload.threshold_name && (
              <div className="text-[11px] text-zinc-600 bg-amber-50 rounded p-2 border border-amber-100">
                <span className="font-mono">{p.payload.threshold_name}</span>:{' '}
                <span className="text-zinc-500">{p.payload.old_value}</span>
                {' → '}
                <span className="font-medium text-amber-700">{p.payload.new_value}</span>
                <span className="text-zinc-500"> ({p.payload.duration_days}d)</span>
              </div>
            )}
            {p.payload.suggested_gate && (
              <div className="text-[11px] text-zinc-600 bg-emerald-50 rounded p-2 border border-emerald-100 font-mono">
                {p.payload.suggested_gate}
              </div>
            )}
            {p.reviewer_note && (
              <div className="text-[11px] text-zinc-500 border-t border-zinc-100 pt-1">
                <span className="text-zinc-400">审核:</span> {p.reviewer_note}
              </div>
            )}
            {p.status === 'pending' && (
              <div className="flex items-center gap-2 pt-1 border-t border-zinc-100">
                <button
                  type="button"
                  onClick={() => review(p.id, 'approve')}
                  disabled={reviewing === p.id}
                  className="inline-flex items-center gap-1 rounded bg-emerald-600 px-2 py-1 text-xs text-white hover:bg-emerald-700 disabled:opacity-50"
                >
                  <Check className="h-3 w-3" /> 批准
                </button>
                <button
                  type="button"
                  onClick={() => review(p.id, 'reject')}
                  disabled={reviewing === p.id}
                  className="inline-flex items-center gap-1 rounded bg-white border border-zinc-300 px-2 py-1 text-xs text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
                >
                  <X className="h-3 w-3" /> 驳回
                </button>
                <span className="text-[10px] text-zinc-400 ml-auto">
                  批准后才会被下游 P2 apply 链路消费
                </span>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Sibling diff rules section */}
      {siblings.length > 0 && (
        <details className="border border-zinc-200 rounded">
          <summary className="px-3 py-2 text-xs text-zinc-700 cursor-pointer hover:bg-zinc-50 flex items-center gap-2">
            <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
            兄弟决策差分规则 ({siblings.length}) — 同信号不同结果的真实因果特征
          </summary>
          <div className="border-t border-zinc-200 p-3 space-y-2">
            <p className="text-[11px] text-zinc-500 leading-relaxed">
              控制 content_fingerprint 相同，提取赢家与输家在数值/类别特征上的差异。
              n_pairs ≥ 3 才升级为 policy_patch_queue 候选。当前 P1 仅展示。
            </p>
            {siblings.slice(0, 10).map(r => (
              <div key={r.id} className="border border-zinc-100 rounded p-2 text-[11px] space-y-1">
                <div className="flex items-center gap-2">
                  <span className="rounded px-1.5 py-0.5 bg-zinc-100 font-mono">{r.asset}</span>
                  <span className="text-zinc-500">fp={r.fingerprint.slice(0, 12)}…</span>
                  <span className="ml-auto text-zinc-400">
                    pairs={r.n_pairs} · conf={r.confidence_score.toFixed(3)} · pnl_gap={(r.pnl_gap_avg * 100).toFixed(2)}%
                  </span>
                </div>
                {Object.keys(r.winner_only_features).length > 0 && (
                  <div className="text-emerald-700">
                    WIN: {Object.keys(r.winner_only_features).slice(0, 4).join(', ')}
                  </div>
                )}
                {Object.keys(r.loser_only_features).length > 0 && (
                  <div className="text-red-700">
                    LOSS: {Object.keys(r.loser_only_features).slice(0, 4).join(', ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
