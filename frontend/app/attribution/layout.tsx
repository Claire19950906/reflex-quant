/**
 * app/attribution/layout.tsx — 行情归因 路由 layout 段
 *
 * page.tsx 是 'use client'，不能直接导出 metadata；标题集中在本 layout 覆盖。
 * 不包额外 DOM，children 直接渲染。
 */
import type { Metadata } from 'next'
import type { ReactNode } from 'react'

export const metadata: Metadata = { title: '行情归因 · Trident' }

export default function AttributionLayout({ children }: { children: ReactNode }) {
  return <>{children}</>
}
