'use client'

import { useEffect, useState } from 'react'
import { CausalChainCandidatesPill } from './causal-chain-candidates-pill'
import { cn } from '@/lib/utils'

/** 12 大支柱导航(可点击 anchor 到子段)
 *  v275 增量: 加 #disc-macro 宏观日历 + #disc-flows 资金流动,
 *  从原 /calendar + /flows 整建制并入, 顶层入口只剩 /discover。
 *  v170+ P2 增量: 加 #disc-intraday 日内流动性 + #disc-weekly 周度观察
 *                 + #disc-storage 存储行业(海力士专用 · 慢变量)。
 */
type Pillar = { id: string; icon: string; title: string; tone: string }

const PILLARS: Pillar[] = [
  // ── v170+ P2 新增(顶部 banner · 开盘就能看到) ──
  { id: 'intraday', icon: '⚡', title: '日内流动性 · 24h 跳动', tone: 'cyan' },
  // ── 7 大探索研究支柱(原) ──
  { id: 'fed',    icon: '🏛',  title: 'Fed 利率预期 + 传导',  tone: 'amber'  },
  { id: 'fedwatch', icon: '📡', title: 'Fed 全景监控(v276)',   tone: 'amber' },
  // ── v277 新增 ──
  { id: 'fedmailbox', icon: '📧', title: 'Fed 邮箱箱(v277)', tone: 'amber' },
  { id: 'cross',  icon: '🌐',  title: '跨资产同步异常',        tone: 'rose'   },
  { id: 'asset',  icon: '🎯',  title: '强信息驱动资产猎手',    tone: 'fuchsia'},
  { id: 'event',  icon: '⚡',  title: '事件驱动快速因果',      tone: 'sky'    },
  { id: 'cycle',  icon: '☯️', title: '周期维度(阴阳共振)',    tone: 'violet' },
  { id: 'meta',   icon: '🤖',  title: 'AI 元认知自检',          tone: 'cyan'   },
  { id: 'collab', icon: '🌍',  title: '多机协作·时区参考',     tone: 'emerald'},
  // ── v170+ P2 新增(慢变量校验) ──
  { id: 'weekly',  icon: '📅', title: '周度观察 · ICSA/拍卖', tone: 'violet' },
  { id: 'storage', icon: '💾', title: '存储行业 · 海力士专用', tone: 'emerald' },
  // ── v275 新增 ──
  { id: 'macro',  icon: '📅',  title: '宏观日历·BLS/FRED/金十', tone: 'indigo' },
  { id: 'flows',  icon: '💸',  title: '资金流动·跨市场公开源',  tone: 'lime'   },
]

function toneRing(tone: string): string {
  switch (tone) {
    case 'amber':   return 'border-amber-500/30  bg-amber-500/5'
    case 'rose':    return 'border-rose-500/30   bg-rose-500/5'
    case 'sky':     return 'border-sky-500/30    bg-sky-500/5'
    case 'fuchsia': return 'border-fuchsia-500/30 bg-fuchsia-500/5'
    case 'violet':  return 'border-violet-500/30 bg-violet-500/5'
    case 'cyan':    return 'border-cyan-500/30   bg-cyan-500/5'
    case 'emerald': return 'border-emerald-500/30 bg-emerald-500/5'
    case 'indigo':  return 'border-indigo-500/30 bg-indigo-500/5'
    case 'lime':    return 'border-lime-500/30   bg-lime-500/5'
    default:        return 'border-border bg-secondary/30'
  }
}

// v283+ · T1 未读红点 · 从 localStorage 读每个 pillar 最近访问时间(ms)
function readLastVisit(pillarId: string): number | null {
  if (typeof window === 'undefined') return null
  try {
    const v = window.localStorage.getItem(`disc-lastvisit-${pillarId}`)
    if (!v) return null
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  } catch { return null }
}

export function DiscoveryPillarNav() {
  // v283+ · T1 未读红点 · mounted 时一次性读所有 pillar lastVisit
  // (之后每个 pillar 自己更新自己,我们挂 storage 事件则可响应,但 1 次扫描够用了)
  const [lastVisits, setLastVisits] = useState<Record<string, number | null>>({})

  useEffect(() => {
    const map: Record<string, number | null> = {}
    for (const p of PILLARS) map[p.id] = readLastVisit(p.id)
    setLastVisits(map)
  }, [])

  return (
    <nav aria-label="发现板块子段导航" className="thin-scroll -mx-1 flex flex-nowrap gap-1.5 overflow-x-auto px-1 pb-1">
      {/* v283+ · 候选池角标(用户投到 causal_chains 的人工条目总数) */}
      <CausalChainCandidatesPill />
      {PILLARS.map((p) => {
        // T1:没访问过 = 显示红点(用户从没见过这个 pillar 内容)。
        const isNew = lastVisits[p.id] == null
        return (
          <a key={p.id} href={`#disc-${p.id}`}
            className={cn(
              'relative inline-flex shrink-0 items-center gap-1.5 rounded border px-2 py-1 font-mono text-[10px] transition-colors',
              'border-border bg-secondary/30 text-muted-foreground hover:text-foreground',
              toneRing(p.tone),
            )}>
            <span aria-hidden>{p.icon}</span>
            <span className="font-semibold">{p.title}</span>
            {isNew && (
              <span
                aria-label="未访问"
                title="尚未展开过此段 · 点击查看"
                className="ml-0.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-rose-500 ring-1 ring-rose-500/40"
              />
            )}
          </a>
        )
      })}
    </nav>
  )
}