'use client'

import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '@/lib/api'
import { useToast } from '@/components/ui/toast'

// AI 分析 Prompt 面板：
// ① 查看/编辑当前 AI 分析实际使用的系统 prompt（页面编辑覆盖代码默认，恢复默认=回到内置）
// ② 管理用户自定义分析指令（启用的注入每次 AI 分析的 system prompt，≤5s 生效）

type MainPromptItem = { content: string; edited: boolean; default: string }
type MainPrompts = {
  system_prompt: MainPromptItem
  json_force_prompt: MainPromptItem
  market_structure_block: MainPromptItem
  doubao_prompt: MainPromptItem
}

type CustomPrompt = {
  id: number
  name: string
  content: string
  enabled: number
  created_at: string
  updated_at: string
}

type UsageModel = { id: string; label: string; weight: number }
type PromptUsage = {
  system_prompt: UsageModel[]
  json_force_prompt: UsageModel[]
  doubao_prompt: UsageModel[]
  market_structure_enabled: boolean
}

type PromptData = {
  main: MainPrompts
  usage: PromptUsage
  custom: CustomPrompt[]
}

// 最终装配预览：主 Prompt + 运行时分区（品种规则 / 因果链 / 复盘经验…）
type PreviewSegment = {
  key: string
  title: string
  text: string
  injected: boolean
  editable: boolean
}
type PromptPreview = {
  mode: string
  prompt: string
  length: number
  segments: PreviewSegment[]
  /** 补充说明：如因果链段按新闻命中、每次不同，故不在预览中展示 */
  note?: string
}

type MainKind = keyof MainPrompts

const MAIN_ITEMS: Array<{ key: MainKind; label: string; desc: string; usageKey?: keyof Pick<PromptUsage, 'system_prompt' | 'json_force_prompt' | 'doubao_prompt'>; isBlock?: boolean }> = [
  { key: 'system_prompt', label: 'System Prompt（八段式主 prompt · 唯一通道）', desc: '所有模型统一使用（已关闭 Prompt Force 双通道叠加）', usageKey: 'system_prompt' },
  { key: 'market_structure_block', label: '盘面结构指令块（开关附加）', desc: '盘面结构分析开关开启时，附加到上面生效的主 prompt 的水位博弈段', isBlock: true },
  { key: 'doubao_prompt', label: 'Doubao 快速模型 prompt', desc: 'Doubao 快速直觉模型专用', usageKey: 'doubao_prompt' },
]

export function AiPromptsPanel() {
  // v302+ P0: 替代散落的 window.confirm
  const { toast } = useToast()
  const [data, setData] = useState<PromptData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showMain, setShowMain] = useState(true)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [editing, setEditing] = useState<Record<string, boolean>>({})
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [content, setContent] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // 最终装配预览（主 Prompt + 运行时分区）
  const [preview, setPreview] = useState<PromptPreview | null>(null)
  const [previewMode, setPreviewMode] = useState<'force' | 'system'>('system')
  const [previewOpen, setPreviewOpen] = useState<Record<string, boolean>>({})

  const loadPreview = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/ai/prompts/preview?mode=${previewMode}`, { cache: 'no-store' })
      if (r.ok) setPreview((await r.json()) as PromptPreview)
    } catch {
      // 静默：预览不可用不影响主 prompt 编辑
    }
  }, [previewMode])

  useEffect(() => {
    void loadPreview()
  }, [loadPreview])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch(`${API_BASE}/ai/prompts`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`list ${r.status}`)
      setData((await r.json()) as PromptData)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // 10s 轮询：引擎/多端变更后自动同步（含装配预览 —— 提示库页启用经验后同步可见）
  useEffect(() => {
    const timer = window.setInterval(() => {
      void load()
      void loadPreview()
    }, 10_000)
    return () => window.clearInterval(timer)
  }, [load, loadPreview])

  const toggleMain = () => setShowMain((v) => !v)

  const startEdit = (key: MainKind) => {
    const item = data?.main[key]
    if (!item) return
    setDraft((d) => ({ ...d, [key]: item.content }))
    setEditing((e) => ({ ...e, [key]: true }))
    setExpanded((x) => ({ ...x, [key]: true }))
  }

  const cancelEdit = (key: MainKind) => {
    setEditing((e) => ({ ...e, [key]: false }))
    setDraft((d) => ({ ...d, [key]: '' }))
  }

  const saveMain = async (key: MainKind) => {
    const c = (draft[key] ?? '').trim()
    if (!c) return
    setSaving(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/ai/prompts/main/${key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: c }),
      })
      if (!r.ok) throw new Error(await r.text())
      setEditing((e) => ({ ...e, [key]: false }))
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const resetMain = async (key: MainKind) => {
    // v302+ P0: 用 toast.confirm 替代 window.confirm
    const ok = await new Promise<boolean>((resolve) => {
      toast.confirm(
        `恢复「${MAIN_ITEMS.find((m) => m.key === key)?.label}」为代码内置默认？你的修改将被丢弃。`,
        () => resolve(true),
        () => resolve(false),
      )
    })
    if (!ok) return
    setSaving(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/ai/prompts/main/${key}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(await r.text())
      setEditing((e) => ({ ...e, [key]: false }))
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '恢复失败')
    } finally {
      setSaving(false)
    }
  }

  const add = async () => {
    const c = content.trim()
    if (!c) return
    setSubmitting(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/ai/prompts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), content: c }),
      })
      if (!r.ok) throw new Error(await r.text())
      setName('')
      setContent('')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '添加失败')
    } finally {
      setSubmitting(false)
    }
  }

  const toggleCustom = async (item: CustomPrompt) => {
    try {
      const r = await fetch(`${API_BASE}/ai/prompts/${item.id}/toggle`, { method: 'PATCH' })
      if (!r.ok) throw new Error(await r.text())
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '启停失败')
    }
  }

  const remove = async (item: CustomPrompt) => {
    // v302+ P0: 用 toast.confirm 替代 window.confirm
    const ok = await new Promise<boolean>((resolve) => {
      toast.confirm(
        `删除自定义指令「${item.name || item.content.slice(0, 20)}」？删除后立即不再注入。`,
        () => resolve(true),
        () => resolve(false),
      )
    })
    if (!ok) return
    try {
      const r = await fetch(`${API_BASE}/ai/prompts/${item.id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(await r.text())
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除失败')
    }
  }

  const enabledCount = (data?.custom ?? []).filter((c) => c.enabled).length
  const editedCount = data ? MAIN_ITEMS.filter((m) => data.main[m.key]?.edited).length : 0

  return (
    <section className="surface-card p-3">
      <div className="flex items-center gap-2">
        <h2 className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">
          AI 分析 Prompt
        </h2>
        <span className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">
          LLM PROMPT
        </span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {loading ? '加载中…' : data ? (
            <>
              {editedCount > 0 && <span className="text-primary">{editedCount} 份已修改 · </span>}
              自定义指令 {enabledCount}/{data.custom.length} 生效
            </>
          ) : ''}
        </span>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        查看/编辑 AI 分析<b className="text-foreground">实际发给模型</b>的系统提示词（六段框架，改完保存立即持久化，≤5s 生效）；
        下方管理<b className="text-foreground">自定义分析指令</b>——启用的指令拼进每次分析的 system prompt，
        用于不改代码就追加约束（例：&quot;遇到降息新闻优先分析黄金 XAU，再看加密&quot;）。过滤关键词（页面下方 L1）与这里是两回事。
      </p>

      {error && (
        <div className="mt-2 rounded border border-short/40 bg-short/10 px-2 py-1 text-[11px] text-short">{error}</div>
      )}

      {/* ── ① 主 Prompt（可编辑） ── */}
      <div className="mt-3 rounded-md border border-border bg-card p-2.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">主 Prompt（编辑=替换分析框架）</span>
          <button
            type="button"
            onClick={toggleMain}
            className="ml-auto rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground"
          >
            {showMain ? '收起 ▾' : '展开 ▸'}
          </button>
        </div>
        <div className="mt-1 rounded border border-hold/20 bg-hold/5 p-1.5 text-[10px] leading-relaxed text-muted-foreground">
          <b className="text-hold">⚠ 区分说明：</b>已<b className="text-foreground">统一单通道</b>——所有模型都走 System Prompt（完整八段式），
          Prompt Force 通道已关闭（不再叠加）、避免无关内容干扰八段式框架；盘面结构块是"开关附加"，Doubao 是另一个快速模型专用。看每条徽章：
          <span className="text-long">绿"生效中"</span>= 你当前模型池真有模型在用（你这边是 <b className="text-foreground">System Prompt</b> 和盘面结构块生效）；
          <span className="text-muted-foreground">灰"未生效"</span>= 池内没有走这份的模型，改了也不会影响当前分析（除非以后往池里加对应通道模型）。
          编辑这里 = <b className="text-short">替换整份框架</b>（影响大，改前复制原文备份）。
        </div>
        {showMain && data && (
          <div className="mt-2 space-y-2">
            {MAIN_ITEMS.map((item) => {
              const m = data.main[item.key]
              const text = m?.content ?? ''
              const open = expanded[item.key]
              const isEditing = editing[item.key]
              // 生效状态：盘面结构块看开关；其余看模型池是否使用该类
              let statusText = ''
              let statusOn = false
              let statusModels: string[] = []
              if (item.isBlock) {
                statusOn = !!data.usage?.market_structure_enabled
                statusText = statusOn ? '生效中 · 开关开启（附加到主 prompt）' : '未生效 · 盘面结构开关已关闭'
              } else if (item.usageKey) {
                const list = data.usage?.[item.usageKey] ?? []
                statusOn = list.length > 0
                if (statusOn) {
                  const total = list.reduce((a, x) => a + (x.weight || 1), 0)
                  statusText = `生效中 · 模型池 ${total} 个槽位使用`
                  statusModels = list.map((x) => x.label)
                } else {
                  statusText = '未生效 · 当前模型池无此类通道的模型'
                }
              }
              return (
                <div key={item.key} className="rounded border border-border bg-secondary/20 p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[10px] font-bold text-foreground">{item.label}</span>
                    {statusText && (
                      <span className={`rounded border px-1.5 py-px font-mono text-[9px] font-bold ${
                        statusOn
                          ? 'border-long/70 bg-long/25 text-long'
                          : 'border-border bg-secondary text-muted-foreground'
                      }`}>
                        {statusOn ? '✓ ' : '✗ '}{statusText}
                      </span>
                    )}
                    {m?.edited ? (
                      <span className="rounded border border-primary/40 bg-primary/10 px-1 py-px font-mono text-[8px] text-primary">已修改</span>
                    ) : (
                      <span className="rounded bg-secondary px-1 py-px font-mono text-[8px] text-muted-foreground">内置</span>
                    )}
                    <button
                      type="button"
                      onClick={() => setExpanded((v) => ({ ...v, [item.key]: !v[item.key] }))}
                      className="rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground"
                    >
                      {open ? '收起 ▾' : `查看全文（${text.length} 字）▸`}
                    </button>
                    <button
                      type="button"
                      onClick={() => (isEditing ? cancelEdit(item.key) : startEdit(item.key))}
                      disabled={saving}
                      className="ml-auto rounded border border-primary/50 bg-primary/10 px-1.5 py-0.5 font-mono text-[9px] text-primary disabled:opacity-50"
                    >
                      {isEditing ? '取消' : '✎ 编辑'}
                    </button>
                  </div>
                  <div className="mt-0.5 font-mono text-[9px] text-muted-foreground/60">{item.desc}</div>
                  {statusModels.length > 0 && (
                    <div className="mt-0.5 font-mono text-[8px] text-muted-foreground/60">
                      使用它的模型：{statusModels.join(' / ')}
                    </div>
                  )}
                  {open && (
                    isEditing ? (
                      <div className="mt-1.5">
                        <textarea
                          value={draft[item.key] ?? ''}
                          onChange={(e) => setDraft((d) => ({ ...d, [item.key]: e.target.value }))}
                          rows={Math.min(24, Math.max(6, Math.ceil((draft[item.key] ?? '').length / 60)))}
                          className="w-full resize-y rounded border border-border bg-card px-2 py-1.5 font-mono text-[10px] leading-relaxed text-foreground focus:border-primary focus:outline-none"
                        />
                        <div className="mt-1 flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void saveMain(item.key)}
                            disabled={saving || !(draft[item.key] ?? '').trim()}
                            className="rounded border border-primary/50 bg-primary/10 px-2 py-1 font-mono text-[10px] text-primary disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {saving ? '保存中…' : '💾 保存并生效'}
                          </button>
                          {m?.edited && (
                            <button
                              type="button"
                              onClick={() => void resetMain(item.key)}
                              disabled={saving}
                              className="rounded border border-short/40 bg-short/5 px-2 py-1 font-mono text-[10px] text-short disabled:opacity-50"
                            >
                              ↺ 恢复内置默认
                            </button>
                          )}
                          <span className="ml-auto font-mono text-[9px] text-muted-foreground/50">
                            保存后写入数据库，引擎 ≤5s 生效，无需重启
                          </span>
                        </div>
                      </div>
                    ) : (
                      <pre className="thin-scroll mt-1.5 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded border border-border/50 bg-card p-2 font-mono text-[10px] leading-relaxed text-foreground/85">
                        {text}
                      </pre>
                    )
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ── ①-B 最终装配预览（主 Prompt + 运行时分区） ──
          主 Prompt 只是"框架"，真正发给模型的 prompt 还动态拼上若干分区
          （品种专项规则 / 因果链 / 当前生效经验规则…）。此区块把装配结果按顺序列出，
          避免"启用了经验却在 Prompt Force 里看不到"的困惑。 */}
      <div className="mt-3 rounded-md border border-border bg-card/50 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground/80">
            最终装配预览（真正发给模型的内容）
          </span>
          <select
            value={previewMode}
            onChange={(e) => setPreviewMode(e.target.value as 'force' | 'system')}
            className="rounded border border-border bg-card px-1.5 py-0.5 font-mono text-[10px] text-foreground focus:border-primary focus:outline-none"
          >
            <option value="system">System Prompt（八段式 · 唯一通道）</option>
          </select>
          {preview && (
            <span className="font-mono text-[10px] text-muted-foreground">
              合计 {preview.length} 字 · {preview.segments.filter((s) => s.injected).length} 个分区
            </span>
          )}
          <button
            type="button"
            onClick={() => void loadPreview()}
            className="ml-auto rounded border border-border px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground hover:text-foreground"
          >
            刷新
          </button>
        </div>
        <div className="mt-1 rounded border border-border/60 bg-secondary/20 px-2 py-1 text-[10px] leading-relaxed text-muted-foreground">
          上面「主 Prompt」是<b className="text-foreground">框架原文</b>（含你在提示库的编辑）；
          实际分析时会在其后依次拼上下面这些<b className="text-foreground">运行时分区</b>。
          所以经验/品种规则<b className="text-foreground">不会出现在主 Prompt 正文里</b>，而是作为独立分区追加。
        </div>
        {preview?.note && (
          <div className="mt-1 rounded border border-border/50 bg-secondary/20 px-2 py-1 font-mono text-[9px] leading-relaxed text-muted-foreground/80">
            {preview.note}
          </div>
        )}
        {preview ? (
          <div className="mt-1.5 space-y-1">
            {preview.segments.map((seg) => (
              <div
                key={seg.key}
                className={`rounded border px-2 py-1 ${seg.injected ? 'border-border bg-card/60' : 'border-dashed border-border bg-secondary/20'}`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[10px] font-bold text-foreground/90">{seg.title}</span>
                  <span className="font-mono text-[9px] text-muted-foreground">
                    {seg.injected ? `${seg.text.length} 字` : '当前未注入'}
                  </span>
                  {seg.editable && (
                    <span className="rounded border border-long/40 bg-long/10 px-1 font-mono text-[8px] text-long">
                      提示库可编辑
                    </span>
                  )}
                  {seg.injected && seg.text.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setPreviewOpen((o) => ({ ...o, [seg.key]: !o[seg.key] }))}
                      className="ml-auto rounded border border-border px-1.5 py-px font-mono text-[9px] text-muted-foreground hover:text-foreground"
                    >
                      {previewOpen[seg.key] ? '收起 ▾' : '查看内容 ▸'}
                    </button>
                  )}
                </div>
                {previewOpen[seg.key] && seg.text && (
                  <pre className="thin-scroll mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded border border-border bg-secondary/40 p-1.5 font-mono text-[10px] leading-relaxed text-foreground/85">
                    {seg.text}
                  </pre>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="mt-1.5 font-mono text-[10px] text-muted-foreground">加载中…</div>
        )}
      </div>

      {/* ── ② 自定义分析指令（注入 AI） ── */}
      <div className="mt-3 rounded-md border border-primary/25 bg-primary/5 p-2.5">
        <div className="font-mono text-[10px] uppercase tracking-widest text-primary/80">
          自定义分析指令（注入每次 AI 分析）
        </div>
        <div className="mt-1 rounded border border-primary/15 bg-primary/5 p-1.5 text-[10px] leading-relaxed text-muted-foreground">
          <b className="text-primary">区分说明：</b>主 Prompt = 模型的"分析框架"（上面那份生效的）；自定义指令 = 每次分析时<b className="text-foreground">额外追加的硬规则</b>（不动框架，标注"优先级最高，必须遵守"）。
          <br /><b className="text-primary">作用范围（发给哪个模型）：</b>启用后自动发给<b className="text-foreground">所有非 Doubao 模型</b>（不分 System Prompt / Prompt Force 通道，全模型都收到）；当前无需手动选择模型，<b className="text-muted-foreground">所有新闻分析都会带你的规则</b>。
        </div>

        {/* 新增表单 */}
        <div className="mt-2 space-y-1.5">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="指令名称（可选，如：黄金优先级）"
            className="w-full rounded border border-border bg-card px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
          />
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="指令内容：告诉 AI 必须遵守的分析规则，例如：&#10;遇到美联储降息/利率相关新闻时：先分析黄金（XAU）影响，再分析加密；&#10;若黄金已大幅上涨则警惕利好出尽，倾向 SELL 或 HOLD。"
            rows={3}
            className="w-full resize-y rounded border border-border bg-card px-2 py-1.5 font-mono text-xs leading-relaxed text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
          />
          <button
            type="button"
            onClick={() => void add()}
            disabled={submitting || !content.trim()}
            className="rounded border border-primary/50 bg-primary/10 px-3 py-1.5 font-mono text-xs text-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? '添加中…' : '添加并立即启用'}
          </button>
        </div>

        {/* 列表 */}
        <div className="mt-2 space-y-1.5">
          {!data || data.custom.length === 0 ? (
            <div className="rounded border border-border bg-card px-2 py-2 font-mono text-[10px] text-muted-foreground">
              暂无自定义指令——添加后即注入 AI 分析
            </div>
          ) : (
            data.custom.map((item) => (
              <div key={item.id} className={`rounded border px-2 py-1.5 ${item.enabled ? 'border-primary/30 bg-card' : 'border-border bg-secondary/30 opacity-70'}`}>
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px] font-semibold text-foreground" title={item.name}>
                    {item.name || item.content.slice(0, 24)}
                  </span>
                  <button
                    type="button"
                    onClick={() => void toggleCustom(item)}
                    className={`rounded border px-1.5 py-0.5 font-mono text-[9px] ${
                      item.enabled
                        ? 'border-long/50 bg-long/10 text-long'
                        : 'border-border text-muted-foreground'
                    }`}
                  >
                    {item.enabled ? '启用中' : '已停用'}
                  </button>
                  <button
                    type="button"
                    onClick={() => void remove(item)}
                    title="删除"
                    className="rounded border border-short/40 px-1 py-0.5 font-mono text-[9px] text-short hover:bg-short/10"
                  >
                    ✕
                  </button>
                </div>
                <div className="mt-0.5 line-clamp-3 whitespace-pre-wrap break-words text-[10px] leading-relaxed text-muted-foreground/90">
                  {item.content}
                </div>
                <div className="mt-0.5 font-mono text-[8px] text-muted-foreground/50">
                  添加于 {item.created_at?.slice(0, 16)}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  )
}
