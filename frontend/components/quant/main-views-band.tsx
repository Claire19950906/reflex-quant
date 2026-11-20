'use client'

/**
 * 首页「四块主视图」入口条 — 按 2.0 结构图 L2 数据带落地。
 * 只做展示与跳转，不拉新接口；四块分别对应归因 / 资金 / 因果 / 反思。
 */

import Link from 'next/link'

const VIEWS = [
  {
    href: '/attribution',
    no: '01',
    title: '归因 K 线',
    desc: '新闻落在 K 线上，看它何时、影响多久。',
    meta: '事件 · 来源 · 影响窗口',
  },
  {
    href: '/capital-flow',
    no: '02',
    title: '资金流向',
    desc: '钱在大类、板块、ETF 之间怎么挪。',
    meta: '大类 · 板块 · 持仓',
  },
  {
    href: '/knowledge-graph',
    no: '03',
    title: '因果结论',
    desc: '系统得出了什么，置信多少，证据在哪。',
    meta: '结论 · 置信 · 证据',
  },
  {
    href: '/review',
    no: '04',
    title: '反思健康',
    desc: '反思是否在跑，最近错在哪、修没修。',
    meta: '心跳 · 队列 · 错误',
  },
]

export function MainViewsBand() {
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 className="font-mono text-[12px] font-semibold uppercase tracking-wide text-foreground">
          四个主视图
        </h2>
        <span className="font-mono text-[11px] text-muted-foreground">点任一块进入</span>
      </div>
      <div className="grid grid-cols-2 divide-x divide-y divide-border lg:grid-cols-4 lg:divide-y-0">
        {VIEWS.map((v) => (
          <Link
            key={v.href}
            href={v.href}
            prefetch={false}
            className="group flex min-h-[104px] flex-col gap-1 px-3 py-3 transition-colors hover:bg-secondary/40"
          >
            <span className="font-mono text-[11px] tracking-widest text-muted-foreground/60">{v.no}</span>
            <span className="text-[15px] font-semibold text-foreground group-hover:text-primary">{v.title}</span>
            <span className="text-[12px] leading-relaxed text-muted-foreground">{v.desc}</span>
            <span className="mt-auto font-mono text-[11px] text-muted-foreground/70">{v.meta} →</span>
          </Link>
        ))}
      </div>
    </section>
  )
}
