'use client'

import { Suspense, useEffect } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'

/**
 * 兼容旧路由：所有访问 /asset-graph 自动跳转到统一入口 /knowledge-graph。
 * 默认 tab 已是「因果图谱」— 与旧路径行为完全一致。
 *
 * 同步保留 ?asset= 参数：归因页若用旧 /asset-graph?asset=XAU 也照样能聚焦。
 *
 * Next.js 14：useSearchParams 必须在 Suspense 内 → default export 包一层。
 */
function CausalGraphLegacyPageInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  useEffect(() => {
    const qs = searchParams?.toString() || ''
    router.replace(`/knowledge-graph${qs ? `?${qs}` : ''}`)
  }, [router, searchParams])
  return (
    <div className="mx-auto flex w-full max-w-[1600px] items-center justify-center p-12 text-xs text-muted-foreground">
      正在跳转到「因果图谱」…
    </div>
  )
}

export default function CausalGraphLegacyPage() {
  return (
    <Suspense fallback={
      <div className="mx-auto flex w-full max-w-[1600px] items-center justify-center p-12 text-xs text-muted-foreground">
        正在跳转到「因果图谱」…
      </div>
    }>
      <CausalGraphLegacyPageInner />
    </Suspense>
  )
}