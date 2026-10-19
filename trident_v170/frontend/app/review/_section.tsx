'use client'

/**
 * app/review/_section.tsx — 段标题 + 错误边界 + 加载占位(v302+ P1/P3 拆分自 page.tsx)
 *
 * 设计:
 *  - SectionHeading 复用于 /review 的段头(icon + 标题 + 副标题)
 *  - SectionErrorBoundary 用 class component 写(React 18 标准),捕获 render/lifecycle 错误
 *  - PanelLoadingSkeleton: a11y 友好的加载占位(role=status + aria-live=polite + minHeight 防 CLS)
 *  - 其它段的稳定性靠子组件自身 try/catch + Zod 校验兜底
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'

/** 段标题(icon + 标题 + 副标题) */
export function SectionHeading({
  icon,
  title,
  subtitle,
}: {
  icon: string
  title: string
  subtitle: string
}) {
  return (
    <div className="flex items-end gap-3 border-b border-border pb-1.5">
      <h2 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
        <span aria-hidden>{icon}</span>
        {title}
      </h2>
      <p className="font-mono text-xs text-muted-foreground">{subtitle}</p>
    </div>
  )
}

/**
 * v302+ P3+: a11y 友好的面板加载占位
 *  - role="status" + aria-live="polite" — 屏幕阅读器会播报 label,但不抢焦点
 *  - minHeight 锁高,防 Cumulative Layout Shift (CLS)
 *  - 默认 label "载入中…" 跟其它面板一致
 */
export function PanelLoadingSkeleton({
  label = '载入中…',
  minHeight,
}: {
  label?: string
  minHeight?: number
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      style={minHeight ? { minHeight: `${minHeight}px` } : undefined}
      className="flex items-center justify-center rounded-md border border-border bg-card/50 p-6 font-mono text-xs text-muted-foreground"
    >
      <span aria-hidden className="mr-2 inline-block h-3 w-3 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground" />
      {label}
    </div>
  )
}

/** 错误边界 —— 一个面板抛错不连累整页。
 *  - 捕获 React 渲染期抛错(render error / lifecycle error)
 *  - 不捕获: 事件回调、异步、SSR —— 这是 React ErrorBoundary 的标准边界
 *  - 重试: 点 "重试" 清掉 error,子树重新挂载(会重新触发 fetch)
 *
 *  用 class 而非 react-error-boundary 包: 项目 0 新依赖原则(package.json
 *  里没装 react-error-boundary)。这是 React 18 写 ErrorBoundary 的唯一
 *  方式 —— React 19 才允许 hooks 写。
 */
export class SectionErrorBoundary extends Component<
  { name: string; children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(
      `[review/section:${this.props.name}] crashed:`,
      error,
      info.componentStack,
    )
  }

  componentDidUpdate(prevProps: { name: string; children: ReactNode }) {
    // v2026.10.19: HMR/页面切换后, 旧 state.error 会一直显示
    // 当子组件身份变化 (例如某个 panel 修好后的新模块实例) 时自动重置
    // 防御: 同一子树在连续两次 render 中抛同一错, 不应立即复位;
    // 这里用 children 引用变化做信号, 这是 Next.js dev HMR 重新挂载子组件的标志
    if (this.state.error && prevProps.children !== this.props.children) {
      this.setState({ error: null })
    }
  }

  render() {
    if (this.state.error) {
      // role="alert" 移到内部 span —— 原版在外层 div,屏幕阅读器会把"重试"
      // 按钮文本一起急促读出。拆开后 alert 只覆盖错误信息,按钮独立 Tab 可达。
      return (
        <div className="flex items-center gap-2 rounded border border-destructive/40 bg-destructive/5 px-3 py-2 font-mono text-xs text-destructive">
          <span role="alert">⚠️ {this.props.name} 面板异常:{this.state.error.message}</span>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="ml-auto underline decoration-dotted hover:text-destructive/80"
          >
            重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
