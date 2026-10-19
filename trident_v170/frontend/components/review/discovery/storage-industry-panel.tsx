'use client'

import { useCallback, useEffect, useState } from 'react'
import { Cpu, Database, Factory, Plus, Trash2 } from 'lucide-react'
import { PushToCausalChainButton } from './push-to-causal-chain-button'
import { cn } from '@/lib/utils'

/* ════════════════════════════════════════════════════════════════════════
 * 💾 存储行业(海力士专用 · 周期套利核心 · 慢变量)
 * (v170+ P2 · 第 4 项)
 *
 * 设计动机:用户原话——
 *   「四、存储行业(海力士专用,周期套利核心,慢变量)
 *    - DRAM/NAND 现货价格(DRAMeXchange / 集邦 TrendForce)
 *    - 原厂合约价(每月更新一次,厂商月底谈下一个月合约)
 *    - 库存周转天数、三大厂资本开支(CapEx)
 *    - ⚠ 重点:行业供需是慢变量,按月/季度才会出现拐点;
 *      美债、美元是日内高频,只会制造短期震荡,不会改变产业周期底层趋势。
 *      这就是区分「短期回调」和「周期反转」的关键。」
 *
 * 实现要点:
 *  ① 后端现状: DRAMeXchange / TrendForce 是付费会员源,本面板不直连。
 *     采用「本地占位 + localStorage 手动录入」的过渡方案,
 *     等后端接 ingest API(计划: storage_feed.py 走官方公开摘要)即可切换。
 *  ② 数据节奏区分: 现货 = 日更(填最近一次报价), 合约价 = 月更,
 *     库存 = 月更, CapEx = 季更。面板顶部明确标示。
 *  ③ 慢变量警示: 顶部固定 banner 强调「慢变量 = 月/季度才有拐点」,
 *     防止被日内美债/美元噪声误读为产业拐点。
 *  ④ 三厂对比: 海力士 / 三星 / 美光 CapEx 行对比,直观看出「谁在加产能」。
 * ════════════════════════════════════════════════════════════════════════ */

type StorageEntry = {
  id: string
  date: string
  category: 'spot_dram' | 'spot_nand' | 'contract_dram' | 'contract_nand' | 'inventory_days' | 'capex_samsung' | 'capex_skhynix' | 'capex_micron'
  value: number
  note?: string
}

const CATEGORY_META: Record<StorageEntry['category'], { label: string; unit: string; color: string; rhythm: string }> = {
  spot_dram:       { label: 'DRAM 现货',     unit: 'USD/颗 (DDR4 1Gx8)',          color: 'text-cyan-600',     rhythm: '日更' },
  spot_nand:       { label: 'NAND 现货',     unit: 'USD/颗 (256Gb TLC)',          color: 'text-violet-600',   rhythm: '日更' },
  contract_dram:   { label: 'DRAM 合约价',   unit: 'USD/颗 (DDR4 1Gx8 月均)',      color: 'text-cyan-700',     rhythm: '月更(月底)' },
  contract_nand:   { label: 'NAND 合约价',   unit: 'USD/颗 (256Gb TLC 月均)',      color: 'text-violet-700',   rhythm: '月更(月底)' },
  inventory_days:  { label: '海力士库存天数', unit: '天(DRAM+NAND)',                color: 'text-amber-600',    rhythm: '月更' },
  capex_samsung:   { label: '三星 CapEx',    unit: '万亿韩元(年化)',                color: 'text-blue-600',     rhythm: '季更' },
  capex_skhynix:   { label: '海力士 CapEx',  unit: '万亿韩元(年化)',                color: 'text-emerald-600',  rhythm: '季更' },
  capex_micron:    { label: '美光 CapEx',    unit: '十亿美元(年化)',                color: 'text-orange-600',   rhythm: '季更' },
}

const LS_KEY = 'v170_storage_panel_v1'

function load(): StorageEntry[] {
  if (typeof window === 'undefined') return []
  try {
    const s = window.localStorage.getItem(LS_KEY)
    if (!s) return []
    return JSON.parse(s) as StorageEntry[]
  } catch { return [] }
}
function save(rows: StorageEntry[]) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(LS_KEY, JSON.stringify(rows))
}

export function StorageIndustryPanel() {
  const [rows, setRows] = useState<StorageEntry[]>([])
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<Partial<StorageEntry>>({
    date: new Date().toISOString().slice(0, 10),
    category: 'spot_dram',
    value: undefined,
    note: '',
  })

  useEffect(() => { setRows(load()) }, [])
  const persist = useCallback((next: StorageEntry[]) => {
    setRows(next); save(next)
  }, [])

  const submit = () => {
    if (!draft.date || !draft.category || draft.value == null) return
    const e: StorageEntry = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      date: String(draft.date),
      category: draft.category as StorageEntry['category'],
      value: Number(draft.value),
      note: draft.note || '',
    }
    persist([e, ...rows].slice(0, 200))
    setAdding(false)
    setDraft({ date: new Date().toISOString().slice(0, 10), category: 'spot_dram', value: undefined, note: '' })
  }
  const del = (id: string) => persist(rows.filter(r => r.id !== id))

  // 按 category 聚合「最近一次」值
  const latestByCat: Record<string, StorageEntry | null> = {}
  for (const r of rows) {
    if (!latestByCat[r.category] || latestByCat[r.category]!.date < r.date) {
      latestByCat[r.category] = r
    }
  }

  return (
    <div className="rounded border border-emerald-500/40 bg-emerald-500/5 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs">
        <div className="flex items-center gap-2">
          <Database className="h-4 w-4 text-emerald-500" aria-hidden />
          <span className="font-bold uppercase tracking-widest text-emerald-700 dark:text-emerald-300">
            💾 存储行业 · 海力士专用 · 慢变量
          </span>
          <span className="rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[9px] font-bold text-emerald-600 dark:text-emerald-400">
            现货日更 · 合约月更 · 库存月更 · CapEx 季更
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setAdding(v => !v)}
            className="inline-flex items-center gap-0.5 rounded border border-border bg-background/60 px-1.5 py-0.5 text-[10px] hover:bg-secondary/50"
          >
            <Plus className="h-3 w-3" />
            <span>录入</span>
          </button>
        </div>
      </div>

      {/* 慢变量警示 */}
      <div className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 p-1.5 font-mono text-[10px] text-amber-700 dark:text-amber-300">
        ⚠ <strong>慢变量警示:</strong>
        DRAM/NAND 现货合约价的<strong>拐点按月/季度出现</strong>;美债、美元、DXY 等日内高频信号
        只会制造短期震荡,不会改变产业周期底层趋势 — 这就是区分「短期回调」与「周期反转」的关键。
      </div>

      {/* 录入表单 */}
      {adding && (
        <div className="mt-2 grid grid-cols-1 gap-2 rounded border border-border bg-background/40 p-2 font-mono text-[10px] md:grid-cols-5">
          <label className="flex flex-col gap-0.5">
            <span className="text-muted-foreground">日期</span>
            <input type="date" value={draft.date || ''}
              onChange={e => setDraft({ ...draft, date: e.target.value })}
              className="rounded border border-border bg-background px-1.5 py-0.5" />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-muted-foreground">类别</span>
            <select value={draft.category}
              onChange={e => setDraft({ ...draft, category: e.target.value as StorageEntry['category'] })}
              className="rounded border border-border bg-background px-1.5 py-0.5">
              {Object.entries(CATEGORY_META).map(([k, m]) => (
                <option key={k} value={k}>{m.label}({m.rhythm})</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-muted-foreground">数值</span>
            <input type="number" step="0.01" value={draft.value ?? ''}
              onChange={e => setDraft({ ...draft, value: e.target.value === '' ? undefined : Number(e.target.value) })}
              className="rounded border border-border bg-background px-1.5 py-0.5 tabular-nums" />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-muted-foreground">备注/来源</span>
            <input type="text" value={draft.note || ''}
              onChange={e => setDraft({ ...draft, note: e.target.value })}
              className="rounded border border-border bg-background px-1.5 py-0.5"
              placeholder="如:TrendForce 月报" />
          </label>
          <div className="flex items-end gap-1.5">
            <button type="button" onClick={submit}
              className="rounded border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-emerald-600 hover:bg-emerald-500/20 dark:text-emerald-300">
              提交
            </button>
            <button type="button" onClick={() => setAdding(false)}
              className="rounded border border-border px-2 py-0.5 hover:bg-secondary/50">
              取消
            </button>
          </div>
        </div>
      )}

      {/* 8 张关键指标卡 */}
      <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-4">
        {Object.entries(CATEGORY_META).map(([key, meta]) => {
          const e = latestByCat[key]
          return (
            <div key={key} className="rounded border border-border bg-background/40 p-2 font-mono">
              <div className="flex items-center gap-1 text-[10px]">
                {key.startsWith('capex') ? <Factory className="h-3 w-3" /> : key.startsWith('inventory') ? <Database className="h-3 w-3" /> : <Cpu className="h-3 w-3" />}
                <span className={cn('font-bold', meta.color)}>{meta.label}</span>
                <span className="ml-auto rounded border border-border bg-secondary/30 px-1 py-0.5 text-[9px] text-muted-foreground">{meta.rhythm}</span>
              </div>
              <div className="mt-1 flex items-baseline gap-1.5">
                <span className={cn('tabular-nums text-lg font-bold', meta.color)}>
                  {e ? e.value.toFixed(meta.unit.startsWith('USD') ? 3 : 1) : '—'}
                </span>
                <span className="text-[9px] text-muted-foreground">{meta.unit}</span>
              </div>
              <div className="mt-0.5 text-[9px] text-muted-foreground/80">
                {e ? `${e.date}${e.note ? ' · ' + e.note : ''}` : '尚无录入'}
              </div>
            </div>
          )
        })}
      </div>

      {/* 录入历史 */}
      {rows.length > 0 && (
        <details className="mt-2 rounded border border-border bg-background/40 p-2 font-mono text-[10px]">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            录入历史 · {rows.length} 条(最近 5 条)
          </summary>
          <div className="mt-2 space-y-1">
            {rows.slice(0, 5).map(r => {
              const m = CATEGORY_META[r.category]
              return (
                <div key={r.id} className="flex items-center gap-2">
                  <span className={cn('font-semibold', m.color)}>{m.label}</span>
                  <span className="text-muted-foreground">{r.date}</span>
                  <span className="tabular-nums">{r.value.toFixed(m.unit.startsWith('USD') ? 3 : 1)}{m.unit}</span>
                  {r.note && <span className="truncate text-muted-foreground/80">· {r.note}</span>}
                  {/* v283+ · #23/#31 · 慢变量(海力士 CapEx / 合约价)→ 周期信号,投到因果链 */}
                  <PushToCausalChainButton origin="manual_from_storage_panel"

                    cause={`storage · ${m.label} ${r.value.toFixed(m.unit.startsWith('USD') ? 3 : 1)}${m.unit}`}
                    effect={m.label.includes('海力士') ? '海力士(SKHynix)' : m.label.includes('三星') ? '三星电子' : m.label.includes('美光') ? '美光(Micron)' : '存储行业整体'}
                    note={`date=${r.date} rhythm=${m.rhythm} note=${r.note ?? ''}`}
                    trigger_words={[m.label, m.rhythm, 'storage'].join(',')}
                    weight={0.5}
                  />
                  <button type="button" onClick={() => del(r.id)}
                    className="text-rose-500/60 hover:text-rose-500" title="删除">
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              )
            })}
          </div>
        </details>
      )}

      <div className="mt-2 rounded border border-emerald-500/20 bg-emerald-500/5 p-1.5 text-[10px] text-muted-foreground">
        💡 <strong className="text-foreground">数据源建议:</strong>
        现货价看{' '}
        <a className="text-cyan-600 hover:underline dark:text-cyan-400" href="https://www.trendforce.com/" target="_blank" rel="noreferrer">TrendForce 集邦</a> /
        <a className="ml-1 text-cyan-600 hover:underline dark:text-cyan-400" href="https://www.dramexchange.com/" target="_blank" rel="noreferrer">DRAMeXchange</a>(付费会员);
        合约价看 <strong className="text-foreground">厂商每月底公布的次月合约</strong>;
        CapEx 看 <strong className="text-foreground">三星/海力士/美光季报</strong> 后的资本开支调整 —
        等后端 storage_feed.py 接入公开摘要后,本面板自动切换为拉取模式,无需手动录入。
      </div>
    </div>
  )
}