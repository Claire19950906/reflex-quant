'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE, fetchWithFallback } from '@/lib/api'

// 信息源异常 toast（全局常驻，不影响 ai_worker / 任何模型推理）
//
// 行为约定：
//   - 每 30s 拉 /api/sources/alerts —— 后端只列"持续异常"（连续失败 ≥2 或 stale ≥15min）
//   - 每次新出现的异常 source 弹一个固定 toast，**必须用户手动点"我知道了"才消失**
//   - 已点掉的 alert 写入 localStorage（24h 缓存），避免刷新后旧异常反复弹
//   - 同一条 alert 用 `${source_id}#${last_fail_ts}` 做唯一 key（fail_ts 变化 = 新一轮失败 = 再弹一次）
//   - 多个 toast 垂直堆叠在右上角，**纯前端组件，对后端 / ai_worker / 模型推理 0 影响**
//   - 后端 API 失败也不会弹（避免自身卡顿引发级联）

type AlertItem = {
  source_id: string
  category: string
  label: string
  severity: 'error' | 'stale'
  consecutive_failures: number
  last_fail_ts: number | null
  secs_since_fail: number | null
  last_error: string
}

/** v345.5: 信息源 last_error 是抓取层的原始异常（含 URL、SSL 细节、栈片段），
 *  直接展示给用户既看不懂也暴露内部地址。这里按常见成因归一成人话。 */
function friendlySourceError(raw: string): string {
  const t = (raw || '').replace(/https?:\/\/[^\s)'"]+/g, '该地址').replace(/\s+/g, ' ').trim()
  if (!t) return '抓取失败（原因未记录）'
  if (/SSL|certificate|CERTIFICATE_VERIFY_FAILED/i.test(t)) return '与该来源的 HTTPS 证书握手失败（对方证书或本地时间异常），稍后重试即可。'
  if (/reset by peer|Connection reset|RST|RemoteDisconnected/i.test(t)) return '对方主动断开连接（常见于境外站点限制访问），稍后重试。'
  if (/timed out|timeout|超时/i.test(t)) return '请求超时：该来源响应过慢或不稳定。'
  if (/\b404\b/.test(t)) return '该来源接口返回 404（地址可能已变更）。'
  if (/\b429\b/.test(t)) return '被该来源限流（429），需要降低抓取频率。'
  if (/\b5\d\d\b/.test(t)) return `该来源服务端异常（HTTP ${t.match(/\b5\d\d\b/)?.[0]}），属于对方故障。`
  if (/Name or service not known|nodename|DNS|getaddrinfo/i.test(t)) return '域名解析失败（DNS），检查网络出口。'
  if (/ProxyError|proxy/i.test(t)) return '代理连接失败，检查代理配置。'
  return t.slice(0, 90)
}

const SEEN_KEY = 'trident_source_alerts_seen_v1'
const SEEN_TTL_S = 86400

function loadSeen(): Record<string, number> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(SEEN_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    const cutoff = Date.now() / 1000 - SEEN_TTL_S
    const out: Record<string, number> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof v === 'number' && v > cutoff) out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

function saveSeen(seen: Record<string, number>) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(SEEN_KEY, JSON.stringify(seen))
  } catch {
    /* quota exceeded 等忽略 */
  }
}

function alertKey(it: AlertItem): string {
  return `${it.source_id}#${Math.round(it.last_fail_ts ?? 0)}`
}

function fmtClock(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtAgo(secs: number | null): string {
  if (secs == null) return ''
  if (secs < 60) return `${Math.round(secs)}秒前`
  if (secs < 3600) return `${Math.round(secs / 60)}分钟前`
  return `${(secs / 3600).toFixed(1)}小时前`
}

const CATEGORY_COLOR: Record<string, string> = {
  行情: 'border-l-blue-500',
  宏观: 'border-l-violet-500',
  新闻: 'border-l-emerald-500',
  引擎: 'border-l-rose-500',
}

const SEVERITY_STYLE: Record<string, { badge: string; ring: string; icon: string }> = {
  error: { badge: 'bg-hold/15 text-hold', ring: 'ring-hold/30', icon: '⚠' },
  stale: { badge: 'bg-amber-500/15 text-amber-600 dark:text-amber-400', ring: 'ring-amber-500/30', icon: '⏱' },
}

export function SourceStatusAlerts() {
  const [alerts, setAlerts] = useState<AlertItem[]>([])
  const [dismissed, setDismissed] = useState<Record<string, number>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  // v343: 默认收起成一颗胶囊，避免几十个异常源直接盖住主图
  const [panelOpen, setPanelOpen] = useState(false)

  useEffect(() => {
    setDismissed(loadSeen())
  }, [])

  const fetchAlerts = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/sources/alerts`, { cache: 'no-store' })
      let d: any
      if (!r.ok) {
        // 主服务 500 时, fallback 到桥接
        d = await fetchWithFallback<{ items?: AlertItem[]; alerts?: AlertItem[] }>('/sources/alerts')
      } else {
        d = await r.json()
      }
      const d2 = d as { items?: AlertItem[]; alerts?: AlertItem[] }
      // 主服务用 items, 桥接用 alerts
      const items = Array.isArray(d2.items) ? d2.items : (Array.isArray(d2.alerts) ? d2.alerts : [])
      setAlerts(items)
    } catch {
      /* silent */
    }
  }, [])

  useEffect(() => {
    void fetchAlerts()
    const t = window.setInterval(fetchAlerts, 30_000)
    return () => window.clearInterval(t)
  }, [fetchAlerts])

  const dismiss = useCallback((key: string) => {
    setDismissed((prev) => {
      const next = { ...prev, [key]: Date.now() / 1000 }
      saveSeen(next)
      return next
    })
  }, [])

  const toggle = useCallback((key: string) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))
  }, [])

  const visibleAlerts = alerts.filter((it) => {
    const k = alertKey(it)
    const ack = dismissed[k]
    if (!ack) return true
    return (it.last_fail_ts ?? 0) > ack
  })

  if (visibleAlerts.length === 0) return null

  // v337-UI: 原来"每条异常一张卡、必须手动关"，桥接抖动时会一次堆十几张把整屏盖住。
  //   现在最多显示 3 张（按失败时间最新优先），其余收成一行"还有 N 个"。
  const MAX_CARDS = 3
  const sorted = [...visibleAlerts].sort((a, b) => (b.last_fail_ts ?? 0) - (a.last_fail_ts ?? 0))
  const shownAlerts = sorted.slice(0, MAX_CARDS)
  const restCount = sorted.length - shownAlerts.length

  // v343: 异常源常年几十个的情况下，"3 张卡 + 还有 N 个" 依然会盖住 K 线主图。
  //   改成默认只显示一行汇总条（右上角一颗小胶囊），点了才展开明细。
  if (!panelOpen) {
    return (
      <div
        className="pointer-events-none fixed right-3 top-16 z-[200] w-auto max-w-[calc(100vw-1.5rem)]"
        data-testid="source-alerts-container"
      >
        <div
          className="pointer-events-auto inline-flex items-center gap-2 rounded-full border border-rose-300 bg-white/95 px-3 py-1.5 shadow-sm dark:border-rose-800 dark:bg-slate-900/95"
          data-testid="source-alert"
        >
          <span className="inline-flex h-4 w-4 items-center justify-center rounded-full bg-rose-500 text-[11px] font-bold text-white">
            !
          </span>
          <span className="font-mono text-[11px] text-slate-700 dark:text-slate-200">
            {sorted.length} 个信息源异常
            {/* v499: 收起态也带上"最近一次"的时间 —— 旧账（20h 前的失败）不该看起来像刚坏 */}
            {sorted[0]?.secs_since_fail != null && (
              <span className="ml-1 text-slate-500 dark:text-slate-400">
                · 最近 {fmtAgo(sorted[0].secs_since_fail)}
              </span>
            )}
          </span>
          <button
            type="button"
            onClick={() => setPanelOpen(true)}
            className="font-mono text-[11px] text-indigo-600 underline-offset-2 hover:underline dark:text-indigo-400"
          >
            查看
          </button>
          <button
            type="button"
            onClick={() => {
              for (const it of sorted) dismiss(alertKey(it))
            }}
            className="rounded bg-emerald-600 px-2 py-0.5 font-mono text-[11px] font-medium text-white hover:bg-emerald-700"
            data-testid="dismiss-alert"
          >
            我知道了
          </button>
        </div>
      </div>
    )
  }

  return (
    <div
      className="pointer-events-none fixed right-3 top-16 z-[200] flex max-h-[55vh] w-[380px] max-w-[calc(100vw-1.5rem)] flex-col gap-2 overflow-y-auto"
      data-testid="source-alerts-container"
    >
      {shownAlerts.map((it) => {
        const k = alertKey(it)
        const sev = SEVERITY_STYLE[it.severity] ?? SEVERITY_STYLE.error
        const catColor = CATEGORY_COLOR[it.category] ?? 'border-l-slate-500'
        const isOpen = !!expanded[k]
        return (
          <div
            key={k}
            className={`pointer-events-auto surface-card border-l-4 ${catColor} ring-1 ${sev.ring}`}
            data-testid="source-alert"
          >
            <button
              type="button"
              onClick={() => toggle(k)}
              className="flex w-full items-start gap-2 px-3 py-2 text-left"
            >
              <span
                className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${sev.badge}`}
              >
                {sev.icon}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="truncate font-mono text-xs font-semibold">
                    {it.label || it.source_id}
                  </span>
                  <span className={`shrink-0 rounded px-1 py-0.5 font-mono text-[11px] ${sev.badge}`}>
                    {it.severity === 'error'
                      ? `连续失败 ${it.consecutive_failures} 次`
                      : '离线/过期'}
                  </span>
                </div>
                <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                  {it.category} · 失败于 {fmtClock(it.last_fail_ts)} · {fmtAgo(it.secs_since_fail)}
                </div>
                {isOpen && it.last_error && (
                  <div className="mt-1.5 break-words rounded bg-secondary/60 px-2 py-1 font-mono text-[11px] text-muted-foreground">
                    {friendlySourceError(it.last_error)}
                  </div>
                )}
              </div>
              <span className="ml-1 shrink-0 text-[11px] text-muted-foreground">
                {isOpen ? '▾' : '▸'}
              </span>
            </button>
            <div className="flex items-center justify-between gap-2 border-t border-border/50 px-3 py-1.5">
              <a
                href="/sources"
                target="_self"
                className="font-mono text-[11px] text-muted-foreground underline-offset-2 hover:underline"
              >
                查看全部信息源 →
              </a>
              <button
                type="button"
                onClick={() => dismiss(k)}
                className="rounded bg-emerald-600 px-2 py-0.5 font-mono text-[11px] font-medium text-white hover:bg-emerald-700"
                data-testid="dismiss-alert"
              >
                ✅ 我知道了
              </button>
            </div>
          </div>
        )
      })}
      <div className="pointer-events-auto surface-card flex items-center justify-between gap-2 px-3 py-1.5">
        <span className="font-mono text-[11px] text-muted-foreground">
          共 {sorted.length} 个信息源异常
        </span>
        <button
          type="button"
          onClick={() => setPanelOpen(false)}
          className="font-mono text-[11px] text-muted-foreground underline-offset-2 hover:underline"
        >
          收起 ▴
        </button>
      </div>
      {restCount > 0 ? (
        <div className="pointer-events-auto surface-card flex items-center justify-between gap-2 px-3 py-2">
          <span className="font-mono text-[11px] text-muted-foreground">
            还有 {restCount} 个信息源异常
          </span>
          <a
            href="/sources"
            target="_self"
            className="font-mono text-[11px] text-muted-foreground underline-offset-2 hover:underline"
          >
            查看全部 →
          </a>
        </div>
      ) : null}
    </div>
  )
}

export default SourceStatusAlerts