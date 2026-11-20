'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { CapabilityPanel } from '@/components/quant/capability-panel'
import { ThresholdPanel } from '@/components/quant/threshold-panel'

// v446 开仓闸 — 位置分位 / 同向确认，两道"开仓前"的闸。
// 口径全在后端（entry_guard.py）：位置只用决策前已收盘 K 线，tick 不足不判。
// 这个页面最重要的作用不是展示"闸门多厉害"，而是把**反向**的那条标出来，
// 避免把 11 笔小样本的 r=0.69 当成规律搬上线。

type Snap = { n: number; avg: number | null; win_rate: number | null; sum?: number }
type Gate = {
  blocked: Snap
  kept: Snap
  delta_kept_minus_blocked: number | null
  t_stat: number | null
  verdict: string
}
type Bucket = { bucket: string; n: number; avg: number | null; win_rate: number | null }
type Horizon = {
  qualified: number
  baseline: Snap
  location: Gate
  confirm: Gate
  both: Gate
  buckets: Bucket[]
}
type Rep = {
  entries: number
  with_position: number
  with_confirm: number
  mode: string
  headline: string
  horizons: Record<string, Horizon>
  config: Record<string, number | string>
  note: string
  cached?: boolean
}
type CheckInfo = {
  available: boolean
  asset: string
  action: string
  position: number | null
  pre_confirm: number | null
  confirm_move_pct: number | null
  block: boolean
  block_reasons: string[]
  suggested_entry: number | null
  suggestion_text?: string
  notes: string[]
  mode: string
}

const fmt = (v: number | null | undefined, d = 3) =>
  v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`
const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`

const verdictTone = (v: string) => {
  if (v.includes('反向') || v.includes('不能启用')) return 'text-short'
  if (v.includes('能筛掉')) return 'text-amber-400'
  if (v.includes('不显著')) return 'text-amber-400'
  return 'text-muted-foreground'
}

export default function GuardsPage() {
  const [days, setDays] = useState(60)
  const [rep, setRep] = useState<Rep | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [focus, setFocus] = useState('1h')

  const [asset, setAsset] = useState('BTC')
  const [action, setAction] = useState('SELL')
  const [check, setCheck] = useState<CheckInfo | null>(null)
  const [checkErr, setCheckErr] = useState('')

  const load = useCallback((d: number) => {
    setBusy(true)
    fetch(`${API_BASE}/guards/entry/scan?days=${d}`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) { setRep(j.data); setErr('') } else setErr(j?.error || '读取失败')
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'))
      .finally(() => setBusy(false))
  }, [])

  useEffect(() => { load(days) }, [days, load])

  const runCheck = () => {
    setCheckErr('')
    fetch(`${API_BASE}/guards/entry/check?asset=${encodeURIComponent(asset)}&action=${action}`)
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok) setCheck(j.data)
        else setCheckErr(j?.error || '读数失败')
      })
      .catch((e) => setCheckErr(e instanceof Error ? e.message : '读数失败'))
  }

  const modeTone =
    rep?.mode === 'enforce' ? 'border-long/60 text-long'
      : rep?.mode === 'off' ? 'border-border text-muted-foreground'
        : 'border-amber-400/60 text-amber-400'
  const modeText =
    rep?.mode === 'enforce' ? '生效：会拦单' : rep?.mode === 'off' ? '已关闭' : '影子：只标注，不拦单'

  return (
    <main className="thin-scroll mx-auto w-full max-w-[1200px] flex-1 overflow-y-auto p-4 md:p-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="font-mono text-sm font-semibold tracking-wider">开仓闸 · 位置分位 / 同向确认</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            决策前先看两件事：现价在 20 分钟窗口的什么位置、前 30 分钟盘面是否已同向 · 只读
          </p>
        </div>
        <div className="flex items-center gap-2">
          {rep && <span className={`rounded-sm border px-2 py-1 font-mono text-[11px] ${modeTone}`}>{modeText}</span>}
          <div className="flex gap-1 font-mono text-[11px]">
            {[30, 60, 120].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`rounded-sm border px-2 py-1 ${
                  days === d ? 'border-primary text-primary' : 'border-border text-muted-foreground'
                }`}
              >
                近 {d} 天
              </button>
            ))}
          </div>
        </div>
      </header>

      {err && <div className="surface-card mb-3 border border-red-500/40 p-3 text-xs text-short">{err}</div>}
      {busy && !rep && <div className="surface-card mb-3 p-3 text-xs text-muted-foreground">首次结算要读十几万根 K 线，约 20 秒…</div>}

      {rep && (
        <>
          <div className="surface-card mb-3 border-l-2 border-amber-400/70 p-3 text-sm">
            <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">结论</span>
            <div className="mt-1">{rep.headline}</div>
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              样本 {rep.entries} 笔 · 位置可算 {rep.with_position} · 同向可判 {rep.with_confirm}
              {rep.cached ? ' · 缓存' : ' · 现算'}
            </div>
          </div>

          <div className="surface-card mb-3 overflow-x-auto p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              拦掉 vs 放行（净收益 / 笔，扣 30bps）
            </div>
            <table className="w-full font-mono text-[11px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border/60">
                  {['持有期', '全样本', '位置闸：拦 → 放', '同向闸：拦 → 放', '两道一起', '判定'].map((h, i) => (
                    <th key={h} className={`py-1.5 font-normal ${i === 0 ? 'text-left' : 'text-right'}`}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {['20m', '1h', '4h'].map((h) => {
                  const v = rep.horizons[h]
                  if (!v) return null
                  const cell = (g: Gate) => (
                    <td className="py-1.5 text-right">
                      <span className={verdictTone(g.verdict)}>{fmt(g.blocked.avg)}</span>
                      <span className="text-muted-foreground/60"> → </span>
                      <span className={verdictTone(g.verdict)}>{fmt(g.kept.avg)}</span>
                      <span className="ml-1 text-muted-foreground/60">n={g.blocked.n}/{g.kept.n}</span>
                    </td>
                  )
                  return (
                    <tr key={h} className="border-b border-border/30">
                      <td className="py-1.5 font-semibold">{h}</td>
                      <td className="py-1.5 text-right">
                        <span className={(v.baseline.avg || 0) > 0 ? 'text-long' : 'text-short'}>
                          {fmt(v.baseline.avg)}
                        </span>
                        <span className="ml-1 text-muted-foreground/60">n={v.baseline.n}</span>
                      </td>
                      {cell(v.location)}
                      {cell(v.confirm)}
                      {cell(v.both)}
                      <td className="py-1.5 text-right text-[10px] leading-tight">
                        <div className={verdictTone(v.location.verdict)}>位置：{v.location.verdict}</div>
                        <div className={verdictTone(v.confirm.verdict)}>同向：{v.confirm.verdict}</div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">
              「拦 → 放」= 被闸门拦掉那组的均值 → 放行那组的均值。放行更高才是闸门有用；
              结论按 |t|≥2 才算显著，样本 &lt;{rep.config.min_conclusion_n} 一律不下结论。
            </p>
          </div>

          <div className="surface-card mb-3 p-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                入场位置与结果（0%=窗口最低，100%=最高）
              </div>
              <div className="flex gap-1 font-mono text-[11px]">
                {['20m', '1h', '4h'].map((h) => (
                  <button
                    key={h}
                    onClick={() => setFocus(h)}
                    className={`rounded-sm border px-2 py-0.5 ${
                      focus === h ? 'border-primary text-primary' : 'border-border text-muted-foreground'
                    }`}
                  >
                    {h}
                  </button>
                ))}
              </div>
            </div>
            <table className="w-full font-mono text-[11px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border/60">
                  {['位置分桶', '样本', '净均值/笔', '胜率', '读法'].map((h, i) => (
                    <th key={h} className={`py-1.5 font-normal ${i === 0 || i === 4 ? 'text-left' : 'text-right'}`}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(rep.horizons[focus]?.buckets || []).map((b) => (
                  <tr key={b.bucket} className="border-b border-border/30">
                    <td className="py-1.5 font-semibold">{b.bucket}</td>
                    <td className="py-1.5 text-right">{b.n}</td>
                    <td className={`py-1.5 text-right ${(b.avg || 0) > 0 ? 'text-long' : 'text-short'}`}>
                      {fmt(b.avg)}
                    </td>
                    <td className="py-1.5 text-right">{pct(b.win_rate)}</td>
                    <td className="py-1.5 text-left text-[10px] text-muted-foreground">
                      {b.bucket === '0–20%' ? '窗口底部（SELL 追空区）'
                        : b.bucket === '80–100%' ? '窗口顶部（BUY 追多区）'
                          : '区间中部'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">
              11 笔实盘里「极值入场」贡献 96% 亏损、r=0.69；但放到 2030 笔上这个规律不成立，
              1 小时档甚至是反的。所以这里只把它当读数，不当规则。
            </p>
          </div>

          <div className="surface-card mb-3 p-3">
            <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              单条实时读数
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <label className="text-[11px] text-muted-foreground">
                品种
                <select
                  value={asset}
                  onChange={(e) => setAsset(e.target.value)}
                  className="ml-1 rounded-sm border border-border bg-transparent px-2 py-1 font-mono text-[11px]"
                >
                  {['BTC', 'ETH', 'SOL', 'WTI', 'XAU'].map((a) => <option key={a} value={a}>{a}</option>)}
                </select>
              </label>
              <label className="text-[11px] text-muted-foreground">
                方向
                <select
                  value={action}
                  onChange={(e) => setAction(e.target.value)}
                  className="ml-1 rounded-sm border border-border bg-transparent px-2 py-1 font-mono text-[11px]"
                >
                  {['BUY', 'SELL'].map((a) => <option key={a} value={a}>{a}</option>)}
                </select>
              </label>
              <button
                onClick={runCheck}
                className="rounded-sm border border-primary px-2 py-1 font-mono text-[11px] text-primary"
              >
                按现在读一次
              </button>
            </div>
            {checkErr && <div className="mt-2 text-[11px] text-short">{checkErr}</div>}
            {check && (
              <div className="mt-3 grid grid-cols-2 gap-2 font-mono text-[11px] md:grid-cols-4">
                <div className="rounded-sm border border-border/50 p-2">
                  <div className="text-muted-foreground">位置分位</div>
                  <div className="mt-1 text-sm">{check.position === null ? '—' : `${(check.position * 100).toFixed(0)}%`}</div>
                </div>
                <div className="rounded-sm border border-border/50 p-2">
                  <div className="text-muted-foreground">前 30 分钟</div>
                  <div className="mt-1 text-sm">
                    {check.pre_confirm === null ? '—' : check.pre_confirm ? '已同向' : '未同向'}
                    {check.confirm_move_pct !== null && (
                      <span className="ml-1 text-muted-foreground">{fmt(check.confirm_move_pct, 2)}</span>
                    )}
                  </div>
                </div>
                <div className="rounded-sm border border-border/50 p-2">
                  <div className="text-muted-foreground">按规则</div>
                  <div className={`mt-1 text-sm ${check.block ? 'text-short' : 'text-long'}`}>
                    {check.block ? '该拦' : '可放'}
                  </div>
                </div>
                <div className="rounded-sm border border-border/50 p-2">
                  <div className="text-muted-foreground">回踩参考</div>
                  <div className="mt-1 text-sm">{check.suggested_entry ?? '—'}</div>
                </div>
                {check.block_reasons.length > 0 && (
                  <div className="col-span-2 rounded-sm border border-amber-400/30 p-2 md:col-span-4">
                    <span className="text-muted-foreground">理由：</span>
                    {check.block_reasons.join('；')}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="surface-card p-3 text-[11px] leading-relaxed text-muted-foreground">
            <p>{rep.note}</p>
            <p className="mt-2">
              当前是 <span className="text-amber-400">{rep.mode}</span> 模式：只把读数写进决策结果，
              <span className="text-foreground">不会拦任何单</span>。要真拦单需设环境变量
              <code className="mx-1 rounded-sm border border-border px-1">ENTRY_GUARD_MODE=enforce</code>
              并重启引擎；按当前证据，<span className="text-short">位置闸不要开</span>，
              同向闸只在 1 小时以上持有期才有正贡献（放行组仍为负，不构成盈利方案）。
            </p>
          </div>
        </>
      )}

      {/* v476 · 生效门槛：v475 只在返回值里给的门槛，现在落库可查 */}
      <ThresholdPanel />

      <section className="mt-4">
      {/* v476 · 本页归属的 Trident-2 能力（只读/纸面，口径见 /api/trident2/features） */}
      <CapabilityPanel
        title="风控与执行能力"
        hint="盘前体检 / 止血剧本 / 组合压力测试 / 保护单 / 事件降仓 / 执行质量 / 流动性 / 杠杆三连 / 清算热力图"
        featureIds={['F03','F05','F07','F08','F09','F10','F25','F27','F38']}
      />
      </section>
    </main>
  )
}
