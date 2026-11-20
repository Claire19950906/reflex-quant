'use client'

import { useEffect, useMemo, useState } from 'react'

/**
 * v477 · 接口覆盖台账（挂在 /connectivity 连接与诊断）
 *
 * 回答的是"所有功能都展现在页面了吗"里最难回答的那一半：
 * 64 项能力面板解决了"能力有没有入口"，但后端还有几百条路由，
 * 哪些真在页面上、哪些只是同一个功能的旧版本、哪些是写操作、哪些一次都没被调过。
 *
 * 数据来源：`frontend/public/api-coverage.json`，由
 * `backend/tools/route_coverage_v477.py` 扫描真实 FastAPI 路由表 + 前端源码生成。
 * 不在页面里现算 —— 生成时点必须可追溯，避免"看着像覆盖了"。
 *
 * 口径警告写进面板：前端存在 `${API_BASE}/api/v188/${kind}` 这种动态拼接，
 * 字符串匹配会把它们算进 alias/orphan，所以 orphan ≠ 一定没被用，只是"没有静态入口"。
 */

type Mod = {
  module: string
  total: number
  shown: number
  alias: number
  action: number
  internal: number
  orphan: number
}

type Payload = {
  generated_at: string
  totals: {
    routes: number
    shown: number
    alias: number
    action: number
    internal: number
    orphan: number
  }
  modules: Mod[]
  orphans: { path: string; method: string; module: string }[]
  notes?: string[]
}

const CLS: Record<string, { label: string; tone: string }> = {
  shown: { label: '已展示', tone: 'text-long' },
  alias: { label: '旧版本别名', tone: 'text-amber-400' },
  action: { label: '写操作', tone: 'text-muted-foreground' },
  internal: { label: '运维面', tone: 'text-muted-foreground/70' },
  orphan: { label: '无静态入口', tone: 'text-short' },
}

export function ApiCoveragePanel({ defaultOpen = false }: { defaultOpen?: boolean }) {
  const [data, setData] = useState<Payload | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [open, setOpen] = useState(defaultOpen)
  const [filter, setFilter] = useState<string>('')

  useEffect(() => {
    const ac = new AbortController()
    fetch('/api-coverage.json', { signal: ac.signal, cache: 'no-store' })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return (await r.json()) as Payload
      })
      .then(setData)
      .catch((e: unknown) => {
        if (!ac.signal.aborted) setErr(e instanceof Error ? e.message : String(e))
      })
    return () => ac.abort()
  }, [])

  const mods = useMemo(() => (data?.modules || []).filter((m) => m.orphan > 0).slice(0, 14), [data])
  const orphans = useMemo(() => {
    const list = data?.orphans || []
    if (!filter) return list
    return list.filter((o) => o.module.includes(filter) || o.path.includes(filter))
  }, [data, filter])

  return (
    <section className="rounded-lg border border-border bg-card/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/70 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-2 font-mono text-[12px] font-semibold text-foreground hover:text-primary"
        >
          <span aria-hidden>{open ? '▼' : '▶'}</span>
          <span>接口覆盖台账</span>
        </button>
        <span className="text-[11px] text-muted-foreground">
          每一条后端路由的页面归属：已展示 / 旧版本别名 / 写操作 / 运维面 / 无静态入口
        </span>
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">
          {err ? <span className="text-short">台账缺失</span> : data ? `生成于 ${data.generated_at}` : '载入中…'}
        </span>
      </div>

      {open && data && (
        <div className="px-3 py-2">
          <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px]">
            <span className="text-muted-foreground">
              路由 <span className="text-foreground">{data.totals.routes}</span>
            </span>
            {(['shown', 'alias', 'action', 'internal', 'orphan'] as const).map((k) => (
              <span key={k} className="text-muted-foreground">
                {CLS[k].label}{' '}
                <span className={CLS[k].tone}>{data.totals[k]}</span>
              </span>
            ))}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-[11px]">
              <thead>
                <tr className="border-b border-border/60 text-left font-mono text-[10px] text-muted-foreground">
                  <th className="py-1 pr-2 font-normal">模块</th>
                  <th className="py-1 pr-2 text-right font-normal">路由</th>
                  <th className="py-1 pr-2 text-right font-normal">已展示</th>
                  <th className="py-1 pr-2 text-right font-normal">别名</th>
                  <th className="py-1 pr-2 text-right font-normal">写</th>
                  <th className="py-1 pr-2 text-right font-normal">运维</th>
                  <th className="py-1 text-right font-normal">无入口</th>
                </tr>
              </thead>
              <tbody>
                {mods.map((m) => (
                  <tr
                    key={m.module}
                    className="cursor-pointer border-b border-border/40 hover:bg-muted/20"
                    onClick={() => setFilter(filter === m.module ? '' : m.module)}
                  >
                    <td className="py-1 pr-2 font-mono text-foreground/80">{m.module}</td>
                    <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{m.total}</td>
                    <td className="py-1 pr-2 text-right font-mono text-long">{m.shown}</td>
                    <td className="py-1 pr-2 text-right font-mono text-amber-400">{m.alias}</td>
                    <td className="py-1 pr-2 text-right font-mono text-muted-foreground">{m.action}</td>
                    <td className="py-1 pr-2 text-right font-mono text-muted-foreground/70">{m.internal}</td>
                    <td className="py-1 text-right font-mono text-short">{m.orphan}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-2 flex items-center gap-2">
            <span className="font-mono text-[11px] text-muted-foreground">
              无静态入口 {orphans.length} 条{filter ? ` · 过滤 ${filter}` : ''}
            </span>
            {filter && (
              <button
                type="button"
                onClick={() => setFilter('')}
                className="rounded border border-muted-foreground/30 px-1.5 py-0.5 font-mono text-[10px] hover:bg-muted/40"
              >
                清除
              </button>
            )}
            <span className="ml-auto font-mono text-[10px] text-muted-foreground/60">
              点模块行可过滤
            </span>
          </div>
          <div className="mt-1 max-h-56 overflow-auto rounded border border-border/50">
            <table className="w-full text-[11px]">
              <tbody>
                {orphans.slice(0, 120).map((o) => (
                  <tr key={`${o.method}-${o.path}`} className="border-b border-border/30">
                    <td className="w-12 px-2 py-0.5 font-mono text-[10px] text-muted-foreground">{o.method}</td>
                    <td className="px-2 py-0.5 truncate font-mono text-foreground/75">{o.path}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {orphans.length > 120 && (
              <div className="px-2 py-1 font-mono text-[10px] text-muted-foreground/60">
                仅显示前 120 条，完整清单见 docs/ops/接口盘点台账-2026-11-19.md
              </div>
            )}
          </div>

          {(data.notes || []).length > 0 && (
            <div className="mt-2 font-mono text-[10px] text-muted-foreground/70">
              {(data.notes || []).map((n) => (
                <div key={n}>· {n}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
