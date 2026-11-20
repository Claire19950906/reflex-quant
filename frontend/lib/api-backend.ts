/**
 * lib/api-backend.ts
 *
 * 后端能力面板的数据访问层。
 *
 * 为什么单独一个文件:
 *   1. 2026-11-02 审计发现 157 个后端端点前端零引用, 归位工作需要一个统一的
 *      取数入口 —— 每个域一个 loader, 由 components/quant/backend-capability-panel.tsx
 *      按 domain key 调度。
 *   2. 这些端点在 WAL 写锁竞争下**可达 40s**(实测 /api/signal-weights/stats
 *      首次 39.3s → 2.1s → 0.26s), 远超 apiGet 默认的 30s 预算,
 *      所以重端点必须显式放宽 timeout。
 *   3. 后端有相当一部分端点用 **HTTP 200 + {ok:false, error:'...'}** 表达失败
 *      (而不是 4xx/5xx) —— apiGet 不会识别这种形状, 必须在这里显式拦截,
 *      否则界面会把「后端未就绪」画成「没有数据」。
 */

import { apiGet, DEFAULT_GET_TIMEOUT_MS } from '@/lib/api'

/** 重端点预算: 首调可能 40s(锁竞争), 给到 75s */
export const SLOW_ENDPOINT_TIMEOUT_MS = 75_000

export type BackendStatTone = 'ok' | 'warn' | 'bad'

export type BackendStat = {
  label: string
  value: string | number
  hint?: string
  tone?: BackendStatTone
}

export type BackendRow = {
  title: string
  subtitle?: string
  body?: string
  meta?: string[]
}

export type BackendPayload = {
  stats: BackendStat[]
  rows: BackendRow[]
  note?: string
}

export type BackendLoader = () => Promise<BackendPayload>

/**
 * 取后端 JSON, 并把 {ok:false, error} 形态转成异常。
 * 后端 v230+ 有一批端点习惯用 ok 字段表达失败(见 reflection/deep/stats 等)。
 */
export async function fetchBackend<T>(path: string, opts: { timeoutMs?: number } = {}): Promise<T> {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_GET_TIMEOUT_MS
  const raw = await apiGet<T>(path, { timeoutMs })
  if (raw && typeof raw === 'object' && 'ok' in (raw as Record<string, unknown>)) {
    const okFlag = (raw as Record<string, unknown>).ok
    if (okFlag === false) {
      const err = (raw as Record<string, unknown>).error
      throw new Error(typeof err === 'string' && err ? err : '后端返回 ok:false(未就绪)')
    }
  }
  return raw
}

/** 数字格式化 —— 后端字段可能是 null/undefined/字符串 */
export function num(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === '') return '—'
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return String(v)
  return digits > 0 ? n.toFixed(digits) : String(Math.round(n))
}

/** 百分比 */
export function pct(v: unknown, digits = 1): string {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${(n * 100).toFixed(digits)}%`
}

/** 时间戳 → 本地字符串 */
export function ts(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n) || n <= 0) return '—'
  return new Date(n * 1000).toLocaleString('zh-CN', { hour12: false })
}

function asArray(v: unknown): unknown[] {
  return Array.isArray(v) ? v : []
}

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}


/** 信号权重 —— /signal-weights/stats */
async function loadSignalWeights(): Promise<BackendPayload> {
  const s = obj(await fetchBackend('/signal-weights/stats', { timeoutMs: SLOW_ENDPOINT_TIMEOUT_MS }))
  const tiers = obj(s.tier_distribution)
  const stats: BackendStat[] = Object.entries(tiers).map(([tier, v]) => {
    const t = obj(v)
    return {
      label: `权重档 ${tier}`,
      value: num(t.n, 0),
      hint: `均权 ${num(t.avg_weight)}`,
    }
  })
  stats.push({
    label: '24h 冷信号',
    value: num(s.cold_signal_count_24h, 0),
    hint: '无信息源可归因',
    tone: Number(s.cold_signal_count_24h) > 0 ? 'warn' : 'ok',
  })
  const cron = obj(s.last_cron_run)
  if (Object.keys(cron).length) {
    stats.push({
      label: '上次权重 cron',
      value: `${num(cron.processed_n, 0)} / ${num(cron.updated_n, 0)}`,
      hint: `${ts(cron.started_at)} · 处理/更新`,
      tone: cron.error_msg ? 'bad' : 'ok',
    })
  }
  const rows: BackendRow[] = asArray(s.top_sources).map((x) => {
    const o = obj(x)
    return {
      title: `${o.news_source ?? '?'} · ${o.asset ?? '?'}`,
      subtitle: `命中率 ${pct(o.hit_rate)} · 样本 ${num(o.sample_n, 0)}`,
      meta: [
        `胜 ${num(o.win_n, 0)} / 负 ${num(o.loss_n, 0)}`,
        `源乘数 ${num(o.source_mult)}`,
        `前瞻均盈亏 ${num(o.avg_forward_pnl)}%`,
      ],
    }
  })
  return { stats, rows, note: `窗口 ${num(s.window_hours, 0)}h · signal_weight_history 43107 行` }
}

/** LLM 调用统计 —— /v187/llm-stats/summary */
async function loadLlmStats(): Promise<BackendPayload> {
  const s = obj(await fetchBackend('/llm-stats/summary', { timeoutMs: SLOW_ENDPOINT_TIMEOUT_MS }))
  const totals = obj(s.totals)
  const byPurpose = asArray(s.by_purpose)
  const sumCalls = byPurpose.reduce<number>((n, x) => n + Number(obj(x).calls ?? 0), 0)
  const sumIn = byPurpose.reduce<number>((n, x) => n + Number(obj(x).tokens_in ?? 0), 0)
  const sumOut = byPurpose.reduce<number>((n, x) => n + Number(obj(x).tokens_out ?? 0), 0)
  const stats: BackendStat[] = [
    { label: '调用总数', value: num(totals.calls ?? sumCalls, 0), hint: `窗口 ${num(s.window_days ?? 0)} 天` },
    { label: '入 token', value: num(totals.tokens_in ?? sumIn, 0) },
    { label: '出 token', value: num(totals.tokens_out ?? sumOut, 0) },
  ]
  const rows: BackendRow[] = byPurpose.map((x) => {
    const o = obj(x)
    const calls = Number(o.calls ?? 0)
    const okCalls = Number(o.ok_calls ?? 0)
    return {
      title: String(o.purpose ?? '?'),
      subtitle: `均延迟 ${num(o.latency_ms_avg, 0)}ms`,
      meta: [
        `调用 ${num(calls, 0)}`,
        `成功 ${num(okCalls, 0)}${calls ? ` (${pct(okCalls / calls, 0)})` : ''}`,
        `token ${num(o.tokens_in, 0)} / ${num(o.tokens_out, 0)}`,
      ],
    }
  })
  return { stats, rows, note: 'ai_call_stats 380525 行 · 用于定位哪条链路在烧 token' }
}


/** 信念系统 —— /beliefs */
async function loadBeliefs(): Promise<BackendPayload> {
  const list = asArray(await fetchBackend('/beliefs'))
  const byStatus = new Map<string, number>()
  for (const x of list) {
    const st = String(obj(x).status ?? 'unknown')
    byStatus.set(st, (byStatus.get(st) ?? 0) + 1)
  }
  const stats: BackendStat[] = [
    { label: '信念总数', value: list.length },
    ...[...byStatus.entries()].map(([k, v]) => ({ label: `状态 ${k}`, value: v })),
  ]
  const rows: BackendRow[] = list.slice(0, 20).map((x) => {
    const o = obj(x)
    const tags = asArray(o.regime_tags).map(String)
    // 实测形态: evidence_for / evidence_against 是计数, 不是文本 —— 不要当文案渲染
    const ev = Number(o.evidence_for ?? 0)
    const evAgainst = Number(o.evidence_against ?? 0)
    return {
      title: String(o.thesis ?? o.belief_uid ?? '(无题)').slice(0, 140),
      subtitle: `${o.asset ?? '—'} · ${o.level ?? '—'} · 置信 ${num(o.confidence)}`,
      meta: [
        o.status ? `状态 ${o.status}` : '',
        o.source ? `来源 ${o.source}` : '',
        `证据 ${ev} 正 / ${evAgainst} 负`,
        tags.length ? `区制 ${tags.join(', ')}` : '',
      ].filter(Boolean),
    }
  })
  return { stats, rows, note: 'belief-maintenance-cron 每 6h 修订 · belief_history 425 条' }
}

/** 深度反思 —— /reflection/deep/stats + /structured_reflection/status */
async function loadReflectionDeep(): Promise<BackendPayload> {
  const [deep, status] = await Promise.all([
    fetchBackend('/reflection/deep/stats', { timeoutMs: SLOW_ENDPOINT_TIMEOUT_MS }).catch((e) => ({
      error: (e as Error).message,
    })),
    fetchBackend('/structured_reflection/status', { timeoutMs: SLOW_ENDPOINT_TIMEOUT_MS }).catch(() => ({})),
  ])
  const d = obj(deep)
  if (d.error) throw new Error(String(d.error))
  const st = obj(status)
  const stats: BackendStat[] = []
  for (const [k, v] of Object.entries(obj(d.enrich_status_breakdown))) {
    stats.push({ label: `enrich ${k}`, value: num(v, 0), tone: k === 'ok' ? 'ok' : 'warn' })
  }
  for (const [band, v] of Object.entries(obj(d.by_luck_band))) {
    const b = obj(v)
    const wr = Number(b.win_rate ?? 0)
    stats.push({
      label: `运气 ${band}`,
      value: pct(wr),
      hint: `n=${num(b.n, 0)}`,
      tone: wr >= 0.6 ? 'ok' : wr >= 0.4 ? 'warn' : 'bad',
    })
  }
  if (st.started !== undefined) {
    stats.push({
      label: '结构化反思 worker',
      value: st.started ? '运行中' : '未启动',
      hint: `uptime ${num(st.uptime_seconds, 0)}s · 队列 ${num(st.queue_pending, 0)}`,
      tone: st.started ? 'ok' : 'bad',
    })
  }
  const rows: BackendRow[] = asArray(d.deep_lessons_top).map((x) => {
    const o = obj(x)
    return {
      title: `${o.asset ?? '—'} ${o.action ?? ''} ${num(o.pnl_pct)}%`,
      subtitle: `形态 ${o.kline_pattern ?? '—'} · 运气分 ${num(o.luck_vs_signal_score)}`,
      body: String(o.deep_lesson ?? '').slice(0, 300) || undefined,
    }
  })
  return { stats, rows, note: `窗口 ${num(d.window_days, 0)} 天 · 运气分档胜率是「信号 vs 运气」的判据` }
}


/** 策略版本 —— /strategies/current */
async function loadStrategies(): Promise<BackendPayload> {
  const c = obj(await fetchBackend('/strategies/current'))
  // 实测形态: { id, slug, name, status, is_current, current_version_id,
  //             active_version: { id, version, params:{...}, source, created_at } }
  const cur = obj(c.current ?? c)
  const ver = obj(cur.active_version)
  const params = obj(ver.params)
  const stats: BackendStat[] = [
    { label: '当前策略', value: String(cur.name ?? cur.slug ?? '—') },
    { label: '版本', value: String(ver.version ?? cur.current_version_id ?? '—') },
    { label: '状态', value: String(cur.status ?? '—') },
    { label: '参数项', value: Object.keys(params).length },
  ]
  const rows: BackendRow[] = Object.entries(params).map(([k, v]) => ({
    title: k,
    subtitle: `v${String(ver.version ?? cur.current_version_id ?? '?')}`,
    meta: [`值 ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`],
  }))
  const metaRows: BackendRow[] = []
  if (ver.source) {
    metaRows.push({ title: `版本来源 ${String(ver.source)}`, subtitle: ver.created_at ? String(ver.created_at) : undefined })
  }
  if (cur.description) {
    metaRows.push({ title: '策略说明', body: String(cur.description) })
  }
  return {
    stats,
    rows: [...metaRows, ...rows],
    note: '审批通过会写入新版本(strategy_store.add_version) —— 批准后回来看这里',
  }
}

/** 运维巡检 —— /admin/id-reconcile/status + /v188/admin/db-stats */
async function loadOpsInspection(): Promise<BackendPayload> {
  const [recon, dbStats] = await Promise.all([
    fetchBackend('/admin/id-reconcile/status', { timeoutMs: SLOW_ENDPOINT_TIMEOUT_MS }).catch((e) => ({
      error: (e as Error).message,
    })),
    fetchBackend('/admin/db-stats', { timeoutMs: SLOW_ENDPOINT_TIMEOUT_MS }).catch((e) => ({
      error: (e as Error).message,
    })),
  ])
  const r = obj(recon)
  if (r.error) throw new Error(String(r.error))
  const d = obj(dbStats)
  const stats: BackendStat[] = []
  const put = (label: string, v: unknown, hint?: string, tone?: BackendStatTone) =>
    stats.push({
      label,
      value: typeof v === 'object' ? JSON.stringify(v) : (v as string | number),
      hint,
      tone,
    })
  // 实测形态: { ok, coverage: { <表>: {total, with_uuid, missing_uuid} },
  //             quarantined, unresolved_alerts, ... }
  const cov = obj(r.coverage)
  let missingTotal = 0
  for (const [table, v] of Object.entries(cov)) {
    const c = obj(v)
    const missing = Number(c.missing_uuid ?? 0)
    missingTotal += missing
    put(
      table,
      `${num(c.with_uuid, 0)} / ${num(c.total, 0)}`,
      `缺 uuid ${num(missing, 0)}`,
      missing > 0 ? 'warn' : 'ok',
    )
  }
  if (r.quarantined !== undefined) put('隔离记录', r.quarantined)
  if (r.unresolved_alerts !== undefined) {
    put('未处理告警', r.unresolved_alerts, undefined, Number(r.unresolved_alerts) > 0 ? 'warn' : 'ok')
  }
  if (missingTotal > 0) {
    stats.push({
      label: '缺 uuid 合计',
      value: missingTotal,
      hint: '待 id-reconcile 回填',
      tone: 'warn',
    })
  }
  const rows: BackendRow[] = []
  if (d.error) {
    rows.push({ title: 'DB 统计不可用', body: String(d.error) })
  } else {
    // 实测形态: { ok, tables:{<表>:行数}, total_rows, run_id_distribution, db_path, db_size_bytes }
    if (d.total_rows !== undefined) put('总行数', d.total_rows)
    if (d.db_size_bytes !== undefined) {
      put('DB 体积', `${(Number(d.db_size_bytes) / 1048576).toFixed(1)} MB`)
    }
    if (d.db_wal_size_bytes !== undefined) {
      put('WAL 体积', `${(Number(d.db_wal_size_bytes) / 1048576).toFixed(1)} MB`)
    }
    if (d.db_path !== undefined) put('DB 路径', d.db_path)
    const tables = obj(d.tables)
    const top = Object.entries(tables)
      .map(([k, v]) => [k, Number(v)] as const)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
    if (top.length) {
      rows.push({
        title: `表行数 Top ${top.length}`,
        body: top.map(([k, v]) => `${k}: ${v.toLocaleString('zh-CN')}`).join(' · '),
      })
    }
    const dist = obj(d.run_id_distribution)
    const distKeys = Object.keys(dist).slice(0, 4)
    if (distKeys.length) {
      rows.push({
        title: 'run_id 分布',
        body: distKeys
          .map((k) => {
            const m = obj(dist[k])
            const parts = Object.entries(m)
              .sort((a, b) => Number(b[1]) - Number(a[1]))
              .slice(0, 3)
              .map(([rid, n]) => `${rid || '(空)'}=${n}`)
            return `${k}: ${parts.join(', ')}`
          })
          .join(' · '),
      })
    }
  }
  return {
    stats,
    rows,
    note: 'id-reconcile-cron 每 300s 跑 · 面板仅暴露只读视图',
  }
}

/** 交易所模式 —— /v190/exchange/status */
async function loadExchangeMode(): Promise<BackendPayload> {
  const s = obj(await fetchBackend('/exchange/status'))
  const stats: BackendStat[] = []
  for (const [k, v] of Object.entries(s)) {
    if (typeof v === 'boolean' || typeof v === 'number' || typeof v === 'string') {
      stats.push({ label: k, value: typeof v === 'boolean' ? (v ? '是' : '否') : v })
    }
  }
  return { stats, rows: [], note: '实盘开关属高危操作 —— 后端相关表当前未启用' }
}

/** 交易日记 —— /v189/journal */
async function loadJournal(): Promise<BackendPayload> {
  const j = obj(await fetchBackend('/journal'))
  const stats: BackendStat[] = []
  for (const [k, v] of Object.entries(j)) {
    if (typeof v === 'number' || typeof v === 'string') stats.push({ label: k, value: v })
  }
  const rows: BackendRow[] = asArray(j.entries ?? j.items).map((x) => {
    const o = obj(x)
    return {
      title: String(o.title ?? o.date ?? o.id ?? '—'),
      body: String(o.summary ?? o.content ?? '').slice(0, 300) || undefined,
    }
  })
  return { stats, rows, note: '交易日记(v189) —— 原 /journal 页面无站内入口' }
}

/** 全部 loader 注册表 —— key 与 lib/backend-registry.ts 的域 key 一致 */
export const BACKEND_LOADERS: Record<string, BackendLoader> = {
  'signal-weights': loadSignalWeights,
  'llm-stats': loadLlmStats,
  beliefs: loadBeliefs,
  'reflection-deep': loadReflectionDeep,
  strategies: loadStrategies,
  'ops-inspection': loadOpsInspection,
  'exchange-mode': loadExchangeMode,
  journal: loadJournal,
}
