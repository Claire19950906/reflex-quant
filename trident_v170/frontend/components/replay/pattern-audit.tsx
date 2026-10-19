'use client'

// ────────────────────────────────────────────────────────────────────────
// v233: 「规律发现」深入自审（递归"为什么"直到问不动为止）。
// 后端 review_agent._audit_patterns_batch 生成，前端只渲染。
// 注意：所有字段都是可选的 —— fallback / 老数据 / LLM 不可用时可能是 undefined。
//
// 渲染：claim 头 + 置信度徽章 + 适用/反例/过拟合 + 证据 + why_chain。
//
// v233+ 修订（用户反馈 2026-10-09）：
//   自审主线从「why / 适用 / 是否过拟合 / 引用注意」收紧为「涨跌逻辑」：
//     - 后端 prompt：系统提示词改为"涨跌逻辑自审者"，并新增 5/6 两条约束
//       （强制追问锚定驱动因子 / 因果链 / 反例；明确禁止哲学/历史溯源/纯统计发散）；
//     - 第一问从"为什么规律成立"改为"涨跌驱动因子是什么"；
//     - fallback caution 文案同步指向"驱动因子 + 反例"复核。
//   前端对应：标题改"涨跌逻辑自审"；why_chain 折叠标改"涨跌逻辑链"；
//   "反条件" 改 "反例（涨跌失效场景）"；"过拟合风险" 加 "(aux)"。
//   目的：让审计重心回归金融产品涨跌逻辑；过拟合 / 引用注意作为辅助而非主线。
//
// 从 review-panel.tsx 拆出（v170 P0+ 巨型组件拆分）：
//   本块本身逻辑闭环（数据 → 渲染，无外部副作用），且体积较大（~200 行）。
//   拆出后 review-panel.tsx 仅做编排，不再持有这一大块纯渲染逻辑。
// ────────────────────────────────────────────────────────────────────────

export type PatternAudit = {
  claim: string
  factual?: boolean | null
  evidence?: string[]
  conditions_apply?: string[]
  conditions_fail?: string[]
  overfit_risks?: string[]
  confidence?: number | null  // 0-1；null = LLM 未生成或不可用
  caution?: string
  why_chain?: Array<{ question: string; answer: string }>
  depth_reached?: number
  stop_reason?:
    | 'no_further_why'
    | 'max_depth'
    | 'contradicted_by_evidence'
    | 'no_llm'
    | 'llm_failed'
    | 'empty_input'
    | 'parse_failed'
    | string
  fabricated_evidence?: string[]  // 引用了不存在 #id 的 evidence 条目
}

const STOP_REASON_LABEL: Record<string, string> = {
  no_further_why: '已自审至"问不动"',
  max_depth: '已达深度上限',
  contradicted_by_evidence: '被本期证据反驳',
  no_llm: 'LLM 未参与（仅启发式骨架）',
  llm_failed: 'LLM 调用失败',
  empty_input: '输入为空',
  parse_failed: 'LLM 输出无法解析',
}

function confidenceBadge(c: number | null | undefined) {
  if (c === null || c === undefined) {
    return { label: '置信度未生成', cls: 'border-muted bg-muted text-muted-foreground' }
  }
  if (c >= 0.7) return { label: `高 ${(c * 100).toFixed(0)}%`, cls: 'border-long/50 bg-long/10 text-long' }
  if (c >= 0.4) return { label: `中 ${(c * 100).toFixed(0)}%`, cls: 'border-amber-500/50 bg-amber-500/10 text-amber-500' }
  return { label: `低 ${(c * 100).toFixed(0)}%`, cls: 'border-short/50 bg-short/10 text-short' }
}

export function PatternAuditItem({ audit }: { audit: PatternAudit }) {
  const conf = confidenceBadge(audit.confidence)
  const stopLabel = audit.stop_reason ? (STOP_REASON_LABEL[audit.stop_reason] || audit.stop_reason) : ''
  const fabricated = (audit.fabricated_evidence || []).length > 0
  const contradiction = audit.stop_reason === 'contradicted_by_evidence'
  const noLLM = audit.stop_reason === 'no_llm' || audit.stop_reason === 'llm_failed' || audit.stop_reason === 'parse_failed'
  const chain = audit.why_chain || []
  const conditionsApply = audit.conditions_apply || []
  const conditionsFail = audit.conditions_fail || []
  const overfitRisks = audit.overfit_risks || []
  const evidence = audit.evidence || []

  return (
    <div className="rounded border border-border bg-background/60 p-2">
      {/* claim + 置信度徽章 + 停止原因 */}
      <div className="flex flex-wrap items-start gap-2">
        <div className="flex-1 text-[11px] leading-relaxed text-foreground">{audit.claim}</div>
        <div className="flex shrink-0 flex-wrap gap-1">
          <span className={`rounded border px-1.5 py-px font-mono text-[9px] ${conf.cls}`}>{conf.label}</span>
          {stopLabel && (
            <span className="rounded border border-border bg-secondary px-1.5 py-px font-mono text-[9px] text-muted-foreground">
              {stopLabel}
              {typeof audit.depth_reached === 'number' ? ` Â· æ·±åº¦${audit.depth_reached}` : ''}
            </span>
          )}
          {fabricated && (
            <span
              className="rounded border border-short/50 bg-short/15 px-1.5 py-px font-mono text-[9px] text-short"
              title="evidence ä¸­å¼ç¨äºä¸å­å¨ ç #idï¼å·²å¼ºå¶åä½ç½®ä¿¡åº¦"
            >
              å¼ç¨å­ç
            </span>
          )}
        </div>
      </div>

      {/* 醒目 caution */}
      {audit.caution && (
        <div
          className={`mt-1.5 rounded border px-2 py-1 text-[10px] leading-relaxed ${
            contradiction
              ? 'border-short/50 bg-short/10 text-short'
              : fabricated
                ? 'border-short/40 bg-short/5 text-short'
                : noLLM
                  ? 'border-amber-500/40 bg-amber-500/5 text-amber-500'
                  : 'border-border bg-secondary text-muted-foreground'
          }`}
        >
          {audit.caution}
        </div>
      )}

      {/* 适用 / 反例 / 过拟合 */}
      {(conditionsApply.length > 0 || conditionsFail.length > 0 || overfitRisks.length > 0) && (
        <div className="mt-1.5 grid grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-3">
          {conditionsApply.length > 0 && (
            <div>
              <div className="font-mono text-[9.5px] uppercase tracking-wide text-long">适用</div>
              <ul className="mt-0.5 space-y-0.5 text-foreground">
                {conditionsApply.map((c, i) => <li key={i}>• {c}</li>)}
              </ul>
            </div>
          )}
          {conditionsFail.length > 0 && (
            <div>
              <div className="font-mono text-[9.5px] uppercase tracking-wide text-short">反例\uff08涨跌失效场景\uff09</div>
              <ul className="mt-0.5 space-y-0.5 text-foreground">
                {conditionsFail.map((c, i) => <li key={i}>• {c}</li>)}
              </ul>
            </div>
          )}
          {overfitRisks.length > 0 && (
            <div>
              <div className="font-mono text-[9.5px] uppercase tracking-wide text-amber-500">过拟合风险\uff08辅助\uff09</div>
              <ul className="mt-0.5 space-y-0.5 text-foreground">
                {overfitRisks.map((c, i) => <li key={i}>• {c}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* 輝重引用\uff1afabricated 的条目画删除线 + 标注\uff1b非伪造 #id 链接到 /history?id=N */}
      {evidence.length > 0 && (
        <details className="mt-1.5">
          <summary className="cursor-pointer select-none font-mono text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground">
            è¼éå¼ç¨（{evidence.length} æ¡）
          </summary>
          <ul className="mt-1 space-y-0.5 text-[10.5px] leading-relaxed text-foreground">
            {evidence.map((e, i) => {
              const isFab = (audit.fabricated_evidence || []).includes(e)
              // 把 #<digits> 切成 text + link 两段\uff1b其他纯字符串原样显示
              const parts = e.split(/(#\d+)/g)
              return (
                <li key={i} className={isFab ? 'text-short line-through' : 'text-foreground'}>
                  •{" "}
                  {parts.map((p, j) => {
                    const m = /^#(\d+)$/.exec(p)
                    if (m && !isFab) {
                      return (
                        <a
                          key={j}
                          href={`/history?id=${m[1]}`}
                          className="rounded px-0.5 font-mono text-[10px] text-primary hover:underline"
                          title={`è·³è½¬å°åå²è¯¦æ #${m[1]}`}
                        >
                          {p}
                        </a>
                      )
                    }
                    return <span key={j}>{p}</span>
                  })}
                  {isFab && <span className="ml-1 text-[9px]">\uff08引用了不存在的 #id\uff09</span>}
                </li>
              )

            })}
          </ul>
        </details>
      )}

      {/* why_chain\uff1a默认折叠 — 点开看问答链 — 命名为"涨跌逻路链"以与自审主线对齐 */}
      {chain.length > 0 && (
        <details className="mt-1.5">
          <summary className="cursor-pointer select-none font-mono text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground">
            æ¶¨è·é»è·¯é¾（{chain.length} é®{typeof audit.depth_reached === 'number' ? ` Â· æ·±åº¦ ${audit.depth_reached}` : ''}）
          </summary>
          <ol className="mt-1 space-y-1 text-[10.5px] leading-relaxed">
            {chain.map((q, i) => (
              <li key={i}>
                <div className="font-mono text-[9.5px] uppercase tracking-wide text-muted-foreground">é® {i + 1}</div>
                <div className="text-foreground">{q.question}</div>
                <div className="mt-0.5 font-mono text-[9.5px] uppercase tracking-wide text-muted-foreground">ç­</div>
                <div className="text-foreground">{q.answer}</div>
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  )
}

export function PatternAuditBlock({ audits }: { audits?: PatternAudit[] }) {
  if (!audits || audits.length === 0) return null
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          è§å¾åç°Â·æ¶¨è·é»è·¯èªå®¡（é©±å¨ / éç¨ / åä¾ / è¿æå Â· å¼ç¨æ³¨æ）
        </span>
      </div>
      <p className="mt-0.5 font-mono text-[10px] leading-relaxed text-muted-foreground">
        ä¸»çº¿：éèäº§åæ¶¨è·çé©±å¨å å­ä¸ç½°æé¾；å¶ä»å ç´ （è¿æå / å¼ç¨æ³¨æ）ä½ä¸ºè¾å©，ä¸å§å®¾å¤ºä¸»ã
      </p>
      <div className="mt-1 space-y-1.5">
        {audits.map((a, i) => (
          <PatternAuditItem key={i} audit={a} />
        ))}
      </div>
    </div>
  )
}
