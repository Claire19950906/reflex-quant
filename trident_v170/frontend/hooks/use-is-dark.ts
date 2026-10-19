'use client'

import { useSyncExternalStore } from 'react'

// ─────────────────────────────────────────────────────────────────────────────
// v2029.1 修复：NotFoundError: Failed to execute 'insertBefore' on 'Node'
//
// 旧实现 `useState(false)` → `useEffect(sync)` 会产生一次「false → 真实值」的二次渲染。
// 在主题为深色（系统 prefers-color-scheme: dark 或 localStorage 'dark'）时：
//   1. 首屏渲染时所有 chart effect 用浅色 createChart
//   2. useEffect 跑完，sync() 读到 dark class → setDark(true)
//   3. React 触发 re-render → 旧 chart 被同步 chart.remove()，新 chart 立即重建
//   4. lightweight-charts 内部 ResizeObserver/MutationObserver 异步回调与 React 18
//      commitPlacement 并发执行，insertBefore 的参考节点被 chart.remove() 提前摘走
//   5. → NotFoundError: 要插入新节点的节点不是此节点的子节点
//
// 改用 useSyncExternalStore：serverSnapshot=false（SSR 一致），clientSnapshot 同步读
// <html> 的 dark class —— 首屏就拿到正确状态，不再有二次渲染重建 chart。
// ─────────────────────────────────────────────────────────────────────────────

function subscribeDark(callback: () => void): () => void {
  if (typeof document === 'undefined') return () => {}
  const root = document.documentElement
  const observer = new MutationObserver(callback)
  observer.observe(root, { attributes: true, attributeFilter: ['class'] })
  return () => observer.disconnect()
}

function getDarkSnapshot(): boolean {
  return document.documentElement.classList.contains('dark')
}

function getServerDarkSnapshot(): boolean {
  // SSR 阶段服务端没有真实 DOM，对齐客户端首帧以避免 hydration mismatch；
  // useSyncExternalStore 会在客户端 hydrate 后立即触发一次 re-render 切换到真实快照。
  return false
}

/** Tracks the `dark` class on <html> so canvas/iframe charts can restyle themselves. */
export function useIsDark(): boolean {
  return useSyncExternalStore(subscribeDark, getDarkSnapshot, getServerDarkSnapshot)
}
