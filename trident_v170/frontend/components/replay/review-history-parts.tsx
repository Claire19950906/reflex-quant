'use client'

/**
 * review-history-parts —— review-history-panel 的子组件 + helpers（v170 P0+）
 *
 * 主组件 review-history-panel.tsx 原本 981 行；将子组件 + fmt helpers 抽到这里，
 * 主文件保留主组件 + 类型导入，单文件行数降至 ~600。
 */

import { useEffect, useState } from 'react'
import { apiGet } from '@/lib/api'
import type { SignalItem, TradeRow, NewsTrade } from '@/lib/types-review-history'

// ─── 参考信号组 ────────────────────────────────────────────────────────────

export function SignalGroup({ title, items }: { title: string; items?: SignalItem[] }) {
  if (!items || items.length === 0) return null
  return (
    <div className="rounded-md border border-border bg-card p-2">
      <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{title}</div>
      <div className="mt-1 space-y-1.5">
        {items.map((it) => (
          <div key={it.label}>
            <div className="flex items-center gap-1.5 font-mono text-[10px]">
              <span className="w-14 shrink-0 truncate text-foreground" title={it.label}>{it.label}</span>
              <div className="h-1.5 flex-1 overflow-hidden rounded bg-secondary">
                <div
                  className={`h-full rounded ${(it.winrate ?? 0) >= 0.5 ? 'bg-long/60' : 'bg-short/60'}`}
                  style={{ width: `${Math.min(Math.round((it.winrate ?? 0) * 100), 100)}%` }}
                />
              </div>
              <span className={`w-8 shrink-0 text-right ${(it.winrate ?? 0) >= 0.5 ? 'text-long' : 'text-short'}`}>
                {it.winrate != null ? `${(it.winrate * 100).toFixed(0)}%` : '—'}
              </span>
              <span className="w-9 shrink-0 text-right text-muted-foreground">{it.total}笔</span>
              <span className={`w-12 shrink-0 text-right ${it.total_pnl >= 0 ? 'text-long' : 'text-short'}`}>
                {it.total_pnl > 0 ? '+' : ''}{it.total_pnl.toFixed(1)}
              </span>
            </div>
            {it.summary && (
              <div className="mt-0.5 pl-0.5 text-[9px] leading-snug text-muted-foreground">
                💡 {it.summary}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── 时间格式化 helpers ────────────────────────────────────────────────────

/** v180 P0+：Unix 秒 → "MM-DD HH:MM" */
export function fmtTs(ts: number | null | undefined): string {
  if (!ts) return ''
  try {
    const d = new Date(ts * 1000)
    const p = (n: number) => String(n).padStart(2, '0')
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  } catch {
    return ''
  }
}

/** ISO / "YYYY-MM-DD HH:MM:SS" → "MM-DD HH:MM" —— 新闻时间显示
 *  原版用 String(text).slice(5, 16) 对带 'T' 分隔的 ISO 返回 "MM-DDTHH:MM"，
 *  把 T 替成空格后跨格式一致（"YYYY-MM-DDTHH:MM:SS"、"YYYY-MM-DD HH:MM:SS"、
 *  带毫秒、带时区偏移都吃到同样结果）。 */
export function fmtTime(text: string | null | undefined): string {
  if (!text) return ''
  return String(text).replace('T', ' ').slice(5, 16)
}

/** 0-1 比例 → 百分号整数；null → — */
export function fmtPct(v: number | null | undefined): string {
  return v == null ? '—' : `${(v * 100).toFixed(0)}%`
}

/** 新闻列表（左栏）— 筛选 + 已结算新闻卡 + 选中态高亮 */
export function NewsListSidebar({
  items,
  selectedId,
  onSelect,
}: {
  items: NewsTrade[]
  selectedId: number | null
  onSelect: (newsId: number) => void
}) {
  return (
    <div className="thin-scroll max-h-[46vh] space-y-1 overflow-y-auto pr-0.5">
      {items.length === 0 && (
        <div className="rounded border border-border bg-secondary/20 p-2 font-mono text-[10px] text-muted-foreground">
          当前筛选下无匹配新闻
        </div>
      )}
      {items.map((item) => (
        <button
          key={item.news_id}
          type="button"
          onClick={() => onSelect(item.news_id)}
          className={`block w-full rounded border px-2 py-1.5 text-left transition ${
            selectedId === item.news_id ? 'border-primary/60 bg-primary/10' : 'border-border bg-card hover:border-primary/30'
          }`}
        >
          <div className="flex items-center gap-1.5 font-mono text-[9px] text-muted-foreground">
            <span>{item.source}</span>
            <span>{fmtTime(item.news_time)}</span>
            <span className={`ml-auto ${item.total_pnl >= 0 ? 'text-long' : 'text-short'}`}>
              {item.total_pnl > 0 ? '+' : ''}{item.total_pnl.toFixed(2)}%
            </span>
          </div>
          <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-foreground/85">{item.content}</div>
          <div className="mt-1 flex h-1 overflow-hidden rounded bg-secondary">
            {(item.wins + item.losses) > 0 ? (
              <>
                <div className="h-full bg-long/60" style={{ width: `${(item.wins / (item.wins + item.losses)) * 100}%` }} />
                <div className="h-full bg-short/60" style={{ width: `${(item.losses / (item.wins + item.losses)) * 100}%` }} />
              </>
            ) : (
              <div className="h-full w-full bg-secondary/60" />
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-2 font-mono text-[9px] text-muted-foreground">
            <span>{item.trade_count} 笔</span>
            <span className="text-long">胜 {item.wins}</span>
            <span className="text-short">负 {item.losses}</span>
            <span className={item.winrate != null && item.winrate >= 0.5 ? 'text-long' : 'text-short'}>
              胜率 {fmtPct(item.winrate)}
            </span>
            {item.has_review ? (
              <span className="ml-auto rounded border border-long/40 bg-long/10 px-1 text-long">已复盘 ✓</span>
            ) : (
              <span className="ml-auto text-muted-foreground/60">未复盘</span>
            )}
          </div>
        </button>
      ))}
    </div>
  )
}

// ─── 待判定原因映射（统一口径 §5） ────────────────────────────────────

const PENDING_REASON_LABEL: Record<string, { label: string; tone: string }> = {
  no_price_data:      { label: '待 K线', tone: 'border-short/40 bg-short/10 text-short' },
  extreme_unsettled:  { label: '待极值', tone: 'border-short/40 bg-short/10 text-short' },
  reflection_timeout: { label: 'worker 超时', tone: 'border-hold/40 bg-hold/10 text-hold' },
  model_retry_break:  { label: 'LLM 超限', tone: 'border-hold/40 bg-hold/10 text-hold' },
  data_quarantine:    { label: '已隔离', tone: 'border-muted-foreground/40 bg-muted/20 text-muted-foreground' },
}

// ─── 交易判定状态徽章 ────────────────────────────────────────────────

/**
 * v180 P0+：每笔交易的判定状态徽章
 *   - WIN/LOSS → 「已判定 · WIN/LOSS」徽章
 *   - BREAKEVEN/空 → 「待判定 + reason」徽章 + 鼠标悬浮显示原因详情
 *   - HOLD → 「未触发」灰徽章
 */
export function TradeStatusBadge({ trade }: { trade: TradeRow }) {
  const ic = trade.is_correct || trade.verdict || ''
  if (ic === 'WIN' || ic === 'LOSS') {
    const tone = ic === 'WIN'
      ? 'border-long/40 bg-long/10 text-long'
      : 'border-short/40 bg-short/10 text-short'
    return (
      <span className={`rounded border px-1 py-0.5 font-mono text-[9px] ${tone}`}>
        已判定 · {ic}
      </span>
    )
  }
  // 未触发（HOLD）：signal gate 拒了，没真建仓，不算待判定
  if (ic === 'BREAKEVEN' && trade.pnl_pct === 0) {
    return (
      <span className="rounded border border-muted-foreground/40 bg-muted/20 px-1 py-0.5 font-mono text-[9px] text-muted-foreground">
        持平
      </span>
    )
  }
  const reason = trade.pending_reason || 'reflection_timeout'
  const meta = PENDING_REASON_LABEL[reason] ?? { label: reason, tone: 'border-hold/40 bg-hold/10 text-hold' }
  return (
    <span className={`rounded border px-1 py-0.5 font-mono text-[9px] ${meta.tone}`} title={reason}>
      待判定 · {meta.label}
    </span>
  )
}

// ─── K线时间线 sparkline ─────────────────────────────────────────────────

/**
 * v180+ 锁定 · K线时间线 sparkline（重写于 v180+ · 用户多次要求硬编码，禁止回滚）
 * ┌─────────────────────────────────────────────────────────────────────────┐
 * │ 硬编码规则（写死 · 不要改）：                                              │
 * │   1. 没有任何「数据点圆圈」——曾经的灰色 dot 已删除（不要加回来）          │
 * │   2. entry / exit 价格文字从 SVG 内拿出，放到上方 flex 横排               │
 * │      （一个左一个右），任何情况下都不可能堆叠/重叠                         │
 * │   3. SVG 内只剩 polyline 折线 + 两条虚线，绝不会被任何文字遮挡             │
 * │   4. SVG 尺寸 320×56 写死，不允许 responsive 改尺寸                       │
 * │   5. SVG 上面到外层 p-2 写死 mt-1 间距，禁止任何元素侵入 SVG              │
 * └─────────────────────────────────────────────────────────────────────────┘
 */
export function TradeSparkline({ trade }: { trade: TradeRow }) {
  const [bars, setBars] = useState<Array<{ t: number; c: number }> | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    if (!trade.entry_ts) {
      setBars(null)
      return () => { alive = false }
    }
    // 直接复用 /api/klines：传 limit=80 即可覆盖 ~1h20min
    const symbol = trade.asset === 'XAU' ? 'XAUUSD'
      : trade.asset === 'WTI' ? 'WTIUSD'
      : trade.asset === 'BTC' || trade.asset === 'ETH' || trade.asset === 'SOL' ? `${trade.asset}USDT`
      : trade.asset
    void (async () => {
      try {
        const data = await apiGet<Array<{ time: number; close: number }>>(
          `/klines/${symbol}?interval=1m&limit=80`,
        )
        if (!alive) return
        const filtered = data
          .map(b => ({ t: b.time, c: Number(b.close) }))
          .filter(b => b.t >= trade.entry_ts! - 5 * 60)
        setBars(filtered.length > 0 ? filtered : data.slice(-30).map(b => ({ t: b.time, c: Number(b.close) })))
        setLoadErr(null)
      } catch (e) {
        if (!alive) return
        setLoadErr(e instanceof Error ? e.message : 'K 线拉取失败')
        setBars(null)
      }
    })()
    return () => { alive = false }
  }, [trade.id, trade.entry_ts, trade.exit_ts, trade.asset])

  if (loadErr) {
    return (
      <div className="mt-1 rounded border border-short/30 bg-short/5 px-2 py-1 font-mono text-[9px] text-short">
        K线加载失败：{loadErr}
      </div>
    )
  }
  if (!bars || bars.length < 2) {
    return (
      <div className="mt-1 rounded border border-border bg-secondary/20 px-2 py-1 font-mono text-[9px] text-muted-foreground">
        K线准备中…
      </div>
    )
  }
  // 硬编码 SVG 尺寸（写死 · 不允许改）
  const width = 320
  const height = 56
  const padX = 4
  const padY = 6
  const minC = Math.min(...bars.map(b => b.c))
  const maxC = Math.max(...bars.map(b => b.c))
  const range = Math.max(maxC - minC, 1e-6)
  const xAt = (i: number) => padX + (i / (bars.length - 1)) * (width - 2 * padX)
  const yAt = (c: number) => height - padY - ((c - minC) / range) * (height - 2 * padY)
  const polyline = bars.map((b, i) => `${xAt(i)},${yAt(b.c)}`).join(' ')
  // entry / exit 价格 y 坐标仅用于画水平虚线（不再画文字）
  const entryY = trade.entry_price ? yAt(trade.entry_price) : null
  const exitY = trade.exit_price ? yAt(trade.exit_price) : null

  return (
    <div className="mt-1 rounded border border-border bg-background/60 p-2">
      {/* 价格文字锁死在 SVG 外面 · flex 横排 · 任何情况不重叠（写死 · 不要改） */}
      <div className="flex items-center justify-between font-mono text-[8px] leading-none">
        <span className="text-long">入 {trade.entry_price?.toFixed(2) ?? '—'}</span>
        <span className="text-short">出 {trade.exit_price?.toFixed(2) ?? '—'}</span>
      </div>
      {/* SVG 写死 320×56 · mt-1 间距 · 不会有任何元素闯进来 */}
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="mt-1 block w-full">
        {/* K 线主线（折线）—— 没有数据点圆圈，灰色 dot 已删除 */}
        <polyline fill="none" stroke="currentColor" strokeWidth="1" className="text-primary/80"
          points={polyline} />
        {/* entry / exit 水平虚线 —— 在 SVG 内，但 SVG 上面没有文字 */}
        {entryY !== null && (
          <line x1={padX} x2={width - padX} y1={entryY} y2={entryY}
            strokeDasharray="3 2" stroke="currentColor" strokeWidth="0.6" className="text-long/70" />
        )}
        {exitY !== null && (
          <line x1={padX} x2={width - padX} y1={exitY} y2={exitY}
            strokeDasharray="3 2" stroke="currentColor" strokeWidth="0.6" className="text-short/70" />
        )}
      </svg>
    </div>
  )
}

/** 参考信号块（提炼自快速评测）— 展开/收起 + 5 个分组（品种/方向/阶段/强度/时机） */
export function ReviewReferenceSignals({
  signals,
  showSignals,
  onToggleShow,
}: {
  signals: {
    overall?: { total: number; winrate: number | null; total_pnl: number } | null
    assets?: SignalItem[]
    directions?: SignalItem[]
    phases?: SignalItem[]
    strengths?: SignalItem[]
    intervals?: SignalItem[]
  } | null
  showSignals: boolean
  onToggleShow: () => void
}) {
  return (
    <div className="mt-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-widest text-primary/80">
          提炼的参考信号（分析新新闻）
        </span>
        <span className="rounded border border-primary/20 bg-primary/5 px-1 py-0.5 font-mono text-[9px] text-primary/70">
          数据=快速评测全信号（quick_sim_trades）
        </span>
        {signals?.overall && (
          <span className="ml-auto font-mono text-[9px] text-muted-foreground">
            全局 {signals.overall.total} 笔 · 胜率{' '}
            {signals.overall.winrate != null ? `${(signals.overall.winrate * 100).toFixed(1)}%` : '—'}
            {' · 总盈亏 '}
            <span className={signals.overall.total_pnl >= 0 ? 'text-long' : 'text-short'}>
              {signals.overall.total_pnl > 0 ? '+' : ''}{signals.overall.total_pnl.toFixed(2)}%
            </span>
          </span>
        )}
        <button
          type="button"
          onClick={onToggleShow}
          className="rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground"
        >
          {showSignals ? '收起 ▾' : '展开 ▸'}
        </button>
      </div>
      {showSignals && (
        signals ? (
          <div className="mt-1.5 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            <SignalGroup title="品种" items={signals.assets} />
            <SignalGroup title="多空方向" items={signals.directions} />
            <SignalGroup title="事件阶段" items={signals.phases} />
            <SignalGroup title="事件强度" items={signals.strengths} />
            <SignalGroup title="新闻后入场时机" items={signals.intervals} />
          </div>
        ) : (
          <div className="mt-1 font-mono text-[10px] text-muted-foreground">信号加载中…</div>
        )
      )}
    </div>
  )
}
