/**
 * app/capital-flow/page.tsx — 资金流向（照 2.0 结构图从 /discover 提为独立页）
 *
 * 只做前端展示：直接复用已有的 FlowsPanel。
 * 数据仍走 /api/macro/context，不新增接口、不改后端。
 *
 * v476：把 Trident-2 里属于「资金与盘口」的 5 项能力（拥挤度反转 / 大额净流入 /
 *   盘口失衡 / 价差套利 / 链上巨鲸）挂在本页末尾。用插槽传进去而不是写进
 *   FlowsPanel —— 那个组件在 /discover 也复用，写死会在两个页面重复出现。
 */

import type { Metadata } from 'next'
import { FlowsPanel } from '@/components/quant/flows-panel'
import { CapabilityPanel } from '@/components/quant/capability-panel'

export const metadata: Metadata = { title: '资金流向 · Trident' }

export default function CapitalFlowPage() {
  return (
    <FlowsPanel
      capabilitySlot={
        <div className="mt-4">
          <CapabilityPanel
            title="资金与盘口能力"
            hint="拥挤度与流向反转 / 大额净流入流出 / 盘口失衡与大单墙 / 价差与套利 / 链上与巨鲸"
            featureIds={['F14', 'F26', 'F39', 'F40', 'F44']}
          />
        </div>
      }
    />
  )
}
