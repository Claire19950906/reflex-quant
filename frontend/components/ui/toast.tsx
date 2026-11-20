'use client'

/**
 * Toast — 轻量 toast 系统（v170 P0+）
 *
 * 替代 window.alert/confirm。0 外部依赖。
 *
 *   <ToastProvider>{children}</ToastProvider>  // 顶层包一次
 *   const { toast } = useToast()                // 任意组件里
 *   toast.success('已删除 #123')                // 3.5s 自动消失
 *   toast.confirm('确定？', onYes, onNo)        // 不消失
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from 'react'
import { CheckCircle2, XCircle, Info, AlertCircle, X } from 'lucide-react'
import { cn } from '@/lib/utils'

export type ToastKind = 'success' | 'error' | 'info' | 'warning'

export type Toast = {
  id: string
  kind: ToastKind
  title: string
  /** 自动消失 ms；0 表示不消失（confirm 模式） */
  ttlMs: number
  onConfirm?: () => void
  onCancel?: () => void
}

type ToastContextValue = {
  toasts: Toast[]
  toast: {
    success: (title: string) => string
    error: (title: string) => string
    info: (title: string) => string
    warning: (title: string) => string
    confirm: (title: string, onConfirm: () => void, onCancel?: () => void) => string
  }
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

const ICONS: Record<ToastKind, typeof CheckCircle2> = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
  warning: AlertCircle,
}

const COLORS: Record<ToastKind, string> = {
  success: 'border-buy/40 bg-buy/10 text-buy',
  error: 'border-short/40 bg-short/10 text-short',
  info: 'border-primary/40 bg-primary/10 text-primary',
  warning: 'border-hold/40 bg-hold/10 text-hold',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const timers = useRef<Map<string, number>>(new Map())

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
    const timer = timers.current.get(id)
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timers.current.delete(id)
    }
  }, [])

  const push = useCallback((t: Omit<Toast, 'id'>): string => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    setToasts((prev) => [...prev, { ...t, id }])
    if (t.ttlMs > 0) {
      const timer = window.setTimeout(() => dismiss(id), t.ttlMs)
      timers.current.set(id, timer)
    }
    return id
  }, [dismiss])

  const toast: ToastContextValue['toast'] = useMemo(() => ({
    success: (title: string) => push({ kind: 'success', title, ttlMs: 3500 }),
    error: (title: string) => push({ kind: 'error', title, ttlMs: 6000 }),
    info: (title: string) => push({ kind: 'info', title, ttlMs: 3500 }),
    warning: (title: string) => push({ kind: 'warning', title, ttlMs: 5000 }),
    confirm: (title: string, onConfirm: () => void, onCancel?: () => void) => push({
      kind: 'warning', title, ttlMs: 0, onConfirm, onCancel,
    }),
  }), [push])

  useEffect(() => {
    const t = timers.current
    return () => {
      t.forEach((id) => window.clearTimeout(id))
      t.clear()
    }
  }, [])

  // … ToastHost JSX 在下面 edit 追加 …
  return (
    <ToastContext.Provider value={{ toasts, toast, dismiss }}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="true"
        className="pointer-events-none fixed bottom-3 right-3 z-50 flex max-w-sm flex-col gap-2"
      >
        {toasts.map((t) => {
          const Icon = ICONS[t.kind]
          return (
            <div
              key={t.id}
              role="alert"
              className={cn(
                'pointer-events-auto flex items-start gap-2 rounded-md border bg-background/95 px-3 py-2 font-mono text-xs shadow-lg backdrop-blur-sm',
                COLORS[t.kind],
              )}
            >
              <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <div className="min-w-0 flex-1">
                <div className="font-semibold">{t.title}</div>
                {t.onConfirm ? (
                  <div className="mt-1.5 flex gap-1.5">
                    <button type="button" onClick={() => { t.onConfirm?.(); dismiss(t.id) }} className="rounded border border-foreground/30 bg-background px-2 py-0.5 text-foreground hover:bg-secondary">确认</button>
                    <button type="button" onClick={() => { t.onCancel?.(); dismiss(t.id) }} className="rounded border border-border bg-background px-2 py-0.5 text-muted-foreground hover:bg-secondary">取消</button>
                  </div>
                ) : null}
              </div>
              <button type="button" onClick={() => dismiss(t.id)} className="shrink-0 opacity-60 hover:opacity-100" aria-label="关闭">
                <X className="h-3 w-3" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within <ToastProvider>')
  return ctx
}
