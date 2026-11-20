/**
 * app/divergence/layout.tsx — 背离雷达 路由 layout 段
 *
 * page.tsx 是 'use client'，不能直接导出 metadata；标题集中在本 layout 覆盖。
 * 不包额外 DOM，children 直接渲染。
 */
import type { Metadata } from 'next'
import type { ReactNode } from 'react'

export const metadata: Metadata = { title: '背离雷达 · Trident' }

export default function DivergenceLayout({ children }: { children: ReactNode }) {
  return <>{children}</>
}
