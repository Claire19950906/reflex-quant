'use client'

import { useCallback, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'
import { useToast } from '@/components/ui/toast'

// 因果链库 — 宏观/市场因果知识条目。v176.3 起 [因果链分析] 分析环节已删除，
// 因果链库不再注入分析流程，仅作知识库维护（数据保留）。
// 每条：因(cause) → 果(effect)；trigger_words 为命中的触发词（历史注入逻辑已停用）。

type CausalChain = {
  id: number
  name: string
  cause: string
  effect: string
  trigger_words: string
  note: string
  enabled: number
  origin?: string
  weight?: number
  created_at: string
  updated_at: string
}

const EMPTY_FORM = { name: '', cause: '', effect: '', trigger_words: '', note: '' }

// 来源标注：manual=人工手动 / auto_real=历史交易复盘提炼 / auto_quick_sim=快速评测提炼 / auto_mixed=混合样本提炼
const ORIGIN_META: Record<string, { label: string; cls: string; tip: string }> = {
  manual: {
    label: '手动',
    cls: 'border-border bg-secondary text-muted-foreground',
    tip: '该链由人工手动添加',
  },
  auto_real: {
    label: '历史复盘',
    cls: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
    tip: '该链由 AI 复盘从真实持仓交易提炼沉淀',
  },
  auto_quick_sim: {
    label: '快速评测',
    cls: 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400',
    tip: '该链由 AI 复盘从快速评测数据提炼沉淀',
  },
  auto_mixed: {
    label: '混合提炼',
    cls: 'border-indigo-500/40 bg-indigo-500/10 text-indigo-500',
    tip: '该链由 AI 复盘合并真实持仓与快速评测样本提炼沉淀',
  },
}

export function CausalChainsPanel() {
  // v302+ P0: 替代散落的 window.confirm
  const { toast } = useToast()
  const [chains, setChains] = useState<CausalChain[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [showForm, setShowForm] = useState(false)

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/causal/chains`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`list ${r.status}`)
      setChains((await r.json()) as CausalChain[])
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 5s 轮询：后台/多端变更后自动更新（usePollRefresh：内置 mount flag + 安全清理，挂载时也自动调用一次）
  usePollRefresh(load, 5_000)

  const set = (key: keyof typeof EMPTY_FORM) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm((prev) => ({ ...prev, [key]: e.target.value }))

  const submit = async () => {
    if (!form.cause.trim() || !form.effect.trim()) {
      setError('因(cause)与果(effect)都不能为空')
      return
    }
    setSubmitting(true)
    setError(null)
    setNotice(null)
    try {
      const url = editingId
        ? `${API_BASE}/causal/chains/${editingId}`
        : `${API_BASE}/causal/chains`
      const r = await fetch(url, {
        method: editingId ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      })
      const res = (await r.json()) as { ok?: boolean; error?: string }
      if (!res.ok) throw new Error(res.error || '保存失败')
      setNotice(editingId ? '因果链已更新（AI ≤5s 生效）' : '因果链已添加（AI ≤5s 生效）')
      setForm(EMPTY_FORM)
      setEditingId(null)
      setShowForm(false)
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败')
    } finally {
      setSubmitting(false)
    }
  }

  const remove = async (item: CausalChain) => {
    // v302+ P0: 用 toast.confirm 替代 window.confirm
    const ok = await new Promise<boolean>((resolve) => {
      toast.confirm(`删除因果链「${item.name || item.cause}」？`, () => resolve(true), () => resolve(false))
    })
    if (!ok) return
    try {
      const r = await fetch(`${API_BASE}/causal/chains/${item.id}`, { method: 'DELETE' })
      const res = (await r.json()) as { ok?: boolean }
      if (!res.ok) throw new Error('删除失败')
      setNotice('已删除')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除失败')
    }
  }

  // 行内改权重：失焦/回车保存（权重 0-100，越大命中时占比越高）
  const saveWeight = async (item: CausalChain, raw: string) => {
    const w = Number(raw)
    if (!Number.isFinite(w) || w < 0 || w > 100) {
      setError('权重需为 0-100 的数字')
      await load()
      return
    }
    if (w === (item.weight ?? 1)) return
    try {
      const r = await fetch(`${API_BASE}/causal/chains/${item.id}/weight`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ weight: w }),
      })
      const res = (await r.json()) as { ok?: boolean; error?: string }
      if (!res.ok) throw new Error(res.error || '权重保存失败')
      setNotice(`权重已设为 ${w}（AI ≤5s 生效）`)
      setError(null)
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '权重保存失败')
      await load()
    }
  }

  const toggle = async (item: CausalChain) => {
    try {
      const r = await fetch(`${API_BASE}/causal/chains/${item.id}/toggle`, { method: 'PATCH' })
      const res = (await r.json()) as { ok?: boolean }
      if (!res.ok) throw new Error('切换失败')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '切换失败')
    }
  }

  const edit = (item: CausalChain) => {
    setEditingId(item.id)
    setForm({
      name: item.name || '',
      cause: item.cause || '',
      effect: item.effect || '',
      trigger_words: item.trigger_words || '',
      note: item.note || '',
    })
    setShowForm(true)
  }

  const active = chains.filter((c) => c.enabled)

  return (
    <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4">
      {/* ── 说明 ── */}
      <section className="surface-card p-3">
        <div className="flex items-center gap-2">
          <h2 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">因果链库</h2>
          <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">CAUSAL</span>
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">
            {active.length}/{chains.length} 条生效 · AI ≤5s 同步
          </span>
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          存宏观/市场因果知识（<b className="text-foreground">因 → 果</b>），如「美元加息/降息 → 黄金/原油/加密承压或走强」。
          v176.3 起 <b className="text-primary">[因果链分析]</b> 分析环节已删除，因果链库不再注入分析流程，仅作知识库维护。
        </p>
        {notice && <div className="mt-2 rounded border border-long/40 bg-long/10 px-2 py-1 text-[11px] text-long">{notice}</div>}
        {error && <div className="mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 text-[11px] text-short">{error}</div>}
      </section>

      {/* ── 新增 / 编辑表单 ── */}
      <section className="surface-card p-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs font-bold uppercase tracking-widest text-foreground">
            {editingId ? `编辑因果链 #${editingId}` : '新增因果链'}
          </span>
          <button
            type="button"
            onClick={() => { setShowForm(!showForm); if (showForm) { setEditingId(null); setForm(EMPTY_FORM) } }}
            className="ml-auto rounded border border-border bg-card px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
          >
            {showForm ? '收起' : '展开'}
          </button>
        </div>
        {showForm && (
          <div className="mt-3 flex flex-col gap-2">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              <label className="block">
                <span className="font-mono text-[11px] text-foreground">显示名（可选）</span>
                <input type="text" value={form.name} onChange={set('name')} placeholder="如：美元利率 → 大类资产"
                  className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground focus:border-primary focus:outline-none" />
              </label>
              <label className="block">
                <span className="font-mono text-[11px] text-foreground">触发词（逗号分隔，新闻命中才注入）</span>
                <input type="text" value={form.trigger_words} onChange={set('trigger_words')} placeholder="如：美联储,加息,降息,fomc,利率决议"
                  className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground focus:border-primary focus:outline-none" />
              </label>
            </div>
            <label className="block">
              <span className="font-mono text-[11px] text-foreground">因（cause）*</span>
              <textarea value={form.cause} onChange={set('cause')} rows={2} placeholder="如：美元加息/降息、美联储缩表/扩表、OPEC 减产/增产…"
                className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground focus:border-primary focus:outline-none" />
            </label>
            <label className="block">
              <span className="font-mono text-[11px] text-foreground">果（effect）*</span>
              <textarea value={form.effect} onChange={set('effect')} rows={2} placeholder="如：黄金/原油/加密承压或走强；美债收益率上行压制风险资产…"
                className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground focus:border-primary focus:outline-none" />
            </label>
            <label className="block">
              <span className="font-mono text-[11px] text-foreground">备注（可选，注入时给 AI 看）</span>
              <input type="text" value={form.note} onChange={set('note')} placeholder="传导逻辑/注意事项（可选）"
                className="mt-1 w-full rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground focus:border-primary focus:outline-none" />
            </label>
            <div className="flex items-center gap-2">
              <button type="button" onClick={() => void submit()} disabled={submitting}
                className="rounded border border-primary/50 bg-primary/10 px-3 py-1.5 font-mono text-xs text-primary disabled:cursor-not-allowed disabled:opacity-50">
                {submitting ? '保存中…' : editingId ? '更新' : '添加'}
              </button>
              {editingId && (
                <button type="button" onClick={() => { setEditingId(null); setForm(EMPTY_FORM); setShowForm(false) }}
                  className="rounded border border-border bg-card px-3 py-1.5 font-mono text-xs text-muted-foreground">
                  取消编辑
                </button>
              )}
            </div>
          </div>
        )}
      </section>

      {/* ── 列表 ── */}
      {loading && chains.length === 0 ? (
        <div className="p-6 text-center font-mono text-xs text-muted-foreground">加载中…</div>
      ) : chains.length === 0 ? (
        <div className="surface-card p-6 text-center font-mono text-xs text-muted-foreground">
          暂无因果链 —— 点上方「展开」新增一条（如：因=美元加息，果=黄金/原油/加密承压）
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {chains.map((item) => (
            <div key={item.id} className={`surface-card p-3 ${item.enabled ? '' : 'opacity-60'}`}>
              <div className="flex items-center gap-2">
                <span className={`rounded border px-1.5 py-0.5 font-mono text-[9px] ${item.enabled ? 'border-long/40 bg-long/10 text-long' : 'border-border bg-secondary text-muted-foreground'}`}>
                  {item.enabled ? '生效' : '停用'}
                </span>
                {(() => {
                  const meta = ORIGIN_META[item.origin || 'manual'] || ORIGIN_META.manual
                  return (
                    <span className={`rounded border px-1.5 py-0.5 font-mono text-[9px] ${meta.cls}`} title={meta.tip}>
                      {meta.label}
                    </span>
                  )
                })()}
                {item.name && (
                  <span className="font-mono text-xs font-bold text-foreground">{item.name}</span>
                )}
                {item.trigger_words && (
                  <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground" title="触发词：新闻命中才注入该链">
                    触发: {item.trigger_words}
                  </span>
                )}
                <span className="ml-auto flex items-center gap-1.5">
                  <span className="flex items-center gap-1" title="强度参数：该链命中新闻时在 AI 分析中的占比，越大越侧重（范围 0-100，默认 1）">
                    <span className="font-mono text-[9px] text-muted-foreground">
                      权重
                      <span className="text-muted-foreground/50">(0-100)</span>
                    </span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      step={0.5}
                      defaultValue={item.weight ?? 1}
                      onBlur={(e) => void saveWeight(item, e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                      }}
                      className="w-14 rounded border border-border bg-card px-1 py-0.5 text-right font-mono text-[10px] text-foreground"
                    />
                  </span>
                  <button type="button" onClick={() => toggle(item)}
                    className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground">
                    {item.enabled ? '停用' : '启用'}
                  </button>
                  <button type="button" onClick={() => edit(item)}
                    className="rounded border border-primary/40 bg-card px-2 py-0.5 font-mono text-[10px] text-primary hover:bg-primary/10">
                    编辑
                  </button>
                  <button type="button" onClick={() => void remove(item)}
                    className="rounded border border-short/40 bg-card px-2 py-0.5 font-mono text-[10px] text-short hover:bg-short/10">
                    删除
                  </button>
                </span>
              </div>
              <div className="mt-2 space-y-1 text-xs">
                <div className="rounded bg-secondary/30 px-2 py-1">
                  <span className="font-mono text-[10px] text-muted-foreground">因：</span>
                  <span className="text-foreground/90">{item.cause}</span>
                </div>
                <div className="rounded bg-secondary/30 px-2 py-1">
                  <span className="font-mono text-[10px] text-muted-foreground">果：</span>
                  <span className="text-foreground/90">{item.effect}</span>
                </div>
                {item.note && (
                  <div className="px-2 py-0.5 text-[11px] text-muted-foreground">备注：{item.note}</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  )
}
