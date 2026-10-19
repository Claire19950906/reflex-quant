import '@testing-library/jest-dom/vitest'
// v302+ P3: jest-axe 扩展 toHaveNoViolations matcher — a11y 断言
import { toHaveNoViolations } from 'jest-axe'

// 期望 extend(Vitest 2.x 推荐方式)
import { expect } from 'vitest'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
expect.extend(toHaveNoViolations as any)

// Next.js / jsdom 默认不实现 matchMedia;某些 UI 在 SSR 时会读它
if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

// ResizeObserver 没在 jsdom 里
if (typeof window !== 'undefined' && !(window as any).ResizeObserver) {
  ;(window as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// v302+ P3: jest-axe 需要 axe-core 才能跑。jsdom 不支持部分现代 API,
// 这里给 jsdom 补一些 polyfill 让 axe 能跑完核心检查(label/name/role/aria)
if (typeof window !== 'undefined') {
  // axe 期望 HTMLElement 完整可用 — jsdom 已提供
  // axe 期望 SVGElement 完整 — jsdom 已提供
  // 仅为 jsdom 缺失的 CSSOM 部分补空函数,避免 axe 误报
  if (!('getComputedStyle' in window)) {
    Object.defineProperty(window, 'getComputedStyle', {
      value: (el: Element) => ({
        getPropertyValue: () => '',
        getPropertyPriority: () => '',
      }),
    })
  }
}
