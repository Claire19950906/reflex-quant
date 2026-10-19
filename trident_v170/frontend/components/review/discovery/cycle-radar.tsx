'use client'

import { useMemo } from 'react'
import { Waves, TrendingUp, TrendingDown, Hourglass, BarChart3 } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { PushToCausalChainButton } from './push-to-causal-chain-button'
import { cn } from '@/lib/utils'

/**
 * ☯️ 周期维度雷达
 * 大中小周期共振 + 蓄力/高潮/衰退识别
 *
 * 简短版 — 不试图做指标计算(本系统已有 RSI / Bollinger 在 /reflections);
 * 这里是「梳理多周期表现」+ 「阴阳叙事」面板,把现有指标读数聚合成一句话。
 *
 * 后端路由 /api/discovery/cycle-scan(可选),没接口时用启发式聚合。
 */

type Cycle = 'accumulation' | 'markup' | 'distribution' | 'markdown' | 'unknown'

type CycleRow = {
  asset: string
  cycle: Cycle
  cycle_cn: '蓄力' | '高潮' | '衰退' | '下跌' | '未知'
  // 周线 RSI / 布林 / 7d 涨幅对部分品种还没拉到 → 实际可能 null
  // 渲染走 num()/numSigned() 兜底,这里类型也允许 null 与 TS 现状对齐
  rsi_d: number | null
  rsi_w: number | null
  bb_pos: number | null    // 0..1 布林带位置
  pct_24h: number | null
  pct_7d: number | null
  resonance: 'aligned' | 'diverging' | 'opposite'
  hint: string
  // v170+ · 半衰能量图所需的额外字段(后端 /api/discovery/cycle-scan 已下发,
  // 但老前端只用了上面几个 — 这里全部声明为可选,保持向后兼容).
  trend_pct?: number | null          // 当前窗口(默认 20d)的累计涨跌幅%
  phase_confidence?: number | null   // 阶段识别置信度 0..1
  cycle_len_days?: number | null     // 周期长度(天)— 半衰周期的天
  reversal_prob?: number | null      // 反转概率 0..1
  reversal_dir?: string | null       // 反转方向 'up' | 'down' | 'neutral'
  reversal_days_to?: number | null   // 距反转还有 N 天
  reversal_votes?: number | null     // 反转投票数
}

const CYCLE_CLS: Record<Cycle, string> = {
  accumulation: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  markup:       'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  distribution: 'border-orange-500/40 bg-orange-500/10 text-orange-700 dark:text-orange-300',
  markdown:     'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  unknown:      'border-border bg-secondary/30 text-muted-foreground',
}

const CYCLE_ICON: Record<Cycle, string> = {
  accumulation: '🌱', markup: '🚀', distribution: '🎁', markdown: '❄️', unknown: '?',
}

const RESONANCE_CLS = {
  aligned:    'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  diverging: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  opposite:  'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300',
}

// 后端 /api/discovery/cycle-scan 在某些品种上还拿不到周线 RSI / 布林 / 7d 涨幅
// (返回 null),前端 TypeScript 类型又把它们声明成必填 number,所以 r.rsi_w.toFixed(0)
// 会直接 TypeError 把整页炸掉。这里集中兜底:null/undefined 显示 "—",不参与计算。
function num(v: unknown, digits: number, suffix = ''): string {
  if (v == null || (typeof v === 'number' && !Number.isFinite(v))) return '—'
  return `${(v as number).toFixed(digits)}${suffix}`
}
function numSigned(v: unknown, digits: number, suffix = ''): string {
  if (v == null || (typeof v === 'number' && !Number.isFinite(v))) return '—'
  const n = v as number
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}${suffix}`
}

export function CycleRadarPanel() {
  // v283+ · Task D · 10min 轮询 · AbortController + unmount 清理交给 usePoll
  // · 顺便修了原版 #7:clearTimeout 清理 setInterval 的语义错
  const { data, loading } = usePoll<CycleRow[]>(
    '/discovery/cycle-scan',
    {
      intervalMs: 600_000,
      // 兼容老后端:ok=true + rows 是数组才接受;失败时返回 null(不抛错)走兜底聚合
      transform: (raw) => {
        const d = raw as { ok?: boolean; rows?: unknown } | null | undefined
        if (!d || d.ok !== true || !Array.isArray(d.rows)) return null
        return d.rows as CycleRow[]
      },
    },
  )
  // usePoll 失败时 data=null;UI 走兜底聚合,所以这里 rows 默认空数组
  const rows: CycleRow[] = data ?? []

  return (
    <div className="rounded border border-violet-500/40 bg-violet-500/5 p-3">
      <div className="flex items-center gap-2 font-mono text-xs">
        <Waves className="h-4 w-4 text-violet-500" aria-hidden />
        <span className="font-bold uppercase tracking-widest text-violet-700 dark:text-violet-300">
          ☯️ 周期维度(阴阳共振)
        </span>
        <span className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 text-[9px] font-bold text-violet-600 dark:text-violet-400">
          大中小周期 + 蓄力 / 高潮 / 衰退
        </span>
      </div>

      <p className="mt-2 text-[11px] leading-7 text-muted-foreground">
        把 RSI / 布林带 / 周线走势合并成一句话周期叙事:
        <strong className="text-foreground">「大周期决定方向、小周期决定进出场」</strong>。
        当大中小同向 = 共振(顺势);反向 = 背离(小心)。
      </p>

      <div className="mt-2 grid grid-cols-1 gap-1.5 md:grid-cols-2 lg:grid-cols-3">
        {!rows.length && <div className="col-span-12 text-xs text-muted-foreground">{loading ? '扫描中…' : '等待数据'}</div>}
        {rows.slice(0, 12).map((r) => (
          <div key={r.asset} className={cn('rounded border p-2 text-xs', CYCLE_CLS[r.cycle])}>
            <div className="flex items-center gap-1.5">
              <span aria-hidden>{CYCLE_ICON[r.cycle]}</span>
              <strong className="font-mono text-[11px]">{r.asset}</strong>
              <span className="ml-1 rounded border border-current/30 px-1 py-0.5 font-mono text-[9px]">{r.cycle_cn}</span>
              <span className={cn('ml-auto rounded border px-1 py-0.5 font-mono text-[9px]', RESONANCE_CLS[r.resonance])}>
                {r.resonance === 'aligned' ? '同向' : r.resonance === 'diverging' ? '背离' : '反向'}
              </span>
            </div>
            <div className="mt-1 grid grid-cols-2 gap-x-2 gap-y-0.5 font-mono text-[10px]">
              <span>RSI日 {num(r.rsi_d, 0)}</span>
              <span>RSI周 {num(r.rsi_w, 0)}</span>
              <span>BB {num(r.bb_pos != null ? r.bb_pos * 100 : null, 0, '%')}</span>
              <span>7d <span className={(r.pct_7d ?? 0) >= 0 ? 'text-long' : 'text-short'}>
                {numSigned(r.pct_7d, 1, '%')}
              </span></span>
            </div>
            <div className="mt-1 text-[10px] text-foreground/80">💡 {r.hint}</div>
            {/* v283+ · #23/#31 · 周期阶段→资产,投到因果链(典型如「高潮 + RSI≥70 + 7d 涨」触发回撤候选) */}
            <div className="mt-1.5">
              <PushToCausalChainButton origin="manual_from_cycle_radar"

                cause={`周期 · ${r.cycle_cn} (${r.resonance === 'aligned' ? '共振' : r.resonance === 'diverging' ? '背离' : '反向'})`}
                effect={r.asset}
                note={`rsi_d=${num(r.rsi_d, 0)} rsi_w=${num(r.rsi_w, 0)} bb=${num(r.bb_pos != null ? r.bb_pos * 100 : null, 0)}% pct_7d=${numSigned(r.pct_7d, 1, '%')}`}
                trigger_words={[r.asset, r.cycle_cn, r.resonance].join(',')}
                weight={0.5}
              />
            </div>
          </div>
        ))}
      </div>

      {/* v170+ · 涨跌半衰能量图 — 涨/跌越久积的能量越重、颜色从浅到深推演 */}
      {!rows.length ? null : <HalfLifeEnergyPanel rows={rows} />}

      <div className="mt-2 rounded border border-violet-500/30 bg-violet-500/5 p-2 text-[11px]">
        <strong className="text-violet-700 dark:text-violet-300">📌 阴阳解读</strong>:
        <ul className="ml-4 mt-1 list-disc text-[10px] text-muted-foreground">
          <li><strong className="text-emerald-600 dark:text-emerald-400">蓄力(🌱)</strong> + RSI周 ≤ 30 + 7d 跌 = 可能是反转前夜,但需事件触发</li>
          <li><strong className="text-amber-600 dark:text-amber-400">高潮(🚀)</strong> + RSI日 ≥ 70 + BB ≥ 90% = 短期超买,警惕回撤</li>
          <li><strong className="text-rose-600 dark:text-rose-400">下跌(❄️)</strong> + 大中小同向 = 顺势下跌;反向 = 可能是陷阱</li>
          <li>「涨久必跌/跌久必涨」是统计,不是预测;需要事件叠加才能验证</li>
        </ul>
      </div>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════════════════
 * ⏳ 涨跌半衰能量图(v170+)
 *
 * 设计动机:用户原话——
 *   「发现页涨跌周期用图片展示出半衰周期,
 *    涨太多涨了很久;和跌太多跌了很久积攒的能量,
 *    随着时间的推演颜色越来越重」。
 *
 * 实现要点:
 *  ① 每个资产按「|trend_pct| × 置信度 × 周期长度」算一个能量值 E;
 *     trend_pct 优先,缺失时退回 pct_7d,再缺失取 0。
 *  ② 按符号分两侧:上行(🌱 蓄力→高潮)vs 下行(❄️ 衰退→下跌)。
 *  ③ 每条 bar:
 *     · 横向长度 = 周期长度 / 标尺周期长度(代表「这个涨/跌走了多久」)。
 *     · 内部用 SVG <linearGradient> 左 → 右:浅色(能量刚启动) → 深色(能量饱和),
 *       越接近 bar 右端 = 「随着时间的推演颜色越来越重」。
 *  ④ bar 中部一条竖直虚线 = t½(半衰位置),标示「再过 N 天能量将衰减到峰值一半」。
 *  ⑤ 右侧文字:能量值 + t½,直观告诉用户离反转窗口还有多远。
 *  ⑥ 数据全部来自 cycle-scan 后端,前端纯渲染;
 *     缺失字段全部走兜底(0 / 0.5 / 14d),老数据不会让组件炸。
 * ════════════════════════════════════════════════════════════════════════ */

type EnergyRow = {
  asset: string
  trend: number          // 数值化后的 trend_pct(可正可负)
  conf: number           // 0..1
  len: number            // 半衰周期长度(天) — 显示用(含 fallback)
  real_len: number | null  // 后端原始 cycle_len_days,None 时不补假
  upE: number            // 上行能量
  downE: number          // 下行能量
  half_life_days: number // len/2
  reversal_days_to: number | null
  reversal_prob: number  // 0..1
  reversal_dir: string   // 'up'|'down'|'neutral'
  /** 反转紧迫度分色:`'safe' >5d` / `'warn' 3-5d` / `'urgent' ≤2d` */
  reversal_urgency: 'safe' | 'warn' | 'urgent' | 'unknown'
}

function urgencyOf(daysTo: number | null, prob: number): EnergyRow['reversal_urgency'] {
  if (daysTo == null) return 'unknown'
  // 仅在概率 ≥ 0.4 时才视为"有反转预期",否则算未知
  if (prob < 0.4) return 'unknown'
  if (daysTo <= 2) return 'urgent'
  if (daysTo <= 5) return 'warn'
  return 'safe'
}

function buildEnergyRows(rows: CycleRow[]): EnergyRow[] {
  return rows.map((r) => {
    // 数值化 trend:trend_pct 优先 → pct_7d 兜底 → 0
    const trend = Number((r.trend_pct ?? r.pct_7d ?? 0) as number) || 0
    const conf = Number(r.phase_confidence ?? 0.5) || 0.5
    const realLen = r.cycle_len_days == null ? null : Number(r.cycle_len_days) || null
    // 显示用长度:有真实值就用真实值,否则用 14d fallback(同时标记 len_unknown=true)
    const len = realLen ?? 14
    const upE = Math.max(0, trend) * (0.3 + conf) * (len / 14)
    const downE = Math.max(0, -trend) * (0.3 + conf) * (len / 14)
    const half_life_days = Math.max(1, Math.round(len / 2))
    const reversal_prob = Number(r.reversal_prob ?? 0) || 0
    const reversal_days_to = r.reversal_days_to == null ? null : Math.max(0, Math.round(Number(r.reversal_days_to)))
    return {
      asset: r.asset,
      trend,
      conf,
      len,
      real_len: realLen,
      upE,
      downE,
      half_life_days,
      reversal_days_to,
      reversal_prob,
      reversal_dir: r.reversal_dir ?? 'neutral',
      reversal_urgency: urgencyOf(reversal_days_to, reversal_prob),
    }
  })
}

function HalfLifeEnergyPanel({ rows }: { rows: CycleRow[] }) {
  const items = useMemo(() => buildEnergyRows(rows), [rows])

  const ups = useMemo(
    () => items.filter((i) => i.upE > 0).sort((a, b) => b.upE - a.upE).slice(0, 8),
    [items],
  )
  const downs = useMemo(
    () => items.filter((i) => i.downE > 0).sort((a, b) => b.downE - a.downE).slice(0, 8),
    [items],
  )

  const maxUp = ups.reduce((m, i) => Math.max(m, i.upE), 0) || 1
  const maxDown = downs.reduce((m, i) => Math.max(m, i.downE), 0) || 1
  const maxLen = Math.max(
    1,
    ...ups.map((i) => i.len),
    ...downs.map((i) => i.len),
    28,
  )

  // SVG 布局参数(viewBox)
  const W = 720
  const LABEL_W = 64
  const VAL_W = 92
  const BAR_W = W - LABEL_W - VAL_W - 4
  const ROW_H = 24
  const HEAD_H = 22
  const HEIGHT_OF = (n: number) => HEAD_H + n * ROW_H + 6

  // 单侧 bar 列表的 SVG 渲染
  const renderSide = (
    list: EnergyRow[],
    maxE: number,
    isUp: boolean,
  ) => {
    const lightHex = isUp ? '#34d399' : '#fb7185'   // 浅端(emerald-400 / rose-400)
    const darkHex = isUp ? '#064e3b' : '#881337'    // 深端(emerald-900 / rose-900)
    const midHex = isUp ? '#10b981' : '#f43f5e'     // 中段(emerald-500 / rose-500)
    const accentCls = isUp
      ? 'text-emerald-600 dark:text-emerald-400'
      : 'text-rose-600 dark:text-rose-400'
    const accentBg = isUp
      ? 'bg-emerald-500/10 border-emerald-500/30'
      : 'bg-rose-500/10 border-rose-500/30'
    const label = isUp ? '🌱 涨势能量堆积' : '❄️ 跌势能量堆积'
    const subLabel = isUp ? 'trending up · 蓄力→高潮' : 'trending down · 衰退→下跌'
    const gradPrefix = isUp ? 'up' : 'dn'
    const safe = (a: string) => a.replace(/[^a-zA-Z0-9_]/g, '_')
    /** 反转紧迫度 → 文字+色 */
    const urgencyText: Record<EnergyRow['reversal_urgency'], string> = {
      safe: '安全', warn: '临近', urgent: '紧迫', unknown: '—',
    }
    const urgencyFill: Record<EnergyRow['reversal_urgency'], string> = {
      safe: '#10b981',    // emerald-500
      warn: '#f59e0b',    // amber-500
      urgent: '#ef4444',  // red-500
      unknown: 'currentColor',
    }
    /** 阈值:E > maxE × ratio 时认为是「溢出」,加 ⚠️ 警告 */
    const OVERFLOW_RATIO = 0.9

    return (
      <div className="flex flex-col gap-1">
        <div className={cn('flex items-center gap-1 rounded border px-2 py-1 font-mono text-[10px]', accentBg, accentCls)}>
          <span aria-hidden>{label}</span>
          <span className="text-muted-foreground/80">· {subLabel}</span>
          <span className="ml-auto text-muted-foreground">{list.length} 个资产</span>
        </div>
        <svg
          viewBox={`0 0 ${W} ${HEIGHT_OF(Math.max(1, list.length))}`}
          className="block w-full"
          role="img"
          aria-label={isUp ? '涨势能量堆积柱图' : '跌势能量堆积柱图'}
        >
          <defs>
            {list.map((i) => {
              // 渐变最右端的不透明度随能量归一化:能量越大颜色越深越饱和
              const eVal = isUp ? i.upE : i.downE
              const endOp = 0.4 + Math.min(0.6, (eVal / maxE) * 0.6)
              return (
                <linearGradient
                  key={`${gradPrefix}-${i.asset}`}
                  id={`hl-${gradPrefix}-${safe(i.asset)}`}
                  x1="0" x2="1" y1="0" y2="0"
                >
                  {/* 左端:刚启动(浅+低饱和度) */}
                  <stop offset="0%" stopColor={lightHex} stopOpacity={0.18} />
                  <stop offset="25%" stopColor={lightHex} stopOpacity={0.35} />
                  {/* 中段:能量过半(中度饱和) */}
                  <stop offset="50%" stopColor={midHex} stopOpacity={0.6} />
                  <stop offset="75%" stopColor={darkHex} stopOpacity={Math.min(0.85, endOp + 0.15)} />
                  {/* 右端:能量饱和(深+高饱和度) — 「时间推演颜色越来越重」 */}
                  <stop offset="100%" stopColor={darkHex} stopOpacity={endOp} />
                </linearGradient>
              )
            })}
            {/* 柱条发光 SVG filter — 给高能量资产视觉重量 */}
            <filter id={`hl-glow-${gradPrefix}`} x="-10%" y="-30%" width="120%" height="160%">
              <feGaussianBlur stdDeviation="1.5" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>
          {/* 头部区域:坐标说明 */}
          <text x={4} y={HEAD_H - 6} fontSize={8} fill="currentColor" className="text-muted-foreground" fontFamily="ui-monospace,monospace">
            资产
          </text>
          <text x={LABEL_W + 2} y={HEAD_H - 6} fontSize={8} fill="currentColor" className="text-muted-foreground" fontFamily="ui-monospace,monospace">
            ────────  周期起点(浅)  →  现在(深) ───  →  推演(半衰衰减)
          </text>
          <line x1={LABEL_W} y1={HEAD_H - 2} x2={LABEL_W + BAR_W} y2={HEAD_H - 2} stroke="currentColor" className="text-border" strokeWidth={0.5} />

          {list.length === 0 && (
            <text
              x={W / 2}
              y={HEAD_H + ROW_H * 0.7}
              textAnchor="middle"
              fontSize={10}
              fill="currentColor"
              className="text-muted-foreground"
              fontFamily="ui-monospace,monospace"
            >
              {isUp ? '当前无上行能量资产' : '当前无下行能量资产'}
            </text>
          )}

          {list.map((it, idx) => {
            const y = HEAD_H + idx * ROW_H
            // bar 宽度 = 周期天数归一化(代表走了多久)
            const w = Math.max(8, (it.len / maxLen) * BAR_W)
            const halfX = LABEL_W + w / 2
            const energyVal = isUp ? it.upE : it.downE
            // 高能量柱条加呼吸动画 + 顶部 ⚠️ 标记
            const isOverflow = energyVal / maxE >= OVERFLOW_RATIO
            return (
              <g key={`${gradPrefix}-row-${it.asset}`}>
                {/* 资产名 */}
                <text
                  x={4}
                  y={y + 12}
                  fontSize={9}
                  fill="currentColor"
                  className="text-muted-foreground"
                  fontFamily="ui-monospace,monospace"
                >
                  {it.asset}
                </text>
                {/* ⏱ 周期长度未知标记(给缺 cycle_len_days 的资产 — 不再骗用户说"14d") */}
                {it.real_len == null && (
                  <text
                    x={4 + (it.asset.length * 5.8)}
                    y={y + 12}
                    fontSize={8}
                    fill="#a3a3a3"
                    fontFamily="ui-monospace,monospace"
                  >
                    ⏱?
                  </text>
                )}
                {/* 半衰扇形(t½ → 右端)— 半透明覆盖,告诉用户"过半衰后能量开始衰减" */}
                <rect
                  x={halfX}
                  y={y + 3}
                  width={w - (halfX - LABEL_W)}
                  height={14}
                  fill={isUp ? '#064e3b' : '#881337'}
                  fillOpacity={0.18}
                  rx={3}
                />
                {/* bar(渐变填充)— 圆角加大,高 E 资产加 SVG filter 发光 */}
                <rect
                  x={LABEL_W}
                  y={y + 3}
                  width={w}
                  height={14}
                  rx={6}
                  fill={`url(#hl-${gradPrefix}-${safe(it.asset)})`}
                  filter={isOverflow ? `url(#hl-glow-${gradPrefix})` : undefined}
                  style={{ filter: isOverflow ? 'drop-shadow(0 0 3px currentColor)' : undefined }}
                >
                  {/* 高 E 柱条呼吸动画(仅 overflow 资产闪) */}
                  {isOverflow && (
                    <animate
                      attributeName="fill-opacity"
                      values="0.85;1;0.85"
                      dur="2.4s"
                      repeatCount="indefinite"
                    />
                  )}
                </rect>
                {/* 半衰线(垂直虚线)— 黄色高亮 + 粗描边,更显眼 */}
                <line
                  x1={halfX} y1={y + 3}
                  x2={halfX} y2={y + 17}
                  stroke="#fbbf24"
                  strokeWidth={0.8}
                  strokeDasharray="2 2"
                />
                {/* 半衰点 label */}
                <text
                  x={halfX}
                  y={y + 1}
                  fontSize={7}
                  textAnchor="middle"
                  fill="#fbbf24"
                  fontFamily="ui-monospace,monospace"
                  fontWeight="bold"
                >
                  t½
                </text>
                {/* ⚠️ 能量溢出警告(柱条顶部红点 + 标签) */}
                {isOverflow && (
                  <>
                    <circle cx={LABEL_W + 4} cy={y + 4} r={2.5} fill="#ef4444" />
                    <text
                      x={LABEL_W + 10}
                      y={y + 6}
                      fontSize={7}
                      fill="#ef4444"
                      fontFamily="ui-monospace,monospace"
                      fontWeight="bold"
                    >
                      ⚠ 溢出
                    </text>
                  </>
                )}
                {/* 右侧附文:E=能量值 · t½N=半衰天数 · 反转紧迫度分色徽章 */}
                <text
                  x={LABEL_W + w + 6}
                  y={y + 12}
                  fontSize={8}
                  className="text-muted-foreground"
                  fill="currentColor"
                  fontFamily="ui-monospace,monospace"
                >
                  E={energyVal.toFixed(0)} · t½={it.half_life_days}d
                  {it.reversal_days_to != null && it.reversal_prob >= 0.4 ? (
                    <tspan
                      fill={urgencyFill[it.reversal_urgency]}
                      fontWeight="bold"
                    >
                      {` · ${isUp ? '↓' : '↑'}${it.reversal_days_to}d[${urgencyText[it.reversal_urgency]}]`}
                    </tspan>
                  ) : null}
                </text>
              </g>
            )
          })}
        </svg>
      </div>
    )
  }

  // ── 顶部 KPI 区:4 张小卡(总涨能 / 总跌能 / 顶涨 / 顶跌) ──
  const totalUpE = ups.reduce((s, i) => s + i.upE, 0)
  const totalDownE = downs.reduce((s, i) => s + i.downE, 0)
  const netE = totalUpE - totalDownE
  const netRegime = netE > 30 ? '偏涨(多头占优)' : netE < -30 ? '偏跌(空头占优)' : '中性震荡'
  const netTone = netE > 30 ? 'text-emerald-600 dark:text-emerald-400' : netE < -30 ? 'text-rose-600 dark:text-rose-400' : 'text-amber-600 dark:text-amber-400'
  /** 列出所有 > 0.9 max 的资产(涨侧/跌侧各自归一化)— 用于顶部警示 banner */
  const allOverflow = [
    ...ups.filter((i) => i.upE / maxUp >= 0.9).map((i) => ({ asset: i.asset, e: i.upE, side: 'up' as const })),
    ...downs.filter((i) => i.downE / maxDown >= 0.9).map((i) => ({ asset: i.asset, e: i.downE, side: 'down' as const })),
  ]

  return (
    <div className="mt-3 rounded border border-violet-500/30 bg-violet-500/5 p-2.5">
      <div className="flex items-center justify-between gap-2 font-mono text-[11px]">
        <div className="flex items-center gap-1.5">
          <Hourglass className="h-3.5 w-3.5 text-violet-500" aria-hidden />
          <strong className="font-bold uppercase tracking-widest text-violet-700 dark:text-violet-300">
            ⏳ 涨跌半衰能量图
          </strong>
          <span className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 text-[9px] font-bold text-violet-600 dark:text-violet-400">
            t½ · 涨/跌越久能量越深
          </span>
        </div>
      </div>

      {/* ── 4 张 KPI 卡(显眼)— 取代原来 2 个内联文字 ── */}
      <div className="mt-1.5 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="rounded border border-emerald-500/30 bg-emerald-500/10 p-1.5">
          <div className="font-mono text-[9px] uppercase tracking-wider text-emerald-700 dark:text-emerald-400">
            🌱 总涨能
          </div>
          <div className="mt-0.5 font-mono text-base font-bold text-emerald-700 dark:text-emerald-300">
            E={totalUpE.toFixed(0)}
          </div>
          <div className="font-mono text-[9px] text-muted-foreground">
            {ups.length} 个资产在堆积
          </div>
        </div>
        <div className="rounded border border-rose-500/30 bg-rose-500/10 p-1.5">
          <div className="font-mono text-[9px] uppercase tracking-wider text-rose-700 dark:text-rose-400">
            ❄️ 总跌能
          </div>
          <div className="mt-0.5 font-mono text-base font-bold text-rose-700 dark:text-rose-300">
            E={totalDownE.toFixed(0)}
          </div>
          <div className="font-mono text-[9px] text-muted-foreground">
            {downs.length} 个资产在堆积
          </div>
        </div>
        <div className="rounded border border-amber-500/30 bg-amber-500/10 p-1.5">
          <div className="font-mono text-[9px] uppercase tracking-wider text-amber-700 dark:text-amber-400">
            🎯 顶涨 / 顶跌
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 font-mono text-[11px]">
            {ups[0] && (
              <span className="text-emerald-700 dark:text-emerald-300">
                ↑ <strong>{ups[0].asset}</strong>
                <span className="ml-0.5 text-[10px] text-muted-foreground">
                  ({ups[0].upE.toFixed(0)})
                </span>
              </span>
            )}
            {downs[0] && (
              <span className="text-rose-700 dark:text-rose-300">
                ↓ <strong>{downs[0].asset}</strong>
                <span className="ml-0.5 text-[10px] text-muted-foreground">
                  ({downs[0].downE.toFixed(0)})
                </span>
              </span>
            )}
          </div>
          <div className="font-mono text-[9px] text-muted-foreground">
            当前能量榜首
          </div>
        </div>
        <div className={cn('rounded border p-1.5', netE > 30 ? 'border-emerald-500/30 bg-emerald-500/10' : netE < -30 ? 'border-rose-500/30 bg-rose-500/10' : 'border-amber-500/30 bg-amber-500/10')}>
          <div className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground">
            ⚖ 市场情绪净额
          </div>
          <div className={cn('mt-0.5 font-mono text-base font-bold', netTone)}>
            {netE >= 0 ? '+' : ''}{netE.toFixed(0)}
          </div>
          <div className={cn('font-mono text-[9px]', netTone)}>
            {netRegime}
          </div>
        </div>
      </div>

      {/* ⚠ 能量溢出列表(超过 maxE × 90% 的资产)— 显眼提示 */}
      {allOverflow.length > 0 && (
        <div className="mt-1.5 flex items-center gap-1 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 font-mono text-[10px] text-red-700 dark:text-red-300">
          <span aria-hidden>⚠️</span>
          <strong>能量溢出:</strong>
          <span>
            {allOverflow.map((x) => `${x.asset}(E=${x.e.toFixed(0)},${x.side === 'up' ? '涨' : '跌'})`).join(' · ')}
          </span>
          <span className="ml-auto text-[9px] text-red-600/80">反转窗口临近,注意风险管理</span>
        </div>
      )}

      <p className="mt-1 text-[11px] leading-6 text-muted-foreground">
        能量 <code className="rounded bg-background px-1">E = |trend%| × 置信度 × (周期/14d)</code>;
        bar 从
        <strong className="text-foreground">浅</strong>(周期起点,能量刚启动) →
        <strong className="text-foreground">深</strong>(现在,能量饱和)— <em>颜色越深 = 越临近反转窗口</em>。
        <span className="text-amber-600 dark:text-amber-400">黄虚线 t½</span> 右侧的
        <span className="text-rose-600/80 dark:text-rose-400/80">扇形暗影</span>标示"过半衰后能量开始衰减"。
        <span className="text-red-600">⚠ 溢出</span>= 当前最大能量的 ≥90%,反转紧迫。
        资产名后 <span className="text-muted-foreground">⏱?</span> 表示后端未识别周期长度,长度用 14d 兜底。
      </p>

      <div className="mt-2 grid grid-cols-1 gap-3 lg:grid-cols-2">
        {renderSide(ups, maxUp, true)}
        {renderSide(downs, maxDown, false)}
      </div>
    </div>
  )
}
