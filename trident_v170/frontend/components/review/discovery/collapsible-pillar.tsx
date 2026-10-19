'use client'

import { useEffect, useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 可折叠的 pillar 容器(v283+ · 解决 #1/#17)
 *
 * 设计动机:发现页 12 个子段一次性全部渲染,首屏 12 个 panel 同时挂载 + 60s 轮询,
 * 首屏压力巨大(尤其在低端机)。改成默认折叠次要 panel,首屏只渲染 4 个核心 panel
 * (intraday/fed/fedwatch/fedmailbox),其它 8 个点开才挂载。
 *
 * 关键不变量:
 *   · 折叠状态下不渲染 children(挂载被阻断 → 内部 usePoll 不创建定时器、不发请求)
 *   · 状态是 React state,不依赖浏览器 details 元素(便于配合 hash anchor 自动展开)
 *   · hash anchor (#disc-foo) 命中时若该 pillar 折叠,自动展开 + scrollIntoView
 *     (由 CollapsiblePillar 自己监听 location.hash 变化)
 */

export interface CollapsiblePillarProps {
  /** anchor id, 如 'disc-fed' */
  id: string
  /** emoji 图标 */
  icon: string
  /** 标题 */
  title: string
  /** tone 风格,传给 DiscoveryPillarNav 一致的色环 */
  tone: 'amber' | 'rose' | 'sky' | 'fuchsia' | 'violet' | 'cyan' | 'emerald' | 'indigo' | 'lime' | 'slate'
  /** 是否默认展开 */
  defaultOpen?: boolean
  /** 右上角可选的额外元素(如: 60s 自动刷新徽章) */
  trailing?: ReactNode
  children: ReactNode
}

/**
 * v283+ · 折叠状态 localStorage 持久化 key。
 * - 单一 key 存所有 pillar 状态(便于一次性 reset/导出)
 * - 后缀 v1:如果未来改 schema 命名空间可读
 */
const STORAGE_KEY = 'disc-pillar-state-v1'

function readStored(): Record<string, boolean> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, boolean>
    }
  } catch { /* corrupted JSON / disabled storage */ }
  return {}
}

function writeStored(map: Record<string, boolean>): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
  } catch { /* quota / disabled */ }
}

const TONE_RING_OPEN: Record<CollapsiblePillarProps['tone'], string> = {
  amber:   'border-amber-500/40   bg-amber-500/5',
  rose:    'border-rose-500/40    bg-rose-500/5',
  sky:     'border-sky-500/40     bg-sky-500/5',
  fuchsia: 'border-fuchsia-500/40 bg-fuchsia-500/5',
  violet:  'border-violet-500/40  bg-violet-500/5',
  cyan:    'border-cyan-500/40    bg-cyan-500/5',
  emerald: 'border-emerald-500/40 bg-emerald-500/5',
  indigo:  'border-indigo-500/40  bg-indigo-500/5',
  lime:    'border-lime-500/40    bg-lime-500/5',
  slate:   'border-slate-500/40   bg-slate-500/5',
}
const TONE_BAR: Record<CollapsiblePillarProps['tone'], string> = {
  amber:   'bg-amber-500/30 text-amber-700 dark:text-amber-300',
  rose:    'bg-rose-500/30 text-rose-700 dark:text-rose-300',
  sky:     'bg-sky-500/30 text-sky-700 dark:text-sky-300',
  fuchsia: 'bg-fuchsia-500/30 text-fuchsia-700 dark:text-fuchsia-300',
  violet:  'bg-violet-500/30 text-violet-700 dark:text-violet-300',
  cyan:    'bg-cyan-500/30 text-cyan-700 dark:text-cyan-300',
  emerald: 'bg-emerald-500/30 text-emerald-700 dark:text-emerald-300',
  indigo:  'bg-indigo-500/30 text-indigo-700 dark:text-indigo-300',
  lime:    'bg-lime-500/30 text-lime-700 dark:text-lime-300',
  slate:   'bg-slate-500/30 text-slate-700 dark:text-slate-300',
}

export function CollapsiblePillar({
  id,
  icon,
  title,
  tone,
  defaultOpen = true,
  trailing,
  children,
}: CollapsiblePillarProps) {
  const [open, setOpen] = useState<boolean>(defaultOpen)
  // v283+ · localStorage 持久化 · 用 hydrated 标志避免 SSR/客户端不一致 + 首挂载期间错写
  const [hydrated, setHydrated] = useState<boolean>(false)

  // v283+ · 挂载时初始化:hash anchor 优先,否则读 localStorage
  useEffect(() => {
    if (typeof window !== 'undefined' && window.location.hash === `#${id}`) {
      // 深链命中 → 强制展开(覆盖 localStorage)+ 滚动到 anchor
      setOpen(true)
      setHydrated(true)
      requestAnimationFrame(() => {
        document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      })
      return
    }
    const stored = readStored()
    if (id in stored && typeof stored[id] === 'boolean') {
      setOpen(stored[id])
    }
    setHydrated(true)
  }, [id])

  // v283+ · 任何 open 变化都写回 localStorage(让用户偏好跨刷新保留)
  useEffect(() => {
    if (!hydrated) return
    const map = readStored()
    if (map[id] === open) return
    writeStored({ ...map, [id]: open })
  }, [open, id, hydrated])

  // v283+ · T1 未读红点 · 记录用户最近一次"手动展开"该 pillar 的时间(ms)
  // 仅在用户主动打开(不是初次挂载自动展开)时更新,这样:
  //   - 初次刷新页面 → lastVisit 为空 → PillarNav 显示红点提醒
  //   - 用户主动打开过 → lastVisit 有值 → 红点消失(直到下次有"新的更新")
  // key 命名:disc-lastvisit-{id},id 与 DiscoveryPillarNav 的 pillar.id 对齐
  useEffect(() => {
    if (!hydrated || !open) return
    if (typeof window === 'undefined') return
    try {
      window.localStorage.setItem(`disc-lastvisit-${id}`, String(Date.now()))
    } catch { /* quota / disabled */ }
  }, [open, id, hydrated])

  // v283+ · pillar-nav anchor (#disc-foo) 命中且当前折叠时,自动展开。
  // 监听 hashchange(用户点 nav);初次挂载 hash 已匹配的由上面 effect 处理。
  useEffect(() => {
    const check = () => {
      if (typeof window === 'undefined') return
      if (window.location.hash === `#${id}` && !open) {
        setOpen(true)
        // 展开后再 scrollIntoView,确保看到的是展开后的内容
        requestAnimationFrame(() => {
          document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
        })
      }
    }
    window.addEventListener('hashchange', check)
    return () => window.removeEventListener('hashchange', check)
  }, [id, open])

  return (
    <div
      id={id}
      className={cn(
        'scroll-mt-3 rounded border transition-colors',
        open ? TONE_RING_OPEN[tone] : 'border-border bg-secondary/10',
      )}
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={`${id}-body`}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-secondary/30"
      >
        <ChevronRight
          className={cn(
            'h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform',
            open && 'rotate-90',
          )}
          aria-hidden
        />
        <span aria-hidden className="text-sm leading-none">{icon}</span>
        <span className="font-mono text-xs font-bold tracking-wide text-foreground">
          {title}
        </span>
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-mono text-[9px] font-bold',
            TONE_BAR[tone],
          )}
        >
          {open ? '▼ 展开中' : '▶ 折叠'}
        </span>
        {trailing && <span className="ml-auto font-mono text-[9px] text-muted-foreground">{trailing}</span>}
      </button>

      {open && (
        <div id={`${id}-body`} className="border-t border-current/10 px-2.5 py-2">
          {children}
        </div>
      )}
    </div>
  )
}