/**
 * LazySection 一次性懒挂载测试 (v302+ P1 重构)。
 *
 * 守护:
 *  - 不可见 → children 不渲染 (省 fetch/SSE/interval)
 *  - 首次进入视口 → 挂载
 *  - 滚出视口 → 不重新卸载 (一次性)
 *  - minHeight 占位防 layout shift
 *  - SSR/旧浏览器兜底 (无 IntersectionObserver)
 *  - 多个实例独立,一个挂载不影响其它
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, cleanup } from '@testing-library/react'
import { LazySection } from './lazy-section'

// v302+ 显式 cleanup: @testing-library/jest-dom/vitest 在某些环境下不自动注册
// afterEach 钩子,这里手动确保每个 test 结束后 DOM 清空(否则 data-testid 会跨
// test 累积,queryBy* 报 Found multiple elements)
afterEach(() => {
  cleanup()
})

let mockObservers: Array<{
  callback: IntersectionObserverCallback
  options?: IntersectionObserverInit
  observe: ReturnType<typeof vi.fn>
  disconnect: ReturnType<typeof vi.fn>
  unobserve: ReturnType<typeof vi.fn>
  fire(el: Element, isIntersecting: boolean): void
}> = []

beforeEach(() => {
  mockObservers = []
  const MockIO = vi.fn((cb: IntersectionObserverCallback, opts?: IntersectionObserverInit) => {
    const o = {
      callback: cb,
      options: opts,
      observe: vi.fn((el: Element) => {
        ;(el as any).__mockObserver = o
      }),
      disconnect: vi.fn(),
      unobserve: vi.fn(),
      fire(el: Element, isIntersecting: boolean) {
        cb(
          [{ isIntersecting, target: el, intersectionRatio: isIntersecting ? 1 : 0 } as any],
          o as any,
        )
      },
    }
    mockObservers.push(o)
    return o as any
  })
  ;(globalThis as any).IntersectionObserver = MockIO
})

afterEach(() => {
  delete (globalThis as any).IntersectionObserver
  mockObservers = []
})

describe('LazySection — 一次性懒挂载', () => {
  it('初始不可见:children 不渲染', () => {
    render(
      <LazySection>
        <div data-testid="heavy">Heavy content</div>
      </LazySection>,
    )
    expect(screen.queryByTestId('heavy')).toBeNull()
  })

  it('不可见时:容器 data-lazy="pending"', () => {
    const { container } = render(
      <LazySection>
        <div>content</div>
      </LazySection>,
    )
    expect(container.firstElementChild?.getAttribute('data-lazy')).toBe('pending')
  })

  it('首次进入视口:children 挂载,data-lazy=mounted,IO disconnect', () => {
    const { container } = render(
      <LazySection>
        <div data-testid="heavy">Heavy content</div>
      </LazySection>,
    )
    expect(mockObservers).toHaveLength(1)
    const obs = mockObservers[0]
    const el = container.firstElementChild as Element
    act(() => {
      obs.fire(el, true)
    })
    expect(screen.queryByTestId('heavy')).not.toBeNull()
    expect(container.firstElementChild?.getAttribute('data-lazy')).toBe('mounted')
    // disconnect 在挂载时 + useEffect cleanup 卸载时各一次(共 2 次)
    expect(obs.disconnect.mock.calls.length).toBeGreaterThanOrEqual(1)
  })

  it('滚出视口后:children 仍挂载 (一次性)', () => {
    const { container } = render(
      <LazySection>
        <div data-testid="heavy">content</div>
      </LazySection>,
    )
    const obs = mockObservers[0]
    const el = container.firstElementChild as Element
    act(() => {
      obs.fire(el, true)
      obs.fire(el, false)
    })
    expect(screen.queryByTestId('heavy')).not.toBeNull()
    // 一次挂载 → disconnect 至少 1 次
    expect(obs.disconnect.mock.calls.length).toBeGreaterThanOrEqual(1)
  })

  it('已挂载后:rerender 不创建新 IO (useEffect deps 正确)', () => {
    const { container } = render(
      <LazySection>
        <div data-testid="heavy">content</div>
      </LazySection>,
    )
    expect(mockObservers).toHaveLength(1)
    const obs = mockObservers[0]
    const el = container.firstElementChild as Element
    act(() => {
      obs.fire(el, true)
    })
    expect(mockObservers).toHaveLength(1)
  })

  it('minHeight:不可见时应用,挂载后去除', () => {
    const { container } = render(
      <LazySection minHeight={300}>
        <div>content</div>
      </LazySection>,
    )
    const el = container.firstElementChild as HTMLElement
    expect(el.style.minHeight).toBe('300px')
    act(() => {
      mockObservers[0].fire(el, true)
    })
    // React 19 + jsdom: style={undefined} 会把属性清成 ''(而不是 null),
    // 关键是 inline style 集合为空(无 minHeight)
    expect(el.style.minHeight).toBe('')
    expect(el.style.length).toBe(0)
  })

  it('自定义 rootMargin 传给 IntersectionObserver', () => {
    render(
      <LazySection rootMargin="500px 100px">
        <div>content</div>
      </LazySection>,
    )
    expect(mockObservers[0].options).toEqual({ rootMargin: '500px 100px', threshold: 0 })
  })

  it('无 IntersectionObserver (SSR 兜底):直接挂载', () => {
    delete (globalThis as any).IntersectionObserver
    render(
      <LazySection>
        <div data-testid="heavy">content</div>
      </LazySection>,
    )
    expect(screen.queryByTestId('heavy')).not.toBeNull()
  })

  it('多个实例独立,各自的 IO 互不干扰', () => {
    const { container: c1 } = render(
      <div>
        <LazySection>
          <div data-testid="a">A</div>
        </LazySection>
      </div>,
    )
    const { container: c2 } = render(
      <div>
        <LazySection>
          <div data-testid="b">B</div>
        </LazySection>
      </div>,
    )
    expect(mockObservers).toHaveLength(2)
    const el1 = c1.querySelector('[data-lazy]') as Element
    act(() => {
      mockObservers[0].fire(el1, true)
    })
    // a 已挂载,b 仍 pending (因为 mockObservers[1] 没触发)
    expect(c1.querySelector('[data-testid="a"]')).not.toBeNull()
    expect(c2.querySelector('[data-testid="b"]')).toBeNull()
    expect(c2.querySelector('[data-lazy="pending"]')).not.toBeNull()
  })

  it('自定义 placeholder:不可见时显示', () => {
    render(
      <LazySection placeholder={<div data-testid="ph">loading…</div>}>
        <div data-testid="real">real</div>
      </LazySection>,
    )
    expect(screen.queryByTestId('ph')).not.toBeNull()
    expect(screen.queryByTestId('real')).toBeNull()
  })

  it('as="section" 渲染为对应标签', () => {
    const { container } = render(
      <LazySection as="section">
        <div>content</div>
      </LazySection>,
    )
    expect(container.firstElementChild?.tagName).toBe('SECTION')
  })

  it('unmount 时不抛错 (已挂载分支不返回 cleanup 仍安全)', () => {
    const { unmount, container } = render(
      <LazySection>
        <div>content</div>
      </LazySection>,
    )
    const obs = mockObservers[0]
    const el = container.firstElementChild as Element
    act(() => {
      obs.fire(el, true)
    })
    expect(() => unmount()).not.toThrow()
  })
})

describe('LazySection — v302+ P4 a11y:aria-busy 状态', () => {
  // aria-busy 在"未挂载 → 挂载"之间翻转:
  //  - 初始 pending:aria-busy="true"(告诉 AT region 正在加载)
  //  - 挂载完成:aria-busy 属性被移除(用 undefined 而不是 "false",
  //    避免 ARIA 树持续抖动)
  // 关键场景:SR 用户进入含 LazySection 的 region 时,会听读
  // "busy, 复盘面板 加载中" 之类(配合 role=status 的占位节点)。
  it('初始 pending:容器有 aria-busy="true"', () => {
    const { container } = render(
      <LazySection>
        <div>content</div>
      </LazySection>,
    )
    const el = container.firstElementChild as HTMLElement
    expect(el.getAttribute('aria-busy')).toBe('true')
  })

  it('挂载完成后:aria-busy 属性被移除(undefined 而非 "false")', () => {
    const { container } = render(
      <LazySection>
        <div data-testid="heavy">content</div>
      </LazySection>,
    )
    const el = container.firstElementChild as HTMLElement
    const obs = mockObservers[0]
    act(() => {
      obs.fire(el, true)
    })
    // "false" 会持续出现在 ARIA 树里;不写该属性让 AT 走默认值
    // (not busy),DOM 树更干净
    expect(el.hasAttribute('aria-busy')).toBe(false)
    expect(el.getAttribute('aria-busy')).toBeNull()
  })

  it('SSR 兜底(无 IO):直接挂载 → 无 aria-busy', () => {
    delete (globalThis as any).IntersectionObserver
    const { container } = render(
      <LazySection>
        <div>content</div>
      </LazySection>,
    )
    const el = container.firstElementChild as HTMLElement
    expect(el.hasAttribute('aria-busy')).toBe(false)
  })
})
