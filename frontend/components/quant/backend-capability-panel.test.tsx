/**
 * components/quant/backend-capability-panel.test.tsx
 *
 * BackendCapabilityPanel —— 后端能力归位面板的行为契约。
 *
 * 守住三条硬约束(它们都不会在编译期暴露, 改坏了界面只会「静默不工作」):
 *   1. **懒加载** —— 未展开时绝不能调 loader(重端点首调可达 40s, 首屏不能被拖死)。
 *   2. **展开即取数** —— 展开后必须真的调 loader 并把 stats/rows 画出来。
 *   3. **失败可见** —— loader 抛错时必须显式提示「后端未就绪」, 而不是画成「没有数据」。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

const { loadSignalWeights, loadBeliefs } = vi.hoisted(() => ({
  loadSignalWeights: vi.fn(),
  loadBeliefs: vi.fn(),
}))

vi.mock('@/lib/api-backend', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api-backend')>()
  return {
    ...actual,
    BACKEND_LOADERS: {
      ...actual.BACKEND_LOADERS,
      'signal-weights': loadSignalWeights,
      beliefs: loadBeliefs,
    },
  }
})

import { BackendCapabilityPanel } from './backend-capability-panel'

describe('BackendCapabilityPanel', () => {
  beforeEach(() => {
    loadSignalWeights.mockReset()
    loadBeliefs.mockReset()
    loadSignalWeights.mockResolvedValue({
      stats: [{ label: '24h 冷信号', value: '3', tone: 'warn' }],
      rows: [{ title: '权重档 A', body: '均权 1.0' }],
      note: '测试 note',
    })
    loadBeliefs.mockResolvedValue({ stats: [], rows: [] })
  })

  // 本仓库 vitest 未开 globals,RTL 的自动 cleanup 不会跑 —— 手动清 DOM
  afterEach(() => {
    cleanup()
  })

  // 1. 基本渲染: 用台账里的 label, 不写死
  it('用台账 label 渲染标题, 并带「后端能力」无障碍标签', () => {
    render(<BackendCapabilityPanel domain="signal-weights" />)
    expect(screen.getByText('信号权重')).toBeTruthy()
    expect(screen.getByLabelText(/后端能力 信号权重/)).toBeTruthy()
  })

  // 2. 懒加载: 未展开不取数
  it('未展开时不调 loader(懒加载)', () => {
    render(<BackendCapabilityPanel domain="signal-weights" />)
    expect(loadSignalWeights).not.toHaveBeenCalled()
  })

  // 3. 展开才取数, 并渲染 stats / rows / note
  it('展开后调 loader 并渲染 stats / rows / note', async () => {
    render(<BackendCapabilityPanel domain="signal-weights" />)
    fireEvent.click(screen.getByText('信号权重'))
    await waitFor(() => expect(loadSignalWeights).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByText('24h 冷信号')).toBeTruthy())
    expect(screen.getByText('权重档 A')).toBeTruthy()
    expect(screen.getByText('测试 note')).toBeTruthy()
  })

  // 4. 重复展开不重复取数(数据已在手时不再打后端)
  it('已有数据后再展开不重复取数', async () => {
    render(<BackendCapabilityPanel domain="signal-weights" />)
    fireEvent.click(screen.getByText('信号权重'))
    await waitFor(() => expect(loadSignalWeights).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByText('信号权重'))
    fireEvent.click(screen.getByText('信号权重'))
    expect(loadSignalWeights).toHaveBeenCalledTimes(1)
  })

  // 5. loader 失败 → 显式「后端未就绪」, 不伪装成空数据
  it('loader 抛错时提示后端未就绪', async () => {
    loadSignalWeights.mockRejectedValue(new Error('后端返回 ok:false(未就绪)'))
    render(<BackendCapabilityPanel domain="signal-weights" />)
    fireEvent.click(screen.getByText('信号权重'))
    await waitFor(() => expect(screen.getByText(/后端未就绪/)).toBeTruthy())
    expect(screen.getByText(/后端返回 ok:false/)).toBeTruthy()
  })

  // 6. 域不存在 → 提示未接入 loader, 不静默
  it('未知域提示未接入 loader', async () => {
    render(<BackendCapabilityPanel domain="no-such-domain" />)
    fireEvent.click(screen.getByText('no-such-domain'))
    await waitFor(() => expect(screen.getByText(/尚未接入 loader/)).toBeTruthy())
  })

  // 7. 空数据 → 明确说「无数据」, 与「未就绪」区分开
  it('有数据但为空时提示该能力当前无数据', async () => {
    render(<BackendCapabilityPanel domain="beliefs" />)
    fireEvent.click(screen.getByText('信念系统'))
    await waitFor(() => expect(screen.getByText('该能力当前无数据。')).toBeTruthy())
  })
})
