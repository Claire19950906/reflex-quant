'use client'

/**
 * components/crowd/crowd-ranking-table.tsx — 月度权威度榜单 (v316)
 *
 * 每条带 explain（人可读的权威度拆解：热度 / 跨平台数 / 持续天数 / 动能 / 媒体权重），
 * 保证任何一条排名都能被追问「凭什么」。
 */

import type { CrowdRankingItem, CrowdRankingSort } from '@/lib/crowd-data'

export const CROWD_SORTS: { key: CrowdRankingSort; label: string; hint: string }[] = [
  { key: 'authority', label: '权威度', hint: '跨平台交叉验证优先 —— 多个独立平台同时在讨论的才算权威' },
  { key: 'heat', label: '热度', hint: '此刻各来源最热的条目' },
  { key: 'momentum', label: '升温', hint: '相对上一周期的热度变化 —— 什么正在起来' },
]

function fmt(n: number | undefined | null, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

/** 后端 asset_class / market 是 GROUP_CONCAT 的逗号串 → 拆成可读标签 */
function splitList(value: string | undefined | null): string[] {
  return String(value || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

export function CrowdRankingTable({
  ranking,
  sort,
  onSortChange,
}: {
  ranking: CrowdRankingItem[]
  sort: CrowdRankingSort
  onSortChange: (s: CrowdRankingSort) => void
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {CROWD_SORTS.map((s) => (
          <button
            key={s.key}
            onClick={() => onSortChange(s.key)}
            title={s.hint}
            className={`rounded px-2 py-1 font-mono text-[11px] ${
              sort === s.key
                ? 'border border-primary text-primary'
                : 'border border-border text-muted-foreground'
            }`}
          >
            {s.label}
          </button>
        ))}
        <span className="font-mono text-[11px] text-muted-foreground">
          {CROWD_SORTS.find((s) => s.key === sort)?.hint}
        </span>
      </div>

      <div className="overflow-x-auto rounded border border-border">
        <table className="w-full font-mono text-[11px]">
          <thead className="bg-muted/30 text-muted-foreground">
            <tr>
              <th className="px-2 py-1 text-left">#</th>
              <th className="px-2 py-1 text-left">话题 / 标的</th>
              <th className="px-2 py-1 text-right">权威度</th>
              <th className="px-2 py-1 text-right">跨平台</th>
              <th className="px-2 py-1 text-right">热度</th>
              <th className="px-2 py-1 text-left">平台</th>
              <th className="px-2 py-1 text-left">权威度依据</th>
            </tr>
          </thead>
          <tbody>
            {ranking.map((r) => {
              const labels = [
                ...new Set([
                  ...(r.themes || []),
                  ...splitList(r.asset_class),
                  ...splitList(r.market),
                ]),
              ].slice(0, 8)
              return (
              <tr key={r.topic_key} className="border-t border-border/60 align-top">
                <td className="px-2 py-1 text-muted-foreground">{r.rank_no}</td>
                <td className="px-2 py-1">
                  <div className="text-foreground">{r.display_name}</div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-1">
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {r.topic_kind}
                    </span>
                    {labels.map((x) => (
                      <span
                        key={x}
                        className="rounded border border-border bg-muted/40 px-1 py-0.5 font-mono text-[11px] text-muted-foreground"
                      >
                        {x}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-2 py-1 text-right text-foreground">{fmt(r.authority_score, 1)}</td>
                <td className="px-2 py-1 text-right">
                  <span
                    className={
                      r.source_count >= 3
                        ? 'text-amber-500'
                        : r.source_count >= 2
                          ? 'text-sky-500'
                          : 'text-muted-foreground'
                    }
                  >
                    {r.source_count}
                  </span>
                </td>
                <td className="px-2 py-1 text-right text-muted-foreground">{fmt(r.heat_total, 2)}</td>
                <td className="px-2 py-1 text-muted-foreground">{(r.sources || []).join(' / ')}</td>
                <td className="max-w-[420px] px-2 py-1 text-[11px] leading-tight text-muted-foreground">
                  {r.explain}
                </td>
              </tr>
              )
            })}
            {!ranking.length && (
              <tr>
                <td colSpan={7} className="px-2 py-3 text-center text-muted-foreground">
                  暂无榜单数据（采集尚未产出）
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default CrowdRankingTable
