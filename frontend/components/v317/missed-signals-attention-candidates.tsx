'use client'

/**
 * components/v317/missed-signals-attention-candidates.tsx
 * 漏检信号页 —— 「注意力先行候选」区块 (v317)
 *
 * 与既有 missed_signal 的分工（别混淆）：
 *   · 既有 missed_signal = **事后**判定：价格异动了，但当时系统给的是 HOLD；
 *   · 这里 = **事前**候选：价格还没动，但跨平台注意力已经出现，系统也还没出手。
 *
 * 数据全部来自真实抓取（Crowd Pulse）+ 真实行情（kline_bars），
 * 每个候选都带 explain 说明「凭什么算候选」，以及系统最近一次决策。
 */

import { useEffect, useState } from 'react'
import {
  fetchMissedSignalAttentionCandidates,
  type CrowdPendingCandidate,
} from '@/lib/crowd-data'

export function MissedSignalsAttentionCandidates({
  refreshMs = 60_000,
  externalRefreshToken = 0,
}: {
  refreshMs?: number
  externalRefreshToken?: number
}) {
  const [cands, setCands] = useState<CrowdPendingCandidate[]>([])
  const [note, setNote] = useState('')
  const [verdict, setVerdict] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    fetchMissedSignalAttentionCandidates({ lookback_hours: 72 })
      .then((r) => {
        if (!alive) return
        if (r.ok === false) {
          setErr(r.error || '接口返回失败')
          setCands([])
        } else {
          setCands(r.candidates || [])
          setNote(r.note || '')
          const v = (r.summary || {}).verdict
          setVerdict(
            v === 'attention_lags_price'
              ? '注意：实测判定为「价格先动、注意力后知」——媒体类注意力不是领先信号'
              : '',
          )
          setErr(null)
        }
      })
      .catch((e) => alive && setErr(String(e)))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [refreshMs, externalRefreshToken])

  return (
    <section className="rounded border border-border p-2">
      <div className="mb-1.5 flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-[11px] font-bold text-foreground">
          注意力先行候选
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">
          注意力已起 · 价格未动 · 系统未出手（事前候选，与事后 missed_signal 互补）
        </span>
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">
          {cands.length} 个
        </span>
      </div>

      {verdict && (
        <div className="mb-1 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 font-mono text-[11px] text-amber-500">
          {verdict}
        </div>
      )}
      {err && (
        <div className="mb-1 flex items-center gap-2 rounded border border-border bg-secondary/40 px-2 py-1 font-mono text-[11px] text-muted-foreground">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-hold/80" aria-hidden />
          <span className="truncate">读取失败：{err}</span>
        </div>
      )}
      {note && !err && (
        <div className="mb-1 font-mono text-[11px] text-muted-foreground">{note}</div>
      )}

      <div className="flex flex-col gap-1">
        {cands.map((c, i) => (
          <div
            key={`${c.symbol}-${c.attention_ts}-${i}`}
            className="rounded border border-border/60 px-2 py-1.5"
          >
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="font-mono text-[11px] font-bold text-amber-500">{c.symbol}</span>
              <span className="font-mono text-[11px] text-muted-foreground">
                {c.attention_at} UTC
              </span>
              <span className="font-mono text-[11px] text-muted-foreground">
                {c.platform_count} 平台（{c.platform_list.join(' · ')}）/ {c.item_count} 条
              </span>
              <span className="font-mono text-[11px] text-muted-foreground">
                最大波动 {c.max_abs_move_pct}% &lt; 阈值 {c.move_pct_threshold}%
              </span>
              <span className="font-mono text-[11px] text-muted-foreground">
                系统最近决策 {c.system_signal?.suggested_action || '无'}
              </span>
              {c.already_acted && (
                <span className="font-mono text-[11px] text-emerald-500">系统已出手</span>
              )}
            </div>
            <div className="mt-0.5 font-mono text-[11px] leading-snug text-muted-foreground">
              {c.explain}
            </div>
            {c.items?.length > 0 && (
              <div className="mt-0.5 flex flex-col gap-0.5">
                {c.items.slice(0, 2).map((it, j) => (
                  <a
                    key={j}
                    href={it.url}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate font-mono text-[11px] text-foreground/80 underline-offset-2 hover:text-primary hover:underline"
                    title={it.title}
                  >
                    [{it.media_channel || it.source}] {it.title}
                  </a>
                ))}
              </div>
            )}
          </div>
        ))}
        {!cands.length && !loading && !err && (
          <div className="font-mono text-[11px] text-muted-foreground">
            当前没有先行候选（不会用占位数据填充）
          </div>
        )}
        {loading && (
          <div className="font-mono text-[11px] text-muted-foreground">加载中…</div>
        )}
      </div>

      {cands.length > 0 && (
        <div className="mt-1 font-mono text-[11px] text-muted-foreground">
          判定依据：注意力取自带真实发布时间戳的跨平台条目；价格移动阈值 ±{cands[0].move_pct_threshold}%
          ，观察窗口 {cands[0].horizon_minutes} 分钟。
        </div>
      )}
      <div className="mt-1 font-mono text-[11px] text-muted-foreground">
        先行候选尚未兑现，这里只列事实，不给命中率口径。
      </div>
    </section>
  )
}

export default MissedSignalsAttentionCandidates