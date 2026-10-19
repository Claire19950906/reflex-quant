'use client'

import { useEffect, useState } from 'react'
import { Search, Triangle } from 'lucide-react'
import { ThemeToggle } from '@/components/theme-toggle'
import { API_BASE } from '@/lib/api'

type AIModel = {
  id: string
  label: string
}

type AIModelsResponse = {
  models: AIModel[]
  selected: string
}

export function DashboardHeader() {
  const [models, setModels] = useState<AIModel[]>([])
  const [selected, setSelected] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState('加载模型列表')

  useEffect(() => {
    let active = true
    let aborted = false
    // v2026.10.15 bugfix: 加 6s 硬超时, 避免后端任何暂态卡顿把下拉锁死 disabled.
    // 失败时按当前已加载的 models 处理(若有), 不再无限 disabled.
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 6000)

    fetch(`${API_BASE}/ai/models`, { signal: ctrl.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('加载失败')
        return response.json() as Promise<AIModelsResponse>
      })
      .then((data) => {
        if (active) {
          // v302+ R2 Round 4 防御: data.models/selected 可能为 undefined (兜底空响应)
          setModels(Array.isArray(data?.models) ? data.models : [])
          setSelected(typeof data?.selected === 'string' ? data.selected : '')
          setStatus('选择 AI 模型')
        }
      })
      .catch((err) => {
        if (aborted || !active) return
        // 区分超时和真错误
        if (err?.name === 'AbortError') {
          setStatus('模型列表加载超时, 点击重试')
        } else {
          setStatus('模型列表加载失败, 点击重试')
        }
      })
      .finally(() => clearTimeout(timer))

    return () => {
      active = false
      aborted = true
      clearTimeout(timer)
      ctrl.abort()
    }
  }, [])

  async function handleModelChange(modelId: string) {
    const previous = selected
    setSelected(modelId)
    setSaving(true)
    setStatus('正在切换模型')

    try {
      const response = await fetch(`${API_BASE}/ai/models`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: modelId }),
      })
      if (!response.ok) throw new Error('保存失败')
      const data = await response.json() as AIModelsResponse
      // v302+ R2 Round 4 防御: 同上
      setModels(Array.isArray(data?.models) ? data.models : [])
      setSelected(typeof data?.selected === 'string' ? data.selected : '')
      setStatus('模型已切换')
    } catch {
      setSelected(previous)
      setStatus('模型切换失败，已回滚')
    } finally {
      setSaving(false)
    }
  }

  return (
    <header className="top-nav sticky top-0 z-20 flex h-16 items-center justify-between px-3 md:px-6">
      <div className="flex shrink-0 items-center gap-2 md:gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-primary/25 bg-primary/10 shadow-sm">
          <Triangle className="h-5 w-5 fill-primary text-primary" aria-hidden="true" />
        </div>
        <div className="hidden leading-tight sm:block">
          <h1 className="font-mono text-lg font-bold tracking-tight text-foreground">
            Trident
          </h1>
          <p className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
            量化交易平台
          </p>
        </div>
      </div>

      <div className="hidden items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5 shadow-sm lg:flex">
        <Search className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
        <span className="font-mono text-xs text-muted-foreground">
          BTC / USDT · Perpetual
        </span>
      </div>

      <div className="flex min-w-0 items-center gap-1.5 sm:gap-2 md:gap-3">
        <select
          value={selected}
          onChange={(event) => void handleModelChange(event.target.value)}
          disabled={saving || models.length === 0}
          aria-label="选择 AI 模型"
          title={status}
          className="h-9 min-w-0 max-w-[8.5rem] truncate rounded-lg border border-border bg-card px-2 font-mono text-[11px] text-foreground shadow-sm transition-colors hover:border-primary/40 disabled:cursor-wait disabled:opacity-60 sm:max-w-[15rem] md:text-xs"
        >
          {models.length === 0 ? <option value="">模型</option> : null}
          {models.map((model) => (
            <option key={model.id} value={model.id}>{model.label}</option>
          ))}
        </select>
        <ThemeToggle />
        {/* v302+ P0: 原 ⚙ Settings 按钮已删除 — 它没有 onClick / 没有 /settings 路由,
            是纯死按钮。现在用 ThemeToggle 替代,等真有设置中心时再补。 */}
      </div>
    </header>
  )
}
