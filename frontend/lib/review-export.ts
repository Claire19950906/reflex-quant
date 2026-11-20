/**
 * review-export — 把单条 Review + DecisionTextResp 序列化为 Markdown(v302+ P1)。
 *
 * 设计:
 *  - 纯函数,无副作用: 输入 review/text → 输出一段可粘贴的 Markdown 文本
 *  - 不依赖 React/DOM,可在 CLI / 测试 / 服务端复用
 *  - Markdown 头用 H1,各段用 H2/H3,符合 GitHub / Notion 渲染
 *  - 缺段/缺维用 ❌,命中用 ✅,便于扫读
 *
 * 用途: DecisionCard 头部加"复制 Markdown"按钮 → 粘贴到 Notion/飞书/研报
 */

import type { Review, DecisionTextResp } from '@/lib/types-reasoning-review'
import { STEPS, DIM_LABELS } from '@/lib/types-reasoning-review'

export type FormatReviewMarkdownOptions = {
  /** 是否含"推理原文" 段(默认 true; 摘要模式可关) */
  includeFullText?: boolean
  /** 是否含"段位齐全表" 段(默认 true) */
  includeStepTable?: boolean
}

const DEFAULT_OPTS: Required<FormatReviewMarkdownOptions> = {
  includeFullText: true,
  includeStepTable: true,
}

function fmtPct(p: number): string {
  return `${(p * 100).toFixed(1)}%`
}

function fmtPnL(pnl: number | null, isCorrect: 0 | 1 | null): string {
  if (pnl === null && isCorrect === null) return '未结算'
  if (isCorrect === 1) return `盈利 ${pnl?.toFixed(2) ?? '?'}`
  if (isCorrect === 0) return `亏损 ${pnl?.toFixed(2) ?? '?'}`
  return `${pnl?.toFixed(2) ?? '?'}`
}

/** 把 Review + Text 序列化为 Markdown 字符串 */
export function formatReviewAsMarkdown(
  review: Review,
  text: DecisionTextResp | null | undefined,
  options: FormatReviewMarkdownOptions = {},
): string {
  const opts = { ...DEFAULT_OPTS, ...options }
  const lines: string[] = []

  // ─── 头 ───────────────────────────────────────────────────────────
  const action = (review.action || '—').toUpperCase()
  const pnlStr = fmtPnL(review.forward_pnl, review.is_correct)
  lines.push(`# 决策 #${review.decision_id} — ${review.asset} ${action}`)
  lines.push('')
  lines.push(
    `| 完备度 | 行动 | 盈亏 | 推理长度 | ` +
      `| --- | --- | --- | --- | ` +
      `| ${review.completion_score.toFixed(1)}% | ${action} | ${pnlStr} | ${review.reasoning_length} 字 |`,
  )
  lines.push('')

  // ─── 段位齐全表 ─────────────────────────────────────────────────
  if (opts.includeStepTable) {
    lines.push('## 段位齐全')
    lines.push('')
    const missing = new Set(review.steps.missing)
    for (const step of STEPS) {
      const isMissing = missing.has(step)
      lines.push(`- ${isMissing ? '❌' : '✅'} ${step}`)
    }
    lines.push('')
  }

  // ─── 6 维完备度 ─────────────────────────────────────────────────
  lines.push('## 6 维完备度')
  lines.push('')
  lines.push(`- **反共识** ${review.counter_consensus.present ? '✅' : '❌'} ${review.counter_consensus.keywords_hit.length ? `命中 [${review.counter_consensus.keywords_hit.join(', ')}]` : ''}`)
  lines.push(`- **因果机制** ${review.causal_mechanism.present ? '✅' : '❌'} ${review.causal_mechanism.keywords_hit.length ? `命中 [${review.causal_mechanism.keywords_hit.join(', ')}]` : ''}`)
  lines.push(
    `- **跨资产方向** ${review.cross_asset_direction.present ? '✅' : '❌'} ` +
      (review.cross_asset_direction.target_asset
        ? `${review.cross_asset_direction.target_asset} ${review.cross_asset_direction.aligned_count} 一致 / ${review.cross_asset_direction.conflict_count} 冲突`
        : ''),
  )
  lines.push(`- **假设** ${review.assumption.present ? '✅' : '❌'} ${review.assumption.keywords_hit.length ? `命中 [${review.assumption.keywords_hit.join(', ')}]` : ''}`)
  lines.push(`- **历史样本** ${review.historical_pattern.present ? '✅' : '❌'} ${review.historical_pattern.keywords_hit.length ? `命中 [${review.historical_pattern.keywords_hit.join(', ')}]` : ''}`)
  lines.push(`- **因素概念** ${review.factor_concepts.count} 个 ${review.factor_concepts.concepts.length ? `[${review.factor_concepts.concepts.join(', ')}]` : ''}`)
  lines.push('')

  // ─── 跨资产明细 ────────────────────────────────────────────────
  if (
    review.cross_asset_direction.present &&
    review.cross_asset_direction.items.length > 0
  ) {
    lines.push('## 跨资产明细')
    lines.push('')
    lines.push('| 资产 | 24h 涨跌 | 方向一致 | 冲突 | 中性 |')
    lines.push('| --- | --- | --- | --- | --- |')
    for (const item of review.cross_asset_direction.items) {
      lines.push(
        `| ${item.symbol} | ${item.pct_24h === null ? '—' : `${item.pct_24h >= 0 ? '+' : ''}${item.pct_24h.toFixed(2)}%`} | ${item.aligned ? '✅' : '❌'} | ${item.conflicts ? '⚠️' : '—'} | ${item.neutral ? '⚪' : '—'} |`,
      )
    }
    lines.push('')
  }

  // ─── 推理原文 ─────────────────────────────────────────────────
  if (opts.includeFullText && text?.full_text) {
    lines.push('## 推理原文')
    lines.push('')
    // 保留换行(原样)
    lines.push('```')
    lines.push(text.full_text.trim())
    lines.push('```')
    lines.push('')
  }

  // ─── 元信息 ───────────────────────────────────────────────────
  lines.push('---')
  lines.push(`*导出于 ${new Date().toISOString()} · formatReviewAsMarkdown*`)

  return lines.join('\n')
}

/** 简短摘要(用于 chat / 评论里引用): "BTC BUY #12345 · 完备度 78% · 3 段缺" */
export function summarizeReviewShort(review: Review): string {
  const action = (review.action || '—').toUpperCase()
  const missing = review.steps.missing.length
  return (
    `${review.asset} ${action} #${review.decision_id} · ` +
    `完备度 ${Math.round(review.completion_score)}% · ` +
    (missing > 0 ? `${missing} 段缺` : '段位齐')
  )
}

/** 复制到剪贴板(浏览器/SSR-safe): 失败时返回 false */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator === 'undefined' || !navigator.clipboard) {
    return false
  }
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

/* 重导出供其他模块用 */
export { DIM_LABELS }
