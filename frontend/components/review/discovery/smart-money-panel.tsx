'use client'

/* ════════════════════════════════════════════════════════════════════════
 * 💎 聪明钱追踪(v285+ · /discover 段 0.5)
 *
 * 用途:把 Hyperliquid 公开钱包加进 watchlist,每 30s 拉 userFills +
 *      clearinghouseState,在 /discover 顶部显示「最近 50 笔成交」和
 *      「当前持仓」,并通过浏览器 Notification API + 提示音做「刚刚
 *      开 BTC 多 0.5x」实时提醒。
 *
 * 设计取舍:
 *  · 不上 WebSocket(链上 WS 要鉴权): 30s 轮询足够,后端 5s 缓存吸收瞬时洪峰。
 *  · watchlist 在 localStorage(零状态、零后端依赖),刷新页面不丢。
 *  · 提醒去重:用 `seenTids` Set 记录已见 fill.id,只在「真正新增」时弹通知。
 *  · Notification 权限按需索取,首次点「🔔 开启浏览器通知」按钮才弹权限请求,
 *    不在 mount 时骚扰用户(浏览器会把自动请求当 spam 屏蔽)。
 *  · 没有「声音文件」依赖 — 用 Web Audio API 即兴合成 880 Hz 短促提示,
 *    一行,零额外资源。
 * ════════════════════════════════════════════════════════════════════════ */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Bell, BellOff, Plus, RefreshCw, Trash2, ExternalLink, TrendingUp, TrendingDown,
  AlertTriangle, Activity, Copy, Check,
} from 'lucide-react'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/utils'

// ── 常量 ──────────────────────────────────────────────────────────────

// 不再写死兜底钱包地址:watchlist 留空时面板提示用户自行添加,
// 地址只存在本地 localStorage,刷新/换设备由用户自己维护。
const POLL_MS = 120_000
const STORAGE_KEY = 'trident_smart_money_watchlist_v1'
const SEEDED_KEY = 'trident_smart_money_seeded_v1'
const SEEN_KEY_PREFIX = 'trident_smart_money_seen_'

// v498: 预置名单 —— 从 Hyperliquid 公开 30 日榜（stats-data…/leaderboard，48,051 个账户）
// 里筛出来的：30 日 PnL > $3M、ROI 59%~154%、账户规模 $1.8M–$16M、
// 且 24h 内有真实成交（9~392 笔），避免选到零成交的"僵尸大户"或刷量做市机器人。
// 只在用户从未设置过 watchlist 时播种一次；用户删完/清空后不会再自动塞回来。
const DEFAULT_WATCH: string[] = [
  '0xba5d9cc840745aeeeeedac5712e32f13ab8ea352', // 30d +154%，BTC / 布伦特原油
  '0x3440f23a87f1950e7a88cd248fd270e92d1132c5', // 30d +123%，BTC / ETH
  '0x47f5a6397b79deaaca5c456503e7c1a49357b942', // 30d +59%，多币种轮动
  '0xa20fb0c9e04063eec5be286e9269028d966646fa', // 30d +73%，ARB / LIT
  '0x7a6d5fc57f6906f337c48fe2763c3a501304f79c', // 30d +112%，ZRO 为主
  '0xcf5343ba750a6e30afbb1dadda08bc78f8c8ee11', // 30d +148%，低频重仓
]
const DEFAULT_LABEL = (a: string) => `${a.slice(0, 6)}…${a.slice(-4)}`
const ADDR_RE = /^0[xX][0-9a-fA-F]{40}$/

// ── 类型 ──────────────────────────────────────────────────────────────

type Fill = {
  tid: number | null
  time: number                // ms epoch
  coin: string
  px: string
  sz: string
  side: 'B' | 'A' | null      // B=buy / A=ask-sell
  dir: string | null          // "Open Long" / "Close Short" / ...
  closed_pnl: string | null
  fee: string | null
  fee_token: string | null
  hash: string | null
}

type Position = {
  coin: string
  side: 'long' | 'short' | 'flat'
  size: string | null
  entry_px: string | null
  position_value: string | null
  unrealized_pnl: string | null
  leverage_value: number | null
  leverage_type: string | null
  liquidation_px: string | null
}

type AddressState = {
  fills: Fill[]
  positions: Position[]
  accountValue: string | null
  totalNtlPos: string | null
  loading: boolean
  error: string | null
  lastFetchedAt: number | null
}

// ── 工具 ──────────────────────────────────────────────────────────────

function loadWatchlist(): string[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    if (Array.isArray(arr) && arr.every((s) => typeof s === 'string' && ADDR_RE.test(s))) {
      return arr
    }
  } catch { /* noop */ }
  return []
}

function saveWatchlist(addrs: string[]) {
  if (typeof window === 'undefined') return
  try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(addrs)) } catch { /* noop */ }
}

function loadSeen(addrs: string[]): Map<string, Set<string>> {
  const m = new Map<string, Set<string>>()
  if (typeof window === 'undefined') return m
  for (const a of addrs) {
    const s = new Set<string>()
    try {
      const raw = window.localStorage.getItem(SEEN_KEY_PREFIX + a)
      if (!raw) { m.set(a, s); continue }
      const arr = JSON.parse(raw)
      if (Array.isArray(arr)) for (const x of arr) s.add(String(x))
    } catch { /* noop */ }
    m.set(a, s)
  }
  return m
}

function saveSeen(addr: string, seen: Set<string>) {
  if (typeof window === 'undefined') return
  try { window.localStorage.setItem(SEEN_KEY_PREFIX + addr, JSON.stringify(Array.from(seen).slice(-1500))) } catch { /* noop */ }
}

function fmtTime(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtUptime(ms: number | null): string {
  if (!ms) return '—'
  const sec = Math.max(0, Math.floor((Date.now() - ms) / 1000))
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  return `${Math.floor(sec / 3600)}h ago`
}

function dirToZh(dir: string | null, side: 'B' | 'A' | null): string {
  if (!dir) return side === 'B' ? '买入' : side === 'A' ? '卖出' : '成交'
  const d = dir.toLowerCase()
  if (d.includes('open long')) return '开多'
  if (d.includes('close long')) return '平多'
  if (d.includes('open short')) return '开空'
  if (d.includes('close short')) return '平空'
  return dir
}

function dirColor(dir: string | null, side: 'B' | 'A' | null): string {
  const zh = dirToZh(dir, side)
  if (zh === '开多' || zh === '平空') return 'text-emerald-600 dark:text-emerald-400'
  if (zh === '开空' || zh === '平多') return 'text-rose-600 dark:text-rose-400'
  return 'text-muted-foreground'
}

// 880 Hz 短促「叮」— Web Audio API 即兴合成,免文件依赖
function playBeep() {
  if (typeof window === 'undefined') return
  try {
    const Ctx = (window.AudioContext || (window as any).webkitAudioContext) as typeof AudioContext | undefined
    if (!Ctx) return
    const ctx = new Ctx()
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.frequency.value = 880
    osc.type = 'sine'
    gain.gain.value = 0
    osc.connect(gain).connect(ctx.destination)
    const t = ctx.currentTime
    gain.gain.linearRampToValueAtTime(0.18, t + 0.01)
    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.32)
    osc.start(t)
    osc.stop(t + 0.34)
    setTimeout(() => ctx.close().catch(() => {}), 600)
  } catch { /* noop */ }
}

async function requestNotifPermission(): Promise<NotificationPermission> {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'denied'
  if (Notification.permission === 'granted' || Notification.permission === 'denied') {
    return Notification.permission
  }
  try { return await Notification.requestPermission() } catch { return 'denied' }
}

function fireNotif(title: string, body: string) {
  if (typeof window === 'undefined' || !('Notification' in window)) return
  if (Notification.permission !== 'granted') return
  try { new Notification(title, { body, icon: '/favicon.ico' }) } catch { /* noop */ }
}

// ── 主组件 ────────────────────────────────────────────────────────────

export function SmartMoneyPanel() {
  const [addrs, setAddrs] = useState<string[]>([])
  const [states, setStates] = useState<Record<string, AddressState>>({})
  const [draftAddr, setDraftAddr] = useState('')
  const [addErr, setAddErr] = useState<string | null>(null)
  const [notifPerm, setNotifPerm] = useState<NotificationPermission>('default')
  const [soundOn, setSoundOn] = useState<boolean>(true)
  const seenRef = useRef<Map<string, Set<string>>>(new Map())
  const initializedRef = useRef<Set<string>>(new Set())
  const [copiedAddr, setCopiedAddr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  // mount:加载 watchlist + 还原 seen 集合 + 检查权限
  useEffect(() => {
    let w = loadWatchlist()
    // v498: 首次使用时播种榜单钱包；已有设置（含用户主动清空）就不再动
    try {
      const seeded = window.localStorage.getItem(SEEDED_KEY)
      if (w.length === 0 && !seeded) {
        w = DEFAULT_WATCH.filter((a) => ADDR_RE.test(a))
        saveWatchlist(w)
        window.localStorage.setItem(SEEDED_KEY, '1')
      }
    } catch { /* noop */ }
    setAddrs(w)
    seenRef.current = loadSeen(w)
    if (typeof window !== 'undefined' && 'Notification' in window) {
      setNotifPerm(Notification.permission)
    }
  }, [])

  // 「uptime」类显示
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 5000)
    return () => clearInterval(id)
  }, [])

  // 轮询每个地址
  useEffect(() => {
    if (addrs.length === 0) return
    let cancelled = false
    const ctrl = new AbortController()

    const applyOne = (addr: string, f: any, st: any) => {
      const fills: Fill[] = (f && f.fills) || []
      const positions: Position[] = (st && st.positions) || []

      const fmtAmt = (v: unknown): string | null =>
        v == null ? null : typeof v === 'number' ? v.toFixed(2) : String(v)

      const seen = seenRef.current.get(addr) ?? new Set<string>()
      if (!seenRef.current.has(addr)) seenRef.current.set(addr, seen)
      const newFills: Fill[] = []
      for (const fl of fills) {
        const k = String(fl.tid ?? `${fl.time}-${fl.coin}-${fl.sz}`)
        if (seen.has(k)) continue
        newFills.push(fl)
        seen.add(k)
      }
      if (seen.size > 1500) {
        const keep = Array.from(seen).slice(-1200)
        seen.clear()
        for (const x of keep) seen.add(x)
      }
      saveSeen(addr, seen)

      const isInit = !initializedRef.current.has(addr)
      if (isInit) {
        initializedRef.current.add(addr)
      } else if (newFills.length > 0) {
        const sample = newFills[0]
        const label = DEFAULT_LABEL(addr)
        const action = dirToZh(sample.dir, sample.side)
        const msg = `${label} 刚刚 ${action} ${sample.coin} ${sample.sz} @ ${sample.px}`
        if (soundOn) playBeep()
        fireNotif(`💎 聪明钱异动 · ${label}`, msg)
      }

      const err = (f && f.ok === false) ? String(f.error || '上游不可达')
        : (st && st.ok === false) ? String(st.error || '上游不可达') : null
      setStates((s) => ({
        ...s,
        [addr]: {
          fills: err ? (s[addr]?.fills ?? []) : fills,
          positions: err ? (s[addr]?.positions ?? []) : positions,
          accountValue: fmtAmt(st && st.account_value),
          totalNtlPos: fmtAmt(st && st.total_ntl_pos),
          loading: false,
          error: err ? '数据缺失（上游不可达）' : null,
          lastFetchedAt: Date.now(),
        },
      }))
    }

    // v498: 6 地址 × 2 端点 = 12 个并发请求会打满浏览器的 6 条同源连接，
    //   全部排队到超时（面板永远"拉取中"）。改成**一个** batch 请求，
    //   后端用线程池并发拉每个钱包的 fills + state。
    const fetchAll = async () => {
      if (addrs.length === 0) return
      setStates((s) => {
        const n: Record<string, AddressState> = { ...s }
        for (const a of addrs) n[a] = { ...(n[a] ?? blankState()), loading: true, error: null }
        return n
      })
      try {
        const batch = await apiGet<{ items?: Array<{ address: string; fills: any; state: any }> }>(
          `/smart-money/batch?addresses=${addrs.join(',')}`,
          { signal: ctrl.signal, timeoutMs: 100_000 },
        )
        if (cancelled) return
        for (const it of batch.items ?? []) applyOne(it.address, it.fills, it.state)
      } catch (e: any) {
        if (cancelled) return
        const raw = String(e?.message ?? e)
        const msg = /50[0-9]|502|bad gateway|timeout/i.test(raw) ? '数据缺失（上游不可达）' : raw
        setStates((s) => {
          const n: Record<string, AddressState> = { ...s }
          for (const a of addrs) n[a] = { ...(n[a] ?? blankState()), loading: false, error: msg }
          return n
        })
      }
    }
    fetchAll()
    const id = setInterval(fetchAll, POLL_MS)
    return () => { cancelled = true; ctrl.abort(); clearInterval(id) }
  }, [addrs, soundOn])

  // 操作
  const handleAdd = useCallback(() => {
    const v = draftAddr.trim()
    if (!ADDR_RE.test(v)) { setAddErr('地址格式:0x + 40 位 hex'); return }
    const norm = v.toLowerCase()
    if (addrs.includes(norm)) { setAddErr('已存在'); return }
    const next = [...addrs, norm]
    setAddrs(next); saveWatchlist(next)
    setDraftAddr(''); setAddErr(null)
  }, [draftAddr, addrs])

  const handleRemove = useCallback((addr: string) => {
    const next = addrs.filter((a) => a !== addr)
    setAddrs(next); saveWatchlist(next)
    // 清理对应 seen 缓存
    try { window.localStorage.removeItem(SEEN_KEY_PREFIX + addr) } catch { /* noop */ }
  }, [addrs])

  const handleRefresh = useCallback(() => {
    setAddrs((arr) => [...arr])  // 触发 effect 重跑
  }, [])

  const handleEnableNotif = useCallback(async () => {
    const p = await requestNotifPermission()
    setNotifPerm(p)
    if (p === 'granted') fireNotif('🔔 通知已开启', '聪明钱异动会通过浏览器通知推给你')
  }, [])

  const handleCopy = useCallback(async (addr: string) => {
    try {
      await navigator.clipboard.writeText(addr)
      setCopiedAddr(addr)
      setTimeout(() => setCopiedAddr(null), 1500)
    } catch { /* noop */ }
  }, [])

  return (
    <div className="flex flex-col gap-3">
      {/* 顶部控制条 */}
      <div className="flex flex-wrap items-center gap-2 surface-card px-2.5 py-2 text-[11px]">
        <button
          type="button"
          onClick={handleRefresh}
          className="inline-flex items-center gap-1 rounded border border-border bg-secondary/40 px-2 py-1 hover:bg-secondary/70"
          title="立即拉取"
        >
          <RefreshCw className="h-3 w-3" /> 立即刷新
        </button>
        <button
          type="button"
          onClick={() => setSoundOn((s) => !s)}
          className={cn(
            'inline-flex items-center gap-1 rounded border px-2 py-1',
            soundOn
              ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
              : 'border-border bg-secondary/40 text-muted-foreground',
          )}
          title="切换提示音"
        >
          {soundOn ? <Bell className="h-3 w-3" /> : <BellOff className="h-3 w-3" />}
          声音 {soundOn ? '开' : '关'}
        </button>
        {notifPerm !== 'granted' ? (
          <button
            type="button"
            onClick={handleEnableNotif}
            className="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/10 px-2 py-1 text-primary"
            title="开启浏览器通知"
          >
            <Bell className="h-3 w-3" /> 开启浏览器通知
          </button>
        ) : (
          <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
            <Bell className="h-3 w-3" /> 通知已开启
          </span>
        )}
        <span className="text-muted-foreground">·</span>
        <span className="text-muted-foreground">轮询 {POLL_MS / 1000}s · {addrs.length} 个地址 · 首轮约 20–60s（Hyperliquid TLS 较慢）</span>
      </div>

      {/* 名单来源说明（v498 预置） */}
      <div className="font-mono text-[10px] leading-relaxed text-muted-foreground">
        名单来自 Hyperliquid 公开 30 日榜：30d PnL &gt; $3M / ROI 59%–154% / 账户 $1.8M–$16M，
        且 24h 内有真实成交。仅作链上行为参考，不代表建议；可随时删除或换成你自己的地址。
      </div>

      {/* 添加地址 */}
      <div className="flex flex-wrap items-center gap-2 surface-card px-2.5 py-2 text-[11px]">
        <Plus className="h-3 w-3 text-muted-foreground" />
        <input
          type="text"
          value={draftAddr}
          onChange={(e) => { setDraftAddr(e.target.value); setAddErr(null) }}
          onKeyDown={(e) => { if (e.key === 'Enter') handleAdd() }}
          placeholder="0x + 40 位 hex 地址(Enter 添加)"
          className="min-w-[280px] flex-1 rounded border border-border bg-background/40 px-2 py-1 font-mono text-[11px] focus:border-primary focus:outline-none"
        />
        <button
          type="button"
          onClick={handleAdd}
          className="rounded border border-primary/50 bg-primary/15 px-2 py-1 text-primary hover:bg-primary/25"
        >添加</button>
        {addErr && <span className="text-rose-500">⚠ {addErr}</span>}
      </div>

      {/* 每地址一块 */}
      {addrs.length === 0 && (
        <div className="rounded border border-dashed border-border p-3 font-mono text-[11px] text-muted-foreground">
          还没有追踪地址 — 在上方输入 0x 开头的钱包地址添加,成交与持仓会显示在这里。
        </div>
      )}
      {addrs.map((addr) => (
        <AddressCard
          key={addr}
          addr={addr}
          state={states[addr] ?? blankState()}
          tick={tick}
          onRemove={() => handleRemove(addr)}
          onCopy={() => handleCopy(addr)}
          copied={copiedAddr === addr}
        />
      ))}
    </div>
  )
}

function blankState(): AddressState {
  return { fills: [], positions: [], accountValue: null, totalNtlPos: null, loading: false, error: null, lastFetchedAt: null }
}

// ── 单地址卡片 ────────────────────────────────────────────────────────

function AddressCard(props: {
  addr: string
  state: AddressState
  tick: number
  onRemove: () => void
  onCopy: () => void
  copied: boolean
}) {
  const { addr, state, tick, onRemove, onCopy, copied } = props
  const label = DEFAULT_LABEL(addr)
  const hlUrl = `https://hyperbot.network/trader/${addr}`

  const recentFills = useMemo(() => state.fills.slice(0, 50), [state.fills])
  const fillsLast24h = useMemo(() => {
    const cutoff = Date.now() - 24 * 3600 * 1000
    return state.fills.filter((f) => f.time >= cutoff)
  }, [state.fills])
  void tick

  return (
    <div className="surface-card flex flex-col gap-2 p-2.5">
      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-1.5">
        <span className="font-mono text-[12px] font-bold text-foreground">{label}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{addr}</span>
        <a href={hlUrl} target="_blank" rel="noopener noreferrer"
           className="inline-flex items-center gap-0.5 text-[11px] text-primary hover:underline">
          <ExternalLink className="h-3 w-3" /> hyperbot
        </a>
        <button type="button" onClick={onCopy}
                className="inline-flex items-center gap-0.5 text-[11px] text-muted-foreground hover:text-foreground"
                title="复制完整地址">
          {copied ? <Check className="h-3 w-3 text-emerald-500" /> : <Copy className="h-3 w-3" />}
          {copied ? '已复制' : '复制'}
        </button>
        <span className="ml-auto flex items-center gap-2 text-[11px]">
          {state.loading && <span className="inline-flex items-center gap-0.5 text-primary"><RefreshCw className="h-3 w-3 animate-spin" /> 拉取中</span>}
          {state.error && <span className="inline-flex items-center gap-0.5 text-rose-500"><AlertTriangle className="h-3 w-3" /> {state.error.slice(0, 40)}</span>}
          {state.lastFetchedAt && !state.error && (
            <span className="text-muted-foreground"><Activity className="inline h-3 w-3" /> {fmtUptime(state.lastFetchedAt)}</span>
          )}
          <button type="button" onClick={onRemove}
                  className="rounded p-0.5 text-muted-foreground hover:bg-rose-500/15 hover:text-rose-500"
                  title="从 watchlist 移除">
            <Trash2 className="h-3 w-3" />
          </button>
        </span>
      </div>

      <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
        <div className="surface-card p-2 text-[11px]">
          <div className="text-[11px] tracking-widest text-muted-foreground">账户净值</div>
          <div className="mt-0.5 font-mono text-lg font-bold text-foreground">
            {state.accountValue ?? '—'}<span className="ml-0.5 text-[11px] text-muted-foreground">USDC</span>
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            名义持仓 {state.totalNtlPos ?? '—'} · 24h 内 {fillsLast24h.length} 笔成交
          </div>
        </div>
        <div className="surface-card p-2 text-[11px] md:col-span-2">
          <div className="mb-1 text-[11px] tracking-widest text-muted-foreground">当前持仓</div>
          {state.positions.length === 0 ? (
            <div className="text-[11px] text-muted-foreground">当前 0 持仓(可能刚平完,或资金还没建仓)</div>
          ) : (
            <div className="flex flex-col gap-0.5">
              {state.positions.map((p, i) => (
                <div key={`${p.coin}-${i}`} className="flex items-center justify-between font-mono text-[11px]">
                  <span className="flex items-center gap-1.5">
                    {p.side === 'long' ? <TrendingUp className="h-3 w-3 text-emerald-500" /> : <TrendingDown className="h-3 w-3 text-rose-500" />}
                    <span className="font-bold">{p.coin}</span>
                    <span className={p.side === 'long' ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}>
                      {p.side.toUpperCase()}
                    </span>
                    <span className="text-muted-foreground">× {p.leverage_value ?? '?'}{p.leverage_type === 'cross' ? 'x(全仓)' : p.leverage_type === 'isolated' ? 'x(逐仓)' : ''}</span>
                  </span>
                  <span className="text-muted-foreground">
                    size {p.size} · 入场 {p.entry_px ?? '—'} · 浮盈 {p.unrealized_pnl ?? '—'}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div>
        <div className="mb-1 flex items-baseline gap-2 text-[11px] uppercase tracking-widest text-muted-foreground">
          最近成交 · 最多 50 笔
          <span className="text-muted-foreground/70">总 {state.fills.length} 笔 · 24h {fillsLast24h.length}</span>
        </div>
        {recentFills.length === 0 ? (
          <div className="text-[11px] text-muted-foreground">无成交</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mono text-[11px]">
              <thead className="text-[11px] uppercase tracking-widest text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="px-1 py-1 text-left">时间</th>
                  <th className="px-1 py-1 text-left">币种</th>
                  <th className="px-1 py-1 text-left">动作</th>
                  <th className="px-1 py-1 text-right">价格</th>
                  <th className="px-1 py-1 text-right">数量</th>
                  <th className="px-1 py-1 text-right">已实现 PnL</th>
                  <th className="px-1 py-1 text-right">手续费</th>
                </tr>
              </thead>
              <tbody>
                {recentFills.map((f, i) => (
                  <tr key={`${f.tid ?? i}-${f.time}`} className="border-b border-border/50 last:border-0">
                    <td className="px-1 py-1 text-muted-foreground">{fmtTime(f.time)}</td>
                    <td className="px-1 py-1 font-bold">{f.coin}</td>
                    <td className={cn('px-1 py-1 font-bold', dirColor(f.dir, f.side))}>{dirToZh(f.dir, f.side)}</td>
                    <td className="px-1 py-1 text-right">{f.px}</td>
                    <td className="px-1 py-1 text-right">{f.sz}</td>
                    <td className={cn('px-1 py-1 text-right',
                      Number(f.closed_pnl) > 0 ? 'text-emerald-600 dark:text-emerald-400'
                      : Number(f.closed_pnl) < 0 ? 'text-rose-600 dark:text-rose-400' : 'text-muted-foreground')}>
                      {f.closed_pnl && f.closed_pnl !== '0' ? f.closed_pnl : '—'}
                    </td>
                    <td className="px-1 py-1 text-right text-muted-foreground">{f.fee ? `${f.fee} ${f.fee_token ?? ''}` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
