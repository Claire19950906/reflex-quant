// components/news/entity-impact-panel.tsx — v302 stub → 真实实现 (2026-10-18 改造)
//
// 原 HEAD 文件是 v302 stub（按 items 原序 slice topN 展示，无聚合），真实"按 entity 聚合"逻辑
// 从未存在过仓库里（无 git 历史）。本次改造按 stub 顶部"真实 EntityImpactPanel 行为"注释还原：
//  - 按 entity 聚合 items（关键词正则匹配 news_text）
//  - 取 topN entity
//  - 渲染 entity / total weight / sentiment / 头条预览
//
// 改动记录（2026-10-18）：
//  - stub slice 展示 → 真实按 entity 聚合 + 排序 + 截断
//  - 新增 8 个资产关键词表（BTC/ETH/SOL/XAU/WTI/AAPL/TSLA/NVDA），正则匹配 news_text
//  - 一条新闻若匹配多个 entity，计入多个（覆盖度优先）
//  - 未匹配 entity 的新闻丢弃（避免 OTHER 行污染）
//  - 聚合指标：count / totalWeight (×regime) / meanSentiment / 头条预览
//  - 不引入点击跳转（/events/:id 路由不存在，避免死链）
//  - header 新增 regime / vix 显示，方便排查权重调整是否生效
//
// 不动：
//  - EntityImpactPanelProps prop 签名（callers 不变）
//  - 外层 DOM 结构（<section> + <header> + <ul>/<li>）
//  - 字体规范 (font-mono text-[11px])
//  - 颜色 token（text-foreground/90, text-muted-foreground, border-border）
//  - emptyText prop 保留（callers 可自定义）
//  - app/attribution/page.tsx 调用方

'use client'

import { useMemo } from 'react'
import { cn } from '@/lib/utils'
import {
  deriveRegime,
  type MarketState,
  type NewsImpactItem,
} from '@/components/news/news-impact-weight'

export type EntityImpactPanelProps = {
  items: NewsImpactItem[]
  marketState: MarketState
  loading?: boolean
  topN?: number
  title?: string
  emptyText?: string
  className?: string
}

/** 8 个标的关键词表 — 与归因页 v291 BTC/ETH/SOL/XAU/WTI 对齐 + 3 个美股
 *  顺序即聚合默认排序权重前的 tie-breaker
 */
const ENTITY_KEYWORDS: ReadonlyArray<{ tag: string; re: RegExp }> = [
  { tag: 'BTC', re: /比特币|Bitcoin|BTC/i },
  { tag: 'ETH', re: /以太坊|Ethereum|ETH/i },
  { tag: 'SOL', re: /Solana|SOL/i },
  { tag: 'XAU', re: /黄金|Gold|XAU/i },
  { tag: 'WTI', re: /原油|WTI|Crude|oil/i },
  { tag: 'AAPL', re: /苹果|Apple|AAPL/i },
  { tag: 'TSLA', re: /特斯拉|Tesla|TSLA/i },
  { tag: 'NVDA', re: /英伟达|NVIDIA|NVDA/i },
]

/** 单个 entity 的聚合结果 */
type EntityRow = {
  tag: string
  count: number
  totalWeight: number
  meanSentiment: number
  topTitle: string
}

/** 从一条 news 提取所有命中 entity tag（去重，保留 ENTITY_KEYWORDS 顺序） */
function matchEntities(text: string): string[] {
  const tags: string[] = []
  for (const { tag, re } of ENTITY_KEYWORDS) {
    if (re.test(text)) tags.push(tag)
  }
  return tags
}

/** sentiment → 显示符号 */
function sentimentArrow(v: number): string {
  if (v > 0.2) return '↑'
  if (v < -0.2) return '↓'
  return '—'
}

/** sentiment → 文字颜色（参考 v291 涨跌色） */
function sentimentCls(v: number): string {
  if (v > 0.2) return 'text-long'
  if (v < -0.2) return 'text-short'
  return 'text-muted-foreground'
}

export function EntityImpactPanel({
  items,
  marketState,
  loading = false,
  topN = 5,
  title = 'Top Entity Impact',
  emptyText = '暂无 entity 影响数据',
  className,
}: EntityImpactPanelProps) {
  const regime = deriveRegime(marketState.vix)

  // 聚合
  const rows = useMemo<EntityRow[]>(() => {
    const acc = new Map<string, { count: number; totalWeight: number; sentimentSum: number; topTitle: string; topWeight: number }>()
    for (const it of items) {
      const text = String((it as { title?: unknown }).title ?? '')
      const tags = matchEntities(text)
      if (tags.length === 0) continue
      const w = typeof it.weight === 'number' ? it.weight * regime : 0
      const s = typeof it.sentiment === 'number' ? it.sentiment : 0
      for (const t of tags) {
        const cur = acc.get(t) ?? { count: 0, totalWeight: 0, sentimentSum: 0, topTitle: '', topWeight: -1 }
        cur.count += 1
        cur.totalWeight += w
        cur.sentimentSum += s
        if (w > cur.topWeight) {
          cur.topWeight = w
          cur.topTitle = String((it as { title?: unknown }).title ?? String((it as { id?: unknown }).id ?? ''))
        }
        acc.set(t, cur)
      }
    }
    const out: EntityRow[] = []
    acc.forEach((v, tag) => {
      out.push({
        tag,
        count: v.count,
        totalWeight: v.totalWeight,
        meanSentiment: v.count > 0 ? v.sentimentSum / v.count : 0,
        topTitle: v.topTitle,
      })
    })
    // 排序: totalWeight 降序, count 降序, ENTITY_KEYWORDS 顺序 tie-breaker
    const order = new Map(ENTITY_KEYWORDS.map((e, i) => [e.tag, i] as const))
    out.sort((a, b) => {
      if (b.totalWeight !== a.totalWeight) return b.totalWeight - a.totalWeight
      if (b.count !== a.count) return b.count - a.count
      return (order.get(a.tag) ?? 99) - (order.get(b.tag) ?? 99)
    })
    return out.slice(0, Math.max(0, topN))
  }, [items, regime, topN])

  return (
    <section
      aria-label={title}
      className={cn('rounded-md border border-border bg-card p-3', className)}
    >
      <header className="mb-2 flex items-center justify-between">
        <h2 className="font-mono text-xs uppercase tracking-wide text-muted-foreground">
          {title} · top {topN}
        </h2>
        <div className="flex items-center gap-2 font-mono text-[10px] text-muted-foreground/60">
          {loading && <span>loading…</span>}
          <span>regime {regime.toFixed(2)} · vix {marketState.vix}</span>
        </div>
      </header>
      {rows.length === 0 ? (
        <div
          role="status"
          className="rounded border border-dashed border-border bg-muted/20 px-3 py-6 text-center font-mono text-[11px] text-muted-foreground"
        >
          {emptyText}
        </div>
      ) : (
        <ul className="divide-y divide-border/40">
          {rows.map((r) => (
            <li
              key={r.tag}
              className="flex items-center justify-between gap-2 py-1.5 font-mono text-[11px]"
            >
              <div className="flex min-w-0 items-center gap-2">
                <span className="w-12 shrink-0 font-semibold text-foreground">
                  {r.tag}
                </span>
                <span className={cn('w-3 shrink-0 text-center font-semibold', sentimentCls(r.meanSentiment))}>
                  {sentimentArrow(r.meanSentiment)}
                </span>
                <span className="text-muted-foreground">
                  {r.count} 条
                </span>
                <span className="truncate text-foreground/80">
                  {r.topTitle}
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-2 text-muted-foreground">
                <span className={cn('font-semibold', sentimentCls(r.meanSentiment))}>
                  w={r.totalWeight.toFixed(2)}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export default EntityImpactPanel
