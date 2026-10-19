'use client'

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'

/** v302+ P1 重构: 一次性懒挂载容器。
 *
 * 设计动机:
 *  - /review 7 段式同屏渲染时,只有顶部 2-3 段在视口里
 *  - 重型段 (复盘/诊断/推理) 在初次加载时全部立即挂载 → 多份 fetch + SSE
 *    + setInterval 同时跑,首屏 FCP/TTI 显著恶化
 *  - 方案: 进入视口后再挂载,挂载后永驻(切走再切回不重新挂载,保持 React 状态)
 *
 * 与 `loading="lazy"` / React.lazy / next/dynamic 的区别:
 *  - `loading="lazy"` 只对图片生效
 *  - React.lazy() + Suspense 是按需加载 chunk,适合路由级别
 *  - next/dynamic({ ssr:false }) 是关掉 SSR,不是懒挂载
 *  - 本组件: 段级别懒挂载(SSR 仍渲染,客户端按需挂载子树),保留 SEO + 不重 mount
 *
 * 边界:
 *  - SSR 时 IntersectionObserver 不可用 → 直接挂载(避免水合错位)
 *  - 已挂载 (hasBeenVisible=true) → 跳过观察,避免无效 IO 操作
 *  - minHeight 占位避免布局跳动,但内容撑高时无副作用
 */
export function LazySection({
  children,
  rootMargin = '200px 0px',
  minHeight,
  placeholder,
  as: As = 'div',
}: {
  children: ReactNode
  /** 提前多少像素开始挂载 (default 200px,用户即将滚动到时就开始) */
  rootMargin?: string
  /** 不可见时的最小高度,避免滚动时 layout shift */
  minHeight?: number
  /** 自定义占位元素,默认 0 高度的透明 div */
  placeholder?: ReactNode
  /** 容器标签,默认 div。section/article 也可 */
  as?: 'div' | 'section' | 'article'
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [hasBeenVisible, setHasBeenVisible] = useState(false)

  useEffect(() => {
    if (hasBeenVisible) return  // 已挂载 → 永驻,不再观察
    if (typeof IntersectionObserver === 'undefined') {
      // SSR / 旧浏览器兜底:直接挂载
      setHasBeenVisible(true)
      return
    }
    const el = ref.current
    if (!el) return
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            setHasBeenVisible(true)
            obs.disconnect()
            break
          }
        }
      },
      { rootMargin, threshold: 0 },
    )
    obs.observe(el)
    return () => obs.disconnect()
  }, [hasBeenVisible, rootMargin])

  const style: CSSProperties | undefined =
    !hasBeenVisible && minHeight ? { minHeight: `${minHeight}px` } : undefined

  return (
    <As
      ref={ref as any}
      style={style}
      data-lazy={hasBeenVisible ? 'mounted' : 'pending'}
      // v302+ P4:aria-busy 在未挂载时=true,告诉 AT(region 正在加载,
      // 请等待) — 配合 role="status" 占位节点可被读屏读为 "复盘面板,加载中"。
      // 挂载完成后清空(aria-busy="false" 不写,因为 false 是默认值,
      // 显式写会让 ARIA 树持续抖动)。
      aria-busy={!hasBeenVisible || undefined}
    >
      {hasBeenVisible ? children : (placeholder ?? null)}
    </As>
  )
}
