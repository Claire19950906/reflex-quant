// 优先直连后端（SSE 实时推送不能被 Next.js rewrites 代理缓冲）：
// NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 → API_BASE=http://127.0.0.1:8000/api（开发直连）
// 未配置时回退相对路径 /api（生产由 nginx 反代，nginx 支持 SSE 流式）
const _apiHost = process.env.NEXT_PUBLIC_API_URL || ''
export const API_BASE = _apiHost ? `${_apiHost.replace(/\/$/, '')}/api` : '/api'

// 桥接服务地址:当 8000 端口的 v237/v291 端点返 500 (如缺 Python 模块 missed_signal_dimensions)
// 时,自动 fallback 到 8100 端口的桥接服务,该服务从同一 SQLite DB 读真实数据并补足端点
const _bridgeHost = process.env.NEXT_PUBLIC_BRIDGE_URL || 'http://127.0.0.1:8100'
export const BRIDGE_BASE = `${_bridgeHost.replace(/\/$/, '')}/api`

// 哪些路径在 8000 失败时 fallback 到 8100 (基于真实 500 响应)
const FALLBACK_PATHS = [
  // v303 Q1+Q5: 8000 整体已死, 任何主路径都得 fallback — News 面板全靠 bridge
  '/events',                           // useNewsImpact → Top Entity / News Impact
  '/v237/missed-signals',              // MissedSignalsPanel 主数据
  '/v237/missed-signals/by-dimension', // 8000 缺 missed_signal_dimensions 模块
  '/v237/judgment-quality',            // MissedSignalsPanel 反思质量
  '/v291/critical-moves',              // event-market-attribution panel
  '/v291/stats',                       // event-market-attribution panel
  '/v291/move-attributions',           // event-market-attribution panel (per-move drill)
  '/v291/attribution-stats',           // 桥接服务提供资金流交叉验证
  '/v291/causal-graph',                // 桥接服务提供因果图谱
  '/v237/calibration',                 // 桥接服务提供周维度校准
  // v180+ P0+: 5 个 review/quant 500 端点 bridge 已接管
  '/review/snapshot',                  // 1450 calls/d 500
  '/abnormal-signals',                 // 692 calls/d 500
  '/sources/alerts',                   // 454 calls/d 500
  '/trading/signals',                  // 160 calls/d 500
  '/v201/impact/by-asset',             // 160 calls/d 500
  // v180+ P0-1: OHLCV 全字段 + 形态识别 (新表 ohlcv_daily)
  '/ohlcv/daily',
  '/ohlcv/pattern-snapshot',
  // v180+ P1: 决策回路 (trade_decisions 表)
  '/decision/list',
  '/decision/stats',
  '/decision/record',     // POST
  '/decision/settle-now', // POST
  // v180+ P0-3: 新闻源 (TECHFLOW + EASTMONEY → news_events)
  '/news/recent',
  '/news/pull-now',       // POST
  // v180+ P0-3 续: CFTC COT (Commitments of Traders) — 机构/散户持仓
  '/cot/recent',
  '/cot/assets',
  '/cot/pull-now',        // POST
  '/cot/emit-now',        // POST (只算 spec + emit, 不拉数据, <1s)
  '/cot/heatmap',         // 跨资产 spec_index 热力图
  // v291+ P1: 资金流聚合上下文 (桥接服务从 cftc_cot_data 直接聚合)
  '/macro/context',
  // v291+ P1: 跨资产传导链路 + 反思摘要 (桥接服务从 causal_chains/trade_reflections_v2 直接聚合)
  '/v291/cross-asset-chains',
  '/v291/reflections/summary',
  // v180+ P0-3: 新闻影响评估 (8000 缺评估逻辑, 桥接服务接管)
  '/news/impact-evaluate',
  // v303 Q1 cont: K线主数据 (MoversPanel chart 数据, 8000 死 → 必须 fallback)
  '/market/history',                    // K线 bar 主体 (1m/4h/30d)
  '/v189/calendar',                     // 日历事件 (K线 marker)
  // 桥接暂未实现但有 catch-all wrapper 的端点 (MoversPanel 其它并发 fetch)
  '/market/attribute',                  // 资产异动归因 (K线下方面板)
  '/market/movers',                     // 行情榜 (顶部分位卡片)
  '/market/depth',                      // 深度/订单簿
  '/v275/calendar/impact-by-asset',     // 日历 K线影响验证
  '/abnormal/swing-points',             // 拐点 (swing/cum4)
  '/admin/abnormal-signals/recompute-10m', // admin 异动重算
  // v307 GUB 补齐 (2026-10-21 审计): 后端有 64 个端点, 这里补上此前漏接的。
  // 判定依据 = 后端实测有真实数据返回, 但前端 0 处调用。详见 lib/api-gub.ts。
  '/tech/signals',                      // 技术信号融合 (支撑位/多形态/分项权重)
  '/tech/structure',                    // K线形态识别 (中文形态名)
  '/tech/anomaly-history',              // 历史异动 + 形态/RSI/MACD 证据链
  '/tech/anomaly',                      // 实时异动扫描 (已过噪声过滤)
  '/health',                            // 数据新鲜度
  '/v291/market-regime',                // COT 大型投机商仓位状态 (需求14 市场基本面)
  '/v291/funding-rate',                 // 永续合约资金费率
  '/v291/orderbook-depth',              // 订单簿深度
  '/v291/event-move-attribution',       // 事件→行情归因明细 (392 行, 含因果质量标注)
  '/v291/causal-chains/feedback',       // 因果链反馈闭环 + needs_recalibration 标记
  '/v189/causal/accuracy-stats',        // 因果链命中率统计 (需求9)
  '/review/causal-graph',               // 因果图谱节点/边
]

/**
 * fetchWithFallback —— 给用裸 fetch 的组件用的 fallback 包装器
 *  - 先打 API_BASE (8000)
 *  - 失败/500 时,如果是已知 broken 路径,改打 BRIDGE_BASE (8100)
 *  - 返回 JSON 解析结果(成功/失败都返回 ok/error 对象,不抛)
 *
 *  v2026.10.19 P0 决策: 桥接 catch-all 返 {ok: true, _bridge_fallback: true, ...} —
 *  老 component 设计就是按 {ok: true} 渲染空态 (例如 MissedSignalsDimensionSummary
 *  的"v302+ P0 后端未上线"banner 是 component 自己写的, 不是 throw 出来的).
 *  如果这里把 wrapper 改成 {ok: false, error: ...} throw, 会破坏这些老 component
 *  的"后端未上线"友好 banner 设计. 所以:
 *    - 这里**继续透传 wrapper** (不 throw, 不改 shape), 老 component 按原路径走
 *    - 真崩的 component (例如 discover-overview-bar 的 summary.cards 访问)
 *      改用 `apiGet` + try/catch, apiGet 已 throw BRIDGE_FALLBACK
 */
export async function fetchWithFallback<T = any>(path: string): Promise<T> {
  // v2026.10.19 P0 fix: 防御 caller 误传 /api 前缀 (base 已有 /api, 重复会拼成 /api/api/...
  // → 命中 bridge_server catch-all 返 wrapper, 真实端点 by-dimension/calibration 等被掩盖).
  // 这里 normalize 一下, 接受 '/api/v237/...' 或 '/v237/...' 两种写法
  // v303 P0 fix: 同时检查 raw path (带 /api/) 和 normalized — 双保险, 防止 caller 写法变化
  // 让 FALLBACK_PATHS 检查失效. 同时让 raw path + /api 路径都能正确识别.
  const normalized = path.startsWith('/api/') ? path.slice(4) : path
  const tryOne = async (base: string) => {
    // v306 P0+: 5xx 瞬态(bridge 重载/SQL 锁/反代超时)自动重试,最多 3 次
    //   退避 0.5 / 1 / 2 秒,避开 Next dev HMR / uvicorn reload 闪断
    //   4xx 不重试(404/422 是稳定错误, 重试浪费往返)
    let lastErr: { ok: false; error: string } | null = null
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt > 0) await new Promise(r => setTimeout(r, 250 * (2 ** (attempt - 1))))
      try {
        // v2026.10.19 P0 fix: 显式 cache: 'no-store' — Next.js dev mode 默认
        // force-cache 裸 fetch 响应, 第一次拿到 wrapper 后即使后端修好,
        // 浏览器二次 fetch 仍命中缓存的 wrapper. 显式 no-store 强制实时打.
        const r = await fetch(`${base}${normalized}`, { cache: 'no-store' })
        if (!r.ok) {
          if (r.status >= 500 && attempt < 2) {
            lastErr = { ok: false, error: `${base}${path} HTTP ${r.status}` }
            continue   // 5xx + 还有重试预算
          }
          const j = await r.json().catch(() => null)
          return { ok: false, error: j?.detail || `${base}${path} HTTP ${r.status}` }
        }
        return await r.json().catch(() => ({ ok: false, error: `bad JSON from ${base}${path}` }))
      } catch (e) {
        // CSP 拒绝 / 网络错误 / DNS 失败 / abort — 不能冒到外层
        // 同样视作瞬态, 重试
        if (attempt < 2) {
          lastErr = { ok: false, error: e instanceof Error ? e.message : String(e) }
          continue
        }
        return { ok: false, error: e instanceof Error ? e.message : String(e) }
      }
    }
    return lastErr ?? { ok: false, error: `${base}${path} HTTP 5xx after retry` }
  }
  const primary = await tryOne(API_BASE)
  if (primary && primary.ok === true) return primary
  // v303 P0 fix: 同时检查 raw path (带 /api/) 和 normalized — caller 传 /api/events 或 /events 都行
  // FALLBACK_PATHS 条目统一带前导 /, 例如 '/events', '/v237/missed-signals'
  if (FALLBACK_PATHS.some(p => path.startsWith(p) || path.startsWith(`/api${p}`))) {
    return await tryOne(BRIDGE_BASE) as T
  }
  return primary as T
}

// ─── v170 P0+ · 扩展 ──────────────────────────────────────────────────────

export type ApiGetOptions = Omit<RequestInit, 'method' | 'body'> & {
  /** cache 模式，默认 'no-store'（review 页全要实时）；配置/字典用 'default' */
  cache?: RequestCache
}

/**
 * apiGet —— 全局 GET 封装（v170 P0+ 增强版）
 *
 * 现状：lib/hooks/use-poll.ts 内置 fetch 包装器，但 review 页 9 个面板 100% 写裸 fetch，
 * 导致 9 套错误处理写法不统一。
 *
 * 新策略：所有 GET 走 apiGet，POST/PUT/DELETE 走 apiSend。
 *
 * - 默认 cache: 'no-store'（实时数据）
 * - 失败时 throw new Error(`${path} ${status}`) —— 与 usePoll 行为一致
 * - JSON 解析失败时 throw 原始 message
 */
export async function apiGet<T>(path: string, opts: ApiGetOptions = {}): Promise<T> {
  const { cache = 'no-store', signal, ...rest } = opts
  // v2026.10.19 P0 fix: 防御 caller 误传 /api 前缀 (base 已有 /api, 重复会拼成 /api/api/...
  // → 命中 bridge_server catch-all 返 wrapper, 真实端点 by-dimension/calibration 等被掩盖).
  const normalized = path.startsWith('/api/') ? path.slice(4) : path
  const url = `${API_BASE}${normalized}`
  let response: Response
  try {
    response = await fetch(url, { cache, signal, ...rest })
  } catch (e) {
    response = new Response(JSON.stringify({ detail: String(e) }), { status: 599 })
  }
  // 主服务失败时,fallback 到桥接服务(8100)
  // v303 P0 fix: 同时检查 raw path (带 /api/) 和 normalized — caller 传 /api/events 或 /events 都行
  if (!response.ok && FALLBACK_PATHS.some(p => path.startsWith(p) || path.startsWith(`/api${p}`))) {
    try {
      const fbUrl = `${BRIDGE_BASE}${normalized}`
      const fbResp = await fetch(fbUrl, { cache, signal, ...rest })
      if (fbResp.ok) {
        const fbData = (await fbResp.json()) as T
        // P0 fix: 桥接 catch-all wrapper (8100 自己也未实现) 也视为未实现 throw
        if (fbData && typeof fbData === 'object' && (fbData as any)._bridge_fallback === true) {
          throw new Error(`BRIDGE_FALLBACK: ${path} (${(fbData as any)._note || 'not implemented'})`)
        }
        return fbData
      }
    } catch (e) {
      // BRIDGE_FALLBACK 透传; 其他 catch 继续走主服务的 599
      if (e instanceof Error && e.message.startsWith('BRIDGE_FALLBACK:')) throw e
      // 桥接也失败,继续抛主服务的错
    }
  }
  if (!response.ok) throw new Error(`${path} ${response.status}`)
  const data = (await response.json()) as T
  // P0 fix: 桥接 catch-all wrapper (_bridge_fallback: true) 视为未实现, throw
  // 让 caller 的 .catch 走空态, 避免 setX(data) 拿到 wrapper 后访问 undefined 字段崩
  if (data && typeof data === 'object' && (data as any)._bridge_fallback === true) {
    throw new Error(`BRIDGE_FALLBACK: ${path} (${(data as any)._note || 'not implemented'})`)
  }
  return data
}

export type ApiSendOptions = Omit<RequestInit, 'method' | 'body'> & {
  cache?: RequestCache
}

/**
 * apiSend —— 全局 POST/PUT/PATCH/DELETE 封装
 *
 * - 默认 cache: 'no-store'
 * - 失败时尝试读 response.text() 作为 detail（后端 FastAPI 默认返回 {detail: '...'}）
 * - 成功时解析 JSON
 */
export async function apiSend<T>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  body?: unknown,
  opts: ApiSendOptions = {},
): Promise<T> {
  const { cache = 'no-store', ...rest } = opts
  const init: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
    cache,
    ...rest,
  }
  if (body !== undefined) init.body = JSON.stringify(body)
  // v2026.10.19 P0 fix: 同 apiGet, 防御 /api 前缀
  const normalized = path.startsWith('/api/') ? path.slice(4) : path
  const response = await fetch(`${API_BASE}${normalized}`, init)
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(detail || `${path} ${method} ${response.status}`)
  }
  // 204 No Content 等不返回 JSON
  const text = await response.text()
  const parsed = (text ? JSON.parse(text) : (undefined as unknown)) as T
  // P0 fix: 同 apiGet, 桥接 catch-all wrapper 抛错
  if (parsed && typeof parsed === 'object' && (parsed as any)._bridge_fallback === true) {
    throw new Error(`BRIDGE_FALLBACK: ${method} ${path} (${(parsed as any)._note || 'not implemented'})`)
  }
  return parsed
}

/**
 * apiDelete —— DELETE 专用便捷封装（多数场景不需要读响应体）
 */
export async function apiDelete(path: string, opts: ApiSendOptions = {}): Promise<void> {
  await apiSend<void>(path, 'DELETE', undefined, opts)
}

// ─── v172 事件结构化输出层 API 包装 ──────────────────────────────────

import type {
  EventAtom, EventTimeCandidate, ImpactCurvePoint, ImpactCurveAggregated,
  NewsStructuredResponse, DimAsset, DimEventType,
  EventAtomSummary, EventImpactStat,
} from './quant-data'

type ListResponse<T> = { items: T[]; count: number }
type NewsTimeline = {
  primary: EventTimeCandidate | null
  candidates: EventTimeCandidate[]
  count: number
  news_id: number
}
type EventAtomDetail = {
  atom: EventAtom
  candidates: EventTimeCandidate[]
  impact_curve: ImpactCurveAggregated
}

/** 事件原子列表 */
export function fetchEventAtoms(params?: {
  type_code?: string; asset_code?: string; news_id?: number
  since_ts?: number; limit?: number
}): Promise<ListResponse<EventAtom>> {
  const q = new URLSearchParams()
  if (params?.type_code)  q.set('type_code',  params.type_code)
  if (params?.asset_code) q.set('asset_code', params.asset_code)
  if (params?.news_id !== undefined) q.set('news_id', String(params.news_id))
  if (params?.since_ts !== undefined) q.set('since_ts', String(params.since_ts))
  if (params?.limit !== undefined) q.set('limit', String(params.limit))
  const qs = q.toString()
  return apiGet<ListResponse<EventAtom>>(`/event_atoms${qs ? `?${qs}` : ''}`)
}

/** 单事件原子详情（+ 候选时间 + 影响曲线聚合） */
export function fetchEventAtom(atomId: number): Promise<EventAtomDetail> {
  return apiGet<EventAtomDetail>(`/event_atoms/${atomId}`)
}

/** 单新闻对应的事件原子列表 */
export function fetchNewsAtoms(newsId: number): Promise<ListResponse<EventAtom>> {
  return apiGet<ListResponse<EventAtom>>(`/news/${newsId}/atoms`)
}

/** 单新闻事件时间线 */
export function fetchNewsEventTimeline(newsId: number): Promise<NewsTimeline> {
  return apiGet<NewsTimeline>(`/news/${newsId}/event_timeline`)
}

/** 单新闻结构化输入/校验 */
export function fetchNewsStructured(newsId: number): Promise<NewsStructuredResponse> {
  return apiGet<NewsStructuredResponse>(`/news/${newsId}/structured`)
}

/** 影响曲线列表 */
export function fetchEventImpactCurves(params?: {
  asset_code?: string; type_code?: string; limit?: number
}): Promise<ListResponse<ImpactCurvePoint>> {
  const q = new URLSearchParams()
  if (params?.asset_code) q.set('asset_code', params.asset_code)
  if (params?.type_code)  q.set('type_code',  params.type_code)
  if (params?.limit !== undefined) q.set('limit', String(params.limit))
  const qs = q.toString()
  return apiGet<ListResponse<ImpactCurvePoint>>(`/event_impact_curves${qs ? `?${qs}` : ''}`)
}

/** 单原子完整曲线（rows + 4 窗口聚合） */
export function fetchEventImpactCurveByAtom(atomId: number): Promise<{
  atom_id: number; rows: ImpactCurvePoint[]; aggregated: ImpactCurveAggregated
}> {
  return apiGet(`/event_impact_curves/by_atom/${atomId}`)
}

/** 每日聚合统计（默认 7 天） */
export function fetchEventImpactStat(days = 7): Promise<ListResponse<EventImpactStat> & { days: number }> {
  return apiGet(`/event_impact_stat?days=${days}`)
}

/** 维度表 - 资产 */
export function fetchDimAssets(): Promise<ListResponse<DimAsset>> {
  return apiGet<ListResponse<DimAsset>>(`/event_dim/assets`)
}

/** 维度表 - 事件类型 */
export function fetchDimEventTypes(): Promise<ListResponse<DimEventType>> {
  return apiGet<ListResponse<DimEventType>>(`/event_dim/event_types`)
}

/** event_atom 7 日汇总 */
export function fetchEventAtomSummary(): Promise<EventAtomSummary> {
  return apiGet<EventAtomSummary>(`/event_atom/summary`)
}
