/**
 * lib/backend-registry.ts
 *
 * 后端能力台账 —— 2026-11-02 全量审计产出。
 *
 * 背景: 后端 383 个 HTTP 端点里, 有 158 个**前端零引用**(后端在写、在 cron 里跑,
 * 但界面上找不到入口)。其中一部分是真正的高价值能力被藏起来了
 * (signal_weight_history 43107 行 / ai_call_stats 380525 行 /
 *  strategy_param_adjustments 119 条待审批 / kg_improvements 159 条提案),
 * 另一部分是内部运维机制或已无数据的功能, 不该占用界面。
 *
 * 本文件是唯一的判定来源:
 *   status='exposed'  → 已归位到某个前端页面(见 home), 由
 *                        components/quant/backend-capability-panel.tsx 渲染
 *   status='downline'  → 判定下线, 前端不提供入口, 端点保留(不删代码, 避免破坏
 *                        cron / 外部调用), 理由见 verdict
 *
 * 审计数据来源(2026-11-02):
 *   - 后端端点扫描: backend/src_python 全量 @app.get/post/put/delete
 *   - 前端引用扫描: frontend 全量 .ts/.tsx (参数路径转正则后匹配)
 *   - 数据量核验: backend/trident_event_bus.db 表行数
 */

export type BackendDomainStatus = 'exposed' | 'downline'

export type BackendDomain = {
  /** 域 key, 与 components/quant/backend-capability-panel.tsx 的 domain prop 对应 */
  key: string
  /** 界面标题 */
  label: string
  /** 图标(emoji), 与现有面板风格一致 */
  icon: string
  /** 归位到哪个前端页面(路由) */
  home: string
  status: BackendDomainStatus
  /** 这组后端能力在做什么 */
  summary: string
  /** 后端数据依据 —— 表名 + 行数, 让"有没有数据"可验证 */
  backing?: string
  /** 该域涉及的后端端点(相对 API_BASE, 不含 /api 前缀) */
  endpoints: string[]
  /** 归位/下线的判定依据 */
  verdict: string
}

export const BACKEND_DOMAINS: BackendDomain[] = [
  // ══════════════════════════════════════════════════════════════════
  // 已归位 (status='exposed')
  // ══════════════════════════════════════════════════════════════════
  {
    key: 'beliefs',
    label: '信念系统',
    icon: '🧠',
    home: '/connectivity',
    status: 'exposed',
    summary: 'AI 对市场运行机制形成的「假设」及其证据链 —— 可分解、可验证、可修订, 是因果图谱的上游。',
    backing: 'beliefs 14 · belief_edges 13 · belief_history 425 (belief-maintenance-cron 每 6h 跑)',
    endpoints: [
      'beliefs',
      'beliefs/revise-all',
      'beliefs/{belief_id}',
      'beliefs/{belief_id}/decompose',
      'beliefs/{belief_id}/external-coverage',
      'beliefs/{belief_id}/knowledge-gaps',
      'beliefs/{belief_id}/lineage',
      'beliefs/{belief_id}/revise',
      'beliefs/{belief_id}/verify',
      'knowledge-gaps',
    ],
    verdict: 'belief-maintenance-cron 每 6h 自动修订信念(425 条历史), 历史上界面完全没有入口; v337 已归位到「后台 → 联通」页(信念图谱与资产驱动图谱同源, 但不进产品页)。',
  },
  {
    key: 'kg-improvements',
    label: '图谱改进提案',
    icon: '🔧',
    home: '/review',
    status: 'exposed',
    summary: 'AI 从归因错误中反推出的因果图谱改进提案(补边/改权重), 需人工批准后由 reconcile worker 落库。',
    backing: 'kg_improvements 159 条 (kg-review-cron 持续产出)',
    endpoints: [
      'analysis/kg-improvements',
      'analysis/kg-improvements/auto-approve',
      'analysis/kg-improvements/{proposal_id}/approve',
      'analysis/kg-improvements/{proposal_id}/reject',
      'causal-graph/hypothesis',
    ],
    verdict: '★静态扫描假阳性更正: approve/reject 其实**已接线** —— components/replay/analysis-improve-panel.tsx 用模板串 `/analysis/kg-improvements/${id}/${decision}` 调用, 静态匹配看不见插值路径, 于是被误判为「零引用」。真实剩余缺口是 auto-approve 与 causal-graph/hypothesis。列表与审批面板都在 /review 的「改进」段, 无需新增面板。',
  },
  {
    key: 'signal-weights',
    label: '信号权重',
    icon: '⚖️',
    home: '/missed-signals',
    status: 'exposed',
    summary: '每条信号决策的置信权重及其拆解公式(base × 质量几何均值 × 冲击系数), 以及信息源命中率排行榜。',
    backing: 'signal_weight_history 43107 · signal_weight_cron_log 1774 (signalweightcron 在跑)',
    endpoints: [
      'signal-weights',
      'signal-weights/recompute',
      'signal-weights/stats',
      'signal-weights/{decision_id}',
    ],
    verdict: '权重评估是漏检信号的归因基础(为什么这条信号被压权), 与漏检统计同源 —— 归位到「漏检信号」页。注意 stats 端点在 WAL 写锁高峰期可达 40s, 面板必须懒加载 + 加长超时。',
  },
  {
    key: 'llm-stats',
    label: 'LLM 调用统计',
    icon: '🤖',
    home: '/connectivity',
    status: 'exposed',
    summary: '按用途/密钥/调用点分组的 token 消耗、调用次数与延迟, 以及成功调用占比。',
    backing: 'ai_call_stats 380525 · monitor_llm_value 115',
    endpoints: [
      'v187/llm-stats/summary',
      'v187/llm-stats/by-key',
      'v187/llm-stats/by-purpose',
      'v187/llm-stats/by-call-site',
      'v187/llm-stats/timeline',
    ],
    verdict: '现在 LLM 通道全挂, 首页「LLM 渠道」只能看到红叉, 看不到「哪条链路在烧 token」—— 归位到「联通」页(后台健康检查区), 用 token/调用量定位是哪条链路在拖后腿。',
  },
  {
    key: 'paper-approvals',
    label: '模拟盘参数审批',
    icon: '✅',
    home: '/replay',
    status: 'exposed',
    summary: '反思系统产出的策略参数调整建议(改阈值/改暴露), 以及策略级周期反思 —— 审批后才会真正写进模拟盘 settings 或策略新版本。',
    backing: 'strategy_param_adjustments 119 条待处理 · strategy_reflections 4',
    endpoints: [
      'paper/adjustments',
      'paper/adjustments/{adj_id}/approve',
      'paper/adjustments/{adj_id}/reject',
      'paper/buy-sell-kpi',
      'paper/strategy-reflections',
      'paper/strategy-reflections/generate',
      'paper/strategy-reflections/{reflection_id}/approve',
      'paper/strategy-reflections/{reflection_id}/reject',
    ],
    verdict: '★最高价值缺口(已修): 119 条参数调整建议卡在 pending。审计发现 components/replay/reflection-improve-panel.tsx **拉取了 adjustments 却从不渲染**, 且其审批调用打在不存在的端点 `/paper/strategy-reflections/{id}/review?action=` (后端只有 /approve 与 /reject) —— 该按钮恒定 404。2026-11-02 已修端点并补上参数调整的批准/驳回 UI, 归位到「模拟盘」页。',
  },
  {
    key: 'strategies',
    label: '策略版本管理',
    icon: '📐',
    home: '/replay',
    status: 'exposed',
    summary: '当前生效策略及其参数快照、历史版本列表、归档与默认参数模板。',
    backing: 'strategies 3 · strategy_versions 3',
    endpoints: ['strategies/current', 'strategies/meta/defaults', 'strategies/{strategy_id}/archive'],
    verdict: '策略版本是「参数调整审批」的落点(approve 会 strategy_store.add_version), 必须与审批同页可见 —— 归位到「模拟盘」页。',
  },
  {
    key: 'trading-manager',
    label: '交易管理器',
    icon: '🛠️',
    home: '/replay',
    status: 'exposed',
    summary: '纸面交易引擎的启停、运行日志与最新成交快照。',
    backing: 'paper_trading_runs 2',
    endpoints: [
      'trading/latest',
      'trading/signals',
      'trading/manager/logs',
      'trading/manager/start',
      'trading/manager/status',
      'trading/manager/stop',
    ],
    verdict: '与模拟盘同属一套引擎, 但启停只能从命令行操作 —— 归位到「模拟盘」页(折叠区)。数据量小, 优先级低。',
  },
  {
    key: 'reflection-deep',
    label: '深度反思',
    icon: '🔬',
    home: '/review',
    status: 'exposed',
    summary: 'v230 深度反思: 按 K线形态/运气区间/抢跑程度/跨资产同步四维分桶的胜率, 以及「这是运气还是信号」的判定。',
    backing: 'trade_reflections_v2 (enrich ok 280 / pending 1964)',
    endpoints: [
      'reflection/deep/stats',
      'reflection/deep/{reflection_id}',
      'reflection/deep/enrich/{reflection_id}',
      'reflection/deep/enrich-pending',
      'reflection/meta-loop/stats',
      'reflection/meta-loop/run',
      'reflection/reasoning-logic-blindspot/scan',
      'reflection/reasoning-logic-blindspot/detect',
      'structured_reflection/status',
      'reasoning/narrative/list',
      'reasoning/narrative/{decision_id}',
      'reasoning/narrative/{decision_id}/generate',
      'reasoning/decision/{decision_id}/text',
      'reasoning/review/list',
      'reasoning/review/segment-history',
      'reasoning/review/{decision_id}',
      'structured_reflections',
    ],
    verdict: '「运气 vs 信号」的胜率分桶是反思系统的核心结论, 但前端只展示明细不展示统计 —— 归位到「反思系统」页。注意 deep/stats 在高并发下会瞬时返回 ok:false, 面板需按「后端未就绪」而非「无数据」渲染。',
  },
  {
    key: 'review-insights',
    label: '复盘洞察与经验库',
    icon: '📚',
    home: '/review',
    status: 'exposed',
    summary: '洞察条目的启停与来源追踪、归因人工反馈、因果图谱证据持久化、经验库归档与统计。',
    backing: 'review_reports 138 · trade_reflections 0 · experience_entries 0',
    endpoints: [
      'review/attributions/auto-review',
      'review/attributions/{attribution_id}/feedback',
      'review/insights/by-origin/{origin}',
      'review/insights/{insight_id}',
      'review/insights/{insight_id}/toggle',
      'review/causal-graph/stats',
      'review/causal-graph/knowledge/{asset}',
      'review/causal-graph/link-cross-asset',
      'review/causal-graph/persist-evidence',
      'review/causal-graph/seed-from-chains',
      'review/causal-graph/update-weights',
      'v186/experiences/stats',
      'v186/experience-reflect/run',
      'v186/gate-reflection-cycle',
      'v186/gate-reflection-cycle/run',
      'v186/paper-reflection/run',
      'v186/persona-stats',
      'v186/trade-reflections',
      'v188/experiences/{exp_id}/archive',
      'v188/experiences/{exp_id}/stats',
      'v188/experiences/{exp_id}/update',
      'v237/reflection-dimension-map',
      'v237/reflection-dimension-stats',
      'policy/patches/{patch_id}/review',
    ],
    verdict: '洞察开关 + 归因反馈是反思系统的人工纠偏入口; 经验库表当前为 0 行(上游 trade_reflections 空), 故经验库相关端点归位但会显示「暂无数据」。',
  },
  {
    key: 'backtest',
    label: '回测运行记录',
    icon: '⏪',
    home: '/backtest',
    status: 'exposed',
    summary: '回测运行历史、当前活跃 run、归档与清理。',
    backing: 'backtest_runs 0 (前端 /backtest 页可直接发起新回测)',
    endpoints: ['backtest/runs', 'v189/backtest/active_run', 'v189/backtest/archive', 'v189/backtest/cleanup'],
    verdict: '/backtest 是一个**真实功能页**(131 行, 能选策略/跑回测/优化参数)但**导航与站内都没有入口** —— 只能手敲 URL, 属于典型的位置设置错误。2026-11-02 已在「模拟盘」页加二级入口(不新增顶部 tab)。',
  },
  {
    key: 'history-review',
    label: '历史交易回溯',
    icon: '📜',
    home: '/history',
    status: 'exposed',
    summary: '按新闻回看已结束交易 —— 该新闻触发了哪些交易、胜负如何、AI 归因总结, 支持重判与人工复盘。',
    backing: 'news_events 18481 · decision_evidence 9473',
    endpoints: [
      'history/news-trades',
      'history/news-trades/pending-count',
      'history/news-trades/rejudge-batch',
      'history/news-trades/{news_id}',
      'history/news-trades/{news_id}/review',
      'history/news-trades/{news_id}/rejudge',
    ],
    verdict: '/history 是**真实功能页(622 行)**但**导航与站内都没有入口** —— 只能手敲 URL。2026-11-02 已在「反思系统」页加二级入口(不新增顶部 tab)。列表与 review 已被 /history 页引用, 真正缺的是 pending-count / rejudge / rejudge-batch 这三个动作端点。',
  },
  {
    key: 'kb',
    label: '知识库维度',
    icon: '📚',
    home: '/knowledge-graph',
    status: 'exposed',
    summary: '知识库的维度表(资产/事件类型)、按维度取条目、全文检索与知识库统计。',
    backing: 'dim_asset 11 · dim_event_type 27 · kline_snapshots 25',
    endpoints: ['kb/asset/{asset}', 'kb/dim/{dim_code}', 'kb/search', 'kb/stats'],
    verdict: '知识库是因果图谱的证据来源, 与图谱/提案同页 —— 归位到「因果图谱」页。',
  },
  {
    key: 'macro-market',
    label: '宏观与跨市场行情',
    icon: '🌐',
    home: '/discover',
    status: 'exposed',
    summary: '美债 10Y/5Y、收益率曲线价差、ES/NQ 期货、永续资金费率、宏观日历快照。',
    backing: '行情由 market_ticks 507776 / kline_bars 支撑',
    endpoints: [
      'macro/calendar',
      'v188/macro/snapshot',
      'v188/markets/status/{asset}',
      'v189/market/es',
      'v189/market/nq',
      'v189/market/us10y',
      'v189/market/us5y',
      'v189/market/yield-curve-spread',
      'v189/market/funding-rate',
      'market/price-at',
    ],
    verdict: '/discover 的「日内流动性情绪」段已经在展示这批指标, 但走的是面板内自建路径, 后端这组端点零引用 —— 归位到 /discover, 作为该段的直连数据源。',
  },
  {
    key: 'discovery-analytics',
    label: '发现域分析',
    icon: '🔭',
    home: '/discover',
    status: 'exposed',
    summary: '跨资产排名、周期分析与叙事、价值低估检测、以及人群画像的主动采集。',
    backing: 'discovery-digest-cron + crowd-pulse 在跑',
    endpoints: [
      'discovery/cross-asset-rankings',
      'discovery/cycle-analysis/{asset}',
      'discovery/cycle-narrative/{asset}',
      'discovery/undervalued-detector',
      'crowd/collect',
    ],
    verdict: '「发现」页已有 12 个 section, 但这组分析端点前端零调用 —— 归位到 /discover, 补上发现页的分析能力。',
  },
  {
    key: 'news-exports',
    label: '新闻导出报表',
    icon: '📤',
    home: '/news',
    status: 'exposed',
    summary: '按批次导出新闻为报表文件、导出延迟监控、导出设置与批次列表。',
    backing: 'news_events 18481',
    endpoints: [
      'news/exports',
      'news/exports/run',
      'news/exports/open',
      'news/exports/settings',
      'news/exports/latency',
      'news/exports/download/{batch}/{filename:path}',
    ],
    verdict: '后端有完整导出链路(跑批/下载/延迟监控)但界面没有入口 —— 归位到「新闻流」页。',
  },
  {
    key: 'ai-prompts',
    label: 'AI 提示词与资产规则',
    icon: '📝',
    home: '/prompts',
    status: 'exposed',
    summary: '主提示词按用途覆盖、提示词启停、资产级规则与规则开关、关键词过滤器的增删。',
    backing: 'ai_main_prompts 表存在但当前 0 行',
    endpoints: [
      'ai/prompts/main/{kind}',
      'ai/prompts/{prompt_id}',
      'ai/prompts/{prompt_id}/toggle',
      'ai/asset-rules/{rule_id}',
      'ai/asset-rules/{rule_id}/toggle',
      'filter/keywords/{keyword_id}',
    ],
    verdict: '★已在位: /prompts 页(components/ai-prompts-panel.tsx + asset-rules-panel.tsx)已在用 /ai/prompts、/ai/asset-rules、/filter/keywords。真正缺的是 /ai/prompts/main/{kind}(主提示词按用途覆盖)与 /ai/prompts/{prompt_id}/toggle —— ai_main_prompts 表当前 0 行, 归位前需确认是否有独立存储。',
  },
  {
    key: 'exchange-mode',
    label: '交易所模式控制',
    icon: '🏦',
    home: '/connectivity',
    status: 'exposed',
    summary: '实盘/纸面模式切换、凭据状态、每日重置与汇总、信号归档、审计流水。',
    backing: 'exchange 相关表当前不存在(功能未启用)',
    endpoints: [
      'v190/exchange/status',
      'v190/exchange/credentials',
      'v190/exchange/audit',
      'v190/exchange/daily_resets',
      'v190/exchange/daily_summary',
      'v190/exchange/mode/start',
      'v190/exchange/mode/stop',
      'v190/exchange/stop_all',
      'v190/exchange/signal_archive',
    ],
    verdict: '实盘开关是**高危操作**, 必须可见可控, 不能只存在于命令行 —— 归位到「联通」页(后台区), 明确标注为实盘开关。',
  },
  {
    key: 'ops-inspection',
    label: '数据一致性巡检',
    icon: '🔎',
    home: '/connectivity',
    status: 'exposed',
    summary: 'ID 对账巡检(状态/检查/告警)、数据库统计、各子系统健康探针。',
    backing: 'id-reconcile-cron 每 300s 跑',
    endpoints: [
      'admin/id-reconcile/status',
      'admin/id-reconcile/check',
      'admin/id-reconcile/alerts',
      'admin/id-reconcile/resolve',
      'admin/id-reconcile/backfill',
      'admin/id-reconcile/migrate',
      'v188/admin/db-stats',
      'master/health',
      'health/internal',
      'health/security',
      'health/tournament',
    ],
    verdict: 'id-reconcile 每 5 分钟跑一次对账, 巡检结果没有任何界面出口 —— 归位到「联通」页。resolve/backfill/migrate 属写操作, 面板内仅暴露 status/check/alerts 只读视图。',
  },
  {
    key: 'misc-ops',
    label: '运维动作集合',
    icon: '🧰',
    home: '/connectivity',
    status: 'exposed',
    summary: 'OHLCV 刷新、日内数据回填/同步、外部数据摄取、异动回填、决策结算、决策已读标记、指标重算、交易日记、事件机会点。',
    backing: '按需触发型端点',
    endpoints: [
      'dashboard/overview',
      'ohlcv/refresh-now',
      'intraday/backfill',
      'intraday/sync',
      'abnormal/backfill',
      'external/fetch-and-ingest',
      'external/ingest',
      'decision/auto-settle-now',
      'decisions/mark-all-read',
      'decisions/{decision_id}/mark-read',
      'v188/indicators',
      'v188/indicators/run',
      'v189/causal/expand-from-insights',
      'v201/event/opportunities',
      'v201/trade-points',
    ],
    verdict: '这批是「手动触发型运维按钮」, 散落在各处无人调用 —— 归位到「联通」页的统一运维区。',
  },
  {
    key: 'journal',
    label: '交易日记',
    icon: '📔',
    home: '/review',
    status: 'exposed',
    summary: 'v189 交易日记 —— 按日记录当天的决策与理由, 供事后复盘追溯。',
    backing: 'journal 按日写入',
    endpoints: ['v189/journal', 'v189/journal/record', 'v189/journal/rollup', 'v189/journal/{date}'],
    verdict: '原 /journal 顶层 tab 已按 v170 口径并入 /review?tab=journal(见 components/app-nav.tsx 注释), 但底层 v189/journal 这组端点前端一次都没调用 —— 「日记」页签拿不到数据。归位到「反思系统」页。',
  },
  {
    key: 'event-atoms',
    label: '事件原子详情',
    icon: '⚛️',
    home: '/attribution',
    status: 'exposed',
    summary: '单个事件原子的详情与其影响曲线(事件 → 价格反应的量化轨迹)。',
    backing: 'event_atom 3041 · event_impact_curve 4384',
    endpoints: [
      'event_atoms/{atom_id}',
      'event_impact_curves',
      'event_impact_curves/by_atom/{atom_id}',
    ],
    verdict: '★已在位: 这几个端点前端**已在使用** —— lib/api.ts 封装了 `/event_atoms/${atomId}` 与 `/event_impact_curves/by_atom/${atomId}`, 供 /attribution 的异动归因面板消费(event_atom 3041 · event_impact_curve 4384); 影响曲线列表端点 /event_impact_curves 目前仍零引用。归位结论: 无需新增面板, 保持现状。',
  },
  {
    key: 'market-series',
    label: '行情时序取数',
    icon: '📈',
    home: '/attribution',
    status: 'exposed',
    summary: '指定标的的 OHLC / tick 序列, 按 symbol 取 K 线, 以及因子/新闻/胜率时序。',
    backing: 'market_ticks 507776 · kline_bars 已有 1h/4h/1d',
    endpoints: [
      'timeseries/ohlc/{symbol}',
      'timeseries/ticks/{symbol}',
      'klines/{symbol}',
      'timeseries/factors',
      'timeseries/news',
      'timeseries/winrate',
    ],
    verdict: '归位到「行情归因」页, 作为异动归因面板的明细取数通道(当前 MoversPanel 自己拼 K 线, 未复用后端这条标准通道)。',
  },
  {
    key: 'causal-chains',
    label: '因果链干预',
    icon: '⛓️',
    home: '/knowledge-graph',
    status: 'exposed',
    summary: '因果链的详情、启停与权重调整。',
    backing: 'causal_chains / causal_chain_outcomes 804',
    endpoints: ['causal/chains/{chain_id}', 'causal/chains/{chain_id}/toggle', 'causal/chains/{chain_id}/weight'],
    verdict: '★已在位: 这两个端点前端**已在使用** —— components/causal-chains-panel.tsx 调 `${API_BASE}/causal/chains/${item.id}/weight` 与 `/toggle`, 且该面板挂在 /knowledge-graph 页(components/knowledge-graph-panel.tsx)。静态扫描未判为孤儿是因为它与其它路径共享 lib/api.ts。归位结论: 无需新增面板, 保持现状。',
  },

  // ══════════════════════════════════════════════════════════════════
  // 判定下线 (status='downline') —— 前端不提供入口, 端点保留
  // ══════════════════════════════════════════════════════════════════
  {
    key: 'security-gateway',
    label: '安全网关与水印',
    icon: '🔐',
    home: '—',
    status: 'downline',
    summary: '安全挑战、入口校验、响应水印 —— 属于服务端反爬机制本身。',
    endpoints: ['security/challenge', 'security/challenge/verify', 'security/entry', 'security/watermark'],
    verdict: '下线: 这是防护机制而非业务功能, 界面暴露反而是信息泄露(告诉攻击者校验逻辑长什么样)。',
  },
  {
    key: 'coinglass-lsr',
    label: 'Coinglass 多空比',
    icon: '🐋',
    home: '—',
    status: 'downline',
    summary: 'Coinglass 衍生品多空比的历史/最新/排行与健康度。',
    endpoints: ['v189/coinglass/health', 'v189/coinglass/lsr/history', 'v189/coinglass/lsr/latest', 'v189/coinglass/lsr/ranking'],
    verdict: '下线: coinglass_lsr_samples 表 **0 行** —— 数据源从未落库, 展示出来只会是一张空表。要么先修采集再上界面, 要么保持下线。',
  },
  {
    key: 'news-sources',
    label: '新闻采集源直控',
    icon: '📡',
    home: '—',
    status: 'downline',
    summary: 'Blockbeats/东财/金十三个采集源的直接抓取入口, 以及新闻源配置与报告。',
    endpoints: ['news/blockbeats', 'news/eastmoney', 'news/qinghong', 'news/sources', 'news/report', 'news/mark-all-read'],
    verdict: '下线: 这些是 cron 采集链的内部触发口, 已有 /news/pull-now 与「信息源」页承担同等职责, 再开手动入口只会让人误触导致重复灌库。',
  },
  {
    key: 'admin-legacy',
    label: '旧版管理动作',
    icon: '🗃️',
    home: '—',
    status: 'downline',
    summary: '封禁、洞察晋级、摆动回写、访客统计、日归档等历史管理动作。',
    endpoints: [
      'admin/block',
      'admin/insight/promote-now',
      'admin/insight/weekly-status',
      'admin/swing-writeback',
      'admin/visitors',
      'v188/admin/daily-archive',
    ],
    verdict: '下线: 对应数据表均已不存在(admin_audit_log 等), 或功能已被 v186+ 的反思链取代, 属于历史遗留。注: /api/backups/{filename} 原先也列在此处, 但 /replay 页的备份下载已在用它 —— 已移出本域。',
  },
  {
    key: 'analysis-process-page',
    label: '分析过程页取数',
    icon: '🕸️',
    home: '/analysis-process',
    status: 'exposed',
    summary: '分析过程页的单条新闻详情、近期分析列表与健康检查。',
    endpoints: ['analysis-process/news/{news_id}', 'analysis-process/healthcheck', 'analysis-process/recent'],
    verdict: '★已在位: /analysis-process 页(lib/analysis-process-data.ts + components/analysis-process/analysis-process-panel.tsx)已在用 /analysis-process/news/{id}、/recent、/healthcheck。归位结论: 无需新增面板, 保持现状。',
  },
]

// ══════════════════════════════════════════════════════════════════════
// 工具函数
// ══════════════════════════════════════════════════════════════════════

/**
 * 已在界面落地的域 key(2026-11-02)。
 *
 * 区分两件事:
 *   - BACKEND_DOMAINS 里 status='exposed' = **已判定**归位到哪个页面(台账结论);
 *   - 这里的 WIRED_DOMAINS = **实际已经**在界面上有入口(通用面板已挂载, 或
 *     该能力本来就已在既有面板里)。
 *
 * 未出现在此集合里的 exposed 域 = 结论已定、待分批接线(端点与页面归属都已明确,
 *  不需要再做判定, 只需要实现面板)。
 */
export const WIRED_DOMAINS = new Set<string>([
  // 本次通过通用面板接入的
  'beliefs',
  'signal-weights',
  'llm-stats',
  'ops-inspection',
  'exchange-mode',
  'reflection-deep',
  'journal',
  'strategies',
  // 本次修复的闭环断点
  'paper-approvals',
  // 本来就已在既有面板里的(审计确认无需新增面板)
  'kg-improvements',
  'causal-chains',
  'event-atoms',
  'ai-prompts',
  'analysis-process-page',
])

/** 该域是否已在界面落地 */
export function isWired(key: string): boolean {
  return WIRED_DOMAINS.has(key)
}

/** 某个前端页面上应当渲染的域(仅 exposed) */
export function domainsForHome(home: string): BackendDomain[] {
  return BACKEND_DOMAINS.filter((d) => d.status === 'exposed' && d.home === home)
}

/** 按 key 取域 */
export function getDomain(key: string): BackendDomain | undefined {
  return BACKEND_DOMAINS.find((d) => d.key === key)
}

/** 全部端点总数(含 downline) */
export function totalEndpointCount(): number {
  return BACKEND_DOMAINS.reduce((n, d) => n + d.endpoints.length, 0)
}

/** 台账自检: 端点是否被重复登记 */
export function findDuplicateEndpoints(): string[] {
  const seen = new Map<string, string[]>()
  for (const d of BACKEND_DOMAINS) {
    for (const e of d.endpoints) {
      const arr = seen.get(e) || []
      arr.push(d.key)
      seen.set(e, arr)
    }
  }
  return [...seen.entries()].filter(([, v]) => v.length > 1).map(([k]) => k)
}
