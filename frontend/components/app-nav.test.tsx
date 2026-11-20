/**
 * components/app-nav.test.tsx — 主导航条
 *
 * 历史口径（v339 P2）：「资金流向」「行情扫描」只是 /discover 的
 * #disc-flows / #disc-cross 锚点（各自 307 跳转），曾被误做成独立 tab。
 *
 * 当前口径：
 *   - 「资金流向」已从锚点提升为独立页 /capital-flow（FlowsPanel 实装），
 *     导航应保留为真实入口。
 *   - 「行情扫描」仍是 /discover#disc-cross 的 307 占位，导航不设独立 tab。
 * 路由与锚点保留，旧书签可用。
 */

import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

vi.mock('next/navigation', () => ({
  usePathname: () => '/',
}))

vi.mock('next/link', () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>{children}</a>
  ),
}))

import { AppNav } from './app-nav'

afterEach(() => cleanup())

describe('AppNav', () => {
  it('渲染主导航', () => {
    render(<AppNav />)
    const nav = screen.getByRole('navigation', { name: /主视图/ })
    expect(nav).toBeInTheDocument()
  })

  it('锚点占位段不冒充独立 tab；已提升的独立页保留入口', () => {
    render(<AppNav />)
    // 行情扫描仍是 /discover#disc-cross 的 307 占位，导航不设独立 tab
    expect(screen.queryByRole('link', { name: /行情扫描/ })).toBeNull()
    // 资金流向已提升为独立页 /capital-flow，导航应保留真实入口
    expect(screen.getByRole('link', { name: /资金流向/ })).toHaveAttribute('href', '/capital-flow')
  })

  it('保留真正的独立页面入口', () => {
    render(<AppNav />)
    expect(screen.getByRole('link', { name: /发现/ })).toHaveAttribute('href', '/discover')
    expect(screen.getByRole('link', { name: /新闻流/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /行情归因/ })).toBeInTheDocument()
  })
})
