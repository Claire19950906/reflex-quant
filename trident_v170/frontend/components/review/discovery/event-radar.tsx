'use client'

import { Zap, AlertCircle, Clock } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { PushToCausalChainButton } from './push-to-causal-chain-button'
import { cn } from '@/lib/utils'

/**
 * ⚡ 事件驱动快速因果雷达
 *
 * 用户原话:「识别其他强信息驱动...被快速的事件触发...怎么快速的几分钟判断这些」
 *
 * 设计思路:
 *   · 拉最近 1-4 小时的突发新闻事件(event_atom)
 *   · 联动因果图谱 causal_graph / causal_chains → 找出"可能受影响的品种"
 *   · 时间窗可视化:从事件触发到受影响品种反应的窗口
 *
 * 后端路由 /api/discovery/event-radar(可选);不实现时本面板显示
 * "建议接入 event_atom + causal_graph 中的 event_match"
 */

type EventRadarItem = {
  event_id: string
  title: string
  ts: number
  age_minutes: number
  urgency: 'low' | 'medium' | 'high'
  likely_affected: string[]   // 资产列表
  causal_chain_id?: number
  chain_score?: number        // 0-1 因果链匹配度
  confidence: number
  hint: string
}

const URGENCY_CLS = {
  high:   'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  medium: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  low:    'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300',
}

const URGENCY_LABEL = { high: '🔴 紧急', medium: '🟠 中等', low: '🟢 一般' }

export function EventRadarPanel() {
  // v283+ · 改用统一 usePoll 轮询(60s);transform 直接保留 raw,
  // 因为后端 ok=false 是「无事件」语义(不是错误),caller 自判断不报错。
  const eventPoll = usePoll<{ ok: boolean; items?: EventRadarItem[]; error?: string }>(
    '/discovery/event-radar',
    { intervalMs: 60_000, transform: (raw) => raw as { ok: boolean; items?: EventRadarItem[]; error?: string } },
  )
  const raw = eventPoll.data
  const items: EventRadarItem[] =
    raw && raw.ok && Array.isArray(raw.items) ? raw.items : []
  // 只有 fetch 真失败时(err 被 hook 写入)才显示「接口待接入」;
  // ok=false 表示后端正常返回「无数据」,不算 error。
  const err = eventPoll.error
  const loading = eventPoll.loading

  return (
    <div className="rounded border border-sky-500/40 bg-sky-500/5 p-3">
      <div className="flex items-center gap-2 font-mono text-xs">
        <Zap className="h-4 w-4 text-sky-500" aria-hidden />
        <span className="font-bold uppercase tracking-widest text-sky-700 dark:text-sky-300">
          ⚡ 事件驱动快速因果
        </span>
        <span className="rounded border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-[9px] font-bold text-sky-600 dark:text-sky-400">
          重大新闻 → 受影响品种(分钟级)
        </span>
      </div>

      <p className="mt-2 text-[11px] leading-7 text-muted-foreground">
        事件 → 因果链 lookup → 倒推受影响品种。
        与 <code className="rounded bg-background px-1">causal_graph.event_match</code> 联动,
        当事件评分 confidence ≥ 0.6 时建议「投到 causal_chains 候选」。
        <strong className="text-foreground">目标延迟 ≤ 5 分钟</strong>(新闻触发 → 雷达命中)。
      </p>

      {err && (
        <div className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs dark:border-amber-700 dark:bg-amber-950">
          <div className="font-semibold text-amber-700 dark:text-amber-300">⏳ 事件雷达接口待接入</div>
          <div className="mt-1 text-amber-600 dark:text-amber-400">{err}</div>
          <div className="mt-1 text-[10px] text-muted-foreground">
            接入方案:复用 <code className="rounded bg-background px-1">event_atom</code> 表,
            跑一个 lookup(基于 causal_graph 的 trigger_words),输出最可能受影响的资产。
            不接入时本面板只显示框架,无具体事件。
          </div>
        </div>
      )}

      <div className="mt-2 space-y-1.5">
        {!items.length && !err && (
          <div className="text-xs text-muted-foreground">{loading ? '扫描中…' : '暂无新事件'}</div>
        )}
        {items.slice(0, 8).map((it) => (
          <div key={it.event_id} className={cn('rounded border p-2', URGENCY_CLS[it.urgency])}>
            <div className="flex items-center gap-1.5">
              <span className="rounded border px-1 py-0.5 font-mono text-[9px]">{URGENCY_LABEL[it.urgency]}</span>
              <span className="font-mono text-[10px] text-muted-foreground/70">
                <Clock className="mr-0.5 inline h-3 w-3" />{it.age_minutes}m ago
              </span>
              <span className="ml-auto font-mono text-[10px]">conf {it.confidence.toFixed(2)}</span>
            </div>
            <div className="mt-1 text-[11px] font-semibold text-foreground">{it.title}</div>
            {it.likely_affected.length > 0 && (
              <div className="mt-1 flex flex-wrap items-center gap-1">
                <span className="text-[10px] text-muted-foreground">受影响:</span>
                {it.likely_affected.map((a) => (
                  <span key={a} className="rounded border border-current/30 bg-background/50 px-1 py-0.5 font-mono text-[10px]">{a}</span>
                ))}
              </div>
            )}
            {it.hint && <div className="mt-1 text-[10px] text-foreground/80">💡 {it.hint}</div>}
            {/* v283+ · #23/#31 · 事件→受影响资产,投到因果链 */}
            {it.likely_affected[0] && (
              <div className="mt-1.5">
                <PushToCausalChainButton origin="manual_from_event_radar"

                  cause={it.title}
                  effect={it.likely_affected[0]}
                  note={`event-radar urgency=${it.urgency} conf=${it.confidence.toFixed(2)} affected=${it.likely_affected.join(',')} age=${it.age_minutes}m`}
                  trigger_words={it.likely_affected.join(',')}
                  weight={Math.min(1, it.confidence)}
                />
              </div>
            )}
            {it.causal_chain_id && (
              <div className="mt-1 text-[9px] text-muted-foreground/70">
                因果链 #{it.causal_chain_id} · 匹配 {(it.chain_score ?? 0).toFixed(2)}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}