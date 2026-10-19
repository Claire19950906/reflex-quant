'use client'
/**
 * components/v237/MissedSignalsDimensionSummary.tsx — v302+ P0
 *
 * 复用后端 /api/v237/missed-signals/by-dimension (v2026.10.15 已存在, 数据源 trade_reflections_v2,
 * 真实样本 ~163 条). 把"三维度错因"提到主视图:
 *   A 买卖信号判断  (long  色)  — 形态预期/心理预期未达标
 *   B 信息分析方向  (short 色)  — 逻辑框架错 / 因果链倒置
 *   C 技术错误      (默认)     — 数据缺失 / 信号慢 / 接口超时
 *
 * 设计:
 *   - compact: 3 卡片无子分类, 给 LiveDesk 顶部 strip 用
 *   - full:    3 卡片 + top-4 子分类 + desc, 给 attribution 顶部主视图用
 *   - 内部轮询 refreshMs, 不依赖父组件.
 *   - 失败态独立 (不抛错) — 不影响 v237 旧面板渲染.
 */

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { cn } from '@/lib/utils'
import { fetchWithFallback } from '@/lib/api'

type SubInfo = { n: number; hit_rate?: number; avg_confidence?: number }
type DimInfo = {
  label: string
  total: number
  hit: number
  hit_rate: number
  by_subcategory: Record<string, SubInfo>
}
type ByDimResp = {
  ok: boolean
  dimensions?: { A?: DimInfo; B?: DimInfo; C?: DimInfo }
  totals?: { grand_total: number }
  error?: string
}

const DIM_META: Record<'A' | 'B' | 'C', { label: string; color: string; desc: string }> = {
  A: {
    label: '买卖信号判断',
    color: 'text-long',
    desc: '对市场还不够了解 · 心理预期计算与形态预期未达标',
  },
  B: {
    label: '信息分析方向',
    color: 'text-short',
    desc: '方向错误 · 逻辑框架错了 · 因果链倒置',
  },
  C: {
    label: '技术错误',
    color: 'text-foreground',
    desc: '数据缺失 · 信号慢 · 接口超时 · 计算异常',
  },
}

const ORDER: Array<'A' | 'B' | 'C'> = ['A', 'B', 'C']

type Variant = 'compact' | 'full'

export function MissedSignalsDimensionSummary({
  className,
  variant = 'full',
  refreshMs = 60_000,
}: {
  className?: string
  variant?: Variant
  refreshMs?: number
}) {
  const [data, setData] = useState<ByDimResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const fetchIt = async () => {
      try {
        // v2026.10.19 P0 fix: 去掉 /api 前缀 (fetchWithFallback base 已有 /api)
        const j: ByDimResp = await fetchWithFallback('/v237/missed-signals/by-dimension?days=90')
        if (cancelled) return
        setData(j)
        if (j?.ok) {
          setErr(null)
        } else {
          setErr(j?.error ?? 'unknown error')
        }
      } catch (e) {
        if (cancelled) return
        setErr(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void fetchIt()
    const id = setInterval(fetchIt, refreshMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [refreshMs])

  if (loading && !data) {
    return (
      <div
        className={cn(
          'rounded border border-muted-foreground/30 bg-card px-3 py-2 font-mono text-[11px] text-muted-foreground',
          className,
        )}
      >
        加载三维度数据 (trade_reflections_v2)…
      </div>
    )
  }
  if (err && !data?.ok) {
    return (
      <div
        className={cn(
          'rounded border border-short/40 bg-short/10 px-3 py-2 font-mono text-[11px] text-short',
          className,
        )}
      >
        <div className="font-semibold">v302+ P0: 三维度后端未上线 (chat workspace)</div>
        <div className="mt-1 text-short/90">
          /api/v237/missed-signals/by-dimension 错误: {err}
        </div>
        <div className="mt-1 text-muted-foreground/90">
          说明: v2026.10.15 commit 写了前端 fetch, 但 chat 后端从未注册 by-dimension 路由
          (db 也缺 trade_reflections_v2 表). 完整后端在
          <code className="mx-1 rounded bg-short/20 px-1">/Users/claire/Desktop/金融1.0/backend</code>
          (trident.db, ~GB 级, 真实样本 163+ 条). 后端补完后本卡片自动出 A/B/C 数据, 无需前端再改.
        </div>
      </div>
    )
  }

  const dims = data?.dimensions ?? {}
  const total = data?.totals?.grand_total ?? 0
  const isCompact = variant === 'compact'

  return (
    <div className={cn('rounded border border-muted-foreground/30 bg-card p-3', className)}>
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-baseline gap-2">
          <h3 className="font-mono text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            三维度错因 (trade_reflections_v2)
          </h3>
          <span className="font-mono text-[10px] text-muted-foreground/60">
            n={total} · 90d
          </span>
        </div>
        <Link
          href="/attribution#missed"
          className="font-mono text-[10px] text-muted-foreground underline hover:text-foreground"
        >
          {isCompact ? '详细 →' : '打开完整漏检 →'}
        </Link>
      </div>
      <div className={cn('grid gap-2', isCompact ? 'grid-cols-3' : 'grid-cols-1 sm:grid-cols-3')}>
        {ORDER.map((k) => {
          const d = dims[k]
          const meta = DIM_META[k]
          if (!d) {
            return (
              <div key={k} className="rounded border border-muted-foreground/20 bg-muted/10 p-2">
                <div className={cn('font-mono text-[10px] font-semibold', meta.color)}>
                  {k}. {meta.label}
                </div>
                <div className="mt-1 font-mono text-[10px] text-muted-foreground/60">无数据</div>
              </div>
            )
          }
          const subs = d.by_subcategory || {}
          const topSubs = Object.entries(subs).slice(0, isCompact ? 0 : 4)
          return (
            <div key={k} className="rounded border border-muted-foreground/20 bg-muted/10 p-2">
              <div className="flex items-center justify-between">
                <div className={cn('font-mono text-[10px] font-semibold', meta.color)}>
                  {k}. {meta.label}
                </div>
                <div className="font-mono text-[10px] text-muted-foreground">
                  n={d.total} · hit={Math.round((d.hit_rate ?? 0) * 100)}%
                </div>
              </div>
              {!isCompact ? (
                <div className="mt-0.5 font-mono text-[9px] text-muted-foreground/70">{meta.desc}</div>
              ) : null}
              {topSubs.length > 0 ? (
                <div className="mt-1 flex flex-col gap-0.5 font-mono text-[10px]">
                  {topSubs.map(([sub, info]) => (
                    <div key={sub} className="flex items-center justify-between">
                      <span className="truncate text-foreground/80" title={sub}>
                        {sub}
                      </span>
                      <span className="text-muted-foreground">{info?.n ?? 0}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

