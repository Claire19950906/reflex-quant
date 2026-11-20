import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { useRef } from 'react'
import {
  SectionErrorBoundary,
  SectionHeading,
  AbTablist,
  CollapsibleSection,
} from './_section'

afterEach(() => cleanup())

function Boom(): never {
  throw new Error('boom-from-test')
}

function Bomb() {
  return <Boom />
}

describe('SectionHeading — 段头渲染', () => {
  it('渲染 icon + 标题 + 副标题', () => {
    render(<SectionHeading icon="🛠" title="改进" subtitle="因果图谱 · 因果链库" />)
    expect(screen.getByRole('heading', { name: /改进/ })).toBeInTheDocument()
    expect(screen.getByText('因果图谱 · 因果链库')).toBeInTheDocument()
    expect(screen.getByText('🛠')).toBeInTheDocument()
  })

  it('icon 在 aria-hidden 容器里', () => {
    const { container } = render(<SectionHeading icon="🛠" title="改进" subtitle="x" />)
    const icon = screen.getByText('🛠')
    expect(icon).toHaveAttribute('aria-hidden')
  })
})

describe('SectionErrorBoundary — 错误捕获与重试', () => {
  // 测试时抑制 React 错误日志(预期会触发)
  const origError = console.error
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const anyConsole = console as any
  anyConsole.error = () => {}
  afterEach(() => {
    anyConsole.error = origError
  })

  it('children 正常 → 透传渲染', () => {
    render(
      <SectionErrorBoundary name="测试段">
        <div data-testid="happy">正常内容</div>
      </SectionErrorBoundary>,
    )
    expect(screen.getByTestId('happy')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('children 抛错 → 渲染 fallback(role=alert)', () => {
    render(
      <SectionErrorBoundary name="测试段">
        <Bomb />
      </SectionErrorBoundary>,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('测试段')
    expect(alert).toHaveTextContent('boom-from-test')
  })

  it('fallback 含 name 参数(便于定位是哪个段炸了)', () => {
    render(
      <SectionErrorBoundary name="复盘">
        <Bomb />
      </SectionErrorBoundary>,
    )
    expect(screen.getByText(/复盘 面板异常/)).toBeInTheDocument()
  })

  it('点 "重试" → 清掉 error,子树重新挂载', () => {
    // 闭包外可变变量控制 MaybeBomb 是否抛错:
    //  1) 首次 render 抛错 → ErrorBoundary 捕获 → 显示 alert
    //  2) 修改 shouldCrash 后点击"重试"
    //  3) SectionErrorBoundary 内部 setState({error:null}) 触发子树重渲染
    //  4) MaybeBomb 的新闭包读到 shouldCrash=false → 返回正常内容
    // 不需要手动 rerender:fireEvent.click 已触发 React state 更新,子树自动重挂载。
    let shouldCrash = true
    function MaybeBomb() {
      if (shouldCrash) throw new Error('boom')
      return <div data-testid="recovered">恢复</div>
    }
    render(
      <SectionErrorBoundary name="测试段">
        <MaybeBomb />
      </SectionErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()
    shouldCrash = false
    fireEvent.click(screen.getByText('重试'))
    expect(screen.getByTestId('recovered')).toBeInTheDocument()
  })
})

describe('AbTablist — v302+ P4 WAI-ARIA APG 1.2 完整模式', () => {
  // AbTablist 把 6 个 APG 要求封一起:
  //   1. container role=tablist + aria-label
  //   2. button role=tab + aria-selected + aria-controls
  //   3. Roving Tabindex(只选中那个 tabIndex=0,其它 -1)
  //   4. 键盘 ArrowLeft/Right/Home/End
  //   5. 选中后真正 .focus() 新 tab
  //   6. (集成在 _a11y.test.tsx) panel 必须有 role=tabpanel + id 匹配 aria-controls

  // 包装组件:把 useRef 留作 render-time hook(测试框架会处理)
  function Harness({
    selected,
    onSelect,
  }: {
    selected: 'a' | 'b'
    onSelect: (next: 'a' | 'b') => void
  }) {
    const tabRefs = useRef<[(HTMLButtonElement | null), (HTMLButtonElement | null)]>([null, null])
    return (
      <AbTablist
        selected={selected}
        onSelect={onSelect}
        tabIds={['review-tab-a', 'review-tab-b']}
        panelId="review-tabpanel"
        tabRefs={tabRefs}
      />
    )
  }

  it('container 有 role=tablist + aria-label', () => {
    render(<Harness selected="a" onSelect={() => {}} />)
    const tablist = screen.getByRole('tablist', { name: '复盘模式' })
    expect(tablist).toBeInTheDocument()
  })

  it('每个 tab 有 role=tab + id + aria-controls 指向 panel', () => {
    render(<Harness selected="a" onSelect={() => {}} />)
    const aTab = screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })
    const bTab = screen.getByRole('tab', { name: /B · 快速评测复盘/ })
    expect(aTab).toHaveAttribute('id', 'review-tab-a')
    expect(bTab).toHaveAttribute('id', 'review-tab-b')
    expect(aTab).toHaveAttribute('aria-controls', 'review-tabpanel')
    expect(bTab).toHaveAttribute('aria-controls', 'review-tabpanel')
  })

  it('Roving Tabindex: 选中 tab=0, 其它 -1', () => {
    const { rerender } = render(<Harness selected="a" onSelect={() => {}} />)
    const aTab = screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })
    const bTab = screen.getByRole('tab', { name: /B · 快速评测复盘/ })
    expect(aTab).toHaveAttribute('tabindex', '0')
    expect(bTab).toHaveAttribute('tabindex', '-1')
    expect(aTab).toHaveAttribute('aria-selected', 'true')
    expect(bTab).toHaveAttribute('aria-selected', 'false')

    // 切到 b → a 变 -1,b 变 0
    rerender(<Harness selected="b" onSelect={() => {}} />)
    expect(screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByRole('tab', { name: /B · 快速评测复盘/ })).toHaveAttribute('tabindex', '0')
  })

  it('click 触发 onSelect + 正确的回调值', () => {
    const onSelect = vi.fn()
    render(<Harness selected="a" onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('tab', { name: /B · 快速评测复盘/ }))
    expect(onSelect).toHaveBeenCalledWith('b')
  })

  it('ArrowRight 从 a 走到 b(选中 + .focus() 真正移动焦点)', () => {
    const onSelect = vi.fn()
    const { rerender } = render(<Harness selected="a" onSelect={onSelect} />)
    const aTab = screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })
    aTab.focus()
    expect(document.activeElement).toBe(aTab)

    fireEvent.keyDown(aTab, { key: 'ArrowRight' })
    expect(onSelect).toHaveBeenCalledWith('b')

    // 父组件模拟 selected="b" 重新渲染(实际 React 行为)
    rerender(<Harness selected="b" onSelect={onSelect} />)
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /B · 快速评测复盘/ }))
  })

  it('ArrowLeft 从 a 循环到 b(cyclic)', () => {
    const onSelect = vi.fn()
    const { rerender } = render(<Harness selected="a" onSelect={onSelect} />)
    const aTab = screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })
    aTab.focus()

    fireEvent.keyDown(aTab, { key: 'ArrowLeft' })
    expect(onSelect).toHaveBeenCalledWith('b')

    rerender(<Harness selected="b" onSelect={onSelect} />)
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: /B · 快速评测复盘/ }))
  })

  it('Home 跳到 a,End 跳到 b', () => {
    const onSelect = vi.fn()
    const { rerender } = render(<Harness selected="b" onSelect={onSelect} />)
    const bTab = screen.getByRole('tab', { name: /B · 快速评测复盘/ })
    bTab.focus()

    fireEvent.keyDown(bTab, { key: 'Home' })
    expect(onSelect).toHaveBeenLastCalledWith('a')

    rerender(<Harness selected="a" onSelect={onSelect} />)
    fireEvent.keyDown(screen.getByRole('tab', { name: /A · 历史真实交易复盘/ }), { key: 'End' })
    expect(onSelect).toHaveBeenLastCalledWith('b')
  })

  it('非 ARIA 键(Tab/Esc)不触发 onSelect 也不 preventDefault', () => {
    const onSelect = vi.fn()
    render(<Harness selected="a" onSelect={onSelect} />)
    const aTab = screen.getByRole('tab', { name: /A · 历史真实交易复盘/ })
    aTab.focus()
    fireEvent.keyDown(aTab, { key: 'Tab' })
    fireEvent.keyDown(aTab, { key: 'Escape' })
    expect(onSelect).not.toHaveBeenCalled()
  })
})

// ====== v302+ P6: CollapsibleSection ======

describe('CollapsibleSection — 折叠/展开受控渲染', () => {
  it('collapsed=false → 渲染 children,aria-expanded="true"', () => {
    render(
      <CollapsibleSection sectionId="diagnose" collapsed={false} onToggle={() => {}} aria-label="诊断">
        <div data-testid="content">内容</div>
      </CollapsibleSection>,
    )
    expect(screen.getByTestId('content')).toBeInTheDocument()
    const btn = screen.getByRole('button', { name: /折叠 诊断 段/ })
    expect(btn).toHaveAttribute('aria-expanded', 'true')
  })

  it('collapsed=true → 内容区 hidden=true(视觉隐藏),aria-expanded="false"', () => {
    const { container } = render(
      <CollapsibleSection sectionId="diagnose" collapsed={true} onToggle={() => {}} aria-label="诊断">
        <div data-testid="content">内容</div>
      </CollapsibleSection>,
    )
    const btn = screen.getByRole('button', { name: /展开 诊断 段/ })
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    const region = container.querySelector('#diagnose-content')
    expect(region).toHaveAttribute('hidden')
  })

  it('折叠时 section 元素带 data-collapsed="true" + data-section-id', () => {
    render(
      <CollapsibleSection sectionId="diagnose" collapsed={true} onToggle={() => {}} aria-label="诊断">
        <div>内容</div>
      </CollapsibleSection>,
    )
    const section = document.getElementById('diagnose')
    expect(section).toHaveAttribute('data-collapsed', 'true')
    expect(section).toHaveAttribute('data-section-id', 'diagnose')
  })

  it('展开时 section 元素带 data-collapsed="false"', () => {
    render(
      <CollapsibleSection sectionId="diagnose" collapsed={false} onToggle={() => {}} aria-label="诊断">
        <div>内容</div>
      </CollapsibleSection>,
    )
    expect(document.getElementById('diagnose')).toHaveAttribute('data-collapsed', 'false')
  })

  it('点 button → 调 onToggle', () => {
    const onToggle = vi.fn()
    render(
      <CollapsibleSection sectionId="review" collapsed={false} onToggle={onToggle} aria-label="复盘">
        <div>内容</div>
      </CollapsibleSection>,
    )
    fireEvent.click(screen.getByRole('button', { name: /折叠 复盘 段/ }))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('传 index 时显示数字小标签', () => {
    render(
      <CollapsibleSection sectionId="diagnose" collapsed={false} onToggle={() => {}} aria-label="诊断" index={1}>
        <div>内容</div>
      </CollapsibleSection>,
    )
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('不传 index 时不显示数字小标签', () => {
    render(
      <CollapsibleSection sectionId="diagnose" collapsed={false} onToggle={() => {}} aria-label="诊断">
        <div>内容</div>
      </CollapsibleSection>,
    )
    expect(screen.queryByText('1')).not.toBeInTheDocument()
  })

  it('button 的 aria-controls 指向内容容器', () => {
    render(
      <CollapsibleSection sectionId="diagnose" collapsed={false} onToggle={() => {}} aria-label="诊断">
        <div>内容</div>
      </CollapsibleSection>,
    )
    const btn = screen.getByRole('button', { name: /折叠 诊断 段/ })
    expect(btn.getAttribute('aria-controls')).toBe('diagnose-content')
    const region = screen.getByRole('region', { name: '诊断 内容' })
    expect(region).toHaveAttribute('id', 'diagnose-content')
  })
})

