// app/news/page.tsx — 新闻流独立页 (2026-10-18 新增)
// 路由: /news
// 后端: bridge_server.py /api/news/recent + /api/news/pull-now (P0-3)

import { NewsFeedPanel } from '@/components/news/news-feed-panel'

export const dynamic = 'force-dynamic'

export default function NewsPage() {
  return (
    <main className="thin-scroll mx-auto w-full max-w-[1600px] flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mb-4">
        <h1 className="font-mono text-base font-semibold uppercase tracking-widest text-foreground">新闻流</h1>
        <p className="mt-1 font-mono text-[11px] text-muted-foreground">
          TechFlow + 东方财富 双源实时新闻 · 5 分钟自动拉取 · 24 小时窗口
        </p>
      </div>
      <NewsFeedPanel hours={24} limit={50} />
    </main>
  )
}
