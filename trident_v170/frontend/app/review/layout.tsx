/**
 * app/review/layout.tsx — /review 路由 layout 段(v302+ P3++)
 *
 * 角色:只为覆盖 <title> + <meta name="description">,更具体的路由级元信息。
 *  - 浏览器标签页 / 书签 / SR 首次进入页面读的都是 <title>
 *  - 不覆盖根 layout 的 <body> / <html> 标签,只追加 metadata
 *  - children 直接渲染,不包额外 DOM(不会和 page.tsx 的 <main> 嵌套)
 *
 * 注:根 layout 已经设了 'Trident Quant Platform',这里把它收窄到 '/review'。
 */
import type { Metadata } from 'next'
import type { ReactNode } from 'react'

export const metadata: Metadata = {
  title: '复盘 — Trident',
  description:
    '复盘系统:历史真实交易复盘(A)与快速评测复盘(B),按 mode 查看诊断 / 归因 / 推理 / 持股。',
}

export default function ReviewLayout({ children }: { children: ReactNode }) {
  return <>{children}</>
}
