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

// v281 PWA: 客户端组件挂载时注册 service worker（/sw.js）。
// 失败静默 — 不能因为 SW 注册问题影响主流程。
// v282 修复（2026-10-18）：dev 模式不注册 SW — SW stale cache 会
// 引用 .next 重生前的 webpack.js module id，导致客户端 hydration 时
// `__webpack_require__` 拿到 undefined module，
//   "Cannot read properties of undefined (reading 'call')" at options.factory
// 注意：必须用 server 端的 process.env.NODE_ENV（layout.tsx 是 server
// component），不能写 inline JS 字符串里 — 浏览器没 process 全局会 ReferenceError。
const registerSW = process.env.NODE_ENV === 'production'
  ? `if ('serviceWorker' in navigator) { window.addEventListener('load', function () { navigator.serviceWorker.register('/sw.js').catch(function (err) { console.warn('[Trident SW] register failed:', err); }); }); }`
  : ''

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
        {/* v281 PWA: 客户端挂载后注册 service worker */}
        <script dangerouslySetInnerHTML={{ __html: registerSW }} />
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
