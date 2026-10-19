'use client'

import { useEffect, useState, useCallback, useMemo } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  RefreshCcw,
  AlertTriangle,
  CircleDashed,
  CheckCircle2,
} from 'lucide-react'
import { apiGet, apiSend } from '@/lib/api'
import { cn } from '@/lib/utils'
import { DataState } from '@/components/ui/data-state'

// 路线 A · 股票类 ai_decisions 反思面板（v276）
// 解决：SKHYNIX / 三星 / 台积电 等无 Binance USDT 永续的股票没有 paper-trading
// 结算 → /review 段 3 看不到任何股票反思。本面板绕过结算要求，直接读
// market_category='STOCK' 的 ai_decisions + 已落库的 decision_errors
// （origin/source = 'stock_reflection_panel'）。

type StockDecision = {
  id: number
  target_asset: string
  suggested_action: string
  sentiment_score: number | null
  reasoning_length: number
  created_at: string
  is_correct: '' | 'WIN' | 'LOSS'
  settled: 0 | 1
  forward_pnl: number | null
  entry_price: number | null
  exit_price: number | null
  market_category: string
  error_codes: string[]
  error_count: number
}

type StockDecisionsResp = {
  total: number
  by_asset: Record<string, number>
  by_action: Record<string, number>
  days: number
  limit: number
  decisions: StockDecision[]
}

type StockAuditResp = {
  mode: string
  scanned: number
  checker_triggered: number
  inserted: number
  skipped_low_completeness: number
  by_code: Record<string, number>
  decisions: { id: number; asset: string; action: string; error_codes: string[]; error_count: number }[]
  origin: string
  error?: string
}

const ERROR_CODE_LABELS: Record<string, string> = {
  E001: '数据/锚点',
  E001a: '硬数据缺失',
  E002: '情感/方向',
  E003: '信号阈值',
  E004: '样本/对照',
  E005: '因果链',
  E006: '规则违反',
  E007: '跨资产',
  E008: '推理缺失',
  E009: '事件逻辑',
  E010: '历史反例',
  E011: '因子排名',
  E012: '时效/相位',
  E013: '时间尺度',
  E014: '市场机制',
  E015: '新闻主体',
  E016: '口径',
  E017: '跨源一致性',
  E018: '聚簇/稀释',
  E019: '完整性低',
  E020: '样本无效',
  E021: '推理·目标一致性',
  E022: '推理·反共识',
  E023: '推理·因果机制',
  E024: '推理·历史模式',
  E025: '推理·跨资产方向',
  E026: '推理·假设',
  E027: '推理·完备度',
}

function labelOf(code: string): string {
  return ERROR_CODE_LABELS[code] || code
}

function actionColor(a: string): string {
  if (a === 'BUY') return 'text-emerald-500'
  if (a === 'SELL') return 'text-rose-500'
  return 'text-muted-foreground'
}

function codeColor(code: string): string {
  if (code.startsWith('E01') || /^E0[2-9]$/.test(code) || /^E1[0-8]$/.test(code)) {
    return 'bg-amber-500/15 border-amber-500/40 text-amber-500'
  }
  if (code === 'E019') return 'bg-slate-500/15 border-slate-500/40 text-slate-400'
  if (code === 'E020') return 'bg-fuchsia-500/15 border-fuchsia-500/40 text-fuchsia-400'
  return 'bg-sky-500/15 border-sky-500/40 text-sky-400'
}

export function StockReflectionPanel() {
  const [data, setData] = useState<StockDecisionsResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [auditing, setAuditing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastAudit, setLastAudit] = useState<StockAuditResp | null>(null)
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set())
  const [filterAsset, setFilterAsset] = useState<string>('')

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const j = await apiGet<StockDecisionsResp>('/reflection/stock-decisions?days=30&limit=200')
      setData(j)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const runAudit = useCallback(async () => {
    setAuditing(true)
    setError(null)
    try {
      const j = await apiSend<StockAuditResp>('/reflection/stock-audit', 'POST', { days: 30, limit: 200 })
      setLastAudit(j)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setAuditing(false)
    }
  }, [load])

  const toggleExpand = useCallback((id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const filtered = useMemo(() => {
    if (!data) return []
    if (!filterAsset) return data.decisions
    return data.decisions.filter((d) => d.target_asset === filterAsset)
  }, [data, filterAsset])

  const codeDist = useMemo(() => {
    const map = new Map<string, number>()
    if (!data) return []
    for (const d of data.decisions) {
      for (const c of d.error_codes) {
        map.set(c, (map.get(c) || 0) + 1)
      }
    }
    return Array.from(map.entries()).sort((a, b) => b[1] - a[1])
  }, [data])

  const auditedCount = useMemo(() => {
    if (!data) return 0
    return data.decisions.filter((d) => d.error_count > 0).length
  }, [data])

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-card/40 p-3">
      <div className="flex flex-wrap items-end justify-between gap-2 border-b border-border pb-2">
        <div className="flex flex-col gap-0.5">
          <h3 className="flex items-center gap-2 font-mono text-sm font-bold text-foreground">
            📈 股票反思（未结算通道）
          </h3>
          <p className="font-mono text-[11px] text-muted-foreground">
            绕过 paper-trading，对 market_category=STOCK 决策跑 E001-E027 checker
          </p>
        </div>
        <button
          type="button"
          onClick={runAudit}
          disabled={auditing || loading}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-md border border-border bg-secondary px-2.5 py-1 font-mono text-xs transition-colors',
            'hover:bg-secondary/70 disabled:opacity-50',
          )}
          title="对最近 30 天 STOCK 决策批量跑反思 checker 并落库"
        >
          {auditing ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCcw className="h-3 w-3" />
          )}
          {auditing ? '触发中…' : '触发最新反思'}
        </button>
      </div>

      <DataState
        loading={loading && !data}
        error={error}
        loadingLabel="载入股票决策…"
        empty={!data || data.decisions.length === 0}
        emptyLabel={filterAsset ? `没有 ${filterAsset} 的决策` : '最近 30 天没有 STOCK 类决策'}
        data={data}
      >
        {(d) => (
          <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Kpi
              label="STOCK 决策总数"
              value={d.total.toString()}
              sub={`近 ${d.days} 天`}
            />
            <Kpi
              label="已触发反思"
              value={auditedCount.toString()}
              sub={`${d.total ? Math.round((auditedCount / d.total) * 100) : 0}% 命中率`}
              accent={auditedCount > 0 ? 'amber' : 'muted'}
            />
            <Kpi
              label="错误码种类"
              value={codeDist.length.toString()}
              sub={codeDist.length ? `TOP: ${labelOf(codeDist[0][0])}` : '无'}
            />
            <Kpi
              label="未结算 / 已结算"
              value={`${d.decisions.filter((d) => !d.settled).length} / ${d.decisions.filter((d) => d.settled).length}`}
              sub="股票决策大多无 paper-trading"
              accent="muted"
            />
          </div>


          {lastAudit && (
            <div className="rounded border border-emerald-500/30 bg-emerald-500/5 px-2 py-1.5 font-mono text-[11px] text-emerald-400">
              ✓ 已扫描 {lastAudit.scanned} 条决策 · 触发 {lastAudit.checker_triggered} 个错误 ·
              落库 {lastAudit.inserted} 条 · E019 跳过 {lastAudit.skipped_low_completeness} 条
              {Object.keys(lastAudit.by_code).length > 0 && (
                <span className="ml-2 text-muted-foreground">
                  · 分布：
                  {Object.entries(lastAudit.by_code)
                    .slice(0, 5)
                    .map(([c, n]) => `${c}×${n}`)
                    .join(' / ')}
                </span>
              )}
            </div>
          )}

          {codeDist.length > 0 && (
            <div className="flex flex-col gap-1">
              <div className="font-mono text-[11px] text-muted-foreground">
                错误码分布
              </div>
              <div className="flex flex-wrap gap-1">
                {codeDist.slice(0, 8).map(([code, n]) => (
                  <span
                    key={code}
                    className={cn(
                      'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]',
                      codeColor(code),
                    )}
                    title={`${labelOf(code)}：${n} 条决策触发`}
                  >
                    <span>{code}</span>
                    <span className="text-muted-foreground">×{n}</span>
                  </span>
                ))}
              </div>
            </div>
          )}

          {Object.keys(d.by_asset).length > 1 && (
            <div className="flex flex-wrap items-center gap-1">
              <span className="font-mono text-[11px] text-muted-foreground">品种：</span>
              <button
                type="button"
                onClick={() => setFilterAsset('')}
                className={cn(
                  'rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors',
                  filterAsset === ''
                    ? 'border-foreground/40 bg-foreground/10 text-foreground'
                    : 'border-border bg-background text-muted-foreground hover:text-foreground',
                )}
              >
                全部 ({d.total})
              </button>
              {Object.entries(d.by_asset)
                .sort((a, b) => b[1] - a[1])
                .map(([asset, n]) => (
                  <button
                    key={asset}
                    type="button"
                    onClick={() => setFilterAsset(asset)}
                    className={cn(
                      'rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors',
                      filterAsset === asset
                        ? 'border-foreground/40 bg-foreground/10 text-foreground'
                        : 'border-border bg-background text-muted-foreground hover:text-foreground',
                    )}
                  >
                    {asset} ({n})
                  </button>
                ))}
            </div>
          )}




          <div className="flex flex-col gap-1">
            {filtered.length === 0 && (
              <div className="px-2 py-3 font-mono text-[11px] text-muted-foreground">
                {filterAsset ? `没有 ${filterAsset} 的决策` : '最近 30 天没有 STOCK 类决策'}
              </div>
            )}
            {filtered.map((d) => {
              const expanded = expandedIds.has(d.id)
              return (
                <div
                  key={d.id}
                  className="rounded border border-border bg-background/40 transition-colors hover:bg-background/70"
                >
                  <button
                    type="button"
                    onClick={() => toggleExpand(d.id)}
                    className="flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left"
                  >
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
                      {expanded ? (
                        <ChevronDown className="h-3 w-3 text-muted-foreground" />
                      ) : (
                        <ChevronRight className="h-3 w-3 text-muted-foreground" />
                      )}
                      <span className="font-bold text-foreground">#{d.id}</span>
                      <span className="rounded bg-secondary px-1 py-0.5 text-[10px] font-bold">
                        {d.target_asset}
                      </span>
                      <span className={cn('font-bold', actionColor(d.suggested_action))}>
                        {d.suggested_action}
                      </span>
                      <span className="text-muted-foreground">
                        情感 {(d.sentiment_score ?? 0).toFixed(2)}
                      </span>
                      <span className="text-muted-foreground">推理 {d.reasoning_length} 字</span>
                      <span className="text-muted-foreground">{formatDate(d.created_at)}</span>
                      {d.settled === 0 ? (
                        <span className="inline-flex items-center gap-0.5 text-muted-foreground">
                          <CircleDashed className="h-3 w-3" /> 未结算
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-0.5 text-emerald-500">
                          <CheckCircle2 className="h-3 w-3" />
                          {d.forward_pnl !== null ? `${(d.forward_pnl * 100).toFixed(2)}%` : '已结算'}
                        </span>
                      )}
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      {d.error_count === 0 ? (
                        <span className="inline-flex items-center gap-0.5 rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                          <CheckCircle2 className="h-3 w-3" /> 无反思
                        </span>
                      ) : (
                        <>
                          <span className="inline-flex items-center gap-0.5 rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] text-amber-500">
                            <AlertTriangle className="h-3 w-3" /> {d.error_count} 错
                          </span>
                          {d.error_codes.slice(0, 4).map((c) => (
                            <span
                              key={c}
                              className={cn(
                                'rounded border px-1 py-0.5 font-mono text-[10px]',
                                codeColor(c),
                              )}
                              title={labelOf(c)}
                            >
                              {c}
                            </span>
                          ))}
                          {d.error_codes.length > 4 && (
                            <span className="font-mono text-[10px] text-muted-foreground">
                              +{d.error_codes.length - 4}
                            </span>
                          )}
                        </>
                      )}
                    </div>
                  </button>
                  {expanded && <StockDecisionDetail decisionId={d.id} />}
                </div>
              )
            })}
          </div>

          <div className="border-t border-border pt-1.5 font-mono text-[10px] text-muted-foreground">
            💡 这条通道不依赖 paper-trading 结算。SKHYNIX / 三星 / 台积电 等无 Binance USDT
            永续的股票，原本因不会结算而消失在 /review 段 3。本面板让它们也能被反思。
          </div>
        </>
        )}
      </DataState>
    </div>
  )
}



function Kpi({
  label,
  value,
  sub,
  accent,
}: {
  label: string
  value: string
  sub?: string
  accent?: 'amber' | 'emerald' | 'muted'
}) {
  const vCls =
    accent === 'amber'
      ? 'text-amber-500'
      : accent === 'emerald'
        ? 'text-emerald-500'
        : 'text-foreground'
  return (
    <div className="rounded border border-border bg-background/40 px-2 py-1.5">
      <div className="font-mono text-[10px] text-muted-foreground">{label}</div>
      <div className={cn('font-mono text-base font-bold', vCls)}>{value}</div>
      {sub && <div className="font-mono text-[10px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

function StockDecisionDetail({ decisionId }: { decisionId: number }) {
  const [detail, setDetail] = useState<any | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiGet<unknown>(`/reasoning/decision/${decisionId}/text`)
      .then((j) => {
        if (!cancelled) {
          setDetail(j)
          setLoading(false)
        }
      })
      .catch(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [decisionId])

  if (loading) {
    return (
      <div className="flex items-center gap-2 border-t border-border px-2 py-2 font-mono text-[11px] text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> 载入推理文本…
      </div>
    )
  }
  if (!detail) {
    return (
      <div className="border-t border-border px-2 py-2 font-mono text-[11px] text-muted-foreground">
        推理文本不可用（决策 #{decisionId}）
      </div>
    )
  }
  const text = detail.full_text || detail.reasoning || ''
  return (
    <div className="flex flex-col gap-1 border-t border-border bg-background/20 px-2 py-1.5">
      <div className="font-mono text-[10px] text-muted-foreground">
        推理文本（{text.length} 字）· 决策 #{decisionId}
      </div>
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-background/40 p-2 font-mono text-[11px] leading-relaxed text-foreground">
        {text}
      </pre>
    </div>
  )
}

function formatDate(s: string): string {
  if (!s) return ''
  const m = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
  return m ? `${m[1]} ${m[2]}` : s.slice(0, 16)
}
