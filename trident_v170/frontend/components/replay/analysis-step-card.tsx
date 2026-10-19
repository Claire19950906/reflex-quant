// v170 P0+：analysis-steps 卡片拆分
//
// 从 analysis-steps.tsx 抽出 4 个展示型组件 + 解析/分类纯函数：
//   - AssetTag          品种标签（主品种/副品种 + 切换）
//   - parseFactorRank   「因素定位」段 JSON 数组解析
//   - StageCard         单段展示（含 因素定位 ranked list 渲染）
//   - SummaryCard       📋 总结（含下单信息）
//
// 主组件（AnalysisSteps）保留在 analysis-steps.tsx。

import { assetLabelOf } from './analysis-parse'
import type { StageItem } from './analysis-parse'

/** 阶段色调 — 不同段用不同 Tailwind 边框/背景，区分主/副品种段 */
export const STAGE_TONES: Record<string, string> = {
  '总框架': 'border-sky-500/40 bg-sky-500/5',
  '因素定位': 'border-amber-500/40 bg-amber-500/5',
  '驱动力': 'border-primary/30 bg-primary/5',
  '水位博弈': 'border-primary/30 bg-primary/5',
  '跨资产资金': 'border-cyan-500/40 bg-cyan-500/5',
  '反共识结论': 'border-long/40 bg-long/5',
  '逻辑自检': 'border-hold/40 bg-hold/5',
  '总结': 'border-primary/40 bg-primary/5',
}

// ─── AssetTag ────────────────────────────────────────────────────────

/** 品种标签 — 主品种 / 副品种切换；点空品种标签有「无内容」提示 */
export function AssetTag({ asset, isMain, active, stageCount, onClick }: {
  asset: string
  isMain: boolean
  active: boolean
  /** 该品种有自己的专属段数量；0 = AI 未逐品种拆分（标签下标灰点提示） */
  stageCount: number
  onClick: () => void
}) {
  const name = assetLabelOf(asset)
  return (
    <button
      type="button"
      // 阻止冒泡：本组件会被放进"整卡可点折叠"的新闻卡里（event-bus 的 li onClick
      // 切换展开）。不阻止的话点标签会冒泡到卡片 → 整卡折叠，表现为
      // "点副品种弹不出来，反而把主品种缩回去了"。
      onClick={(e) => {
        e.stopPropagation()
        onClick()
      }}
      className={`rounded border px-2 py-0.5 font-mono text-[10px] font-bold tracking-wider transition-colors ${
        active
          ? isMain
            ? 'border-long/60 bg-long/15 text-long'
            : 'border-indigo-500/60 bg-indigo-500/15 text-indigo-300'
          : 'border-border text-muted-foreground hover:bg-secondary'
      }`}
      title={`${isMain ? '主品种' : '副品种'}${stageCount > 0 ? ` · ${stageCount} 段专属推演` : ' · AI 未单独输出该品种内容'}`}
    >
      <span className="opacity-70">{isMain ? '主' : '副'}</span> {asset}
      {name !== asset && <span className="ml-1 font-normal opacity-80">{name}</span>}
      {/* 明确标出该品种有无专属内容：空标签加虚线边框，避免"点了没反应"的错觉 */}
      {stageCount === 0 && <span className="ml-1 font-normal opacity-60" title="AI 未单独输出该品种的六段内容">无内容</span>}
    </button>
  )
}

// ─── parseFactorRank + 方向 tone ─────────────────────────────────────

/** 「因素定位」段内容通常是 JSON 数组（[{"factor":…,"rank":…,"direction":…}]）。
 *  能解析成功就渲染成分级列表；否则原样展示（旧数据 / 模型自由发挥的兜底）。
 */
export function parseFactorRank(text: string): Array<{ factor: string; rank?: number; direction?: string }> | null {
  const s = (text || '').trim()
  if (!s) return null
  try {
    const arr = JSON.parse(s)
    if (!Array.isArray(arr) || arr.length === 0) return null
    if (!arr.every((x) => x && typeof x === 'object' && typeof x.factor === 'string' && x.factor.trim())) return null
    return arr.map((x) => ({
      factor: String(x.factor),
      rank: typeof x.rank === 'number' ? x.rank : undefined,
      direction: x.direction ? String(x.direction) : undefined,
    }))
  } catch {
    return null
  }
}

// 紧跟品种的正向动词（"XAU 推动"、"黄金上涨"6 字内）→ 视为正向讲该品种
// 导出供 analysis-steps.tsx 在「该段未标注品种时归主品种」判断里使用
export const _POSITIVE_NEAR_RE = [
  /XAU.{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
  /(黄金|金价).{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
  /WTI.{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
  /(原油|油价).{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
  /BTC.{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
  /(比特币|大饼).{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
  /ETH.{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
  /(以太坊).{0,6}(推动|上涨|下行|利好|利空|避险|驱动|走强|走弱|承压|支撑|拖累|联动|主导)/,
]
// 否定提及模式："无直接传导"、"与X无关"、"不涉及" → 不算正向
// 导出供 analysis-steps.tsx 在「该段未标注品种时归主品种」判断里使用
export const _NEGATION_RE = [
  /无.{0,6}传导/,
  /与.{0,10}无关/,
  /无.{0,6}直接影响/,
  /不涉及/,
]

/** direction 字段 → Tailwind 文本色类（正红/负绿） */
export function dirTone(dir: string): string {
  const d = (dir || '').trim()
  if (/利多|看涨|bull|增持/i.test(d)) return 'font-mono text-[9px] text-long'
  if (/利空|看跌|bear|减持/i.test(d)) return 'font-mono text-[9px] text-short'
  return 'font-mono text-[9px] text-muted-foreground'
}

// ─── StageCard ───────────────────────────────────────────────────────

export function StageCard({ stageKey, items, reason }: {
  stageKey: string
  items: StageItem[]
  /** 本段为什么没有内容（空段时展示，避免只给一个空框让用户猜） */
  reason?: string
}) {
  // STAGE_HINTS 仍在 analysis-parse.ts，这里通过 stageKey 渲染；tone 用本地 STAGE_TONES。
  const tone = STAGE_TONES[stageKey] ?? 'border-border bg-secondary/30'
  return (
    <div className={`rounded border px-2.5 py-2 ${tone}`}>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-foreground">{stageKey}</span>
      </div>
      {items.length > 0 ? (
        <div className="mt-1 space-y-1">
          {items.map((it, i) => {
            const factors = stageKey === '因素定位' ? parseFactorRank(it.text) : null
            return factors ? (
              <div key={`${stageKey}-${i}`}>
                {it.label && (
                  <span className="mr-1 font-mono text-[10px] font-bold text-muted-foreground/80">{it.label}：</span>
                )}
                <ol className="mt-0.5 space-y-0.5">
                  {factors.map((f, j) => (
                    <li key={j} className="flex items-baseline gap-1.5 text-[11px] leading-relaxed">
                      <span className="shrink-0 font-mono text-[9px] text-muted-foreground/50">#{f.rank ?? j + 1}</span>
                      <span className="break-words text-foreground/85">{f.factor}</span>
                      {f.direction && <span className={`shrink-0 ${dirTone(f.direction)}`}>{f.direction}</span>}
                    </li>
                  ))}
                </ol>
              </div>
            ) : (
              <p key={`${stageKey}-${i}`} className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-foreground/85">
                {it.label && (
                  <span className="mr-1 font-bold text-muted-foreground/80">{it.label}：</span>
                )}
                {it.text}
              </p>
            )
          })}
        </div>
      ) : (
        <p className="mt-1 font-mono text-[10px] leading-relaxed text-muted-foreground/70">
          <span className="italic opacity-70">（本段无内容）</span>
          {reason && <span className="ml-1">{reason}</span>}
        </p>
      )}
    </div>
  )
}

// ─── SummaryCard ─────────────────────────────────────────────────────

/** AI 总结 — 6 段推理收口后的最终结论（方向/分数/结论/关键维度） */
export type AnalysisSummary = {
  /** 该总结对应的品种代码（副品种标签用；主品种可省略，由 mainAsset 提供） */
  asset?: string
  action?: string
  score?: number | null
  reason?: string
  horizon?: string
  eventStrength?: string
  directCatalyst?: boolean | number
  marketConfirmation?: string
  heatScore?: number | null
  impactScore?: number | null
  assetImpact?: number | null
  /** 需求5 新增子模块输出：关注度得分(0-100) / 弹性窗 / 原始数据戳 / 硬数据截图 */
  attentionScore?: number | null
  elasticityWindow?: string
  dataAnchorTs?: string
  hardDataEvidence?: string
  invalidation?: string
  /** 下单信息：有值表示已建仓，在总结下方展示 */
  entryPrice?: number | null
  noTradeReason?: string
  gateReason?: string
  /** true = 同新闻的副品种（用于标签区分主/副） */
  isSibling?: boolean
}

/** Horizon/Strength/Confirmation 字段 → UI 中文标签 */
const HORIZON_LABELS: Record<string, string> = {
  intraday: '即时影响', '1-3d': '短期趋势(1-3天)', '1w+': '趋势(1周+)',
}
const STRENGTH_LABELS: Record<string, string> = { low: '弱催化', medium: '中等', high: '强催化' }
const CONFIRM_LABELS: Record<string, string> = { positive: '盘面确认', negative: '盘面背离', unknown: '未确认' }

function fmtScore(score?: number | null): string {
  if (score === null || score === undefined || Number.isNaN(score)) return '—'
  return `${score > 0 ? '+' : ''}${score.toFixed(2)}`
}

/** 弹性窗参数（JSON 字符串）→ 一行可读摘要；解析失败/异常时原样返回 */
function fmtElasticity(raw?: string): string {
  if (!raw) return ''
  try {
    const o = typeof raw === 'string' ? JSON.parse(raw) : raw
    if (Array.isArray(o)) return `${o.length} 档弹性窗`
    if (o && typeof o === 'object') {
      const parts: string[] = []
      if (o.minutes != null) parts.push(`${o.minutes}min`)
      if (o.peak_after_minutes != null) parts.push(`峰值 ${o.peak_after_minutes}min`)
      if (o.decay != null) parts.push(`衰减 ${o.decay}`)
      if (parts.length) return parts.join(' · ')
    }
  } catch {
    // 非法 JSON → 原样展示
  }
  return raw
}

// ─── SummaryCard ─────────────────────────────────────────────────────

/** 📋 总结卡 — 动作 / 分数 / 入场价 / 失效条件 / 多维度标签 */
export function SummaryCard({ asset, isMain, summary, showHeader }: {
  asset: string
  isMain: boolean
  summary?: AnalysisSummary
  showHeader: boolean
}) {
  if (!summary) return null
  const action = (summary.action || '').toUpperCase()
  const actionTone = action === 'BUY' ? 'text-long' : action === 'SELL' ? 'text-short' : action === 'HOLD' ? 'text-hold' : ''
  const traded = summary.entryPrice != null && Number(summary.entryPrice) > 0
  const entry = summary.entryPrice != null ? Number(summary.entryPrice) : null
  return (
    <div className="rounded border border-primary/40 bg-primary/8 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-primary">📋 总结</span>
        {showHeader && (
          <span className="font-mono text-[10px] font-bold text-foreground/80">
            {asset}{assetLabelOf(asset) !== asset ? ` ${assetLabelOf(asset)}` : ''}
          </span>
        )}
        {action && <span className={`font-mono text-xs font-bold tracking-wider ${actionTone}`}>{action}</span>}
        <span className={`font-mono text-xs font-bold tabular-nums ${(summary.score ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>
          {fmtScore(summary.score)}
        </span>
      </div>
      {summary.reason && (
        <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-foreground/85">
          {summary.reason}
        </p>
      )}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px]">
        {traded ? (
          <span className="rounded border border-long/40 bg-long/10 px-1.5 py-0.5 text-long">
            已建仓 · 入场 {entry?.toFixed(2)}
          </span>
        ) : (
          <div className="rounded border border-border bg-secondary/40 px-1.5 py-0.5 text-muted-foreground">
            <span className="font-bold">未建仓 · </span>
            <span className="font-mono text-[10px] text-muted-foreground">
              {summary.noTradeReason || summary.gateReason || '未建仓（原因未记录）'}
            </span>
          </div>
        )}
      </div>
      {action === 'HOLD' && (
        <div className="mt-1.5 rounded border border-border bg-secondary/40 px-2 py-1">
          <span className="font-mono text-[10px] font-bold tracking-wider text-muted-foreground">观望不下单</span>
          {summary.noTradeReason && (
            <span className="ml-2 font-mono text-[10px] text-muted-foreground/80">{summary.noTradeReason}</span>
          )}
        </div>
      )}

      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[9px] text-muted-foreground">
        {summary.horizon && <span>⏱ {HORIZON_LABELS[summary.horizon] ?? summary.horizon}</span>}
        {summary.eventStrength && <span>💥 {STRENGTH_LABELS[summary.eventStrength] ?? summary.eventStrength}</span>}
        {summary.marketConfirmation && <span>🎯 {CONFIRM_LABELS[summary.marketConfirmation] ?? summary.marketConfirmation}</span>}
        {summary.directCatalyst ? <span>⚡ 直接催化</span> : null}
        {summary.heatScore != null && summary.heatScore > 0 && <span>🔥 关注度 {Math.round(summary.heatScore)}</span>}
        {summary.impactScore != null && summary.impactScore > 0 && <span>🌊 影响力 {Math.round(summary.impactScore)}</span>}
        {summary.assetImpact != null && summary.assetImpact > 0 && <span>🎯 资产影响 {Math.round(summary.assetImpact)}</span>}
        {summary.attentionScore != null && <span>⭐ 关注度得分 {Math.round(summary.attentionScore)}/100</span>}
        {summary.dataAnchorTs && <span>🕓 数据锚 {summary.dataAnchorTs}</span>}
      </div>
      {summary.elasticityWindow && (
        <div className="mt-1.5 rounded border border-border bg-secondary/40 px-2 py-1">
          <span className="font-mono text-[9px] font-bold uppercase tracking-wider text-muted-foreground">弹性窗参数</span>
          <span className="ml-2 font-mono text-[10px] text-foreground/85">{fmtElasticity(summary.elasticityWindow)}</span>
        </div>
      )}
      {summary.hardDataEvidence && (
        <div className="mt-1.5 rounded border border-border bg-secondary/40 px-2 py-1">
          <span className="font-mono text-[9px] font-bold uppercase tracking-wider text-muted-foreground">硬数据截图</span>
          <pre className="mt-0.5 whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed text-foreground/85">{summary.hardDataEvidence}</pre>
        </div>
      )}
      {summary.invalidation && (
        <p className="mt-1.5 border-t border-border/40 pt-1 text-[10px] leading-relaxed text-short/90">
          失效条件：{summary.invalidation}
        </p>
      )}
    </div>
  )
}