import { redirect } from 'next/navigation'

/**
 * app/capital-flow/page.tsx — 链上资金流向占位页 (v302+ P3 计划)
 *
 * 设计意图: 8 大类资产 × 4 类资金源 (SVG 粒子动画)
 *
 * 现状 (v302+ Round 3 后): 实际功能仍在 /discover (发现页),
 * app-nav.tsx 已把"资金流向"tab href 切到本路由 — 解决 React duplicate key
 * warning (之前"发现"和"资金流向"两个 tab 都用 /discover, key 冲突).
 *
 * 当前实现: 307 redirect → /discover, 等真正的资金流向图页面建好后,
 * 把本文件替换为真实 page component 即可, app-nav.tsx 不动.
 */
export default function CapitalFlowPage(): never {
  redirect('/discover')
}
