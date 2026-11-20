import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { EventBus } from './event-bus'

// v362: EventBus 三态渲染回归 —— 首屏载入中 / 拉完 0 条空态 / 后端报错不可用态。
// 事件流组件内部自建 EventSource，jsdom 没有，这里给一个最小桩。
class MockEventSource {
  static instances: MockEventSource[] = []
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onopen: (() => void) | null = null
  close() {}
  constructor() {
    MockEventSource.instances.push(this)
  }
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

describe('EventBus · 载入/空/错误三态', () => {
  beforeEach(() => {
    MockEventSource.instances = []
    vi.stubGlobal('EventSource', MockEventSource)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('首屏先显示载入中，拉取 0 条后进入空态', async () => {
    global.fetch = vi.fn((url) => {
      if (String(url).includes('/events/today')) {
        return Promise.resolve(jsonResponse({ total_count: 0 }))
      }
      return Promise.resolve(jsonResponse([]))
    }) as unknown as typeof fetch

    render(<EventBus />)

    // 拉取未完成前：载入中，不是空态
    expect(screen.getByText('载入中…')).toBeInTheDocument()

    await waitFor(() => {
      expect(screen.getByText('当前时段暂无事件')).toBeInTheDocument()
    })
  })

  it('后端返回错误时显示「事件流暂不可用」，不误报空态', async () => {
    global.fetch = vi.fn(() => Promise.resolve(new Response('boom', { status: 500 }))) as unknown as typeof fetch

    render(<EventBus />)

    await waitFor(() => {
      expect(screen.getByText('事件流暂不可用')).toBeInTheDocument()
    })
    expect(screen.queryByText('当前时段暂无事件')).not.toBeInTheDocument()
  })
})
