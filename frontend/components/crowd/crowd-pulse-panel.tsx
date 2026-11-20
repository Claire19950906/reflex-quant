'use client'

/**
 * components/crowd/crowd-pulse-panel.tsx — Crowd Pulse 主面板 (v316)
 *
 * 三个视图：
 *   榜单 —— 月度权威度榜（每条带 explain，可追问"凭什么"）
 *   人群 —— 9 个维度的人群画像分群（占比条）
 *   信源 —— 各数据源权威度 + 最近抓到的原始条目（可回溯 url）
 *
 * 数据全部来自真实抓取；后端已做归一（热度按源归一、交叉验证按平台计），
 * 前端不做任何二次推断。
 */

import { useEffect, useState } from 'react'
import {
  fetchCrowdAttentionLead,
  fetchCrowdAudience,
  fetchCrowdItems,
  fetchCrowdPendingAttention,
  fetchCrowdRanking,
  fetchCrowdSources,
  fetchCrowdTopics,
  type CrowdAttentionResp,
  type CrowdAudienceResp,
  type CrowdItem,
  type CrowdPendingCandidate,
  type CrowdRankingItem,
  type CrowdRankingSort,
  type CrowdSourceAuth,
  type CrowdTopicKind,
  type CrowdTopicSort,
  type CrowdTopicsResp,
} from '@/lib/crowd-data'
import { CrowdAttentionLead } from './crowd-attention-lead'
import { CrowdAudienceGrid } from './crowd-audience-grid'
import { CrowdRankingTable } from './crowd-ranking-table'
import { CrowdSourcesPanel } from './crowd-sources-panel'
import { CrowdTopicCards } from './crowd-topic-cards'

const REFRESH_MS = 120_000

type Tab = 'topics' | 'ranking' | 'audience' | 'sources' | 'attention'

const TABS: { key: Tab; label: string }[] = [
  { key: 'topics', label: '话题与观点' },
  { key: 'ranking', label: '权威度榜单' },
  { key: 'audience', label: '人群画像' },
  { key: 'sources', label: '数据源与原始条目' },
  { key: 'attention', label: '注意力领先' },
]

export function CrowdPulsePanel() {
  const [tab, setTab] = useState<Tab>('topics')
  const [sort, setSort] = useState<CrowdRankingSort>('authority')
  const [ranking, setRanking] = useState<CrowdRankingItem[]>([])
  const [audience, setAudience] = useState<CrowdAudienceResp | null>(null)
  const [sources, setSources] = useState<CrowdSourceAuth[]>([])
  const [items, setItems] = useState<CrowdItem[]>([])
  const [attention, setAttention] = useState<CrowdAttentionResp | null>(null)
  const [pending, setPending] = useState<CrowdPendingCandidate[]>([])
  const [pendingNote, setPendingNote] = useState('')
  // v318: 话题 × 观点（默认视图 —— 直接回答"关注什么 + 持什么观点"）
  const [topics, setTopics] = useState<CrowdTopicsResp | null>(null)
  const [topicSort, setTopicSort] = useState<CrowdTopicSort>('authority')
  const [topicKind, setTopicKind] = useState<CrowdTopicKind>('topic')
  const [topicDays, setTopicDays] = useState(5)
  const [topicErr, setTopicErr] = useState<string | null>(null)
  const [month, setMonth] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  // 话题 × 观点：独立取数（换筛选/排序只重拉这一份，不动其它面板）
  useEffect(() => {
    let alive = true
    fetchCrowdTopics({
      days: topicDays,
      sort: topicSort,
      kind: topicKind,
      limit: 60,
      item_limit: 5,
    })
      .then((d) => {
        if (!alive) return
        setTopics(d)
        setTopicErr(null)
      })
      .catch((e) => {
        if (!alive) return
        setTopicErr(String(e))
        setTopics(null)
      })
    return () => {
      alive = false
    }
  }, [topicSort, topicKind, topicDays, tick])

  useEffect(() => {
    let alive = true
    setLoading(true)
    Promise.all([
      fetchCrowdRanking({ limit: 60, sort }),
      fetchCrowdAudience(),
      fetchCrowdSources(),
      fetchCrowdItems({ limit: 40 }),
      // v317: 注意力领先性检验 + 先行漏检候选
      fetchCrowdAttentionLead({ lookback_hours: 48, limit: 40 }),
      fetchCrowdPendingAttention({ lookback_hours: 48 }),
    ])
      .then(([r, a, s, it, al, pa]) => {
        if (!alive) return
        setRanking(r.ranking || [])
        setMonth(r.month || '')
        setAudience(a)
        setSources(s.sources || [])
        setItems(it.items || [])
        setAttention(al)
        setPending(pa.pending || [])
        setPendingNote(pa.pending_note || '')
        setErr(null)
      })
      .catch((e) => alive && setErr(String(e)))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [sort, tick])

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-2">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`rounded px-2 py-1 font-mono text-[11px] ${
              tab === t.key
                ? 'bg-primary text-primary-foreground'
                : 'border border-border text-muted-foreground hover:text-foreground'
            }`}
          >
            {t.label}
          </button>
        ))}
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">
          {month ? `数据月份 ${month}` : '暂无数据'} · 每 {REFRESH_MS / 1000}s 刷新
        </span>
      </div>

      {err && (
        <div className="flex items-start gap-2 rounded border border-border bg-secondary/40 px-3 py-2 text-muted-foreground">
          <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-hold/80" aria-hidden />
          <span className="min-w-0">
            <span className="text-[11px]">人群数据暂不可用</span>
            <span className="block truncate font-mono text-[11px] opacity-70">读取失败：{err}</span>
          </span>
        </div>
      )}
      {loading && !err && (
        <div className="font-mono text-[11px] text-muted-foreground">加载中…</div>
      )}

      {tab === 'topics' && (
        <div className="flex flex-col gap-2">
          {topicErr ? (
            <div className="flex items-start gap-2 rounded border border-border bg-secondary/40 px-3 py-2 text-muted-foreground">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-hold/80" aria-hidden />
              <span className="min-w-0">
                <span className="text-[11px]">话题视图暂不可用</span>
                <span className="block truncate font-mono text-[11px] opacity-70">话题视图读取失败：{topicErr}</span>
              </span>
            </div>
          ) : (
            <CrowdTopicCards
              data={topics}
              sort={topicSort}
              onSortChange={setTopicSort}
              kind={topicKind}
              onKindChange={setTopicKind}
              days={topicDays}
              onDaysChange={setTopicDays}
            />
          )}
        </div>
      )}
      {tab === 'ranking' && (
        <CrowdRankingTable ranking={ranking} sort={sort} onSortChange={setSort} />
      )}
      {tab === 'audience' && <CrowdAudienceGrid audience={audience} />}
      {tab === 'sources' && <CrowdSourcesPanel sources={sources} items={items} />}
      {tab === 'attention' && (
        <CrowdAttentionLead data={attention} pending={pending} pendingNote={pendingNote} />
      )}
    </div>
  )
}

export default CrowdPulsePanel
