'use client'

/**
 * components/quant/backend-capability-panel.tsx
 *
 * 通用「后端能力面板」—— 2026-11-02 后端能力归位工作的通用载体。
 *
 * 背景: 审计发现 157 个后端端点前端零引用(后端在写、在 cron 里跑, 界面却没有
 * 入口)。本面板把「某个后端能力域」挂到现有页面上, 不新增任何页面。
 *
 * 三条硬约束(都来自实测):
 *   1. **懒加载** —— 展开才取数。/api/signal-weights/stats 在 WAL 写锁竞争下
 *      首调实测 39.3s, 若随宿主页面首屏加载会把整页拖死。
 *   2. **长超时** —— 重端点走 SLOW_ENDPOINT_TIMEOUT_MS(75s), 不受 apiGet 默认 30s 限制。
 *   3. **区分「未就绪」与「无数据」** —— 后端部分端点用 200 + {ok:false,error}
 *      表达失败, apiGet 不会识别, 由 lib/api-backend.ts 的 fetchBackend 拦截。
 *
 * 用法:
 *   <BackendCapabilityPanel domain="signal-weights" />
 */

import { useCallback, useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, RefreshCw, AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { getDomain } from '@/lib/backend-registry'
import { BACKEND_LOADERS, type BackendPayload } from '@/lib/api-backend'

export type BackendCapabilityPanelProps = {
  /** 域 key, 对应 lib/backend-registry.ts 的 BackendDomain.key */
  domain: string
  /** 初始是否展开(默认折叠 —— 懒加载) */
  defaultOpen?: boolean
  /** 覆盖标题 */
  title?: string
  /** 附加类名 */
  className?: string
}

export function BackendCapabilityPanel({
  domain,
  defaultOpen = false,
  title,
  className,
}: BackendCapabilityPanelProps) {
  const meta = getDomain(domain)
  const loader = BACKEND_LOADERS[domain]
  const [open, setOpen] = useState(defaultOpen)
  const [data, setData] = useState<BackendPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!loader) {
      setError(`域 ${domain} 尚未接入 loader（见 lib/api-backend.ts 的 BACKEND_LOADERS）`)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const payload = await loader()
      setData(payload)
    } catch (e) {
      setError((e as Error).message)
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [domain, loader])

  // 展开时才首次取数 —— 避免拖慢宿主页面首屏
  useEffect(() => {
    if (open && !data && !loading && !error) void load()
  }, [open, data, loading, error, load])

  const label = title ?? meta?.label ?? domain
  const icon = meta?.icon ?? '•'

  return (
    <section
      className={cn('rounded border border-border bg-card/60', className)}
      aria-label={`后端能力 ${label}`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left font-mono text-[11px] uppercase tracking-wider text-foreground hover:bg-secondary/30"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        )}
        <span aria-hidden>{icon}</span>
        <span className="font-semibold">{label}</span>
        {meta?.backing && (
          <span className="hidden truncate font-mono text-[11px] normal-case text-muted-foreground sm:inline">
            {meta.backing}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1.5">
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" aria-hidden />
          ) : null}
          {data && !loading ? (
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation()
                void load()
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.stopPropagation()
                  void load()
                }
              }}
              className="rounded border border-border bg-secondary/30 px-1.5 py-0.5 text-[11px] normal-case text-muted-foreground hover:bg-secondary/50"
            >
              <RefreshCw className="h-3 w-3" aria-hidden />
            </span>
          ) : null}
        </span>
      </button>

      {open ? (
        <div className="space-y-3 border-t border-border px-3 py-2.5">
          {meta?.summary ? (
            <p className="text-[11px] leading-relaxed text-muted-foreground">{meta.summary}</p>
          ) : null}

          {error ? (
            <div className="flex items-start gap-2 rounded border border-amber-500/40 bg-amber-500/5 px-2 py-1.5 font-mono text-[11px] text-amber-600">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>后端未就绪：{error}</span>
            </div>
          ) : null}

          {loading && !data ? (
            <div className="font-mono text-[11px] text-muted-foreground">
              载入中…（首次可能需要 40s，后端在算全表统计）
            </div>
          ) : null}


          {data && data.stats.length > 0 ? (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
              {data.stats.map((s, i) => (
                <div
                  key={`${s.label}-${i}`}
                  className="rounded border border-border bg-secondary/20 px-2 py-1.5"
                >
                  <div className="truncate font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                    {s.label}
                  </div>
                  <div
                    className={cn(
                      'mt-0.5 font-mono text-sm font-bold',
                      s.tone === 'bad'
                        ? 'text-red-500'
                        : s.tone === 'warn'
                          ? 'text-amber-500'
                          : 'text-foreground',
                    )}
                  >
                    {s.value}
                  </div>
                  {s.hint && (
                    <div className="truncate font-mono text-[11px] text-muted-foreground">{s.hint}</div>
                  )}
                </div>
              ))}
            </div>
          ) : null}

          {data && data.rows.length > 0 ? (
            <ul className="space-y-1.5">
              {data.rows.map((r, i) => (
                <li key={`${r.title}-${i}`} className="rounded border border-border bg-secondary/20 px-2 py-1.5">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-[11px] font-semibold text-foreground">{r.title}</span>
                    {r.subtitle && (
                      <span className="font-mono text-[11px] text-muted-foreground">{r.subtitle}</span>
                    )}
                  </div>
                  {r.body && (
                    <p className="mt-0.5 line-clamp-3 text-[11px] leading-relaxed text-muted-foreground">
                      {r.body}
                    </p>
                  )}
                  {r.meta && r.meta.length > 0 ? (
                    <div className="mt-0.5 flex flex-wrap gap-2 font-mono text-[11px] text-muted-foreground">
                      {r.meta.map((m, j) => (
                        <span key={j}>{m}</span>
                      ))}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}

          {data && data.stats.length === 0 && data.rows.length === 0 ? (
            <div className="font-mono text-[11px] text-muted-foreground">该能力当前无数据。</div>
          ) : null}

          {data?.note ? (
            <p className="border-t border-border pt-1.5 font-mono text-[11px] text-muted-foreground">
              {data.note}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
