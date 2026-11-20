'use client'

/**
 * components/crowd/crowd-audience-grid.tsx — 人群画像分群网格 (v316)
 *
 * 9 个维度：platform / market / asset_class / theme / sentiment /
 *          time_window / influence_tier / media_tier / media_channel
 * 每个分群给出真实条目数、热度占比、以及贡献该分群的来源列表。
 */

import type { CrowdAudienceResp } from '@/lib/crowd-data'

function Bar({ pct, highlight }: { pct: number; highlight: boolean }) {
  const w = Math.max(1, Math.min(100, pct * 100))
  return (
    <div className="h-1.5 w-full rounded bg-muted/40">
      <div className={`h-1.5 rounded ${highlight ? 'bg-amber-500' : 'bg-sky-500'}`} style={{ width: `${w}%` }} />
    </div>
  )
}

export function CrowdAudienceGrid({ audience }: { audience: CrowdAudienceResp | null }) {
  const dims = audience?.dimensions || {}
  const names = Object.keys(dims)
  if (!names.length) {
    return (
      <div className="font-mono text-[11px] text-muted-foreground">
        暂无画像数据（采集尚未产出或尚未运行）
      </div>
    )
  }
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {names.map((dim) => {
        const rows = dims[dim] || []
        const label = audience?.dimension_labels?.[dim] || dim
        const top = rows[0]?.segment
        return (
          <div key={dim} className="rounded border border-border p-2">
            <div className="mb-1.5 flex items-baseline justify-between">
              <span className="font-mono text-[11px] font-bold text-foreground">{label}</span>
              <span className="font-mono text-[11px] text-muted-foreground">{dim}</span>
            </div>
            <div className="flex flex-col gap-1.5">
              {rows.slice(0, 8).map((s) => (
                <div key={s.segment}>
                  <div className="flex items-baseline justify-between font-mono text-[11px]">
                    <span className="truncate text-foreground" title={s.segment}>{s.segment}</span>
                    <span className="ml-2 shrink-0 text-muted-foreground">
                      {s.item_count} 条 · {(s.share * 100).toFixed(1)}%
                    </span>
                  </div>
                  <Bar pct={s.share} highlight={s.segment === top} />
                </div>
              ))}
              {!rows.length && (
                <div className="font-mono text-[11px] text-muted-foreground">无数据</div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

export default CrowdAudienceGrid
