'use client'

/**
 * components/crowd/crowd-attention-lead.tsx — 注意力是否先于价格移动 (v317)
 *
 * 这个视图回答的是「人群注意力有没有增量预测力」，不是「注意力之后涨没涨」。
 * 两者差别很大：媒体往往是被价格波动触发去写报道的，
 * 所以只看「注意力之后有没有涨」会得到一个虚高的假象。
 *
 * 因此这里同时展示三组数：
 *   表面命中率 —— 注意力之后窗口内有没有价格移动（会虚高）
 *   反向对照   —— 注意力之前价格是否已经动过（比例高 = 媒体在追价格）
 *   干净子集   —— 剔除「价格已先动」后的命中率（真正的增量预测力）
 * 以及一个明确判定 verdict，而不是让用户自己看数字猜。
 */

import type {
  CrowdAttentionEvent,
  CrowdAttentionResp,
  CrowdPendingCandidate,
} from '@/lib/crowd-data'

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return `${(n * 100).toFixed(1)}%`
}

function num(n: number | null | undefined, suffix = ''): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return `${n}${suffix}`
}

const VERDICT_STYLE: Record<string, { text: string; cls: string }> = {
  attention_leads_price: {
    text: '注意力领先价格移动（反向对照未推翻）',
    cls: 'text-emerald-500',
  },
  attention_lags_price: {
    text: '价格先动、注意力后知 —— 注意力不领先，多数情况是媒体追价格',
    cls: 'text-amber-500',
  },
  inconclusive: { text: '证据不足，无法判定方向', cls: 'text-muted-foreground' },
  no_data: { text: '无可对照的行情数据', cls: 'text-muted-foreground' },
}

function Metric({
  label,
  value,
  hint,
  tone = 'text-foreground',
}: {
  label: string
  value: string
  hint?: string
  tone?: string
}) {
  return (
    <div className="rounded border border-border/70 px-2 py-1.5">
      <div className="font-mono text-[11px] text-muted-foreground">{label}</div>
      <div className={`font-mono text-[13px] ${tone}`}>{value}</div>
      {hint && (
        <div className="font-mono text-[11px] leading-tight text-muted-foreground">{hint}</div>
      )}
    </div>
  )
}


function EventRow({ ev }: { ev: CrowdAttentionEvent }) {
  const prior = ev.reverse?.prior_move
  return (
    <div className="rounded border border-border/60 px-2 py-1.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-[11px] font-bold text-primary">{ev.symbol}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{ev.attention_at} UTC</span>
        <span className="font-mono text-[11px] text-muted-foreground">
          {ev.platform_count} 平台 / {ev.item_count} 条
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">
          {ev.platform_list.join(' · ')}
        </span>
        {ev.price?.has_move ? (
          <span className="font-mono text-[11px] text-emerald-500">
            后 {num(ev.price.lead_minutes, ' 分钟')} 价格动 {ev.price.move_pct}%
          </span>
        ) : (
          <span className="font-mono text-[11px] text-muted-foreground">
            后 {ev.price?.horizon_minutes ?? '—'} 分钟未达阈值
          </span>
        )}
        {prior && (
          <span className="font-mono text-[11px] text-amber-500">
            前 {num(ev.reverse.reverse_lead_minutes, ' 分钟')} 价格已动
          </span>
        )}
      </div>
      <div className="mt-0.5 font-mono text-[11px] leading-snug text-muted-foreground">
        {ev.explain}
      </div>
      {ev.items?.length > 0 && (
        <div className="mt-1 flex flex-col gap-0.5">
          {ev.items.slice(0, 2).map((it, i) => (
            <a
              key={`${ev.symbol}-${i}`}
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
  )
}

function PendingRow({ p }: { p: CrowdPendingCandidate }) {
  return (
    <div className="rounded border border-border/60 px-2 py-1.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-[11px] font-bold text-amber-500">{p.symbol}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{p.attention_at} UTC</span>
        <span className="font-mono text-[11px] text-muted-foreground">
          {p.platform_count} 平台 / {p.item_count} 条
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">
          最大波动 {p.max_abs_move_pct}% &lt; 阈值 {p.move_pct_threshold}%
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">
          系统最近决策 {p.system_signal?.suggested_action || '无'}
        </span>
      </div>
      <div className="mt-0.5 font-mono text-[11px] leading-snug text-muted-foreground">{p.explain}</div>
    </div>
  )
}


export function CrowdAttentionLead({
  data,
  pending,
  pendingNote,
}: {
  data: CrowdAttentionResp | null
  pending?: CrowdPendingCandidate[]
  pendingNote?: string
}) {
  if (!data) {
    return <div className="font-mono text-[11px] text-muted-foreground">暂无数据</div>
  }
  const s = data.summary
  const verdict = VERDICT_STYLE[s.verdict] || VERDICT_STYLE.inconclusive
  const p = data.params

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded border border-border bg-muted/20 px-2 py-1.5">
        <div className="font-mono text-[11px] text-foreground">
          判定：<span className={verdict.cls}>{verdict.text}</span>
        </div>
        <div className="mt-0.5 font-mono text-[11px] leading-snug text-muted-foreground">
          {data.note}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-1.5 md:grid-cols-3 lg:grid-cols-6">
        <Metric
          label="注意力事件"
          value={String(s.events_total)}
          hint={`其中 ${s.events_with_price} 个有行情可对照`}
        />
        <Metric
          label="表面命中率"
          value={pct(s.hit_rate)}
          hint={`${s.events_with_move} 个在 ${p.horizon_minutes} 分钟内达标`}
        />
        <Metric
          label="干净子集命中率"
          value={pct(s.clean_hit_rate)}
          hint={`剔除价格已先动的 ${s.events_with_prior_move} 个样本后`}
          tone={
            s.clean_hit_rate !== null && s.clean_hit_rate < 0.3
              ? 'text-amber-500'
              : 'text-foreground'
          }
        />
        <Metric
          label="注意力前价格已动"
          value={pct(s.prior_move_ratio)}
          hint={`反向中位 ${num(s.reverse_median_lead_minutes, ' 分钟')}`}
          tone={
            s.prior_move_ratio !== null && s.prior_move_ratio >= 0.5
              ? 'text-amber-500'
              : 'text-foreground'
          }
        />
        <Metric
          label="中位领先"
          value={num(s.median_lead_minutes, ' 分钟')}
          hint={`干净子集 ${num(s.clean_avg_lead_minutes, ' 分钟')}`}
        />
        <Metric
          label="覆盖标的"
          value={s.symbols.length ? s.symbols.join(' ') : '—'}
          hint={`阈值 ±${p.move_pct}% / ${p.timeframe}`}
        />
      </div>

      <div className="rounded border border-border p-2">
        <div className="mb-1.5 font-mono text-[11px] font-bold text-foreground">
          逐事件明细
          <span className="ml-1 font-normal text-muted-foreground">
            （含反向对照：注意力之前价格是否已动）
          </span>
        </div>
        <div className="flex max-h-[460px] flex-col gap-1 overflow-y-auto">
          {data.events.map((ev, i) => (
            <EventRow key={`${ev.symbol}-${ev.attention_ts}-${i}`} ev={ev} />
          ))}
          {!data.events.length && (
            <div className="font-mono text-[11px] text-muted-foreground">
              没有符合条件的事件（不会用占位数据填充）
            </div>
          )}
        </div>
      </div>

      <div className="rounded border border-border p-2">
        <div className="mb-1.5 font-mono text-[11px] font-bold text-foreground">
          先行漏检候选
          <span className="ml-1 font-normal text-muted-foreground">
            （注意力已起、价格未动、系统未出手）
          </span>
        </div>
        {pendingNote && (
          <div className="mb-1 font-mono text-[11px] text-muted-foreground">{pendingNote}</div>
        )}
        <div className="flex flex-col gap-1">
          {(pending || []).map((p2, i) => (
            <PendingRow key={`${p2.symbol}-${p2.attention_ts}-${i}`} p={p2} />
          ))}
          {!(pending || []).length && (
            <div className="font-mono text-[11px] text-muted-foreground">当前没有先行候选</div>
          )}
        </div>
      </div>
    </div>
  )
}

export default CrowdAttentionLead
