'use client'

import { cn } from '@/lib/utils'

// v170 P0+ 巨型组件拆分：该块纯渲染，无外部依赖，仅做编排
// v240+ Evidence badges — 展示 reflection_tournament / Bayes / BOCPD 增强信号
// evidence_refs 来自 blindspot_reflector._synthesize_tournament_json
// 字段示例:
//   _tournament_quorum: 2 | 3
//   _tournament_verifier_ids: ["v1","v2","v3"]
//   _source: "tournament" | "single"
//   _bayes_p_curr_lt_base: true | false
//   _bocpd_fresh_run_prob: 0.0-1.0
export function EvidenceBadges({ refs }: { refs: any }) {
  if (!refs || typeof refs !== 'object') return null
  const badges: { key: string; label: string; title: string; color: string }[] = []

  if (refs._source === 'tournament') {
    const q = refs._tournament_quorum
    const vids = Array.isArray(refs._tournament_verifier_ids)
      ? refs._tournament_verifier_ids.join('/') : '?'
    badges.push({
      key: 't',
      label: `🎯 quorum=${q}/3 [${vids}]`,
      title: `本条经 k=3 LLM verifier 投票, ${q}/3 同意才 confirm`,
      color: 'border-primary/40 bg-primary/10 text-primary',
    })
  } else if (refs._source === 'single') {
    badges.push({
      key: 's',
      label: '⚙️ single LLM',
      title: '本条由单次 LLM 调用产出（未启用 Tournament）',
      color: 'border-border bg-secondary/30 text-muted-foreground',
    })
  }
  if (refs._bayes_p_curr_lt_base === true) {
    badges.push({
      key: 'b',
      label: '📉 Bayes 真实胜率<基线',
      title: `Beta-Binomial 后验判定: p(curr) < base - delta (alpha=${refs._bayes_alpha ?? '?'}, beta=${refs._bayes_beta ?? '?'})`,
      color: 'border-warn/40 bg-warn/10 text-warn',
    })
  }
  if (typeof refs._bocpd_fresh_run_prob === 'number' && refs._bocpd_fresh_run_prob >= 0.5) {
    badges.push({
      key: 'c',
      label: `🔻 突变 p=${refs._bocpd_fresh_run_prob.toFixed(2)}`,
      title: 'Bayesian Online Change-Point: 信号分布发生突变',
      color: 'border-destructive/40 bg-destructive/10 text-destructive',
    })
  }
  if (!badges.length) return null
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1">
      {badges.map(b => (
        <span key={b.key} title={b.title}
          className={cn('inline-flex items-center rounded border px-1 py-0.5 font-mono text-[9px]', b.color)}>
          {b.label}
        </span>
      ))}
    </div>
  )
}

