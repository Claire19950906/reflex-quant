'use client'

import { useState } from 'react'
import type { PeerReview } from '@/lib/quant-data'
import { AnalysisSteps } from './analysis-steps'

// 多模型交叉验证展示：主模型 + 验证模型各自完整分析（可展开六段推理）
// 仅展示与标注，不改变主决策（后端已定）。

function fmtScore(score?: number | null): string {
  if (score === null || score === undefined || Number.isNaN(score)) return '—'
  return `${score > 0 ? '+' : ''}${Number(score).toFixed(2)}`
}

function fmtDim(v?: number | null): string {
  return v == null ? '—' : `${Math.round(v)}`
}

function dimLabel(p: PeerReview): string {
  const parts: string[] = []
  if (p.expected_horizon) parts.push(`周期 ${p.expected_horizon}`)
  if (p.event_strength) parts.push(`强度 ${p.event_strength}`)
  if (p.market_confirmation) parts.push(`盘面 ${p.market_confirmation === 'positive' ? '确认' : p.market_confirmation === 'negative' ? '背离' : '未确认'}`)
  if (p.direct_catalyst != null && p.direct_catalyst !== 0 && p.direct_catalyst !== false) parts.push('⚡ 直接催化')
  if (p.heat_score != null && p.heat_score > 0) parts.push(`🔥关注 ${fmtDim(p.heat_score)}`)
  if (p.impact_score != null && p.impact_score > 0) parts.push(`🌊影响 ${fmtDim(p.impact_score)}`)
  if (p.asset_impact != null && p.asset_impact > 0) parts.push(`🎯资产 ${fmtDim(p.asset_impact)}`)
  return parts.join(' · ')
}

export function PeerReviewsBlock({ mainAction, peers }: { mainAction: string; peers?: PeerReview[] }) {
  const list = (peers ?? []).filter((p) => p && p.action)
  const [openIdx, setOpenIdx] = useState<Record<number, boolean>>({})
  if (list.length === 0) return null

  const main = (mainAction || '').toUpperCase()
  const votes = [main, ...list.map((p) => String(p.action || '').toUpperCase())]
  const hasBuy = votes.includes('BUY')
  const hasSell = votes.includes('SELL')
  const directional = votes.filter((a) => a === 'BUY' || a === 'SELL')
  const conflict = hasBuy && hasSell

  return (
    <div className={`rounded border px-2.5 py-2 ${conflict ? 'border-short/40 bg-short/5' : 'border-long/30 bg-long/5'}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] font-bold uppercase tracking-wider text-foreground">
          ⚖ 多模型交叉验证（{1 + list.length} 个模型）
        </span>
        {conflict ? (
          <span className="rounded border border-short/50 bg-short/10 px-1.5 py-px font-mono text-[9px] font-bold text-short">
            ⚠ 分歧：{hasBuy ? 'BUY' : ''}{hasBuy && hasSell ? ' vs ' : ''}{hasSell ? 'SELL' : ''}
          </span>
        ) : (
          <span className="rounded border border-long/50 bg-long/10 px-1.5 py-px font-mono text-[9px] font-bold text-long">
            ✓ 方向一致（{directional.length} 个方向结论同向）
          </span>
        )}
        <span className="ml-auto font-mono text-[9px] text-muted-foreground/60">
          各模型独立分析 · 仅展示，主决策不变
        </span>
      </div>

      {/* 主模型 */}
      <div className="mt-1.5 rounded border border-primary/40 bg-primary/5 px-2 py-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[10px]">
          <span className="font-bold text-primary">主模型（决策）</span>
          <span className={main === 'BUY' ? 'text-long' : main === 'SELL' ? 'text-short' : 'text-hold'}>{main || '—'}</span>
          <span className="text-muted-foreground/70">→ 本次决策依据 · 完整六段见上方"大模型归因"</span>
        </div>
      </div>

      {/* 验证模型：可展开完整六段 */}
      <div className="mt-1 space-y-1">
        {list.map((p, i) => {
          const a = String(p.action || '').toUpperCase()
          const open = !!openIdx[i]
          const path = p.reasoning_path || ''
          const dims = dimLabel(p)
          return (
            <div key={`${p.model_id ?? p.model_label ?? ''}-${i}`} className="rounded border border-border bg-card">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()  // 防止冒泡触发外层新闻折叠
                  setOpenIdx((o) => ({ ...o, [i]: !o[i] }))
                }}
                className="flex w-full flex-wrap items-center gap-x-2 gap-y-0.5 px-2 py-1 text-left font-mono text-[10px] hover:bg-secondary/30"
              >
                <span className="w-4 shrink-0 text-muted-foreground">#{i + 1}</span>
                <span className="min-w-0 flex-1 truncate text-foreground" title={p.model_label}>{p.model_label ?? p.model_id ?? '验证模型'}</span>
                <span className={a === 'BUY' ? 'text-long' : a === 'SELL' ? 'text-short' : a === 'HOLD' ? 'text-hold' : 'text-muted-foreground'}>
                  {a || '—'}
                </span>
                <span className={`w-12 shrink-0 text-right ${(p.score ?? 0) >= 0 ? 'text-long' : 'text-short'}`}>{fmtScore(p.score)}</span>
                {a === main ? (
                  <span className="shrink-0 rounded bg-long/10 px-1 text-[8px] text-long">同主</span>
                ) : a === 'HOLD' ? (
                  <span className="shrink-0 rounded bg-hold/10 px-1 text-[8px] text-hold">观望</span>
                ) : (
                  <span className="shrink-0 rounded bg-short/10 px-1 text-[8px] text-short">相反</span>
                )}
                <span className="shrink-0 font-mono text-[9px] text-muted-foreground/70">{open ? '收起 ▾' : '展开完整分析 ▸'}</span>
              </button>
              {open && (
                <div className="border-t border-border/50 px-2 py-1.5">
                  {p.reasoning && (
                    <div className="mb-1 text-[11px] leading-relaxed text-foreground/90">
                      <b className="font-mono text-[10px] text-muted-foreground">结论：</b>{p.reasoning}
                    </div>
                  )}
                  {dims && (
                    <div className="mb-1 font-mono text-[9px] text-muted-foreground/70">{dims}</div>
                  )}
                  {path ? (
                    <AnalysisSteps
                      reasoningPath={path}
                      summary={{
                        action: a,
                        score: p.score ?? null,
                        reason: p.reasoning ?? '',
                        horizon: p.expected_horizon ?? undefined,
                        eventStrength: p.event_strength ?? undefined,
                        directCatalyst: p.direct_catalyst != null ? p.direct_catalyst : undefined,
                        marketConfirmation: p.market_confirmation ?? undefined,
                        heatScore: p.heat_score ?? null,
                        impactScore: p.impact_score ?? null,
                        assetImpact: p.asset_impact ?? null,
                      }}
                    />
                  ) : (
                    <div className="font-mono text-[10px] text-muted-foreground">（该模型未输出分段推理）</div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
