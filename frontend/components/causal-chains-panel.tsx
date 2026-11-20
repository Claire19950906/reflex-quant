'use client'

import { useCallback, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'
import { useToast } from '@/components/ui/toast'

// 因果链库 — 宏观/市场因果知识条目。v176.3 起 [因果链分析] 分析环节已删除，
// 因果链库不再注入分析流程，仅作知识库维护（数据保留）。
// 每条：因(cause) → 果(effect)；trigger_words 为命中的触发词（历史注入逻辑已停用）。

type ChainEvidence = {
  n_outcomes?: number
  hit_rate?: number
  ci_low?: number
  ci_high?: number
  base_rate?: number | null
  lift?: number | null
  p_value?: number | null
  tier?: string
  mag_n?: number | null
  mag_hit_rate?: number | null
}

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
  // v417: 证据字段（后端 list_chains 已下发）
  asset?: string
  expected_direction?: string
  hit_count?: number
  miss_count?: number
  confidence?: number
  last_verified_ts?: number
  tier?: string
  evidence?: ChainEvidence
}

// 档位口径（与后端 chain_prior 同一套）：
//   strong     = 样本≥12 且跑赢同品种同向基准≥10pp、p≤0.05 → 可作方向依据
//   strong_mag = BOTH 幅度链达标 → 只说明"追不追"，永不可定向
//   weak       = 3~11 样本 → 只能当解释假设
//   none       = 无样本
const TIER_META: Record<string, { label: string; cls: string; tip: string }> = {
  strong: {
    label: '方向已验证',
    cls: 'border-long/50 bg-long/10 text-long',
    tip: '样本≥12 且命中率跑赢同品种同向基准≥10pp、p≤0.05：这条可以作为方向依据',
  },
  strong_mag: {
    label: '幅度已验证',
    cls: 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400',
    tip: '幅度/胜率有统计支撑，只说明"这种局面追不追"，不能据此定方向',
  },
  weak: {
    label: '样本不足',
    cls: 'border-border bg-secondary text-muted-foreground',
    tip: '3~11 个样本：只能作为候选解释，不得据此否决或加仓',
  },
  none: {
    label: '无样本',
    cls: 'border-border bg-secondary text-muted-foreground',
    tip: '从未被触发评估过，没有任何证据',
  },
}

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`)
const pp = (v?: number | null) =>
  v == null ? '—' : `${v >= 0 ? '+' : ''}${Math.round(v * 100)}pp`

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
    if (!form.trigger_words.trim()) {
      setError('必须至少给一个触发词 —— 没有触发词的链永远不会被触发，也就永远无法验证')
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
      const res = (await r.json()) as { ok?: boolean; error?: string; warning?: string; enabled?: boolean }
      if (!res.ok) throw new Error(res.error || '保存失败')
      // v418: 后端判不出品种会自动停用（那条链永远不会被触发），必须让填的人当场知道，
      // 否则他会以为自己加了一条生效链。
      if (res.warning) {
        setNotice(res.warning)
      } else {
        setNotice(editingId ? '因果链已更新（AI ≤5s 生效）' : '因果链已添加（AI ≤5s 生效）')
      }
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
  // v418: 死链 —— 没品种或没触发词。评估器见到它们直接跳过，永远是"零样本占位"，
  // 但会稀释"778 条链"这个分母（实测 308 条，其中 288 条是自动建链漏写 asset 造成的）。
  const DEAD_ASSETS = ['', 'NONE', 'NULL', 'UNKNOWN', 'MACRO']
  const isDead = (c: { asset?: string; trigger_words?: string }) =>
    DEAD_ASSETS.includes((c.asset || '').toUpperCase()) || !(c.trigger_words || '').trim()
  const deadCount = chains.filter(isDead).length
  const usable = chains.length - deadCount
  // v417: 默认只显示生效链 —— 旧列表把 659 条停用/冗余链一起铺开，真正在跑的
  // 119 条被埋在里面，等于"看不见有用的那部分"。
  const [onlyEnabled, setOnlyEnabled] = useState(true)
  const visible = onlyEnabled ? active : chains
  const strongCount = active.filter((c) => c.tier === 'strong').length
  const strongMagCount = active.filter((c) => c.tier === 'strong_mag').length
  const sampled = chains.filter((c) => (c.evidence?.n_outcomes ?? 0) > 0).length

  return (
    <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4">
      {/* ── 说明 ── */}
      <section className="surface-card p-3">
        <div className="flex items-center gap-2">
          <h2 className="font-mono text-sm font-bold tracking-widest text-foreground">因果链库</h2>
          <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[11px] text-primary">因果链</span>
          <button
            type="button"
            onClick={() => setOnlyEnabled(!onlyEnabled)}
            className="ml-auto rounded border border-border bg-card px-2 py-0.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
            title="默认只看生效链；切换后连停用/冗余链一起显示"
          >
            {onlyEnabled ? '只看生效' : '显示全部'}
          </button>
          <span className="font-mono text-[11px] text-muted-foreground">
            {active.length}/{usable} 生效 · 废链 {deadCount} · 方向达标 <span className="text-long">{strongCount}</span> · 幅度达标 {strongMagCount} · 有样本 {sampled}
          </span>
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          存宏观/市场因果知识（<b className="text-foreground">因 → 果</b>）。链会以「链级已验证证据」注入 AI 分析：
          只有<b className="text-long">跑赢同品种同向基准 ≥10pp 且 p≤0.05</b> 的链（样本≥12）才能作为方向依据，
          其余只当候选解释。绝对命中率不算数 —— 行情自己会漂。
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
            className="ml-auto rounded border border-border bg-card px-2 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
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
          {visible.map((item) => (
            <div key={item.id} className={`surface-card p-3 ${item.enabled ? '' : 'opacity-60'}`}>
              <div className="flex items-center gap-2">
                <span className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${item.enabled ? 'border-long/40 bg-long/10 text-long' : 'border-border bg-secondary text-muted-foreground'}`}>
                  {item.enabled ? '生效' : '停用'}
                </span>
                {(() => {
                  const meta = TIER_META[item.tier || 'none'] || TIER_META.none
                  return (
                    <span className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${meta.cls}`} title={meta.tip}>
                      {meta.label}
                    </span>
                  )
                })()}
                {item.asset && (
                  <span className="rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                    {item.asset}
                  </span>
                )}
                {item.expected_direction && item.expected_direction !== 'BOTH' && (
                  <span className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${item.expected_direction === 'UP' ? 'border-long/40 text-long' : 'border-short/40 text-short'}`}>
                    {item.expected_direction}
                  </span>
                )}
                {(() => {
                  const meta = ORIGIN_META[item.origin || 'manual'] || ORIGIN_META.manual
                  return (
                    <span className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${meta.cls}`} title={meta.tip}>
                      {meta.label}
                    </span>
                  )
                })()}
                {item.name && (
                  <span className="font-mono text-xs font-bold text-foreground">{item.name}</span>
                )}
                {item.trigger_words && (
                  <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground" title="触发词：新闻命中才注入该链">
                    触发: {item.trigger_words}
                  </span>
                )}
                <span className="ml-auto flex items-center gap-1.5">
                  <span className="flex items-center gap-1" title="强度参数：该链命中新闻时在 AI 分析中的占比，越大越侧重（范围 0-100，默认 1）">
                    <span className="font-mono text-[11px] text-muted-foreground">
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
                      className="w-14 rounded border border-border bg-card px-1 py-0.5 text-right font-mono text-[11px] text-foreground"
                    />
                  </span>
                  <button type="button" onClick={() => toggle(item)}
                    className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[11px] text-muted-foreground hover:text-foreground">
                    {item.enabled ? '停用' : '启用'}
                  </button>
                  <button type="button" onClick={() => edit(item)}
                    className="rounded border border-primary/40 bg-card px-2 py-0.5 font-mono text-[11px] text-primary hover:bg-primary/10">
                    编辑
                  </button>
                  <button type="button" onClick={() => void remove(item)}
                    className="rounded border border-short/40 bg-card px-2 py-0.5 font-mono text-[11px] text-short hover:bg-short/10">
                    删除
                  </button>
                </span>
              </div>
              <div className="mt-2 space-y-1 text-xs">
                <div className="rounded bg-secondary/30 px-2 py-1">
                  <span className="font-mono text-[11px] text-muted-foreground">因：</span>
                  <span className="text-foreground/90">{item.cause}</span>
                </div>
                <div className="rounded bg-secondary/30 px-2 py-1">
                  <span className="font-mono text-[11px] text-muted-foreground">果：</span>
                  <span className="text-foreground/90">{item.effect}</span>
                </div>
                {item.note && (
                  <div className="px-2 py-0.5 text-[11px] text-muted-foreground">备注：{item.note}</div>
                )}
                {/* v417: 证据条 —— 一条链准不准，必须在这里能看见 */}
                {(() => {
                  const ev = item.evidence
                  const n = ev?.n_outcomes ?? 0
                  const hits = Math.round((ev?.hit_rate ?? 0) * n)
                  const hasBase = ev?.base_rate != null && ev?.lift != null
                  return (
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded bg-secondary/30 px-2 py-1 font-mono text-[11px] text-muted-foreground">
                      <span>
                        样本 <span className="text-foreground">n={n}</span>
                      </span>
                      {n > 0 ? (
                        <>
                          <span>
                            命中 <span className="text-foreground">{hits}/{n} = {pct(ev?.hit_rate)}</span>
                          </span>
                          <span title="Wilson 95% 区间：点估计的精度，不是精确值">
                            区间 {pct(ev?.ci_low)}–{pct(ev?.ci_high)}
                          </span>
                          {hasBase && (
                            <>
                              <span title="同品种同方向“闭眼猜”能蒙对的概率（近 60 天 5 日窗口）">
                                基准 {pct(ev?.base_rate)}
                              </span>
                              <span
                                className={((ev?.lift ?? 0) >= 0.1 ? 'text-long' : 'text-foreground')}
                                title="超额 = 命中率 − 基准率；≥10pp 且 p≤0.05 才算跑赢"
                              >
                                超额 {pp(ev?.lift)}
                              </span>
                              <span>p={ev?.p_value == null ? '—' : Number(ev.p_value).toFixed(3)}</span>
                            </>
                          )}
                        </>
                      ) : (
                        <span className="text-muted-foreground/70">从未被触发评估 —— 无任何统计证据</span>
                      )}
                      <span title="Beta(2,2) 后验均值，供排序参考">
                        置信 {(item.confidence ?? 0).toFixed(2)}
                      </span>
                    </div>
                  )
                })()}
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  )
}
