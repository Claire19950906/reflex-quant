'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'

// v383 一键处置：静默品种 / 忽略该笔 / 反手（记影子）
// 三个动作都落到 decision_actions 表，静默会被写入口真实拦截（不是前端假过滤）

type Props = {
  decisionId: number
  asset?: string
  eventType?: string
  onDone?: (msg: string) => void
}

type Result = { ok: boolean; effect?: string; error?: string; revoked?: number }

type Silence = { id: number; asset: string; remaining_min: number }

const ACTIONS: { key: string; label: string; minutes?: number; note: string }[] = [
  { key: 'silence_asset', label: '静默 30 分钟', minutes: 30, note: '页面内快速静默' },
  { key: 'silence_asset', label: '静默 4 小时', minutes: 240, note: '页面内快速静默' },
  { key: 'ignore_decision', label: '忽略该笔', note: '页面内快速忽略' },
  { key: 'reverse', label: '反手（记影子）', note: '页面内快速反手' },
]

export function DecisionQuickActions({ decisionId, asset, eventType, onDone }: Props) {
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [silences, setSilences] = useState<Silence[]>([])

  // v388：列出当前生效的静默，支持一键撤销（之前只能等过期）
  const loadSilences = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/v1/decisions/muted`)
      const j = await r.json()
      setSilences((j?.silences || []) as Silence[])
    } catch { /* 读不到就只是不显示 */ }
  }, [])

  useEffect(() => { void loadSilences() }, [loadSilences])

  const unsilence = useCallback(async (silenceId: number) => {
    setBusy(`unsilence:${silenceId}`)
    setMsg('')
    try {
      const r = await fetch(`${API_BASE}/v1/decisions/unsilence`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: silenceId }),
      })
      const j: Result = await r.json()
      const text = j.ok ? `已撤销静默（${j.revoked ?? 0} 条）` : (j.error || '撤销失败')
      setMsg(text)
      onDone?.(text)
      await loadSilences()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '撤销失败')
    } finally {
      setBusy('')
    }
  }, [loadSilences, onDone])

  const run = useCallback(async (
    key: string, minutes?: number, scope: 'asset' | 'event' = 'asset', et = '',
  ) => {
    setBusy(`${key}:${minutes ?? 0}`)
    setMsg('')
    try {
      const res = await fetch(`${API_BASE}/v1/decisions/${decisionId}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: key,
          duration_min: minutes ?? 0,
          note: 'ui',
          scope,
          event_type: et,
        }),
      })
      const json: Result = await res.json()
      const text = json.ok ? (json.effect || '已记录') : (json.error || '操作失败')
      setMsg(text)
      onDone?.(text)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '操作失败')
    } finally {
      setBusy('')
    }
  }, [decisionId, onDone])

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {asset && <span className="font-mono text-[11px] text-muted-foreground">{asset}</span>}
      {ACTIONS.map((a) => {
        const id = `${a.key}:${a.minutes ?? 0}`
        return (
          <button
            key={a.label}
            type="button"
            disabled={!!busy}
            onClick={() => void run(a.key, a.minutes)}
            className="rounded border border-border bg-secondary px-2 py-1 font-mono text-[11px] text-muted-foreground transition hover:border-primary hover:text-foreground disabled:opacity-50"
          >
            {busy === id ? '…' : a.label}
          </button>
        )
      })}
      {eventType && (
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void run('silence_asset', 240, 'event', eventType)}
          className="rounded border border-border bg-secondary px-2 py-1 font-mono text-[11px] text-muted-foreground transition hover:border-primary hover:text-foreground disabled:opacity-50"
          title="只静默该事件类型，同品种其它事件照常分析"
        >
          {busy === 'silence_asset:240' && '…'}
          {busy !== 'silence_asset:240' && `只静默 ${eventType} 事件`}
        </button>
      )}
      {silences.map((s) => (
        <button
          key={s.id}
          type="button"
          disabled={!!busy}
          onClick={() => void unsilence(s.id)}
          className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 font-mono text-[11px] text-amber-500 transition hover:border-amber-500 disabled:opacity-50"
          title="撤销该条静默（置过期，不删记录）"
        >
          {busy === `unsilence:${s.id}` ? '…' : `撤销静默 ${s.asset}${s.remaining_min ? ` (${s.remaining_min}分)` : ' (永久)'}`}
        </button>
      ))}
      {msg && <span className="font-mono text-[11px] text-emerald-500">{msg}</span>}
    </div>
  )
}
