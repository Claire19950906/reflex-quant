'use client'

/**
 * v302+ P6: /review 折叠状态 hook — 单一 URL 参数 ?collapse=id1,id2 管 7 段 section 展开/折叠。
 *
 * 设计要点:
 *  - 单参 / 逗号分隔(而非多键): section 数 ≤ 10,逗号分隔比 N 个 ?collapsed_X=1 干净
 *  - 纯函数化解析 / 序列化(readFromUrl / applyPatch) — 与 useReviewUrlState 同模式
 *  - 写入走 router.replace(scroll:false) — 不让用户点 "折叠" 时页面被滚到顶部
 *  - 不在内部 useState 镜像:URL 即 source of truth,所有响应靠 useSearchParams 触发
 *  - 默认所有 section 展开:首次进 /review 看到完整内容,用户主动折叠后 URL 持久化
 *  - 不污染 _inner 的 SECTION_MATRIX / REVIEW_SECTIONS:这是个并行关注点
 *
 * URL 形态:
 *   /review                              → 全部展开
 *   /review?collapse=review              → 只折叠 review 段
 *   /review?collapse=reasoning,dataHealth → 折叠两段
 *
 * 注意:SectionKey 是从 _spec 派生的字符串字面量联合,这里用 string 接收以避免循环依赖。
 * 调用方负责传合法的 section.id,非法 id 会被静默忽略(读时过滤,写时容错)。
 */

import { useCallback, useMemo } from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'

export const COLLAPSE_PARAM = 'collapse'
const DELIM = ','

/** 纯函数: 从 URLSearchParams 读出折叠的 id 集合。 */
export function readCollapsedFromUrl(sp: URLSearchParams | null | undefined): ReadonlySet<string> {
  const raw = sp?.get(COLLAPSE_PARAM) ?? ''
  if (!raw) return new Set()
  return new Set(
    raw
      .split(DELIM)
      .map((s) => s.trim())
      .filter(Boolean),
  )
}

/** 纯函数: 把当前折叠集合序列化为 URL patch。 */
export function collapseToUrlPatch(
  collapsed: ReadonlySet<string>,
): ReadonlyArray<readonly [string, string | null]> {
  if (collapsed.size === 0) {
    return [[COLLAPSE_PARAM, null]]
  }
  return [[COLLAPSE_PARAM, Array.from(collapsed).join(DELIM)]]
}

/**
 * /review 折叠状态 hook。返回:
 *  - collapsed: Set<string> 当前折叠的 section id
 *  - isCollapsed(id): 该 id 是否折叠
 *  - toggle(id): 翻转该 id
 *  - expandAll(): 全部展开(URL 清空 collapse 键)
 *  - collapseAll(ids): 全部折叠(传入完整 id 列表)
 *
 * 用法:
 *   const { isCollapsed, toggle, expandAll, collapseAll } = useReviewCollapse()
 *   <CollapsibleSection sectionId="review" collapsed={isCollapsed('review')} onToggle={() => toggle('review')}>
 */
export function useReviewCollapse() {
  const sp = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()

  const collapsed = useMemo(() => readCollapsedFromUrl(sp), [sp])

  const writeCollapsed = useCallback(
    (next: ReadonlySet<string>) => {
      const cur = new URLSearchParams(sp?.toString() ?? '')
      const patch = collapseToUrlPatch(next)
      for (const [k, v] of patch) {
        if (v == null || v === '') cur.delete(k)
        else cur.set(k, v)
      }
      const qs = cur.toString()
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false })
    },
    [sp, router, pathname],
  )

  const isCollapsed = useCallback((id: string) => collapsed.has(id), [collapsed])

  const toggle = useCallback(
    (id: string) => {
      const next = new Set(collapsed)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      writeCollapsed(next)
    },
    [collapsed, writeCollapsed],
  )

  const expandAll = useCallback(() => writeCollapsed(new Set()), [writeCollapsed])

  const collapseAll = useCallback(
    (allIds: readonly string[]) => writeCollapsed(new Set(allIds)),
    [writeCollapsed],
  )

  return useMemo(
    () => ({ collapsed, isCollapsed, toggle, expandAll, collapseAll }),
    [collapsed, isCollapsed, toggle, expandAll, collapseAll],
  )
}
