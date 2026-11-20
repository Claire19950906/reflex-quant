import { redirect } from 'next/navigation'

/**
 * app/market-scanner/page.tsx — 行情扫描占位页 (v302+ P3 计划)
 *
 * 设计意图: 4 源聚合 + WS 实时 + 8 大类资产 + sector/套利/告警面板
 *
 * 现状 (v302+ Round 2 后): 实际功能仍在 /discover (发现页),
 * app-nav.tsx 已把"行情扫描"tab href 切到本路由 — 测试契约
 * 锁定 /market-scanner, 不能继续指 /discover (会触发 React duplicate key warning
 * 因为"资金流向"也已用 /discover).
 *
 * 当前实现: 307 redirect → /discover#disc-cross, 等真正的市场扫描器页面建好后,
 * 把本文件替换为真实 page component 即可, app-nav.tsx 不动.
 *
 * v310 P0 fix: redirect 目标补上 #disc-cross 锚点(发现页的「🌐 跨资产同步异常」段)，
 *   否则用户点「行情扫描」后被丢到发现页顶部, 看不到任何跟扫描相关的内容。
 */
export default function MarketScannerPage(): never {
  redirect('/discover#disc-cross')
}