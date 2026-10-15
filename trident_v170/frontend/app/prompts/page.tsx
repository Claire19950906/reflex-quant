'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { AiPromptsPanel } from '@/components/ai-prompts-panel'
import { AssetRulesPanel } from '@/components/asset-rules-panel'
import { ActiveInsightsPanel } from '@/components/active-insights-panel'
import { useToast } from '@/components/ui/toast'

// ai提示词 — 功能：
// ① AI 分析 Prompt（查看当前模型 system prompt + 管理自定义分析指令，注入 AI）
// ② 品种专项分析规则（按金融品类单独设置分析规则，AI 按品种套用）
// ③ L1 关键词过滤（采集入库噪声过滤）：查看 / 新增 / 停用，即时生效

type KeywordItem = {
  id: number
  kind: 'junk' | 'core'
  keyword: string
  builtin: number
  active: number
  note: string
  created_at: string
}

type KeywordList = {
  junk: KeywordItem[]
  core: KeywordItem[]
  total: number
  active_count: number
}

const junkColor = 'border-short/30 bg-short/5'
const coreColor = 'border-long/30 bg-long/5'

export default function PromptsPage() {
  // v302+ P0: 替代散落的 window.confirm
  const { toast } = useToast()
  const [data, setData] = useState<KeywordList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [kind, setKind] = useState<'junk' | 'core'>('junk')
  const [keyword, setKeyword] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch(`${API_BASE}/filter/keywords`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`list ${r.status}`)
      setData((await r.json()) as KeywordList)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // 10s 轮询：多端/后台变更（API 或引擎同步）后页面自动更新
  useEffect(() => {
    const timer = window.setInterval(() => void load(), 10_000)
    return () => window.clearInterval(timer)
  }, [load])

  const add = async () => {
    const kw = keyword.trim()
    if (!kw) return
    setSubmitting(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/filter/keywords`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, keyword: kw, note }),
      })
      if (!r.ok) throw new Error(await r.text())
      setKeyword('')
      setNote('')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '添加失败')
    } finally {
      setSubmitting(false)
    }
  }

  const remove = async (item: KeywordItem) => {
    // v302+ P0: 用 toast.confirm 替代 window.confirm
    const ok = await new Promise<boolean>((resolve) => {
      toast.confirm(`停用提示词「${item.keyword}」？停用后立即不再参与过滤。`, () => resolve(true), () => resolve(false))
    })
    if (!ok) return
    try {
      const r = await fetch(`${API_BASE}/filter/keywords/${item.id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(await r.text())
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除失败')
    }
  }

  const active = (items?: KeywordItem[]) => (items ?? []).filter((i) => i.active)

  function KeywordGroup({ title, items, tone, desc }: { title: string; items: KeywordItem[]; tone: 'junk' | 'core'; desc: string }) {
    const list = active(items)
    return (
      <div className={`rounded-md border p-3 ${tone === 'junk' ? junkColor : coreColor}`}>
        <div className="flex items-center gap-2">
          <span className={`font-mono text-xs font-bold uppercase tracking-widest ${tone === 'junk' ? 'text-short' : 'text-long'}`}>
            {title}
          </span>
          <span className="ml-auto rounded bg-card px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
            {list.length} 个生效
          </span>
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{desc}</p>
        <div className="thin-scroll mt-2 max-h-[46vh] space-y-1 overflow-y-auto pr-0.5">
          {list.length === 0 && (
            <div className="font-mono text-[10px] text-muted-foreground">暂无生效提示词</div>
          )}
          {list.map((item) => (
            <div key={item.id} className="flex items-center gap-1.5 rounded border border-border bg-card px-1.5 py-1">
              <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-foreground" title={item.note || item.keyword}>
                {item.keyword}
              </span>
              {item.builtin ? (
                <span className="shrink-0 rounded bg-secondary px-1 py-px font-mono text-[8px] text-muted-foreground">内置</span>
              ) : (
                <span className="shrink-0 rounded bg-primary/10 px-1 py-px font-mono text-[8px] text-primary">自定义</span>
              )}
              <button
                type="button"
                onClick={() => void remove(item)}
                title="停用（软删除）"
                className="shrink-0 rounded border border-short/40 px-1 font-mono text-[9px] text-short hover:bg-short/10"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:p-4">
      {/* ── ① AI 分析 Prompt（主 prompt 查看 + 自定义指令管理） ── */}
      <AiPromptsPanel />

      {/* ── ② 品种专项分析规则（按金融品类单独设置，AI 按品种套用） ── */}
      <AssetRulesPanel />

      {/* ── ①.5 当前生效经验规则（区分来源，对照调 prompt） ── */}
      <ActiveInsightsPanel />

      {/* ── ② L1 关键词过滤 ── */}
      <section className="surface-card p-3">
        <div className="flex items-center gap-2">
          <h2 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">L1 · 关键词过滤</h2>
          <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">FILTER L1</span>
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">
            {data ? `${data.active_count}/${data.total} 个生效 · 新增/停用即时生效` : '加载中…'}
          </span>
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          这是新闻采集入库的第 1 层过滤（L1 关键词匹配）：命中 <b className="text-short">JUNK 噪声词</b> 的新闻直接丢弃；
          命中 <b className="text-long">CORE 高相关词</b> 的新闻直接高相关入库（跳过 LLM 二次判定）。可人工新增提示词，
          或停用认为失效的提示词——保存后立即生效。
        </p>
      </section>

      {/* 新增表单 */}
      <section className="surface-card p-3">
        <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">新增提示词</div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as 'junk' | 'core')}
            className="rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground focus:border-primary focus:outline-none"
            aria-label="类型"
          >
            <option value="junk">JUNK 噪声词（命中即丢弃）</option>
            <option value="core">CORE 高相关词（命中即直通）</option>
          </select>
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void add() }}
            placeholder="输入提示词 / 关键词…"
            className="min-w-0 flex-1 rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
          />
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="备注（可选）"
            className="w-40 rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
          />
          <button
            type="button"
            onClick={() => void add()}
            disabled={submitting || !keyword.trim()}
            className="rounded border border-primary/50 bg-primary/10 px-3 py-1.5 font-mono text-xs text-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? '添加中…' : '添加'}
          </button>
        </div>
        {error && (
          <div className="mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 text-[11px] text-short">{error}</div>
        )}
      </section>

      {/* 两组提示词 */}
      {loading && !data ? (
        <div className="p-6 text-center font-mono text-xs text-muted-foreground">加载中…</div>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <KeywordGroup
            title="JUNK · 噪声过滤词"
            tone="junk"
            items={data?.junk ?? []}
            desc="命中任一即判为噪声丢弃（如：空投 / 抽奖 / 教程 / 周报 / 辟谣等）"
          />
          <KeywordGroup
            title="CORE · 高相关直通词"
            tone="core"
            items={data?.core ?? []}
            desc="命中任一即高相关直通入库，跳过 L2（如：美联储 / CPI / 非农 / 爆仓 / 制裁等）"
          />
        </div>
      )}
    </main>
  )
}
