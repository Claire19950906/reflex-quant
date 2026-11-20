'use client'

/**
 * components/crowd/crowd-sources-panel.tsx — 数据源权威度 + 原始条目 (v316)
 *
 * 左：各来源的权威度（条目数 / 覆盖话题数 / 被其它平台复现次数 / 权威度分）
 * 右：最近抓到的原始条目 —— 每条都能点回原页面，验证数据是真抓的。
 */

import type { CrowdItem, CrowdSourceAuth } from '@/lib/crowd-data'

function fmt(n: number | undefined | null, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

export function CrowdSourcesPanel({
  sources,
  items,
}: {
  sources: CrowdSourceAuth[]
  items: CrowdItem[]
}) {
  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
      <div className="rounded border border-border p-2">
        <div className="mb-1.5 font-mono text-[11px] font-bold text-foreground">数据源权威度</div>
        <table className="w-full font-mono text-[11px]">
          <thead className="text-muted-foreground">
            <tr>
              <th className="px-1 py-1 text-left">源</th>
              <th className="px-1 py-1 text-right">条目</th>
              <th className="px-1 py-1 text-right">话题</th>
              <th className="px-1 py-1 text-right">被复现</th>
              <th className="px-1 py-1 text-right">权威度</th>
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <tr key={`${s.source}-${s.media_channel}`} className="border-t border-border/60">
                <td className="px-1 py-1">
                  <div className="text-foreground">{s.source}</div>
                  <div className="truncate text-muted-foreground" title={s.media_channel}>
                    {s.media_channel?.slice(0, 40)}
                  </div>
                </td>
                <td className="px-1 py-1 text-right text-muted-foreground">{s.items}</td>
                <td className="px-1 py-1 text-right text-muted-foreground">{s.topics}</td>
                <td className="px-1 py-1 text-right text-amber-500">{s.cross_verified}</td>
                <td className="px-1 py-1 text-right text-foreground">{fmt(s.authority_score, 1)}</td>
              </tr>
            ))}
            {!sources.length && (
              <tr>
                <td colSpan={5} className="px-1 py-2 text-center text-muted-foreground">暂无数据</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="rounded border border-border p-2">
        <div className="mb-1.5 font-mono text-[11px] font-bold text-foreground">
          最近抓到的原始条目{' '}
          <span className="font-normal text-muted-foreground">（点击可回溯原页面）</span>
        </div>
        <div className="flex max-h-[520px] flex-col gap-1 overflow-y-auto">
          {items.map((it, i) => (
            <a
              key={`${it.source}-${it.title}-${i}`}
              href={it.url}
              target="_blank"
              rel="noreferrer"
              className="rounded border border-border/60 px-2 py-1 hover:border-primary/50"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-mono text-[11px] text-primary">{it.source}</span>
                <span className="font-mono text-[11px] text-muted-foreground">
                  {it.media_channel || '—'}
                </span>
              </div>
              <div className="font-mono text-[11px] leading-snug text-foreground">{it.title}</div>
            </a>
          ))}
          {!items.length && (
            <div className="font-mono text-[11px] text-muted-foreground">暂无条目</div>
          )}
        </div>
      </div>
    </div>
  )
}

export default CrowdSourcesPanel
