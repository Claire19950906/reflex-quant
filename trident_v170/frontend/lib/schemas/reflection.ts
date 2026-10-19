/**
 * /reflection/* 接口响应的 zod schema（v275+）。
 *
 * 后端 FastAPI 不强制 schema,字段是 reflection 反序列化时已经裁好的产物;
 * 加 zod 后,UI 在拿到意外结构时能拿到"哪个字段出了问题"的具体错误信息,
 * 不再因为 undefined.length / .toFixed() 直接白屏。
 *
 * 接入方式:
 *   const raw = await apiGet<unknown>(`/reflection/recent?...`)
 *   const parsed = parseReflectionRecent(raw)
 *   if (!parsed.ok) {
 *     // 注意:保留旧数据 + 写一行 console.warn,不让 schema mismatch 杀掉 UI
 *     console.warn('reflection/recent shape mismatch:', parsed.error)
 *     return
 *   }
 *   setRecent(parsed.data)
 */

import { z } from 'zod'

/* ─── 决策错误条目 ───────────────────────────────────────── */

export const DecisionErrorSchema = z.object({
  id: z.number().optional(),
  decision_id: z.number(),
  error_code: z.string(),
  origin: z.string(),
  severity: z.number(),
  detail: z.string(),
  source: z.string().optional(),
  evidence_refs: z.unknown().optional(),
  created_at: z.string(),
})
export type DecisionError = z.infer<typeof DecisionErrorSchema>

/* ─── /reflection/recent 响应 ──────────────────────────────── */

const CrossAssetSnapshotSchema = z.object({
  anchor_ts: z.string().optional(),
  asset: z.string().optional(),
  macro_regime: z.string().optional(),
  risk_off_count: z.number().optional(),
  risk_on_count: z.number().optional(),
  total_symbols: z.number().optional(),
  warning: z.string().optional(),
  pct_only: z.record(z.string(), z.number()).optional(),
  error: z.string().optional(),
}).partial()

const CodePnlImpactSchema = z.object({
  code: z.string(),
  n_errors: z.number(),
  n_settled: z.number(),
  avg_pnl: z.number().nullable(),
  total_pnl: z.number(),
  win_rate_when_coded: z.number().nullable(),
  worst_pnl: z.number().nullable(),
  best_pnl: z.number().nullable(),
})

export const ReflectionRecentSchema = z.object({
  total: z.number(),
  errors: z.array(DecisionErrorSchema),
  code_stats: z.record(z.string(), z.number()),
  origin_stats: z.record(z.string(), z.number()),
  severity_stats: z.record(z.string(), z.number()),
  prompt_preview: z.string(),
  // 可选字段:后端在 feature flag 关闭时不返回
  code_subkinds: z.record(z.string(), z.number()).optional(),
  cross_asset_snapshot: CrossAssetSnapshotSchema.optional(),
  code_pnl_impact: z.array(CodePnlImpactSchema).optional(),
})
export type ReflectionRecent = z.infer<typeof ReflectionRecentSchema>

/* ─── /reflection/stats 响应 ──────────────────────────────── */

export const ReflectionStatsSchema = z.object({
  days: z.number(),
  total: z.number(),
  by_code: z.record(z.string(), z.number()),
  by_origin: z.record(z.string(), z.number()),
  by_severity: z.record(z.string(), z.number()),
  rows: z.array(z.object({
    error_code: z.string(),
    origin: z.string(),
    severity: z.number(),
    cnt: z.number(),
  })),
})
export type ReflectionStats = z.infer<typeof ReflectionStatsSchema>

/* ─── safe 解析器(返回 {ok, data} / {ok:false, error}) ─────── */

export type ParseResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string }

/**
 * safeParseWithPath —— 把 zod 的 issue 序列化成"path: message, path: message"形式,
 * 方便 console.warn 或 UI 显示。
 */
function formatZodError(err: z.ZodError): string {
  return err.issues
    .slice(0, 5) // 截前 5 条,避免太冗长
    .map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`)
    .join('; ')
}

export function parseReflectionRecent(raw: unknown): ParseResult<ReflectionRecent> {
  const r = ReflectionRecentSchema.safeParse(raw)
  if (!r.success) return { ok: false, error: formatZodError(r.error) }
  return { ok: true, data: r.data }
}

export function parseReflectionStats(raw: unknown): ParseResult<ReflectionStats> {
  const r = ReflectionStatsSchema.safeParse(raw)
  if (!r.success) return { ok: false, error: formatZodError(r.error) }
  return { ok: true, data: r.data }
}