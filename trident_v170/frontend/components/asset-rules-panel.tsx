'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { useToast } from '@/components/ui/toast'

// 品种专项分析规则面板 — 按金融品类（黄金/原油/比特币/美股代码…）单独设置分析规则。
// AI 分析时按 target_asset / sibling 品种套用对应规则；asset='all' 为全品种兜底。
// 启用的规则由引擎注入每次分析的 prompt（≤5s 生效，免重启）。

type AssetRule = {
  id: number
  asset: string
  asset_name: string
  content: string
  enabled: number
  priority: number
  display?: string
  default_display?: string
  created_at: string
  updated_at: string
}

type PromptsPayload = {
  asset_rules?: AssetRule[]
  assets?: Record<string, string>
}

// 兜底建议清单（后端 assets 映射不可用时的候选项）。
// 注意：这只是"建议"，不是白名单——品种输入框可自由填任意代码（COPPER/NATGAS/JP225…），
// 后端会统一大写存储，未收录的品种按代码本身显示。
const FALLBACK_ASSETS: Record<string, string> = {
  all: '全品种', XAU: '黄金', WTI: '原油', BTC: '比特币', ETH: '以太坊', SOL: 'Solana',
  NVDA: '英伟达', TSLA: '特斯拉', AAPL: '苹果', MSTR: '微策略',
  MSFT: '微软', AMZN: '亚马逊', GOOGL: '谷歌', COIN: 'Coinbase',
  PLTR: 'Palantir', META: 'Meta', SMCI: '美超微', MARA: 'Marathon', RIOT: 'Riot',
}

// 常用品种（首屏只露这几个，避免下拉被 19 项淹没）；其余走"显示全部建议"
const COMMON_ASSETS = ['all', 'XAU', 'WTI', 'BTC', 'ETH']

// 品种代码归一，必须与后端 ai_prompts._norm_asset 保持一致：
// all 保持小写（后端 ASSET_DISPLAY 键为小写 all），其余统一大写；中文别名也认。
function assetKeyOf(code: string): string {
  const t = (code || '').trim()
  const low = t.toLowerCase()
  if (low === 'all' || t === '全部' || t === '全品种' || t === '所有品种') return 'all'
  return t.toUpperCase()
}

function lookupAssetName(code: string, map: Record<string, string>): string {
  const k = assetKeyOf(code)
  // 未知品种返回空串：后端会按代码本身显示，不强行造名字
  return map[k] ?? map[code.trim()] ?? ''
}

export function AssetRulesPanel() {
  // v302+ P0: 替代散落的 window.confirm
  const { toast } = useToast()
  const [rules, setRules] = useState<AssetRule[]>([])
  const [assets, setAssets] = useState<Record<string, string>>(FALLBACK_ASSETS)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [newAsset, setNewAsset] = useState('XAU')
  const [showAllAssets, setShowAllAssets] = useState(false)
  const [newContent, setNewContent] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [draft, setDraft] = useState<Record<number, { content: string; name: string }>>({})
  const [savingId, setSavingId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch(`${API_BASE}/ai/prompts`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`list ${r.status}`)
      const data = (await r.json()) as PromptsPayload
      setRules(data.asset_rules ?? [])
      if (data.assets && Object.keys(data.assets).length > 0) setAssets(data.assets)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    // 10s 轮询：后台/多端变更后页面自动同步
    const timer = window.setInterval(() => void load(), 10_000)
    return () => window.clearInterval(timer)
  }, [load])

  // 新增：品种代码与显示名都用同一套归一（all 保持小写），避免显示名丢失
  const add = async () => {
    const content = newContent.trim()
    if (!content || !newAsset.trim()) return
    setSubmitting(true)
    setError(null)
    try {
      const code = assetKeyOf(newAsset)
      const r = await fetch(`${API_BASE}/ai/asset-rules`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          asset: code,
          asset_name: lookupAssetName(newAsset, assets),
          content,
        }),
      })
      if (!r.ok) throw new Error(await r.text())
      setNewContent('')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '新增失败')
    } finally {
      setSubmitting(false)
    }
  }

  const save = async (rule: AssetRule) => {
    const d = draft[rule.id]
    const content = (d?.content ?? rule.content).trim()
    const assetName = (d?.name ?? rule.asset_name ?? '').trim()
    if (!content) return
    setSavingId(rule.id)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/ai/asset-rules/${rule.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, asset_name: assetName, priority: rule.priority }),
      })
      if (!r.ok) throw new Error(await r.text())
      setDraft((prev) => {
        const n = { ...prev }
        delete n[rule.id]
        return n
      })
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败')
    } finally {
      setSavingId(null)
    }
  }

  const toggle = async (rule: AssetRule) => {
    try {
      const r = await fetch(`${API_BASE}/ai/asset-rules/${rule.id}/toggle`, { method: 'PATCH' })
      if (!r.ok) throw new Error(await r.text())
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '切换失败')
    }
  }

  const remove = async (rule: AssetRule) => {
    const label = rule.display || rule.asset
    // v302+ P0: 用 toast.confirm 替代 window.confirm
    const ok = await new Promise<boolean>((resolve) => {
      toast.confirm(`删除「${label}」的专项规则？删除后 AI 不再参考该品种规则。`, () => resolve(true), () => resolve(false))
    })
    if (!ok) return
    try {
      const r = await fetch(`${API_BASE}/ai/asset-rules/${rule.id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(await r.text())
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除失败')
    }
  }

  const enabledCount = rules.filter((r) => r.enabled).length

  return (
    <section className="surface-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">
          品种专项分析规则
        </h2>
        <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">
          PER-ASSET
        </span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {loading && rules.length === 0 ? '加载中…' : `${enabledCount}/${rules.length} 生效 · 改动 ≤5s 生效`}
        </span>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        为不同金融品种单独设定分析规则——AI 分析时先识别新闻主体与主品种，
        再<b className="text-foreground">按品种套用对应规则</b>（主品种用主品种规则、每个副品种用各自规则）。
        规则会注入每次 AI 分析的 prompt；<b className="text-foreground">all</b> 作为全品种兜底规则。
        品种可以<b className="text-foreground">自由输入</b>（COPPER/NATGAS/JP225…都能填，输入框有常用建议）；
        卡片左侧小框是<b className="text-foreground">显示名</b>（可随时改，留空则用内置名）。
      </p>

      {/* 规则格式模板：照这个四段式写，条件可判定，模型才能稳定执行 */}
      <div className="mt-2 rounded border border-border bg-secondary/30 px-2 py-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[10px] font-bold text-foreground">规则写法模板</span>
          <span className="font-mono text-[9px] text-muted-foreground">
            按下面四段写，条件写具体（见文末对比）
          </span>
        </div>
        <ul className="mt-1 space-y-0.5 font-mono text-[10px] leading-relaxed text-muted-foreground">
          <li>
            <b className="text-foreground">【核心驱动】</b>该品种真正看什么（列 1-3 个因子，别贪多）
          </li>
          <li>
            <b className="text-foreground">【方向偏好】</b>什么条件 → 什么方向（用 · 分条，条件写具体）
          </li>
          <li>
            <b className="text-foreground">【噪音过滤】</b>哪些情况不提分、不给方向（可选，但很有用）
          </li>
          <li>
            <b className="text-foreground">【禁止】</b>最容易被模型搞错的事（防张冠李戴）
          </li>
        </ul>
        <div className="mt-1.5 border-t border-border pt-1.5">
          <div className="font-mono text-[10px] text-muted-foreground">
            <b className="text-foreground">关键技巧：条件要可判定</b>。对比一下：
          </div>
          <div className="mt-0.5 space-y-0.5 font-mono text-[10px] leading-relaxed">
            <div className="text-short">
              ❌ &quot;黄金对利率敏感，注意风险&quot; —— 模型无法执行，等于没说
            </div>
            <div className="text-long">
              ✅ &quot;TIPS 上行 + 无地缘升级 → 优先 SELL 或 HOLD，不追多&quot; —— 条件明确、动作明确
            </div>
          </div>
        </div>
      </div>

      {/* 新增 */}
      <div className="mt-2 flex flex-wrap items-start gap-2">
        <input
          value={newAsset}
          onChange={(e) => setNewAsset(e.target.value)}
          list="asset-rule-suggestions"
          placeholder="品种代码"
          aria-label="品种"
          className="w-32 rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        />
        <datalist id="asset-rule-suggestions">
          {Object.entries(assets).map(([code, name]) => (
            <option key={code} value={code}>
              {name}
            </option>
          ))}
        </datalist>
        <span className="self-center font-mono text-[10px] text-muted-foreground">
          {lookupAssetName(newAsset, assets) || (newAsset.trim() ? '自定义' : '选或填')}
        </span>
        <button
          type="button"
          onClick={() => setShowAllAssets((v) => !v)}
          className="self-center rounded border border-border px-1.5 font-mono text-[9px] text-muted-foreground hover:bg-secondary"
        >
          {showAllAssets ? '只看常用' : '全部建议'}
        </button>
        <div className="flex w-full flex-wrap gap-1 pt-0.5">
          {(showAllAssets ? Object.entries(assets) : COMMON_ASSETS.map((c) => [c, assets[c] ?? c])).map(
            ([code, name]) => (
              <button
                key={code}
                type="button"
                onClick={() => setNewAsset(code)}
                title={name}
                className={`rounded border px-1.5 py-px font-mono text-[9px] ${
                  assetKeyOf(newAsset) === code
                    ? 'border-primary/60 bg-primary/15 text-primary'
                    : 'border-border text-muted-foreground hover:bg-secondary'
                }`}
              >
                {code}
              </button>
            ),
          )}
        </div>
        <textarea
          value={newContent}
          onChange={(e) => setNewContent(e.target.value)}
          placeholder="输入该品种的分析规则（如：黄金对实际利率敏感，TIPS 上行时优先考虑做空；地缘避险买盘确认后才追多）"
          rows={2}
          className="thin-scroll min-w-[280px] flex-1 resize-y rounded border border-border bg-card px-2 py-1.5 font-mono text-xs leading-relaxed text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        />
        <button
          type="button"
          onClick={() => void add()}
          disabled={submitting || !newContent.trim()}
          className="rounded border border-primary/50 bg-primary/10 px-3 py-1.5 font-mono text-xs text-primary disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? '添加中…' : '添加规则'}
        </button>
      </div>

      {error && (
        <div className="mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 text-[11px] text-short">{error}</div>
      )}

      {/* 规则列表 */}
      <div className="mt-2 space-y-1.5">
        {rules.length === 0 && !loading && (
          <div className="rounded border border-border bg-card px-2 py-2 font-mono text-[11px] text-muted-foreground">
            暂无品种规则——AI 按内置规则分析。添加后立即生效。
          </div>
        )}
        {rules.map((rule) => {
          const d = draft[rule.id]
          const curName = d?.name ?? rule.asset_name ?? ''
          const dirty =
            (d !== undefined && (d.content !== rule.content || d.name !== (rule.asset_name ?? '')))
          return (
            <div
              key={rule.id}
              className={`rounded border px-2 py-1.5 ${
                rule.enabled ? 'border-primary/30 bg-primary/5' : 'border-border bg-secondary/30 opacity-70'
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-secondary px-1.5 py-px font-mono text-[10px] font-bold text-foreground">
                  {rule.asset}
                </span>
                <input
                  value={curName}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      [rule.id]: { content: prev[rule.id]?.content ?? rule.content, name: e.target.value },
                    }))
                  }
                  placeholder={rule.default_display ?? rule.asset}
                  aria-label={`${rule.asset} 显示名`}
                  className="w-32 rounded border border-border bg-card px-1.5 py-px font-mono text-[10px] text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
                />
                <span className={`font-mono text-[9px] ${rule.enabled ? 'text-long' : 'text-muted-foreground'}`}>
                  {rule.enabled ? '● 生效中' : '○ 已停用'}
                </span>
                <div className="ml-auto flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => void save(rule)}
                    disabled={!dirty || savingId === rule.id}
                    title={dirty ? '保存修改' : '没有改动'}
                    className="rounded border border-primary/50 bg-primary/10 px-1.5 font-mono text-[9px] text-primary disabled:cursor-not-allowed disabled:border-border disabled:bg-transparent disabled:text-muted-foreground"
                  >
                    {savingId === rule.id ? '保存中…' : dirty ? '保存 *' : '已保存'}
                  </button>
                  {dirty && (
                    <button
                      type="button"
                      onClick={() =>
                        setDraft((prev) => {
                          const n = { ...prev }
                          delete n[rule.id]
                          return n
                        })
                      }
                      className="rounded border border-border px-1.5 font-mono text-[9px] text-muted-foreground hover:bg-secondary"
                    >
                      撤销
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => void toggle(rule)}
                    className="rounded border border-border px-1.5 font-mono text-[9px] text-muted-foreground hover:bg-secondary"
                  >
                    {rule.enabled ? '停用' : '启用'}
                  </button>
                  <button
                    type="button"
                    onClick={() => void remove(rule)}
                    className="rounded border border-short/40 px-1.5 font-mono text-[9px] text-short hover:bg-short/10"
                  >
                    删除
                  </button>
                </div>
              </div>
              <textarea
                value={d?.content ?? rule.content}
                onChange={(e) =>
                  setDraft((prev) => ({
                    ...prev,
                    [rule.id]: { content: e.target.value, name: prev[rule.id]?.name ?? rule.asset_name ?? '' },
                  }))
                }
                rows={2}
                className="thin-scroll mt-1 w-full resize-y rounded border border-border bg-card px-2 py-1 font-mono text-[11px] leading-relaxed text-foreground focus:border-primary focus:outline-none"
              />
            </div>
          )
        })}
      </div>
    </section>
  )
}
