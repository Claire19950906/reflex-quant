'use client'

import { useCallback, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v381 问库 — 一句话查真相：意图路由到白名单只读查询，结论里的数字全部来自库
// 不做自由 SQL、不猜因果：问「为什么涨跌」只给异动分布 + 指向归因页

type Row = Record<string, string | number | null>

type Answer = {
  ok: boolean
  intent?: string
  answer?: string
  error?: string
  table?: Row[]
  parsed?: { intent: string; asset: string; days: number; question: string }
  sources?: { 表?: string; 口径?: string }[]
}

const EXAMPLES = [
  '这周为什么亏',
  '最大回撤多少',
  '最近 7 天命中率怎么样',
  '黄金这周表现如何',
  '系统最常犯哪些错',
  '漏检了多少信号',
  '黄金最近为什么跌',
  '11555 这笔为什么亏',
  '新闻来源哪家最多',
]

export default function AskPage() {
  const [q, setQ] = useState('')
  const [res, setRes] = useState<Answer | null>(null)
  const [loading, setLoading] = useState(false)

  const ask = useCallback(async (question: string) => {
    const text = question.trim()
    if (!text) return
    setQ(text)
    setLoading(true)
    try {
      const r = await fetch(`${API_BASE}/nlq?q=${encodeURIComponent(text)}`)
      setRes(await r.json())
    } catch (e) {
      setRes({ ok: false, error: e instanceof Error ? e.message : '查询失败' })
    } finally {
      setLoading(false)
    }
  }, [])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4">
        <h1 className="font-mono text-sm font-semibold tracking-wider">问库</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          一句话问系统。只走白名单只读查询，结论里的每个数字都能追到表。
        </p>
      </header>

      <div className="surface-card p-3">
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void ask(q) }}
            placeholder="例如：黄金最近 30 天表现如何"
            className="w-full rounded border border-border bg-background px-3 py-2 font-mono text-xs outline-none focus:border-primary"
          />
          <button
            type="button"
            onClick={() => void ask(q)}
            disabled={loading}
            className="rounded bg-primary px-4 py-2 font-mono text-xs text-primary-foreground disabled:opacity-50"
          >
            {loading ? '查询中…' : '问'}
          </button>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {EXAMPLES.map((e) => (
            <button
              key={e}
              type="button"
              onClick={() => void ask(e)}
              className="rounded bg-secondary px-2 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
            >
              {e}
            </button>
          ))}
        </div>
      </div>

      {res && (
        <div className="mt-3 space-y-3">
          {res.parsed && (
            <div className="flex flex-wrap gap-2 font-mono text-[11px] text-muted-foreground">
              <span className="rounded bg-secondary px-1.5 py-0.5">意图 {res.intent}</span>
              <span className="rounded bg-secondary px-1.5 py-0.5">品种 {res.parsed.asset || '不限'}</span>
              <span className="rounded bg-secondary px-1.5 py-0.5">窗口 {res.parsed.days} 天</span>
            </div>
          )}

          {res.ok ? (
            <div className="surface-card p-4">
              <p className="text-sm leading-relaxed">{res.answer}</p>
              {!!res.sources?.length && (
                <div className="mt-2 font-mono text-[11px] text-muted-foreground">
                  口径：{res.sources.map((s) => `${s.表 || ''}${s.口径 ? `（${s.口径}）` : ''}`).join('；')}
                </div>
              )}
            </div>
          ) : (
            <div className="surface-card border border-red-500/30 p-4 text-xs text-red-400">
              {res.error || '查询失败'}
            </div>
          )}

          {!!res.table?.length && (
            <div className="surface-card overflow-x-auto p-3">
              <table className="w-full min-w-[560px] font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    {Object.keys(res.table[0]).map((k) => (
                      <th key={k} className="py-1.5 text-left font-normal">{k}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {res.table.map((row, i) => (
                    <tr key={i} className="border-b border-border/30">
                      {Object.keys(res.table![0]).map((k) => (
                        <td key={k} className="py-1.5">{row[k] ?? '—'}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </main>
  )
}
