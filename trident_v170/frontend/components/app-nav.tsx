'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

// 7 大类导航分组（v275 精简：原 8 大类合并发现/宏观/资金 → 一个 /discover 入口）：
//   信号 → 模拟 → 归因 → 反思 → 图谱 → 发现 → 分析过程
// 最后一块是「后台」（信息源 / ai提示词），用分隔标签区分。
type Tab = { href: string; label: string; badge?: string; badgeTitle?: string }

const MAIN_TABS: Tab[] = [
  { href: '/', label: '实时信号台' },
  { href: '/replay', label: '模拟盘', badge: 'PAPER' },
  { href: '/attribution', label: '行情归因' },
  // v291+ P0: 漏检信号专页 — 反思系统聚焦
  { href: '/missed-signals', label: '漏检信号', badge: 'v291', badgeTitle: 'v291+ 三维度细分 (A1/A2/A3 · B1/B2/B3 · C1/C2/C3)' },
  { href: '/review', label: '反思系统' },
  // v170 P0+ 入口合并：原 /experiences / /journal 顶层 tab 已删除（统一口径要求 §8）。
  // 二级子菜单见 /review/page.tsx 下设：交易反思日记（journal） + 策略经验库（experiences）。
  { href: '/knowledge-graph', label: '因果图谱' },
  // v275: 原 7 段探索研究 + 宏观日历 + 资金流动 三入口合并为一个 /discover
  // v2026.10.15: 去掉无解释的"9" badge, 改用 tooltip 提示「已收敛多源入口」
  { href: '/discover', label: '发现', badge: '多', badgeTitle: '已收敛 7+ 探索/宏观/资金入口' },
  // v302+ P3: 链上资金流向图 — 8 大类资产 × 4 类资金源(SVG 粒子动画)
  // v302+ Round 2: /capital-flow 页面未建,临时指向 /discover (发现页) 避免 404
  { href: '/discover', label: '资金流向', badge: '→', badgeTitle: '暂合并到 /discover, 待 /capital-flow 页面建好再拆开' },
  // v302+ P3: 跨市场异动扫描器 — 4 源聚合 + WS 实时 + 8 大类资产 + sector/套利/告警面板
  // 测试期望 href=/market-scanner（components/app-nav.test.tsx）。注释里提的 Round 2 临时指向
  // /discover 已不再需要——测试契约稳定下来之后,直接使用 /market-scanner 路由。
  { href: '/market-scanner', label: '行情扫描', badge: '→', badgeTitle: 'v302+ P3 跨市场异动扫描器 (4 源聚合 + WS 实时)' },
  // v180+ P0-3: 实时新闻流 — TechFlow + 东方财富 双源
  { href: '/news', label: '新闻流', badge: 'NEW', badgeTitle: 'P0-3 · 双源 RSS 镜像 · 5min 自动拉取' },
  { href: '/analysis-process', label: '分析过程' },
]

const ADMIN_TABS: Tab[] = [
  { href: '/sources', label: '信息源' },
  { href: '/connectivity', label: '联通' },
  { href: '/prompts', label: 'ai提示词' },
]

function isActive(pathname: string, href: string) {
  if (href === '/') return pathname === '/'
  return pathname === href || pathname.startsWith(`${href}/`)
}

function TabLink({ tab, pathname }: { tab: Tab; pathname: string }) {
  const active = isActive(pathname, tab.href)
  return (
    <Link
      key={tab.href}
      href={tab.href}
      prefetch={false}
      className={`flex shrink-0 items-center gap-2 border-b-2 px-3 py-3 font-mono text-xs uppercase tracking-wider transition-colors sm:px-4 sm:tracking-widest ${
        active
          ? 'border-primary text-primary'
          : 'border-transparent text-muted-foreground hover:text-foreground'
      }`}
    >
      <span>{tab.label}</span>
      {tab.badge ? (
        <span className={`rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-bold ${
          active
            ? 'border-hold/40 bg-hold/10 text-hold'
            : 'border-border bg-secondary text-muted-foreground'
        }`}>
          {tab.badge}
        </span>
      ) : null}
    </Link>
  )
}

export function AppNav() {
  const pathname = usePathname() || '/'
  const adminActive = ADMIN_TABS.some((t) => isActive(pathname, t.href))
  // v302+ P0: 视口宽度不足以容纳所有 tab 时,显示右侧 fade-out 渐变提示用户
  // 还有更多内容可向右滚动。scrollRef 用于测量 scrollLeft/scrollWidth。
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const [canScrollRight, setCanScrollRight] = useState(false)
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const update = () => {
      // 仅当内容宽度 > 容器宽度 且 未滚到最右时显示渐变
      setCanScrollRight(
        el.scrollWidth - el.clientWidth - el.scrollLeft > 4,
      )
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    // 视口 resize 也要重测
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => {
      el.removeEventListener('scroll', update)
      ro.disconnect()
    }
  }, [pathname])
  return (
    // sticky top-[88px] = DashboardHeader h-16 (64px) + MarketHoursBar h-6 (24px)
    // z-[5] 压在 MarketHoursBar 的 z-10 下面，避免遮盖市场状态条
    <nav className="sticky top-[88px] z-[5] border-b border-border bg-card" aria-label="主视图">
      <div className="relative">
        <div
          ref={scrollRef}
          className="thin-scroll mx-auto flex w-full max-w-[1600px] items-center gap-1 overflow-x-auto px-3 md:px-6"
        >
          {MAIN_TABS.map((tab) => <TabLink key={tab.href} tab={tab} pathname={pathname} />)}
          {/* 后台分组分隔标签 */}
          <span
            aria-hidden
            className={`mx-2 hidden h-5 w-px shrink-0 bg-border sm:block ${
              adminActive ? 'bg-primary/50' : ''
            }`}
          />
          <span
            aria-hidden
            className={`hidden shrink-0 select-none border-b-2 px-2 pb-3 pt-3 font-mono text-[10px] uppercase tracking-widest sm:inline-block ${
              adminActive ? 'border-primary/60 text-primary/70' : 'border-transparent text-muted-foreground/60'
            }`}
          >
            后台
          </span>
          {ADMIN_TABS.map((tab) => <TabLink key={tab.href} tab={tab} pathname={pathname} />)}
        </div>
        {/* v302+ P0: 右侧 fade-out 渐变 — 视觉提示「还有更多内容可向右滚动」。
            pointer-events-none 确保不挡 tab 点击;用 bg-card 同色起点保持视觉对齐。 */}
        {canScrollRight ? (
          <div
            aria-hidden
            className="pointer-events-none absolute inset-y-0 right-0 w-12 bg-gradient-to-l from-card to-transparent"
          />
        ) : null}
      </div>
    </nav>
  )
}
