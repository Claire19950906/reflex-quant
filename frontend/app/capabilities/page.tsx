'use client'

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { API_BASE } from '@/lib/api'
import { CapabilityPanel } from '@/components/quant/capability-panel'

type Tab = 'ask' | 'scenario' | 'research'

type Row = Record<string, string | number | null>
type Answer = {
  ok: boolean
  intent?: string
  answer?: string
  error?: string
  table?: Row[]
  sources?: Record<string, string>[]
}

type Impact = { asset?: string; direction?: string; range?: string; note?: string }
type Path = {
  name: string
  probability?: number | null
  triggers?: string[]
  impacts?: Impact[]
  actions?: string[]
}
type Scenario = {
  event: string
  as_of: string
  paths: Path[]
  watch?: string[]
  confidence?: string
  assumptions?: string[]
  source?: string
}

type Hypothesis = { text?: string; falsifiable_by?: string }
type Signal = { name?: string; rule?: string; params?: Record<string, unknown>; expected?: string }
type Note = {
  title?: string
  source?: string
  published_at?: string
  summary?: string
  hypotheses?: Hypothesis[]
  signals?: Signal[]
  risks?: string[]
  tags?: string[]
  source_kind?: string
  chars?: number
}
type NoteItem = { id: number; created_at: string; filename: string; title: string; status: string; note: Note }

const ASK_EXAMPLES = [
  '这周为什么亏',
  '最大回撤多少',
  '最近 7 天命中率怎么样',
  '系统最常犯哪些错',
]

function Tag({ children }: { children: ReactNode }) {
  return <span className="rounded-sm border border-border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">{children}</span>
}

export default function CapabilitiesPage() {
  const [tab, setTab] = useState<Tab>('scenario')

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4">
        <h1 className="font-mono text-sm font-semibold tracking-wider">能力扩展</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          情景树推演 · 研报摄入 · 问账本。只读账本与结构化抽取，不改下单链路。
        </p>
      </header>

      <nav className="mb-4 flex gap-1 border-b border-border pb-2">
        {([
          ['scenario', '情景树'],
          ['research', '研报摄入'],
          ['ask', '问账本'],
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`rounded-sm px-3 py-1.5 font-mono text-xs ${
              tab === key ? 'bg-foreground text-background' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === 'scenario' && <ScenarioPanel />}
      {tab === 'research' && <ResearchPanel />}
      {tab === 'ask' && <AskPanel />}

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="交互能力"
        hint="四图联动 / 自然语言改单（保护名单，只出草案）"
        featureIds={['F06','F17']}
      />
      </section>
    </main>
  )
}

/* ── 情景树 ─────────────────────────────────────────────────────────────── */
function ScenarioPanel() {
  const [event, setEvent] = useState('今晚 CPI 公布，市场预期 3.2%，前值 3.4%')
  const [horizon, setHorizon] = useState(3)
  const [data, setData] = useState<Scenario | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const run = useCallback(async () => {
    if (!event.trim()) return
    setLoading(true)
    setErr('')
    try {
      const r = await fetch(`${API_BASE}/capabilities/scenario`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event, horizon_days: horizon }),
      })
      const j = await r.json()
      if (!r.ok) throw new Error(j.detail || '推演失败')
      setData(j)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '推演失败')
    } finally {
      setLoading(false)
    }
  }, [event, horizon])

  return (
    <section className="space-y-4">
      <div className="surface-card p-3">
        <label className="font-mono text-[11px] text-muted-foreground">事件</label>
        <textarea
          value={event}
          onChange={e => setEvent(e.target.value)}
          rows={2}
          className="mt-1 w-full resize-y rounded-sm border border-border bg-transparent px-2 py-1.5 text-sm outline-none focus:border-foreground"
          placeholder="例：FOMC 加息 25bp 且表态偏鹰 / 非农大幅低于预期 / 某标的解锁"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="font-mono text-[11px] text-muted-foreground">往后看</label>
          <select
            value={horizon}
            onChange={e => setHorizon(Number(e.target.value))}
            className="rounded-sm border border-border bg-transparent px-2 py-1 font-mono text-xs"
          >
            {[1, 3, 7].map(d => <option key={d} value={d}>{d} 天</option>)}
          </select>
          <button
            type="button"
            onClick={run}
            disabled={loading}
            className="ml-auto rounded-sm bg-foreground px-3 py-1.5 font-mono text-xs text-background disabled:opacity-50"
          >
            {loading ? '推演中…' : '生成情景树'}
          </button>
        </div>
      </div>

      {err && <p className="text-xs text-destructive">{err}</p>}

      {data && (
        <>
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
            <Tag>{data.source === 'llm' ? '模型推演' : '结构模板（模型未返回）'}</Tag>
            <Tag>置信 {data.confidence || '—'}</Tag>
            <Tag>{data.as_of}</Tag>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            {data.paths.map((p, i) => (
              <article key={i} className="surface-card flex flex-col gap-2 p-3">
                <header className="flex items-baseline justify-between gap-2">
                  <h3 className="text-sm font-semibold">{p.name}</h3>
                  <span className="font-mono text-lg tabular-nums">
                    {p.probability == null ? '—' : `${Math.round(Number(p.probability) * 100)}%`}
                  </span>
                </header>

                {!!p.triggers?.length && (
                  <div>
                    <p className="font-mono text-[11px] text-muted-foreground">触发信号</p>
                    <ul className="mt-1 space-y-0.5 text-xs">
                      {p.triggers.map((t, j) => <li key={j}>· {t}</li>)}
                    </ul>
                  </div>
                )}

                {!!p.impacts?.length && (
                  <div>
                    <p className="font-mono text-[11px] text-muted-foreground">持仓影响</p>
                    <ul className="mt-1 space-y-1 text-xs">
                      {p.impacts.map((im, j) => (
                        <li key={j} className="flex justify-between gap-2">
                          <span>{im.asset} {im.direction}</span>
                          <span className="font-mono tabular-nums">{im.range}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {!!p.actions?.length && (
                  <div className="mt-auto border-t border-border pt-2">
                    <p className="font-mono text-[11px] text-muted-foreground">现在做什么</p>
                    <ul className="mt-1 space-y-0.5 text-xs">
                      {p.actions.map((a, j) => <li key={j}>→ {a}</li>)}
                    </ul>
                  </div>
                )}
              </article>
            ))}
          </div>

          {(!!data.watch?.length || !!data.assumptions?.length) && (
            <div className="surface-card grid gap-3 p-3 text-xs md:grid-cols-2">
              {!!data.watch?.length && (
                <div>
                  <p className="font-mono text-[11px] text-muted-foreground">开盘后盯什么</p>
                  <ul className="mt-1 space-y-0.5">{data.watch.map((w, i) => <li key={i}>· {w}</li>)}</ul>
                </div>
              )}
              {!!data.assumptions?.length && (
                <div>
                  <p className="font-mono text-[11px] text-muted-foreground">假设与缺口</p>
                  <ul className="mt-1 space-y-0.5">{data.assumptions.map((a, i) => <li key={i}>· {a}</li>)}</ul>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </section>
  )
}

/* ── 研报摄入 ───────────────────────────────────────────────────────────── */
function ResearchPanel() {
  const [note, setNote] = useState<Note | null>(null)
  const [items, setItems] = useState<NoteItem[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const loadList = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/capabilities/research?limit=20`)
      const j = await r.json()
      setItems(j.items || [])
    } catch {
      setItems([])
    }
  }, [])

  useEffect(() => { void loadList() }, [loadList])

  const upload = useCallback(async (file: File) => {
    if (file.size > 12 * 1024 * 1024) {
      setErr('文件超过 12MB，先拆一下')
      return
    }
    setLoading(true)
    setErr('')
    try {
      const buf = await file.arrayBuffer()
      const bytes = new Uint8Array(buf)
      let binary = ''
      for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i])
      const b64 = btoa(binary)
      const r = await fetch(`${API_BASE}/capabilities/research/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, content_base64: b64 }),
      })
      const j = await r.json()
      if (!r.ok) throw new Error(j.detail || '摄入失败')
      setNote(j.note || null)
      void loadList()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '摄入失败')
    } finally {
      setLoading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }, [loadList])

  return (
    <section className="space-y-4">
      <div className="surface-card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={loading}
            className="rounded-sm bg-foreground px-3 py-1.5 font-mono text-xs text-background disabled:opacity-50"
          >
            {loading ? '解析中…' : '上传 PDF / 文本'}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.txt,.md"
            className="hidden"
            onChange={e => {
              const f = e.target.files?.[0]
              if (f) void upload(f)
            }}
          />
          <p className="text-[11px] text-muted-foreground">
            抽假设、信号、风险与标签，入库待回测；扫描版 PDF 需先 OCR。
          </p>
        </div>
      </div>

      {err && <p className="text-xs text-destructive">{err}</p>}

      {note && (
        <article className="surface-card space-y-3 p-3">
          <header className="flex flex-wrap items-baseline gap-2">
            <h2 className="text-sm font-semibold">{note.title || '未命名'}</h2>
            <Tag>{note.source_kind === 'llm' ? '模型抽取' : '规则摘要（模型未返回）'}</Tag>
            {note.chars ? <Tag>{note.chars} 字</Tag> : null}
          </header>

          {note.summary && <p className="text-xs leading-relaxed">{note.summary}</p>}

          {!!note.hypotheses?.length && (
            <div>
              <p className="font-mono text-[11px] text-muted-foreground">假设（可否证）</p>
              <ul className="mt-1 space-y-1 text-xs">
                {note.hypotheses.map((h, i) => (
                  <li key={i}>
                    <span>{h.text}</span>
                    {h.falsifiable_by && <span className="text-muted-foreground">（反证：{h.falsifiable_by}）</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {!!note.signals?.length && (
            <div>
              <p className="font-mono text-[11px] text-muted-foreground">可回测信号</p>
              <ul className="mt-1 space-y-1 text-xs">
                {note.signals.map((s, i) => (
                  <li key={i}>
                    <span className="font-medium">{s.name}</span>
                    <span className="text-muted-foreground">：{s.rule}</span>
                    {s.expected && <span className="text-muted-foreground">（预期：{s.expected}）</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {!!note.risks?.length && (
            <div>
              <p className="font-mono text-[11px] text-muted-foreground">风险</p>
              <ul className="mt-1 space-y-0.5 text-xs">{note.risks.map((x, i) => <li key={i}>· {x}</li>)}</ul>
            </div>
          )}

          {!!note.tags?.length && (
            <div className="flex flex-wrap gap-1">{note.tags.map((t, i) => <Tag key={i}>{t}</Tag>)}</div>
          )}
        </article>
      )}

      {!!items.length && (
        <div className="surface-card p-3">
          <p className="font-mono text-[11px] text-muted-foreground">已入库</p>
          <ul className="mt-1 divide-y divide-border text-xs">
            {items.map(it => (
              <li key={it.id} className="flex items-baseline justify-between gap-2 py-1.5">
                <button
                  type="button"
                  className="truncate text-left hover:underline"
                  onClick={async () => {
                    const r = await fetch(`${API_BASE}/capabilities/research/${it.id}`)
                    const j = await r.json()
                    if (j.item) setNote(j.item.note)
                  }}
                >
                  #{it.id} {it.title}
                </button>
                <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                  {it.status} · {it.created_at.slice(5, 16)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

/* ── 问账本 ─────────────────────────────────────────────────────────────── */
function AskPanel() {
  const [q, setQ] = useState('这周为什么亏')
  const [res, setRes] = useState<Answer | null>(null)
  const [loading, setLoading] = useState(false)

  const ask = useCallback(async (question: string) => {
    if (!question.trim()) return
    setQ(question)
    setLoading(true)
    try {
      const r = await fetch(`${API_BASE}/nlq?q=${encodeURIComponent(question)}`)
      setRes(await r.json())
    } catch (e) {
      setRes({ ok: false, error: e instanceof Error ? e.message : '查询失败' })
    } finally {
      setLoading(false)
    }
  }, [])

  const cols = res?.table?.[0] ? Object.keys(res.table[0]) : []

  return (
    <section className="space-y-4">
      <div className="surface-card p-3">
        <div className="flex gap-2">
          <input
            value={q}
            onChange={e => setQ(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') void ask(q) }}
            className="w-full rounded-sm border border-border bg-transparent px-2 py-1.5 text-sm outline-none focus:border-foreground"
            placeholder="问账本：这周为什么亏 / 最大回撤多少"
          />
          <button
            type="button"
            onClick={() => void ask(q)}
            disabled={loading}
            className="shrink-0 rounded-sm bg-foreground px-3 py-1.5 font-mono text-xs text-background disabled:opacity-50"
          >
            {loading ? '查…' : '问'}
          </button>
        </div>
        <div className="mt-2 flex flex-wrap gap-1">
          {ASK_EXAMPLES.map(x => (
            <button
              key={x}
              type="button"
              onClick={() => void ask(x)}
              className="rounded-sm border border-border px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
            >
              {x}
            </button>
          ))}
        </div>
      </div>

      {res && (
        <div className="surface-card p-3">
          {res.error && <p className="text-xs text-destructive">{res.error}</p>}
          {res.answer && <p className="text-xs leading-relaxed">{res.answer}</p>}
          {!!res.table?.length && (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-xs tabular-nums">
                <thead>
                  <tr className="border-b border-border text-left font-mono text-[11px] text-muted-foreground">
                    {cols.map(c => <th key={c} className="py-1 pr-3 font-normal">{c}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {res.table.map((row, i) => (
                    <tr key={i} className="border-b border-border/50">
                      {cols.map(c => <td key={c} className="py-1 pr-3">{row[c] ?? '—'}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!!res.sources?.length && (
            <p className="mt-2 font-mono text-[11px] text-muted-foreground">
              口径：{res.sources.map(s => `${s['表']}（${s['口径'] || ''}）`).join('；')}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
