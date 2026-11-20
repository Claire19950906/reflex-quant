// -*- coding: utf-8 -*-
/** tz-utils.ts — 前端时间结构化工具（与 Python tz_utils.format_dual 等价）。 */
export type TzName = 'Asia/Shanghai' | 'UTC' | 'US/Eastern' | 'US/Pacific'

const TZ_OPTIONS: readonly TzName[] = ['Asia/Shanghai', 'UTC', 'US/Eastern', 'US/Pacific'] as const
const DEFAULT_TZ: TzName = 'Asia/Shanghai'
const STORAGE_KEY = 'TRIDENT_TZ'

function _readStorageTz(): TzName | null {
  if (typeof window === 'undefined') return null
  try {
    const v = window.localStorage.getItem(STORAGE_KEY)
    if (v && (TZ_OPTIONS as readonly string[]).includes(v)) return v as TzName
  } catch { /* SSR or quota */ }
  return null
}

export function getDisplayTzName(): TzName {
  const stored = _readStorageTz()
  return stored ?? DEFAULT_TZ
}

export function setDisplayTz(name: TzName): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, name)
    window.dispatchEvent(new CustomEvent('trident:tz-changed', { detail: name }))
  } catch { /* ignore */ }
}

export function listDisplayTzOptions(): readonly TzName[] { return TZ_OPTIONS }

export interface DualTime {
  epoch: number
  iso: string
  utc: string
  cn: string
  us_eastern: string
  us_pacific: string
  display: string
  display_tz: TzName
}

function _fmt(date: Date, tz: string): string {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
  return fmt.format(date).replace(', ', ' ')
}

function _tzNameToIANA(name: TzName): string {
  switch (name) {
    case 'Asia/Shanghai': return 'Asia/Shanghai'
    case 'UTC': return 'UTC'
    case 'US/Eastern': return 'America/New_York'
    case 'US/Pacific': return 'America/Los_Angeles'
  }
}

export function formatDual(epoch: number | string | null | undefined, displayTz?: TzName): DualTime {
  const e = typeof epoch === 'string' ? parseInt(epoch, 10) : (epoch ?? 0)
  if (!Number.isFinite(e) || e <= 0) {
    return {
      epoch: 0, iso: '', utc: '', cn: '', us_eastern: '', us_pacific: '', display: '',
      display_tz: displayTz ?? getDisplayTzName(),
    }
  }
  const d = new Date(e * 1000)
  const tz = displayTz ?? getDisplayTzName()
  return {
    epoch: e,
    iso: d.toISOString().replace(/\.\d{3}Z$/, 'Z'),
    utc: _fmt(d, 'UTC'),
    cn: _fmt(d, 'Asia/Shanghai'),
    us_eastern: _fmt(d, 'America/New_York'),
    us_pacific: _fmt(d, 'America/Los_Angeles'),
    display: _fmt(d, _tzNameToIANA(tz)),
    display_tz: tz,
  }
}

export function validateIso8601(value: unknown): { ok: boolean; reason: string; normalized: string } {
  if (value === null || value === undefined || value === '') {
    return { ok: false, reason: 'empty', normalized: '' }
  }
  const s = String(value).trim()
  if (s === 'N/A' || s === 'AMBIGUOUS') {
    return { ok: true, reason: 'na_marker', normalized: s }
  }
  const naiveRe = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$/
  if (naiveRe.test(s)) {
    // v302+ P0+【修复】trident 后端 naive 串代表"北京时间"，补 +08:00 而非 +Z。
    const d = new Date(s.replace(' ', 'T') + '+08:00')
    if (!isNaN(d.getTime())) {
      return { ok: true, reason: 'naive_iso', normalized: d.toISOString().replace(/\.\d{3}Z$/, 'Z') }
    }
    return { ok: false, reason: 'invalid_naive_iso', normalized: '' }
  }
  const awareRe = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(Z|[+\-]\d{2}:?\d{2})$/
  if (awareRe.test(s)) {
    const d = new Date(s.includes('T') ? s : s.replace(' ', 'T'))
    if (!isNaN(d.getTime())) {
      return { ok: true, reason: 'aware_iso', normalized: d.toISOString().replace(/\.\d{3}Z$/, 'Z') }
    }
  }
  return { ok: false, reason: 'not_iso8601', normalized: '' }
}

export function getWindowLabel(minutesAfter: number | string): '5m' | '30m' | '2h' | '1d' | 'extended' {
  const m = typeof minutesAfter === 'string' ? parseFloat(minutesAfter) : (minutesAfter ?? 0)
  if (!Number.isFinite(m) || m <= 5) return '5m'
  if (m <= 30) return '30m'
  if (m <= 120) return '2h'
  if (m <= 1440) return '1d'
  return 'extended'
}

export const WINDOW_LABELS = ['5m', '30m', '2h', '1d', 'extended'] as const

/**
 * v302+ P0+【新增】把后端 raw_news/timestamp 字符串统一按 Asia/Shanghai 解读为 unix 秒。
 *
 * 后端 _format_time 同时输出两种格式:
 *   - 'YYYY-MM-DD HH:MM:SS' (Beijing 朴素串, 旧 realtime_filter 用 datetime('now','localtime'))
 *   - 'YYYY-MM-DDTHH:MM:SS+08:00' (ISO 带偏移, 新版 online 写入用)
 * 两种都代表"北京时间"。如果前端直接 `new Date(s.replace(' ', 'T')).getTime()`,
 * 无 tz 那部分会被浏览器按系统本地时区解释, 北京用户 OK 但其它时区
 * 会把早上 8 点前的今日新闻误判成"昨日"(8h 偏差)。
 *
 * 这里强制补 `+08:00` 后缀确保跨时区稳定。
 * 返回 null 表示无法解析, 调用方应跳过该事件。
 */
export function parseBjTimestamp(s: string): number | null {
  if (!s) return null
  // 已经是 ISO 带偏移 (Z / +08:00 / -05:00 等)
  if (/[Tt]\d{2}:\d{2}/.test(s) && /[Zz]|[+-]\d{2}:?\d{2}$/.test(s)) {
    const ms = Date.parse(s)
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null
  }
  // Beijing 朴素串 / 北京 ISO 无偏移 → 强制补 +08:00
  const m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/)
  if (!m) return null
  const ms = Date.parse(`${m[1]}T${m[2]}+08:00`)
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null
}