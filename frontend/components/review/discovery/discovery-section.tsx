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
import { CollapsiblePillar } from './collapsible-pillar'
import { CrossAssetRankingsPanel } from './cross-asset-rankings-panel'
import { UndervaluedDetectorPanel } from './undervalued-detector-panel'
import { CrowdPulsePanel } from '@/components/crowd/crowd-pulse-panel'

/**
 * 发现页的内层分组（v345.9 · 信息架构重构）
 *
 * 改动原因（用户反馈）：Fed 被拆成 3 个并列折叠块（fed / fedwatch / fedmailbox），
 * 同一个数据源占三格；顶部任务说明 + 每段一句长副标题 + 底部「板块定位」，
 * 说明文字比数据还多，真正要看的东西全被推到下面。
 *
 * 现在按「一件事一个块」归并：
 *   · Fed 组 —— 利率预期 + 传导 + 全景 + 邮箱，一个折叠块装完
 *   · 资产与事件组 —— 资产猎手 / 事件因果 / 跨资产 / 周期
 *   · 系统自检组 —— AI 元认知 / 多机协作（低频，默认折叠）
 */

export function DiscoveryFedGroup() {
  return (
    <CollapsiblePillar
      id="disc-fed"
      icon="🏛"
      title="Fed 利率预期 · 传导 · 全景 · 邮箱"
      tone="amber"
      defaultOpen
      trailing="60s 刷新"
    >
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <FedRatePanel />
          <FedImpactPanel />
        </div>
        <FedWatchPanel />
        <FedMailboxPanel />
      </div>
    </CollapsiblePillar>
  )
}

export function DiscoveryAssetGroup() {
  return (
    <>
      <CollapsiblePillar
        id="disc-asset"
        icon="🎯"
        title="强信息驱动资产猎手"
        tone="fuchsia"
        defaultOpen
      >
        <AssetScoutPanel />
      </CollapsiblePillar>

      <CollapsiblePillar id="disc-event" icon="⚡" title="事件驱动快速因果" tone="sky">
        <EventRadarPanel />
      </CollapsiblePillar>

      <CollapsiblePillar id="disc-cross" icon="🌐" title="跨资产同步异常" tone="rose">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <CrossAssetAnomalyPanel />
          <CrossAssetMetaPanel />
        </div>
        <div className="mt-3">
          <CrossAssetRankingsPanel />
        </div>
      </CollapsiblePillar>

      <CollapsiblePillar id="disc-undervalued" icon="💎" title="超跌 / 低估检测" tone="emerald">
        <UndervaluedDetectorPanel />
      </CollapsiblePillar>

      <CollapsiblePillar id="disc-cycle" icon="☯️" title="周期维度 · 阴阳共振" tone="violet">
        <CycleRadarPanel />
      </CollapsiblePillar>

      <CollapsiblePillar id="disc-crowd" icon="👥" title="人群画像 · 二级市场在关注什么" tone="indigo">
        <CrowdPulsePanel />
      </CollapsiblePillar>
    </>
  )
}

export function DiscoverySystemGroup() {
  return (
    <>
      <CollapsiblePillar id="disc-meta" icon="🤖" title="AI 元认知自检" tone="cyan">
        <MetaCognitionPanel />
      </CollapsiblePillar>

      <CollapsiblePillar id="disc-collab" icon="🌍" title="多机协作 · 时区参考" tone="emerald">
        <CollabPanel />
      </CollapsiblePillar>
    </>
  )
}
