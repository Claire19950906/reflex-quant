/**
 * review-view-snapshot.ts — 把 /review 当前 URL 状态序列化为可分享的 Markdown(v302+ P6)。
 *
 * 设计:
 *  - 与 review-export.ts(formatReviewAsMarkdown)的区别: 后者需要完整 Review 对象,
 *    用于单条决策的复盘;本模块只序列化"用户在 /review 看到的视图状态",用于
 *    截屏前的快照 / 同事对齐当前 view。
 *  - 纯函数: 入参 (urlState + sectionVisibility) → Markdown 字符串
 *  - 不读 DOM, 不依赖 React, 可在测试 / CLI 复用
 *  - 包含 timestamp、URL 原文、各 section 可见性、当前选中的错误码,够用但不冗余
 *
 * 注意:
 *  - Review 自身的复盘内容(归因/推理/改进)由 review-export.ts 处理;本文件不重复
 *  - 时间戳是生成时刻,不是 URL 里读出来的(URL 没存时间)
 *  - "已折叠 N/总 M" 段让读者一眼看出"这个截图是 9 段中展开了 5 段的状态"
 */

import type { ReviewUrlState } from './use-review-url-state'

export type ViewSnapshotInput = {
  /** 当前 URL 状态(从 useReviewUrlState 读) */
  urlState: ReviewUrlState
  /** 当前 visible 的 section 数(从 SECTION_MATRIX 派生) */
  totalSectionCount: number
  /** 已展开的 section 数(总 - 折叠) */
  expandedSectionCount: number
  /** 当前在浏览器地址栏的完整 URL(用于贴回) */
  fullUrl: string
  /** section 列表(每项有 id + label) — 用于逐项展示可见性 */
  sections: ReadonlyArray<{ id: string; label: string; visible: boolean }>
}

const TAB_LABELS: Record<ReviewUrlState['tab'], string> = {
  all: '全部',
  journal: '交易日记',
  experiences: '经验',
}
const AB_LABELS: Record<ReviewUrlState['ab'], string> = {
  a: 'A · 历史真实交易复盘',
  b: 'B · 快速评测复盘',
}

function fmtTimestamp(d: Date = new Date()): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${y}-${m}-${day} ${hh}:${mm}`
}

export function formatViewSnapshotAsMarkdown(input: ViewSnapshotInput): string {
  const { urlState, totalSectionCount, expandedSectionCount, fullUrl, sections } = input
  const lines: string[] = []

  lines.push(`# 复盘视图快照`)
  lines.push('')
  lines.push(`> 生成于 ${fmtTimestamp()}`)
  lines.push('')

  // 状态块
  lines.push('## 当前状态')
  lines.push('')
  lines.push(`- **模式**: ${TAB_LABELS[urlState.tab]}`)
  lines.push(`- **复盘面板**: ${AB_LABELS[urlState.ab]}`)
  lines.push(`- **时段**: ${urlState.period}`)
  if (urlState.period === 'custom') {
    lines.push(`  - 自定义范围: ${urlState.customRange.fromDate} → ${urlState.customRange.toDate}`)
  }
  lines.push(`- **当前错误码**: ${urlState.err ?? '(无)'}`)
  lines.push(`- **段位展开**: ${expandedSectionCount} / ${totalSectionCount}`)
  lines.push('')

  // Section 可见性
  lines.push('## 段位清单')
  lines.push('')
  for (const s of sections) {
    if (!s.visible) continue
    lines.push(`- ✅ ${s.label}`)
  }
  lines.push('')

  // 完整 URL(给读者复制回 /review 复现)
  lines.push('## 链接')
  lines.push('')
  lines.push(`<${fullUrl}>`)
  lines.push('')

  // 复盘说明(footer)
  lines.push('---')
  lines.push('')
  lines.push(
    `本快照只反映"打开 /review 时看到的页面状态",不包含具体归因/推理内容。`,
  )
  lines.push(
    `如需分享单条决策的复盘,使用决策卡上的"复制 Markdown"按钮。`,
  )

  return lines.join('\n')
}
