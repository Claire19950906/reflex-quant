// 后端基址：未配置 NEXT_PUBLIC_API_URL 时走相对路径 /api，由 Next rewrites 反代到后端
// （本机后端为 8100 bridge；生产由 nginx 反代，nginx 支持 SSE 流式）。
const _apiHost = process.env.NEXT_PUBLIC_API_URL || ''
export const API_BASE = _apiHost ? `${_apiHost.replace(/\/$/, '')}/api` : '/api'

// 桥接服务地址：主服务不可达 / 端点 500 时的 fallback 目标，从同一 SQLite DB 读真实数据。
// 本机与主服务同指 8100；SAME_BACKEND 为真时 fallback 链自动跳过（不再重复打同一 URL）。
const _bridgeHost = process.env.NEXT_PUBLIC_BRIDGE_URL || 'http://127.0.0.1:8100'
export const BRIDGE_BASE = `${_bridgeHost.replace(/\/$/, '')}/api`

// Streamlit 离线复盘面板地址（dashboard/app.py）。默认空串 = 未启用，
// 前端不渲染入口按钮；本机调试可用 NEXT_PUBLIC_STREAMLIT_URL=http://127.0.0.1:8501 打开。
export const STREAMLIT_URL = (process.env.NEXT_PUBLIC_STREAMLIT_URL || '').replace(/\/$/, '')

// v310 P0 fix: 主服务与桥接**同址**时, fallback 链是死代码 ——
//   frontend/.env.local 把 NEXT_PUBLIC_API_URL 指向 8100, BRIDGE_BASE 默认也是 8100
//   → API_BASE === BRIDGE_BASE, 主服务失败后再打"桥接"就是打**同一个 URL**,
//     纯浪费一次往返 + 让错误信息出现两个来源。
//   生产(Nginx 反代)下 API_BASE='/api' ≠ BRIDGE_BASE → fallback 行为完全不变。
export const SAME_BACKEND = API_BASE === BRIDGE_BASE

// v308 P1-bugfix: 前端 GET 的总超时 (毫秒)。代码注释里一直写"前端 30s 超时",
// 但实现里从来没有超时 —— signal 只在 caller 主动传时才有。
// 后端 bridge 的硬超时是 20s (MARKET_REGIME_TIMEOUT_S), 前端留 30s 冗余,
// 让后端先吐出结构化降级结果 (ok=false + degraded=true), 前端才有东西可显示。
// 注意这是**主服务 + 桥接 fallback 共享**的预算, 不是各自 30s。
export const DEFAULT_GET_TIMEOUT_MS = 60_000

// 主服务失败时 fallback 到桥接服务的路径清单（基于历史 500 / 不可达响应）
const FALLBACK_PATHS = [
  '/events',                           // useNewsImpact → Top Entity / News Impact
  '/missed-signals',              // MissedSignalsPanel 主数据
  '/missed-signals/by-dimension',
  '/missed-signal/attention-candidates', // 漏检信号页"注意力先行候选"
  '/judgment-quality',            // MissedSignalsPanel 反思质量
  '/critical-moves',              // event-market-attribution panel
  '/stats',                       // event-market-attribution panel
  '/move-attributions',           // event-market-attribution panel (per-move drill)
  '/event-move-attribution-pending', // v334 待复核区 (bridge 提供)
  '/shadow-ledger-summary',       // v335 影子样本 (闸门没放行的单也在记账)
  '/attribution-stats',           // 桥接服务提供资金流交叉验证
  '/causal-graph',                // 桥接服务提供因果图谱
  '/calibration',                 // 桥接服务提供周维度校准
  // review/quant 历史 500 端点，由桥接服务接管
  '/review/snapshot',
  '/abnormal-signals',
  '/sources/alerts',
  '/trading/signals',
  // 模拟盘页首屏依赖的账户/持仓接口，主服务掉线时需能自动切到桥接（同一份 SQLite）
  '/replay/positions',
  '/replay/signals',
  '/replay/settings',
  '/replay/stats',
  '/cross-asset/anomaly',
  '/markets/status',
  '/impact/by-asset',             // 160 calls/d 500
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
  // lsr-panel 的 /crypto/lsr/latest 仅桥接服务提供，需进名单避免 404 空面板
  '/crypto/lsr/latest',
  // v291+ P1: 资金流聚合上下文 (桥接服务从 cftc_cot_data 直接聚合)
  '/macro/context',
  // v291+ P1: 跨资产传导链路 + 反思摘要 (桥接服务从 causal_chains/trade_reflections_v2 直接聚合)
  '/cross-asset-chains',
  '/reflections/summary',
  // 新闻影响评估 (桥接服务接管)
  '/news/impact-evaluate',
  // K线主数据 (MoversPanel chart 数据)
  '/market/history',                    // K线 bar 主体 (1m/4h/30d)
  '/calendar',                     // 日历事件 (K线 marker)
  // 桥接暂未实现但有 catch-all wrapper 的端点 (MoversPanel 其它并发 fetch)
  '/market/attribute',                  // 资产异动归因 (K线下方面板)
  '/market/movers',                     // 行情榜 (顶部分位卡片)
  '/market/depth',                      // 深度/订单簿
  '/calendar/impact-by-asset',     // 日历 K线影响验证
  '/abnormal/swing-points',             // 拐点 (swing/cum4)
  '/admin/abnormal-signals/recompute-10m', // admin 异动重算
  // v307 GUB 补齐 (2026-10-21 审计): 后端有 64 个端点, 这里补上此前漏接的。
  // 判定依据 = 后端实测有真实数据返回, 但前端 0 处调用。详见 lib/api-gub.ts。
  '/tech/signals',                      // 技术信号融合 (支撑位/多形态/分项权重)
  '/tech/structure',                    // K线形态识别 (中文形态名)
  '/tech/anomaly-history',              // 历史异动 + 形态/RSI/MACD 证据链
  '/tech/anomaly',                      // 实时异动扫描 (已过噪声过滤)
  '/health',                            // 数据新鲜度
  '/market-regime',                // COT 大型投机商仓位状态 (需求14 市场基本面)
  '/funding-rate',                 // 永续合约资金费率
  '/orderbook-depth',              // 订单簿深度
  '/event-move-attribution',       // 事件→行情归因明细 (含因果质量标注)
  '/abnormal-attribution',              // AI 异动归因明细 (事件+形态+量能 → 可执行结论)
  '/causal-chains/feedback',       // 因果链反馈闭环 + needs_recalibration 标记
  '/causal/accuracy-stats',        // 因果链命中率统计 (需求9)
  '/review/causal-graph',               // 因果图谱节点/边
]

/**
 * fetchWithFallback —— 给用裸 fetch 的组件用的 fallback 包装器
 *  - 先打 API_BASE（主服务）
 *  - 失败/500 时,如果是已知 fallback 路径,改打 BRIDGE_BASE（桥接服务）
 *  - 返回 JSON 解析结果(成功/失败都返回 ok/error 对象,不抛)
 *
 *  v308 P1 (不再透传假 ok):
 *  桥接 catch-all 对未实现端点会返 {ok: true, _bridge_fallback: true, ...空数据}。
 *  老 component 按 {ok: true} 渲染空态, 看起来像"后端跑过了、确实没数据" ——
 *  这是假成功。现在统一把 _bridge_fallback 归一化成 {ok: false, error: ...},
 *  组件会走「后端未实现 / 无真实数据」分支, 而不是把空表当结论。
 *  注意: 这里**不 throw** (保留老 component 的错误分支设计), 只改 ok 与 error;
 *  真崩的 component 仍应改用 `apiGet` + try/catch, apiGet 会 throw BRIDGE_FALLBACK。
 */
export async function fetchWithFallback<T = any>(path: string): Promise<T> {
  // v2026.10.19 P0 fix: 防御 caller 误传 /api 前缀 (base 已有 /api, 重复会拼成 /api/api/...
  // → 命中 bridge_server catch-all 返 wrapper, 真实端点 by-dimension/calibration 等被掩盖).
  // 这里 normalize 一下, 接受 '/api/xxx' 或 '/xxx' 两种写法
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
      // v310 P0 fix: 补硬超时 —— 本函数原来**完全没有超时**(apiGet 有 30s 预算,
      // 这里裸 fetch 没有上限), 后端一卡, 面板永远转圈。这正是 v308 在 apiGet 里
      // 修掉的同一个 bug, 当时漏掉了这个函数。每次 attempt 各自计时。
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), DEFAULT_GET_TIMEOUT_MS)
      try {
        // v2026.10.19 P0 fix: 显式 cache: 'no-store' — Next.js dev mode 默认
        // force-cache 裸 fetch 响应, 第一次拿到 wrapper 后即使后端修好,
        // 浏览器二次 fetch 仍命中缓存的 wrapper. 显式 no-store 强制实时打.
        const r = await fetch(`${base}${normalized}`, { cache: 'no-store', signal: ctrl.signal })
        if (!r.ok) {
          if (r.status >= 500 && attempt < 2) {
            lastErr = { ok: false, error: `${base}${path} HTTP ${r.status}` }
            continue   // 5xx + 还有重试预算
          }
          const j = await r.json().catch(() => null)
          return { ok: false, error: j?.detail || `${base}${path} HTTP ${r.status}` }
        }
        const j2 = await r.json().catch(() => null)
        if (!j2) return { ok: false, error: `bad JSON from ${base}${path}` }
        // v308 P1: 桥接 catch-all 的空壳 (ok=true + _bridge_fallback) 是「假成功」——
        // 前端会把它当成"后端跑过了、确实没数据"。这里统一降级成 ok=false,
        // 组件就会走「后端未实现 / 无真实数据」分支。保留原 shape 字段, 避免崩。
        if (typeof j2 === 'object' && (j2 as any)._bridge_fallback === true) {
          return {
            ...(j2 as any),
            ok: false,
            error: `后端未实现该接口: ${(j2 as any)._note || path}`,
          }
        }
        return j2
      } catch (e) {
        // CSP 拒绝 / 网络错误 / DNS 失败 / abort — 不能冒到外层
        // 同样视作瞬态, 重试
        // v310: abort 是**我们自己触发的超时**, 不是网络抖动 —— 直接判定超时失败,
        // 不要再重试 3 次(每次都 30s, 面板会白等 90s)。
        const aborted = ctrl.signal.aborted
        const msg = aborted
          ? `请求超时 ${Math.round(DEFAULT_GET_TIMEOUT_MS / 1000)}s: ${base}${path}`
          : (e instanceof Error ? e.message : String(e))
        if (!aborted && attempt < 2) {
          lastErr = { ok: false, error: msg }
          continue
        }
        return { ok: false, error: msg }
      } finally {
        clearTimeout(timer)
      }
    }
    return lastErr ?? { ok: false, error: `${base}${path} HTTP 5xx after retry` }
  }
  const primary = await tryOne(API_BASE)
  if (primary && primary.ok === true) return primary
  // v310 P0 fix: 主备同址(API_BASE === BRIDGE_BASE)时不要再打一次桥接 ——
  //   那就是同一个 URL, 纯浪费一次往返, 还让错误信息出现两个来源。
  if (SAME_BACKEND) return primary as T
  // v303 P0 fix: 同时检查 raw path (带 /api/) 和 normalized — caller 传 /api/events 或 /events 都行
  // FALLBACK_PATHS 条目统一带前导 /, 例如 '/events', '/missed-signals'
  if (FALLBACK_PATHS.some(p => path.startsWith(p) || path.startsWith(`/api${p}`))) {
    return await tryOne(BRIDGE_BASE) as T
  }
  return primary as T
}

// ─── v170 P0+ · 扩展 ──────────────────────────────────────────────────────

export type ApiGetOptions = Omit<RequestInit, 'method' | 'body'> & {
  /** cache 模式，默认 'no-store'（review 页全要实时）；配置/字典用 'default' */
  cache?: RequestCache
  /**
   * 整体请求超时 (毫秒)，默认 30s。传 0 关闭。
   *
   * v308 P1-bugfix: 之前这里**根本没有超时** —— signal 只是从 caller 透传,
   * caller 不给就无限等。代码注释里到处写"前端 30s 超时", 但前端从来没实现过,
   * 于是一个卡住的后端 = 面板永远转圈。现在真正实现它。
   */
  timeoutMs?: number
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
  const { cache = 'no-store', signal, timeoutMs = DEFAULT_GET_TIMEOUT_MS, ...rest } = opts
  // v2026.10.19 P0 fix: 防御 caller 误传 /api 前缀 (base 已有 /api, 重复会拼成 /api/api/...
  // → 命中 bridge_server catch-all 返 wrapper, 真实端点 by-dimension/calibration 等被掩盖).
  const normalized = path.startsWith('/api/') ? path.slice(4) : path
  const url = `${API_BASE}${normalized}`
  // v308 P1-bugfix: 真正实现超时 (之前只是注释里写过, 代码里没有)。
  // 主服务 + 桥接 fallback **共享同一个总预算** —— 否则主服务卡满 30s 后桥接再卡 30s,
  // 面板要等 60s 才报错。用 deadline 统一约束。
  const deadline = timeoutMs > 0 ? Date.now() + timeoutMs : null
  const remaining = () => (deadline === null ? Infinity : Math.max(0, deadline - Date.now()))
  const fetchWithTimeout = async (target: string): Promise<Response> => {
    const left = remaining()
    if (left <= 0) {
      return new Response(JSON.stringify({ detail: 'timeout' }), { status: 599 })
    }
    // caller 自己传了 signal 就用它; 否则我们自己造一个带 timeout 的
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), left)
    const callerSignal = signal
    const onCallerAbort = () => ctrl.abort()
    if (callerSignal) {
      if (callerSignal.aborted) ctrl.abort()
      else callerSignal.addEventListener('abort', onCallerAbort, { once: true })
    }
    try {
      return await fetch(target, { cache, signal: ctrl.signal, ...rest })
    } finally {
      clearTimeout(timer)
      callerSignal?.removeEventListener('abort', onCallerAbort)
    }
  }
  let response: Response
  try {
    response = await fetchWithTimeout(url)
  } catch (e) {
    response = new Response(JSON.stringify({ detail: String(e) }), { status: 599 })
  }
  // 主服务失败时,fallback 到桥接服务(8100)
  // v303 P0 fix: 同时检查 raw path (带 /api/) 和 normalized — caller 传 /api/events 或 /events 都行
  // v310 P0 fix: SAME_BACKEND 时跳过 —— 主备同址, 再打一次就是同一个 URL。
  if (!response.ok && !SAME_BACKEND && FALLBACK_PATHS.some(p => path.startsWith(p) || path.startsWith(`/api${p}`))) {
    try {
      const fbUrl = `${BRIDGE_BASE}${normalized}`
      // v308 P1-bugfix: 桥接也必须走同一个超时预算 (fetchWithTimeout)。
      // 之前这里是裸 fetch, 主服务卡满 30s 之后桥接还能再卡 30s。
      const fbResp = await fetchWithTimeout(fbUrl)
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
  if (!response.ok) {
    // v308 P1-bugfix: 超时和普通失败要能区分开, 否则面板只能显示 "599" 这种
    // 无信息量的状态码, 用户不知道是"后端慢"还是"后端挂了"。
    if (response.status === 599) {
      throw new Error(`${path} 请求超时或网络不可达 (${timeoutMs > 0 ? `${Math.round(timeoutMs / 1000)}s` : '无上限'})`)
    }
    throw new Error(`${path} ${response.status}`)
  }
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
  /**
   * 写操作在「网络错误(压根没收到任何响应)」时, 是否仍允许 fallback 到桥接重发。
   *
   * 默认 false —— 因为「没收到响应」**不等于**「服务端没执行」:
   * 响应在回程丢失/连接被重置时, 写操作其实已经落库了, 再发一次就是重复写入。
   * 只有确认幂等的操作 (例如「确保某条记录存在」) 才应该打开这个开关。
   *
   * 注意这与 404 不同: 404 明确表示「路由不存在, 肯定没执行」, 那种情况
   * 无论如何都会 fallback (见 apiSend 内部注释)。
   */
  allowRetryOnNetworkError?: boolean
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
  const { cache = 'no-store', allowRetryOnNetworkError = false, ...rest } = opts
  const init: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
    cache,
    ...rest,
  }
  if (body !== undefined) init.body = JSON.stringify(body)
  // v2026.10.19 P0 fix: 同 apiGet, 防御 /api 前缀
  const normalized = path.startsWith('/api/') ? path.slice(4) : path
  const sendOne = async (base: string): Promise<T> => {
    let response: Response
    try {
      response = await fetch(`${base}${normalized}`, init)
    } catch (e) {
      // v308 P1-bugfix: 区分「网络错误(没收到任何响应)」与「收到响应但处理失败」。
      // 前者写操作**可能**已经在服务端执行了 (响应回程丢失), 不能盲目重发。
      // 标记 networkError=true, 由调用方决定要不要重试。
      const err = new Error(e instanceof Error ? e.message : String(e)) as Error & {
        status?: number
        networkError?: boolean
      }
      err.networkError = true
      throw err
    }
    if (!response.ok) {
      const detail = await response.text().catch(() => '')
      const err = new Error(detail || `${path} ${method} ${response.status}`) as Error & {
        status?: number
        networkError?: boolean
      }
      err.status = response.status
      throw err
    }
    // 204 No Content 等不返回 JSON
    const text = await response.text()
    let parsed: T
    try {
      parsed = (text ? JSON.parse(text) : (undefined as unknown)) as T
    } catch (e) {
      // v308 P1-bugfix: 之前这里直接 JSON.parse 抛 SyntaxError, 该错误**没有 status**,
      // 于是被下面的 "status == null → 确定没执行" 判定为可重试 → 向桥接重发写操作。
      // 但 HTTP 200 说明服务端**已经执行并可能已落库**, 重发 = 重复写入。
      // 现在显式标记 status=200, 让它走「不重试」分支。
      const err = new Error(`响应不是合法 JSON: ${path} ${method} (HTTP ${response.status})`) as Error & {
        status?: number
        networkError?: boolean
      }
      err.status = response.status
      throw err
    }
    // P0 fix: 同 apiGet, 桥接 catch-all wrapper 抛错
    if (parsed && typeof parsed === 'object' && (parsed as any)._bridge_fallback === true) {
      throw new Error(`BRIDGE_FALLBACK: ${method} ${path} (${(parsed as any)._note || 'not implemented'})`)
    }
    return parsed
  }
  try {
    return await sendOne(API_BASE)
  } catch (e) {
    // v308 P1: 写操作也走 fallback 链 (主服务 stale 时, 很多写端点 404)。
    //
    // v308 P1-bugfix 判定细化 —— 只有「确定没执行」才允许重发:
    //   · 404          → 路由不存在, 服务端根本没这个接口, 肯定没执行。安全重发。
    //   · 网络错误      → 响应没回来, **可能已经落库**了。默认不重发;
    //                   调用方显式传 allowRetryOnNetworkError=true 才重发。
    //   · 其它状态码    → 服务端回了错误 (4xx/5xx), 可能"已写入但响应失败"。不重发。
    //   · JSON 解析失败 → HTTP 2xx, 服务端已执行。不重发。
    const err = e as Error & { status?: number; networkError?: boolean }
    const status = err?.status
    const isNotFound = status === 404
    const isNetworkError = err?.networkError === true
    const definitelyNotExecuted = isNotFound || (isNetworkError && allowRetryOnNetworkError)
    // v310 P0 fix: SAME_BACKEND 时跳过 —— 主备同址, 再发一次 = 同一个 URL 重复写入。
    if (definitelyNotExecuted && !SAME_BACKEND && FALLBACK_PATHS.some(p => path.startsWith(p) || path.startsWith(`/api${p}`))) {
      try {
        return await sendOne(BRIDGE_BASE)
      } catch {
        // 桥接也失败 → 保留主服务的错误信息
      }
    }
    throw e
  }
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
