'use client'

import { Loader2 } from 'lucide-react'
import { usePoll } from '@/lib/hooks/use-poll'
import { PushToCausalChainButton } from './push-to-causal-chain-button'
import { cn } from '@/lib/utils'

/* ════════════════════════════════════════════════════════════════════════
 * 跨资产异常 · 形态归因 — v2026-10-08 接通 /api/cross-asset/anomaly
 *
 * 数据流:
 *   /api/cross-asset/anomaly → macro_regime + classes_aligned + is_anomaly + severity
 *   本面板根据 macro_regime 高亮主导归因,其它次要驱动保持静态参考。
 * ════════════════════════════════════════════════════════════════════════ */

type AnomalyResponse = {
  ok: boolean
  macro_regime?: string
  classes_aligned?: number
  classes_total?: number
  is_anomaly?: boolean
  severity?: string
  direction?: string
  message?: string
  snapshot_ts?: string
}

const REGIME_DRIVER: Record<string, { icon: string; name: string; hint: string; tone: string }> = {
  BULL:     { icon: '🐂', name: '风险偏好 · 普涨',  hint: '行业同向上行 — 主导驱动通常是流动性宽松 / 降息预期 / 盈利改善', tone: 'emerald' },
  BEAR:     { icon: '🐻', name: '风险偏好 · 普跌',  hint: '行业同向下跌 — 主导驱动通常是流动性收紧 / 衰退预期 / 避险', tone: 'rose' },
  RISK_ON:  { icon: '🟢', name: 'Risk-On · 风险偏好回升', hint: '股票/信用利差上行 + 国债收益率上行 — Fed 鹰派或增长强韧', tone: 'emerald' },
  RISK_OFF: { icon: '🔴', name: 'Risk-Off · 风险偏好回落', hint: '股票/信用利差下行 + 国债收益率下行 — Fed 鸽派或衰退担忧', tone: 'rose' },
  MIXED:    { icon: '🌐', name: '分化 · 无统一方向', hint: '行业不同向 — 没有单一主导驱动;按行业基本面拆解', tone: 'slate' },
  UNKNOWN:  { icon: '❓', name: '暂无数据',         hint: '等待 anomaly 引擎产出', tone: 'slate' },
}

export function CrossAssetMetaPanel() {
  // v283+ · Task D · 5min 轮询 · AbortController + unmount 清理交给 usePoll
  const { data, loading } = usePoll<AnomalyResponse>('/api/cross-asset/anomaly', {
    intervalMs: 5 * 60 * 1000,
    // 后端无 ok 字段时也接受(老版本兼容);新版有 ok 时取 ok=true 的数据
    transform: (raw) => {
      const d = raw as AnomalyResponse
      if (d && typeof d === 'object' && 'ok' in d && d.ok === false) return null
      return d
    },
  })

  const regime = (data?.macro_regime || 'UNKNOWN').toUpperCase()
  const driver = REGIME_DRIVER[regime] || REGIME_DRIVER.UNKNOWN
  const toneCls =
    driver.tone === 'emerald'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
      : driver.tone === 'rose'
        ? 'border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300'
        : 'border-slate-500/40 bg-slate-500/10 text-slate-700 dark:text-slate-300'

  return (
    <div className="rounded border border-rose-500/30 bg-white p-3 text-sm dark:bg-slate-900">
      <div className="mb-2 flex items-center justify-between gap-2 font-semibold">
        <span>🌐 跨资产异常 · 形态归因</span>
        {loading && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
      </div>

      <div className={cn('mb-2 rounded border p-2 text-[11px]', toneCls)}>
        <div className="flex items-center gap-1.5 font-bold">
          <span className="text-base">{driver.icon}</span>
          <span>{driver.name}</span>
          {data?.classes_aligned !== undefined && (
            <span className="ml-auto rounded border border-current/40 px-1 py-0.5 text-[9px] font-mono">
              {data.classes_aligned}/{data.classes_total ?? 5} 行业同向
              {data?.is_anomaly && ' · 异常'}
            </span>
          )}
        </div>
        <div className="mt-0.5 opacity-90">{driver.hint}</div>
        {data?.message && (
          <div className="mt-1 text-[10px] opacity-75">{data.message}</div>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        复用左侧 <code className="rounded bg-secondary px-1">cross-asset-anomaly-panel</code> 检测结果，
        但这里回答「<strong className="text-foreground">为什么会这样</strong>」——
        也就是「k 线形态差不多」背后的驱动因素拆解。
      </p>

      <div className="mt-3 space-y-2 text-xs">
        <CauseRow icon="🏛" name="Fed 路径异动" desc="2Y UST 24h 移动 >10bp → 几乎所有风险资产同向，只是方向正反不同" link="#disc-fed" highlight={regime === 'RISK_ON' || regime === 'RISK_OFF'} />
        <CauseRow icon="💵" name="美元指数 DXY" desc="DXY 强势 → EM 资产、新兴市场货币承压；DXY 弱 → 反向" link="#disc-fed" />
        <CauseRow icon="📈" name="VIX 风险偏好" desc="VIX >25 → 风险资产普跌 + 避险债/黄金普涨" highlight={data?.severity === 'high'} />
        <CauseRow icon="🌏" name="时区切换" desc="美股收盘 → 亚盘接力，信息传导有时间差；主力盘切换时波动放大" link="#disc-collab" />
        <CauseRow icon="📰" name="重大新闻冲击" desc="突发宏观/地缘 → 1-2 小时内多品种同涨同跌（k线形态高度一致）" link="#disc-event" />
      </div>

      {/* v283+ · #23/#31 · 把当前 cross-asset 形态归因一键投到因果链 */}
      {data?.macro_regime && data.macro_regime !== 'UNKNOWN' && (
        <div className="mt-3 flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">当前形态:</span>
          <PushToCausalChainButton origin="manual_from_cross_asset_meta"

            cause={`跨资产同步 · regime=${regime}`}
            effect={`${data.classes_aligned ?? 0}/${data.classes_total ?? 5} 行业同向`}
            note={`cross-asset-meta severity=${data.severity ?? 'n/a'} direction=${data.direction ?? 'n/a'} message=${data.message ?? ''}`}
            trigger_words={`cross-asset,${regime},${data.severity ?? ''}`}
            weight={0.6}
            label="⭐ 入候选"
          />
        </div>
      )}

      <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/5 p-2 text-[11px]">
        <strong className="text-amber-900 dark:text-amber-200">💡 4 行业同向的「真实驱动」常是其中 1-2 个，不是全部同向同源</strong>。
        反思系统用 <code className="rounded bg-background px-1">reflection_ontology.py::rule_33</code>（cross_asset_chain）
        自动检查，但「先有结论再找证据」的偏差需要 AI 元认知（见下方）补救。
      </div>
    </div>
  )
}

function CauseRow({ icon, name, desc, link, highlight }: { icon: string; name: string; desc: string; link?: string; highlight?: boolean }) {
  return (
    <div className={cn(
      'flex items-start gap-2 rounded border p-2',
      highlight
        ? 'border-amber-500/40 bg-amber-500/10'
        : 'border-border bg-secondary/20',
    )}>
      <span aria-hidden className="text-base">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[11px] font-bold text-foreground">
          {name}
          {highlight && <span className="ml-1 text-[9px] text-amber-600 dark:text-amber-400">· 当前主导</span>}
        </div>
        <div className="text-[11px] text-muted-foreground">{desc}</div>
      </div>
      {link && (
        <a href={link} className="shrink-0 self-start rounded border border-primary/40 px-1.5 py-0.5 font-mono text-[9px] text-primary hover:bg-primary/10">
          跳 →
        </a>
      )}
    </div>
  )
}




