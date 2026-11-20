// v170 P0+：replay-panel 共享常量与纯函数
//
// 从 replay-panel.tsx 抽出 3 个无副作用项：
//   - llmShortModel    模型 id → 显示简名
//   - TRACKS           模拟盘赛道枚举 + 中文标签
//   - NEWS_SOURCES     新闻源枚举 + 显示简名
//
// 约束（务必遵守）：
//   - 任何新增 NewsSourceKey / PaperTrack 都要在这里追加对应中文标签。

import type { NewsSourceKey, PaperTrack } from '@/lib/replay-data'

/** LLM 模型 id → 显示简名：openai/gpt-4o-mini → gpt-4o-mini；xinshu/gpt-5.5 → gpt-5.5 (xinshu) */
export function llmShortModel(id?: string | null): string {
  if (!id) return '当前模型'
  const base = id.includes('/') ? (id.split('/').pop() || id) : id
  return id.startsWith('xinshu/') ? `${base} (xinshu)` : base
}

/** 模拟盘赛道（PaperTrack）→ 中文标签 */
export const TRACKS: Array<[PaperTrack, string]> = [
  ['crypto', '加密货币'],
  ['gold', '黄金'],
  ['oil', '原油'],
  ['stocks', '美股'],
]

/** 新闻源（NewsSourceKey）→ 显示简名 */
export const NEWS_SOURCES: Array<[NewsSourceKey, string]> = [
  ['financialjuice', 'FJ'],
  ['tree_news', 'Tree'],
  ['techflow', '深潮'],
  ['eastmoney', '东财'],
  ['blockbeats', '律动'],
  ['jin10', '金十'],
  ['fred', 'FRED'],
  ['finnhub', 'Finnhub'],
  ['feishu_jin10', '飞书金十'],
  ['reuters', 'Reuters'],
  ['bls', 'BLS'],
  ['yahoo_finance', 'Yahoo'],
  ['marketwatch', 'MarketWatch'],
  ['fxstreet', 'FXStreet'],
]