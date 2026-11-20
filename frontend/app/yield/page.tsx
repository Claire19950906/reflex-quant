'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { CapabilityPanel } from '@/components/quant/capability-panel'

// v414 产能体检页 — 与 analysis_yield.py 同一口径（同一 report()），
// 页面数字 = CLI 数字 = 端点数字。处置口径由后端下发，这里不抄第二份。

type Bucket = {
  state: string
  label: string
  cn: string
  n: number
  share_of_all: number | null
  share_of_in_scope: number | null
  counts_as_failure: boolean
  todo: string
  retryable: boolean
}

type RetryItem = {
  news_id: number
  attempts: number
  last_at: string
  reason: string
  asset: string
  head: string
}

type Channel = {
  model_id: string
  calls: number
  ok_calls: number
  fail_rate: number | null
  short_rate: number | null
  empty_rate: number | null
  avg_out_tokens: number | null
  avg_latency_s: number | null
  tokens_in: number
  verdict: string
  why: string
}

type QueueHealth = {
  available: boolean
  hours: number
  pending: number
  pending_actionable: number
  noise_pending: number
  processing: number
  done: number
  filtered: number
  oldest_pending_age_min: number | null
  intake_per_h: number
  out_per_h: number
  decided_news_per_h: number
  eta_drain_h: number | null
  verdict: string
}

type Funnel = {
  available: boolean
  days: number
  inbound: number
  filtered: number
  analyzed: number
  frozen: number
  decided_news: number
  filter_rate: number | null
  analyze_rate: number | null
  filtered_by_gate: number
  filtered_gate_down: number
  gate_down_share: number | null
  analyzed_gate_down: number
  expired_gate_down: number
  verdict_tradeable_but_filtered: number
  filtered_with_asset: number
  note: string
}

type YieldData = {
  days: number
  totals: {
    all: number
    out_of_scope: number
    in_scope: number
    real_failure: number
    directional: number
    hard_status_known: number
    hard_status_unknown: number
  }
  rates: {
    complete_over_in_scope: number | null
    usable_over_in_scope: number | null
    real_failure_over_in_scope: number | null
    directional_per_day: number | null
    evidence_ready_over_known: number | null
  }
  buckets: Bucket[]
  retry: { n: number; items: RetryItem[] }
  channels: Channel[]
  queue: QueueHealth
  funnel: Funnel
  definitions: Record<string, string>
}

function pct(v: number | null) {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(1)}%`
}

export default function YieldPage() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<YieldData | null>(null)
  const [error, setError] = useState('')

  const load = useCallback((d: number) => {
    setError('')
    fetch(`${API_BASE}/analysis/yield?days=${d}`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) setData(j.data)
        else setError(j?.error || '读取失败')
      })
      .catch((e) => setError(e instanceof Error ? e.message : '读取失败'))
  }, [])

  useEffect(() => { load(days) }, [days, load])

  const r = data?.rates
  const t = data?.totals

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">产能体检</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            把「降级 X%」拆成五个能动手的桶 · 只读 · 口径来自 analysis/contract.py
          </p>
        </div>
        <div className="flex gap-1 font-mono text-[11px]">
          {[1, 7, 30].map((d) => (
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

      {error && (
        <div className="surface-card border border-red-500/30 p-3 text-xs text-red-400">{error}</div>
      )}

      {data && t && r && (
        <>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {[
              ['范围内决策', String(t.in_scope), `全部 ${t.all} 条，剔除越界 ${t.out_of_scope} 条`],
              ['完整率', pct(r.complete_over_in_scope), `可用率（含压缩输出）${pct(r.usable_over_in_scope)}`],
              ['真实失败率', pct(r.real_failure_over_in_scope), `分母是范围内 ${t.in_scope} 条`],
              ['方向性产出', `${(r.directional_per_day ?? 0).toFixed(2)} 条/日`, `共 ${t.directional} 条 BUY/SELL`],
            ].map(([k, v, n]) => (
              <div key={k} className="surface-card p-3">
                <div className="font-mono text-[11px] text-muted-foreground">{k}</div>
                <div className="mt-0.5 font-mono text-xl font-semibold">{v}</div>
                <div className="font-mono text-[11px] text-muted-foreground">{n}</div>
              </div>
            ))}
          </div>

          <div className="surface-card mt-3 p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              终态分布与处置
            </div>
            <table className="w-full font-mono text-[11px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border/60">
                  {['终态', '条数', '占范围内', '算失败', '今天该做什么'].map((h, i) => (
                    <th key={h} className={`py-1.5 font-normal ${i === 0 || i === 4 ? 'text-left' : 'text-right'}`}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.buckets.map((b) => (
                  <tr key={b.state} className="border-b border-border/30 align-top">
                    <td className="py-1.5">
                      {b.cn}
                      <span className="ml-1 text-muted-foreground/60">{b.state}</span>
                    </td>
                    <td className="py-1.5 text-right">{b.n}</td>
                    <td className="py-1.5 text-right">{b.share_of_in_scope === null ? '—' : pct(b.share_of_in_scope)}</td>
                    <td className="py-1.5 text-right">{b.counts_as_failure ? '是' : '否'}</td>
                    <td className="py-1.5 pl-3 text-left text-muted-foreground">{b.todo}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="surface-card mt-3 p-3">
            <div className="mb-2 flex items-center justify-between font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              <span>可重试清单（模型没返回、之后没成功过）</span>
              <span>{data.retry.n} 条</span>
            </div>
            {data.retry.items.length === 0 ? (
              <div className="text-xs text-muted-foreground">这个窗口没有可重试的新闻。</div>
            ) : (
              <table className="w-full font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    {['news_id', '次数', '最近一次', '品种', '标题'].map((h, i) => (
                      <th key={h} className={`py-1.5 font-normal ${i === 0 || i === 4 ? 'text-left' : 'text-right'}`}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.retry.items.map((x) => (
                    <tr key={x.news_id} className="border-b border-border/30">
                      <td className="py-1.5">{x.news_id}</td>
                      <td className="py-1.5 text-right">{x.attempts}</td>
                      <td className="py-1.5 text-right text-muted-foreground">{x.last_at?.slice(5, 16) || '—'}</td>
                      <td className="py-1.5 text-right">{x.asset || '—'}</td>
                      <td className="max-w-[520px] truncate py-1.5 pl-3 text-left text-muted-foreground">{x.head || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="mt-2 font-mono text-[11px] text-muted-foreground/70">
              只列「模型侧失败」；结构不达标（parse_failed）原样重跑大概率还是失败，应改 prompt/契约，不在这里凑数。
            </p>
          </div>

          <div className="surface-card mt-3 p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              模型通道质量（只看输出够不够，不看输出多少）
            </div>
            {data.channels.length === 0 ? (
              <div className="text-xs text-muted-foreground">没有可读的调用统计（ai_call_stats 为空或不存在）。</div>
            ) : (
              <table className="w-full font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    {['model_id', '调用', '失败率', '短输出', '空输出', '平均输出', '均延迟', '判定'].map((h, i) => (
                      <th key={h} className={`py-1.5 font-normal ${i === 0 || i === 7 ? 'text-left' : 'text-right'}`}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.channels.map((c) => (
                    <tr key={c.model_id} className="border-b border-border/30 align-top">
                      <td className="py-1.5">{c.model_id}</td>
                      <td className="py-1.5 text-right">{c.calls}</td>
                      <td className="py-1.5 text-right">{pct(c.fail_rate)}</td>
                      <td className="py-1.5 text-right">{pct(c.short_rate)}</td>
                      <td className="py-1.5 text-right">{pct(c.empty_rate)}</td>
                      <td className="py-1.5 text-right">{c.avg_out_tokens ?? '—'}</td>
                      <td className="py-1.5 text-right">{c.avg_latency_s ?? '—'}s</td>
                      <td className="py-1.5 pl-3 text-left">
                        <span className={c.verdict === '正常' ? 'text-muted-foreground' : 'text-amber-400'}>
                          {c.verdict}
                        </span>
                        {c.why && <span className="ml-1 text-muted-foreground/70">{c.why}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="mt-2 font-mono text-[11px] text-muted-foreground/70">
              短输出 = tokens_out &lt; 600（八段写不完）。阈值 20% / 40% 是看板口径、未回测，只用来抓明显异常，
              不构成「该通道不行」的统计结论；调用数 &lt; 20 一律报「样本不足」。
            </p>
          </div>

          {data.queue?.available && (
            <div className="surface-card mt-3 p-3">
              <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                <span>队列水位（近 {data.queue.hours} 小时口径）</span>
                <span
                  className={
                    data.queue.verdict.startsWith('正常') || data.queue.verdict.startsWith('队列已清空')
                      ? 'text-muted-foreground'
                      : 'text-amber-400'
                  }
                >
                  {data.queue.verdict}
                </span>
              </div>
              <div className="grid gap-2 font-mono text-[11px] sm:grid-cols-2 lg:grid-cols-5">
                {[
                  ['PENDING 合计', `${data.queue.pending}`],
                  ['其中可分析', `${data.queue.pending_actionable}`],
                  ['噪音滞留', `${data.queue.noise_pending}`],
                  ['进', `${data.queue.intake_per_h} 条/时`],
                  ['出', `${data.queue.out_per_h} 条/时`],
                  ['排空约', data.queue.eta_drain_h === null ? '算不出' : `${data.queue.eta_drain_h} 小时`],
                ].map(([k, v]) => (
                  <div key={k} className="rounded-sm border border-border/60 px-2 py-1.5">
                    <div className="text-muted-foreground">{k}</div>
                    <div className="mt-0.5">{v}</div>
                  </div>
                ))}
              </div>
              <p className="mt-2 font-mono text-[11px] text-muted-foreground/70">
                「出」= 近 N 小时离开 PENDING 的条数，含被门卫否掉的，不等于出决策
                （另有 {data.queue.decided_news_per_h} 条新闻/时真正产出了决策）。
                「PENDING 合计」含噪音滞留（`is_noise=1`，认领 SQL 不收，只会等清理归档）；
                真正要排的空是「其中可分析」。
                最老可分析 PENDING 年龄：{data.queue.oldest_pending_age_min === null ? '—' : `${data.queue.oldest_pending_age_min} 分钟`}。
                排空估算用「出」做分母，出速率为 0 时不下结论。
              </p>
            </div>
          )}

          {data.funnel?.available && (
            <div className="surface-card mt-3 p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                入口漏斗（近 {data.funnel.days} 天）—— 产能卡在哪一层
              </div>
              <div className="grid gap-2 font-mono text-[11px] sm:grid-cols-2 lg:grid-cols-4">
                {[
                  ['进来', String(data.funnel.inbound), 'is_noise=0 的新新闻'],
                  ['被否掉', `${data.funnel.filtered} (${pct(data.funnel.filter_rate)})`, 'FILTERED'],
                  ['进分析', `${data.funnel.analyzed} (${pct(data.funnel.analyze_rate)})`, 'DONE + PROCESSING'],
                  ['出决策新闻', String(data.funnel.decided_news), '按 news_id 去重（已拆多品种）'],
                ].map(([k, v, n]) => (
                  <div key={k} className="rounded-sm border border-border/60 px-2 py-1.5">
                    <div className="text-muted-foreground">{k}</div>
                    <div className="mt-0.5">{v}</div>
                    <div className="mt-0.5 text-muted-foreground/70">{n}</div>
                  </div>
                ))}
              </div>
              <table className="mt-3 w-full font-mono text-[11px]">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border/60">
                    <th className="py-1.5 text-left font-normal">分层 / 可疑项</th>
                    <th className="py-1.5 text-right font-normal">条数</th>
                    <th className="py-1.5 pl-3 text-left font-normal">说明</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ['门卫有判定后否', data.funnel.filtered_by_gate, '真正的过滤（gate_unavailable=0）'],
                    ['门卫不可用·降级放行后仍被否', data.funnel.filtered_gate_down, 'gate_unavailable=2，等于白丢'],
                    ['门卫不可用·仍出决策', data.funnel.analyzed_gate_down, '决策缺 gate_context，质量要打问号'],
                    ['过 24h 宽限才归档', data.funnel.expired_gate_down, 'v419 起才允许，留痕 expired_gate_down 可回放'],
                    ['冻结待回放', data.funnel.frozen, 'gate_unavailable=1，等回放补判'],
                    ['门卫判「可交易」却被否', data.funnel.verdict_tradeable_but_filtered, '最可疑，优先抽样'],
                    ['被否但门卫给了标的', data.funnel.filtered_with_asset, '有明确对象却整条丢弃'],
                  ].map(([label, n, why]) => (
                    <tr key={String(label)} className="border-b border-border/30">
                      <td className="py-1.5">{label}</td>
                      <td className="py-1.5 text-right">{String(n)}</td>
                      <td className="py-1.5 pl-3 text-muted-foreground">{why}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 font-mono text-[11px] text-muted-foreground/70">{data.funnel.note}</p>
            </div>
          )}

          <div className="surface-card mt-3 p-3">
            <dl className="space-y-1 font-mono text-[11px]">
              {Object.entries(data.definitions).map(([k, v]) => (
                <div key={k} className="grid grid-cols-[220px_1fr] gap-2">
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
              <div className="grid grid-cols-[220px_1fr] gap-2">
                <dt className="text-muted-foreground">证据状态未知</dt>
                <dd>{t.hard_status_unknown} 条（未写入 hard_data_status，单列，不参与就绪率分母）</dd>
              </div>
            </dl>
          </div>
        </>
      )}

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="系统健康能力"
        hint="改动审计 / 告警分级 / 异动四件套 / 策略健康 / 执行链路 / 数据源体检"
        featureIds={['F02','F35','F36','F45','F46','F47']}
      />
      </section>
    </main>
  )
}
