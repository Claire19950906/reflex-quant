'use client'

import { Suspense, useState } from 'react'
import { CausalChainsPanel } from '@/components/causal-chains-panel'
import { CausalChainsFeedbackPanel } from '@/components/v291/causal-chains-feedback-panel'
import { MarketRegimePanel } from '@/components/v291/market-regime-panel'
import { OrderbookDepthPanel } from '@/components/v291/orderbook-depth-panel'
import { FundingRatePanel } from '@/components/v291/funding-rate-panel'
import { CausalGraphPanel } from '@/components/quant/causal-graph-panel'
import { Network, GitBranch, History, BarChart3, BookOpen, Percent } from 'lucide-react'

type TabKey = 'graph' | 'causal' | 'feedback' | 'regime' | 'orderbook' | 'funding'

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
            推理系统作为先验。
          </p>
        </div>
        <nav
          className="inline-flex rounded-md border border-border bg-card p-0.5"
          role="tablist"
          aria-label="因果图谱视图切换"
        >
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
            label="挂单深度"
          />
          <TabButton
            active={tab === 'funding'}
            onClick={() => setTab('funding')}
            icon={<Percent className="h-4 w-4" />}
            label="资金费率"
          />
        </nav>
      </header>

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
        'inline-flex items-center gap-2 rounded px-3 py-1.5 font-mono text-xs uppercase tracking-wider transition-colors ' +
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