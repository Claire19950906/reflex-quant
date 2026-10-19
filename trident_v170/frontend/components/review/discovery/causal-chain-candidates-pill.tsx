'use client'

import { useEffect, useRef, useState } from 'react'
import { Star, ExternalLink, X, Loader2 } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { API_BASE } from '@/lib/api'
import { cn } from '@/lib/utils'

/**
 * v283+ · 因果链「人工候选池」角标 + popover
 *
 * 设计动机:
 *   /discover 多个 panel 都能"入候选"(event-radar / meta-cognition /
 *   weekly-observations / storage-industry / 自填假设 …),
 *   但用户没有"我投了多少"的可视反馈。做一个轻量 chip:
 *   - 5min 轮询 /api/causal/chains
 *   - 过滤 origin='manual'(人工/手填)= 用户的全部候选
 *   - 点击 chip 弹 popover,显示最近 5 条候选(cause/effect/weight/启用状态)
 *   - 底部"查看全部"跳到 /causal-chains 管理页
 *
 * popover 实现要点:
 *   - 用 `position: fixed` + getBoundingClientRect 定位,避免被 nav 的
 *     `overflow-x-auto` 裁切(fixed 元素脱离祖先 overflow 影响)
 *   - mousedown 外部 / ESC 关闭
 *   - open=false 时卸载 popover(避免无意义的渲染 + 状态干扰)
 */

type Chain = {
  id: number
  name?: string
  origin?: string
  enabled?: number
  cause: string
  effect: string
  created_at?: number
  weight?: number
  trigger_words?: string
}

const POPOVER_PREVIEW = 5
const POPOVER_WIDTH = 320

/**
 * v283+ · 来源面板识别 · 根据 cause 前缀匹配
 * (各 panel 的 PushToCausalChainButton 已统一使用 `{source} ·` / `{source}:` 前缀)
 * 不在前缀列表的(用户手填/event-radar 早期无前缀)归为「事件」。
 */
const SOURCE_META: ReadonlyArray<{
  match: (cause: string) => boolean
  label: string
  icon: string
  cls: string
}> = [
  { match: (c) => c.startsWith('weekly-obs'),   label: '周度',   icon: '📅', cls: 'border-violet-500/40 text-violet-700 dark:text-violet-300' },
  { match: (c) => c.startsWith('storage'),      label: '存储',   icon: '💾', cls: 'border-emerald-500/40 text-emerald-700 dark:text-emerald-300' },
  { match: (c) => c.startsWith('asset-scout'),  label: '猎手',   icon: '🎯', cls: 'border-fuchsia-500/40 text-fuchsia-700 dark:text-fuchsia-300' },
  { match: (c) => c.startsWith('跨资产同步'),   label: '跨资产', icon: '🌐', cls: 'border-rose-500/40 text-rose-700 dark:text-rose-300' },
  { match: (c) => c.startsWith('周期'),         label: '周期',   icon: '☯️', cls: 'border-violet-500/40 text-violet-700 dark:text-violet-300' },
  { match: (c) => c.startsWith('meta-cog'),     label: '元认知', icon: '🤖', cls: 'border-cyan-500/40 text-cyan-700 dark:text-cyan-300' },
]
const FALLBACK_SOURCE = { label: '事件', icon: '⚡', cls: 'border-sky-500/40 text-sky-700 dark:text-sky-300' }

function detectSource(cause: string): { label: string; icon: string; cls: string } {
  for (const m of SOURCE_META) {
    if (m.match(cause)) return m
  }
  return FALLBACK_SOURCE
}

export function CausalChainCandidatesPill() {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 })
  const rootRef = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)

  const { data, loading, error, refresh } = usePoll<Chain[]>(
    `${API_BASE}/api/causal/chains`,
    {
      intervalMs: 5 * 60_000,
      transform: (raw) => {
        if (!Array.isArray(raw)) return null
        const chains = raw as unknown as Chain[]
        return chains.filter((c) => !c.origin || c.origin === 'manual')
      },
    },
  )

  const total = data?.length ?? 0
  const enabledCount = data?.filter((c) => c.enabled !== 0).length ?? 0
  // v283+ · T5 搜索框 + T6 来源分组:先把最近 N 条按关键词过滤,再按来源 prefix label 分组
  const [filterQuery, setFilterQuery] = useState<string>('')
  const sourceMatchedRaw = (() => {
    const q = filterQuery.trim().toLowerCase()
    const src = data ?? []
    if (!q) return src
    return src.filter((c) => {
      const a = (c.cause || '').toLowerCase()
      const b = (c.effect || '').toLowerCase()
      return a.includes(q) || b.includes(q)
    })
  })()
  // 注:分组只针对预览;没搜索时取前 N,有搜索时不分页(展示所有匹配项)
  const recent = filterQuery.trim()
    ? sourceMatchedRaw
    : sourceMatchedRaw.slice(0, POPOVER_PREVIEW)
  // 按来源分组(来源 label 当 key)
  const recentBySource: Record<string, { meta: { label: string; icon: string; cls: string }; items: typeof recent }> = {}
  for (const c of recent) {
    const meta = detectSource(c.cause)
    const key = meta.label
    if (!recentBySource[key]) recentBySource[key] = { meta, items: [] }
    recentBySource[key].items.push(c)
  }
  // v283+ · T6 来源分组 · 每组默认折叠(避免一个来源占满屏),用户可点开
  const [openSources, setOpenSources] = useState<Set<string>>(new Set())

  // v283+ · 单条启停切换(乐观更新：成功才 refresh，失败显示 toast)
  const [toggling, setToggling] = useState<Set<number>>(new Set())
  const [toggleErr, setToggleErr] = useState<string | null>(null)
  const errTimerRef = useRef<number | null>(null)

  // v283+ · unmount / popover 关闭时清理 setTimeout(防内存泄漏 + 错状态)
  useEffect(() => {
    return () => {
      if (errTimerRef.current !== null) {
        window.clearTimeout(errTimerRef.current)
        errTimerRef.current = null
      }
    }
  }, [])

  const handleToggleChain = async (chainId: number) => {
    if (toggling.has(chainId)) return
    setToggling((prev) => new Set(prev).add(chainId))
    setToggleErr(null)
    if (errTimerRef.current !== null) {
      window.clearTimeout(errTimerRef.current)
      errTimerRef.current = null
    }
    try {
      const r = await fetch(`${API_BASE}/api/causal/chains/${chainId}/toggle`, {
        method: 'PATCH',
        cache: 'no-store',
      })
      if (!r.ok) {
        const detail = await r.text().catch(() => '')
        throw new Error(`HTTP ${r.status}${detail ? ' · ' + detail.slice(0, 80) : ''}`)
      }
      // 成功后立即 refresh 拉取最新状态
      await refresh()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setToggleErr(`#${chainId} 切换失败:${msg}`)
      // 4s 自动清除错误提示
      errTimerRef.current = window.setTimeout(() => {
        setToggleErr(null)
        errTimerRef.current = null
      }, 4000)
    } finally {
      setToggling((prev) => {
        const next = new Set(prev)
        next.delete(chainId)
        return next
      })
    }
  }

  // v283+ · weight 一键 +/- 调整(步进 0.05,clamp 0-2)
  const [weightToggling, setWeightToggling] = useState<Set<number>>(new Set())

  const handleAdjustWeight = async (
    chainId: number,
    currentWeight: number,
    delta: number,
  ) => {
    if (weightToggling.has(chainId)) return
    const next = Math.max(0, Math.min(2, Number((currentWeight + delta).toFixed(2))))
    // 边界(到 0 或 2)时若已无变化就跳过
    if (next === currentWeight) return
    setWeightToggling((prev) => new Set(prev).add(chainId))
    setToggleErr(null)
    if (errTimerRef.current !== null) {
      window.clearTimeout(errTimerRef.current)
      errTimerRef.current = null
    }
    try {
      const r = await fetch(`${API_BASE}/api/causal/chains/${chainId}/weight`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ weight: next }),
        cache: 'no-store',
      })
      if (!r.ok) {
        const detail = await r.text().catch(() => '')
        throw new Error(`HTTP ${r.status}${detail ? ' · ' + detail.slice(0, 80) : ''}`)
      }
      // 成功后立即 refresh 拉取最新权重
      await refresh()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setToggleErr(`#${chainId} 权重调整失败:${msg}`)
      errTimerRef.current = window.setTimeout(() => {
        setToggleErr(null)
        errTimerRef.current = null
      }, 4000)
    } finally {
      setWeightToggling((prev) => {
        const n = new Set(prev)
        n.delete(chainId)
        return n
      })
    }
  }

  useEffect(() => {
    if (!open) return
    const onDocMouseDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  const toggle = () => {
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect()
      const belowTop = r.bottom + 4
      const aboveTop = r.top - 4
      const wouldOverflow = belowTop + 320 > window.innerHeight
      setPos({
        left: Math.max(8, Math.min(r.left, window.innerWidth - POPOVER_WIDTH - 8)),
        top: wouldOverflow ? Math.max(8, aboveTop - 320) : belowTop,
      })
    }
    setOpen((o) => !o)
  }

  // v283+ · T6 来源分组 · 切换某来源组的折叠状态
  const toggleSourceGroup = (label: string) => {
    setOpenSources((prev) => {
      const n = new Set(prev)
      if (n.has(label)) n.delete(label)
      else n.add(label)
      return n
    })
  }

  // v283+ · T4 批量启停 · 三个模式:
  //   - mode='set' ids=[1,2,3] enabled=true  → 全部启用/停用
  //   - mode='flip' ids=[1,2,3]              → 逐个翻(后端读当前 enabled 反转)
  //   - mode='scope' enabled=true            → 全表范围(本页面只显示 manual 的)
  // 失败显示 toast,成功直接 refresh
  const [bulkRunning, setBulkRunning] = useState<boolean>(false)
  const handleBulk = async (mode: 'set' | 'flip' | 'scope', enabled?: boolean, ids?: number[]) => {
    if (bulkRunning) return
    setBulkRunning(true)
    setToggleErr(null)
    if (errTimerRef.current !== null) {
      window.clearTimeout(errTimerRef.current)
      errTimerRef.current = null
    }
    try {
      const body: Record<string, unknown> = { mode }
      if (mode === 'set') {
        body.ids = ids
        body.enabled = enabled
      } else if (mode === 'flip') {
        body.ids = ids
      } else {
        // scope
        body.enabled = enabled
        body.origin = 'manual'  // 只在 manual 来源里翻/设
      }
      const r = await fetch(`${API_BASE}/api/causal/chains/bulk-toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        cache: 'no-store',
      })
      if (!r.ok) {
        const detail = await r.text().catch(() => '')
        throw new Error(`HTTP ${r.status}${detail ? ' · ' + detail.slice(0, 80) : ''}`)
      }
      await refresh()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setToggleErr(`批量失败:${msg}`)
      errTimerRef.current = window.setTimeout(() => {
        setToggleErr(null)
        errTimerRef.current = null
      }, 4000)
    } finally {
      setBulkRunning(false)
    }
  }

  // T6 · 单条候选渲染(从原来 inline 的 li 抽出,避免重复导致 key 重复警告)
  // eslint-disable-next-line react/no-unstable-nested-components
  const renderItem = (c: Chain) => {
    const enabled = c.enabled !== 0
    const src = detectSource(c.cause)
    return (
      <li
        key={c.id}
        className={cn(
          'rounded border px-1.5 py-1',
          enabled
            ? 'border-amber-500/20 bg-amber-500/5'
            : 'border-border bg-secondary/10 opacity-60',
        )}
      >
        <div className="mb-0.5 flex items-center gap-1">
          <span
            className={cn(
              'inline-flex items-center gap-0.5 rounded border px-1 text-[8px] font-bold',
              src.cls,
            )}
            title={`来源面板:${src.label}`}
          >
            <span aria-hidden>{src.icon}</span>
            {src.label}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => handleToggleChain(c.id)}
            disabled={toggling.has(c.id)}
            title={enabled ? '点击停用' : '点击启用'}
            className={cn(
              'shrink-0 rounded border px-1 text-[8px] font-bold transition-colors',
              'disabled:cursor-not-allowed disabled:opacity-50',
              enabled
                ? 'border-emerald-500/40 text-emerald-600 hover:bg-rose-500/10 hover:text-rose-500 dark:text-emerald-400'
                : 'border-rose-500/40 text-rose-500 hover:bg-emerald-500/10 hover:text-emerald-600',
            )}
          >
            {toggling.has(c.id) ? '…' : enabled ? '启用' : '停用'}
          </button>
          <span className="tabular-nums text-[9px] text-muted-foreground">#{c.id}</span>
          {c.weight != null && (
            <span className="ml-auto inline-flex items-center gap-0.5 rounded border border-border bg-secondary/30 px-0.5">
              <button
                type="button"
                onClick={() => handleAdjustWeight(c.id, c.weight!, -0.05)}
                disabled={weightToggling.has(c.id) || c.weight <= 0}
                title="降低 0.05"
                className="px-1 text-[10px] font-bold text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-30"
              >
                −
              </button>
              <span
                className={cn(
                  'min-w-[2.4em] text-center tabular-nums text-[9px] font-bold',
                  weightToggling.has(c.id) ? 'text-amber-500' : 'text-foreground',
                )}
                title={`当前权重 ${c.weight.toFixed(2)}（后端范围 0-100，前端步进 0.05）`}
              >
                {weightToggling.has(c.id) ? '…' : c.weight.toFixed(2)}
              </span>
              <button
                type="button"
                onClick={() => handleAdjustWeight(c.id, c.weight!, +0.05)}
                disabled={weightToggling.has(c.id) || c.weight >= 2}
                title="增加 0.05"
                className="px-1 text-[10px] font-bold text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-30"
              >
                +
              </button>
            </span>
          )}
        </div>
        <div className="mt-0.5 truncate text-foreground" title={c.cause}>
          <strong>因:</strong> {c.cause}
        </div>
        <div className="truncate text-foreground/80" title={c.effect}>
          <strong>果:</strong> {c.effect}
        </div>
      </li>
    )
  }

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        ref={btnRef}
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={
          error
            ? `⚠ 候选池加载失败:${error}`
            : loading && total === 0
              ? '⏳ 加载中...'
              : `⭐ 我的候选池:共 ${total} 条(启用 ${enabledCount} 条)\n点击查看最近 ${POPOVER_PREVIEW} 条`
        }
        className={cn(
          'inline-flex shrink-0 items-center gap-1.5 rounded border px-2 py-1 font-mono text-[10px] transition-colors',
          'border-amber-500/40 bg-amber-500/10 text-amber-700 hover:bg-amber-500/20 dark:text-amber-300',
          open && 'ring-1 ring-amber-500/60',
        )}
      >
        <Star className={cn('h-3 w-3 text-amber-500', total > 0 && 'fill-amber-500')} aria-hidden />
        <span className="font-bold">候选池</span>
        {error ? (
          <span className="text-rose-500">!</span>
        ) : loading && total === 0 ? (
          <span className="text-muted-foreground">…</span>
        ) : (
          <span className="tabular-nums">{total}</span>
        )}
        {enabledCount > 0 && enabledCount < total && (
          <>
            <span className="text-[9px] text-muted-foreground">/启用 {enabledCount}</span>
            {/* T8 进度条 · 已启用 / 总数 的 mini bar · 复用 tone 色 */}
            <span
              className="ml-0.5 inline-block h-1 w-7 overflow-hidden rounded-full bg-rose-500/20 align-middle"
              title={`已启用 ${enabledCount}/${total}(${Math.round((enabledCount / Math.max(1, total)) * 100)}%)`}
            >
              <span
                className="block h-full bg-emerald-500/70 transition-all"
                style={{ width: `${(enabledCount / Math.max(1, total)) * 100}%` }}
              />
            </span>
          </>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="我的因果链候选池"
          style={{ left: pos.left, top: pos.top, width: POPOVER_WIDTH }}
          className="fixed z-50 rounded border border-amber-500/40 bg-popover shadow-lg shadow-amber-500/10"
        >
          {/* header */}
          <div className="flex items-center gap-1.5 border-b border-amber-500/30 bg-amber-500/10 px-2 py-1.5 font-mono text-[10px]">
            <Star className="h-3 w-3 fill-amber-500 text-amber-500" aria-hidden />
            <span className="font-bold text-amber-700 dark:text-amber-300">我的候选池</span>
            <span className="text-muted-foreground">·</span>
            <span className="text-muted-foreground">共 {total} 条 / 启用 {enabledCount}</span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="ml-auto rounded p-0.5 text-muted-foreground hover:bg-secondary/40 hover:text-foreground"
              title="关闭"
              aria-label="关闭候选池"
            >
              <X className="h-3 w-3" />
            </button>
          </div>

          {/* T5 搜索框 · 按 cause / effect 实时过滤 */}
          <div className="border-b border-amber-500/20 px-2 py-1">
            <input
              type="text"
              value={filterQuery}
              onChange={(e) => setFilterQuery(e.target.value)}
              placeholder="🔍 搜 cause / effect…"
              className="w-full rounded border border-amber-500/30 bg-background/60 px-1.5 py-0.5 font-mono text-[10px] text-foreground placeholder:text-muted-foreground focus:border-amber-500/60 focus:outline-none"
            />
          </div>

          {/* body: 最近 N 条 */}
          <div className="max-h-80 overflow-y-auto p-1.5 font-mono text-[10px]">
            {toggleErr && (
              <div
                role="alert"
                className="mb-1 rounded border border-rose-500/40 bg-rose-500/10 px-1.5 py-1 text-rose-600 dark:text-rose-400"
              >
                ⚠ {toggleErr}
              </div>
            )}
            {error ? (
              <div className="px-1.5 py-2 text-rose-500">⚠ {error}</div>
            ) : loading && recent.length === 0 ? (
              <div className="flex items-center gap-1.5 px-1.5 py-2 text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" />
                加载中…
              </div>
            ) : recent.length === 0 ? (
              <div className="px-1.5 py-2 text-muted-foreground">
                还没有候选 — 去 ⭐ 各 panel 投一条试试
              </div>
            ) : (
              // v283+ · T6 来源分组 · 按 detectSource(cause) 分组,每组可折叠
              <ul className="space-y-1">
                {Object.entries(recentBySource).map(([srcKey, group]) => {
                  const isOpen = openSources.has(srcKey)
                  return (
                    <li key={srcKey} className="rounded border border-amber-500/15 bg-amber-500/[0.02]">
                      <button
                        type="button"
                        onClick={() => toggleSourceGroup(srcKey)}
                        aria-expanded={isOpen}
                        className={cn(
                          'flex w-full items-center gap-1 rounded px-1 py-0.5 transition-colors hover:bg-amber-500/10',
                          group.meta.cls,
                        )}
                      >
                        <span aria-hidden>{group.meta.icon}</span>
                        <span className="font-bold">{srcKey}</span>
                        <span className="text-[8px] opacity-60">{group.items.length}</span>
                        <span className="ml-auto text-[9px] opacity-60">{isOpen ? '▼' : '▶'}</span>
                      </button>
                      {isOpen && (
                        <ul className="mt-0.5 space-y-1 px-0.5 pb-0.5">
                          {group.items.map(renderItem)}
                        </ul>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>

          {/* footer: T4 批量操作 + 查看全部 */}
          <div className="border-t border-amber-500/20 px-2 py-1.5">
            <div className="grid grid-cols-3 gap-1">
              <button
                type="button"
                onClick={() => handleBulk('scope', true)}
                disabled={bulkRunning || total === 0}
                title="把 manual 来源全部启用"
                className="rounded border border-emerald-500/30 bg-emerald-500/5 px-1 py-0.5 font-mono text-[9px] font-bold text-emerald-700 transition-colors hover:bg-emerald-500/15 disabled:cursor-not-allowed disabled:opacity-40 dark:text-emerald-300"
              >
                全部启用
              </button>
              <button
                type="button"
                onClick={() => handleBulk('scope', false)}
                disabled={bulkRunning || total === 0}
                title="把 manual 来源全部停用"
                className="rounded border border-rose-500/30 bg-rose-500/5 px-1 py-0.5 font-mono text-[9px] font-bold text-rose-700 transition-colors hover:bg-rose-500/15 disabled:cursor-not-allowed disabled:opacity-40 dark:text-rose-300"
              >
                全部停用
              </button>
              <button
                type="button"
                onClick={() => handleBulk('scope')}
                disabled={bulkRunning || total === 0}
                title="manual 来源逐条翻转 enabled"
                className="rounded border border-amber-500/30 bg-amber-500/5 px-1 py-0.5 font-mono text-[9px] font-bold text-amber-700 transition-colors hover:bg-amber-500/15 disabled:cursor-not-allowed disabled:opacity-40 dark:text-amber-300"
              >
                {bulkRunning ? '…' : '全部翻转'}
              </button>
            </div>
            <a
              href="/causal-chains"
              className="mt-1 inline-flex w-full items-center justify-center gap-1 rounded border border-amber-500/30 bg-amber-500/5 px-2 py-1 font-mono text-[10px] text-amber-700 transition-colors hover:bg-amber-500/15 dark:text-amber-300"
            >
              查看全部 {total} 条 →
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        </div>
      )}
    </div>
  )
}
