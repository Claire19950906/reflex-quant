'use client'

/**
 * v172 结构化输出层 UI 组件库。
 * 设计原则：缺失字段 → 显示 "—"；verdict 颜色编码；候选时间按 confidence 倒序；
 * 影响曲线 4 窗口用 ASCII bar 直观可视化（避免引入 recharts）。
 */

import { useEffect, useState } from 'react'
import {
  fetchNewsAtoms, fetchNewsEventTimeline, fetchNewsStructured,
  fetchEventImpactCurveByAtom,
} from '@/lib/api'
import type {
  EventAtom, EventTimeCandidate, ImpactCurveAggregated,
  NewsStructuredResponse, StructuredCheck, StructuredInput,
} from '@/lib/quant-data'
import { formatDual } from '@/lib/tz-utils'

// ── 颜色与样式工具 ─────────────────────────────────────────────

const VERDICT_STYLES: Record<string, string> = {
  ok:         'border-long/40 bg-long/10 text-long',
  fabricated: 'border-short/40 bg-short/10 text-short',
  partial:    'border-yellow-500/40 bg-yellow-500/10 text-yellow-600 dark:text-yellow-300',
  missing:    'border-muted-foreground/40 bg-muted text-muted-foreground',
}
function verdictClass(v: string | null | undefined): string {
  if (!v) return 'border-border bg-muted text-muted-foreground'
  return VERDICT_STYLES[v] ?? 'border-border bg-muted text-muted-foreground'
}
function verdictLabel(v: string | null | undefined): string {
  if (!v) return '—'
  return ({
    ok: '✅ 全部引用命中', fabricated: '❌ 检测到凭空引用',
    partial: '⚠ 部分引用缺失', missing: '— 未引用',
  } as Record<string, string>)[v] ?? v
}

const DIR_STYLES: Record<string, string> = {
  bullish: 'text-long', bearish: 'text-short', unknown: 'text-muted-foreground',
}
function dirLabel(d: string): string {
  return ({bullish: '看涨', bearish: '看跌', unknown: '未知'} as Record<string, string>)[d] ?? d
}

// ── 1. EventAtomCard ──────────────────────────────────────────

export function EventAtomCard({ atom }: { atom: EventAtom }) {
  const t = formatDual(atom.event_time)
  return (
    <div className="rounded-md border border-border bg-card p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] font-bold text-primary">
          {atom.event_type_code}
        </span>
        <span className="text-muted-foreground">{atom.event_type_zh || '—'}</span>
        <span className="ml-auto rounded border border-border bg-secondary px-2 py-0.5 font-mono text-[10px]">
          {atom.asset_code}
        </span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 font-mono text-[10px] text-muted-foreground">
        <div>
          <span className="text-foreground/70">事件时间</span>
          <div className="text-foreground">{t.cn || '—'}</div>
          <div className="text-[9px]">{t.utc} UTC · conf {atom.event_time_confidence?.toFixed(2) ?? '—'}</div>
        </div>
        <div>
          <span className="text-foreground/70">方向 / 强度</span>
          <div className={`font-semibold ${DIR_STYLES[atom.direction] || 'text-foreground'}`}>
            {dirLabel(atom.direction)}
            {atom.direction_confidence !== null && atom.direction_confidence !== undefined && (
              <span className="ml-1 text-muted-foreground">({(atom.direction_confidence * 100).toFixed(0)}%)</span>
            )}
          </div>
          <div className="text-[9px]">量级 {atom.magnitude_value ?? '—'}{atom.magnitude_unit || ''}</div>
        </div>
      </div>
      {atom.actor && (
        <div className="mt-2 text-[10px] text-muted-foreground">
          <span className="text-foreground/70">施动者：</span>{atom.actor}
        </div>
      )}
      {atom.reasoning && (
        <div className="mt-2 border-t border-border pt-2 font-mono text-[10px] leading-relaxed text-foreground/80">
          {atom.reasoning}
        </div>
      )}
      <div className="mt-2 flex justify-between text-[9px] text-muted-foreground/60">
        <span>parser {atom.parser_version}</span>
        <span>id #{atom.id} · news #{atom.raw_news_id}</span>
      </div>
    </div>
  )
}

// ── 2. StructuredInputCard ───────────────────────────────────

function _tryParse(s: string | undefined): StructuredInput | null {
  if (!s) return null
  try {
    const v = JSON.parse(s)
    return v && typeof v === 'object' ? v as StructuredInput : { _raw: s }
  } catch { return { _raw: s, _parse_error: true } }
}

export function StructuredInputCard({ data }: { data: string | StructuredInput | null | undefined }) {
  const obj = (typeof data === 'object' && data !== null) ? data : _tryParse(typeof data === 'string' ? data : undefined)
  if (!obj || Object.keys(obj).length === 0) {
    return <div className="text-[10px] text-muted-foreground/60">无 structured_input 数据</div>
  }
  const sections: { key: keyof StructuredInput; label: string; mono?: boolean }[] = [
    { key: 'meta',         label: '元数据' },
    { key: 'events',       label: '事件数组' },
    { key: 'snapshot',     label: '快照引用' },
    { key: 'relations',    label: '因果关系' },
    { key: 'baseline',     label: '历史基线' },
    { key: 'text_summary', label: '摘要', mono: true },
  ]
  return (
    <div className="space-y-2">
      {sections.map(({ key, label, mono }) => {
        const v = obj[key]
        if (v === undefined || v === null) return null
        const text = typeof v === 'string' ? v : JSON.stringify(v, null, 2)
        return (
          <div key={String(key)} className="rounded border border-border bg-secondary/40 p-2">
            <div className="mb-1 flex items-center justify-between">
              <span className="font-mono text-[10px] font-semibold text-foreground">{label}</span>
              <span className="font-mono text-[9px] text-muted-foreground">{String(key)}</span>
            </div>
            <pre className={`max-h-48 overflow-auto whitespace-pre-wrap break-all text-[10px] text-foreground/85 ${mono ? 'font-mono' : ''}`}>
              {text.length > 800 ? text.slice(0, 800) + '… [+更多]' : text}
            </pre>
          </div>
        )
      })}
    </div>
  )
}

// ── 3. StructuredCheckCard ───────────────────────────────────

function _parseCheck(s: string | StructuredCheck | null | undefined): StructuredCheck | null {
  if (!s) return null
  if (typeof s === 'object') return s as StructuredCheck
  try { return JSON.parse(s) as StructuredCheck } catch { return null }
}

export function StructuredCheckCard({ data }: { data: string | StructuredCheck | null | undefined }) {
  const obj = _parseCheck(data)
  if (!obj || Object.keys(obj).length === 0) {
    return <div className="text-[10px] text-muted-foreground/60">无 structured_check 数据</div>
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[10px] text-muted-foreground">verdict:</span>
        <span className={`rounded border px-2 py-0.5 font-mono text-[10px] font-semibold ${verdictClass(obj.verdict)}`}>
          {verdictLabel(obj.verdict)}
        </span>
        {obj.check_at && (
          <span className="font-mono text-[9px] text-muted-foreground">check_at: {formatDual(obj.check_at).cn}</span>
        )}
      </div>
      {obj.mentioned && obj.mentioned.length > 0 && (
        <div>
          <div className="mb-1 font-mono text-[10px] text-long">✓ mentioned</div>
          <div className="flex flex-wrap gap-1">
            {obj.mentioned.map((m, i) => (
              <span key={i} className="rounded border border-long/40 bg-long/5 px-1.5 py-px font-mono text-[9px] text-long">{m}</span>
            ))}
          </div>
        </div>
      )}
      {obj.missing && obj.missing.length > 0 && (
        <div>
          <div className="mb-1 font-mono text-[10px] text-yellow-600">⚠ missing</div>
          <div className="flex flex-wrap gap-1">
            {obj.missing.map((m, i) => (
              <span key={i} className="rounded border border-yellow-500/40 bg-yellow-500/5 px-1.5 py-px font-mono text-[9px] text-yellow-600">{m}</span>
            ))}
          </div>
        </div>
      )}
      {obj.fabricated && obj.fabricated.length > 0 && (
        <div>
          <div className="mb-1 font-mono text-[10px] text-short">✗ fabricated</div>
          <div className="flex flex-wrap gap-1">
            {obj.fabricated.map((m, i) => (
              <span key={i} className="rounded border border-short/40 bg-short/5 px-1.5 py-px font-mono text-[9px] text-short">{m}</span>
            ))}
          </div>
        </div>
      )}
      {obj.reason && (
        <div className="border-t border-border pt-1 font-mono text-[10px] leading-relaxed text-foreground/80">{obj.reason}</div>
      )}
    </div>
  )
}

// ── 4. EventTimeline ─────────────────────────────────────────

export function EventTimeline({ candidates }: { candidates: EventTimeCandidate[] }) {
  if (!candidates || candidates.length === 0) {
    return <div className="text-[10px] text-muted-foreground/60">无候选时间记录</div>
  }
  const sorted = [...candidates].sort((a, b) => {
    if (a.is_primary !== b.is_primary) return a.is_primary ? -1 : 1
    return (b.confidence ?? 0) - (a.confidence ?? 0)
  })
  return (
    <div className="space-y-1">
      {sorted.map((c) => {
        const t = formatDual(c.candidate_ts)
        const isInfer = c.source === 'ai_inferred' || c.source === 'cross_net'
        return (
          <div key={c.id} className={`flex items-start gap-2 rounded border px-2 py-1 ${
            c.is_primary ? 'border-primary/40 bg-primary/5' : 'border-border bg-card'
          }`}>
            <span className={`mt-0.5 font-mono text-[10px] ${c.is_primary ? 'text-primary' : 'text-muted-foreground'}`}>
              {c.is_primary ? '★' : '·'}
            </span>
            <div className="flex-1">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-mono text-[11px] font-semibold text-foreground">{t.cn}</span>
                <span className="font-mono text-[9px] text-muted-foreground">
                  conf {c.confidence !== null ? c.confidence.toFixed(2) : '—'}
                </span>
                <span className={`rounded px-1.5 py-px font-mono text-[9px] ${
                  isInfer
                    ? 'border-yellow-500/40 bg-yellow-500/10 text-yellow-600'
                    : 'border-border bg-secondary text-muted-foreground'
                }`}>
                  {c.source}
                </span>
                <span className="font-mono text-[9px] text-muted-foreground">{c.tz}</span>
              </div>
              {c.reasoning && (
                <div className="mt-0.5 font-mono text-[9px] leading-relaxed text-muted-foreground/80">{c.reasoning}</div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── 5. ImpactCurveChart ──────────────────────────────────────

const WINDOW_LABELS = ['5m', '30m', '2h', '1d'] as const

export function ImpactCurveChart({ curve }: { curve: ImpactCurveAggregated }) {
  const windows = WINDOW_LABELS.filter((w) => curve[w])
  if (windows.length === 0) {
    return <div className="text-[10px] text-muted-foreground/60">无影响曲线数据</div>
  }
  const max = Math.max(...windows.map((w) => Math.abs(curve[w]?.delta_pct ?? 0)), 1)
  return (
    <div className="space-y-1">
      {windows.map((w) => {
        const pt = curve[w]
        if (!pt) return null
        const delta = pt.delta_pct ?? 0
        const pct = (Math.abs(delta) / max) * 100
        const positive = delta >= 0
        return (
          <div key={w} className="flex items-center gap-2 font-mono text-[10px]">
            <span className="w-8 text-muted-foreground">{w}</span>
            <div className="relative h-4 flex-1 rounded bg-secondary/60">
              <div
                className={`absolute top-0 h-4 rounded ${positive ? 'bg-long/60' : 'bg-short/60'}`}
                style={{
                  width: `${pct}%`,
                  left: positive ? '50%' : `${50 - pct}%`,
                }}
              />
              <div className="absolute left-1/2 top-0 h-4 w-px bg-border" />
            </div>
            <span className={`w-14 text-right tabular-nums ${positive ? 'text-long' : 'text-short'}`}>
              {positive ? '+' : ''}{delta.toFixed(2)}%
            </span>
            <span className="w-16 text-right tabular-nums text-muted-foreground">
              {pt.price !== null && pt.price !== undefined ? `$${pt.price.toFixed(0)}` : '—'}
            </span>
          </div>
        )
      })}
      <div className="mt-1 flex justify-between border-t border-border pt-1 font-mono text-[9px] text-muted-foreground/60">
        <span>window → delta% (vol {curve[windows[0]]?.volume_ratio?.toFixed(2) ?? '—'})</span>
        <span>source: {curve[windows[0]]?.note?.slice(0, 30) || '—'}</span>
      </div>
    </div>
  )
}

// ── 6. StructuredPanel —— 详情面板（聚合上面 5 个组件）───────

export function StructuredPanel({ newsId }: { newsId: number }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [atoms, setAtoms] = useState<EventAtom[]>([])
  const [timeline, setTimeline] = useState<NewsTimelinePayload | null>(null)
  const [structured, setStructured] = useState<NewsStructuredResponse | null>(null)
  const [firstCurve, setFirstCurve] = useState<ImpactCurveAggregated>({})

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(null)
    ;(async () => {
      try {
        const [atomsR, tlR, stR] = await Promise.all([
          fetchNewsAtoms(newsId),
          fetchNewsEventTimeline(newsId),
          fetchNewsStructured(newsId),
        ])
        if (cancelled) return
        setAtoms(atomsR.items)
        setTimeline({ primary: tlR.primary, candidates: tlR.candidates, count: tlR.count })
        setStructured(stR)
        if (atomsR.items.length > 0) {
          const cR = await fetchEventImpactCurveByAtom(atomsR.items[0].id)
          if (!cancelled) setFirstCurve(cR.aggregated)
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message ?? '加载失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [newsId])

  if (loading) return <div className="p-3 text-[10px] text-muted-foreground">结构化层加载中…</div>
  if (error)   return <div className="p-3 text-[10px] text-short">⚠ {error}</div>

  const noData =
    atoms.length === 0 &&
    (!timeline || timeline.candidates.length === 0) &&
    (!structured || (
      !structured.structured_input && !structured.structured_check &&
      !structured.event_type_code && !structured.snapshot_proximity
    ))
  if (noData) {
    return <div className="p-3 text-[10px] text-muted-foreground/60">该新闻暂无结构化数据（v173+ 推理 + v172 规则抽取后才落库）</div>
  }

  return (
    <div className="space-y-4 rounded-lg border border-primary/20 bg-primary/[0.02] p-3">
      <div className="flex items-center gap-2 border-b border-primary/10 pb-1.5">
        <span className="font-mono text-[10px] font-semibold text-primary">v172 结构化输出层</span>
        <span className="font-mono text-[9px] text-muted-foreground">机器内部思考可读化（事件原子 / 结构化输入 / 反向校验 / 时间线 / 影响曲线）</span>
      </div>

      {structured && (structured.event_type_code || structured.snapshot_proximity) && (
        <div className="flex flex-wrap items-center gap-2">
          {structured.event_type_code && (
            <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] font-bold text-primary">
              type={structured.event_type_code}
            </span>
          )}
          {structured.snapshot_proximity && (
            <span className="rounded border border-border bg-secondary px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
              snapshot_proximity={structured.snapshot_proximity}
            </span>
          )}
          {structured.verdict && (
            <span className={`rounded border px-2 py-0.5 font-mono text-[10px] font-semibold ${verdictClass(structured.verdict)}`}>
              check: {verdictLabel(structured.verdict)}
            </span>
          )}
        </div>
      )}

      <Section title="① 事件原子（规则确定性抽取）" count={atoms.length}>
        {atoms.length === 0
          ? <div className="text-[10px] text-muted-foreground/60">无</div>
          : (
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {atoms.map((a) => <EventAtomCard key={a.id} atom={a} />)}
            </div>
          )}
      </Section>

      <Section title="② 候选时间线（多源锚定）" count={timeline?.candidates.length ?? 0}>
        <EventTimeline candidates={timeline?.candidates ?? []} />
      </Section>

      <Section title="③ LLM 结构化输入（6 字段 payload）">
        <StructuredInputCard data={structured?.structured_input ?? null} />
      </Section>

      <Section title="④ 反向引用校验（LLM 输出 vs 输入一致性）">
        <StructuredCheckCard data={structured?.structured_check ?? null} />
      </Section>

      <Section title="⑤ 影响曲线（5m / 30m / 2h / 1d）">
        <ImpactCurveChart curve={firstCurve} />
      </Section>
    </div>
  )
}

type NewsTimelinePayload = {
  primary: EventTimeCandidate | null
  candidates: EventTimeCandidate[]
  count: number
}

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2">
        <span className="font-mono text-[11px] font-semibold text-foreground">{title}</span>
        {count !== undefined && (
          <span className="rounded bg-secondary px-1.5 py-px font-mono text-[9px] text-muted-foreground">{count}</span>
        )}
      </div>
      {children}
    </div>
  )
}