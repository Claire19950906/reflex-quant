'use client'

import React, { useState } from 'react'
import { ChevronDown, ChevronRight, Info } from 'lucide-react'
import { SignalBadge } from './signal-badge'
import { formatPrice } from './price-ticker'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import { computeMFE, computePnL } from '@/lib/signal-utils'
import type { PriceMap } from '@/hooks/use-binance-prices'

type Props = {
  events: ApiEvent[]
  activeMarket: MarketCategory
  livePrices: PriceMap
}

const CATEGORY_LABEL: Record<string, string> = {
  CRYPTO: '加密货币',
  GOLD: '黄金',
  OIL: '原油',
  // v180 P0+：补 STOCK — 否则「美股/A股/个股」新闻归类为 OTHER 不可分类。详见统一口径文档 §5。
  STOCK: '股票',
  MACRO: '宏观',
  // v212 fix: 'OTHER' 已从 MarketCategory 删除。极少数"已通过筛选但无直接品种"事件归入 AGGREGATE。
  AGGREGATE: '聚合事件',
}

const TRACKED_ASSETS = new Set(['BTC', 'ETH', 'SOL', 'XAU', 'GOLD', 'WTI'])

function VerdictLabel({ verdict }: { verdict: string }) {
  const v = verdict?.toUpperCase()
  if (!v) return <span className="text-[10px] text-muted-foreground/40">—</span>
  if (v === 'WIN') return <span className="inline-flex items-center gap-0.5 text-[10px] font-medium text-long">✅ 预测正确</span>
  if (v === 'LOSS') return <span className="inline-flex items-center gap-0.5 text-[10px] font-medium text-short">❌ 预测错误</span>
  return <span className="text-[10px] text-muted-foreground/50">➖ 无实质影响</span>
}

function VerdictLabelSm({ verdict }: { verdict: string }) {
  const v = verdict?.toUpperCase()
  if (!v) return <span className="text-[9px] text-muted-foreground/40">—</span>
  if (v === 'WIN') return <span className="inline-flex items-center gap-0.5 text-[9px] font-medium text-long">✅ 预测正确</span>
  if (v === 'LOSS') return <span className="inline-flex items-center gap-0.5 text-[9px] font-medium text-short">❌ 预测错误</span>
  return <span className="text-[9px] text-muted-foreground/50">➖ 无实质影响</span>
}

function filterAndGroup(events: ApiEvent[], activeMarket: MarketCategory) {
  const filtered = events.filter((e) => {
    if (e.action === 'HOLD' || e.action === 'FAILED') return false
    if (activeMarket !== 'ALL' && e.market_category !== activeMarket) return false
    if (e.parent_id !== null && e.parent_id !== undefined) return false
    const asset = e.target_asset ?? ''
    if (e.market_category === 'GOLD' || e.market_category === 'OIL') return true
    if (!asset || asset === 'NONE' || !TRACKED_ASSETS.has(asset.toUpperCase())) return false
    return true
  })

  const childMap = new Map<number, ApiEvent[]>()
  for (const ev of events) {
    if (ev.parent_id === null || ev.parent_id === undefined) continue
    const list = childMap.get(ev.parent_id) || []
    list.push(ev)
    childMap.set(ev.parent_id, list)
  }

  return { parents: filtered, childMap }
}

export function SignalLedger({ events, activeMarket, livePrices }: Props) {
  const { parents, childMap } = filterAndGroup(events, activeMarket)
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set())

  const toggleExpand = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const label = CATEGORY_LABEL[activeMarket] ?? activeMarket
  const title = activeMarket === 'ALL' ? 'AI 活跃信号台账' : `AI 活跃信号台账 - ${label}`

  return (
    <section className="glass rounded-lg border border-border">
      <div className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      </div>
      {/* v170 Phase F：幽灵成交 banner — 解释"为什么首页会显示已结算虚拟持仓"
          mock_trades 是 paper trading 的快速回放样本，并非真实交易所成交。
          用户鼠标悬停查看完整 tooltip。 */}
      <div
        className="flex items-start gap-1.5 border-b border-border bg-amber-950/20 px-4 py-1.5 text-[10px] text-amber-200/90"
        title="本面板包含已结算的虚拟持仓（来源 mock_trades）。这些条目用于复盘真实持仓 (paper) 在不同价格路径下的模拟表现，并非真实交易所成交。每条 mock_trade 都通过 trade_uuid 与 ai_decisions / trade_reflections_v2 串联，便于在 /admin/id-reconcile 追溯对账状态。真正的实时未实现盈亏请参考「反思系统 → A 真实持仓战绩」。"
      >
        <Info className="mt-px h-3 w-3 flex-shrink-0" aria-hidden="true" />
        <span>
          本面板含已结算<strong className="font-semibold">虚拟持仓</strong>（源自 <code className="font-mono text-amber-300/90">mock_trades</code>）。
          用于复盘 AI 在不同价格路径下的模拟表现，并非真实交易所成交。
          每条均挂 <code className="font-mono text-amber-300/90">trade_uuid</code>，与 ai_decisions / trade_reflections_v2 串联；可在 /admin/id-reconcile 追溯对账。
          实时未实现盈亏请看「反思系统 → A 真实持仓战绩」。
        </span>
      </div>
      {/* v285 — 左右两栏对称：限高 80vh（约 35-45 行）+ 内部滚动；避免右侧 70+ 行把 grid 行撑高到 ~3000px */}
      <div className="max-h-[min(80vh,1100px)] overflow-auto">
        <div className="overflow-x-auto">
        {parents.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-muted-foreground">
            暂无 {label} 信号
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                <th className="w-8 px-1 py-2" />
                <th className="px-4 py-2 font-medium">交易对</th>
                <th className="px-4 py-2 font-medium">信号</th>
                <th className="px-4 py-2 text-right font-medium">入场价</th>
                <th className="px-4 py-2 text-right font-medium">当前价</th>
                <th className="px-4 py-2 text-right font-medium">最大浮盈</th>
                <th className="px-4 py-2 text-right font-medium">未实现盈亏</th>
                <th className="px-4 py-2 text-center font-medium">状态</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {parents.map((ev) => {
                const children = childMap.get(ev.id) || []
                const actualChildren = ev.child_count > 0 && children.length === 0
                  ? []
                  : children
                const hasChildren = ev.child_count > 0
                const isExpanded = expandedIds.has(ev.id)

                const isCommodity = ev.market_category === 'GOLD' || ev.market_category === 'OIL'
                  || ev.target_asset === 'XAU' || ev.target_asset === 'GOLD' || ev.target_asset === 'WTI'
                const asset = ev.target_asset && ev.target_asset !== 'NONE'
                  ? ev.target_asset
                  : ev.market_category === 'GOLD' ? 'XAU'
                  : ev.market_category === 'OIL' ? 'WTI'
                  : null
                const assetSuffix = isCommodity ? 'USD' : 'USDT'
                const pair = asset
                  ? `${asset}/${assetSuffix}`
                  : '—'
                const pairKey = asset ? `${asset}USDT` : ''

                // 只用后端真实入场价（entry_price）；未建仓的信号无入场价 → 显示 '—'
                const entry = ev.entry_price ?? undefined
                const currentInfo = pairKey ? livePrices[pairKey] : undefined
                const current = currentInfo?.price ?? 0

                const pnl = entry && current
                  ? computePnL(ev.action, entry, current)
                  : null

                return (
                  <React.Fragment key={ev.id}>
                    <tr className="transition-colors hover:bg-secondary/30">
                      <td className="px-1 py-2.5 text-center">
                        {hasChildren ? (
                          <button
                            onClick={() => toggleExpand(ev.id)}
                            className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:text-foreground transition-colors"
                            aria-label={isExpanded ? '折叠子事件' : '展开子事件'}
                          >
                            {isExpanded
                              ? <ChevronDown className="h-3.5 w-3.5" />
                              : <ChevronRight className="h-3.5 w-3.5" />
                            }
                          </button>
                        ) : (
                          <span className="inline-block w-5" />
                        )}
                      </td>
                      <td className="px-4 py-2.5 font-mono font-medium text-foreground">
                        {pair}
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-1.5">
                          <SignalBadge signal={ev.action} />
                          {hasChildren && (
                            <span className="rounded-full bg-primary/15 px-1.5 py-0 text-[10px] font-bold text-primary">
                              {ev.child_count}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-right font-mono tabular-nums text-muted-foreground">
                        {entry ? `$${formatPrice(entry, asset ?? undefined)}` : '—'}
                      </td>
                      <td className="px-4 py-2.5 text-right font-mono tabular-nums text-muted-foreground">
                        {current ? `$${formatPrice(current, asset ?? undefined)}` : '—'}
                      </td>
                      <td className="px-4 py-2.5 text-right font-mono tabular-nums text-long/90">
                        {(() => {
                          const mfeVal = computeMFE(ev.action, entry ?? null, ev.max_price, ev.min_price)
                          return mfeVal !== null ? `${mfeVal >= 0 ? '+' : ''}${Math.round(mfeVal * 100) / 100}%` : '—'
                        })()}
                      </td>
                      <td
                        className={`px-4 py-2.5 text-right font-mono font-semibold tabular-nums ${
                          pnl?.positive ? 'text-long' : 'text-short'
                        }`}
                      >
                        {pnl ? `${pnl.positive ? '+' : ''}${pnl.pct}%` : '—'}
                      </td>
                      <td className="px-2 py-2.5 text-center">
                        <VerdictLabel verdict={ev.is_correct} />
                      </td>
                    </tr>

                    {/* Child rows — rendered when expanded */}
                    {isExpanded && children.map((child) => {
                      const childEntry = child.entry_price ?? undefined
                      const childCurrentInfo = livePrices[`${child.target_asset}USDT`]
                      const childCurrent = childCurrentInfo?.price ?? 0
                      const childPnl = childEntry && childCurrent
                        ? computePnL(child.action, childEntry, childCurrent)
                        : null

                      return (
                        <tr
                          key={child.id}
                          className="bg-secondary/10 transition-colors hover:bg-secondary/30"
                        >
                          <td className="px-1 py-2 text-center">
                            <span className="text-[10px] text-muted-foreground/60 font-mono">
                              └
                            </span>
                          </td>
                          <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                            {child.reason || `#${child.id}`}
                          </td>
                          <td className="px-4 py-2">
                            <div className="flex items-center gap-1.5">
                              <SignalBadge signal={child.action} />
                              <span className="rounded bg-muted px-1 py-0 text-[9px] text-muted-foreground">
                                +1
                              </span>
                            </div>
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs tabular-nums text-muted-foreground">
                            {childEntry ? `$${formatPrice(childEntry, child.target_asset ?? undefined)}` : '—'}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs tabular-nums text-muted-foreground">
                            {childCurrent ? `$${formatPrice(childCurrent, child.target_asset ?? undefined)}` : '—'}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs tabular-nums text-long/90">
                            {(() => {
                              const childMfe = computeMFE(child.action, childEntry ?? null, child.max_price, child.min_price)
                              return childMfe !== null ? `${childMfe >= 0 ? '+' : ''}${Math.round(childMfe * 100) / 100}%` : '—'
                            })()}
                          </td>
                          <td
                            className={`px-4 py-2 text-right font-mono text-xs tabular-nums ${
                              childPnl?.positive ? 'text-long' : 'text-short'
                            }`}
                          >
                            {childPnl ? `${childPnl.positive ? '+' : ''}${childPnl.pct}%` : '—'}
                          </td>
                          <td className="px-2 py-2 text-center">
                            <VerdictLabelSm verdict={child.is_correct} />
                          </td>
                        </tr>
                      )
                    })}

                    {/* 多品种副品种行（同新闻其它品种的独立信号，有入场价=已建仓）— rendered when expanded */}
                    {isExpanded && (ev.sibling_decisions ?? []).filter((sd) => sd.entry_price).map((sd) => {
                      const sibAsset = (sd.target_asset || '').toUpperCase()
                      const sibEntry = sd.entry_price ?? undefined
                      const sibCurrentInfo = livePrices[`${sibAsset}USDT`]
                      const sibCurrent = sibCurrentInfo?.price ?? 0
                      const sibPnl = sibEntry && sibCurrent
                        ? computePnL(sd.action, sibEntry, sibCurrent)
                        : null
                      return (
                        <tr
                          key={`sib-${sd.decision_id ?? sd.target_asset}`}
                          className="bg-amber-500/5 transition-colors hover:bg-amber-500/10"
                        >
                          <td className="px-1 py-2 text-center">
                            <span className="text-[10px] text-amber-600/70 dark:text-amber-400/70 font-mono">
                              └
                            </span>
                          </td>
                          <td className="px-4 py-2 font-mono text-xs text-amber-700/90 dark:text-amber-300/90">
                            {sibAsset} · 副品种{sd.reason ? `｜${sd.reason}` : ''}
                          </td>
                          <td className="px-4 py-2">
                            <div className="flex items-center gap-1.5">
                              <SignalBadge signal={sd.action} />
                            </div>
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs tabular-nums text-muted-foreground">
                            {sibEntry ? `$${formatPrice(sibEntry, sibAsset)}` : '—'}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs tabular-nums text-muted-foreground">
                            {sibCurrent ? `$${formatPrice(sibCurrent, sibAsset)}` : '—'}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-xs tabular-nums text-long/90">
                            {(() => {
                              const sibMfe = computeMFE(sd.action, sibEntry ?? null, sd.max_price ?? null, sd.min_price ?? null)
                              return sibMfe !== null ? `${sibMfe >= 0 ? '+' : ''}${Math.round(sibMfe * 100) / 100}%` : '—'
                            })()}
                          </td>
                          <td
                            className={`px-4 py-2 text-right font-mono text-xs tabular-nums ${
                              sibPnl?.positive ? 'text-long' : 'text-short'
                            }`}
                          >
                            {sibPnl ? `${sibPnl.positive ? '+' : ''}${sibPnl.pct}%` : '—'}
                          </td>
                          <td className="px-2 py-2 text-center">
                            <VerdictLabelSm verdict={sd.is_correct ?? ''} />
                          </td>
                        </tr>
                      )
                    })}

                    {/* Show message when children exist but none in current batch */}
                    {isExpanded && ev.child_count > 0 && children.length === 0 && (
                      <tr className="bg-secondary/10">
                        <td />
                        <td colSpan={7} className="px-4 py-2 text-xs text-muted-foreground">
                          聚合了 {ev.child_count} 个子信号（数据未在当前批次加载）
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        )}
        </div>
      </div>
    </section>
  )
}

