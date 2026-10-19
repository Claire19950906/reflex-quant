'use client'

/**
 * CopyMarkdownButton — 决策详情 / 列表行的"复制 Markdown"按钮(v302+ P1)。
 *
 * 流程: 点击 → formatReviewAsMarkdown(review, text) → navigator.clipboard.writeText
 *       → 短暂显示"已复制"反馈(2 秒后回到默认)
 *
 * 不依赖第三方 UI: 只用 lucide-react 的 Copy / Check icon。
 */

import { useState, useCallback } from 'react'
import { Copy, Check, AlertCircle } from 'lucide-react'
import type { Review, DecisionTextResp } from '@/lib/types-reasoning-review'
import { formatReviewAsMarkdown } from '@/lib/review-export'
import { cn } from '@/lib/utils'

type Props = {
  review: Review
  text?: DecisionTextResp | null
  /** 紧凑模式(只显示 icon,无文字) */
  compact?: boolean
  className?: string
  /** 点击时是否 stopPropagation(嵌套在 button 内时为 true) */
  stopPropagation?: boolean
  /** 点击时是否 preventDefault(防止父级 submit / 自定义处理) */
  preventDefault?: boolean
}

type Status = 'idle' | 'ok' | 'err'

export function CopyMarkdownButton({
  review,
  text,
  compact = false,
  className,
  stopPropagation = false,
  preventDefault = false,
}: Props) {
  const [status, setStatus] = useState<Status>('idle')

  const handleClick = useCallback(
    async (e: React.MouseEvent<HTMLButtonElement>) => {
      if (stopPropagation) e.stopPropagation()
      if (preventDefault) e.preventDefault()
      const md = formatReviewAsMarkdown(review, text)
      if (typeof navigator === 'undefined' || !navigator.clipboard) {
        setStatus('err')
      } else {
        try {
          await navigator.clipboard.writeText(md)
          setStatus('ok')
        } catch {
          setStatus('err')
        }
      }
      setTimeout(() => setStatus('idle'), 2000)
    },
    [review, text, stopPropagation, preventDefault],
  )

  const icon =
    status === 'ok' ? <Check className="h-3 w-3" />
    : status === 'err' ? <AlertCircle className="h-3 w-3" />
    : <Copy className="h-3 w-3" />

  const label =
    status === 'ok' ? '已复制'
    : status === 'err' ? '复制失败'
    : '复制 Markdown'

  return (
    <button
      type="button"
      onClick={handleClick}
      title={label}
      data-testid="copy-md-btn"
      data-status={status}
      className={cn(
        'inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-0.5',
        'text-[10px] text-muted-foreground transition-colors hover:bg-foreground/5',
        status === 'ok' && 'border-foreground/30 text-foreground',
        status === 'err' && 'border-foreground/30 text-foreground',
        className,
      )}
    >
      {icon}
      {!compact && <span>{label}</span>}
    </button>
  )
}
