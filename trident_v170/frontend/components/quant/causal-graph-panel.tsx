'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { API_BASE } from '@/lib/api'
import { RefreshCw, Loader2 } from 'lucide-react'

// 因果图谱 — 品种涨跌驱动知识图谱（后端 causal_graph 模块）。
// 节点 = 因素/资产/宏观变量/结论；边 = 因果(causes)/条件(conditions)/证实/证伪。
// 由 5-why 推理循环生成：最底层「核心驱动」→ 推理链中间层 → 「条件因素」 → 资产(果)。
// 本页用纯 SVG 分层布局（无第三方图库依赖）：
//   右 = 资产(果) —— 中间 = 推理链 / 条件因素 —— 左 = 核心驱动(最底层因)
// 边的方向从「因」指向「果(资产)」。

type NodeKind = 'asset' | 'core' | 'condition' | 'chain' | 'evidence'
type EdgeKind = 'causes' | 'conditions' | 'supports' | 'contradicts' | 'correlates_with'

type GraphNode = {
  id: number
  asset: string
  label: string
  kind: NodeKind
  is_core: number
  confidence: number
  evidence: string
  version: number
  active: number
  updated_at: string
}

type GraphEdge = {
  id: number
  from_id: number
  to_id: number
  kind: EdgeKind
  weight: number
  evidence: string
}

type Narrative = {
  total: number
  confirmed: number
  unconfirmed: number
  contradicted: number
  confirmed_factors: string[]
  contradicted_factors: string[]
  summary: string
}

type GraphData = { nodes: GraphNode[]; edges: GraphEdge[]; narrative?: Record<string, Narrative> }

const NODE_KIND_META: Record<NodeKind, { fill: string; r: number; label: string }> = {
  asset: { fill: '#f59e0b', r: 32, label: '资产(果)' },
  core: { fill: '#ef4444', r: 24, label: '核心驱动(最底层因)' },
  condition: { fill: '#fb923c', r: 20, label: '条件因素' },
  chain: { fill: '#94a3b8', r: 17, label: '推理链层' },
  evidence: { fill: '#22c55e', r: 14, label: '证据节点' },
}

const EDGE_KIND_META: Record<EdgeKind, { stroke: string; dash: string; label: string }> = {
  causes: { stroke: '#ef4444', dash: 'none', label: '因果 causes' },
  conditions: { stroke: '#fb923c', dash: '7 5', label: '条件 conditions' },
  supports: { stroke: '#22c55e', dash: 'none', label: 'support' },
  contradicts: { stroke: '#dc2626', dash: 'none', label: 'contradict' },
  correlates_with: { stroke: '#a855f7', dash: '5 4', label: 'correlates_with' },
}

function parseEvidence(s: string): string[] {
  if (!s) return []
  try {
    const v = JSON.parse(s)
    // 新格式（dict）：取 chain 字段，过滤掉 _ 前缀的元数据
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const chain = v.chain
      if (Array.isArray(chain)) return chain.map(String)
      return []
    }
    // 旧格式（纯 list）
    return Array.isArray(v) ? v.map(String) : [String(v)]
  } catch {
    return [s]
  }
}

// 解析核验元数据（_status / _fail / _verified_at / _fact_ids / _position）
export interface EvidenceMeta {
  status: 'CONFIRMED' | 'UNCONFIRMED' | 'CONTRADICTED' | null
  fail: string | null
  verified_at: string | null
  fact_ids: number[]
  position: 'cause' | 'middle' | 'effect' | null
}

function parseEvidenceMeta(s: string): EvidenceMeta {
  const empty: EvidenceMeta = {
    status: null, fail: null, verified_at: null, fact_ids: [], position: null,
  }
  if (!s) return empty
  try {
    const v = JSON.parse(s)
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      return {
        status: v._status ?? null,
        fail: v._fail ?? null,
        verified_at: v._verified_at ?? null,
        fact_ids: Array.isArray(v._fact_ids) ? v._fact_ids : [],
        position: v._position ?? null,
      }
    }
    return empty
  } catch {
    return empty
  }
}

// 核验状态 badge 配色
const STATUS_STYLE: Record<string, { bg: string; text: string; label: string; icon: string }> = {
  CONFIRMED: { bg: '#dcfce7', text: '#15803d', label: '已证实', icon: '✓' },
  UNCONFIRMED: { bg: '#f1f5f9', text: '#64748b', label: '未证实', icon: '?' },
  CONTRADICTED: { bg: '#fee2e2', text: '#b91c1c', label: '已证伪', icon: '✗' },
}

const FAIL_LABEL: Record<string, string> = {
  WRONG_HYPOTHESIS: '假设本身错',
  WRONG_INFERENCE: '推理链中间层错',
  MISSING_CONTEXT: '缺少关键信息',
}

const POSITION_LABEL: Record<string, string> = {
  cause: '因端',
  middle: '中间层',
  effect: '果端',
}

// 分层：以资产为「果」(层 0)，逆着边的箭头方向回溯，越靠近「因」层号越大（越靠左）。
function computeLayout(nodes: GraphNode[], edges: GraphEdge[]) {
  const layer = new Map<number, number>()
  for (const n of nodes) layer.set(n.id, n.kind === 'asset' ? 0 : -1)

  for (let round = 0; round < nodes.length; round++) {
    let changed = false
    for (const e of edges) {
      const to = layer.get(e.to_id)
      if (to !== undefined && to >= 0) {
        const cand = to + 1
        const from = layer.get(e.from_id)
        if (from === undefined || from < cand) {
          layer.set(e.from_id, cand)
          changed = true
        }
      }
    }
    if (!changed) break
  }
  for (const n of nodes) if (layer.get(n.id) === -1) layer.set(n.id, 1)

  const maxLayer = Math.max(0, ...Array.from(layer.values()))
  const groups = new Map<number, GraphNode[]>()
  for (const n of nodes) {
    const l = layer.get(n.id) ?? 0
    if (!groups.has(l)) groups.set(l, [])
    groups.get(l)!.push(n)
  }
  const rank = (n: GraphNode) => (n.is_core ? 1000 : 0) + n.confidence
  for (const g of groups.values()) g.sort((a, b) => rank(b) - rank(a))

  const colW = 210
  const rowH = 96
  const mx = 40
  const my = 60
  const perGroup = Math.max(1, ...Array.from(groups.values()).map((g) => g.length))
  const height = my * 2 + perGroup * rowH
  const width = mx * 2 + maxLayer * colW + 100

  const pos = new Map<number, { x: number; y: number }>()
  for (const [l, g] of groups) {
    const x = mx + (maxLayer - l) * colW
    const totalH = g.length * rowH
    const startY = (height - totalH) / 2
    g.forEach((n, i) => {
      pos.set(n.id, { x: x + 30, y: startY + i * rowH + rowH / 2 })
    })
  }
  return { pos, maxLayer, width, height, groups }
}

export function CausalGraphPanel() {
  const searchParams = useSearchParams()
  // 归因页 → 资产图谱的连通入口：?asset=XAU → 自动选中对应品种
  const urlAsset = searchParams.get('asset')?.trim().toUpperCase() || ''
  const [data, setData] = useState<GraphData>({ nodes: [], edges: [] })
  const [assets, setAssets] = useState<string[]>([])
  const [activeAsset, setActiveAsset] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reasoning, setReasoning] = useState(false)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [hoveredId, setHoveredId] = useState<number | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [verifyResult, setVerifyResult] = useState<any>(null)
  const [verifyError, setVerifyError] = useState<string | null>(null)

  const load = useCallback(async (keepAsset = true) => {
    try {
      const r = await fetch(`${API_BASE}/review/causal-graph`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`causal-graph ${r.status}`)
      const d = (await r.json()) as GraphData
      const allAssets = Array.from(new Set((d.nodes || []).map((n) => n.asset)))
      setData(d)
      setAssets(allAssets)
      // 优先级：URL ?asset= 参数 > 上次选择 > 第一个可用品种
      setActiveAsset((prev) => {
        if (urlAsset && allAssets.includes(urlAsset)) return urlAsset
        if (keepAsset && prev && allAssets.includes(prev)) return prev
        return allAssets[0] || ''
      })
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

  // URL ?asset= 变化时（用户从归因页切到另一个品种）— 自动跟随切换，
  // 仅当数据已加载且该品种确实存在时。
  useEffect(() => {
    if (!urlAsset || !assets.length) return
    if (assets.includes(urlAsset) && urlAsset !== activeAsset) {
      setActiveAsset(urlAsset)
    }
  }, [urlAsset, assets, activeAsset])

  const reason = async () => {
    setReasoning(true)
    try {
      const r = await fetch(`${API_BASE}/review/causal-graph/reason`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // 无选中品种时传空对象 → 后端 assets=None → 对全部历史品种跑 5-why（首次初始化）
        body: JSON.stringify(activeAsset ? { assets: [activeAsset] } : {}),
      })
      if (!r.ok) throw new Error(`reason ${r.status}`)
      const res = (await r.json()) as {
        detail?: Record<string, unknown>
        reasoned?: string[]
      }
      const entry = activeAsset
        ? (res.detail?.[activeAsset] as { ok?: boolean; error?: string } | undefined)
        : undefined
      if (entry && entry.ok === false) {
        setError(`推理未完成：${entry.error || '无候选因素或 LLM 不可用'}`)
      } else if (!activeAsset && !(res.reasoned && res.reasoned.length > 0)) {
        setError('推理未完成：没有可用品种或全部失败')
      } else {
        setError(null)
      }
      await load(true)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '推理失败')
    } finally {
      setReasoning(false)
    }
  }

  const view = useMemo(() => {
    if (!activeAsset) return { nodes: [] as GraphNode[], edges: [] as GraphEdge[] }
    // v276+ cross-asset view
    if (activeAsset === '__ALL__') {
      return {
        nodes: data.nodes.filter((n) => n.asset === 'all' || n.kind === 'asset'),
        edges: data.edges.filter((e) => e.kind === 'correlates_with' || e.kind === 'causes'),
      }
    }
    const nodeIds = new Set(data.nodes.filter((n) => n.asset === activeAsset).map((n) => n.id))
    return {
      nodes: data.nodes.filter((n) => nodeIds.has(n.id)),
      edges: data.edges.filter((e) => nodeIds.has(e.from_id) && nodeIds.has(e.to_id)),
    }
  }, [data, activeAsset])

  const layout = useMemo(() => computeLayout(view.nodes, view.edges), [view])

  // 图例只展示当前视图里真实出现的边类型（后端当前实现只写 causes / conditions，
  // 其它两种虽 schema 允许但本次未生成，避免误导用户认为未出现的种类代表缺失关系）。
  const presentEdgeKinds = useMemo(
    () => Array.from(new Set(view.edges.map((e) => e.kind))) as EdgeKind[],
    [view.edges],
  )

  const selected = view.nodes.find((n) => n.id === selectedId) || null

  // hover 高亮：同时高亮节点和与其相连的边。Bug 41 修复：如果 selectedId 节点在
  // 当前 view 中已不存在（例如 reason 把它合并/删了），selected 自动变 null。
  const focusId = hoveredId ?? selectedId
  const focusNodeIds = useMemo(() => {
    if (focusId == null) return new Set<number>()
    const set = new Set<number>([focusId])
    for (const e of view.edges) {
      if (e.from_id === focusId) set.add(e.to_id)
      if (e.to_id === focusId) set.add(e.from_id)
    }
    return set
  }, [focusId, view.edges])

  // ESC 关闭右侧详情侧栏（Bug 43）
  useEffect(() => {
    if (selectedId == null) return
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') setSelectedId(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selectedId])

  // view 一变清 selected（Bug 30）：asset 切换或 reason 后数据刷新，之前的选中
  // 可能已不对应当前 view。清空避免选中节点不显示在画布上的"幽灵侧栏"。
  const viewKey = `${activeAsset}|${view.nodes.length}|${view.edges.length}`
  useEffect(() => {
    setSelectedId(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey])

  return (
    <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-3 p-3 md:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">因果图谱 · 品种涨跌驱动知识图谱</h1>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
            用「连问为什么」(5-why) 追溯品种涨跌的最底层驱动。图的箭头从「因」指向「果（资产）」——
            左侧是核心驱动（最底层原因），右侧是资产本身，中间是推理链层与条件因素。旧结论被反复推理会
            上调置信度并升级版本；不再被证实的会衰减、失效。点击节点查看证据链。
          </p>
          {urlAsset && (
            <div className="mt-2 inline-flex items-center gap-2 rounded border border-primary/40 bg-primary/5 px-2 py-1 font-mono text-[10px] text-primary">
              🔗 从「归因页」跳转来 · 自动聚焦 {urlAsset}
              <a
                href="/attribution"
                className="ml-1 underline decoration-dotted underline-offset-2 hover:text-primary/80"
              >
                ← 返回归因
              </a>
            </div>
          )}
        </div>
        <button
          onClick={reason}
          disabled={reasoning}
          className="flex shrink-0 items-center gap-2 rounded-md border border-border bg-card px-3 py-2 font-mono text-xs uppercase tracking-wider hover:border-primary hover:text-primary disabled:cursor-not-allowed disabled:opacity-50"
        >
          {reasoning ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {reasoning ? '推理中…' : '重新推理'}
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {/* v276+ ALL cross-asset */}
        <button
          onClick={() => { setActiveAsset('__ALL__'); setSelectedId(null) }}
          className={`rounded-md border px-3 py-1.5 font-mono text-xs uppercase tracking-wider transition-colors ${
            activeAsset === '__ALL__'
              ? 'border-purple-500 bg-purple-500/10 text-purple-600 dark:text-purple-400'
              : 'border-border bg-secondary text-muted-foreground hover:text-foreground'
          }`}
          title="all assets + cross-asset neurons"
        >
          ALL
        </button>
        {assets.map((a) => (
          <button
            key={a}
            onClick={() => {
              setActiveAsset(a)
              setSelectedId(null)
            }}
            className={`rounded-md border px-3 py-1.5 font-mono text-xs uppercase tracking-wider transition-colors ${
              a === activeAsset
                ? 'border-primary bg-primary/10 text-primary'
                : 'border-border bg-secondary text-muted-foreground hover:text-foreground'
            }`}
          >
            {a}
          </button>
        ))}
        {!assets.length && !loading && (
          <span className="font-mono text-xs text-muted-foreground">暂无品种</span>
        )}
      </div>

      {/* v276+ brain health bar */}
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-purple-500/30 bg-purple-500/5 px-3 py-2 text-[11px] text-muted-foreground">
        <span className="font-mono">
          <b className="text-foreground">brain:</b>{' '}
          <span className="ml-1 text-purple-600 dark:text-purple-400">
            {data.nodes.filter((n) => n.kind === 'asset').length} assets
          </span>
          {' / '}
          <span className="text-purple-600 dark:text-purple-400">
            {data.edges.filter((e) => e.kind === 'correlates_with').length} cross-neurons
          </span>
          {' / '}
          <span className="text-rose-600 dark:text-rose-400">
            {data.edges.filter((e) => e.kind === 'contradicts').length} counter-evidence
          </span>
          {' / '}
          <span className="text-emerald-600 dark:text-emerald-400">
            {data.edges.filter((e) => e.kind === 'supports').length} supports
          </span>
        </span>
        <span className="font-mono">
          last-growth:{' '}
          {data.nodes
            .map((n) => n.updated_at)
            .filter(Boolean)
            .sort()
            .slice(-1)[0] || '—'}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-y border-border py-2 text-[11px] text-muted-foreground">
        {(Object.keys(NODE_KIND_META) as NodeKind[]).map((k) => (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span className="inline-block h-3 w-3 rounded-full" style={{ background: NODE_KIND_META[k].fill }} />
            {NODE_KIND_META[k].label}
          </span>
        ))}
        <span className="mx-1 h-3 w-px bg-border" />
        {(presentEdgeKinds.length ? presentEdgeKinds : (Object.keys(EDGE_KIND_META) as EdgeKind[])).map((k) => (
          <span key={k} className="inline-flex items-center gap-1.5">
            <span
              className="inline-block h-0 w-6 border-t-2"
              style={{
                borderColor: EDGE_KIND_META[k].stroke,
                borderTopStyle: EDGE_KIND_META[k].dash === 'none' ? 'solid' : 'dashed',
              }}
            />
            {EDGE_KIND_META[k].label}
          </span>
        ))}
      </div>

      {data.narrative?.[activeAsset] && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border bg-secondary/40 px-3 py-2 text-[11px]">
          <span className="font-mono text-muted-foreground">
            <b className="font-semibold text-foreground">当前叙事：</b>
            {data.narrative[activeAsset].summary}
          </span>
          {data.narrative[activeAsset].confirmed_factors.length > 0 && (
            <span className="font-mono text-emerald-600 dark:text-emerald-400">
              ✓ 已证实：{data.narrative[activeAsset].confirmed_factors.join('、')}
            </span>
          )}
          {data.narrative[activeAsset].contradicted_factors.length > 0 && (
            <span className="font-mono text-rose-600 dark:text-rose-400">
              ✗ 已证伪：{data.narrative[activeAsset].contradicted_factors.join('、')}
            </span>
          )}
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 py-16 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="font-mono text-xs">加载图谱…</span>
        </div>
      ) : error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      ) : view.nodes.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border py-20 text-center">
          <p className="font-mono text-xs text-muted-foreground">
            该品种暂无图谱。点击右上「重新推理」由 AI 生成 5-why 因果链。
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-3 lg:flex-row">
          <div className="min-w-0 flex-1 overflow-x-auto rounded-lg border border-border bg-card p-2">
            <svg
              viewBox={`0 0 ${layout.width} ${layout.height}`}
              style={{ minWidth: Math.min(layout.width, 760), height: layout.height }}
              className="mx-auto"
              role="img"
              aria-label="因果图谱"
            >
              <defs>
                {(Object.keys(EDGE_KIND_META) as EdgeKind[]).map((k) => (
                  <marker
                    key={k}
                    id={`arrow-${k}`}
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="7"
                    markerHeight="7"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 10 5 L 0 10 z" fill={EDGE_KIND_META[k].stroke} />
                  </marker>
                ))}
              </defs>

              {view.edges.map((e) => {
                const from = layout.pos.get(e.from_id)
                const to = layout.pos.get(e.to_id)
                if (!from || !to) return null
                const meta = EDGE_KIND_META[e.kind]
                // Bug 28：focus 时高亮关联边，非 focus 边降透明度
                const edgeInFocus = focusId != null && (e.from_id === focusId || e.to_id === focusId)
                const dim = focusId != null && !edgeInFocus
                return (
                  <line
                    key={e.id}
                    x1={from.x}
                    y1={from.y}
                    x2={to.x}
                    y2={to.y}
                    stroke={meta.stroke}
                    strokeWidth={edgeInFocus ? 2.5 : 0.8 + Math.min(e.weight, 4) * 0.5}
                    strokeDasharray={meta.dash}
                    opacity={dim ? 0.15 : 0.7}
                    markerEnd={`url(#arrow-${e.kind})`}
                  />
                )
              })}

              {view.nodes.map((n) => {
                const p = layout.pos.get(n.id)
                if (!p) return null
                const meta = NODE_KIND_META[n.kind]
                const isSel = selectedId === n.id
                const isHover = hoveredId === n.id
                const inFocus = focusNodeIds.has(n.id)
                const dim = focusId != null && !inFocus
                const labelLines =
                  n.label.length > 6 ? [n.label.slice(0, 6), n.label.slice(6, 12)] : [n.label]
                return (
                  <g
                    key={n.id}
                    onClick={() => setSelectedId(isSel ? null : n.id)}
                    onMouseEnter={() => setHoveredId(n.id)}
                    onMouseLeave={() => setHoveredId((prev) => (prev === n.id ? null : prev))}
                    className="cursor-pointer"
                    opacity={dim ? 0.25 : 1}
                    style={{ transition: 'opacity 120ms ease-out' }}
                  >
                    {(isSel || isHover) && (
                      <circle
                        cx={p.x}
                        cy={p.y}
                        r={meta.r + 6}
                        fill="none"
                        stroke={isSel ? '#6366f1' : '#fbbf24'}
                        strokeWidth={isSel ? 2 : 1.5}
                        className={isSel ? 'animate-pulse' : undefined}
                      />
                    )}
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={meta.r}
                      fill={meta.fill}
                      fillOpacity={0.92}
                      stroke="rgba(255,255,255,0.55)"
                      strokeWidth={isHover ? 2 : 1}
                    />
                    <text
                      x={p.x}
                      y={p.y + 4}
                      textAnchor="middle"
                      fontSize={n.kind === 'asset' ? 14 : 11}
                      fontWeight={700}
                      fill="#fff"
                    >
                      {Math.round(n.confidence * 100)}
                    </text>
                    {labelLines.map((line, i) => (
                      <text
                        key={i}
                        x={p.x}
                        y={p.y + meta.r + 16 + i * 15}
                        textAnchor="middle"
                        fontSize={11}
                        fill="currentColor"
                        className="fill-slate-500 dark:fill-slate-400"
                      >
                        {line}
                      </text>
                    ))}
                  </g>
                )
              })}
            </svg>
          </div>

          {selected && (
            <aside className="w-full shrink-0 rounded-lg border border-border bg-card p-4 lg:w-80">
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-sm font-semibold leading-snug">{selected.label}</h3>
                <span
                  className="shrink-0 rounded-md px-2 py-0.5 font-mono text-[10px] uppercase"
                  style={{
                    background: `${NODE_KIND_META[selected.kind].fill}22`,
                    color: NODE_KIND_META[selected.kind].fill,
                  }}
                >
                  {NODE_KIND_META[selected.kind].label}
                </span>
              </div>
              <dl className="mt-4 space-y-3 text-xs">
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">置信度</dt>
                  <dd className="font-mono">{(selected.confidence * 100).toFixed(0)}%</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">版本</dt>
                  <dd className="font-mono">v{selected.version}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">状态</dt>
                  <dd className="font-mono">{selected.active ? '生效' : '失效'}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">归属品种</dt>
                  <dd className="font-mono">{selected.asset}</dd>
                </div>
                {(() => {
                  const meta = parseEvidenceMeta(selected.evidence)
                  const statusStyle = meta.status ? STATUS_STYLE[meta.status] : null
                  return (
                    <>
                      <div className="flex items-center justify-between gap-2">
                        <dt className="text-muted-foreground">核验状态</dt>
                        <dd>
                          {statusStyle ? (
                            <span
                              className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-medium"
                              style={{ background: statusStyle.bg, color: statusStyle.text }}
                              title={meta.fail ? `失败诊断: ${FAIL_LABEL[meta.fail] || meta.fail}` : ''}
                            >
                              <span>{statusStyle.icon}</span>
                              <span>{statusStyle.label}</span>
                            </span>
                          ) : (
                            <span className="text-xs text-muted-foreground">未核验</span>
                          )}
                        </dd>
                      </div>
                      {meta.fail && (
                        <div className="flex justify-between">
                          <dt className="text-muted-foreground">失败原因</dt>
                          <dd className="text-right text-xs text-red-600">
                            {FAIL_LABEL[meta.fail] || meta.fail}
                          </dd>
                        </div>
                      )}
                      {meta.position && (
                        <div className="flex justify-between">
                          <dt className="text-muted-foreground">逻辑位置</dt>
                          <dd className="font-mono text-xs">
                            {POSITION_LABEL[meta.position] || meta.position}
                          </dd>
                        </div>
                      )}
                      {meta.verified_at && (
                        <div className="flex justify-between">
                          <dt className="text-muted-foreground">核验时间</dt>
                          <dd className="font-mono text-[10px] text-muted-foreground">
                            {meta.verified_at}
                          </dd>
                        </div>
                      )}
                      {meta.fact_ids.length > 0 && (
                        <div className="flex justify-between">
                          <dt className="text-muted-foreground">引用事实</dt>
                          <dd className="font-mono text-[10px] text-muted-foreground">
                            {meta.fact_ids.length} 条
                          </dd>
                        </div>
                      )}
                    </>
                  )
                })()}
              </dl>
              <div className="mt-4 border-t border-border pt-3">
                <p className="mb-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                  证据链 (why_chain)
                </p>
                {parseEvidence(selected.evidence).length ? (
                  <ol className="space-y-1.5">
                    {parseEvidence(selected.evidence).map((w, i) => (
                      <li key={i} className="flex gap-2 text-xs leading-relaxed">
                        <span className="font-mono text-muted-foreground">{i + 1}.</span>
                        <span>{w}</span>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="text-xs text-muted-foreground">—</p>
                )}
              </div>
              <div className="mt-4 border-t border-border pt-3">
                <button
                  type="button"
                  onClick={async () => {
                    if (!selected) return
                    setVerifying(true)
                    setVerifyError(null)
                    try {
                      const resp = await fetch('/api/review/causal-graph/verify', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ asset: selected.asset }),
                      })
                      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
                      const data = await resp.json()
                      const r = await fetch(
                        `/api/review/causal-graph?asset=${encodeURIComponent(selected.asset)}&_=${Date.now()}`,
                      )
                      const fresh = await r.json()
                      setData(fresh)
                      const refreshed = fresh.nodes.find((n: GraphNode) => n.id === selected.id)
                      if (refreshed) setSelectedId(refreshed.id)
                      setVerifyResult(data.summary || data)
                    } catch (e: any) {
                      setVerifyError(String(e))
                    } finally {
                      setVerifying(false)
                    }
                  }}
                  disabled={verifying}
                  className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium transition hover:bg-accent disabled:opacity-50"
                >
                  {verifying ? '核验中…' : '🔍 手动核验（用近期事实核验此品种所有节点）'}
                </button>
                {verifyResult && (
                  <p className="mt-2 text-[10px] text-muted-foreground">
                    上次核验: {JSON.stringify(verifyResult.by_status || verifyResult)}
                  </p>
                )}
                {verifyError && (
                  <p className="mt-2 text-[10px] text-red-600">核验失败: {verifyError}</p>
                )}
              </div>

              {/* v189: 因果图谱准确性统计 — 基于 review_insights + causal_chains */}
              <CausalAccuracyWidget asset={activeAsset} />
            </aside>
          )}
        </div>
      )}
    </div>
  )
}

// ── v189: 因果图谱准确性 Widget ────────────────────────────────────────
type AccuracyResp = {
  ok: boolean
  asset: string
  days: number
  n_truth_insights: number
  n_causal_chains: number
  coverage: number
  avg_weight_chain: number
  low_confidence_chain_count: number
  low_confidence_sample: Array<{
    id: number; name: string; cause: string; effect: string;
    weight: number; origin: string; trigger_words: string;
  }>
  origin_breakdown_chain: Record<string, number>
  origin_breakdown_truth: Record<string, number>
  trust_breakdown: { trusted: number; pending: number }
  generated_at?: string
  error?: string
}

function CausalAccuracyWidget({ asset }: { asset: string }) {
  const [days, setDays] = useState(30)
  const [data, setData] = useState<AccuracyResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const r = await fetch(
        `${API_BASE}/v189/causal/accuracy-stats?asset=${encodeURIComponent(asset)}&days=${days}`,
        { cache: 'no-store' },
      )
      const j = (await r.json()) as AccuracyResp
      setData(j)
      if (!j.ok) setErr(j.error ?? 'unknown')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'fetch failed')
    } finally {
      setLoading(false)
    }
  }, [asset, days])

  useEffect(() => { void load() }, [load])

  if (!data && loading) {
    return (
      <div className="rounded-md border border-border bg-background p-3">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">📊 准确性统计</div>
        <div className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 加载中…
        </div>
      </div>
    )
  }
  if (!data) return null
  const coverageColor = data.coverage >= 0.7 ? 'text-emerald-500' : data.coverage >= 0.4 ? 'text-amber-500' : 'text-rose-500'
  const trustedRatio = data.trust_breakdown.trusted / Math.max(1, data.trust_breakdown.trusted + data.trust_breakdown.pending)

  return (
    <div className="rounded-md border border-border bg-background p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          📊 准确性统计 · {data.asset} · {data.days}d
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-muted/40 disabled:opacity-50"
          title="刷新"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="mb-2 flex items-center gap-1">
        {[7, 30, 90].map((d) => (
          <button
            key={d}
            type="button"
            onClick={() => setDays(d)}
            className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
              days === d ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-secondary'
            }`}
          >
            {d}d
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-2 text-[11px]">
        <div className="rounded border border-border/60 bg-card/50 p-2">
          <div className="text-[9px] uppercase text-muted-foreground">覆盖率</div>
          <div className={`mt-0.5 font-mono tabular-nums text-base font-semibold ${coverageColor}`}>
            {(data.coverage * 100).toFixed(1)}%
          </div>
          <div className="mt-0.5 text-[9px] text-muted-foreground">
            {data.n_truth_insights} 经验 / {data.n_causal_chains} 链路
          </div>
        </div>
        <div className="rounded border border-border/60 bg-card/50 p-2">
          <div className="text-[9px] uppercase text-muted-foreground">可信赖率</div>
          <div className="mt-0.5 font-mono tabular-nums text-base font-semibold text-emerald-500">
            {(trustedRatio * 100).toFixed(1)}%
          </div>
          <div className="mt-0.5 text-[9px] text-muted-foreground">
            ≥5 样本 {data.trust_breakdown.trusted} / 待验 {data.trust_breakdown.pending}
          </div>
        </div>
        <div className="rounded border border-border/60 bg-card/50 p-2">
          <div className="text-[9px] uppercase text-muted-foreground">平均权重</div>
          <div className="mt-0.5 font-mono tabular-nums text-base font-semibold">
            {data.avg_weight_chain.toFixed(2)}
          </div>
        </div>
        <div className="rounded border border-border/60 bg-card/50 p-2">
          <div className="text-[9px] uppercase text-muted-foreground">低权重链路</div>
          <div className={`mt-0.5 font-mono tabular-nums text-base font-semibold ${data.low_confidence_chain_count > 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
            {data.low_confidence_chain_count}
          </div>
        </div>
      </div>

      {data.low_confidence_sample.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[10px] text-rose-500 hover:underline">
            ⚠ {data.low_confidence_sample.length} 条低权重链路（点查看）
          </summary>
          <ul className="mt-1 space-y-1">
            {data.low_confidence_sample.slice(0, 5).map((c) => (
              <li key={c.id} className="rounded border border-border/40 bg-card/30 px-2 py-1 text-[10px]">
                <div className="flex items-center justify-between gap-1">
                  <span className="font-mono text-muted-foreground">#{c.id}</span>
                  <span className="rounded bg-rose-500/15 px-1 py-px font-mono text-rose-500">
                    w={c.weight.toFixed(2)}
                  </span>
                </div>
                <div className="mt-0.5">
                  <span>{c.cause}</span>
                  <span className="px-1 text-muted-foreground">→</span>
                  <span>{c.effect}</span>
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="mt-2 text-[9px] text-muted-foreground/70">
        生成于 {data.generated_at?.slice(11, 19) ?? '—'}
      </div>
    </div>
  )
}