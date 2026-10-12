'use client'

/**
 * app/review/_section.tsx — 段标题 + 错误边界(v302+ P1 拆分自 page.tsx)
 *
 * 设计:
 *  - SectionHeading 复用于 /review 的段头(icon + 标题 + 副标题)
 *  - SectionErrorBoundary 用 class component 写(React 18 标准),捕获 render/lifecycle 错误
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
