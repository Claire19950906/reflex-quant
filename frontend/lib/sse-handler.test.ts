/**
 * SSE 单例 + 错误处理测试（v302+ P0+ 修复）。
 *
 * 重点守护:
 *  - CLOSED 状态的 _sseSource 会被清掉,下次 ensureSSE 创建新连接（防僵尸）
 *  - onerror 被正确注册（不是只 onmessage）
 *  - 单 onerror handler 不影响其它 onmessage 路径
 *  - handler 异常被 try/catch 隔离,不影响其它订阅者
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

// 必须在 import data-channel-standards 之前 mock EventSource,
// 否则模块初始化时 const _sseSource = null 是 OK 的,
// 但 useChannelPoll 在测试中实际触发 ensureSSE() 时会拿真实 EventSource
class MockEventSource {
  static instances: MockEventSource[] = []
  static OPEN = 0
  static CONNECTING = 1
  static CLOSED = 2
  readyState: number = MockEventSource.CONNECTING
  url: string
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  close() {
    this.readyState = MockEventSource.CLOSED
  }
  /** 测试辅助:模拟收到一条 message 帧 */
  emitMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) })
  }
  /** 测试辅助:模拟服务器关闭连接 */
  simulateClose() {
    this.readyState = MockEventSource.CLOSED
    this.onerror?.()
  }
}

beforeEach(() => {
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
  // 强制重新加载模块以清空 _sseSource 单例
  vi.resetModules()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('SSE 单例 + 错误处理 (v302+ P0+)', () => {
  it('ensureSSE 创建 EventSource 并注册 onmessage + onerror', async () => {
    const { onChannelEvent, emitChannelEvent } = await import('./data-channel-standards')
    const unsub = onChannelEvent('test:type', () => {})
    expect(MockEventSource.instances).toHaveLength(1)
    const es = MockEventSource.instances[0]
    expect(es.onmessage).not.toBeNull()
    expect(es.onerror).not.toBeNull()  // 关键:onerror 也要被注册
    unsub()
  })

  it('CLOSED 后下次 ensureSSE 创建新连接（防僵尸）', async () => {
    const { onChannelEvent } = await import('./data-channel-standards')
    const unsub1 = onChannelEvent('a', () => {})
    expect(MockEventSource.instances).toHaveLength(1)
    const first = MockEventSource.instances[0]

    // 模拟服务器关闭:onerror 触发,清掉 _sseSource
    first.simulateClose()
    expect(first.readyState).toBe(MockEventSource.CLOSED)

    unsub1()
    const unsub2 = onChannelEvent('b', () => {})
    // 关键:应该有新 EventSource,而不是复用已 CLOSED 的旧连接
    expect(MockEventSource.instances).toHaveLength(2)
    expect(MockEventSource.instances[1]).not.toBe(first)
    unsub2()
  })

  it('多个订阅者共享同一 EventSource（单例）', async () => {
    const { onChannelEvent, emitChannelEvent } = await import('./data-channel-standards')
    const u1 = onChannelEvent('a', vi.fn())
    const u2 = onChannelEvent('b', vi.fn())
    expect(MockEventSource.instances).toHaveLength(1)
    u1(); u2()
  })

  it('emitChannelEvent 不经 SSE,直接调本地订阅者', async () => {
    const { onChannelEvent, emitChannelEvent } = await import('./data-channel-standards')
    const h = vi.fn()
    const unsub = onChannelEvent('local:event', h)
    emitChannelEvent('local:event', { x: 1 })
    expect(h).toHaveBeenCalledWith({ x: 1 })
    unsub()
  })

  it('SSE 帧解析:JSON 解析失败时静默忽略 (不影响其它 handler)', async () => {
    const { onChannelEvent } = await import('./data-channel-standards')
    const unsub = onChannelEvent('ok:type', vi.fn())
    const es = MockEventSource.instances[0]
    // 直接 emit 无效 JSON
    es.onmessage?.({ data: 'not json{' })
    // 不抛错即通过
    unsub()
  })

  it('handler 异常被隔离 (一个 handler throw 不影响其它)', async () => {
    const { onChannelEvent, emitChannelEvent } = await import('./data-channel-standards')
    const h1 = vi.fn(() => { throw new Error('boom') })
    const h2 = vi.fn()
    const u1 = onChannelEvent('same:type', h1)
    const u2 = onChannelEvent('same:type', h2)
    emitChannelEvent('same:type', { v: 1 })
    expect(h1).toHaveBeenCalledOnce()
    expect(h2).toHaveBeenCalledOnce()  // h1 抛了,h2 仍执行
    u1(); u2()
  })

  it('单帧多订阅者按注册顺序执行', async () => {
    const { onChannelEvent, emitChannelEvent } = await import('./data-channel-standards')
    const order: string[] = []
    const u1 = onChannelEvent('seq:type', () => { order.push('a') })
    const u2 = onChannelEvent('seq:type', () => { order.push('b') })
    const u3 = onChannelEvent('seq:type', () => { order.push('c') })
    emitChannelEvent('seq:type', {})
    expect(order).toEqual(['a', 'b', 'c'])
    u1(); u2(); u3()
  })

  it('unknown event type 时不调任何 handler', async () => {
    const { onChannelEvent, emitChannelEvent } = await import('./data-channel-standards')
    const h = vi.fn()
    const unsub = onChannelEvent('known', h)
    emitChannelEvent('unknown', { x: 1 })
    expect(h).not.toHaveBeenCalled()
    unsub()
  })

  it('SSE 帧没有 type 字段时走默认 type="message" 路径', async () => {
    const { onChannelEvent } = await import('./data-channel-standards')
    const h = vi.fn()
    const unsub = onChannelEvent('message', h)
    const es = MockEventSource.instances[0]
    es.emitMessage({ noType: true })  // 没有 type
    expect(h).toHaveBeenCalledWith({ noType: true })
    unsub()
  })
})
