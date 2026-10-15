/**
 * components/app-nav.test.tsx — 主导航条 (v302+ P3 加入 market-scanner)
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
  it('渲染主导航 + 行情扫描入口', () => {
    render(<AppNav />)
    const nav = screen.getByRole('navigation', { name: /主视图/ })
    expect(nav).toBeInTheDocument()
    expect(screen.getByText('行情扫描')).toBeInTheDocument()
  })

  it('market-scanner 链接 href 正确', () => {
    render(<AppNav />)
    const link = screen.getByRole('link', { name: /行情扫描/ })
    expect(link.getAttribute('href')).toBe('/market-scanner')
  })
})