'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'

// v350-UI: 导航从「13 个入口 + 3 个分组标签平铺」改成「7 个主入口 + 更多」。
//
// 为什么改：1680px 下这一行要塞 16 个元素，字号被压到 10~11px，
//   分组标签（交易/分析/研究/后台）跟 tab 抢同一行的横向空间，
//   用户的原话是"页面特别混乱"。判断标准很简单——第一眼能不能看清有哪几块。
//
// 取舍：只用得最频繁的 7 个留在外面，其余收进「更多」下拉；
//   分组标签不再单独占位，改由「更多」里的分组小标题承担分区作用。
//   路由一个都没删，旧书签照常可用。
type Tab = { href: string; label: string }

const PRIMARY_TABS: Tab[] = [
  // 6 个核心视图全部在主导航：实时台 / 背离 / 归因 / 资金 / 因果 / 反思。
  // 模拟盘、漏检信号属低频诊断，收进「更多」，避免主行继续膨胀。
  { href: '/', label: '实时信号台' },
  { href: '/divergence', label: '背离雷达' },
  { href: '/attribution', label: '行情归因' },
  { href: '/capital-flow', label: '资金流向' },
  { href: '/knowledge-graph', label: '因果图谱' },
  { href: '/review', label: '反思健康' },
  { href: '/discover', label: '发现' },
  { href: '/news', label: '新闻流' },
]

const MORE_GROUPS: { label: string; tabs: Tab[] }[] = [
  {
    // v345: /backtest（策略回测）和 /history（历史新闻交易回溯）两个页面做好了，
    //   但导航里一直没有入口 —— 等于做好了没人能进去。收进「更多 → 交易」。
    label: '交易',
    tabs: [
      { href: '/actionable', label: '可执行信号' },
    { href: '/profit', label: '能不能赚钱' },
    { href: '/edge', label: '优势体检' },
    { href: '/guards', label: '开仓闸' },
    { href: '/bets', label: '该不该开火' },
    { href: '/ledger', label: '账本' },
    { href: '/trading-health', label: '交易健康' },
      { href: '/replay', label: '模拟盘' },
      { href: '/backtest', label: '策略回测' },
      { href: '/history', label: '历史回溯' },
    ],
  },
  {
    label: '研究',
    tabs: [
      { href: '/missed-signals', label: '漏检信号' },
      { href: '/targets', label: '未识别标的' },
      { href: '/crowd', label: '人群画像' },
      { href: '/analysis-process', label: '分析过程' },
      { href: '/causal-chains', label: '因果链' },
      { href: '/asset-graph', label: '资产图谱' },
      { href: '/counterfactual', label: '反事实' },
      { href: '/capabilities', label: '能力扩展' },
      { href: '/ask', label: '问库' },
      { href: '/memory', label: '记忆' },
    ],
  },
  {
    label: '后台',
    tabs: [
      { href: '/settings', label: '交易设置' },
      { href: '/sources', label: '信息源' },
      { href: '/scorecard', label: '成绩单' },
      { href: '/yield', label: '产能体检' },
      { href: '/connectivity', label: '联通' },
      { href: '/prompts', label: 'AI 提示词' },
    ],
  },
]

const ALL_TABS: Tab[] = [...PRIMARY_TABS, ...MORE_GROUPS.flatMap((g) => g.tabs)]

function isActive(pathname: string, href: string) {
  if (href === '/') return pathname === '/'
  return pathname === href || pathname.startsWith(`${href}/`)
}

function TabLink({ tab, pathname }: { tab: Tab; pathname: string }) {
  const active = isActive(pathname, tab.href)
  return (
    <Link
      href={tab.href}
      className={`shrink-0 border-b-2 px-2.5 py-2.5 text-[13px] transition-colors sm:px-3 ${
        active
          ? 'border-primary text-primary'
          : 'border-transparent text-muted-foreground hover:text-foreground'
      }`}
    >
      {tab.label}
    </Link>
  )
}

export function AppNav() {
  const pathname = usePathname() || '/'
  const activeMore = MORE_GROUPS.find((g) => g.tabs.some((t) => isActive(pathname, t.href))
  )
  const activeMoreTab = MORE_GROUPS.flatMap((g) => g.tabs).find((t) => isActive(pathname, t.href))
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  // 点外面 / 按 Esc 收起；路由变化也收起（避免下拉挂在别的页面上）
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current) return
      if (!wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  useEffect(() => setOpen(false), [pathname])

  return (
    // sticky top-[92px] = DashboardHeader h-16 (64px) + MarketHoursBar h-6 (24px)
    <nav className="sticky top-[92px] z-[5] border-b border-border bg-card" aria-label="主视图">
      <div ref={wrapRef} className="relative">
        <div className="thin-scroll mx-auto flex w-full max-w-[1600px] items-center gap-0.5 overflow-x-auto px-3 md:px-6">
          {PRIMARY_TABS.map((tab) => <TabLink key={tab.href} tab={tab} pathname={pathname} />)}

          <span aria-hidden className="mx-2 hidden h-5 w-px shrink-0 bg-border sm:block" />

          <div className="relative shrink-0">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              aria-haspopup="menu"
              className={`border-b-2 px-2.5 py-2.5 text-[13px] transition-colors sm:px-3 ${
                activeMoreTab
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {activeMoreTab ? activeMoreTab.label : '更多'}
              <span className="ml-1 text-[11px] text-muted-foreground/70">{open ? '▴' : '▾'}</span>
            </button>

            {open ? (
              <div
                role="menu"
                className="absolute left-0 top-full z-20 w-44 rounded-lg border border-border bg-card p-1.5 shadow-lg"
              >
                {MORE_GROUPS.map((group) => (
                  <div key={group.label} className="py-1">
                    <p className="px-2 pb-1 font-mono text-[11px] tracking-widest text-muted-foreground/60">
                      {group.label}
                    </p>
                    {group.tabs.map((tab) => (
                      <Link
                        key={tab.href}
                        href={tab.href}
                        role="menuitem"
                        className={`block rounded px-2 py-1.5 text-[13px] transition-colors ${
                          isActive(pathname, tab.href)
                            ? 'bg-primary/10 text-primary'
                            : 'text-muted-foreground hover:bg-secondary/60 hover:text-foreground'
                        }`}
                      >
                        {tab.label}
                      </Link>
                    ))}
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {activeMoreTab ? (
            <span className="ml-auto hidden shrink-0 pl-3 font-mono text-[11px] text-muted-foreground/60 xl:inline">
              {activeMore?.label || ''} / {activeMoreTab.label}
            </span>
          ) : null}
        </div>
      </div>
    </nav>
  )
}
