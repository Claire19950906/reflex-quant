'use client'

import { KnowledgeGraphPanel } from '@/components/knowledge-graph-panel'

export default function KnowledgeGraphPage() {
  return (
    <div className="flex flex-col gap-3 p-3">
      <KnowledgeGraphPanel />

      {/* v337-UI: 原「信念系统」能力卡（beliefs/belief_edges/belief_history 表名）已
          移到 后台 → 联通 页，与其它运维面板同处；产品页不再出现内部表名和 cron 名。 */}
    </div>
  )
}