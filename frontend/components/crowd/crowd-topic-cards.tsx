'use client'

/**
 * components/crowd/crowd-topic-cards.tsx — 话题 × 观点（v318）
 *
 * 人话版的「市场上的人在关注什么、持什么观点」：
 *   · 关注什么 —— 话题卡片（题材 / 市场 / 谁在说 / 热度 / 权威度）
 *   · 持什么观点 —— 看多 / 看空 / 中性的**真实计数** + 立场结论
 *   · 说了什么 —— 展开看原话（标题 + 来源 + 可回溯 url）
 *
 * 口径全部来自后端 crowd_pulse.topic_overview()，前端不做二次推断。
 * 观点是**标题关键词判定**（不是语义模型），UI 上如实标注，
 * 不包装成"市场情绪指数"；"无明确方向"也不等于"看平"。
 */

import { useMemo, useState, type ReactNode } from 'react'
import type {
  CrowdTopic,
  CrowdTopicsResp,
  CrowdTopicSort,
  CrowdTopicKind,
} from '@/lib/crowd-data'

const STANCE_TEXT: Record<string, string> = {
  看多主导: 'text-long',
  看空主导: 'text-short',
  多空分歧: 'text-hold',
  无明确方向: 'text-muted-foreground',
}

const KIND_LABEL: Record<string, string> = {
  theme: '题材',
  news: '新闻',
  asset: '标的',
}

function Chip({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'accent' }) {
  return (
    <span
      className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${
        tone === 'accent'
          ? 'border-primary/40 bg-primary/10 text-primary'
          : 'border-border bg-muted/40 text-muted-foreground'
      }`}
    >
      {children}
    </span>
  )
}

/** 多空观点条：三段比例 + 立场结论（比例按条目数，不是按热度） */
function SentimentBar({ s }: { s: CrowdTopic['sentiment'] }) {
  const total = Math.max(1, s.total)
  const pct = (n: number) => (n / total) * 100
  const directed = s.bull + s.bear
  return (
    <div className="flex flex-col gap-1">
      <div className="flex h-2 w-full overflow-hidden rounded bg-muted/60">
        {s.bull > 0 && (
          <div className="h-full bg-long" style={{ width: `${pct(s.bull)}%` }} title={`看多 ${s.bull}`} />
        )}
        {s.neutral > 0 && (
          <div
            className="h-full bg-muted-foreground/30"
            style={{ width: `${pct(s.neutral)}%` }}
            title={`中性/无方向 ${s.neutral}`}
          />
        )}
        {s.bear > 0 && (
          <div className="h-full bg-short" style={{ width: `${pct(s.bear)}%` }} title={`看空 ${s.bear}`} />
        )}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[11px]">
        <span className="text-long">看多 {s.bull}</span>
        <span className="text-muted-foreground">中性 {s.neutral}</span>
        <span className="text-short">看空 {s.bear}</span>
        {directed > 0 ? (
          <span className={`font-bold ${STANCE_TEXT[s.stance] ?? ''}`}>
            → {s.stance}
            {s.bull > 0 && s.bear > 0 ? ` ${(s.bull / s.bear).toFixed(1)}:1` : ''}
          </span>
        ) : (
          <span className="text-muted-foreground">→ 标题里没有方向词（不等于看平）</span>
        )}
      </div>
    </div>
  )
}

/** 单个话题卡片：是什么 / 谁在说 / 什么观点 / 说了什么 */
function TopicCard({ t, maxHeat }: { t: CrowdTopic; maxHeat: number }) {
  const [open, setOpen] = useState(false)
  const heatPct = maxHeat > 0 ? Math.max(2, (t.heat_total / maxHeat) * 100) : 0
  const tags = [...new Set([...t.themes, ...t.markets])].filter(Boolean).slice(0, 6)
  const momentum = t.momentum
  return (
    <div className="rounded border border-border bg-card/60 p-2">
      <div className="flex items-start gap-2">
        <span className="w-5 shrink-0 pt-0.5 font-mono text-[11px] text-muted-foreground">
          {t.rank_no}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-[13px] font-bold leading-tight text-foreground">
              {t.display_name}
            </span>
            <span className="font-mono text-[11px] text-muted-foreground">
              {KIND_LABEL[t.topic_kind] ?? t.topic_kind}
            </span>
            {t.source_count >= 3 && (
              <span className="font-mono text-[11px] text-amber-500">
                {t.source_count} 个平台同时在聊
              </span>
            )}
            {momentum !== 0 && (
              <span className={`font-mono text-[11px] ${momentum > 0 ? 'text-long' : 'text-short'}`}>
                {momentum > 0 ? '升温' : '降温'} {momentum > 0 ? '+' : ''}
                {(momentum * 100).toFixed(0)}%
              </span>
            )}
          </div>

          <div className="mt-1 flex flex-wrap items-center gap-2">
            <div className="h-1.5 w-20 shrink-0 rounded bg-muted/50">
              <div className="h-1.5 rounded bg-primary/70" style={{ width: `${heatPct}%` }} />
            </div>
            <span className="font-mono text-[11px] text-muted-foreground">
              热度 {t.heat_total.toFixed(2)} · 权威度 {t.authority_score.toFixed(1)} ·{' '}
              {t.source_count} 平台 · {t.days_active} 天 · {t.mentions} 条提及
            </span>
          </div>

          <div className="mt-1.5">
            <SentimentBar s={t.sentiment} />
          </div>

          {tags.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {tags.map((x) => (
                <Chip key={x}>{x}</Chip>
              ))}
            </div>
          )}

          {t.platform_audiences.length > 0 && (
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              谁在说：{t.platform_audiences.join(' · ')}
            </div>
          )}

          <button
            onClick={() => setOpen(!open)}
            className="mt-1.5 font-mono text-[11px] text-primary hover:underline"
          >
            {open
              ? '▾ 收起原话'
              : `▸ 展开原话（${t.item_total} 条${
                  t.items.length < t.item_total ? `，显示最近 ${t.items.length} 条` : ''
                }）`}
          </button>

          {open && (
            <div className="mt-1 flex flex-col gap-1 border-l-2 border-border pl-2">
              {t.items.map((it, i) => (
                <div key={`${it.url}-${i}`} className="font-mono text-[11px] leading-tight">
                  <span className="text-muted-foreground">[{it.source}]</span>{' '}
                  {it.url ? (
                    <a
                      href={it.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-foreground hover:text-primary hover:underline"
                    >
                      {it.title}
                    </a>
                  ) : (
                    <span className="text-foreground">{it.title}</span>
                  )}
                </div>
              ))}
              {!t.items.length && (
                <div className="font-mono text-[11px] text-muted-foreground">
                  该话题暂无带 url 的原始条目
                </div>
              )}
              <div className="font-mono text-[11px] leading-tight text-muted-foreground">
                权威度依据：{t.explain}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

const SORTS: { key: CrowdTopicSort; label: string; hint: string }[] = [
  { key: 'authority', label: '跨平台共识', hint: '多个独立平台同时在讨论的，才算市场真在关注' },
  { key: 'heat', label: '热度', hint: '此刻各来源最热的条目' },
  { key: 'momentum', label: '升温', hint: '相对上一周期的热度变化 —— 什么正在起来' },
  { key: 'latest', label: '最新', hint: '最近才冒头的话题' },
  { key: 'bullish', label: '看多占比', hint: '标题方向词里看多比例最高的' },
]

const KINDS: { key: CrowdTopicKind; label: string; hint: string }[] = [
  { key: 'topic', label: '话题·讨论什么', hint: '题材 + 新闻 —— 大家在聊什么' },
  { key: 'asset', label: '标的·买什么', hint: '个股 / 币种 —— 大家在盯哪些票' },
  { key: '', label: '全部', hint: '话题 + 标的' },
]

const DAY_OPTIONS = [3, 5, 7, 14, 30]

type StanceFilter = 'all' | 'directed' | 'bull' | 'bear'

const STANCE_FILTERS: { key: StanceFilter; label: string }[] = [
  { key: 'all', label: '全部话题' },
  { key: 'directed', label: '有方向观点' },
  { key: 'bull', label: '看多主导' },
  { key: 'bear', label: '看空主导' },
]

export function CrowdTopicCards({
  data,
  sort,
  onSortChange,
  kind,
  onKindChange,
  days,
  onDaysChange,
}: {
  data: CrowdTopicsResp | null
  sort: CrowdTopicSort
  onSortChange: (s: CrowdTopicSort) => void
  kind: CrowdTopicKind
  onKindChange: (k: CrowdTopicKind) => void
  days: number
  onDaysChange: (d: number) => void
}) {
  const [kw, setKw] = useState('')
  const [stanceFilter, setStanceFilter] = useState<StanceFilter>('all')

  const topics = useMemo(() => {
    const list = data?.topics || []
    const k = kw.trim().toLowerCase()
    return list.filter((t) => {
      const s = t.sentiment
      if (stanceFilter === 'directed' && s.bull + s.bear === 0) return false
      if (stanceFilter === 'bull' && s.stance !== '看多主导') return false
      if (stanceFilter === 'bear' && s.stance !== '看空主导') return false
      if (!k) return true
      return (
        t.display_name.toLowerCase().includes(k) ||
        t.themes.some((x) => x.toLowerCase().includes(k)) ||
        t.markets.some((x) => x.toLowerCase().includes(k)) ||
        t.platform_audiences.some((x) => x.toLowerCase().includes(k))
      )
    })
  }, [data, kw, stanceFilter])

  const maxHeat = useMemo(() => Math.max(...topics.map((t) => t.heat_total), 0), [topics])

  if (!data) {
    return (
      <div className="font-mono text-[11px] text-muted-foreground">话题视图加载中…</div>
    )
  }

  const sum = data.summary

  return (
    <div className="flex flex-col gap-2">
      {/* ── 概览：一屏看清「关注什么 + 什么立场 + 谁在说」 ── */}
      <div className="grid grid-cols-1 gap-2 rounded border border-border bg-muted/20 p-2 lg:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <div className="font-mono text-[11px] text-foreground">
            <span className="font-bold">{data.window.label || '全部数据'}</span>
            <span className="text-muted-foreground">
              {' '}· {sum.mentions} 条讨论 · {sum.topic_count} 个话题 · {sum.platform_count} 个平台
            </span>
          </div>
          <div>
            <div className="mb-1 font-mono text-[11px] text-muted-foreground">
              整体观点（按条目数）
            </div>
            <SentimentBar s={sum.sentiment} />
          </div>
        </div>
        <div className="flex flex-col gap-1 font-mono text-[11px] leading-relaxed">
          <div>
            <span className="text-muted-foreground">热门题材：</span>
            {sum.top_themes.length
              ? sum.top_themes.map((t) => `${t.name} ${t.mentions}`).join(' · ')
              : '—'}
          </div>
          <div>
            <span className="text-muted-foreground">市场分布：</span>
            {sum.top_markets.length
              ? sum.top_markets.map((m) => `${m.name} ${m.mentions}`).join(' · ')
              : '—'}
          </div>
          <div>
            <span className="text-muted-foreground">人群圈层：</span>
            {sum.platform_mentions.length
              ? sum.platform_mentions.map((p) => `${p.audience} ${p.mentions}`).join(' · ')
              : '—'}
          </div>
          <div className="text-muted-foreground/80">{data.note}</div>
        </div>
      </div>

      {/* ── 筛选 ── */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <div className="flex items-center gap-1">
          {KINDS.map((k) => (
            <button
              key={k.key || 'all'}
              onClick={() => onKindChange(k.key)}
              title={k.hint}
              className={`rounded px-2 py-1 font-mono text-[11px] ${
                kind === k.key
                  ? 'border border-primary text-primary'
                  : 'border border-border text-muted-foreground hover:text-foreground'
              }`}
            >
              {k.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          {SORTS.map((s) => (
            <button
              key={s.key}
              onClick={() => onSortChange(s.key)}
              title={s.hint}
              className={`rounded px-2 py-1 font-mono text-[11px] ${
                sort === s.key
                  ? 'border border-primary text-primary'
                  : 'border border-border text-muted-foreground hover:text-foreground'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <span className="font-mono text-[11px] text-muted-foreground">窗口</span>
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              onClick={() => onDaysChange(d)}
              className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${
                days === d
                  ? 'border border-primary text-primary'
                  : 'border border-border text-muted-foreground hover:text-foreground'
              }`}
            >
              {d}天
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          {STANCE_FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setStanceFilter(f.key)}
              className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${
                stanceFilter === f.key
                  ? 'border border-primary text-primary'
                  : 'border border-border text-muted-foreground hover:text-foreground'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <input
          value={kw}
          onChange={(e) => setKw(e.target.value)}
          placeholder="搜话题 / 题材 / 市场…"
          className="rounded border border-border bg-background px-2 py-1 font-mono text-[11px] text-foreground placeholder:text-muted-foreground"
        />
      </div>

      {/* ── 话题卡片 ── */}
      <div className="flex flex-col gap-1.5">
        {topics.map((t) => (
          <TopicCard key={t.topic_key} t={t} maxHeat={maxHeat} />
        ))}
        {!topics.length && (
          <div className="rounded border border-border px-3 py-4 text-center font-mono text-[11px] text-muted-foreground">
            {data.count
              ? '当前筛选条件下没有话题（换个筛选或清空搜索）'
              : '暂无话题数据（采集尚未产出）'}
          </div>
        )}
      </div>
    </div>
  )
}

export default CrowdTopicCards
