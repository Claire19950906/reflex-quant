// /review 总览带 (ReviewOverview) 类型定义
//
// 设计动机：
//   ❶ 4 处独立 panel 都拿到 /review/snapshot 的同一份 shape（paper + quick_sim + today +
//      window），原本各自定义结构、同步问题靠口头约定 → 改一处忘记同步别处是高频 bug。
//   ❷ PaperStats 里 winrate_pct vs winrate 两种标度并存、ProfitLossRatio 后端用字符串
//      序列化 "Infinity"，这些"非显然约束"集中在类型层就不会散落在每个消费者。
//
// 约束（务必遵守）：
//   - 任何改 PaperStats / QuickSimOverall 字段的 PR，必须同步 /review/snapshot 后端 schema
//   - winrate_pct 是 0-100 标度（52.6 = 52.6%），不要与 decimal winrate（0.526）混用
//   - profit_loss_ratio 后端 JSON 不允许 Infinity，用字符串 "Infinity" / "∞" 表达

// ──────────────────────────────────────────────────────────────────────
// § 1 · A · 真实持仓战绩 (paper-trading 结算口)
// ──────────────────────────────────────────────────────────────────────

export type PaperStats = {
  total?: number
  tracking?: number
  settled?: number
  wins?: number
  losses?: number
  holds?: number

  /**
   * decimal 形式胜率 (0.526) — 保留以兼容其他消费方。
   * v300+ P0+ 后总览带已统一改读 winrate_pct，禁止新建消费者用此字段。
   * @deprecated 新代码请用 winrate_pct（0-100 标度，同口径同单位）
   */
  winrate?: number | null

  /** 0-100 标度（如 52.6 = 52.6%）。A / B / 总览统一用此。 */
  winrate_pct?: number | null

  avg_pnl?: number | null

  /**
   * 盈亏比 avg_win / |avg_loss|。
   * 后端用字符串表达（"Infinity"/"∞"）因为 JSON 不允许 Infinity。
   * @see fmtRatio in review-overview.tsx
   */
  profit_loss_ratio?: number | string | null

  /** 期望值 wr*avg_win + (1-wr)*avg_loss，单位 pct（5.0 = 5%/笔）。 */
  expected_value_per_trade?: number | null

  /** '正向' | '负向' | '中性' | '数据不足' */
  expectancy_label?: string | null
}

// ──────────────────────────────────────────────────────────────────────
// § 2 · B · 快速评测综合 (quick-sim snapshot)
// ──────────────────────────────────────────────────────────────────────

export type QuickSimOverall = {
  total?: number
  open?: number
  settled?: number
  wins?: number
  losses?: number
  breakeven?: number

  /** 0-100 标度 */
  winrate_pct?: number | null
  avg_pnl_pct?: number | null
  total_pnl_pct?: number | null
  profitable?: boolean | null
}

// ──────────────────────────────────────────────────────────────────────
// § 3 · 今日（与首页 StatCards 同源同口径）
// ──────────────────────────────────────────────────────────────────────

export type TodayStats = {
  signals: number
  settled: number
  date: string

  /**
   * v180 P0+：PeriodSelector 选定时段内的信号数（与 signals 不同口径）。
   * 当 PeriodSelector=today 时等于 signals，否则表示 7d/30d/custom 窗口内的累计。
   */
  windowSignals: number | null
  windowSettled: number | null
}

/**
 * v180 P0+：PeriodSelector 选定时段内的窗口元数据（用于卡片对账条）。
 * 后端 /review/snapshot 返回的 window 字段。
 */
export type WindowMeta = {
  from_ts: number | null
  to_ts: number | null
  signals: number | null
  settled: number | null
}

// ──────────────────────────────────────────────────────────────────────
// § 4 · 窗口内新闻流速（与首页 /api/events/today 同步）
// ──────────────────────────────────────────────────────────────────────

export type TodayFlow = {
  news: number
  analyzed: number

  /**
   * 实时信号数（最近 N 小时的 BUY/SELL；LLM/数据源全挂时归零）。
   * 兼容旧后端（无 live_signal_count）时退化到 signal_count。
   */
  signal: number

  /**
   * 今日累计信号数（含早场可能 stale 的信号，与 live_signal_count 区分）。
   * 用于对账条：'实时窗口' vs '今日累计'。
   */
  signalStale: number

  /** live_signal_window_hours（小时），通常 1 / 4 / 8 */
  liveWindowHours: number
}

// ──────────────────────────────────────────────────────────────────────
// § 5 · /review/snapshot 完整响应
// ──────────────────────────────────────────────────────────────────────

export type ReviewSnapshot = {
  paper?: PaperStats
  quick_sim?: QuickSimOverall
  today?: { signals?: number; settled?: number; date?: string }
  window?: WindowMeta
}

// ──────────────────────────────────────────────────────────────────────
// § 6 · 高亮"变化检测"用的 prev 快照
// ──────────────────────────────────────────────────────────────────────

/**
 * ReviewOverview 内部用于"数字变化 → 闪一下"对比的上一帧快照。
 * 字段初始值全为 null → 首次拉取不闪（这是设计行为，避免打开页面就闪）。
 */
export type FlashPrev = {
  aWinrate: number | null
  bWinrate: number | null
  activeCount: number | null
  newsCount: number | null
}

/**
 * 高亮 key 的 state shape（4 字段独立高亮）。
 * 每字段 == true 表示该数字刚刷新，正在 FLASH_MS 毫秒的高亮期内。
 */
export type FlashState = {
  a: boolean
  b: boolean
  rule: boolean
  news: boolean
}
