'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v422 未识别标的 TOP 榜 — 回答"routing_miss 那一半决策，新闻到底提到了什么品种"。
// 数据全部只读（routers/targets.py → target_resolver.py），这里不做任何判定。

type Sample = { news_id: number; source: string; at: string; head: string }
type TargetRow = {
  target: string
  count: number
  share: number | null
  aliases: string[]
  samples: Sample[]
  why?: string
}
type Data = {
  total: number
  with_candidate: number
  recover_rate_upper: number | null
  targets: TargetRow[]
  no_clue: number
  no_clue_samples: Sample[]
  window_days: number
  scanned: number
  elapsed_s: number
  caveat: string
}

const pct = (v: number | null) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`)

export default function TargetsPage() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<Data | null>(null)
  const [err, setErr] = useState('')

  const load = useCallback((d: number) => {
    fetch(`${API_BASE}/targets/unresolved?days=${d}`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) { setData(j.data); setErr('') }
        else setErr(j?.error || '读取失败')
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'))
  }, [])

  useEffect(() => { load(days) }, [days, load])

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">未识别标的 TOP 榜</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            这些新闻被判成「看懂了但没落到品种」— 榜上是原文里明明提到了的品种 · 只读
          </p>
        </div>
        <div className="flex gap-1 font-mono text-[11px]">
          {[1, 7, 14, 30].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`rounded-sm border px-2 py-1 ${
                days === d ? 'border-primary text-primary' : 'border-border text-muted-foreground'
              }`}
            >
              近 {d} 天
            </button>
          ))}
        </div>
      </header>

      {err && <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">{err}</div>}

      {data && (
        <>
          <div className="surface-card mb-3 grid grid-cols-2 gap-3 p-3 font-mono text-[11px] md:grid-cols-4">
            <div>
              <div className="text-muted-foreground">扫描条数（去重后）</div>
              <div className="text-lg">{data.total}</div>
            </div>
            <div>
              <div className="text-muted-foreground">原文能认出品种</div>
              <div className="text-lg text-amber-400">{data.with_candidate}</div>
            </div>
            <div>
              <div className="text-muted-foreground">上限救回率</div>
              <div className="text-lg">{pct(data.recover_rate_upper)}</div>
            </div>
            <div>
              <div className="text-muted-foreground">一个字都认不出</div>
              <div className="text-lg">{data.no_clue}</div>
            </div>
          </div>

          <div className="surface-card mb-3 p-3">
            <div className="mb-2 flex items-baseline justify-between font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              <span>被漏掉的品种</span>
              <span>按命中条数排序 · 一条新闻最多计 2 个品种</span>
            </div>
            {data.targets.length === 0 ? (
              <div className="text-xs text-muted-foreground">这段时间没有"能认出品种"的漏判。</div>
            ) : (
              <table className="w-full font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    {['品种', '条数', '占扫描', '命中的写法', '为什么会被漏'].map((h, i) => (
                      <th key={h} className={`py-1.5 font-normal ${i === 0 ? 'text-left' : i < 3 ? 'text-right' : 'text-left'}`}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.targets.map((t) => (
                    <tr key={t.target} className="border-b border-border/30 align-top">
                      <td className="py-1.5 font-semibold">{t.target}</td>
                      <td className="py-1.5 text-right">{t.count}</td>
                      <td className="py-1.5 text-right">{pct(t.share)}</td>
                      <td className="py-1.5 pl-3 text-left text-muted-foreground">
                        {t.aliases.slice(0, 4).join(' / ')}
                      </td>
                      <td className="py-1.5 pl-3 text-left text-muted-foreground/80">{t.why || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {data.targets[0]?.samples?.length > 0 && (
            <div className="surface-card mb-3 p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                Top 品种样本（{data.targets[0].target}）
              </div>
              <ul className="space-y-1 text-[11px] text-muted-foreground">
                {data.targets[0].samples.map((s) => (
                  <li key={s.news_id} className="line-clamp-1">
                    <span className="text-muted-foreground/60">#{s.news_id} {s.at}</span> {s.head}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="surface-card p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              认不出品种的样本（{data.no_clue} 条里抽几条）
            </div>
            {data.no_clue_samples.length === 0 ? (
              <div className="text-xs text-muted-foreground">没有样本。</div>
            ) : (
              <ul className="space-y-1 text-[11px] text-muted-foreground">
                {data.no_clue_samples.map((s) => (
                  <li key={s.news_id} className="line-clamp-2">
                    <span className="text-muted-foreground/60">#{s.news_id} {s.source}</span> {s.head}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <p className="mt-3 font-mono text-[11px] text-muted-foreground/70">{data.caveat}</p>
          <p className="mt-1 font-mono text-[11px] text-muted-foreground/70">
            扫描 {data.scanned} 条 · 耗时 {data.elapsed_s}s · 窗口 {data.window_days} 天。
            加别名走 `POST /api/targets/aliases`（只写 config 文件，不碰生产库）。
          </p>
        </>
      )}
    </main>
  )
}
