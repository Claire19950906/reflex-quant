'use client'
import { friendlyError } from '@/lib/friendly-error'
import { sharedJson } from '@/lib/shared-json'

import { memo, useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { ChevronDown, ChevronRight, Download, Loader2, Radio, Wifi, WifiOff } from 'lucide-react'
import type { ApiEvent, MarketCategory } from '@/lib/quant-data'
import { periodToQuery, type PeriodRange } from '@/lib/data-channel-standards'
import { computeMFE, computeMAE, computeMFETime, computeMAETime, getImpactThreshold, calculateHeatScore } from '@/lib/signal-utils'
import { SignalBadge } from './signal-badge'
import { PeerReviewsBlock } from '@/components/replay/peer-reviews'
import { TradeDecisionNote } from '@/components/replay/trade-decision'
import { AnalysisSteps } from '@/components/replay/analysis-steps'
import { GatekeeperFilterStream } from './gatekeeper-filter-stream'
import { API_BASE } from '@/lib/api'

const SSE_URL = `${API_BASE}/events/stream`
const MAX_EVENTS = 10000
// 需求9：关闭渲染截断——事件流完整渲染（EventItem 已用 React.memo 做优化）；
// 共享给统计组件的数据仍截断到 1000 条（统计卡片无需遍历全量）。
const SHARE_LIMIT = 1000

// v346: 分类 invariant 告警去重/限频。未分类新闻批量出现时（上游 gate/AI 未分析），
// 旧实现每条都 console.warn 一个未序列化的 Object，一次刷上千条、淹没真正的报错。
// 改为：同一条事件(id)只告警一次；重复命中计入抑制数，满 50 条汇总一条；Set 超上限滚动清空。
const _INVARIANT_WARN_CAP = 500
const _INVARIANT_SUPPRESS_FLUSH = 50
const _invariantWarned = new Set<number>()
let _invariantSuppressed = 0

// v311: AI 文本字段里的空 JSON 占位 —— "{}" / "[]" / "null"。
// ai_decisions.decision_context 有 1970/6076 行是 "{}"(AI 判定"无上下文"时写空对象),
// 直接渲染就是屏幕上一串 "{}"。后端已归一, 这里再挡一层。
function meaningfulText(v?: string | null): string {
  const s = (v ?? '').trim()
  if (!s || s === '{}' || s === '[]' || s === 'null' || s === 'None') return ''
  return s
}

// ── v310: 实时推送(SSE) payload 归一化 + 互补合并 ──────────────────────
// 桥接层 /api/events/stream 每 5s 推「精简事件对象」: 有 id/source/target_asset/
// market_category/analysis_status, 但**没有 news_text**(正文)/score/reason/ai_time。
// 前端把它直接当完整 ApiEvent 用 → 列表顶部那批实时事件只剩标签、没有正文;
// 更糟的是旧实现按 news_id/id **整体覆盖**掉 /api/events 全量接口里同一条的完整记录,
// 于是这批事件的正文永久丢失(表现: 上面几条只有标签, 往下翻才有正文)。
// 修复: ① 字段别名归一(headline/body/content 兜底);
//       ② 合并时只让「真正带上值」的字段覆盖, 缺失字段由旧记录(全量接口)补齐。
const SSE_TEXT_KEYS = ['news_text', 'text', 'content', 'body', 'headline', 'title'] as const

function pickSseText(raw: Record<string, unknown>): string {
  for (const key of SSE_TEXT_KEYS) {
    const value = raw[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

/** 稀疏化: 只保留实时包里真正带上的字段(缺失的键干脆不出现), 便于"缺什么补什么"。 */
function normalizeSseEvent(raw: unknown): Record<string, unknown> {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const out: Record<string, unknown> = {}
  if (typeof r.id === 'number') out.id = r.id
  if (typeof r.news_id === 'number') out.news_id = r.news_id
  if (typeof r.analysis_status === 'string') out.analysis_status = r.analysis_status
  if (typeof r.timestamp === 'string') out.timestamp = r.timestamp
  else if (typeof r.ts === 'string') out.timestamp = r.ts
  if (typeof r.ai_time === 'string') out.ai_time = r.ai_time
  if (typeof r.source === 'string') out.source = r.source
  const text = pickSseText(r)
  if (text) out.news_text = text
  if (typeof r.action === 'string' && r.action.trim()) out.action = r.action.trim().toUpperCase()
  if (typeof r.score === 'number' && Number.isFinite(r.score)) out.score = r.score
  if (typeof r.reason === 'string' && r.reason.trim()) out.reason = r.reason
  if (typeof r.market_category === 'string') out.market_category = r.market_category
  if (typeof r.target_asset === 'string') out.target_asset = r.target_asset
  if (typeof r.decision_id === 'number') out.decision_id = r.decision_id
  if (typeof r.cluster_size === 'number') out.cluster_size = r.cluster_size
  if (typeof r.settled === 'number') out.settled = r.settled
  if (typeof r.is_correct === 'string') out.is_correct = r.is_correct
  return out
}

/** 稀疏包 → 完整 ApiEvent(缺的字段用中性值, 不伪造结论)。 */
function toApiEvent(p: Record<string, unknown>): ApiEvent {
  const timestamp = typeof p.timestamp === 'string' ? p.timestamp : ''
  return {
    id: typeof p.id === 'number' ? p.id : 0,
    news_id: typeof p.news_id === 'number' ? p.news_id : (typeof p.id === 'number' ? p.id : 0),
    decision_id: typeof p.decision_id === 'number' ? p.decision_id : null,
    analysis_status: (typeof p.analysis_status === 'string' ? p.analysis_status : 'PENDING') as ApiEvent['analysis_status'],
    timestamp,
    ai_time: typeof p.ai_time === 'string' ? p.ai_time : timestamp,
    source: typeof p.source === 'string' ? p.source : 'unknown',
    news_text: typeof p.news_text === 'string' ? p.news_text : '',
    action: (typeof p.action === 'string' ? p.action : 'HOLD') as ApiEvent['action'],
    score: typeof p.score === 'number' ? p.score : 0,
    reason: typeof p.reason === 'string' ? p.reason : '',
    market_category: (typeof p.market_category === 'string' ? p.market_category : 'AGGREGATE') as ApiEvent['market_category'],
    target_asset: typeof p.target_asset === 'string' ? p.target_asset : '',
    parent_id: null,
    child_count: 0,
    reasoning_path: '',
    vip_tag: '',
    entry_price: null,
    exit_price: null,
    max_price: null,
    min_price: null,
    max_price_time: 0,
    min_price_time: 0,
    entry_time: timestamp,
    is_correct: typeof p.is_correct === 'string' ? p.is_correct : '',
    settled: typeof p.settled === 'number' ? p.settled : 0,
    doubao_action: 'HOLD',
    doubao_reasoning: '',
    cluster_size: typeof p.cluster_size === 'number' ? p.cluster_size : 1,
  }
}

/** 互补合并: incoming 里「有值」的字段覆盖 base; 缺失/空串/非数值字段保留 base。 */
function mergeEvent(base: ApiEvent, incoming: Record<string, unknown>): ApiEvent {
  const merged: Record<string, unknown> = { ...base }
  for (const [key, value] of Object.entries(incoming)) {
    if (value === undefined || value === null) continue
    if (typeof value === 'string' && value.trim() === '') continue
    if (typeof value === 'number' && !Number.isFinite(value)) continue
    merged[key] = value
  }
  return merged as ApiEvent
}

/** 只补空缺: source 非空、而 base 为空/缺失的字段才写入; 绝不覆盖 base 已有值。 */
function fillMissing(base: ApiEvent, source: ApiEvent): ApiEvent {
  const merged: Record<string, unknown> = { ...base }
  let changed = false
  for (const [key, value] of Object.entries(source as unknown as Record<string, unknown>)) {
    if (value === undefined || value === null) continue
    if (typeof value === 'string' && value.trim() === '') continue
    if (typeof value === 'number' && !Number.isFinite(value)) continue
    const current = merged[key]
    const empty = current === undefined || current === null || (typeof current === 'string' && current.trim() === '')
    if (!empty) continue
    merged[key] = value
    changed = true
  }
  return changed ? (merged as ApiEvent) : base
}

const EVENT_CATEGORIES: { key: MarketCategory; label: string }[] = [
  { key: 'ALL', label: '全部' },
  { key: 'CRYPTO', label: '加密' },
  { key: 'STOCK', label: '股票' },
  { key: 'HYNIX', label: '海力士' },
  { key: 'GOLD', label: '黄金' },
  { key: 'OIL', label: '原油' },
  { key: 'MACRO', label: '宏观' },
  // v212 fix: 删除"其他"桶。如果出现"已通过筛选、无直接品种"的稀有情况，归类为'聚合'。
  // 设计依据：筛层早就过滤掉"无品类"新闻；跑到这里的是"有市场影响但跨品种"的合法事件。
  { key: 'AGGREGATE', label: '聚合' },
]

// 股票类品种清单（后端 paper_trading stocks 赛道 + v175 A股）—— target_asset 命中即归"股票"。
// 注意：SKHYNIX 已从中移除 → 改归 HYNIX 桶（用户反馈全盘看不到海力士分析）；
//      MU 同样归 HYNIX（Micron 中文圈被称"美国海力士"）。
// v2026.10.15: 扩 10 只大盘股（半导体/金融/消费/能源）— 让大盘股新闻有独立分类
const STOCK_ASSETS = new Set([
  'NVDA', 'TSLA', 'AAPL', 'MSTR', 'MSFT', 'AMZN', 'GOOGL',
  'COIN', 'PLTR', 'META', 'SMCI', 'MARA', 'RIOT',
  'AMD', 'NFLX', 'CRM', 'JPM', 'DIS', 'ORCL', 'BA', 'GS', 'INTC', 'V',
  'CN_STOCK',
])

// v2026.10.15: 加密品种清单 — 与 STOCK_ASSETS 分离, 防止 BTC/ETH/SOL 误归股票
// 8 个新加密品种（不含原有 BTC/ETH/SOL —— 它们仍在 eventCategory 的硬编码 regex 里）
const CRYPTO_ASSETS = new Set([
  'XRP', 'DOGE', 'BNB', 'AVAX', 'LINK', 'DOT', 'ADA', 'LTC',
])

// 海力士品种清单：target_asset 或文本命中即归 HYNIX
const HYNIX_ASSETS = new Set([
  'SKHYNIX',        // 韩国海力士 (000660.KS)
  'MU',             // 美国海力士（Micron，中文圈用语）
  'US_HYNIX',       // 美国海力士（user-facing 别名）
  'KR_HYNIX',       // 韩国海力士（user-facing 别名）
])

function eventCategory(event: ApiEvent): MarketCategory {
  // 海力士 cap（v??）：海力士主体必须最先识别，避免被"美股新闻源兜底 → STOCK"规则吞掉
  //   （也覆盖 yahoo_stock/finnhub 来源的 Hynix 新闻 — 让全盘能看到 Hynix 分析）。
  const asset = event.target_asset?.toUpperCase() ?? ''
  if (HYNIX_ASSETS.has(asset) || /海力士|000660\.?ks|sk[ _]?hynix|us_hynix|kr_hynix/i.test(asset)) return 'HYNIX'
  const text = event.news_text?.toLowerCase?.() ?? ''
  if (text && /海力士|sk[\s\u00A0]?hynix|000660\.?ks|sk[\s\u00A0]?海力士/.test(text)) return 'HYNIX'

  // 美股新闻源（yahoo_stock/finnhub）→ 一律归"美股"分类（即使 target 未定向到清单内股票）
  const source = (event.source || '').toLowerCase()
  if (source.includes('yahoo_stock') || source.includes('finnhub')) return 'STOCK'
  // 优先按"AI 实际分析对象"target_asset 定标签（标签与分析内容永远一致）；
  // target_asset 为空/NONE 才回退到 AI 的 market_category，再回退到文本关键词兜底。
  if (asset && asset !== 'NONE') {
    if (/XAU|GOLD|XAG|SILVER/.test(asset)) return 'GOLD'
    if (/WTI|CL|OIL|BRENT/.test(asset)) return 'OIL'
    if (STOCK_ASSETS.has(asset)) return 'STOCK'
    if (CRYPTO_ASSETS.has(asset) || /BTC|ETH|SOL/.test(asset)) return 'CRYPTO'
  }
  if (event.market_category && event.market_category !== 'AGGREGATE' && event.market_category !== 'ALL') {
    return event.market_category
  }
  if (/bitcoin|ethereum|crypto|binance|stablecoin/.test(text)) return 'CRYPTO'
  if (/gold|bullion/.test(text)) return 'GOLD'
  if (/crude|oil|opec|hormuz|energy price/.test(text)) return 'OIL'
  if (/central bank|inflation|treasury|bond|yield|interest rate|pmi|gdp|tariff/.test(text)) return 'MACRO'
  // v212 fix: 不再返回'OTHER'。如果走到这里，说明筛层与关键词都没匹配上——
  // 这是合法的"聚合"状态（罕见），用 'AGGREGATE' 标识；同时 console.warn 留痕，
  // 监控如果频繁出现，说明筛层或关键词规则有漏洞。
  if (typeof window !== 'undefined') {
    const eid = event.id
    const seen = eid != null && _invariantWarned.has(eid)
    if (!seen) {
      if (eid != null) {
        if (_invariantWarned.size >= _INVARIANT_WARN_CAP) _invariantWarned.clear()
        _invariantWarned.add(eid)
      }
      // eslint-disable-next-line no-console
      console.warn(
        '[invariant] news passed screening but has no ticker category:',
        `id=${eid} source=${event.source} target_asset=${event.target_asset || 'NONE'} market_category=${event.market_category || ''} snippet=${(event.news_text || '').slice(0, 60)}`,
      )
    } else {
      _invariantSuppressed += 1
      if (_invariantSuppressed >= _INVARIANT_SUPPRESS_FLUSH) {
        // eslint-disable-next-line no-console
        console.warn(`[invariant] 另有 ${_invariantSuppressed} 条同类未分类新闻已去重抑制（不再逐条刷屏）`)
        _invariantSuppressed = 0
      }
    }
  }
  return 'AGGREGATE'
}

// 按资产代码归品类（多品种标签用）：主品种与各副品种各自归属的品类
function categoryForAsset(asset?: string): MarketCategory | null {
  const a = (asset || '').toUpperCase()
  if (!a || a === 'NONE') return null
  if (/XAU|GOLD|XAG|SILVER/.test(a)) return 'GOLD'
  if (/WTI|CL|OIL|BRENT/.test(a)) return 'OIL'
  if (HYNIX_ASSETS.has(a) || /海力士|000660\.?ks|sk[ _]?hynix/i.test(a)) return 'HYNIX'
  if (STOCK_ASSETS.has(a)) return 'STOCK'
  if (CRYPTO_ASSETS.has(a) || /BTC|ETH|SOL/.test(a)) return 'CRYPTO'
  return null
}

// 新闻涉及的【全部品类】标签（第一层）：主品种 + sibling 副品种的品类，去重保序
function eventCategories(event: ApiEvent): MarketCategory[] {
  const source = (event.source || '').toLowerCase()
  if (source.includes('yahoo_stock') || source.includes('finnhub')) return ['STOCK']
  const out: MarketCategory[] = []
  const mainCat = categoryForAsset(event.target_asset)
  if (mainCat && !out.includes(mainCat)) out.push(mainCat)
  for (const sd of event.sibling_decisions ?? []) {
    const c = categoryForAsset(sd.target_asset) ?? (sd.market_category as MarketCategory | undefined)
    if (c && c !== 'AGGREGATE' && c !== 'ALL' && !out.includes(c)) out.push(c)
  }
  if (out.length > 0) return out
  // 无目标品种 → 回退单品类逻辑
  const fallback = eventCategory(event)
  return fallback === 'AGGREGATE' && (event.market_category && event.market_category !== 'AGGREGATE' && event.market_category !== 'ALL')
    ? [event.market_category as MarketCategory]
    : [fallback]
}

function shortTimestamp(value: string) {
  const match = value.match(/(\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)/)
  return match ? `${match[1]} ${match[2]}` : value
}

// 解析北京时间串 → ms（无时区串补 +08:00，不依赖浏览器本地时区）
function timeToMs(value?: string): number | null {
  if (!value) return null
  const v = (value || '').trim()
  const iso = /[zZ]|[+-]\d{2}:?\d{2}$/.test(v) ? v.replace(' ', 'T') : v.replace(' ', 'T') + '+08:00'
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

// after − before 的秒数差（after/before 为北京时间串）；解析失败返回 null。
function timeDeltaSeconds(after?: string, before?: string): number | null {
  const a = timeToMs(after)
  const b = timeToMs(before)
  if (a === null || b === null) return null
  return Math.max(0, Math.round((a - b) / 1000))
}

// 来源平台标签映射：把后端 source 字符串归一成简短平台名
function sourceLabel(source: string): string {
  const s = (source || '').toLowerCase()
  if (s.includes('feishu') || s.includes('飞书')) return '飞书转金十'
  if (s.includes('jin10') || s.includes('金十')) return '金十'
  if (s.includes('blockbeats') || s.includes('律动')) return '律动'
  if (s.includes('techflow') || s.includes('深潮')) return '深潮'
  if (s.includes('reuters') || s.includes('路透')) return 'Reuters 路透'
  if (s.includes('bls') || s.includes('劳工统计')) return 'BLS 劳工统计'
  if (s.includes('eastmoney') || s.includes('东财')) return '东财'
  if (s.includes('financialjuice') || s.startsWith('ws:')) return 'FinancialJuice'
  if (s.includes('tree') || s.includes('telegram')) return 'TreeNews'
  if (s.includes('fred') || s.includes('stlouisfed')) return 'FRED'
  if (s.includes('finnhub') || s.includes('yahoo_stock')) return 'Finnhub'
  if (s.includes('yahoo_finance')) return 'Yahoo Finance'
  if (s.includes('marketwatch')) return 'MarketWatch'
  if (s.includes('fxstreet')) return 'FXStreet'
  if (s.includes('cointelegraph')) return 'Cointelegraph'
  if (s.includes('oilprice')) return 'OilPrice'
  if (s.includes('sim_test') || s.includes('probe')) return '' // 测试数据不标注
  // 兜底：只剥离 [xxx] 或 xxx: 这类前缀，其余（含下划线的 source key）原样截断，避免误删成 _finance / 空
  const cleaned = (source || '').replace(/^\[[^\]]*\]\s*/, '').replace(/^[a-z]+:\s*/i, '').trim()
  return (cleaned || source || '').slice(0, 12)
}

type Props = {
  activeMarket?: MarketCategory
  onMarketChange?: (market: MarketCategory) => void
  onEventsUpdated?: (events: ApiEvent[]) => void
  onPriceUpdate?: (asset: string, price: number, change24h?: number) => void
  // v188 P0+：「今日信号」卡片点击 → true 表示只看今天的 BUY/SELL 信号（DONE + 非 HOLD/FAILED）
  todayOnly?: boolean
  // v362: 全局时段；传入后事件流按窗口重取（hours / from_ts / to_ts）
  period?: PeriodRange
}

function VipBadge({ tag }: { tag: string }) {
  const label = tag.replace('[', '').replace(']', '')
  let color = 'border-hold/50 bg-hold/10 text-hold'
  if (tag.includes('FED')) color = 'border-primary/50 bg-primary/10 text-primary'
  if (tag.includes('MUSK')) color = 'border-primary/40 bg-primary/8 text-primary'
  return (
    <span className={'inline-flex items-center rounded border px-1.5 py-0 text-[11px] font-bold uppercase tracking-wider ' + color}>
      {label}
    </span>
  )
}

function VerdictBadge({ verdict }: { verdict: string }) {
  if (!verdict) return null
  let color = 'border-muted-foreground/30 bg-muted/20 text-muted-foreground'
  if (verdict === 'WIN') color = 'border-long/50 bg-long/10 text-long'
  if (verdict === 'LOSS') color = 'border-short/50 bg-short/10 text-short'
  return (
    <span className={'inline-flex items-center rounded border px-1.5 py-0 text-[11px] font-bold uppercase tracking-wider ' + color}>
      {verdict === 'WIN' ? 'CORRECT' : verdict === 'LOSS' ? 'WRONG' : verdict}
    </span>
  )
}

// 持仓跟踪子块：2h forward test 中【一个已建仓品种】的 entry/max/exit/min + 浮盈浮亏指标。
// 主品种与副品种共用同样式；主行有分级止盈/时间戳扩展字段，副品种（API 精简字段）不传即可。
type ForwardTrackBlockProps = {
  assetLabel?: string | null
  tag?: string | null
  action?: string | null
  entry: number | null
  max: number | null
  min: number | null
  exit: number | null
  settled: number
  isCorrect?: string | null
  // 强影响阈值用品种（主行用 target_asset 精确判断；副品种 fallback assetLabel）
  thresholdAsset?: string | null
  // 主行扩展（副品种 API 无这些字段）
  partialPrice?: number | null
  partialRatio?: number | null
  partialPnlPct?: number | null
  entryTime?: string
  maxPriceTime?: number
  minPriceTime?: number
}

function ForwardTrackBlock({
  assetLabel, tag, action, entry, max, min, exit, settled, isCorrect, thresholdAsset,
  partialPrice, partialRatio, partialPnlPct, entryTime, maxPriceTime, minPriceTime,
}: ForwardTrackBlockProps) {
  const e = entry ?? 0
  const mfe = e > 0 ? computeMFE(action ?? '', e, max ?? 0, min ?? 0) : null
  const mae = e > 0 ? computeMAE(action ?? '', e, max ?? 0, min ?? 0) : null
  const mfeTime = entryTime ? computeMFETime(action ?? '', entryTime, maxPriceTime ?? 0, minPriceTime ?? 0) : null
  const maeTime = entryTime ? computeMAETime(action ?? '', entryTime, maxPriceTime ?? 0, minPriceTime ?? 0) : null
  const threshold = getImpactThreshold(thresholdAsset ?? assetLabel ?? '')
  const isHighImpact = mfe !== null && mfe > threshold
  const showPartial = partialPrice != null && partialPrice > 0
  return (
    <div className="rounded border border-border/50 bg-secondary/30 px-2.5 py-2">
      {(assetLabel || tag) && (
        <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
          {assetLabel && (
            <span className="font-mono text-[11px] font-bold tracking-wide text-foreground/90">{assetLabel.toUpperCase()}</span>
          )}
          {tag && (
            <span className="rounded bg-secondary px-1.5 py-px font-mono text-[11px] font-bold uppercase tracking-wider text-muted-foreground">{tag}</span>
          )}
          {action && (
            <span className={'font-mono text-[11px] font-bold tracking-wider ' + (action === 'BUY' ? 'text-long' : action === 'SELL' ? 'text-short' : 'text-muted-foreground')}>
              {action}
            </span>
          )}
        </div>
      )}
      <div className="grid grid-cols-4 gap-x-3 gap-y-1 font-mono text-[11px]">
        <span className="text-muted-foreground/60">Entry</span>
        <span className="text-right tabular-nums text-foreground/90">{entry && entry > 0 ? `$${entry.toFixed(2)}` : '—'}</span>
        <span className="text-muted-foreground/60">Max</span>
        <span className="text-right tabular-nums text-long/90">{max && max > 0 ? `$${max.toFixed(2)}` : '—'}</span>
        <span className="text-muted-foreground/60">Exit</span>
        <span className="text-right tabular-nums text-foreground/90">
          {settled === 1 ? (exit && exit > 0 ? `$${exit.toFixed(2)}` : '—') : 'pending'}
        </span>
        <span className="text-muted-foreground/60">Min</span>
        <span className="text-right tabular-nums text-short/90">{min && min > 0 ? `$${min.toFixed(2)}` : '—'}</span>
      </div>
      {showPartial && (
        <div className="mt-1.5 inline-flex items-center gap-2 rounded border border-hold/40 bg-hold/10 px-2 py-0.5 font-mono text-[11px]">
          <span className="font-bold tracking-wider text-hold">分级止盈</span>
          <span className="text-muted-foreground">
            已平 <b className="text-foreground">{(partialRatio ?? 0) * 100}%</b> @{' '}
            <b className="text-foreground">${partialPrice!.toFixed(2)}</b>
          </span>
          <span className={partialPnlPct != null && partialPnlPct >= 0 ? 'text-long' : 'text-short'}>
            该部分 {(partialPnlPct ?? 0).toFixed(2)}%
          </span>
          <span className="text-muted-foreground/60">
            剩余 {(1 - (partialRatio ?? 0)) * 100}% 最终 @{exit && exit > 0 ? `$${exit.toFixed(2)}` : '—'}
          </span>
        </div>
      )}
      {(mfe !== null || mae !== null) && (
        <div className="mt-1.5 flex items-center gap-4 font-mono text-[11px]">
          {mfe !== null && (
            <span>
              <span className="text-muted-foreground/60">最大浮盈 </span>
              <span className="tabular-nums text-long/90">{mfe >= 0 ? '+' : ''}{mfe.toFixed(2)}%</span>
              {mfeTime && <span className="tabular-nums text-muted-foreground/50"> ({mfeTime}m)</span>}
            </span>
          )}
          {mae !== null && (
            <span>
              <span className="text-muted-foreground/60">最大浮亏 </span>
              <span className="tabular-nums text-short/90">{mae >= 0 ? '+' : ''}{mae.toFixed(2)}%</span>
              {maeTime && <span className="tabular-nums text-muted-foreground/50"> ({maeTime}m)</span>}
            </span>
          )}
        </div>
      )}
      {isHighImpact && (
        <div className="mt-1.5 inline-flex items-center gap-1.5 rounded border border-hold/50 bg-hold/10 px-2 py-0.5">
          <span className="text-[11px]">🔥</span>
          <span className="font-mono text-[11px] font-bold tracking-wider text-hold">强影响</span>
        </div>
      )}
      {settled === 1 && isCorrect && (
        <div className="mt-2 flex items-center gap-2">
          <span className="text-[11px] text-muted-foreground/60">Verdict</span>
          <VerdictBadge verdict={isCorrect} />
        </div>
      )}
    </div>
  )
}

function AnalysisStatusBadge({ status }: { status: ApiEvent['analysis_status'] }) {
  const labels = {
    PENDING: '待 AI 分析',
    PROCESSING: 'AI 分析中',
    FAILED: 'AI 分析失败',
    // v317: 噪声新闻（门卫筛掉，引擎永不处理）—— 不是"待 AI 分析"，
    // 否则会显示一个永远不会被处理的徽章，误导用户等一个不会来的分析。
    SKIPPED: '噪声已跳过',
    // v321: 门卫(LLM)通道故障时入库，等 replay_deferred_gate 回放补判。
    // 同样不是"待 AI 分析"—— 主分析不会认领它，得等门卫回放成功。
    GATE_PENDING: '门卫待回放',
    // v322: 超龄/僵尸行终态 —— 系统主动决定不再分析（已不可交易）。
    // 同样不是"待 AI 分析"—— 等它也不会有结果，只会让人以为队列在动。
    FILTERED: '已归档',
  } as const
  if (status === 'DONE') return null
  const color = status === 'FAILED'
    ? 'border-short/40 bg-short/10 text-short'
    : status === 'SKIPPED' || status === 'GATE_PENDING' || status === 'FILTERED'
    ? 'border-muted-foreground/30 bg-muted/20 text-muted-foreground'
    : 'border-hold/40 bg-hold/10 text-hold'
  return (
    <span className={'inline-flex items-center rounded border px-1.5 py-0 text-[11px] font-bold tracking-wider ' + color}>
      {labels[status] ?? '待 AI 分析'}
    </span>
  )
}

function HeatBadge({ size }: { size: number }) {
  if (size < 2) return null
  const score = calculateHeatScore(size)
  let colors: string
  if (score > 80) {
    colors = 'border-short/60 bg-short/15 text-short'
  } else if (score > 50) {
    colors = 'border-hold/50 bg-hold/10 text-hold'
  } else {
    colors = 'border-muted-foreground/30 bg-muted/20 text-muted-foreground'
  }
  return (
    <span className={'inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0 text-[11px] font-bold tracking-wider ' + colors}>
      🔥 热度 {score} (关联 {size} 源)
    </span>
  )
}

function DoubaoBadge({ action, reason }: { action: string; reason: string }) {
  const disagree = action && action !== 'HOLD'
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/8 px-1.5 py-0 text-[11px] font-bold uppercase tracking-wider text-primary"
      title={reason?.slice(0, 200) || 'Doubao secondary verification'}
    >
      DB {action}
      {disagree && <span className="inline-block h-1 w-1 rounded-full bg-hold" title="Model disagreement" />}
    </span>
  )
}

// 影响类型标注：已分析新闻按 AI 的 expected_horizon（intraday=即时 / 1-3d、1w+=中长期）；
// 未分析时按 FRED 事件标题前缀兜底（[FRED发布]=即时 / [FRED趋势]=中长期）。
function ImpactBadge({ analyzed, horizon, text }: { analyzed: boolean; horizon?: string; text: string }) {
  const h = (horizon || '').toLowerCase()
  const t = (text || '').toLowerCase()
  let kind: 'intraday' | 'medium' | null = null
  if (analyzed && h) {
    if (h === 'intraday') kind = 'intraday'
    else if (h === '1-3d' || h === '1w+') kind = 'medium'
  } else {
    if (t.includes('[fred发布]')) kind = 'intraday'
    else if (t.includes('[fred趋势]')) kind = 'medium'
  }
  if (!kind) return null
  return kind === 'intraday'
    ? (
      <span className="shrink-0 rounded border border-short/40 bg-short/10 px-1.5 py-0.5 font-mono text-[11px] text-short" title="影响集中在事件当下，短周期交易">
        即时影响
      </span>
    )
    : (
      <span className="shrink-0 rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary" title="对行情有中长期影响，可考虑趋势/波段周期">
        中长期影响
      </span>
    )
}

// 影响评估徽章：行内只显示紧凑的 关注度（避免挤掉右侧 BUY/SELL/HOLD 信号标签），
// 市场影响力/直接针对 等完整信息放悬停 tooltip。
function ImpactDetailBadge({ item }: { item: ApiEvent }) {
  const heat = typeof item.heat_score === 'number' ? item.heat_score : null
  const impact = typeof item.impact_score === 'number' ? item.impact_score : null
  const assetImpact = typeof item.asset_impact === 'number' ? item.asset_impact : null
  if (heat == null && impact == null && assetImpact == null) return null

  // 行内显示：图标 + 关注度分数（分档图标：8-10 🔥 / 6-7 👀 / 5 📌）
  let display = ''
  if (heat != null && heat >= 5) {
    const icon = heat >= 8 ? '🔥' : heat >= 6 ? '👀' : '📌'
    display = `${icon} 关注度 ${Math.round(heat)}`
  }
  if (!display) return null

  // tooltip 完整信息（悬停可见）
  const tooltipParts: string[] = ['AI 评估（非真实舆情）', `关注度 ${heat ?? '—'}/10`]
  if (impact != null) {
    const label = impact >= 9 ? '剧烈冲击' : impact >= 7 ? '强冲击' : impact >= 4 ? '中等冲击' : '弱冲击'
    tooltipParts.push(`市场影响力 ${Math.round(impact)}/10（${label}）`)
  }
  const direct = item.direct_catalyst === 1 || item.direct_catalyst === true
  if (assetImpact != null && assetImpact >= 7 && item.target_asset && item.target_asset !== 'NONE') {
    tooltipParts.push(`直接针对${item.target_asset}（影响 ${Math.round(assetImpact)}/10）`)
  } else if (direct && item.target_asset && item.target_asset !== 'NONE') {
    tooltipParts.push(`直接针对${item.target_asset}`)
  }

  const hot = heat != null && heat >= 7
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[11px] tracking-wider ${
        hot ? 'border-short/50 bg-short/10 text-short' : 'border-primary/40 bg-primary/8 text-primary'
      }`}
      title={tooltipParts.join(' · ')}
    >
      {display}
    </span>
  )
}

export function EventBus({ activeMarket = 'ALL', onMarketChange, onEventsUpdated, onPriceUpdate, todayOnly = false, period }: Props) {
  const [events, setEvents] = useState<ApiEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  // v362: 首屏拉取完成标志 —— 未完成前显示载入中，避免把"还在加载"误显示成空态。
  const [loaded, setLoaded] = useState(false)
  const [connected, setConnected] = useState(false)
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set())
  const [libraryCount, setLibraryCount] = useState<number | null>(null)
  // P3-UI: 默认收起限高（showAll=false），首屏只露前 20 条摘要，把结论区留给 KPI/持仓；
  //     点右上「全部」才展开全量。compact 默认开，单条更紧凑。
  const [showAll, setShowAll] = useState(false)
  const [compact, setCompact] = useState(true)

  const esRef = useRef<EventSource | null>(null)
  const mountedRef = useRef(true)
  const onEventsRef = useRef(onEventsUpdated)
  onEventsRef.current = onEventsUpdated
  const onPriceRef = useRef(onPriceUpdate)
  onPriceRef.current = onPriceUpdate
  // v362: 时段引用供回调读取，避免时段变化重建 SSE 连接；首跑跳过一次自动重取。
  const periodRef = useRef<PeriodRange | undefined>(period)
  periodRef.current = period
  const firstPeriodRunRef = useRef(true)

  // ── v310: 正文补全 + 历史重放防线 ───────────────────────────────────
  // ① 后端 SSE 曾从 id=0 开始推 → 新客户端一连上收到的就是全表最老的行(那些行 headline/body
  //    多为空), 前端又 unshift 到列表顶部 → 列表顶部全是"只有标签、没有正文"的垃圾,
  //    真正带正文的新事件反而沉到下面。newestIdRef 记录已知最新 id, 推送 id 明显更小
  //    → 判定为历史重放, 直接丢弃(前端不再依赖后端已重启)。
  // ② 实时推送不带 news_text → 定期从全量接口把缺失的正文补回来(只补空缺)。
  //    restCoveredIdsRef 记录「全量接口已确认存在」的 id → 这些 id 到最后仍没有正文,
  //    说明服务端确实没有正文(而不是还没拉到), UI 才能给"此事件暂无正文"的实话。
  const eventsRef = useRef<ApiEvent[]>([])
  const newestIdRef = useRef(0)
  const restCoveredIdsRef = useRef<Set<number>>(new Set())
  const hydrateBusyRef = useRef(false)
  const REPLAY_TOLERANCE = 200

  // SSE 批量节流：AI 分析时推送高频，合并到 300ms 一次 setState，避免频繁重渲染
  const pendingEventsRef = useRef<ApiEvent[]>([])
  const flushTimerRef = useRef<number | null>(null)

  // P1-B: 历史列表首轮失败退避重试。首轮撞上 bridge 卡顿/反代 hang up 时,
  //   旧逻辑只在挂载和时段切换拉一次历史, 失败后只剩 SSE 增量 → 列表永久空、
  //   必须手动刷新。这里按 20s/40s/60s 退避重拉, 成功即清零; 卸载时清理定时器。
  const historyRetryTimerRef = useRef<number | null>(null)
  const historyRetryCountRef = useRef(0)
  const HISTORY_RETRY_DELAYS = [20_000, 40_000, 60_000]
  const HISTORY_RETRY_MAX = HISTORY_RETRY_DELAYS.length

  const flushEvents = useCallback(() => {
    flushTimerRef.current = null
    const batch = pendingEventsRef.current
    pendingEventsRef.current = []
    if (batch.length === 0) return
    setEvents((prev) => {
      let next = prev
      for (const raw of batch) {
        const partial = normalizeSseEvent(raw)
        if (partial.id === undefined && partial.news_id === undefined) continue
        // v310: 丢弃历史重放事件(见上面 REPLAY_TOLERANCE 注释)
        if (typeof partial.id === 'number' && partial.id < newestIdRef.current - REPLAY_TOLERANCE) continue
        const existingIndex = next.findIndex(
          (event) =>
            (typeof partial.id === 'number' && event.id === partial.id) ||
            (typeof partial.news_id === 'number' && event.news_id === partial.news_id),
        )
        if (existingIndex >= 0) {
          next = [...next]
          // v310: 实时推送是「精简对象」(没有正文), 旧实现整体覆盖 →
          //       全量接口里同一条的正文被冲掉 → 列表顶部事件只剩标签、没有正文。
          //       现在只让实时包「真正带上值」的字段覆盖, 其余保留旧记录的完整值。
          next[existingIndex] = mergeEvent(next[existingIndex], partial)
        } else {
          next = [toApiEvent(partial), ...next]
          if (next.length > MAX_EVENTS) next.length = MAX_EVENTS
        }
      }
      return next
    })
  }, [])

  useEffect(() => {
    eventsRef.current = events
    let maxId = 0
    for (const event of events) {
      if (event.id > maxId) maxId = event.id
    }
    if (maxId > newestIdRef.current) newestIdRef.current = maxId
  }, [events])

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (hydrateBusyRef.current) return
      const current = eventsRef.current
      // 找到「最老的缺正文事件」在列表里的名次 → 按名次决定要拉多少条(避免每次拉 1000 条)
      const sorted = [...current].sort((a, b) => b.id - a.id)
      let need = 0
      for (let i = 0; i < sorted.length; i++) {
        if (!sorted[i].news_text) need = i + 1
      }
      if (need === 0) return
      const limit = Math.min(1000, Math.max(120, need + 30))
      hydrateBusyRef.current = true
      void (async () => {
        try {
          const res = await fetch(`${API_BASE}/events?limit=${limit}&flat=1&_v=v310-hydrate`, { cache: 'no-store' })
          if (!res.ok) return
          const raw: unknown = await res.json()
          const list: ApiEvent[] = Array.isArray(raw)
            ? (raw as ApiEvent[])
            : Array.isArray((raw as { items?: unknown })?.items)
              ? ((raw as { items: ApiEvent[] }).items)
              : []
          if (!mountedRef.current || list.length === 0) return
          // 记录「全量接口已确认存在」的 id → UI 据此区分"还没拉到" 与 "服务端确实没有正文"
          for (const item of list) restCoveredIdsRef.current.add(item.id)
          const byId = new Map<number, ApiEvent>()
          const byNewsId = new Map<number, ApiEvent>()
          for (const item of list) {
            byId.set(item.id, item)
            if (typeof item.news_id === 'number') byNewsId.set(item.news_id, item)
          }
          setEvents((prev) => {
            let changed = false
            const next = prev.map((event) => {
              const found = byId.get(event.id) ?? (typeof event.news_id === 'number' ? byNewsId.get(event.news_id) : undefined)
              if (!found) return event
              const merged = fillMissing(event, found)
              if (merged !== event) changed = true
              return merged
            })
            return changed ? next : prev
          })
        } catch {
          // 忽略: 下一轮继续
        } finally {
          hydrateBusyRef.current = false
        }
      })()
    }, 10_000)
    return () => window.clearInterval(timer)
  }, [])

  const handleSseMessage = useCallback((ev: MessageEvent) => {
    if (!mountedRef.current) return
    try {
      const parsed = JSON.parse(ev.data)
      if (parsed.type === 'price_update' && parsed.asset) {
        onPriceRef.current?.(parsed.asset, parsed.price, typeof parsed.change24h === 'number' ? parsed.change24h : undefined)
        return
      }
      if (typeof parsed.id === 'number') {
        pendingEventsRef.current.push(parsed as ApiEvent)
        if (flushTimerRef.current == null) {
          flushTimerRef.current = window.setTimeout(flushEvents, 300)
        }
      }
    } catch {
      // ignore
    }
  }, [flushEvents])

  const fetchHistory = useCallback(async (opts?: { reset?: boolean }) => {
    const qs = periodRef.current ? periodToQuery(periodRef.current).toString() : ''
    if (opts?.reset) {
      if (mountedRef.current) { setEvents([]); setLoaded(false); setError(null) }
      historyRetryCountRef.current = 0
      if (historyRetryTimerRef.current) { window.clearTimeout(historyRetryTimerRef.current); historyRetryTimerRef.current = null }
    }
    // v293 强标识: 让 DevTools console 可见, 用户能确认 chunk 已更新
    if (typeof window !== 'undefined') console.log('[v293-fixed] event-bus loadHistory() with ?flat=1')
    try {
      // v293: 用 ?flat=1 让后端直接返裸 ApiEvent[] 数组；v362 带上时段窗口参数
      const res = await fetch(`${API_BASE}/events?limit=${SHARE_LIMIT}&flat=1&_v=v293${qs ? `&${qs}` : ''}`, { cache: 'no-store' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      // 三重防御: 即使服务端误返 {ok,items,count} 也不崩
      const raw: unknown = await res.json()
      const items: ApiEvent[] = Array.isArray(raw)
        ? (raw as ApiEvent[])
        : Array.isArray((raw as { items?: unknown })?.items)
          ? ((raw as { items: ApiEvent[] }).items)
          : Array.isArray((raw as { data?: unknown })?.data)
            ? ((raw as { data: ApiEvent[] }).data)
            : []
      if (!mountedRef.current) return
      // 记录「全量接口已确认存在」的 id → 这些 id 若仍无正文, 说明服务端确实没有正文
      for (const item of items) restCoveredIdsRef.current.add(item.id)
      setEvents((current) => {
        // 四重防御: items 万一是 undefined 也不崩
        const safe = Array.isArray(items) ? items : []
        // v310: 不再「按 news_id 丢弃全量记录」—— 实时推送(精简对象)已经占位时,
        // 直接丢掉全量记录会让最新一批事件永久只剩标签、没有正文。
        // 改成按 id/news_id 合并: 全量记录的正文补进实时记录(fillMissing 只补空缺)。
        // v362: 时段切换重取时以空列表为底，避免上一窗口的旧事件残留。
        const base = opts?.reset ? [] : current
        const indexByKey = new Map<string, number>()
        base.forEach((event, index) => {
          indexByKey.set(`id:${event.id}`, index)
          indexByKey.set(`nid:${event.news_id}`, index)
        })
        const merged = [...base]
        for (const item of safe) {
          const keyId = `id:${item.id}`
          const keyNid = `nid:${item.news_id}`
          const existingIndex = indexByKey.get(keyId) ?? indexByKey.get(keyNid)
          if (existingIndex === undefined) {
            merged.push(item)
            indexByKey.set(keyId, merged.length - 1)
            indexByKey.set(keyNid, merged.length - 1)
          } else {
            merged[existingIndex] = fillMissing(merged[existingIndex], item)
          }
        }
        return merged
          .sort((a, b) => b.id - a.id)
          .slice(0, MAX_EVENTS)
      })
      setError(null)
      setLoaded(true)
      // 历史列表已成功拉取 → 清掉未完成的退避重试计数与定时器
      historyRetryCountRef.current = 0
      if (historyRetryTimerRef.current) { window.clearTimeout(historyRetryTimerRef.current); historyRetryTimerRef.current = null }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : 'fetch failed')
        setLoaded(true)
      }
      // P1-B: 失败后按退避序列重拉历史, 不把空态一直留到手动刷新
      if (historyRetryCountRef.current < HISTORY_RETRY_MAX) {
        const delay = HISTORY_RETRY_DELAYS[historyRetryCountRef.current]
        historyRetryCountRef.current += 1
        if (historyRetryTimerRef.current) window.clearTimeout(historyRetryTimerRef.current)
        historyRetryTimerRef.current = window.setTimeout(() => {
          historyRetryTimerRef.current = null
          if (mountedRef.current) void fetchHistory()
        }, delay)
      }
    }
  }, [])

  const loadCounts = useCallback(async () => {
    const qs = periodRef.current ? periodToQuery(periodRef.current).toString() : ''
    try {
      // v499: 与 StatCards / 信号计数同参数轮询同一接口 → sharedJson 合并
      const data: unknown = await sharedJson<unknown>(
        `${API_BASE}/events/today${qs ? `?${qs}` : ''}`, 4000, { cache: 'no-store' })
      const obj = (data && typeof data === 'object') ? (data as Record<string, unknown>) : {}
      if (!mountedRef.current) return
      const total = Number(obj.total_count ?? obj.news_count ?? 0)
      if (Number.isFinite(total) && total > 0) setLibraryCount(total)
    } catch {
      // 计数接口失败时回退列表长度
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void fetchHistory()
    void loadCounts()
    const countTimer = setInterval(() => void loadCounts(), 15_000)
    const es = new EventSource(SSE_URL)
    esRef.current = es
    es.onopen = () => { if (mountedRef.current) { setConnected(true); setError(null) } }
    es.onmessage = handleSseMessage
    es.onerror = () => { if (mountedRef.current) setConnected(false) }
    return () => {
      mountedRef.current = false
      clearInterval(countTimer)
      if (historyRetryTimerRef.current) { window.clearTimeout(historyRetryTimerRef.current); historyRetryTimerRef.current = null }
      es.close()
      esRef.current = null
    }
  }, [fetchHistory, loadCounts, handleSseMessage])

  // v362: 时段变化 → 清空当前列表并按新窗口重取（首跑由挂载 effect 负责，跳过一次）
  const periodKey = period?.key
  const periodFrom = period?.fromTs
  const periodTo = period?.toTs
  const periodHours = period?.hours
  useEffect(() => {
    if (firstPeriodRunRef.current) { firstPeriodRunRef.current = false; return }
    void fetchHistory({ reset: true })
    void loadCounts()
  }, [periodKey, periodFrom, periodTo, periodHours, fetchHistory, loadCounts])

  useEffect(() => {
    onEventsRef.current?.(events.slice(0, SHARE_LIMIT))
  }, [events])

  const toggleExpand = useCallback((id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }, [])

  // ── 导出下拉菜单：合并「今日信号 /api/export/excel」与「反思系统全量 /api/review/export.xlsx」 ──
  // 两个端点返回不同范围的 .xlsx（前者单 Sheet 信号明细，后者 5 Sheets 反思全量），
  // 放在 EventBus 头部统一入口，避免 review-overview 顶部再独立出一个 XLSX 按钮重复占用首屏。
  const [exportMenuOpen, setExportMenuOpen] = useState(false)
  const [exportingKey, setExportingKey] = useState<null | 'signals' | 'review'>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const exportMenuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!exportMenuOpen) return
    const onDocClick = (ev: MouseEvent) => {
      if (exportMenuRef.current && !exportMenuRef.current.contains(ev.target as Node)) {
        setExportMenuOpen(false)
      }
    }
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') setExportMenuOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [exportMenuOpen])

  const closeExportMenu = useCallback(() => {
    setExportMenuOpen(false)
  }, [])

  // 选项 A：今日信号明细 —— 后端流式响应，直接新开 tab 触发下载即可（最快路径）
  const exportSignals = useCallback(() => {
    setExportError(null)
    window.open(`${API_BASE}/export/excel`, '_blank')
    closeExportMenu()
  }, [closeExportMenu])

  // 选项 B：反思系统全量 —— 后端 JSON 错误回包 + Content-Disposition 解析文件名，
  // 用 blob + <a download> 触发可靠下载（与 review-overview 旧实现一致）
  const exportReview = useCallback(async () => {
    if (exportingKey) return
    setExportingKey('review')
    setExportError(null)
    try {
      const res = await fetch(`${API_BASE}/review/export.xlsx`, { cache: 'no-store' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const cd = res.headers.get('Content-Disposition') || ''
      const m = cd.match(/filename="([^"]+)"/)
      const filename = m?.[1] || `trident_review_${Date.now()}.xlsx`
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      closeExportMenu()
    } catch (exc: unknown) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      setExportError(msg)
    } finally {
      setExportingKey(null)
    }
  }, [closeExportMenu, exportingKey])

  const filteredEvents = (() => {
    let list: ApiEvent[] = activeMarket === 'ALL'
      ? events
      : events.filter((event) => eventCategory(event) === activeMarket)
    // v188 P0+：「今日信号」过滤 —— 仅今日 + DONE + 非 HOLD/FAILED
    if (todayOnly) {
      // 用本地日期（e.timestamp 是北京时间的 "YYYY-MM-DD HH:MM:SS"，不带时区）。
      // 不能用 toISOString().slice(0,10)（UTC），否则本地凌晨 0-8 点会切到前一天。
      const now = new Date()
      const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
      list = list.filter((e) => {
        const ts = (e.timestamp || '').slice(0, 10)
        return ts === todayStr
          && e.analysis_status === 'DONE'
          && e.action !== 'HOLD'
          && e.action !== 'FAILED'
      })
    }
    return list
  })()

  // P3-UI: 收起态首屏只露 20 条摘要（方案规格：默认 20 条，展开才看原文）；
  //     「展开全部」走 showAll=true 完整列表。
  const VISIBLE_INITIAL = 20
  const visibleEvents = showAll ? filteredEvents : filteredEvents.slice(0, VISIBLE_INITIAL)

  return (
    // v286 — showAll=true 时 section 用 h-auto + min-h-0：让左列自然撑高，事件列表以全部内容呈现；
    //        showAll=false 时回到 h-full flex-col，ol flex-1 + max-h 在面板内独立滚动。
    <section
      className={
        'glass flex flex-col rounded-lg border border-border ' +
        (showAll ? 'h-auto min-h-0' : 'h-full')
      }
      aria-label="实时信号台"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Radio className="h-4 w-4 text-primary" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-foreground">实时事件流</h2>
          {/* v286 — 屏上条数计数：让用户立刻知道总共有多少。
                showAll=true 时显示「全部 N 条」（不再显示「visibleEvents.length /」分数，避免误读为截断）。 */}
          <span
            className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] tabular-nums text-primary"
            title={showAll
              ? `当前显示全部 ${visibleEvents.length} 条${filteredEvents.length !== events.length ? `（库内 ${events.length} 条）` : ''}`
              : `当前面板显示 ${visibleEvents.length} / ${filteredEvents.length} 条${filteredEvents.length !== events.length ? `（库内 ${events.length} 条）` : ''}（收起态，截到前 ${VISIBLE_INITIAL} 条）`}
          >
            {showAll
              ? `全部 ${visibleEvents.length}${filteredEvents.length !== events.length ? '' : ''}`
              : `${visibleEvents.length}/${filteredEvents.length}`}
          </span>
          {filteredEvents.length !== events.length && (
            <span
              className="font-mono text-[11px] text-muted-foreground/70"
              title="「未分类前的全库条数」（activeMarket + todayOnly 过滤前）"
            >
              ({events.length} 库内)
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {/* v286 — 紧凑模式 toggle：缩 title/降低 padding，单屏多塞 ~40% 条目。默认开。 */}
          <button
            type="button"
            onClick={() => setCompact((v) => !v)}
            className={
              'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[11px] transition-colors ' +
              (compact
                ? 'border-primary/50 bg-primary/15 text-primary'
                : 'border-border text-muted-foreground hover:text-foreground hover:border-primary/50')
            }
            title={compact ? '当前：紧凑模式（单行 + 缩小 padding，单屏多约 40% 条目）。点击切回标准模式' : '当前：标准模式。点击切换到紧凑模式（默认）'}
            aria-pressed={compact}
          >
            <span aria-hidden="true">≡</span>
            <span>紧凑</span>
          </button>
          {/* v286 — 全部/收起 toggle：默认「全部」（不限高、显示全部事件），点此切到「收起」限高态。 */}
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className={
              'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[11px] transition-colors ' +
              (showAll
                ? 'border-amber-500/50 bg-amber-500/15 text-amber-500'
                : 'border-border text-muted-foreground hover:text-foreground hover:border-primary/50')
            }
            title={showAll ? `当前：展开全部（共 ${filteredEvents.length} 条全部渲染，页面会拉长）。点击切回收起限高态` : `当前：收起限高态（首屏前 ${VISIBLE_INITIAL} 条摘要；共 ${filteredEvents.length} 条）。点击展开全部`}
            aria-pressed={showAll}
          >
            <span aria-hidden="true">{showAll ? '↕' : '↓'}</span>
            <span>{showAll ? '全部' : '收起'}</span>
          </button>
          {/* 统一导出入口：合并 EventBus「今日信号」与 ReviewOverview「反思系统全量」两路 xlsx */}
          <div ref={exportMenuRef} className="relative">
            <button
              type="button"
              onClick={() => setExportMenuOpen((v) => !v)}
              className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground hover:border-primary/50 transition-colors"
              title="导出 .xlsx（信号明细 / 反思系统全量）"
              aria-haspopup="menu"
              aria-expanded={exportMenuOpen}
            >
              <Download className="h-3 w-3" />
              <span>导出</span>
              <ChevronDown
                className={'h-3 w-3 transition-transform ' + (exportMenuOpen ? 'rotate-180' : '')}
                aria-hidden="true"
              />
            </button>
            {exportMenuOpen && (
              <div
                role="menu"
                className="absolute right-0 top-[calc(100%+4px)] z-40 w-64 rounded-md border border-border bg-card shadow-lg"
              >
                <div className="border-b border-border/60 px-3 py-1.5 font-mono text-[11px] uppercase tracking-widest text-muted-foreground">
                  导出 .xlsx
                </div>
                <button
                  type="button"
                  role="menuitem"
                  onClick={exportSignals}
                  className="flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-secondary/60"
                >
                  <span className="mt-0.5 text-base leading-none" aria-hidden="true">📊</span>
                  <span className="flex-1">
                    <span className="block text-[11px] font-medium text-foreground">今日信号明细</span>
                    <span className="mt-0.5 block font-mono text-[11px] text-muted-foreground">
                      xlsx · 最近 5 天 BUY/SELL · BTC / 黄金 / 原油
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => void exportReview()}
                  disabled={exportingKey === 'review'}
                  className="flex w-full items-start gap-2 border-t border-border/40 px-3 py-2 text-left transition-colors hover:bg-secondary/60 disabled:cursor-wait disabled:opacity-60"
                >
                  <span className="mt-0.5 text-base leading-none" aria-hidden="true">🗂️</span>
                  <span className="flex-1">
                    <span className="block text-[11px] font-medium text-foreground">
                      {exportingKey === 'review' ? '打包中…' : '反思系统全量'}
                    </span>
                    <span className="mt-0.5 block font-mono text-[11px] text-muted-foreground">
                      xlsx · 5 Sheets · Overview / Trades / Reflections / Insights / Experiences
                    </span>
                  </span>
                </button>
                {exportError && (
                  <p className="border-t border-border/40 px-3 py-1.5 font-mono text-[11px] text-muted-foreground">
                    导出失败: {exportError}
                  </p>
                )}
              </div>
            )}
          </div>
          {connected ? <Wifi className="h-3.5 w-3.5 text-long" /> : <WifiOff className="h-3.5 w-3.5 text-muted-foreground" />}
          <span className="font-mono text-[11px] uppercase tracking-widest text-muted-foreground">live</span>
        </div>
      </div>
      {/* 门卫筛选双流 — 显示 L1/L2 筛子"晒进来 / 没晒进来"的最新新闻。零 LLM，15s 轮询 */}
      <GatekeeperFilterStream />
      {/* v285 — 限高 / 全量由 showAll 决定；紧凑模式降低 padding + 标题行数 */}
      <div
        className={
          'flex gap-1 overflow-x-auto border-b border-border px-3 ' +
          (compact ? 'py-1' : 'py-2') +
          ' thin-scroll'
        }
        aria-label="新闻分类"
      >
        {EVENT_CATEGORIES.map((category) => {
          const count = category.key === 'ALL'
            ? (libraryCount ?? events.length)
            : events.filter((event) => eventCategory(event) === category.key).length
          return (
            <button
              key={category.key}
              type="button"
              onClick={() => onMarketChange?.(category.key)}
              className={'inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-colors ' + (
                activeMarket === category.key
                  ? 'bg-primary/12 text-primary'
                  : 'text-muted-foreground hover:bg-secondary hover:text-foreground'
              )}
            >
              {category.label}
              <span className="font-mono text-[11px] opacity-60">{count}</span>
            </button>
          )
        })}
      </div>
      <ol
        className={
          'thin-scroll divide-y divide-border ' +
          // v286 — showAll=true（默认）→ flex-none（自然撑高、不被父 flex 抢占剩余空间）、无 max-h，
          //        配合 section 改 h-auto，让左列自然高度显示全部事件。
          //        showAll=false（「收起」态）→ flex-1 撑满 + max-h 提到 min(95vh, 1800px)，
          //        面板内独立滚动，紧凑/标准都给到比较宽松的高度。
          (showAll
            ? 'flex-none'
            : 'flex-1 overflow-y-auto min-h-[700px] max-h-[min(95vh,1800px)] ' +
              (compact ? '' : 'lg:max-h-[min(85vh,1500px)]'))
        }
      >
        {error && events.length === 0 && (
          <li className="px-4 py-8 text-center" title={friendlyError(error)}>
            <p className="text-[13px] text-foreground/80">事件流暂不可用</p>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
              后端返回错误（5xx）或正在重启，通常 1–2 分钟自愈；期间不阻断其它面板。
            </p>
            <p className="mt-1 font-mono text-[11px] text-muted-foreground/70">{friendlyError(error)}</p>
          </li>
        )}
        {!loaded && !error && events.length === 0 && (
          <li className="px-4 py-8 text-center">
            <Loader2 className="mx-auto h-4 w-4 animate-spin text-muted-foreground" />
            <p className="mt-2 text-[13px] text-foreground/80">载入中…</p>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">正在拉取所选时段的事件流</p>
          </li>
        )}
        {loaded && !error && events.length === 0 && (
          <li className="px-4 py-8 text-center">
            <p className="text-[13px] text-foreground/80">当前时段暂无事件</p>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
              这里显示所选时段内的事件与实时新推送；该时段内后端没有可取事件时即为空，不是加载失败。
              <br />
              超出当前时段的历史逐条记录见
              <Link href="/review" className="text-primary underline-offset-2 hover:underline">复盘</Link> 与
              <Link href="/missed-signals" className="text-primary underline-offset-2 hover:underline">漏检信号</Link> 页。
            </p>
          </li>
        )}
        {!error && events.length > 0 && filteredEvents.length === 0 && (
          <li className="px-4 py-6 text-center text-sm text-muted-foreground">该分类暂无新闻</li>
        )}
        {/* v285 — visibleEvents = filteredEvents 在 showAll=false 时截断到前 200 条，
               防止 1300 条全量渲染卡首屏；展开模式下用完整 filteredEvents */}
        {foldSameWindow(visibleEvents).map((item) => (
          <EventItem
            key={item.id}
            item={item}
            open={expandedIds.has(item.id)}
            onToggle={toggleExpand}
            foldedCount={item.foldedCount ?? 0}
            compact={compact}
            textMissingConfirmed={restCoveredIdsRef.current.has(item.id) && !item.news_text}
          />
        ))}
        {!showAll && visibleEvents.length < filteredEvents.length && (
          <li className="px-4 py-3 text-center">
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/10 px-3 py-1 font-mono text-[11px] text-primary transition-colors hover:bg-primary/20"
            >
              <span aria-hidden="true">↓</span>
              <span>
                还有 {filteredEvents.length - visibleEvents.length} 条 — 点此展开全部
              </span>
            </button>
          </li>
        )}
      </ol>
    </section>
  )
}

// 实时推送类源：到达即发布、没有独立"平台发布时间"概念（WS 金融快讯 / webhook 推送）
function isRealtimePushSource(source: string): boolean {
  const s = (source || '').toLowerCase()
  return s.startsWith('ws:') || s.includes('financialjuice') || s.includes('tree') || s.includes('telegram')
}

// 官方数据类源（FRED）：轮询检测到官方发布，数据所属日期在标题/正文（观测日 X），
// 无独立"发布时间"字段可比对——若用观测日当发布时间会被新鲜度闸误杀（月度数据天然滞后）。
function isOfficialDataSource(source: string): boolean {
  const s = (source || '').toLowerCase()
  return s.includes('fred') || s.includes('stlouis')
}

// ⏱ 徽章三档证据（优先级从高到低）：
//   ┌ cross_net_first_seen_ts 非空 → 「⏱ MM-DD HH:MM ·+Xh」精确时间 + lag（紫）
//   │   └ AMBIGUOUS                  → 「⏱ ?」灰徽章，点开看 AI 解释
//   ├ net_origin_info 非空        → 「⏱ —」灰紫徽章，点开看 AI 原文
//   └ 仅 first_seen_ts            → 「⏱ MM-DD HH:MM ·+Xh」灰徽章（系统多源去重）
//   └ 都空                         → 不渲染
// 紧凑日期时间："2026-09-28 16:31:23" / "2026-09-28T16:31:23+08:00" → "09-28 16:31"
function fmtShortMdHm(value: string): string {
  if (!value) return ''
  const m = value.match(/(\d{2})-(\d{2})[ T](\d{2}:\d{2})/)
  if (m) return `${m[1]}-${m[2]} ${m[3]}`
  return value.slice(5, 16).replace('T', ' ')
}

// 计算"我们入库时间 - 原始时间"的延迟（秒）。两端都是北京时间（后端 _format_time 已统一）。
// 后端 raw_news.timestamp 是入库时间（datetime('now')）；originalTs 是全网/系统最早时间。
// 负值表示原始时间晚于入库时间（理论上不该发生，但兜底截 0）。
function lagSec(originalTs: string, ingestTs: string): number | null {
  const a = timeToMs(originalTs)
  const b = timeToMs(ingestTs)
  if (a == null || b == null) return null
  return Math.max(0, Math.floor((b - a) / 1000))
}

// 紧凑延迟：<60s→+Xs；<1h→+Xm；<1d→+Xh（>0 时附 Ym）；≥1d→+Xd（>0 时附 Yh）。
function fmtLag(sec: number): string {
  if (sec < 60) return `+${sec}s`
  if (sec < 3600) return `+${Math.floor(sec / 60)}m`
  if (sec < 86400) {
    const h = Math.floor(sec / 3600)
    const m = Math.floor((sec % 3600) / 60)
    return m > 0 ? `+${h}h${m}m` : `+${h}h`
  }
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  return h > 0 ? `+${d}d${h}h` : `+${d}d`
}

function NetLagBadge({ item }: { item: ApiEvent }) {
  const netOrigin = (item.net_origin_info || '').trim()
  const firstSeen = (item.first_seen_ts || '').trim()
  const cnTs = (item.cross_net_first_seen_ts || '').trim()
  const cnSrc = (item.cross_net_source || '').trim()

  // ① AI 给出的结构化全网最早时间（默认直接显示 MM-DD HH:MM + +Xh 延迟）。
  //   AMBIGUOUS 没有可解析时间 → 显示 ⏱ ? 灰徽章，点开看 AI 解释。
  if (cnTs) {
    const ambiguous = cnTs === 'AMBIGUOUS'
    const lag = ambiguous ? null : lagSec(cnTs, item.timestamp)
    return (
      <details className="shrink-0 group" onClick={(e) => e.stopPropagation()}>
        <summary
          className={
            'inline-flex cursor-pointer list-none items-center gap-0.5 whitespace-nowrap rounded border px-1 py-px font-mono text-[11px] ' +
            (ambiguous
              ? 'border-zinc-500/30 bg-zinc-500/10 text-zinc-400'
              : 'border-violet-500/40 bg-violet-500/10 text-violet-400')
          }
        >
          <span aria-hidden="true">⏱</span>
          {ambiguous ? '?' : fmtShortMdHm(cnTs)}
          {lag != null && (
            <span className="text-violet-300/70">·{fmtLag(lag)}</span>
          )}
        </summary>
        <div className="mt-0.5 max-w-md rounded border border-violet-500/30 bg-zinc-900/80 px-1.5 py-1 text-[11px] leading-snug text-violet-200 shadow">
          {ambiguous ? (
            <div>事件存在两种可能时间，无法唯一判定</div>
          ) : (
            <div className="font-mono">全网最早：{cnTs}</div>
          )}
          {cnSrc && <div className="mt-0.5 text-violet-300/90">来源：{cnSrc}</div>}
          {lag != null && (
            <div className="mt-0.5 text-violet-300/70">延迟：{fmtLag(lag)}（入库 {item.timestamp?.slice(11, 16) || ''}）</div>
          )}
        </div>
      </details>
    )
  }

  // ② AI 自然语言回答（无结构化时间），显示 ⏱ — 灰徽章，点开看 AI 原文。
  if (netOrigin) {
    const noInfo = netOrigin === '无法确认'
    return (
      <details className="shrink-0 group" onClick={(e) => e.stopPropagation()}>
        <summary
          className={
            'inline-flex cursor-pointer list-none items-center gap-0.5 rounded border px-1 py-px font-mono text-[11px] ' +
            (noInfo
              ? 'border-zinc-500/30 bg-zinc-500/10 text-zinc-500'
              : 'border-violet-500/40 bg-violet-500/10 text-violet-500')
          }
        >
          <span aria-hidden="true">⏱</span>—
        </summary>
        <div className="mt-0.5 max-w-md rounded border border-violet-500/30 bg-zinc-900/80 px-1.5 py-1 text-[11px] leading-snug text-violet-200 shadow">
          {netOrigin}
        </div>
      </details>
    )
  }

  // ③ 系统多源去重降级显示（AI 没给全网首发时间）。直接显示系统最早采集时刻（MM-DD HH:MM + +Xh），
  //    点开看完整时间 + "≠ 首发时间" 提示。
  if (firstSeen) {
    const t = fmtShortMdHm(firstSeen) || firstSeen.slice(5, 16).replace('T', ' ')
    const lag = lagSec(firstSeen, item.timestamp)
    return (
      <details className="shrink-0 group" onClick={(e) => e.stopPropagation()}>
        <summary className="inline-flex cursor-pointer list-none items-center gap-0.5 whitespace-nowrap rounded border border-zinc-500/30 bg-zinc-500/10 px-1 py-px font-mono text-[11px] text-zinc-500">
          <span aria-hidden="true">⏱</span>
          {t}
          {lag != null && (
            <span className="text-zinc-400/80">·{fmtLag(lag)}</span>
          )}
        </summary>
        <div className="mt-0.5 max-w-md rounded border border-zinc-500/30 bg-zinc-900/80 px-1.5 py-1 text-[11px] leading-snug text-zinc-300 shadow">
          <div>AI 未确认全网首发时间</div>
          <div className="font-mono mt-0.5">系统最早采集：{firstSeen}</div>
          {lag != null && (
            <div className="mt-0.5 text-zinc-400">延迟：{fmtLag(lag)}（入库 {item.timestamp?.slice(11, 16) || ''}）</div>
          )}
          <div className="mt-0.5 text-zinc-400">（同内容多源去重）≠ 首发时间</div>
        </div>
      </details>
    )
  }

  return null
}

// AI 分析时间 + 分析耗时（= ai_time − 入库时间，精确到秒）。
// ai_time 是后端 ad.created_at（AI 完成分析并落库决策的时刻）；
// 耗时 = 从新闻入库到 AI 结论落库的间隔（含排队 + 分析），反映信号产出快慢。
function AiTime({ item }: { item: ApiEvent }) {
  const ai = item.ai_time || ''
  if (!ai || ai === '——') return null
  const takenSec = timeDeltaSeconds(ai, item.timestamp)  // AI 完成 − 入库
  return (
    <span className="hidden shrink-0 items-center gap-1 font-mono text-[11px] text-muted-foreground/60 xl:inline-flex" title={`AI 分析完成于 ${ai}；从新闻入库(${item.timestamp})到 AI 结论落库耗时 ${takenSec ?? '—'} 秒`}>
      AI {shortTimestamp(ai)}
      {takenSec !== null && (
        <span className={takenSec >= 60 ? 'text-amber-500/80' : 'text-long/80'}>
          ·{takenSec >= 60 ? `${Math.floor(takenSec / 60)}m${takenSec % 60 ? `${takenSec % 60}s` : ''}` : `${takenSec}s`}
        </span>
      )}
    </span>
  )
}

// 单条事件项 — React.memo：SSE 高频推送时只重渲染状态变化的那条，
// 其余条目不渲染（item 引用稳定 + onToggle 用 useCallback），解决分析中页面卡顿。
// 需求9：同 5 分钟窗口高相关新闻折为主标签（相关性用 AI 已给出的 market_category /
// target_asset 判断，不硬卡相关性阈值——AI 已判定同类即视为相关，按 AI 判断展示）
type FoldedEvent = ApiEvent & { foldedCount?: number }

function foldSameWindow(events: ApiEvent[]): FoldedEvent[] {
  const out: FoldedEvent[] = []
  const buckets = new Map<string, ApiEvent[]>()
  for (const e of events) {
    const t = Date.parse(e.timestamp) || 0
    const bucket = Math.floor(t / 300_000) // 5 分钟窗口
    const category = e.market_category && e.market_category !== 'AGGREGATE' && e.market_category !== 'ALL'
      ? e.market_category
      : (e.target_asset && e.target_asset !== 'NONE' ? e.target_asset.toUpperCase() : e.source || '?')
    const key = `${bucket}|${category}`
    const arr = buckets.get(key)
    if (arr) arr.push(e)
    else buckets.set(key, [e])
  }
  for (const arr of buckets.values()) {
    const head = arr[0] as FoldedEvent
    if (arr.length > 1) head.foldedCount = arr.length - 1
    out.push(head)
  }
  return out
}

const EventItem = memo(function EventItem({
  item,
  open,
  onToggle,
  foldedCount = 0,
  compact = false,
  textMissingConfirmed = false,
}: {
  item: ApiEvent
  open: boolean
  onToggle: (id: number) => void
  foldedCount?: number
  compact?: boolean
  /** true = 已经跑过补全、确认这条事件在库里就没有正文 */
  textMissingConfirmed?: boolean
}) {
  const isAnalyzed = item.analysis_status === 'DONE' && item.decision_id !== null
  const hasTracking = !!(item.entry_price && item.entry_price > 0) // 主行已建仓
  const mainAssetLabel = (item.target_asset || '').trim().toUpperCase() || null
  const mainAssetDisplay = mainAssetLabel && mainAssetLabel !== 'NONE' ? mainAssetLabel : null
  const mainDecisionVisible = isAnalyzed && !!item.action && ['BUY', 'SELL', 'HOLD', 'FAILED'].includes((item.action || '').toUpperCase())
  const hasAnyTracking = hasTracking
  const hasDoubao = !!(item.doubao_action && item.doubao_action !== 'HOLD')
  const expandable = mainDecisionVisible || hasAnyTracking || (!!(item.doubao_reasoning && item.doubao_reasoning.trim()))
  return (
    <li
      onClick={() => expandable && onToggle(item.id)}
      className={expandable ? 'cursor-pointer transition-colors hover:bg-secondary/30' + (open ? ' bg-secondary/20' : '') : ''}
    >
      {/* v285 — 紧凑模式：缩小 padding、标题单行裁断，匀出 ~40% 高度给更多条目 */}
      <div className={compact ? 'px-3 py-1 sm:px-4' : 'px-3 py-2.5 sm:px-4'}>
        <div className={compact ? 'mb-0.5 flex min-w-0 items-center gap-1.5' : 'mb-1.5 flex min-w-0 items-center gap-1.5'}>
          {eventCategories(item).map((cat) => (
            <span key={cat} className="shrink-0 rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {EVENT_CATEGORIES.find((category) => category.key === cat)?.label ?? cat}
            </span>
          ))}
          {sourceLabel(item.source) && (
            <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary">
              {sourceLabel(item.source)}
            </span>
          )}
          {foldedCount > 0 && (
            <span
              className="shrink-0 rounded bg-amber-500/10 px-1.5 py-0.5 font-mono text-[11px] text-amber-500"
              title={`同 5 分钟窗口内 ${foldedCount} 条同相关新闻已折叠为主标签`}
            >
              同窗 +{foldedCount}
            </span>
          )}
          <ImpactBadge analyzed={isAnalyzed} horizon={item.expected_horizon} text={item.news_text} />
          {isAnalyzed && <ImpactDetailBadge item={item} />}
          <NetLagBadge item={item} />
          {isAnalyzed && <AiTime item={item} />}
          <div className="ml-auto flex shrink-0 items-center gap-1">
            {isAnalyzed ? (
              <SignalBadge
                signal={item.action}
                traded={hasTracking}
                note={
                  // HOLD：用闸门原因（之前一律空，导致 dashboard 看不到为什么 HOLD）
                  // BUY/SELL 未建仓：用 no_trade_reason
                  item.action === 'HOLD'
                    ? (item.trade_gate_reason || item.no_trade_reason || undefined)
                    : (item.no_trade_reason || undefined)
                }
              />
            ) : (
              <AnalysisStatusBadge status={item.analysis_status} />
            )}
            {isAnalyzed && item.vip_tag && <VipBadge tag={item.vip_tag} />}
            {hasDoubao && <DoubaoBadge action={item.doubao_action} reason={item.doubao_reasoning} />}
            {item.settled === 1 && item.is_correct && <VerdictBadge verdict={item.is_correct} />}
            {expandable && (
              <span className="inline-flex h-4 w-4 items-center justify-center rounded text-muted-foreground/50">
                {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              </span>
            )}
          </div>
        </div>
        {item.news_text ? (
          <p
            className={
              // v306 — 去掉 line-clamp-2,新闻正文始终完整展开(原信息丢失问题)
              // 紧凑模式只保留 padding/字号差异,不再裁断文字
              'break-words font-semibold text-foreground/90 text-pretty ' +
              (compact
                ? 'text-[12px] leading-snug'
                : 'text-[13px] leading-relaxed')
            }
            title={item.news_text}
          >
            {item.news_text}
          </p>
        ) : (
          // v310: 正文缺失时给一句实话(而不是留一片空白, 让人以为"详情被删了")
          <p className={'text-muted-foreground/50 ' + (compact ? 'text-[11px]' : 'text-[12px]')}>
            {textMissingConfirmed ? '（此事件暂无正文）' : '（正文补全中…）'}
          </p>
        )}
        {(item.cluster_size ?? 1) > 1 && (
          <div className="mt-1"><HeatBadge size={item.cluster_size ?? 1} /></div>
        )}
        <div className={compact ? 'mt-1 flex items-center justify-between gap-2' : 'mt-1.5 flex items-center justify-between gap-2'}>
          {isAnalyzed ? (
            <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
              score {item.score > 0 ? '+' : ''}{item.score.toFixed(2)}
            </span>
          ) : (
            <span className="font-mono text-[11px] text-muted-foreground">{item.reason}</span>
          )}
          {isAnalyzed && item.reason && (
            <p className="max-w-[70%] truncate text-[11px] leading-relaxed text-muted-foreground">AI {item.reason}</p>
          )}
        </div>
        {(item.action === 'BUY' || item.action === 'SELL') && !hasTracking && meaningfulText(item.no_trade_reason) && (
          <div className="mt-1 inline-flex items-center gap-1.5 rounded border border-hold/40 bg-hold/10 px-2 py-0.5">
            <span className="font-mono text-[11px] font-bold tracking-wider text-hold">未建仓</span>
            <span className="font-mono text-[11px] text-muted-foreground" title={meaningfulText(item.no_trade_reason)}>{meaningfulText(item.no_trade_reason)}</span>
          </div>
        )}
      </div>
      {open && (
        <div className="border-t border-border/50 bg-secondary/25">
          {/* v311: 新闻溯源与影响 —— 发布时间/全网首发/闸门判定/影响解读。
              之前 bridge 的 /api/events 不返回这些字段，展开后只能看到 "no detail available"。 */}
          {(item.published_at || item.first_seen_ts || item.net_origin_info || item.cross_net_source
            || item.gate_core_impact || item.gate_verdict || item.url) && (
            <div className="border-b border-border/30 px-4 py-2.5">
              <div className="mb-1.5 font-mono text-[11px] uppercase tracking-wider text-muted-foreground/60">
                News provenance · 新闻溯源
              </div>
              <div className="space-y-1 text-[11px] leading-relaxed">
                {(item.published_at || item.first_seen_ts) && (
                  <div className="text-muted-foreground">
                    <span className="text-foreground/80">发布</span> {item.published_at || '—'}
                    {item.first_seen_ts ? (
                      <>
                        <span className="ml-2 text-foreground/80">全网首见</span> {item.first_seen_ts}
                      </>
                    ) : null}
                  </div>
                )}
                {(item.cross_net_source || item.net_origin_info) && (
                  <div className="text-muted-foreground">
                    <span className="text-foreground/80">首发</span> {item.cross_net_source || '—'}
                    {item.net_origin_info ? <span className="ml-1">（{item.net_origin_info}）</span> : null}
                  </div>
                )}
                {(item.gate_verdict || item.relevance_score != null) && (
                  <div className="text-muted-foreground">
                    <span className="text-foreground/80">闸门</span> {item.gate_verdict || '—'}
                    {item.relevance_score != null ? (
                      <>
                        <span className="ml-2 text-foreground/80">相关性</span> {item.relevance_score}
                      </>
                    ) : null}
                  </div>
                )}
                {item.gate_core_impact && (
                  <p className="whitespace-pre-wrap break-words text-muted-foreground">
                    <span className="text-foreground/80">影响 </span>
                    {item.gate_core_impact}
                  </p>
                )}
                {item.url && (
                  <div>
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-primary underline underline-offset-2 hover:text-primary/80"
                    >
                      查看原文 ↗
                    </a>
                  </div>
                )}
              </div>
            </div>
          )}
          {/* 交易决策说明：单一主决策的下单/不下单原因链（含闸门结论）。 */}
          {isAnalyzed && mainDecisionVisible && (
            <div className="border-b border-border/30 px-4 py-2.5">
              <TradeDecisionNote
                assetLabel={mainAssetDisplay}
                action={item.action}
                score={item.score}
                entryPrice={item.entry_price}
                noTradeReason={item.no_trade_reason}
                gateReason={item.trade_gate_reason}
              />
            </div>
          )}
          {/* 八段式推演：复用复盘页 AnalysisSteps，实时总线也展示完整分析过程（数据=reasoning_path） */}
          {isAnalyzed && meaningfulText(item.reasoning_path) && (
            <div className="border-b border-border/30 px-4 py-2.5">
              <AnalysisSteps
                reasoningPath={meaningfulText(item.reasoning_path)}
                mainAsset={item.target_asset}
                summary={{
                  action: item.action,
                  score: item.score,
                  reason: item.reason ?? '',
                  entryPrice: item.entry_price ?? undefined,
                  noTradeReason: item.no_trade_reason ?? undefined,
                  gateReason: item.trade_gate_reason ?? undefined,
                  horizon: item.expected_horizon ?? undefined,
                  eventStrength: item.event_strength ?? undefined,
                  directCatalyst: item.direct_catalyst ?? undefined,
                  marketConfirmation: item.market_confirmation ?? undefined,
                  heatScore: item.heat_score ?? undefined,
                  impactScore: item.impact_score ?? undefined,
                  assetImpact: item.asset_impact ?? undefined,
                  attentionScore: item.attention_score ?? undefined,
                  elasticityWindow: item.elasticity_window ?? undefined,
                  dataAnchorTs: item.data_anchor_ts ?? undefined,
                  hardDataEvidence: item.hard_data_evidence ?? undefined,
                  invalidation: item.invalidation_condition ?? undefined,
                }}
              />
            </div>
          )}
          {item.peer_reviews && item.peer_reviews.length > 0 && (
            <div className="px-4 py-2.5 border-b border-border/30">
              <PeerReviewsBlock mainAction={item.action} peers={item.peer_reviews} />
            </div>
          )}
          {item.doubao_reasoning && item.doubao_reasoning.trim() && (
            <div className="px-4 py-2.5 border-b border-border/30">
              <div className="mb-1.5 flex items-center gap-2">
                <span className="font-mono text-[11px] uppercase tracking-wider text-primary/80">Doubao</span>
                {item.doubao_action && (
                  <span className={'font-mono text-[11px] font-bold uppercase tracking-wider ' + (item.doubao_action === 'BUY' ? 'text-long' : item.doubao_action === 'SELL' ? 'text-short' : 'text-muted-foreground')}>
                    {item.doubao_action}
                  </span>
                )}
              </div>
              <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted-foreground/80 selection:bg-primary/20">{item.doubao_reasoning}</pre>
            </div>
          )}
          {hasAnyTracking && (
            <div className="px-4 py-2.5">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground/60">
                2h forward test · 持仓跟踪
                {hasTracking && item.settled === 0 && ' (tracking...)'}
              </div>
              <div className="flex flex-col gap-2">
                {hasTracking && (
                  <ForwardTrackBlock
                    action={item.action}
                    entry={item.entry_price}
                    max={item.max_price}
                    min={item.min_price}
                    exit={item.exit_price}
                    settled={item.settled}
                    isCorrect={item.is_correct}
                    thresholdAsset={item.target_asset}
                    partialPrice={item.partial_exit_price ?? null}
                    partialRatio={item.partial_exit_ratio ?? null}
                    partialPnlPct={item.partial_pnl_pct ?? null}
                    entryTime={item.entry_time}
                    maxPriceTime={item.max_price_time}
                    minPriceTime={item.min_price_time}
                  />
                )}
              </div>
            </div>
          )}
          {!(isAnalyzed && mainDecisionVisible) &&
            !hasAnyTracking &&
            !(item.peer_reviews && item.peer_reviews.length > 0) &&
            !(!!(item.doubao_reasoning && item.doubao_reasoning.trim())) &&
            // v311: 已有新闻溯源/影响信息时不再显示 "no detail available"
            !(item.published_at || item.net_origin_info || item.cross_net_source
              || item.gate_core_impact || item.gate_verdict) && (
            <div className="px-4 py-2.5 font-mono text-[11px] italic text-muted-foreground/40">no detail available</div>
          )}
        </div>
      )}
    </li>
  )
})
