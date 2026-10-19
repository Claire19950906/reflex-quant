'use client'

import { useState, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { Star, Loader2, Check, X } from 'lucide-react'
import { API_BASE } from '@/lib/api'
import { cn } from '@/lib/utils'

/**
 * ⭐ 入因果链按钮(v283+ · #23/#31 · 解决发现页「只看不入」的痛点)
 *
 * 4 个发现型 panel(asset-scout / event-radar / cross-asset-meta / cycle-radar)
 * 各点击可挂一颗 ⭐,把当前 panel 命中的「事件/资产/相位」一键投到
 * /api/causal/chains,作为 /knowledge-graph 与 /causal-chains 的输入。
 *
 * 失败语义:不抛错,UI 反馈 ✅ / ❌,1.6s 后恢复原状。
 *   · ❌ 多半因为这条 cause+effect 已存在(后端 add_chain 抛 ValueError)→ 提示重复
 *   · ❌ 网络/HTTP 错误 → 显示后端 message
 *
 * 设计取舍:
 *   · 不做 modal,直接传 props(caller 已经从 panel 拿好了 cause/effect/note)
 *   · 走 /api 前缀 → 通过 nginx 转发,不直连 8000 端口
 *   · 用 AbortController 避免快连点导致旧请求回来覆盖新状态
 */

export interface PushToCausalChainButtonProps {
  cause: string
  effect: string
  note?: string
  trigger_words?: string
  weight?: number
  /** v283+ · 来源标记:manual / manual_from_panel / auto_real / auto_quick_sim / auto_mixed
   * 用于后端做来源筛选 + 批量操作时区分人工/自动。
   * 默认 "manual"(用户在 chip 里点的,非 quick-sim 自动)。 */
  origin?: string
  /** 按钮内显示的简短原因(可选,默认 '⭐ 入候选') */
  label?: string
  /** 额外 className */
  className?: string
}

type Status = 'idle' | 'pending' | 'success' | 'duplicate' | 'error'

const ERROR_MSG_TTL_MS = 2200

export function PushToCausalChainButton({
  cause,
  effect,
  note = '',
  trigger_words = '',
  weight = 0.6,
  origin = 'manual',
  label = '⭐ 入候选',
  className,
}: PushToCausalChainButtonProps) {
  const [status, setStatus] = useState<Status>('idle')
  const [errMsg, setErrMsg] = useState<string>('')
  const ctrlRef = useRef<AbortController | null>(null)
  const router = useRouter()

  const submit = async () => {
    if (status === 'pending') return
    // 取消上一轮(快连点)
    if (ctrlRef.current) {
      try { ctrlRef.current.abort() } catch { /* noop */ }
    }
    const ctrl = new AbortController()
    ctrlRef.current = ctrl

    setStatus('pending')
    setErrMsg('')
    try {
      const r = await fetch(`${API_BASE}/causal/chains`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        cache: 'no-store',
        signal: ctrl.signal,
        body: JSON.stringify({
          cause: cause.trim(),
          effect: effect.trim(),
          note,
          trigger_words,
          weight,
          origin,
        }),
      })
      if (!r.ok) {
        // 后端 ValueError 走 400;其它 HTTP 错误
        const text = await r.text().catch(() => '')
        if (r.status === 400 && /exist|duplicate|已存在/i.test(text)) {
          setStatus('duplicate')
        } else {
          setStatus('error')
          setErrMsg(text.slice(0, 80) || `HTTP ${r.status}`)
        }
        return
      }
      setStatus('success')
      // v302+: ⭐ 入链成功后 800ms 后跳到 /review 段 7 (analysis-improve),
      // 把这条 cause+effect 作为 from-discover 预过滤项。
      window.setTimeout(() => {
        try {
          const params = new URLSearchParams({
            prefilter: 'from-discover',
            cause: cause.trim().slice(0, 60),
            effect: effect.trim().slice(0, 60),
          })
          router.push(`/review#improve?${params.toString()}`)
        } catch {
          /* noop - 不阻塞 UX */
        }
      }, 800)
    } catch (cause2) {
      if (cause2 instanceof DOMException && cause2.name === 'AbortError') return
      setStatus('error')
      setErrMsg(cause2 instanceof Error ? cause2.message : '提交失败')
    } finally {
      if (ctrlRef.current === ctrl) ctrlRef.current = null
    }
  }

  // 副作用:error / duplicate 时 2.2s 后回到 idle,避免红 X 卡住
  useEffect(() => {
    if (status !== 'error' && status !== 'duplicate') return
    const t = setTimeout(() => setStatus('idle'), ERROR_MSG_TTL_MS)
    return () => clearTimeout(t)
  }, [status])

  const disabled = !cause.trim() || !effect.trim() || status === 'pending'

  return (
    <button
      type="button"
      onClick={() => void submit()}
      disabled={disabled}
      title={errMsg || `${cause} → ${effect}`}
      className={cn(
        'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors',
        // 默认态:琥珀色描边,像「候选项」标记
        status === 'idle' && 'border-amber-500/40 bg-amber-500/5 text-amber-700 hover:bg-amber-500/15 dark:text-amber-300',
        // pending:灰色
        status === 'pending' && 'border-border bg-secondary/50 text-muted-foreground',
        // success:绿
        status === 'success' && 'border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
        // duplicate:黄
        status === 'duplicate' && 'border-yellow-500/40 bg-yellow-500/15 text-yellow-700 dark:text-yellow-300',
        // error:红
        status === 'error' && 'border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300',
        // disabled
        disabled && status === 'idle' && 'cursor-not-allowed opacity-50',
        className,
      )}
    >
      {status === 'idle' && (
        <>
          <Star className="h-3 w-3" aria-hidden />
          <span>{label}</span>
        </>
      )}
      {status === 'pending' && (
        <>
          <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
          <span>提交中…</span>
        </>
      )}
      {status === 'success' && (
        <>
          <Check className="h-3 w-3" aria-hidden />
          <span>已入链</span>
        </>
      )}
      {status === 'duplicate' && (
        <>
          <Star className="h-3 w-3 fill-current" aria-hidden />
          <span>已在链</span>
        </>
      )}
      {status === 'error' && (
        <>
          <X className="h-3 w-3" aria-hidden />
          <span>失败{errMsg ? `: ${errMsg}` : ''}</span>
        </>
      )}
    </button>
  )
}