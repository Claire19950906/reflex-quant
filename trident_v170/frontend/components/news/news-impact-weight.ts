// components/news/news-impact-weight.ts — v302 stub
//
// 原 HEAD 文件被用户在 working tree 删除（D 文件，无 git 历史可恢复）。
// app/attribution/page.tsx 仍 import `MarketState` type，stub 提供最小兼容定义。
// 真实"新闻影响权重"逻辑（regime 调整 / sentiment 折扣）随组件一起丢失，
// 此处仅保留类型 + 最小辅助函数，避免编译失败。

/** 市场状态：regime 由 VIX 推算，regime=1.0 中性，<1 收紧，>1 放大 */
export type MarketState = {
  vix: number
}

/** 由 VIX 推 regime：vix=20 → 1.0，vix<10 → 0.5，vix>40 → 1.5（线性插值，clamp [0.5, 1.5]） */
export function deriveRegime(vix: number): number {
  if (!Number.isFinite(vix)) return 1.0
  const clamped = Math.max(0, Math.min(60, vix))
  // 0.5 + (vix/40) * 1.0,  clamp [0.5, 1.5]
  return Math.max(0.5, Math.min(1.5, 0.5 + (clamped / 40) * 1.0))
}

/** 最小 NewsImpactItem 类型（app/attribution/page.tsx 用作泛型） */
export type NewsImpactItem = {
  id: string | number
  asset?: string
  title?: string
  weight?: number
  sentiment?: number
  created_at?: string
  [k: string]: unknown
}
