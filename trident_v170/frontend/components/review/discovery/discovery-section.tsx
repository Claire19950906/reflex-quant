'use client'

import { FedRatePanel } from '@/components/replay/fed-rate-panel'
import { CrossAssetAnomalyPanel } from '@/components/replay/cross-asset-anomaly-panel'
import { MetaCognitionPanel } from './meta-cognition'
import { AssetScoutPanel } from './asset-scout'
import { CycleRadarPanel } from './cycle-radar'
import { EventRadarPanel } from './event-radar'
import { CollabPanel } from './collab-panel'
import { FedImpactPanel } from './fed-impact'
import { FedWatchPanel } from './fed-watch'
import { FedMailboxPanel } from './fed-mailbox-panel'
import { CrossAssetMetaPanel } from './cross-asset-meta'
import { DiscoveryMissionBanner } from './discovery-mission-banner'
import { DiscoveryPillarNav } from './discovery-pillar-nav'
import { CollapsiblePillar } from './collapsible-pillar'

/**
 * 段 0 · 发现/探索研究(Discovery & Exploration)
 * 设计动机:用户原话——「本板块是发现研究,寻找铃兰总结规律的前置,
 * 探索更多一些,可以发现新的规律、新的投资机会、补充选优思维框架的死和不足」。
 * 7 个子板块,各自承担一个探索维度。
 *
 * v283+ · #1/#17 · 9 个 panel 默认折叠,只 3 个核心 panel(intraday 不在本段,
 *       加 fed / fedwatch / fedmailbox 共 3 个核心)默认展开。
 *       折叠状态 children 不挂载 → 内部 usePoll 不创建定时器 / 不发请求。
 */

export function DiscoverySection() {
  return (
    <section id="discovery" aria-label="发现·探索研究" className="scroll-mt-3">
      <DiscoveryMissionBanner />
      <DiscoveryPillarNav />

      {/* 核心 panel(默认展开) */}
      <CollapsiblePillar
        id="disc-fed"
        icon="🏛"
        title="Fed 利率预期 + 传导"
        tone="amber"
        defaultOpen
      >
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <FedRatePanel />
          <FedImpactPanel />
        </div>
      </CollapsiblePillar>

      <CollapsiblePillar
        id="disc-fedwatch"
        icon="📡"
        title="Fed 全景监控"
        tone="amber"
        defaultOpen
        trailing="60s 刷新"
      >
        <FedWatchPanel />
      </CollapsiblePillar>

      <CollapsiblePillar
        id="disc-fedmailbox"
        icon="📧"
        title="Fed 邮箱箱"
        tone="amber"
        defaultOpen
        trailing="与 fedwatch 共用 Provider"
      >
        <FedMailboxPanel />
      </CollapsiblePillar>

      {/* 次要 panel(默认折叠) */}
      <CollapsiblePillar
        id="disc-cross"
        icon="🌐"
        title="跨资产同步异常"
        tone="rose"
      >
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <CrossAssetAnomalyPanel />
          <CrossAssetMetaPanel />
        </div>
      </CollapsiblePillar>

      <CollapsiblePillar
        id="disc-asset"
        icon="🎯"
        title="强信息驱动资产猎手"
        tone="fuchsia"
      >
        <AssetScoutPanel />
      </CollapsiblePillar>

      <CollapsiblePillar
        id="disc-event"
        icon="⚡"
        title="事件驱动快速因果"
        tone="sky"
      >
        <EventRadarPanel />
      </CollapsiblePillar>

      <CollapsiblePillar
        id="disc-cycle"
        icon="☯️"
        title="周期维度(阴阳共振)"
        tone="violet"
      >
        <CycleRadarPanel />
      </CollapsiblePillar>

      <CollapsiblePillar
        id="disc-meta"
        icon="🤖"
        title="AI 元认知自检"
        tone="cyan"
      >
        <MetaCognitionPanel />
      </CollapsiblePillar>

      <CollapsiblePillar
        id="disc-collab"
        icon="🌍"
        title="多机协作·时区参考"
        tone="emerald"
      >
        <CollabPanel />
      </CollapsiblePillar>

      <div className="rounded border border-border bg-secondary/20 p-2 text-[10px] text-muted-foreground">
        <strong className="text-foreground">📜 板块定位:</strong>
        本板块所有面板只用于「探索研究」「寻找规律」「补充框架」—
        <strong className="text-rose-600 dark:text-rose-400">不直接产生模拟盘交易信号</strong>。
        想进入交易链路请走 /replay;本板块的产出可投到
        <code className="rounded bg-background px-1">causal_chains.json</code> /
        <code className="rounded bg-background px-1">causal_graph.json</code>,
        在 /knowledge-graph / /causal-chains 审批后才进入分析链路。
      </div>
    </section>
  )
}