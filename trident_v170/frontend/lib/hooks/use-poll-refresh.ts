'use client'

import { useEffect, useRef } from 'react'

/**
 * usePollRefresh —— 周期性触发 refresh 函数 + 组件卸载安全
 *
 * 替代散落在 /review 7 个面板里的「useEffect + setInterval + alive flag」模板：
 *   useEffect(() => {
 *     let alive = true
 *     void reload()
 *     const t = setInterval(() => alive && void reload(), 60_000)
 *     return () => { alive = false; clearInterval(t) }
 *   }, [reload])
 *
 * 与 useApiPoll 的关系：useApiPoll 是「fetch + state + error」一体化；
 * usePollRefresh 只管「多久触发一次回调」，对已经有复杂本地 load() 逻辑
 * （空状态/错误分流、filterCode/filterOrigin 拼接）的面板更安全。
 *
 * 关键点：
 *   - 内置 mounted flag，组件 unmount 后即使 timer 触发也不再调用 refresh
 *   - refresh 通过 ref 传递，函数引用变化不重置 interval（避免反复 clear/set）
 *   - deps 变化时立即重拉一次（不等 period），等价于 useApiPoll 的 deps 行为
 */
export function usePollRefresh(
  refresh: () => void | Promise<void>,
  intervalMs: number,
  deps: ReadonlyArray<unknown> = [],
  enabled: boolean = true,
): void {
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh
  useEffect(() => {
    if (!enabled) return
    let mounted = true
    void refreshRef.current()
    const timer = window.setInterval(() => {
      if (mounted) void refreshRef.current()
    }, intervalMs)
    return () => {
      mounted = false
      window.clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled, ...deps])
}