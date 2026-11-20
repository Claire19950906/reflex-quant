'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE } from '@/lib/api'

/**
 * v476 · 能力面板（Trident-2 64 项功能按业务归属挂进页面）
 *
 * 背景：64 项功能 2026-11-16 就在后端跑通了（`/api/trident2/*` 11 个接口），
 * 但前端引用数为 0 —— 页面上完全看不到"系统还有这些能力"。
 *
 * 为什么不做一个"64 项大总表"页：这 64 项不是同一类东西。
 *   盘前体检属于开仓闸、持仓 thesis 属于账本、链上巨鲸属于资金流向……
 *   堆在一页里，用户还是不知道"我现在看的这件事，系统有没有替我看"。
 *   所以这里只做**一个通用面板**，由各页面按分类/ID 取自己那一段。
 *
 * 三条口径（与项目一致）：
 *   1. 不造数：接口挂了就写"未响应"，不补占位行、不假装 0；
 *   2. 口径在后端：状态/severity/headline 全部透传 `t2_results`，前端不算第二套；
 *   3. 保护名单只出计划：F05/F08/F09/F17/F55/F58 是真实交易/改单类，
 *      前端禁用运行按钮，避免"点一下就把计划当执行"。
 */

type Feature = {
  id: string
  batch?: string
  category?: string
  name: string
  what?: string
  priority?: string
  landing?: string
  implemented?: boolean
  last_run?: {
    run_ts?: string
    status?: string
    severity?: string
    headline?: string
    value?: string | null
    sample_size?: number | null
    anchor_ts?: string | null
  } | null
}

/** 与后端 `catalog.is_protected` 一致；改一处要两边同步（cd backend && python -c "from trident2 import catalog;print(...)" 可校验） */
const PROTECTED_IDS = new Set(['F05', 'F08', 'F09', 'F17', 'F55', 'F58'])

const STATUS_TONE: Record<string, string> = {
  ok: 'text-long',
  alert: 'text-short',
  warn: 'text-primary',
  no_data: 'text-muted-foreground',
  not_configured: 'text-muted-foreground/70',
  skipped: 'text-muted-foreground/70',
  error: 'text-short',
}

const STATUS_LABEL: Record<string, string> = {
  ok: '正常',
  alert: '告警',
  warn: '注意',
  no_data: '无数据',
  not_configured: '未配置',
  skipped: '已跳过',
  error: '失败',
}

function fmtTs(ts?: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function CapabilityPanel({
  title,
  hint,
  categories,
  featureIds,
  defaultOpen = false,
  limit,
}: {
  title: string
  hint?: string
  /** 按后端 category 过滤，例如 ['风控与执行'] */
  categories?: string[]
  /** 精确到具体功能，适合"这一页只关心这几项"的场景 */
  featureIds?: string[]
  defaultOpen?: boolean
  limit?: number
}) {
  const [items, setItems] = useState<Feature[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(defaultOpen)
  const [running, setRunning] = useState<string | null>(null)
  const [runNote, setRunNote] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    setLoading(true)
    setErr(null)

    // 后端挂了要能收场：12s 没回就当作"未响应"，页面不许无限转圈
    const timer = setTimeout(() => ac.abort(), 12_000)
    fetch(`${API_BASE}/trident2/features`, { signal: ac.signal, cache: 'no-store' })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return (await res.json()) as { ok?: boolean; features?: Feature[]; detail?: string }
      })
      .then((data) => {
        if (ac.signal.aborted) return
        if (!data?.ok) throw new Error(data?.detail || '接口返回 ok=false')
        setItems(data.features || [])
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return
        setErr(e instanceof Error ? e.message : String(e))
        setItems(null)
      })
      .finally(() => {
        clearTimeout(timer)
        if (!ac.signal.aborted) setLoading(false)
      })

    return () => {
      clearTimeout(timer)
      ac.abort()
    }
  }, [tick])

  const filtered = useMemo(() => {
    if (!items) return []
    let list = items
    if (categories?.length) list = list.filter((f) => f.category && categories.includes(f.category))
    if (featureIds?.length) list = list.filter((f) => featureIds.includes(f.id))
    // 优先 P0，再按 id
    list = [...list].sort((a, b) => {
      const pa = a.priority === 'P0' ? 0 : 1
      const pb = b.priority === 'P0' ? 0 : 1
      return pa - pb || a.id.localeCompare(b.id)
    })
    return limit ? list.slice(0, limit) : list
  }, [items, categories, featureIds, limit])

  const ranCount = filtered.filter((f) => f.last_run).length

  const runOne = useCallback(
    async (id: string) => {
      setRunning(id)
      setRunNote(null)
      try {
        const res = await fetch(`${API_BASE}/trident2/run/${id}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kwargs: {} }),
        })
        const data = (await res.json()) as { ok?: boolean; result?: Feature['last_run']; detail?: string }
        if (!res.ok || !data?.result) throw new Error(data?.detail || `HTTP ${res.status}`)
        setItems((prev) =>
          prev ? prev.map((f) => (f.id === id ? { ...f, last_run: data.result } : f)) : prev,
        )
        setRunNote(`${id} 已跑完（纸面，未触碰订单）`)
      } catch (e: unknown) {
        setRunNote(`${id} 运行失败：${e instanceof Error ? e.message : String(e)}`)
      } finally {
        setRunning(null)
      }
    },
    [],
  )

  return (
    <section className="rounded-lg border border-border bg-card/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/70 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-2 font-mono text-[12px] font-semibold text-foreground transition hover:text-primary"
        >
          <span aria-hidden>{open ? '▼' : '▶'}</span>
          <span>{title}</span>
        </button>
        {hint ? <span className="text-[11px] text-muted-foreground">{hint}</span> : null}
        <div className="ml-auto flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
          {err ? (
            <span className="text-short">能力目录未响应</span>
          ) : loading ? (
            <span>载入中…</span>
          ) : (
            <>
              <span>{filtered.length} 项</span>
              <span>
                已运行 <span className="text-foreground">{ranCount}</span>/{filtered.length}
              </span>
            </>
          )}
          <button
            type="button"
            onClick={() => setTick((t) => t + 1)}
            className="rounded border border-muted-foreground/30 bg-card px-1.5 py-0.5 text-foreground hover:bg-muted/40"
            title="重新拉取最近一次运行结果"
          >
            刷新
          </button>
        </div>
      </div>

      {open ? (
        <div className="px-3 py-2">
          {runNote ? <div className="mb-2 font-mono text-[11px] text-muted-foreground">{runNote}</div> : null}

          {err ? (
            <div className="rounded border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
              目录接口 <span className="font-mono">/api/trident2/features</span> 当前没有响应（{err}）。
              这里不显示任何占位数据 —— 等主服务恢复后点「刷新」即可。
            </div>
          ) : loading ? (
            <div className="py-2 text-[11px] text-muted-foreground">载入中…</div>
          ) : filtered.length === 0 ? (
            <div className="rounded border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground">
              这个分类下没有匹配的功能（可能接口版本还未包含）。
            </div>
          ) : (
            <div className="flex flex-col divide-y divide-border/60">
              {filtered.map((f) => {
                const last = f.last_run
                const tone = STATUS_TONE[last?.status || ''] || 'text-muted-foreground/70'
                const protectedItem = PROTECTED_IDS.has(f.id)
                return (
                  <div key={f.id} className="flex flex-wrap items-start gap-x-2 gap-y-1 py-1.5">
                    <span className="mt-0.5 rounded-sm border border-border px-1 font-mono text-[10px] text-muted-foreground">
                      {f.id}
                    </span>
                    <span className="text-[12px] font-medium text-foreground">{f.name}</span>
                    {f.priority ? (
                      <span
                        className={`mt-0.5 font-mono text-[10px] ${f.priority === 'P0' ? 'text-primary' : 'text-muted-foreground/70'}`}
                      >
                        {f.priority}
                      </span>
                    ) : null}
                    {protectedItem ? (
                      <span className="mt-0.5 rounded border border-short/40 px-1 font-mono text-[10px] text-short">
                        保护 · 只出计划
                      </span>
                    ) : null}
                    {!f.implemented ? (
                      <span className="mt-0.5 rounded border border-border px-1 font-mono text-[10px] text-muted-foreground/60">
                        未实现
                      </span>
                    ) : null}

                    <span className="ml-auto flex items-center gap-2 font-mono text-[10px]">
                      {last ? (
                        <>
                          <span className={tone}>{STATUS_LABEL[last.status || ''] || last.status}</span>
                          <span className="text-muted-foreground/70">{fmtTs(last.run_ts)}</span>
                        </>
                      ) : (
                        <span className="text-muted-foreground/60">未运行</span>
                      )}
                      <button
                        type="button"
                        disabled={protectedItem || !f.implemented || running === f.id}
                        onClick={() => runOne(f.id)}
                        title={protectedItem ? '保护名单：只出计划/草稿，界面不触发执行' : '跑一次（只写 t2_* 纸面账本）'}
                        className="rounded border border-border px-1.5 py-0.5 text-foreground hover:bg-muted/40 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        {running === f.id ? '运行中…' : '跑一次'}
                      </button>
                    </span>

                    {(f.what || last?.headline) && (
                      <div className="w-full text-[11px] leading-snug text-muted-foreground">
                        {last?.headline ? (
                          <span className="text-foreground/80">{last.headline}</span>
                        ) : (
                          <span>{f.what}</span>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      ) : null}
    </section>
  )
}
