import { Analytics } from '@vercel/analytics/next'
import type { Metadata, Viewport } from 'next'
import { DashboardHeader } from '@/components/quant/dashboard-header'
import { AppNav } from '@/components/app-nav'
import { SourceStatusAlerts } from '@/components/source-status-alerts'
import { MarketHoursBar } from '@/components/quant/market-hours-bar'
import { ToastProvider } from '@/components/ui/toast'
import './globals.css'

// 服务器访问 Google Fonts 被墙（ECONNRESET）时 build 失败：
// 改用系统字体栈（globals.css 定义），不依赖 next/font 联网拉字体。
// eslint-disable-next-line @typescript-eslint/no-unused-vars
const geistSans = { variable: '' }
// eslint-disable-next-line @typescript-eslint/no-unused-vars
const geistMono = { variable: '' }

export const metadata: Metadata = {
  title: 'Trident Quant Platform',
  description:
    'Institutional-grade AI quantitative trading dashboard with real-time event bus, live signals, and AI sentiment analytics.',
  generator: 'v0.app',
  manifest: '/manifest.json',
  appleWebApp: {
    capable: true,
    statusBarStyle: 'black-translucent',
    title: 'Trident',
  },
}

// v307: 已彻底移除 Service Worker（v308 起）。
//
// 为什么删（不是继续修）：
//   1. SW 的 `fetch` handler 拦截 `/api/*` 并做 network-first + 缓存兜底，
//      网络失败时用 `new Response(..., {status:503})` 伪造响应。实测表现为
//      attribution 页 MU 的 `/api/market/history/MU?interval=10m` 在浏览器里
//      稳定返 503（耗时 3~4ms，纯缓存命中，没走网络），而同一 URL 用 curl
//      直连 bridge(8100) / Next rewrite(3030) / 24 并发压测全部 200。
//      → 前端把"网络故障"误读成"后端 503"，tryOne 重试 3 次全是缓存里的毒响应。
//   2. SHELL_CACHE 缓存 `/_next/static/*`（stale-while-revalidate），在 `.next`
//      重建后仍吐旧 webpack chunk，引用已删除的 module id，导致 hydration 报
//      "Cannot read properties of undefined (reading 'call')"。
//   3. SW 的设计目标是"弱网/离线下 /review、/replay 仍可读缓存壳"，但 v303
//      起后端已由 bridge(8100) 全量接管且稳定返回结构化软空值，实时数据不再
//      依赖离线兜底，该目标已过时。
//
// 保留 manifest.json / 图标：PWA 安装能力不受影响，只是没有离线壳缓存。
// 迁移提示：已安装过 SW 的浏览器需手动清一次（DevTools → Application →
// Service Workers → Unregister，或 Storage → Clear site data）。

export const viewport: Viewport = {
  colorScheme: 'light dark',
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#f5f7fa' },
    { media: '(prefers-color-scheme: dark)', color: '#151619' },
  ],
}

const themeScript = `(function(){try{var t=localStorage.getItem('trident-theme');var d=t?t==='dark':matchMedia('(prefers-color-scheme: dark)').matches;document.documentElement.classList.toggle('dark',d)}catch(e){}})()`

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`bg-background ${geistSans.variable} ${geistMono.variable}`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <link rel="apple-touch-icon" href="/apple-icon.png" />
      </head>
      <body className="bg-background font-sans antialiased">
        {/* v302+ P0: 顶层 ToastProvider — 替代散落各页的 window.confirm/alert。
            任何页面用 useToast() 都能拿到 toast.{success,error,warning,info,confirm}。 */}
        <ToastProvider>
          <div className="app-shell flex h-screen flex-col overflow-hidden">
            <DashboardHeader />
            {/* 市场状态条从 AppNav 下方上移至 DashboardHeader 下方、AppNav 上方。
                DashboardHeader (h-16) sticky top-0，本条 sticky top-16 紧跟其下；
                AppNav sticky top-[88px]（h-16 + h-6 = 88px）压在最下，三层都钉住。 */}
            <MarketHoursBar />
            <AppNav />
            {/* v302+ P0: 改用统一的滚动容器 — 三个 sticky 头部固定后,内容区只剩
                flex-1 min-h-0 overflow-y-auto 这一段可滚动;页面内各组件不再需要
                各自管 overflow,避免「body 滚 + 组件内 main 滚」的双滚动条。
                anti-bot-guard 仍能通过 document.addEventListener('scroll', ...) 捕获
                因为 scroll 事件在 capture 阶段向上传递到 document。 */}
            <main
              id="app-scroll-root"
              data-scroll-root
              className="thin-scroll flex-1 min-h-0 overflow-y-auto"
            >
              {children}
            </main>
          </div>
          {/* 全局信息源异常 toast（不影响任何模型推理；纯前端轮询） */}
          <SourceStatusAlerts />
        </ToastProvider>
        {process.env.NODE_ENV === 'production' && <Analytics />}
      </body>
    </html>
  )
}
