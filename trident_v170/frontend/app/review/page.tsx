'use client'

/**
 * app/review/page.tsx — /review 路由入口(SSR-safe 薄壳,v302+ P1 重构)
 *
 * 设计:
 *  - 内容树全部在 _inner.tsx(7 段 + 时段选择器 + A/B tab)
 *  - 错误边界 / 段标题 在 _section.tsx
 *  - 本文件只剩 3 件套: ToastProvider + Suspense + 根级 ErrorBoundary
 *
 *  为什么 Suspense 在根级:
 *  Next 14 要求 useSearchParams 必须包在 <Suspense> 内 — useReviewUrlState
 *  内部用 usePathname/useRouter/useSearchParams 三个 hook,根级 Suspense 是
 *  SSR hydration 的安全网,丢了会在 prerender 阶段报错。
 */

import { Suspense } from 'react'
import { ToastProvider } from '@/components/ui/toast'
import { SectionErrorBoundary } from './_section'
import { ReviewPageInner } from './_inner'

export default function ReviewPage() {
  return (
    <ToastProvider>
      {/* v302+ 修正:ErrorBoundary 必须包住 <Suspense>,这样 Suspense fallback
          渲染期抛错也能被 ErrorBoundary 兜底(原顺序 ErrorBoundary 在 Suspense
          内部时,fallback 是 ErrorBoundary 的兄弟节点,捕获不到)。 */}
      <SectionErrorBoundary name="反思系统">
        <Suspense
          fallback={
            <div
              role="status"
              aria-live="polite"
              className="mx-auto flex w-full max-w-[1600px] items-center justify-center p-8 font-mono text-xs text-muted-foreground"
            >
              载入中…
            </div>
          }
        >
          <ReviewPageInner />
        </Suspense>
      </SectionErrorBoundary>
    </ToastProvider>
  )
}
