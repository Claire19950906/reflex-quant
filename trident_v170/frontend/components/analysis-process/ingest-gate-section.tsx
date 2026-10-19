'use client'

/**
 * v203+: 分区 1+2 —— 入库 + Gate 筛选。
 * 展示 raw_news 原文(截断可展开) + Gate 4 步标签(主资产 / kind / timing / strength / verdict)。
 */

import type { APGate, APRawNews } from '@/lib/analysis-process-data'
import { fmtTs, sourceLabel } from '@/lib/analysis-process-data'
import { JsonDump, KeyValueGrid, SectionCard, SubBlock } from './sections-shared'

function strengthBadge(s: number | null): string {
  if (s === null || s === undefined) return 'border-border bg-muted text-muted-foreground'
  if (s >= 8) return 'border-short/40 bg-short/10 text-short'
  if (s >= 6) return 'border-hold/40 bg-hold/10 text-hold'
  if (s >= 4) return 'border-primary/40 bg-primary/10 text-primary'
  return 'border-border bg-muted text-muted-foreground'
}

function verdictBadge(v: string | null): string {
  const s = (v || '').toLowerCase()
  // v181: 只剩 2 类:可交易 / 排除
  if (s.includes('可交易') || s.includes('通过') || s === 'ok' || s === 'pass') {
    return 'border-long/40 bg-long/10 text-long'
  }
  if (s.includes('阻断') || s.includes('拒绝') || s.includes('排除') || s === 'block' || s === 'reject') {
    return 'border-short/40 bg-short/10 text-short'
  }
  // 兜底(老数据可能残留)
  if (s.includes('观察') || s === 'hold' || s === 'wait' || s.includes('只能看')) {
    return 'border-hold/40 bg-hold/10 text-hold'
  }
  return 'border-border bg-muted text-muted-foreground'
}

// v181: 1-100 影响力打分(<20 红,20-50 黄,>50 绿)
function impactBadge(n: number | null | undefined): string {
  if (n === null || n === undefined) return 'border-border bg-muted text-muted-foreground'
  if (n >= 50) return 'border-long/40 bg-long/10 text-long'
  if (n >= 20) return 'border-hold/40 bg-hold/10 text-hold'
  return 'border-short/40 bg-short/10 text-short'
}

// v181: 新闻时效(>12h 红,6-12h 黄,<6h 绿)
function ageBadge(h: number | null | undefined): string {
  if (h === null || h === undefined) return 'border-border bg-muted text-muted-foreground'
  if (h > 12) return 'border-short/40 bg-short/10 text-short'
  if (h > 6) return 'border-hold/40 bg-hold/10 text-hold'
  return 'border-long/40 bg-long/10 text-long'
}

export function IngestGateSection({
  news,
  gate,
  ts_iso,
}: {
  news: APRawNews | null
  gate: APGate | null
  ts_iso?: string
}) {
  if (!news) {
    return (
      <SectionCard tone="blue" step="1" title="入库 + Gate 筛选" subtitle="raw_news + gate">
        <div className="font-mono text-xs text-muted-foreground">暂无数据</div>
      </SectionCard>
    )
  }
  const source = String(news.source || '')
  const fingerprint = String(news.content_fingerprint || news['content_hash'] || '')
  return (
    <SectionCard
      tone="blue"
      step="1"
      title="入库 + Gate 筛选"
      subtitle={`raw_news #${news.id} · ${sourceLabel(source)}`}
      badge={ts_iso}
    >
      <div className="space-y-3">
        {/* 原文(可折叠) */}
        <details className="rounded-md border border-border bg-background/40">
          <summary className="cursor-pointer px-3 py-2 font-mono text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            ▾ 原文(全文) · {String(news.content || '').length} 字
          </summary>
          <div className="border-t border-border px-3 py-2 text-[12px] leading-relaxed text-foreground/90 whitespace-pre-wrap">
            {String(news.content || '—')}
          </div>
        </details>

        {/* 入库元数据 */}
        <SubBlock title="入库字段" tone="note">
          <KeyValueGrid
            items={[
              { label: 'news_id', value: news.id, mono: true },
              { label: 'source', value: sourceLabel(source), mono: true },
              { label: 'timestamp', value: fmtTs(news.timestamp ?? news.ts), mono: true },
              { label: 'status', value: String(news.status || '—'), mono: true },
              { label: 'fingerprint', value: fingerprint ? fingerprint.slice(0, 16) + '…' : '—', mono: true },
              { label: 'is_noise', value: String(news.is_noise ?? '—'), mono: true },
              { label: 'relevance', value: String(news.relevance_score ?? '—'), mono: true },
            ]}
          />
        </SubBlock>

        {/* Gate 4 步标签 */}
        {gate ? (
          <SubBlock title="Gate 4 步评估" tone="warn">
            <div className="flex flex-wrap items-center gap-2 pb-2">
              <span
                className={`rounded-md border px-2 py-1 font-mono text-[11px] font-bold ${verdictBadge(gate.verdict)}`}
              >
                {gate.verdict || '—'}
              </span>
              <span className={`rounded-md border px-2 py-1 font-mono text-[10px] ${strengthBadge(gate.strength)}`}>
                strength {gate.strength ?? '—'}/10
              </span>
              {/* v181: 影响力打分 1-100(<20 红,>=20 黄,>=50 绿) */}
              <span className={`rounded-md border px-2 py-1 font-mono text-[10px] ${impactBadge(gate.impact_1to100)}`}>
                impact {gate.impact_1to100 ?? '—'}/100
              </span>
              {/* v181: 新闻时效(>12h 红) */}
              <span className={`rounded-md border px-2 py-1 font-mono text-[10px] ${ageBadge(gate.news_age_hours)}`}>
                age {gate.news_age_hours !== null && gate.news_age_hours !== undefined ? `${gate.news_age_hours}h` : '—'}
              </span>
              <span className="rounded-md border border-border bg-secondary px-2 py-1 font-mono text-[10px] text-foreground">
                kind: {gate.kind || '—'}
              </span>
              <span className="rounded-md border border-border bg-secondary px-2 py-1 font-mono text-[10px] text-foreground">
                timing: {gate.timing || '—'}
              </span>
              <span className="rounded-md border border-border bg-secondary px-2 py-1 font-mono text-[10px] text-foreground">
                main: {gate.main_asset || '—'}
              </span>
            </div>
            <KeyValueGrid
              items={[
                { label: 'main_asset', value: gate.main_asset || '—', mono: true },
                { label: 'strength', value: String(gate.strength ?? '—'), mono: true, accent: 'text-hold' },
                { label: 'verdict', value: gate.verdict || '—', mono: true },
                { label: 'impact_1to100', value: String(gate.impact_1to100 ?? '—'), mono: true, accent: gate.impact_1to100 !== null && gate.impact_1to100 !== undefined && gate.impact_1to100 >= 20 ? 'text-long' : 'text-short' },
                { label: 'news_age_hours', value: gate.news_age_hours !== null && gate.news_age_hours !== undefined ? `${gate.news_age_hours}h` : '—', mono: true, accent: gate.news_age_hours !== null && gate.news_age_hours !== undefined && gate.news_age_hours <= 12 ? 'text-long' : 'text-short' },
                { label: 'is_material', value: String(gate.is_material ?? '—'), mono: true },
                { label: 'assets', value: Array.isArray(gate.assets) ? gate.assets.join(', ') : String(gate.assets || '—'), mono: true },
              ]}
            />
            {gate.core_impact ? (
              <div className="mt-2 rounded border border-border bg-background/60 px-2 py-1.5 text-[11px] leading-relaxed text-foreground/85">
                <span className="mr-1 font-mono text-[10px] font-bold uppercase tracking-wider text-hold">core_impact:</span>
                {String(gate.core_impact)}
              </div>
            ) : null}
          </SubBlock>
        ) : null}

        {/* 兜底:完整 raw_news JSON */}
        <details className="rounded-md border border-border bg-background/40">
          <summary className="cursor-pointer px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            ▾ raw_news 全字段 JSON
          </summary>
          <div className="border-t border-border px-3 py-2">
            <JsonDump data={news} />
          </div>
        </details>
      </div>
    </SectionCard>
  )
}
