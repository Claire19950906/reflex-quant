'use client'

/**
 * useFlashTimer — 数据"心跳"高亮定时器（v170 P0+ 反思页统一）
 *
 * 设计动机：review-overview.tsx 等多处直接调用 `window.setTimeout(...)` 延迟把
 * flash key 置回 false，存在三类问题：
 *   ❶ 组件 unmount 后回调仍触发 → React 18 setState 警告
 *   ❷ 数字频繁变化 → 旧定时器未清理，高亮时长被叠加延长
 *   ❸ "开"和"关"在两处调用，难保证对应正确
 *
 * 本 hook 只负责"关"这一侧：把指定 key 在 durationMs 后置回 false；同一 key 在
 * 重新触发时，旧定时器会被新定时器自然接管，不存在叠加问题。
 *
 * 用法（典型场景 "数字变了 → 闪一下 → 自动复原"）：
 *   const [flash, setFlash] = useState<{ a: boolean; b: boolean }>({a:false, b:false})
 *   const flashOnFor = useFlashTimer(setFlash, FLASH_MS)
 *
 *   if (aChanged) {
 *     setFlash((f) => (f.a ? f : { ...f, a: true }))   // ① 点亮（去重，避免重复 setState）
 *     flashOnFor('a')                                    // ② 700ms 后自动熄灭
 *   }
 *
 * 多个 key 同时熄灭：
 *   flashOnFor(['a', 'b'])
 *
 * 设计要点：
 *   - 每 key 独立跟踪 setTimeout id（Map<key, id>）：重新触发时新定时器覆盖旧定时器
 *   - unmount 时统一清理所有挂起的 timeout → 防止 setState on unmounted
 *   - 保持稳定的 useCallback 引用，避免下游 useEffect 误触发
 */

import { useCallback, useEffect, useRef } from 'react'

type FlashSetter<T extends Record<string, boolean>> = React.Dispatch<React.SetStateAction<T>>

export function useFlashTimer<T extends Record<string, boolean>>(
  setFlash: FlashSetter<T>,
  durationMs: number,
): (keys: keyof T | Array<keyof T>) => void {
  // key → 当前挂起的 setTimeout id。新定时器覆盖旧定时器。
  // 用 number：因为 hook 一律用 window.setTimeout 创建 + window.clearTimeout 清理，
  // 它们的签名在浏览器 DOM 下都是 number（@types/node 全局 setTimeout 返回 NodeJS.Timeout，
  // 在浏览器环境不用，避免类型歧义）。
  const timersRef = useRef<Map<keyof T, number>>(new Map())

  // unmount 时统一清理所有挂起的 timeout —— 防止 setState on unmounted warning
  useEffect(() => {
    const timers = timersRef.current
    return () => {
      timers.forEach((id) => window.clearTimeout(id))
      timers.clear()
    }
  }, [])

  return useCallback(
    (keys: keyof T | Array<keyof T>) => {
      const arr = Array.isArray(keys) ? keys : [keys]
      const timers = timersRef.current
      for (const k of arr) {
        // 取消该 key 上挂着的旧定时器（防止叠加延长高亮）
        const prev = timers.get(k)
        if (prev !== undefined) window.clearTimeout(prev)
        // 新定时器：durationMs 后把该 key 置回 false（已为 false 时去重，保持 prev 引用）
        const id = window.setTimeout(() => {
          setFlash((s) => (s[k] ? { ...s, [k]: false as T[keyof T] } : s))
          timers.delete(k)
        }, durationMs)
        timers.set(k, id)
      }
    },
    [setFlash, durationMs],
  )
}
