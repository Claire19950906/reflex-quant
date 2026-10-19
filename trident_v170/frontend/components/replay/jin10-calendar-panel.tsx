'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { apiGet } from '@/lib/api'
import { usePollRefresh } from '@/lib/hooks/use-poll-refresh'

// 金十财经日历面板 — 数据来自 /api/news/jin10/calendar（jin10_mcp list_calendar）
// 独立模块：本周财经日历（公布时间 / 重要性★ / 实际·预测·前值），按日期分组

export type Jin10CalendarItem = {
  pub_time: string
  star: number | null
  title: string
  actual: number | string | null
  consensus: number | string | null
  previous: number | string | null
}

type CalendarResponse =
  | { status: 'ok'; items: Jin10CalendarItem[]; error?: string }
  | { status: 'unavailable' | 'empty'; items?: never; error?: string }

function fmtVal(v: number | string | null): string {
  if (v === null || v === undefined || v === '') return '—'
  return String(v)
}

// '2026-08-31T07:00:00+08:00' → { day: '2026-08-31', time: '07:00' }
function splitTime(iso: string): { day: string; time: string } {
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(iso || '')
  if (m) return { day: m[1], time: m[2] }
  return { day: (iso || '').slice(0, 10), time: (iso || '').slice(11, 16) }
}

function dayLabel(day: string, todayKey: string): string {
  const d = new Date(`${day}T00:00:00`)
  const week = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  const label = `${day.slice(5)} 周${week}`
  if (todayKey && day === todayKey) return `${label} · 今天`
  return label
}

// 本地"今天"字符串（YYYY-MM-DD）；只在客户端计算，SSR 与首帧保持 '' 一致，
// 避免服务端/浏览器时区差异导致 hydration mismatch（触发 dev error overlay）。
function localTodayKey(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

// v277: 标题 → 1 行白话影响
const TITLE_IMPACT_CN: Record<string, string> = {
  FOMC: '鹰/鸽信号 — 极度影响 2Y/10Y 利率、美元、股票、黄金、数字币。',
  美联储: '鹰/鸽信号 — 极度影响 2Y/10Y 利率、美元、股票、黄金、数字币。',
  Powell: '鲍威尔讲话 — 市场最大单一波动源。',
  鲍威尔: '鲍威尔讲话 — 市场最大单一波动源。',
  CPI: '通胀 — 强劲=鹰(加息预期), 弱=鸽(降息预期)。',
  通胀: '通胀 — 强劲=鹰(加息预期), 弱=鸽(降息预期)。',
  PCE: 'Fed 偏好通胀指标 — 强劲=鹰, 弱=鸽。',
  非农: '非农就业 — 强劲=鹰(加息), 弱=鸽(降息)。',
  GDP: '经济增速 — 强劲=鹰, 弱=鸽。',
  PMI: '经济景气度 — 高于50且加速=鹰, 低于50=鸽。',
  利率决议: '央行议息 — 直接改写短期利率路径。',
  Rate: '央行议息 — 直接改写短期利率路径。',
  PPI: 'PPI — 上游通胀,常领先 CPI。',
}

function impactCnForTitle(title: string): string {
  for (const k of Object.keys(TITLE_IMPACT_CN)) {
    if (title.includes(k)) return TITLE_IMPACT_CN[k]
  }
  return ''
}

export function Jin10CalendarPanel() {
  const [items, setItems] = useState<Jin10CalendarItem[]>([])
  const [note, setNote] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [todayKey, setTodayKey] = useState('')

  useEffect(() => {
    setTodayKey(localTodayKey())
  }, [])

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiGet<CalendarResponse>('/news/jin10/calendar')
      if (data.status !== 'ok' || !data.items) {
        setItems([])
        setNote(data.error || (data.status === 'unavailable' ? '金十日历不可用（检查 JIN10_MCP_TOKEN）' : '暂无日历数据'))
        return
      }
      setItems(data.items)
      setNote(null)
    } catch (cause) {
      setItems([])
      setNote(cause instanceof Error ? cause.message : '金十日历接口不可用')
    } finally {
      setLoading(false)
    }
  }, [])

  // v302 P0+：手写 setInterval → usePollRefresh（统一 alive flag + cleanup）
  usePollRefresh(reload, 30_000)

  // 按日期分组（保持后端返回的时间顺序：同一天内按时间正序）
  const groups = useMemo(() => {
    const map = new Map<string, Jin10CalendarItem[]>()
    for (const item of items) {
      const { day } = splitTime(item.pub_time)
      if (!map.has(day)) map.set(day, [])
      map.get(day)!.push(item)
    }
    return Array.from(map.entries())
  }, [items])

  return (
    <div className="surface-card shrink-0 p-2.5">
      <div className="flex items-center justify-between">
        <h3 className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">金十财经日历 · 本周</h3>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={loading}
          className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground disabled:opacity-60"
        >
          {loading ? '刷新中…' : '刷新'}
        </button>
      </div>
      <div className="mt-1 flex items-center gap-3 font-mono text-[9px] text-muted-foreground/70">
        <span>★ = 重要性（图例见页面顶端板块统筹）</span>
      </div>
      {note ? (
        <div className="mt-1.5 font-mono text-[11px] text-muted-foreground">{note}</div>
      ) : (
        <div className="thin-scroll mt-2 space-y-2.5 overflow-y-auto">
          {groups.map(([day, rows]) => {
            const isToday = day === todayKey
            return (
              <div key={day}>
                <div className={`mb-1 font-mono text-[10px] ${isToday ? 'text-primary' : 'text-muted-foreground/70'}`}>
                  {dayLabel(day, todayKey)} · {rows.length} 项
                </div>
                <div className="space-y-0.5">
                  {rows.map((item, idx) => {
                    const published = item.actual !== null && item.actual !== undefined && item.actual !== ''
                    const star = item.star ?? 0
                    const isFive = star >= 5          // v277: 5★ 重大事件
                    const high = star >= 3 || isFive
                    // 配色：5★已公布=玫红实心（最醒目）；5★未公布=玫红预警框（即将公布/红框脉冲感）；
                    //       4★已公布=深蓝实心；4★未公布=蓝色预警框；
                    //       普通已公布=灰色；普通未公布=白色
                    const rowCls = isFive
                      ? published
                        ? 'border-rose-500 bg-rose-500/30 text-rose-700 dark:text-rose-200'
                        : 'border-rose-500/80 bg-rose-500/15 ring-1 ring-rose-500/40'
                      : high
                        ? published
                          ? 'border-primary bg-primary/25 text-primary'
                          : 'border-primary/40 bg-primary/10'
                        : published
                          ? 'border-border bg-secondary/40 text-muted-foreground'
                          : 'border-border text-foreground'
                    const impactCn = isFive || high ? impactCnForTitle(item.title) : ''
                    return (
                      <div
                        key={`${day}-${idx}`}
                        className={`rounded border px-2 py-1 font-mono text-[10px] ${rowCls}`}
                      >
                        <div className="grid grid-cols-[44px_18px_1fr_auto] items-center gap-x-2 md:grid-cols-[52px_22px_1fr_auto]">
                          <span className="opacity-80">{splitTime(item.pub_time).time}</span>
                          <span className={isFive ? 'text-rose-700 dark:text-rose-200' : high ? 'text-primary' : 'text-muted-foreground/60'}>
                            {isFive && <span aria-hidden>🌟</span>}{'★'.repeat(star)}
                          </span>
                          <span className={`truncate ${isFive ? 'font-bold text-foreground' : ''}`}>
                            {item.title}
                          </span>
                          <span className="whitespace-nowrap text-[9px] text-muted-foreground">
                            {published ? (
                              <span className={isFive ? 'text-rose-700 dark:text-rose-200' : high ? 'text-primary' : ''}>
                                实际 <b>{fmtVal(item.actual)}</b>
                              </span>
                            ) : (
                              <span>
                                预测 <b>{fmtVal(item.consensus)}</b> · 前值 {fmtVal(item.previous)}
                              </span>
                            )}
                          </span>
                        </div>
                        {isFive && impactCn && (
                          <div className="mt-0.5 pl-1 text-[9px] leading-3.5 text-rose-700/90 dark:text-rose-200/80">
                            📌 {impactCn}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
          {groups.length === 0 && !note && (
            <div className="font-mono text-[11px] text-muted-foreground">加载中…</div>
          )}
        </div>
      )}
    </div>
  )
}
