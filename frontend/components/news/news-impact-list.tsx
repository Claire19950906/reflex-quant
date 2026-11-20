// components/news/news-impact-list.tsx — v302 stub → 真实实现 (2026-10-18 改造)
//
// 原 HEAD 文件是 v302 stub（只显示空数据），真实"按 weight 排序 / 显示涨跌方向 / 应用 regime"逻辑
// 从未存在过仓库里（无 git 历史）。本次改造按 stub 顶部"真实 NewsImpactList 行为"注释还原：
//  - 按 weight 排序（降序）
//  - 渲染 action badge / title / weight / sentiment / source / 时间
//  - 数据来源: useNewsImpact (lib/hooks/use-news-impact.ts 真实实现)
//
// 改动记录（2026-10-18）：
//  - stub 列表 → 真实排序 + action badge + source + 时间
//  - 接入 deriveRegime(vix) 调整 weight 显示（regime ∈ [0.5, 1.5], vix=20 → 1.0）
//  - 限制最多 30 条（避免几百条新闻撑爆归因页）
//  - 不引入点击跳转（/events/:id 路由不存在，避免死链）
//  - header 新增 regime / vix 显示，方便排查权重调整是否生效
//
// 不动：
//  - NewsImpactListProps prop 签名（callers 不变）
//  - 外层 DOM 结构（<section> + <header> + <ul>/<li>）
//  - 字体规范 (font-mono text-[11px])
//  - 颜色 token（text-foreground/90, text-muted-foreground, border-border）
//  - emptyText prop 保留（callers 可自定义）
//  - app/attribution/page.tsx 调用方

'use client'

import { useMemo, type ReactNode } from 'react'
import { useElapsed } from '@/lib/hooks/use-elapsed'
import { cn } from '@/lib/utils'
import {
  deriveRegime,
  type MarketState,
  type NewsImpactItem,
} from '@/components/news/news-impact-weight'

export type NewsImpactListProps = {
  items: NewsImpactItem[]
  marketState: MarketState
  loading?: boolean
  title?: string
  emptyText?: string
  /** v337-UI: 空的时候告诉用户"为什么空 + 下一步做什么"，而不是只给一句"暂无数据" */
  emptyReason?: ReactNode
  className?: string
}

/** 列表上限 — 避免几百条新闻撑爆归因页 */
const MAX_ROWS = 30

/** action → 涨跌色 (参考 v291 event-market-attribution kindBadge 风格)
 *  BUY=绿, SELL=红, HOLD=灰, 其它=灰
 */
function actionBadgeCls(action: unknown): string {
  if (action === 'BUY') return 'border-long/40 bg-long/15 text-long'
  if (action === 'SELL') return 'border-short/40 bg-short/15 text-short'
  return 'border-muted-foreground/30 bg-muted/30 text-muted-foreground'
}

/** 时间格式: ISO 字符串或 unix ts → HH:MMZ */
function fmtTime(v: unknown): string {
  if (typeof v !== 'string' || !v) return ''
  // 接受 ISO 字符串（含 'T'/'Z'）或纯数字 unix 秒
  const d = /^\d+$/.test(v) ? new Date(Number(v) * 1000) : new Date(v)
  if (isNaN(d.getTime())) return ''
  // v337-UI: 原来输出 UTC 的 HH:MMZ，和页头/导航的本地时间混用，用户会算错"这条是几点发生的"。
  //   统一成本地时间（与浏览器时区一致），不再带 Z 后缀。
  return d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' })
}

/** 归一化 source: "ws:jin10" / "internal:..." → "jin10" / "..." */
function fmtSource(v: unknown): string {
  if (typeof v !== 'string' || !v) return ''
  return v.replace(/^(ws|internal|api|fred):/i, '').slice(0, 12)
}

export function NewsImpactList({
  items,
  marketState,
  loading = false,
  title = 'News Impact Weights',
  emptyText = '暂无新闻影响数据',
  emptyReason,
  className,
}: NewsImpactListProps) {
  // regime: 0.5–1.5, vix=20 → 1.0
  const regime = deriveRegime(marketState.vix)
  const elapsedMs = useElapsed(loading)
  const elapsedS = Math.floor(elapsedMs / 1000)

  // 排序 + regime 调整 + 截断
  const rows = useMemo(() => {
    return items
      .map((it) => ({
        ...it,
        adjWeight: typeof it.weight === 'number' ? it.weight * regime : null,
      }))
      .sort((a, b) => (b.adjWeight ?? -1) - (a.adjWeight ?? -1))
      .slice(0, MAX_ROWS)
  }, [items, regime])
  // 注: NewsImpactItem 的索引签名 [k: string]: unknown 在 spread 后 TS 推断会丢,
  // 下面访问 .action/.source 时用 `it as { ... }` 收窄类型.

  return (
    <section
      aria-label={title}
      className={cn('rounded-md border border-border bg-card p-3', className)}
    >
      <header className="mb-2 flex items-center justify-between">
        <h2 className="font-mono text-xs uppercase tracking-wide text-muted-foreground">
          {title}
        </h2>
        <div className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground/60">
          {loading && (
            <span>
              读取中…{elapsedS > 0 ? ` ${elapsedS}s` : ''}
              {elapsedS >= 6 ? '（接口慢，后端在算）' : ''}
            </span>
          )}
          <span>regime {regime.toFixed(2)} · vix {marketState.vix}</span>
        </div>
      </header>
      {rows.length === 0 ? (
        <div
          role="status"
          className="rounded border border-dashed border-border bg-muted/20 px-3 py-4 text-center font-mono text-[11px] text-muted-foreground"
        >
          <p>{emptyText}</p>
          {emptyReason ? (
            <div className="mx-auto mt-2 max-w-[760px] space-y-1 border-t border-border/50 pt-2 text-left text-[11px] leading-relaxed text-muted-foreground/90">
              {emptyReason}
            </div>
          ) : null}
        </div>
      ) : (
        <ul className="divide-y divide-border/40">
          {rows.map((it) => {
            const action = typeof (it as { action?: unknown }).action === 'string'
              ? (it as { action?: string }).action
              : ''
            const t = fmtTime(it.created_at)
            const src = fmtSource((it as { source?: unknown }).source)
            return (
              <li
                key={String(it.id)}
                className="flex items-center justify-between gap-2 py-1.5 font-mono text-[11px]"
              >
                <div className="flex min-w-0 items-center gap-2">
                  {action && (
                    <span
                      className={cn(
                        'rounded border px-1 text-[11px] font-semibold',
                        actionBadgeCls(action),
                      )}
                    >
                      {action}
                    </span>
                  )}
                  <span className="truncate text-foreground/90">
                    {it.title ?? it.id}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-2 text-muted-foreground">
                  {src && (
                    <span className="text-[11px] text-muted-foreground/60">
                      {src}
                    </span>
                  )}
                  {t && <span className="text-[11px]">{t}</span>}
                  {it.adjWeight !== null && (
                    <span className="font-semibold">w={it.adjWeight.toFixed(2)}</span>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export default NewsImpactList
