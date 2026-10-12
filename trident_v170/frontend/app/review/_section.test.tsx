import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { SectionErrorBoundary, SectionHeading } from './_section'

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
