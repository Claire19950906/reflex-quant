'use client'

import { friendlyError } from '@/lib/friendly-error'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { updatePaperTrading, type PaperTradingSettings } from '@/lib/replay-data'
import { CapabilityPanel } from '@/components/quant/capability-panel'

// 交易设置 — 为什么单独开一页
// 这些参数（方向门槛 / 止损倍数 / 止盈倍数与比例 / 三个开关）全部作用于**真实纸交易**：
// decision_guard 用买卖门槛切方向，quick_sim.check_tp_sl 用止损止盈参数结算持仓。
// 它们此前只散落在 /replay（模拟盘）面板里，名字叫「回放」，用户很容易以为只是回测用的。
// 这里按作用域分组，让每个数字旁边写明它影响哪一步。

type NumKey =
  | 'buy_min_score' | 'sell_min_score'
  | 'sl_mult'
  | 'tp1_mult' | 'tp1_ratio' | 'tp2_mult' | 'tp2_ratio'

type NumField = {
  key: NumKey
  label: string
  step: string
  min: number
  max: number
  hint: string
}

type SwitchKey = 'tp_enabled' | 'gate_enabled' | 'market_structure_enabled'

type Patch = Partial<Pick<PaperTradingSettings,
  NumKey | SwitchKey | 'news_trade_max_age_seconds'>>

const GROUPS: { title: string; scope: string; fields: NumField[] }[] = [
  {
    title: '方向判定',
    scope: 'decision_guard 每轮把最终分切成 BUY / SELL / HOLD。这两个门槛决定「多少人能进门」。',
    fields: [
      { key: 'buy_min_score', label: 'BUY 门槛', step: '0.05', min: 0.1, max: 0.9, hint: 'final_score ≥ 此值 → BUY' },
      { key: 'sell_min_score', label: 'SELL 门槛', step: '0.05', min: 0.1, max: 0.9, hint: 'final_score ≤ -此值 → SELL' },
    ],
  },
  {
    title: '止损',
    scope: 'quick_sim.check_tp_sl 每轮按 ATR 倍数算止损价。这是唯一直接决定单笔亏损上限的参数。',
    fields: [
      { key: 'sl_mult', label: '止损 ×ATR', step: '0.05', min: 0.1, max: 3, hint: '0.5 = 0.5×ATR，钳制 0.1~3.0' },
    ],
  },
  {
    title: '止盈',
    scope: '两档止盈：倍数定目标价，比例定该档平掉多少仓位。两档比例合计不得超过 0.95。',
    fields: [
      { key: 'tp1_mult', label: 'TP1 倍数', step: '0.1', min: 0.1, max: 3, hint: '第一档目标价' },
      { key: 'tp1_ratio', label: 'TP1 比例', step: '0.05', min: 0, max: 0.9, hint: '第一档平掉的仓位比例' },
      { key: 'tp2_mult', label: 'TP2 倍数', step: '0.1', min: 0.1, max: 3, hint: '第二档目标价' },
      { key: 'tp2_ratio', label: 'TP2 比例', step: '0.05', min: 0, max: 0.9, hint: '第二档平掉的仓位比例' },
    ],
  },
]

const SWITCHES: { key: SwitchKey; label: string; hint: string }[] = [
  { key: 'tp_enabled', label: '止盈止损结算', hint: '关闭后 check_tp_sl 直接跳过，持仓只能等到期或人工处理' },
  { key: 'gate_enabled', label: '证据 / 策略闸门', hint: '关闭即宽松模式：BUY/SELL 只要有真实行情就建仓' },
  { key: 'market_structure_enabled', label: '盘面结构分析', hint: 'AI 分析时是否附带多周期盘面结构判断' },
]

export default function SettingsPage() {
  const [settings, setSettings] = useState<PaperTradingSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null)
  // v345: 原来接口一慢/一挂，这一页就是**纯白屏**（settings=null 且没有 error）,
  //   用户只看到标题不知道在等什么。这里给一个带秒数的读取态 + 重试。
  const [waited, setWaited] = useState(0)

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/replay/settings`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setSettings((await r.json()) as PaperTradingSettings)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '读取设置失败')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (settings || error) return
    const t = setInterval(() => setWaited((w) => w + 1), 1000)
    return () => clearInterval(t)
  }, [settings, error])

  const save = useCallback(
    async (patch: Patch, label: string) => {
      if (!settings) return
      setSaving(label)
      try {
        const saved = await updatePaperTrading({
          is_running: settings.is_running,
          tracks: settings.tracks,
          ...patch,
        })
        setSettings(saved)
        setError(null)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : `${label} 保存失败`)
      } finally {
        setSaving(null)
      }
    },
    [settings],
  )

  return (
    <main className="mx-auto max-w-3xl px-4 py-6 font-mono">
      <h1 className="text-lg font-semibold text-foreground">交易设置</h1>
      <p className="mt-1 text-xs text-muted-foreground">
        下面每一项都会立刻写入数据库，引擎下一批处理即生效（无缓存、无需重启）。
        实时准入底线（信号阈值）由策略版本管理，不在本页调整。
      </p>

      {error && (
        <p className="mt-3 rounded border border-short/40 bg-short/5 px-3 py-2 text-xs text-short">{friendlyError(error)}</p>
      )}

      {!settings && !error && (
        <div className="mt-4 flex items-center gap-3 rounded border border-dashed border-border bg-secondary/20 px-3 py-3 text-xs text-muted-foreground">
          <span>读取设置中… 已等待 {waited}s</span>
          <span className="text-[11px] text-muted-foreground/70">
            接口较慢时这里会一直等；超过 20s 基本可判定后端没响应
          </span>
          {waited >= 20 && (
            <button
              type="button"
              onClick={() => { setWaited(0); void load() }}
              className="ml-auto rounded border border-border px-2 py-0.5 text-[11px] text-foreground hover:bg-secondary"
            >
              重试
            </button>
          )}
        </div>
      )}

      {settings ? (
        <div className="mt-5 space-y-4">
          {GROUPS.map((group) => (
            <section key={group.title} className="rounded border border-border bg-card p-3">
              <div className="flex items-baseline justify-between gap-3">
                <h2 className="text-sm font-semibold text-foreground">{group.title}</h2>
              </div>
              <p className="mb-3 mt-0.5 text-[11px] leading-relaxed text-muted-foreground">{group.scope}</p>
              <div className="space-y-2">
                {group.fields.map((field) => (
                  <label key={field.key} className="flex items-center gap-3 text-xs">
                    <span className="w-24 shrink-0 text-muted-foreground">{field.label}</span>
                    <input
                      type="number"
                      step={field.step}
                      min={field.min}
                      max={field.max}
                      defaultValue={settings[field.key] ?? undefined}
                      key={`${field.key}-${String(settings[field.key] ?? '')}`}
                      disabled={saving === field.label}
                      onBlur={(event) => {
                        const next = Number(event.target.value)
                        const current = settings[field.key]
                        if (!Number.isFinite(next) || (current != null && next === current)) return
                        void save({ [field.key]: next } as Patch, field.label)
                      }}
                      className="w-20 rounded border border-border bg-background px-2 py-1 text-foreground disabled:opacity-50"
                    />
                    <span className="text-[11px] text-muted-foreground">{field.hint}</span>
                  </label>
                ))}
              </div>
            </section>
          ))}

          <section className="rounded border border-border bg-card p-3">
            <h2 className="text-sm font-semibold text-foreground">开关</h2>
            <p className="mb-3 mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
              这三个开关决定闸门和结算是否执行，关掉任何一个都会显著放大风险。
            </p>
            <div className="space-y-2">
              {SWITCHES.map((item) => {
                const on = Boolean(settings[item.key] ?? true)
                return (
                  <div key={item.key} className="flex items-center gap-3 text-xs">
                    <span className="w-24 shrink-0 text-muted-foreground">{item.label}</span>
                    <button
                      type="button"
                      disabled={saving === item.label}
                      onClick={() => void save({ [item.key]: !on } as Patch, item.label)}
                      className={`rounded border px-2 py-1 text-[11px] disabled:opacity-50 ${
                        on ? 'border-long/40 bg-long/10 text-long' : 'border-border text-muted-foreground'
                      }`}
                    >
                      {on ? 'ON' : 'OFF'}
                    </button>
                    <span className="text-[11px] text-muted-foreground">{item.hint}</span>
                  </div>
                )
              })}
            </div>
          </section>

          <section className="rounded border border-border bg-card p-3">
            <h2 className="text-sm font-semibold text-foreground">新闻时效</h2>
            <p className="mb-3 mt-0.5 text-[11px] text-muted-foreground">
              新闻发布超过这个时长（秒）不再建仓，防止拿旧新闻追单。
            </p>
            <label className="flex items-center gap-3 text-xs">
              <span className="w-24 shrink-0 text-muted-foreground">最大时效</span>
              <input
                type="number"
                step="60"
                min={30}
                max={86400}
                defaultValue={settings.news_trade_max_age_seconds ?? 2700}
                key={`age-${settings.news_trade_max_age_seconds ?? 2700}`}
                disabled={saving === '新闻时效'}
                onBlur={(event) => {
                  const next = Number(event.target.value)
                  if (!Number.isFinite(next) || next === settings.news_trade_max_age_seconds) return
                  void save({ news_trade_max_age_seconds: next }, '新闻时效')
                }}
                className="w-24 rounded border border-border bg-background px-2 py-1 text-foreground disabled:opacity-50"
              />
              <span className="text-[11px] text-muted-foreground">秒，钳制 30~86400</span>
            </label>
          </section>

          <p className="text-[11px] text-muted-foreground">
            当前状态：{settings.is_running ? 'AUTO PAPER ON' : 'AUTO PAPER OFF'} · 赛道
            {' '}{settings.tracks.length} 个 · 更新于 {settings.updated_at ?? '—'}
          </p>
        </div>
      ) : (
        <p className="mt-5 text-xs text-muted-foreground">读取中…</p>
      )}

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="兜底安全能力"
        hint="灾备降级 / 密钥与权限"
        featureIds={['F63','F64']}
      />
      </section>
    </main>
  )
}
