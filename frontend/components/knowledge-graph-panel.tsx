'use client'

import { Suspense, useEffect, useState } from 'react'
import { CausalChainsPanel } from '@/components/causal-chains-panel'
import { CausalChainsFeedbackPanel } from '@/components/v291/causal-chains-feedback-panel'
import { MarketRegimePanel } from '@/components/v291/market-regime-panel'
import { OrderbookDepthPanel } from '@/components/v291/orderbook-depth-panel'
import { FundingRatePanel } from '@/components/v291/funding-rate-panel'
import { CausalGraphPanel } from '@/components/quant/causal-graph-panel'
import { API_BASE } from '@/lib/api'
import { Network, GitBranch, History, BarChart3, BookOpen, Percent, Download, Activity } from 'lucide-react'

type TabKey = 'graph' | 'causal' | 'feedback' | 'regime' | 'orderbook' | 'funding'

// v407: 后端 /api/causal/export 支持的四张表
const EXPORT_TABLES = [
  { key: 'chains', label: '因果链' },
  { key: 'nodes', label: '图谱节点' },
  { key: 'edges', label: '图谱边' },
  { key: 'outcomes', label: '结果记录' },
] as const

// v413: 库体检 —— 以前要 curl 才能知道"库是不是在骗人"（启用链多少、置信度是不是常量、
// 多少链永远触发不了）。这里直接显示，看不健康第一眼就知道。
type Quality = {
  chains_total: number
  chains_effective: number
  chains_no_keyword: number
  chains_both_direction: number
  chains_n_ge_12: number
  chains_n_ge_12_effective?: number
  chains_dead?: number
  chains_usable?: number
  avg_confidence: number
  direction_breakdown?: { direction: string; n: number; hit_rate: number; note?: string }[]
  // v417: 库整体"超额"与达标信号 —— 绝对命中率必须跟基准一起看，否则会被行情漂移骗
  library_edge?: {
    hit_rate: number | null
    n: number
    base_rate: number | null
    lift: number | null
    chains_strong: number
    verdict: string
  }
  coverage?: {
    veto_ready_chains?: number
    veto_ready_signals?: number
    news_assets?: number
    blind_n?: number
    blind_top?: { asset: string; news: number }[]
    gate?: { min_samples: number; min_lift_pp: number; max_p_value: number; note: string }
  }
  // v421: 注入日志 ↔ 决策 对账（decision_id 全 NULL，只能离线按品种+时间窗配对）
  injection?: {
    total: number
    matched: number
    ambiguous?: number
    unmatched?: number
    matched_ratio: number
    by_mode?: Record<string, number>
    window_s?: number
    note?: string
  }
  // v422: 影子否决 —— 接入 conflict_veto 会拦掉多少单、拦对没拦对（只记录不干预）
  veto_shadow?: {
    total: number
    would_veto: number
    veto_ratio?: number
    settled: number
    false_veto: number
    true_veto: number
    note?: string
  }
}

/**
 * 统一入口：把"因果图谱（AI 自动生成的 5-why 因果图）"和"因果链库（人工
 * 标注/启用的硬规则）"放在同一页面下，两者数据源独立但概念上是同一件事 —
 * 资产涨跌的驱动因果图谱。
 *
 * 实现策略：两个面板组件完全保留原样（避免功能回归），本页只做壳 + tab 切换。
 *
 * Next.js 14 要求 useSearchParams() 在 Suspense 内 — 用 <Suspense> 包住
 * CausalGraphPanel；tab 切换时只有 graph 分支进 Suspense，causal 不受影响。
 */
export function KnowledgeGraphPanel() {
  const [tab, setTab] = useState<TabKey>('graph')
  const [quality, setQuality] = useState<Quality | null>(null)

  useEffect(() => {
    let alive = true
    fetch(`${API_BASE}/causal/quality?days=30`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (alive && d && d.ok) setQuality(d as Quality)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  return (
    <>
      <header className="mx-auto flex w-full max-w-[1600px] flex-wrap items-end justify-between gap-3 p-3 pb-0 md:p-6 md:pb-0">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">
            因果图谱 · 资产涨跌驱动
          </h1>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
            同一概念的两种维护方式：AI 自动从历史 5-why 推理生成「因果图谱」，
            人工标注/启用「因果链库」硬规则。两条数据流在分析时被回灌到
            推理系统作为先验。右侧「传感器」只是同一批行情的三个读取角度，不是图谱的一部分。
          </p>
        </div>
        {/* v337-UI: 原来 6 个 tab 平铺，把「3 个因果视图」和「3 个行情传感器」混在一起，
            用户点进去会以为是同一层级的 6 个功能。这里分成两组，组间加一条竖线。 */}
        <nav
          className="inline-flex max-w-full flex-nowrap items-center gap-0.5 overflow-x-auto thin-scroll rounded-md border border-border bg-card p-0.5"
          role="tablist"
          aria-label="因果图谱视图切换"
        >
          <span className="hidden shrink-0 px-1.5 font-mono text-[11px] tracking-widest text-muted-foreground/60 md:inline">图谱</span>
          <TabButton
            active={tab === 'graph'}
            onClick={() => setTab('graph')}
            icon={<Network className="h-4 w-4" />}
            label="因果图谱"
          />
          <TabButton
            active={tab === 'causal'}
            onClick={() => setTab('causal')}
            icon={<GitBranch className="h-4 w-4" />}
            label="因果链库"
          />
          <TabButton
            active={tab === 'feedback'}
            onClick={() => setTab('feedback')}
            icon={<History className="h-4 w-4" />}
            label="反馈闭环"
          />
          <span aria-hidden className="mx-1 h-5 w-px bg-border" />
          <span className="hidden shrink-0 px-1.5 font-mono text-[11px] tracking-widest text-muted-foreground/60 md:inline">传感器</span>
          <TabButton
            active={tab === 'regime'}
            onClick={() => setTab('regime')}
            icon={<BarChart3 className="h-4 w-4" />}
            label="市场状态"
          />
          <TabButton
            active={tab === 'orderbook'}
            onClick={() => setTab('orderbook')}
            icon={<BookOpen className="h-4 w-4" />}
            label="盘口深度"
          />
          <TabButton
            active={tab === 'funding'}
            onClick={() => setTab('funding')}
            icon={<Percent className="h-4 w-4" />}
            label="资金费率"
          />
        </nav>
      </header>

      {/* v407: 图谱此前只能看、不能拿走 —— 四张表一键导出 CSV（复盘会/外部评审要用） */}
      <div className="mx-auto mt-2 flex w-full max-w-[1600px] flex-wrap items-center gap-1.5 px-3 md:px-6">
        <span className="shrink-0 font-mono text-[11px] tracking-widest text-muted-foreground/60">
          <Download className="mr-1 inline h-3 w-3" />
          导出
        </span>
        {EXPORT_TABLES.map((t) => (
          <a
            key={t.key}
            href={`${API_BASE}/causal/export?what=${t.key}&limit=5000`}
            className="shrink-0 rounded border border-border bg-card px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
            title={`导出 ${t.label} (CSV)`}
          >
            {t.label}
          </a>
        ))}
        {quality && (
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <span className="shrink-0 font-mono text-[11px] tracking-widest text-muted-foreground/60">
              <Activity className="mr-1 inline h-3 w-3" />
              库体检
            </span>
            <Meter label="启用链" value={quality.chains_effective} total={quality.chains_total} tone="ok" />
            <Meter label="平均置信度" value={quality.avg_confidence.toFixed(2)} tone={quality.avg_confidence > 0.8 ? "warn" : "ok"} />
            <Meter
              label="n≥12(生效)"
              value={quality.chains_n_ge_12_effective ?? quality.chains_n_ge_12}
              tone={(quality.chains_n_ge_12_effective ?? quality.chains_n_ge_12) > 0 ? "ok" : "warn"}
            />
            {/* v417: 唯一能行使否决的刻度 —— 跑赢基准≥10pp 且 p≤0.05。
                旧口径（Wilson 下界>0.5）实测恒为 0，这一格永远是 0 就等于没有这一档。 */}
            <Meter
              label="达标信号"
              value={quality.coverage?.veto_ready_signals ?? 0}
              tone={(quality.coverage?.veto_ready_signals ?? 0) > 0 ? "ok" : "warn"}
            />
            {quality.library_edge?.lift != null && (
              <Meter
                label={`库超额 vs 基准${Math.round((quality.library_edge.base_rate || 0) * 100)}%`}
                value={`${quality.library_edge.lift >= 0 ? '+' : ''}${Math.round(quality.library_edge.lift * 100)}pp`}
                tone={quality.library_edge.lift >= 0.1 ? "ok" : "warn"}
              />
            )}
            <Meter
              label="废链(无品种/无词)"
              value={quality.chains_dead ?? 0}
              tone={(quality.chains_dead ?? 0) === 0 ? "ok" : "warn"}
            />
            <Meter label="永不触发" value={quality.chains_no_keyword} tone={quality.chains_no_keyword > 0 ? "warn" : "ok"} />
            {/* v420: 盲区 —— 有新闻但零可用链。这类品种 inject_block 只能吐"本品种 0 条"，
                不显式列出来，"覆盖 4 个品种"就会被误读成"图谱够用了"。 */}
            {(quality.coverage?.blind_n ?? 0) > 0 && (
              <Meter
                label={`盲区 ${quality.coverage?.blind_n}/${quality.coverage?.news_assets ?? 0} 品种`}
                value={(quality.coverage?.blind_top || [])
                  .slice(0, 3)
                  .map((b) => `${b.asset}(${b.news})`)
                  .join(' · ')}
                tone="warn"
              />
            )}
            {/* v421: 注入到底进没进下单 —— 1782 次注入里只有 88 次能对上决策，
                其余是归因取证/批量注入。这一格回答"图谱对下单有没有影响"。 */}
            {(quality.injection?.total ?? 0) > 0 && (
              <Meter
                label={`进下单决策 ${quality.injection?.matched}/${quality.injection?.total}`}
                value={`${Math.round((quality.injection?.matched_ratio ?? 0) * 100)}%`}
                tone={(quality.injection?.matched_ratio ?? 0) >= 0.2 ? 'ok' : 'warn'}
              />
            )}
            {/* v422: 影子否决 —— 只记录不拦截。假否决>0 说明"拦了本该赚的单"。 */}
            {(quality.veto_shadow?.total ?? 0) > 0 && (
              <Meter
                label={`假想否决 ${quality.veto_shadow?.would_veto}/${quality.veto_shadow?.total}`}
                value={
                  (quality.veto_shadow?.settled ?? 0) > 0
                    ? `假否决 ${quality.veto_shadow?.false_veto} · 真否决 ${quality.veto_shadow?.true_veto}`
                    : '尚无结算'
                }
                tone={(quality.veto_shadow?.false_veto ?? 0) > 0 ? 'warn' : 'ok'}
              />
            )}
            {(quality.direction_breakdown || [])
              .filter((d) => d.direction !== 'BOTH')
              .map((d) => (
                <Meter
                  key={d.direction}
                  label={`${d.direction} 命中`}
                  value={`${Math.round(d.hit_rate * 100)}%`}
                  tone={d.hit_rate < 0.45 ? "warn" : "ok"}
                />
              ))}
          </div>
        )}
      </div>

      <div role="tabpanel">
        {tab === 'graph' ? (
          <Suspense fallback={<div className="p-6 text-center font-mono text-xs text-muted-foreground">加载因果图谱…</div>}>
            <CausalGraphPanel />
          </Suspense>
        ) : tab === 'causal' ? (
          <CausalChainsPanel />
        ) : tab === 'feedback' ? (
          <CausalChainsFeedbackPanel />
        ) : tab === 'regime' ? (
          <MarketRegimePanel />
        ) : tab === 'orderbook' ? (
          <OrderbookDepthPanel />
        ) : (
          <FundingRatePanel />
        )}
      </div>
    </>
  )
}

function Meter({
  label,
  value,
  total,
  tone = 'ok',
}: {
  label: string
  value: number | string
  total?: number
  tone?: 'ok' | 'warn'
}) {
  return (
    <span
      className={
        'shrink-0 rounded border px-2 py-1 font-mono text-[11px] ' +
        (tone === 'warn'
          ? 'border-amber-500/40 bg-amber-500/10 text-amber-600'
          : 'border-border bg-card text-muted-foreground')
      }
      title={total ? `${label}: ${value} / ${total}` : label}
    >
      {label} <span className="text-foreground">{value}</span>
      {total !== undefined ? <span className="opacity-60">/{total}</span> : null}
    </span>
  )
}

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={
        'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded px-2.5 py-1.5 text-xs tracking-wide transition-colors ' +
        (active
          ? 'bg-primary text-primary-foreground shadow'
          : 'text-muted-foreground hover:text-foreground')
      }
    >
      {icon}
      {label}
    </button>
  )
}