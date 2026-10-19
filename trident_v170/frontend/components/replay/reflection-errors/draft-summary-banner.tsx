'use client'

/**
 * DraftSummaryBanner —— 草稿总览横幅(v302+ PR-15)。
 *
 * 设计动机:用户 LS 里残留多少未提交反思草稿,之前完全不可见。
 * 这个 banner 在 /review 顶部显示,让用户知道"还有 3 个草稿没提交"
 * 以及"哪些 >7 天没动 / 哪些决策有多个错误码草稿"。
 *
 * 设计:
 *  - 仅在 total > 0 时渲染(空时 return null,不留占位)
 *  - 默认折叠,只显示统计摘要(节省纵向空间)
 *  - 展开后按 decisionId 分组列出,每行:决策 ID + 错误码 + savedAt 相对时间 + 删除
 *  - 顶部"清空全部"按钮(防误操作,要二次确认)
 *  - stale(>7天)和 multi-error 用 badge 区分
 */

import { useState } from 'react'
import { useDraftSummary } from './use-draft-summary'

/** 把 ISO 时间格式化成"X 分钟前 / X 天前"等相对字符串 */
function relativeTime(iso?: string): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return '—'
  const diff = Date.now() - t
  if (diff < 0) return '刚刚'
  const min = Math.floor(diff / 60_000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小时前`
  const day = Math.floor(hr / 24)
  if (day < 30) return `${day} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

export function DraftSummaryBanner() {
  const summary = useDraftSummary()
  const [open, setOpen] = useState(false)
  const [confirmingClear, setConfirmingClear] = useState(false)

  if (summary.total === 0) return null

  return (
    <section
      data-testid="draft-summary-banner"
      aria-label="反思草稿总览"
      className="rounded-md border border-amber-500/40 bg-amber-50/40 px-3 py-2 dark:bg-amber-950/20"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[14px]" aria-hidden>📝</span>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-2 text-left font-mono text-[12px] font-semibold"
          aria-expanded={open}
        >
          {summary.total} 个未提交反思草稿
          <span className="text-muted-foreground">
            ({summary.byDecision.length} 决策)
          </span>
        </button>
        {summary.stale.length > 0 && (
          <span
            data-testid="badge-stale"
            className="rounded bg-amber-200 px-1.5 py-0.5 font-mono text-[10px] text-amber-900"
            title="这些草稿超过 7 天没更新了"
          >
            {summary.stale.length} 个 &gt; 7 天
          </span>
        )}
        {summary.multiErrorDecisions.length > 0 && (
          <span
            data-testid="badge-multi-error"
            className="rounded bg-purple-200 px-1.5 py-0.5 font-mono text-[10px] text-purple-900"
            title="同一决策多个错误码各有草稿,可能需要交叉分析"
          >
            {summary.multiErrorDecisions.length} 决策多错误码
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {confirmingClear ? (
            <>
              <span className="font-mono text-[10px] text-amber-900">确认清空全部?</span>
              <button
                type="button"
                onClick={() => {
                  summary.clear()
                  setConfirmingClear(false)
                }}
                className="rounded border border-red-500/60 px-1.5 py-0.5 font-mono text-[10px] text-red-700 hover:bg-red-50"
              >
                是
              </button>
              <button
                type="button"
                onClick={() => setConfirmingClear(false)}
                className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-secondary"
              >
                否
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingClear(true)}
              className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-secondary"
              title="删除所有未提交反思草稿(LS 清空)"
            >
              清空全部
            </button>
          )}
        </div>
      </div>
      {open && (
        <ul
          data-testid="draft-list"
          className="mt-2 space-y-1 border-t border-amber-500/30 pt-2"
        >
          {summary.byDecision.map((g) => (
            <li
              key={g.decisionId}
              data-testid={`draft-row-${g.decisionId}`}
              className="rounded border border-amber-500/20 bg-background/60 px-2 py-1.5"
            >
              <div className="flex flex-wrap items-center gap-2 text-[11px]">
                <span className="font-mono font-semibold">决策 #{g.decisionId}</span>
                <span className="text-muted-foreground">
                  {g.entries.length} 个草稿 · 最早 {relativeTime(g.oldestSavedAt)}
                </span>
                {g.entries.length >= 2 && (
                  <span className="rounded bg-purple-100 px-1 font-mono text-[9px] text-purple-800">
                    多错误码
                  </span>
                )}
                <span className="ml-auto" />
                {g.entries.map((e) => {
                  const isStale = summary.stale.some(
                    (s) => s.decisionId === e.decisionId && s.errorCode === e.errorCode,
                  )
                  return (
                    <span
                      key={e.errorCode}
                      className="inline-flex items-center gap-1 rounded border border-border bg-secondary/50 px-1.5 py-0.5"
                    >
                      <span className="font-mono text-[10px]">{e.errorCode}</span>
                      <span className="text-[9px] text-muted-foreground">
                        {relativeTime(e.draft.savedAt)}
                      </span>
                      {isStale && (
                        <span className="text-[9px] text-amber-700" title=">7天未更新">🔥</span>
                      )}
                      <button
                        type="button"
                        onClick={() => summary.remove(e.decisionId, e.errorCode)}
                        className="text-[9px] text-red-600 hover:underline"
                        title="删除这条草稿"
                        aria-label={`删除 ${e.errorCode} 草稿`}
                      >
                        ×
                      </button>
                    </span>
                  )
                })}
              </div>
              {g.entries.some((e) => e.draft.text?.trim()) && (
                <ul className="mt-1 space-y-0.5 text-[10px] text-muted-foreground">
                  {g.entries
                    .filter((e) => e.draft.text?.trim())
                    .map((e) => (
                      <li key={e.errorCode} className="truncate">
                        <span className="font-mono">{e.errorCode}:</span>{' '}
                        {e.draft.text!.trim().slice(0, 60)}
                        {e.draft.text!.length > 60 ? '…' : ''}
                      </li>
                    ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
