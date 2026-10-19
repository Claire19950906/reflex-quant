'use client'

import { useEffect, useState } from 'react'
import { apiGet, apiSend } from '@/lib/api'
import { Loader2, Check, Pencil, X, ChevronDown, Search } from 'lucide-react'
import { useReasonCodes } from '@/lib/hooks/use-reason-codes'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'
import { DataState } from '@/components/ui/data-state'

// 归因复核面板 — pending 队列逐条「同意」/「纠错」，纠错且 code 不同会自动回灌经验规则。
// 数据源：GET /api/review/attributions?status=pending
// 写接口：POST /api/review/attributions/{id}/feedback {status, corrected_code, corrected_text, note}

// v170 P0+：归因码字典由 useReasonCodes hook 动态加载（后端 GET /review/reason-codes），
// 失败/未上线时回退 FALLBACK_REASON_NAMES（与后端 review_agent._REASON_NAMES 同步 23 类）。
// 此前硬编码 const 已删除 —— 后端加第 24 类前端不再失配。
// 所有调用点（in REASON_NAMES / REASON_NAMES[k] / Object.entries）保持不变：hook 返回的
// names 字段类型与原 const 一致，零 diff 接入。

type Attribution = {
  id: number
  source: string
  ref_id: number
  trade_id: number | null
  asset: string
  action: string
  verdict: string
  pnl_pct: number | null
  reason_code: number | null
  reason_text: string
  reasons_json: string
  core_impact: string
  human_status: string
  created_at: string
  failing_sources?: string[]  // v222: 失败源清单（数据缺失型归因具体哪个 feed 挂了）
}

type ReasonsJson = Array<{ code?: number; text?: string; is_primary?: boolean }>

function parseReasonsJson(s: string): ReasonsJson {
  if (!s) return []
  try {
    const arr = JSON.parse(s)
    if (Array.isArray(arr)) return arr
  } catch {
    // not JSON, ignore
  }
  return []
}

function fmtPct(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(2)}%`
}

function fmtTime(s: string): string {
  if (!s) return ''
  return s.replace('T', ' ').slice(0, 16)
}

// v188 P1-6: 诊断面板子组件 — 展示 decision_layer_signals 命中 + 反思 + 经验应用
function DiagnosisPanel({ data }: { data: any }) {
  if (!data) return null
  if (data.ok === false) {
    return (
      <div className="mt-1.5 rounded border border-short/40 bg-short/10 p-2 text-[10px] text-short">
        诊断加载失败：{data.error || 'unknown error'}
      </div>
    )
  }
  const d = data
  const dec = d.decision || {}
  const ref = d.reflection
  const hits = d.signals_hit || []
  const agg = d.signals_layer_total || []
  const apps = d.applied_experiences || []
  return (
    <div className="mt-1.5 space-y-1.5 rounded border border-primary/30 bg-primary/5 p-2 text-[10px]">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="rounded bg-primary/20 px-1.5 py-0.5 font-bold text-foreground">#{d.decision_id}</span>
        <span className="text-foreground">{dec.target_asset} {dec.suggested_action}</span>
        <span className="text-muted-foreground">{dec.is_correct}</span>
        <span className="text-muted-foreground">forward_pnl={dec.forward_pnl != null ? `${(Number(dec.forward_pnl) * 100).toFixed(2)}%` : '—'}</span>
        {d.missed_signal && <span className="rounded bg-short/30 px-1 py-0.5 text-short">missed_signal</span>}
        <span className="ml-auto text-muted-foreground">{fmtTime(dec.created_at)}</span>
      </div>
      {ref ? (
        <div className="rounded bg-card p-1.5">
          <div className="mb-0.5 font-bold text-foreground">反思 #{ref.id}</div>
          <div className="text-muted-foreground">层：{ref.layer} / {ref.sub_layer} / <span className="font-bold text-foreground">{ref.signal_name}</span></div>
          {ref.root_cause && <div className="text-muted-foreground">主因：{ref.root_cause.slice(0, 200)}</div>}
          {ref.lesson && <div className="text-muted-foreground">教训：{ref.lesson.slice(0, 200)}</div>}
          {ref.llm_depth_reflection && (
            <details className="mt-1 text-muted-foreground">
              <summary className="cursor-pointer hover:text-foreground">LLM 深度反思（展开）</summary>
              <div className="mt-1 whitespace-pre-wrap">{ref.llm_depth_reflection.slice(0, 800)}</div>
            </details>
          )}
          <div className="mt-0.5 text-muted-foreground">验证：{ref.verification || '—'}（窗口 {ref.verification_window || '—'}）</div>
        </div>
      ) : (
        <div className="rounded bg-card p-1.5 text-muted-foreground">无 trade_reflections_v2 记录</div>
      )}
      {hits.length > 0 && (
        <div className="rounded bg-card p-1.5">
          <div className="mb-0.5 font-bold text-foreground">decision_layer_signals 命中（{dec.target_asset} {dec.suggested_action}）</div>
          <div className="max-h-32 space-y-0.5 overflow-y-auto">
            {hits.map((h: any, i: number) => {
              const nt = Number(h.n_total || 0)
              const nl = Number(h.n_loss || 0)
              const nw = Number(h.n_win || 0)
              const lr = nt ? Math.round((nl / nt) * 1000) / 10 : null
              return (
                <div key={i} className="flex flex-wrap gap-1.5 text-muted-foreground">
                  <span>{h.layer}/{h.sub_layer}/{h.signal_name}</span>
                  <span className="text-foreground">n={nt}</span>
                  <span className="text-short">loss={nl}</span>
                  <span className="text-long">win={nw}</span>
                  {lr != null && <span className={lr > 60 ? 'text-short' : lr < 40 ? 'text-long' : 'text-foreground'}>loss率 {lr}%</span>}
                </div>
              )
            })}
          </div>
        </div>
      )}
      {agg.length > 0 && (
        <div className="rounded bg-card p-1.5">
          <div className="mb-0.5 font-bold text-foreground">同 (layer/sub_layer/signal_name) 跨 asset 聚合</div>
          <div className="max-h-24 space-y-0.5 overflow-y-auto">
            {agg.map((a: any, i: number) => (
              <div key={i} className="flex flex-wrap gap-1.5 text-muted-foreground">
                <span>{a.asset}/{a.action}</span>
                <span className="text-foreground">n={a.n_total}</span>
                <span className={a.loss_rate > 60 ? 'text-short' : a.loss_rate < 40 ? 'text-long' : 'text-foreground'}>loss率 {a.loss_rate}%</span>
              </div>
            ))}
          </div>
        </div>
      )}
      {apps.length > 0 && (
        <div className="rounded bg-card p-1.5">
          <div className="mb-0.5 font-bold text-foreground">应用经验（{apps.length}）</div>
          {apps.map((a: any, i: number) => (
            <div key={i} className="flex flex-wrap gap-1.5 text-muted-foreground">
              <span>#{a.experience_id}</span>
              <span className="text-foreground">{a.title}</span>
              <span className="text-muted-foreground">({a.kind})</span>
              <span className={a.llm_invoked ? 'text-long' : 'text-muted-foreground'}>{a.llm_invoked ? '✓ llm' : '○ llm'}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function AttributionReviewPanel() {
  const [filter, setFilter] = useState<'pending' | 'agree' | 'corrected'>('pending')
  const [items, setItems] = useState<Attribution[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [correctedCode, setCorrectedCode] = useState<number>(6)
  const [correctedText, setCorrectedText] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [actionMsg, setActionMsg] = useState<{ id?: number; kind: 'ok' | 'err'; text: string } | null>(null)
  // v188 P1-6: 归因页 ↔ decision_layer_signals 联动
  const [diagnosisOpenId, setDiagnosisOpenId] = useState<number | null>(null)
  const [diagnosisById, setDiagnosisById] = useState<Record<number, any>>({})
  const [diagnosisLoading, setDiagnosisLoading] = useState<number | null>(null)

  // v170 P0+：归因码字典（hook 加载；alias 保持调用点零 diff）
  const { names: REASON_NAMES } = useReasonCodes()

  const fetchDiagnosis = async (tradeId: number) => {
    if (diagnosisById[tradeId]) {
      setDiagnosisOpenId(diagnosisOpenId === tradeId ? null : tradeId)
      return
    }
    setDiagnosisLoading(tradeId)
    setDiagnosisOpenId(tradeId)
    try {
      const d = await apiGet<unknown>(`/v188/decision/${tradeId}/diagnosis`)
      setDiagnosisById((prev) => ({ ...prev, [tradeId]: d }))
    } catch (e) {
      setDiagnosisById((prev) => ({
        ...prev,
        [tradeId]: { ok: false, error: e instanceof Error ? e.message : String(e) },
      }))
    } finally {
      setDiagnosisLoading(null)
    }
  }

  const load = () => {
    setLoading(true)
    apiGet<Attribution[]>(`/review/attributions?status=${filter}&limit=100`)
      .then((d) => {
        setItems(d)
        setError(null)
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setLoading(false))
  }

  // v302 P0+：手写 setInterval → usePollRefresh（统一 alive flag + cleanup + deps 触发）
  usePollRefresh(load, 60_000, [filter])

  const submitFeedback = async (id: number, payload: Record<string, unknown>) => {
    setSubmitting(true)
    setActionMsg(null)
    try {
      const j = await apiSend<{ pushed_rule?: boolean }>(`/review/attributions/${id}/feedback`, 'POST', payload)
      const verb = payload.status === 'agree' ? '✓ 已确认' : '✗ 已纠错'
      const extra = j.pushed_rule ? '（code 变更已回灌经验规则）' : ''
      setActionMsg({ id, kind: 'ok', text: `${verb}${extra}` })
      setEditingId(null)
      load()
    } catch (e) {
      setActionMsg({ id, kind: 'err', text: `✗ 失败：${e instanceof Error ? e.message : String(e)}` })
    } finally {
      setSubmitting(false)
    }
  }

  const handleAgree = (id: number) => submitFeedback(id, { status: 'agree' })

  const openEdit = (item: Attribution) => {
    setEditingId(item.id)
    setCorrectedCode(item.reason_code && REASON_NAMES[item.reason_code] ? item.reason_code : 6)
    setCorrectedText('')
    setNote('')
    setActionMsg(null)
  }

  const handleCorrect = (id: number) => {
    if (!(correctedCode in REASON_NAMES)) {
      setActionMsg({ id, kind: 'err', text: '✗ code 必须在 1-23 范围内' })
      return
    }
    submitFeedback(id, {
      status: 'corrected',
      corrected_code: correctedCode,
      corrected_text: correctedText,
      note,
    })
  }
return (
    <section className="surface-card p-3">
      <div className="flex items-center gap-2">
        <h2 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">归因复核队列</h2>
        <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">REVIEW QUEUE</span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">共 {items.length} 条{loading && <Loader2 className="ml-1 inline h-3 w-3 animate-spin" />}</span>
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">逐条复核 AI 归因。「同意」= 标记 human_status=agree；「纠错」= 改写 reason_code，若 code 不同会自动回灌为经验规则（review_insights）。</p>
      {/* v180 P0+：数据通道统一口径 §6 — 标明源表 + 时区 */}
      <span className="mt-1 inline-block rounded border border-primary/30 bg-primary/10 px-1 py-0.5 font-mono text-[9px] text-primary">
        数据 = [真实] attribution_reviews · human_status={filter} · 北京时区
      </span>
      <div className="mt-2 inline-flex rounded border border-border bg-secondary/30 p-0.5">
        {(['pending', 'agree', 'corrected'] as const).map((f) => (
          <button key={f} type="button" onClick={() => { setFilter(f); setEditingId(null); setActionMsg(null) }} className={`rounded px-2 py-0.5 font-mono text-[10px] transition-colors ${filter === f ? 'bg-background text-foreground' : 'text-muted-foreground hover:text-foreground'}`}>{f === 'pending' ? '待复核' : f === 'agree' ? '已确认' : '已纠错'}</button>
        ))}
      </div>
      {error && (<div className="mt-2 rounded border border-short/40 bg-short/10 p-2 font-mono text-[11px] text-short">加载失败：{error}</div>)}
      {actionMsg && (<div className={`mt-2 rounded border p-1.5 font-mono text-[10px] ${actionMsg.kind === 'ok' ? 'border-long/30 bg-long/10 text-long' : 'border-short/30 bg-short/10 text-short'}`}>{actionMsg.text}</div>)}
      <div className="mt-2">
        <DataState
          loading={loading && items.length === 0}
          loadingLabel="载入归因复核队列…"
          empty={!loading && items.length === 0}
          emptyLabel={`暂无「${filter === 'pending' ? '待复核' : filter === 'agree' ? '已确认' : '已纠错'}」归因。`}
          data={items}
        >
          {(list) => <div className="space-y-2">{list.map((it) => {
          const reasons = parseReasonsJson(it.reasons_json)
          const primary = reasons.find((r) => r.is_primary)
          const isEditing = editingId === it.id
          const isLoss = (it.verdict || '').toUpperCase() === 'LOSS'
          return (
            <div key={it.id} className={`rounded border bg-card p-2.5 font-mono ${isLoss ? 'border-short/30' : 'border-border'}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-foreground">#{it.id}</span>
                <span className="text-[11px] font-bold text-foreground">{it.asset || '—'} {it.action || ''}</span>
                <span className={`text-[10px] ${isLoss ? 'text-short' : 'text-muted-foreground'}`}>{it.verdict || '—'} · {fmtPct(it.pnl_pct)}</span>
                <span className="rounded border border-border px-1 py-0.5 text-[9px] text-muted-foreground">{it.source}</span>
                <span className="ml-auto text-[9px] text-muted-foreground">{fmtTime(it.created_at)}</span>
              </div>
              <div className="mt-1.5 text-[11px]">
                <span className="text-muted-foreground">主因：</span>
                <span className="font-bold text-short">
                  {it.reason_code != null && REASON_NAMES[it.reason_code]
                    ? `${it.reason_code} ${REASON_NAMES[it.reason_code]}`
                    : (it.reason_code != null ? <span className="text-muted-foreground">未知({it.reason_code})</span> : '—')}
                </span>
                {it.reason_text && <span className="ml-1 text-muted-foreground">— {it.reason_text.slice(0, 200)}</span>}
              </div>
              {reasons.length > 1 && (
                <div className="mt-1 space-y-0.5 text-[10px]">
                  {reasons.filter((r) => !(r.is_primary && r.code === primary?.code)).map((r, i) => (
                    <div key={i} className="text-muted-foreground">
                      · 次因 {r.code != null && REASON_NAMES[r.code] ? `${r.code} ${REASON_NAMES[r.code]}` : (r.code != null ? `未知(${r.code})` : '—')}
                      {r.text ? ` — ${r.text.slice(0, 100)}` : ''}
                    </div>
                  ))}
                </div>
              )}
              {/* v222: 失败源清单（数据缺失时高亮，让用户知道具体哪个 feed 挂了） */}
              {Array.isArray(it.failing_sources) && it.failing_sources.length > 0 && (
                <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px]">
                  <span className="text-short">⚠ 失败源：</span>
                  {it.failing_sources.map((s, i) => (
                    <span key={i} className="rounded border border-short/40 bg-short/10 px-1 py-0.5 font-mono text-short">{s}</span>
                  ))}
                </div>
              )}
              {it.core_impact && <div className="mt-1 truncate text-[10px] text-muted-foreground" title={it.core_impact}>筋：{it.core_impact.slice(0, 120)}</div>}
              {it.trade_id && (
                <div className="mt-1">
                  <button type="button" onClick={() => fetchDiagnosis(it.trade_id!)} disabled={diagnosisLoading === it.trade_id} className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/5 px-2 py-0.5 text-[10px] text-primary hover:bg-primary/10 disabled:opacity-50">
                    {diagnosisLoading === it.trade_id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Search className="h-3 w-3" />}
                    82-signal 诊断
                  </button>
                  {diagnosisOpenId === it.trade_id && diagnosisById[it.trade_id] && (
                    <DiagnosisPanel data={diagnosisById[it.trade_id]} />
                  )}
                </div>
              )}
{filter === 'pending' && (
                <div className="mt-2">
                  {!isEditing ? (
                    <div className="flex items-center gap-2">
                      <button type="button" onClick={() => handleAgree(it.id)} disabled={submitting} className="inline-flex items-center gap-1 rounded bg-long px-2 py-1 text-[10px] text-white hover:bg-long/80 disabled:opacity-50"><Check className="h-3 w-3" /> 同意</button>
                      <button type="button" onClick={() => openEdit(it)} disabled={submitting} className="inline-flex items-center gap-1 rounded border border-short/40 bg-short/10 px-2 py-1 text-[10px] text-short hover:bg-short/20 disabled:opacity-50"><Pencil className="h-3 w-3" /> 纠错</button>
                      <span className="ml-auto text-[9px] text-muted-foreground">{actionMsg?.id === it.id ? actionMsg.text : ''}</span>
                    </div>
                  ) : (
                    <div className="rounded border border-short/30 bg-short/5 p-2 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-2 text-[11px]">
                        <label className="flex items-center gap-1">改为
                          <select value={correctedCode} onChange={(e) => setCorrectedCode(Number(e.target.value))} className="rounded border border-border bg-background px-1 py-0.5 text-[10px]">
                            {Object.entries(REASON_NAMES).map(([k, v]) => (<option key={k} value={k}>{k} {v}</option>))}
                          </select>
                        </label>
                        <button type="button" onClick={() => handleCorrect(it.id)} disabled={submitting || !(correctedCode in REASON_NAMES)} className="ml-auto inline-flex items-center gap-1 rounded bg-short px-2 py-1 text-[10px] text-white hover:bg-short/80 disabled:opacity-50">{submitting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />} 提交纠错</button>
                        <button type="button" onClick={() => { setEditingId(null); setActionMsg(null) }} className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-[10px] text-muted-foreground hover:bg-accent"><X className="h-3 w-3" /> 取消</button>
                      </div>
                      <div className="flex flex-wrap items-center gap-2 text-[10px]">
                        <label className="flex flex-1 items-center gap-1 min-w-[200px]">修正文本（可选）<input type="text" value={correctedText} onChange={(e) => setCorrectedText(e.target.value)} placeholder="为什么这个 code 更合理" className="flex-1 rounded border border-border bg-background px-1 py-0.5 text-[10px]" /></label>
                        <label className="flex flex-1 items-center gap-1 min-w-[200px]">备注（可选）<input type="text" value={note} onChange={(e) => setNote(e.target.value)} placeholder="补充上下文" className="flex-1 rounded border border-border bg-background px-1 py-0.5 text-[10px]" /></label>
                      </div>
                      <details className="text-[10px] text-muted-foreground">
                        <summary className="cursor-pointer hover:text-foreground"><ChevronDown className="inline h-3 w-3" /> 展开 code 含义说明</summary>
                        <div className="mt-1 grid grid-cols-1 gap-0.5 sm:grid-cols-2">
                          {Object.entries(REASON_NAMES).map(([k, v]) => (<div key={k} className={Number(k) === correctedCode ? 'text-short font-bold' : ''}>{k} {v}</div>))}
                        </div>
                      </details>
                    </div>
                  )}
                </div>
              )}
              {filter !== 'pending' && (<div className="mt-1 text-[10px] text-muted-foreground">human_status: {it.human_status}</div>)}
            </div>
          )
        })}</div>}
        </DataState>
      </div>
    </section>
  )
}