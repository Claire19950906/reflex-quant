'use client'

import { useEffect, useMemo, useState } from 'react'
import { Target, Flame, TrendingUp, Coins, BookMarked, Sparkles, RefreshCw } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { PushToCausalChainButton } from './push-to-causal-chain-button'
import { cn } from '@/lib/utils'

/**
 * 🎯 强信息驱动资产猎手(Asset Scout)
 *
 * 用户原话:「怎么识别其他强信息驱动的金融产品?
 *   海力士、其他被超级低估的超级股票,山寨币、代币」
 *
 * 三个维度扫描:
 *   · 🟠 消息股:近期新闻/异动 → 个股消息密度高的(海力士、苹果、特斯拉...)
 *   · 🔥 山寨币 / 代币:coingecko 中 24h 涨幅异常 + 流动性中等(高波动机会)
 *   · 💎 超级低估股票:基本面 + 技术面 → 长期下跌但企业基本面没恶化(均值回归)
 *
 * 数据源:
 *   · /api/market/movers — 涨跌幅排行榜
 *   · /api/discovery/asset-scout — 综合打分
 *   · 兜底:仅显示 movers
 */

type ScoutBucket = 'news_stock' | 'altcoin' | 'undervalued' | 'event_token'

type ScoutItem = {
  symbol: string
  name: string
  bucket: ScoutBucket
  score: number           // 0-100 综合分
  price: number | null
  change_24h: number | null
  drivers: string[]       // 最近驱动这个标的事件的新闻
  source_count: number
  class?: string
  hint?: string
}

type ScoutResponse = {
  ok: boolean
  ts: number
  items: ScoutItem[]
  news_density: Array<{ symbol: string; count: number; hot_topics: string[] }>
}

const BUCKET_META: Record<ScoutBucket, { label: string; icon: string; cls: string }> = {
  news_stock: { label: '消息股',   icon: '🟠', cls: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300' },
  altcoin:    { label: '山寨币',   icon: '🔥', cls: 'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300' },
  undervalued:{ label: '超级低估', icon: '💎', cls: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' },
  event_token:{ label: '事件代币', icon: '⚡', cls: 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300' },
}

const SAMPLE: ScoutResponse = {
  ok: true,
  ts: Date.now() / 1000,
  items: [],
  news_density: [],
}

export function AssetScoutPanel() {
  const [activeBucket, setActiveBucket] = useState<ScoutBucket | 'all'>('all')

  // v283+ · Task D 收尾 · scout 主源失败时启用 movers 兜底(原 reload 双接口 fallback 拆成 2 个 usePoll)
  // · 顺便修了 #7 clearTimeout 清理 setInterval 的语义错
  const {
    data: scout,
    loading: scoutLoading,
    error: scoutErr,
    refresh: reload,
  } = usePoll<ScoutResponse>('/discovery/asset-scout', {
    intervalMs: 300_000,
    transform: (raw) => {
      const d = raw as ScoutResponse | null
      if (!d) return null
      if ('ok' in d && d.ok === false) return null
      return d
    },
  })

  // v283+ · 一旦 scout 报错过就启用 movers 兜底(不撤回,避免反复 toggle)
  const [moversEnabled, setMoversEnabled] = useState(false)
  useEffect(() => {
    if (scoutErr) setMoversEnabled(true)
  }, [scoutErr])

  const { data: movers, loading: moversLoading } = usePoll<{ items?: unknown[] }>(
    '/market/movers',
    {
      intervalMs: 300_000,
      enabled: moversEnabled,
    },
  )

  // 合并数据:scout 优先,movers 兜底转 ScoutItem[]
  const data: ScoutResponse = useMemo(() => {
    if (scout && scout.items && scout.items.length > 0) return scout
    const rawItems = (movers?.items as Array<Record<string, unknown>> | undefined) ?? []
    if (rawItems.length === 0) return scout ?? { ok: true, ts: 0, items: [], news_density: [] }
    const items: ScoutItem[] = rawItems.slice(0, 20).map((it) => {
      // v282+: HONESTY — change24h 为 null 时 score 不能硬编码为 30 (假数据)
      const ch24 = typeof it.change24h === 'number' ? it.change24h : null
      const score = ch24 == null
        ? 0  // 无 24h 数据 → score=0,让"信号强度"诚实归零
        : Math.min(100, Math.abs(ch24) * 15 + 30)
      const sym = String(it.asset || '')
      return {
        symbol: sym,
        name: sym,
        bucket: (sym.includes('BTC') || sym.includes('ETH') || sym.includes('SOL'))
          ? 'altcoin' : 'news_stock',
        score,
        price: typeof it.price === 'number' ? it.price : null,
        change_24h: ch24,
        drivers: [],
        source_count: typeof it.source_count === 'number' ? it.source_count : 0,
      }
    })
    return { ok: true, ts: Date.now() / 1000, items, news_density: [] }
  }, [scout, movers])

  // 只有"两个都失败"才显示错误(跟原版 catch 嵌套语义一致)
  // movers 启用后如果拉到了 data → 兜底成功,err 自动清空
  const moversFetched = moversEnabled && !!movers
  const err = scoutErr && !moversFetched ? scoutErr : null
  const loading = scoutLoading || moversLoading
  const filteredItems = data.items.filter((it) => activeBucket === 'all' || it.bucket === activeBucket) ?? []

  return <AssetScoutBody data={data} loading={loading} err={err} activeBucket={activeBucket} setActiveBucket={setActiveBucket} filteredItems={filteredItems} reload={reload} />
}

function AssetScoutBody({ data, loading, err, activeBucket, setActiveBucket, filteredItems, reload }: {
  data: ScoutResponse | null
  loading: boolean
  err: string | null
  activeBucket: ScoutBucket | 'all'
  setActiveBucket: (b: ScoutBucket | 'all') => void
  filteredItems: ScoutItem[]
  reload: () => void
}) {
  return (
    <div className="rounded border border-fuchsia-500/40 bg-fuchsia-500/5 p-3">
      <div className="flex items-center gap-2 font-mono text-xs">
        <Target className="h-4 w-4 text-fuchsia-500" aria-hidden />
        <span className="font-bold uppercase tracking-widest text-fuchsia-700 dark:text-fuchsia-300">
          🎯 强信息驱动资产猎手
        </span>
        <button onClick={reload} disabled={loading} className="ml-auto rounded border border-border px-4 py-1 text-[10px] hover:bg-secondary/50 disabled:opacity-40">
          <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
        </button>
      </div>

      <p className="mt-2 text-[11px] leading-7 text-muted-foreground">
        三维扫描:<strong className="text-foreground">消息股</strong>(海力士等消息密度高 + 短期价格异动)、
        <strong className="text-foreground">山寨币</strong>(24h 涨幅异常 + 流动性筛)、
        <strong className="text-foreground">超级低估</strong>(长期下跌但基本面没崩 + 技术超卖)。
      </p>

      {err && (
        <div className="mt-2 rounded border border-rose-300 bg-rose-50 p-2 text-xs dark:border-rose-700 dark:bg-rose-950">
          <div className="font-semibold text-rose-700 dark:text-rose-300">⚠️ 资产猎手接口异常(已用 movers 兜底)</div>
          <div className="mt-1 text-rose-600 dark:text-rose-400">{err}</div>
        </div>
      )}

      <div className="mt-2 flex flex-wrap gap-1.5">
        <button onClick={() => setActiveBucket('all')}
          className={cn('rounded border px-2 py-0.5 font-mono text-[10px]', activeBucket === 'all'
            ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-secondary/30 text-muted-foreground hover:text-foreground')}>
          全部 ({data?.items.length ?? 0})
        </button>
        {(Object.keys(BUCKET_META) as ScoutBucket[]).map((b) => {
          const meta = BUCKET_META[b]
          const count = data?.items.filter((it) => it.bucket === b).length ?? 0
          return (
            <button key={b} onClick={() => setActiveBucket(b)}
              className={cn('rounded border px-2 py-0.5 font-mono text-[10px]', activeBucket === b
                ? meta.cls : 'border-border bg-secondary/30 text-muted-foreground hover:text-foreground')}>
              {meta.icon} {meta.label} ({count})
            </button>
          )
        })}
      </div>

      <div className="mt-2 grid grid-cols-1 gap-1.5 md:grid-cols-2">
        {!data && <div className="text-xs text-muted-foreground">{loading ? '扫描中…' : '等待数据'}</div>}
        {filteredItems.slice(0, 12).map((it) => {
          const meta = BUCKET_META[it.bucket]
          const changePct = it.change_24h ?? 0
          return (
            <div key={it.symbol + it.bucket} className={cn('rounded border p-2', meta.cls)}>
              <div className="flex items-center gap-1.5">
                <span aria-hidden>{meta.icon}</span>
                <strong className="font-mono text-[11px]">{it.symbol}</strong>
                <span className="text-[10px] text-muted-foreground truncate">{it.name}</span>
                <span className="ml-auto font-mono text-[10px] font-bold">
                  {changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%
                </span>
              </div>
              <div className="mt-1 flex items-center gap-1.5 text-[9px]">
                <span className="font-mono">综合 {it.score.toFixed(0)}/100</span>
                {it.source_count > 0 && <span className="text-muted-foreground">· 源 {it.source_count}</span>}
                {it.price != null && <span className="text-muted-foreground">· ${it.price.toLocaleString()}</span>}
              </div>
              {it.drivers.length > 0 && (
                <ul className="mt-1 space-y-0.5 text-[10px] text-foreground/80">
                  {it.drivers.slice(0, 2).map((d, i) => <li key={i}>· {d}</li>)}
                </ul>
              )}
              {it.hint && <div className="mt-1 text-[9px] text-muted-foreground/80">💡 {it.hint}</div>}
              {/* v283+ · #23/#31 · 一键投到因果链,后端 /api/causal/chains */}
              <div className="mt-1.5">
                <PushToCausalChainButton origin="manual_from_asset_scout"

                  cause={`asset-scout 命中: ${BUCKET_META[it.bucket].label}`}
                  effect={it.symbol}
                  note={`score=${it.score.toFixed(0)} 24h=${it.change_24h?.toFixed(2) ?? 'n/a'} drivers=${it.drivers.slice(0,2).join('; ')}`}
                  trigger_words={[it.symbol, BUCKET_META[it.bucket].label, 'asset-scout'].join(',')}
                  weight={0.5}
                />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}