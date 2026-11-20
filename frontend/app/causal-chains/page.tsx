'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

/**
 * 兼容旧路由：所有访问 /causal-chains 自动跳转到统一入口
 * /knowledge-graph（默认 tab 仍是因果图谱 — 因果库通过 tab 切换进入）。
 * 旧菜单/收藏链接不会 404。
 */
export default function CausalChainsLegacyPage() {
  const router = useRouter()
  useEffect(() => {
    router.replace('/knowledge-graph')
  }, [router])
  return (
    <div className="mx-auto flex w-full max-w-[1600px] items-center justify-center p-12 text-xs text-muted-foreground">
      正在跳转到「因果图谱」…
    </div>
  )
}
