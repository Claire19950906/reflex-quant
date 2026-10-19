'use client'

import type {
  APCausalChain,
  APExperienceEntry,
  APReviewInsight,
} from '@/lib/analysis-process-data'
import { JsonDump, KeyValueGrid, SectionCard, SubBlock } from './sections-shared'

function activeBadge(active?: number | null): string {
  return active
    ? 'border-long/40 bg-long/10 text-long'
    : 'border-border bg-muted text-muted-foreground'
}

export function ExperienceSection({
  insights,
  experiences,
  chains,
}: {
  insights: APReviewInsight[]
  experiences: APExperienceEntry[]
  chains: APCausalChain[]
}) {
  const hasAny = insights?.length || experiences?.length || chains?.length
  return (
    <SectionCard
      tone="gold"
      step="6"
      title="经验沉淀"
      subtitle="review_insights + experience_entries + causal_chains"
      badge={
        hasAny
          ? `${(insights?.length || 0) + (experiences?.length || 0)} 条`
          : '暂无'
      }
    >
      <div className="space-y-3">
        <p className="text-[12px] leading-relaxed text-foreground/90">
          复盘归因之后,系统把"以后遇到类似情况该怎么处理"提炼成可执行的规则,写进经验库。下次再遇到同类型新闻,Gate/AI 推理时会参考这些规则。
        </p>
        {!hasAny ? (
          <div className="font-mono text-xs text-muted-foreground">
            暂无相关经验/规则 —— 通常在复盘归因生成后,review_agent 才会向 review_insights 写新规则。
          </div>
        ) : null}

        {insights?.length ? (
          <SubBlock title={`review_insights × ${insights.length} · 洞察规则`} tone="note">
            <p className="mb-2 text-[11px] text-muted-foreground">
              从复盘总结出的"以后该怎么处理"。active = 启用中。
            </p>
            {insights.map((it, i) => (
              <div key={it.id || i} className="border-t border-border py-2 first:border-t-0 first:pt-0">
                <div className="mb-1 flex flex-wrap items-baseline gap-2">
                  <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${activeBadge(it.active)}`}>
                    {it.active ? 'active' : 'inactive'}
                  </span>
                  {it.asset ? (
                    <span className="rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-foreground">
                      {String(it.asset)}
                    </span>
                  ) : null}
                  {it.kind ? (
                    <span className="rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                      {String(it.kind)}
                    </span>
                  ) : null}
                  <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                    insight #{it.id}
                  </span>
                </div>
                <p className="text-[12px] leading-relaxed text-foreground/90">
                  {String(it.rule || '—')}
                </p>
                {it.counter_factual ? (
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    counter_factual: {String(it.counter_factual)}
                  </div>
                ) : null}
                <KeyValueGrid
                  items={[
                    { label: 'scope', value: String(it.scope || '—'), mono: true },
                    { label: 'origin', value: String(it.origin || '—'), mono: true },
                    { label: 'samples', value: String(it.source_samples ?? '—'), mono: true },
                  ]}
                />
              </div>
            ))}
          </SubBlock>
        ) : null}

        {experiences?.length ? (
          <SubBlock title={`experience_entries × ${experiences.length} · 经验条目`} tone="muted">
            <p className="mb-2 text-[11px] text-muted-foreground">
              更结构化的"以后看到 X 信号 → 应该 Y 动作"型条目,被 Gate 阶段直接使用。
            </p>
            {experiences.slice(0, 8).map((e, i) => (
              <div key={e.id || i} className="border-t border-border py-2 first:border-t-0 first:pt-0">
                <div className="mb-1 flex flex-wrap items-baseline gap-2">
                  <span className="rounded border border-hold/40 bg-hold/10 px-1.5 py-0.5 font-mono text-[10px] font-bold text-hold">
                    {String(e.kind || '—')}
                  </span>
                  <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                    experience #{e.id}
                  </span>
                </div>
                <p className="text-[12px] leading-relaxed text-foreground/90">
                  {String(e.title || e.content || '—')}
                </p>
                <KeyValueGrid
                  items={[
                    { label: 'priority', value: String(e.priority ?? '—'), mono: true },
                    { label: 'weight', value: String(e.weight ?? '—'), mono: true },
                    { label: 'status', value: String(e.status || '—'), mono: true },
                    { label: 'assets', value: String(e.applicable_assets || '—'), mono: true },
                  ]}
                />
              </div>
            ))}
            {experiences.length > 8 ? (
              <details className="mt-1 rounded border border-border bg-background/60">
                <summary className="cursor-pointer px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                  ▾ 还有 {experiences.length - 8} 条
                </summary>
                <div className="border-t border-border px-2 py-2">
                  <JsonDump data={experiences.slice(8)} />
                </div>
              </details>
            ) : null}
          </SubBlock>
        ) : null}

        {chains?.length ? (
          <SubBlock title={`causal_chains(最近更新)× ${chains.length} · 因果链`} tone="muted">
            <p className="mb-2 text-[11px] text-muted-foreground">
              "什么类型的新闻 → 影响什么标的"的因果规则,权重越高越可信。Gate 会拿来做快速预判。
            </p>
            {chains.slice(0, 6).map((c, i) => (
              <div key={c.id || i} className="border-t border-border py-2 first:border-t-0 first:pt-0">
                <div className="mb-1 flex items-baseline gap-2">
                  <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-primary">
                    {String(c.name || '—')}
                  </span>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    weight={String(c.weight ?? '—')}
                  </span>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    enabled={String(c.enabled ?? '—')}
                  </span>
                </div>
                <div className="grid grid-cols-1 gap-x-4 gap-y-0.5 sm:grid-cols-2">
                  <div className="font-mono text-[10px] text-muted-foreground">cause</div>
                  <div className="text-[12px] text-foreground/90">{String(c.cause || '—')}</div>
                  <div className="font-mono text-[10px] text-muted-foreground">effect</div>
                  <div className="text-[12px] text-foreground/90">{String(c.effect || '—')}</div>
                </div>
                {c.trigger_words ? (
                  <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                    triggers: {String(c.trigger_words)}
                  </div>
                ) : null}
              </div>
            ))}
          </SubBlock>
        ) : null}
      </div>
    </SectionCard>
  )
}