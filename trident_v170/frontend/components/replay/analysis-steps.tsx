'use client'

import { useMemo, useState } from 'react'
import {
  ASSET_NAMES,
  assetLabelOf,
  buildAssetTabs,
  buildPerAsset,
  detectAssets,
  detectMentionedAssets,
  normAsset,
  parseAnalysisSteps,
  FIXED_STAGE_KEYS,
  SHARED_STAGE_KEYS,
  type AssetTab,
  type StageItem,
} from './analysis-parse'
import {
  AssetTag,
  StageCard,
  SummaryCard,
  _NEGATION_RE,
  _POSITIVE_NEAR_RE,
  type AnalysisSummary,
} from './analysis-step-card'

// 多品种六段式展示 — 按品种重组（每个品种一个完整区块）。
// 解析逻辑见 ./analysis-parse.ts（纯函数，可离线测试）。
//
// 布局：顶部品种标签切换（主/副）→ 该品种专属四段 → 全品种共享段（跨资产资金/逻辑自检）
//       → 该品种总结（含下单信息，有建仓显示入场价，否则说明原因）
//
// 兼容：单品种新闻退化为主品种一个标签；模型未分段时原样展示（不丢信息）。

export type { AnalysisStep, AssetTab } from './analysis-parse'
export type { AnalysisSummary } from './analysis-step-card'

// 检测 reasoning_path 是否是「服务端兜底 / 摘要补全」而非真实 AI 推理。
// v200 P3: 兜底横幅文案修订 — 不再误导用户「无真实 AI 分析」
// 实际 LLM 给了真东西（reasoning 45-80 字符），仅未展开成完整八段。
// 改为中性表述：标明是「精简版」/「扩展版」让用户知道内容来源。
const _FALLBACK_PATTERNS: { re: RegExp; kind: 'summary' | 'none' | 'graph' | 'full'; label: string }[] = [
  // v200 P3: LLM 真东西摘要 — 是 AI 真输出，仅未展开八段
  { re: /LLM 真东西摘要/, kind: 'summary', label: '✓ LLM 真东西：LLM 给了真实判断摘要，服务端按 action/方向铺成八段骨架' },
  // v176.3：旧"LLM 输出了简短摘要，未展开八段推演"（仅历史记录命中，新输出已不再使用）
  { re: /LLM 输出了简短摘要，未展开八段推演/, kind: 'summary', label: 'LLM 给了短摘要，服务端补全为八段骨架' },
  // 因果图谱注入但 LLM 未应用
  { re: /因果图谱已注入（但 LLM 未应用）|未注入因果图谱/, kind: 'graph', label: '因果图谱已注入，但 LLM 没用上（已替换为占位说明）' },
  // 真兜底：LLM 完全没给 reasoning 时
  { re: /LLM 未填/, kind: 'full', label: 'LLM 本轮未输出 reasoning 字段（数据缺，分析维度不足）' },
]

function detectFallbackReasoning(reasoningPath: string): { kind: string; label: string } | null {
  if (!reasoningPath) return null
  for (const p of _FALLBACK_PATTERNS) {
    if (p.re.test(reasoningPath)) return { kind: p.kind, label: p.label }
  }
  return null
}

export function AnalysisSteps({ reasoningPath, summary, siblingSummaries, maxHeight, mainAsset, fullReasoningOnMain = false }: {
  reasoningPath: string
  summary?: AnalysisSummary
  /** 副品种结论（含下单状态）——每个品种一个标签页，各自独立六段+总结+下单 */
  siblingSummaries?: AnalysisSummary[]
  maxHeight?: string
  /** 主品种代码（用于标签排序与主/副区分） */
  mainAsset?: string
  /** true = 本卡是同新闻的副品种卡：完整推演已在主卡展示，这里只给自己的总结 */
  fullReasoningOnMain?: boolean
}) {
  const steps = useMemo(() => parseAnalysisSteps(reasoningPath), [reasoningPath])
  const fallback = useMemo(() => detectFallbackReasoning(reasoningPath), [reasoningPath])
  const contentByKey = useMemo(() => {
    const map = new Map<string, string>()
    for (const s of steps) map.set(s.key, s.content)
    return map
  }, [steps])

  // 品种标签：主品种固定第一，随后是各副品种（来自 siblingSummaries），
  // 最后补 reasoning_path 里检测到但未列出结论的品种。
  const tabs = useMemo(() => {
    const main = normAsset(mainAsset ?? '') || detectAssets(reasoningPath)[0] || ''
    const sibAssets = (siblingSummaries ?? []).map((s) => s.asset || '')
    return buildAssetTabs(main, sibAssets, reasoningPath)
  }, [mainAsset, siblingSummaries, reasoningPath])

  // 按品种拆段：前四段按"品种："前缀拆分；未标注文字单独留存作兜底
  const perAsset = useMemo(() => buildPerAsset(contentByKey), [contentByKey])

  const resolvedMain = tabs[0]?.asset ?? normAsset(mainAsset ?? '') ?? ''
  const [activeAsset, setActiveAsset] = useState<string>('')
  const current = activeAsset || resolvedMain
  const currentStages = perAsset.map.get(current)
  const hasStages = steps.length > 0

  // 总结按品种取：主品种用 summary，副品种用 siblingSummaries 里对应项。
  // 切到某个品种标签 → 总结与下单信息都跟着切成该品种的。
  const summaryByAsset = useMemo(() => {
    const map = new Map<string, AnalysisSummary>()
    const mainKey = normAsset(mainAsset ?? '') || resolvedMain
    if (summary && mainKey) map.set(mainKey, summary)
    for (const s of siblingSummaries ?? []) {
      const a = normAsset(s.asset ?? '')
      if (a) map.set(a, s)
    }
    return map
  }, [summary, siblingSummaries, mainAsset, resolvedMain])

  const currentSummary = summaryByAsset.get(current)
    // 该品种没有独立结论（如仅在文本里被提及）→ 回落到主总结，避免空白
    ?? (current === resolvedMain ? summary : undefined)
  const currentIsMain = current === resolvedMain

  if (!hasStages && !summary && !(siblingSummaries?.length)) {
    return reasoningPath.trim() ? (
      <div className="rounded border border-border bg-secondary/20 px-2.5 py-2">
        <div className="font-mono text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
          推理过程（模型未按六段分段，原样展示）
        </div>
        <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-foreground/85">
          {reasoningPath}
        </p>
      </div>
    ) : null
  }

  // 副品种卡：完整推演已在主卡，这里只给自己的总结 + 下单
  if (fullReasoningOnMain && currentSummary) {
    return (
      <div className="space-y-1.5">
        <SummaryCard asset={current || '—'} isMain={false} summary={currentSummary} showHeader />
        <p className="px-1 font-mono text-[9px] text-muted-foreground/60">
          六段完整推演见本新闻主品种卡片（同一份分析，避免重复展示）
        </p>
      </div>
    )
  }

  // 无标签内容（模型没按品种前缀分写时的通用段落）—— 这些内容此前没有归属，
  // 导致主品种六段大片空白（实测最近 120 条里 260 个段无标签，比带标签的还多）。
  const unlabeledContent = useMemo(
    () => [...perAsset.unlabeled.values()].filter(Boolean).join('\n'),
    [perAsset],
  )

  // 无标签内容能否安全归给主品种？
  // 按【段】判断（不是整篇一起判断）：某段的无标签文字若不涉及其它品种，就归主品种。
  // 此前用整篇判断 → 只要别处提过其它品种就全否 → 主品种六段大片空白
  // （用户实测："主NONE 无内容"。主品种是新闻主体，它的分析必须优先展示到位）。
  //
  // ★ 这里必须用【本段】检测到的其它品种，不能用整篇的 tabs：
  //   tabs 含"整篇检测到"的品种，于是某段明明只讲主品种，只要同一新闻的**别的段**
  //   提过副品种，该段就会被挡住 → 假"无内容"（实测 34.8% 的主品种段属于这种误挡）。
  // 正/负判定正则在文件顶部（_POSITIVE_NEAR_RE / _NEGATION_RE，模块级常量）
  const unlabeledOwnsMain = (stageKey: string): boolean => {
    const un = perAsset.unlabeled.get(stageKey)
    if (!un) return false
    const m = (resolvedMain || '').toUpperCase()
    // v176.9 fix：主品种是 NONE/OTHER 时也兜底（之前会全段"本段无内容"）。
    // 三层判断：
    //   ① 否定提及（如"与XAU/WTI无直接传导链"）→ 归 NONE（主品种就是 NONE）
    //   ② 正向动词紧跟其它品种（"黄金推动价格上涨"）→ 不归 NONE（防串味）
    //   ③ 其它（中性提及 / 仅枚举 / 无品种）→ 归 NONE（兜底）
    // v212 fix: 'OTHER' 已从 MarketCategory 删除。主品种取值只可能是 NONE 或某资产名；
    // 'AGGREGATE' 是 MarketCategory 不是 target_asset，不会出现在主品种位置。
    if (!m || m === 'NONE') {
      const hasNegation = _NEGATION_RE.some((re) => re.test(un))
      if (hasNegation) return true
      const hasPositiveNear = _POSITIVE_NEAR_RE.some((re) => re.test(un))
      if (hasPositiveNear) return false
      return true
    }
    const mentioned = new Set([...detectAssets(un), ...detectMentionedAssets(un)])
    const others = [...mentioned].filter((a) => a !== resolvedMain)
    return others.length === 0
  }

  // 取该品种本段的内容；主品种在该段没有专属行时，用"该段未标注但只涉主品种"的文字兜底
  // （副品种不做这个兜底 —— 它的内容必须来自它自己的行，避免混入主品种分析）。
  const stageItemsFor = (key: string): StageItem[] => {
    const own = currentStages?.get(key)
    if (own && own.length) return own
    if (currentIsMain && unlabeledOwnsMain(key)) {
      const un = perAsset.unlabeled.get(key)
      return un ? [{ label: '', text: un }] : []
    }
    return []
  }

  // 本段为什么没有内容 —— 空框必须给出原因，否则用户只能猜"是抓不到数据还是模型没写"。
  // 四类真实成因（实测最近 120 条归因，占比见 scripts/verify_empty_reason.mjs）：
  //   ① 该段整段为空            → 模型没写这一段
  //   ② 该段把多品种写在同一句  → 提到了该品种，但没给它的独立判断（不敢归属，怕串味）
  //   ③ 该段只在讲别的品种      → 模型这一段没分析这个品种
  //   ④ 该段没有品种词，但只给了主品种 → 副品种拿不到（副品种必须有自己的独立行）
  const emptyReasonFor = (key: string): string | undefined => {
    if (stageItemsFor(key).length > 0) return undefined
    const raw = (perAsset.raw.get(key) ?? '').trim()
    if (!raw) return '模型本次没有输出这一段。'
    const mentioned = new Set([...detectAssets(raw), ...detectMentionedAssets(raw)])
    const others = [...mentioned].filter((a) => a !== current)
    // v212 fix: 主品种取值只可能是 NONE 或某资产名；'OTHER'/'AGGREGATE' 是 MarketCategory，不是 target_asset。
    const noMainAsset = ['', 'NONE'].includes((current || '').toUpperCase())
    if (mentioned.has(current) && others.length > 0) {
      return `这一段把 ${current} 和 ${others.join('、')} 写在同一句里，没有给出 ${current} 自己的独立判断。`
    }
    if (others.length > 0) {
      return noMainAsset
        ? `模型这一段只分析了 ${others.join('、')}；本次没有判定出可交易的主品种，故不归到任何品种名下。`
        : `模型这一段只分析了 ${others.join('、')}，没有针对 ${current} 的内容。`
    }
    if (currentIsMain) {
      return '模型这一段没有写任何品种，内容无法确认是在讲主品种。'
    }
    // 副品种：该段没有品种词 → 内容会归给主品种，不会分给副品种（避免串味）
    return `模型这一段没有单独写 ${current}，只写了主品种 ${resolvedMain} 的分析；`
      + `副品种必须有自己的独立行，不挪用主品种内容。`
  }

  return (
    <div className="space-y-1.5" style={maxHeight ? { maxHeight, overflowY: 'auto' } : undefined}>
      {tabs.length > 1 && (
        // 容器级兜底：组件会被放进"整卡可点折叠"的新闻卡里，标签区任何点击都不该冒泡。
        // AssetTag 自身已 stopPropagation，这里再拦一层，避免以后新增元素又踩同一个坑。
        <div
          className="flex flex-wrap items-center gap-1 rounded border border-border bg-secondary/30 px-2 py-1"
          onClick={(e) => e.stopPropagation()}
        >
          <span className="mr-1 font-mono text-[9px] uppercase tracking-wider text-muted-foreground/70">
            按品种查看
          </span>
          {tabs.map((t) => (
            <AssetTag
              key={t.asset}
              asset={t.asset}
              isMain={t.isMain}
              active={t.asset === current}
              stageCount={perAsset.map.get(t.asset)?.size ?? 0}
              onClick={() => setActiveAsset(t.asset)}
            />
          ))}
        </div>
      )}

      {/* v200 P3: 兜底横幅 — 改为信息提示样式，不再误导为"严重警告"
          仅在 LLM 完全未输出时显示 amber 警告；LLM 给了真东西时显示绿色提示 */}
      {fallback && (
        <div
          className={`rounded border px-2.5 py-1.5 text-[10px] leading-relaxed ${
            fallback.kind === 'full'
              ? 'border-amber-500/40 bg-amber-500/10 text-amber-200/95'
              : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200/95'
          }`}
          title={fallback.kind === 'full'
            ? 'LLM 本轮未给出 reasoning，需关注数据通道是否完整'
            : 'LLM 给出了真实推理（精简摘要），下方八段为服务端按 action/方向铺的骨架'}
        >
          <span className={`font-mono font-bold uppercase tracking-wider ${
            fallback.kind === 'full' ? 'text-amber-400/95' : 'text-emerald-400/95'
          }`}>
            {fallback.kind === 'full' ? '⚠ 数据缺' : '✓ LLM 真实分析'}
          </span>
          <span className="ml-2">{fallback.label}</span>
        </div>
      )}

      {hasStages ? (
        <>
          {(currentStages?.size ?? 0) === 0 && !(currentIsMain && unlabeledContent) ? (
            /* 该品种 0 段专属内容且无可用兜底：不再铺空段卡片（看起来像页面坏了），
               改成一条明确说明；结论与下单情况见下方总结。 */
            <div className="rounded border border-dashed border-border bg-secondary/20 px-2.5 py-2 text-[10px] leading-relaxed text-muted-foreground/85">
              <div className="font-mono font-bold text-muted-foreground">
                {current}
                {assetLabelOf(current) !== current ? ` ${assetLabelOf(current)}` : ''} 本次没有逐段分析
              </div>
              <div className="mt-0.5">
                {`AI 未为该品种单独分写六段${tabs.length > 1 ? '（它只在其它段里被一句提及）' : ''}。`}
                该品种的结论与下单情况见下方总结。
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 px-1 pt-0.5">
                <span className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground/70">
                  {current}{assetLabelOf(current) !== current ? ` ${assetLabelOf(current)}` : ''} 六段推演
                </span>
                <span className="h-px flex-1 bg-border" />
              </div>
              {/* 主品种无逐品种标注时：本段未标注内容若只涉主品种 → 归它展示（尽力让主品种到位） */}
              {currentIsMain && (currentStages?.size ?? 0) === 0 && unlabeledContent && (
                <div className="rounded border border-dashed border-border bg-secondary/20 px-2.5 py-1.5 font-mono text-[10px] leading-relaxed text-muted-foreground/80">
                  AI 未按品种分写六段，以下为该品种对应的分析内容（未标注品种但已确认不含其它品种）。
                </div>
              )}
              {/* 总结段由下方 SummaryCard（📋 总结）统一呈现，避免同一结论重复显示两次 */}
              {FIXED_STAGE_KEYS.filter((key) => key !== '总结').map((key) => (
                <StageCard
                  key={key}
                  stageKey={key}
                  items={stageItemsFor(key)}
                  reason={emptyReasonFor(key)}
                />
              ))}
            </>
          )}

        </>
      ) : reasoningPath.trim() ? (
        <div className="rounded border border-border bg-secondary/20 px-2.5 py-2">
          <div className="font-mono text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            推理过程（模型未按六段分段，原样展示）
          </div>
          <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-foreground/85">
            {reasoningPath}
          </p>
        </div>
      ) : null}

      <SummaryCard
        asset={current}
        isMain={currentIsMain}
        summary={currentSummary}
        showHeader={tabs.length > 1}
      />
    </div>
  )
}
