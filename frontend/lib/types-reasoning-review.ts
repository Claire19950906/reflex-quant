// v170 P0+：reasoning-review-panel 共享类型定义
//
// 抽出来减少 reasoning-review-panel.tsx 的行数（256 → ~150）
// 主文件保留主组件，子组件 (DecisionCard) 在 decision-card.tsx 复用本类型。
// 与 lib/types-review-history.ts 同源 — 详见 .cline/data/workspaces/chat/trident_v170/frontend/lib/types-review-history.ts:1
//
// 约束（务必遵守）：
//   - 任何改 Review / DecisionTextResp / CrossAssetItem 的 PR，必须同步 reasoning-review 后端 schema。

/** 跨资产方向检查 — 单标的 24h 涨跌方向 & 是否与决策方向一致 */
export type CrossAssetItem = {
  symbol: string
  pct_24h: number | null
  aligned: boolean
  conflicts: boolean
  neutral: boolean
}

/** 单条推理审查 — 6 维完备度评估（无 severity/level，所有问题平铺） */
export type Review = {
  decision_id: number
  asset: string
  action: string
  reasoning_length: number
  is_correct: 0 | 1 | null
  forward_pnl: number | null
  steps: { present: string[]; missing: string[]; pct: number }
  counter_consensus: { present: boolean; keywords_hit: string[] }
  causal_mechanism: { present: boolean; keywords_hit: string[] }
  factor_concepts: { count: number; concepts: string[] }
  cross_asset_direction: {
    present: boolean
    target_asset: string
    items: CrossAssetItem[]
    aligned_count: number
    conflict_count: number
    detail: string
  }
  assumption: { present: boolean; keywords_hit: string[] }
  historical_pattern: { present: boolean; keywords_hit: string[] }
  calibration: { confidence: number | null; pnl_realized: number | null; is_correct: 0 | 1 | null }
  notes: string[]
  completion_score: number
}

/** /reasoning/review 列表响应 */
export type ReviewListResp = {
  limit: number
  days: number
  only_failed: boolean
  count: number
  items: Review[]
}

/** 推理文本分段位置 — 与后端 reasoning_text/_split_segments 对齐 */
export type SegmentPosition = {
  segment: string
  present: boolean
  char_index: number | null
}

/** /reasoning/text 响应 — 单条决策的完整推理文本与分段位置 */
export type DecisionTextResp = {
  decision_id: number
  asset: string
  action: string
  reasoning_path: string
  reasoning: string
  asset_reasoning: string
  full_text: string
  full_text_length: number
  segments: {
    all: string[]
    present: string[]
    missing: string[]
    positions: SegmentPosition[]
  }
}

/** 8 段推理链 — 与 reasoning_review._ANALYSIS_STEPS 完全对齐 */
export const STEPS: readonly string[] = [
  '总框架',
  '因素定位',
  '驱动力',
  '水位博弈',
  '跨资产资金',
  '反共识结论',
  '逻辑自检',
  '总结',
]

/** 6 维评估的 UI 中文标签 — 与 Review 字段一一对应 */
export const DIM_LABELS: Record<string, string> = {
  steps: '8 段推理链完整性',
  counter_consensus: '反共识/对立观点',
  causal_mechanism: '因果机制',
  cross_asset_direction: '跨资产传导方向',
  assumption: '假设/前提',
  historical_pattern: '历史反例/样本',
}
