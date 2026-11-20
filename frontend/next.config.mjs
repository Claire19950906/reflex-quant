/** @type {import('next').NextConfig} */
// 本机后端由 8100(bridge) 托管全套路由；默认兜底指 8100，NEXT_PUBLIC_API_URL 可显式覆盖。
const backend = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8100'
// v180+ P0+: 桥接服务地址 (fallback 目标, 同源 SQLite DB 读真实数据)
// 必须与 CSP connect-src + lib/api.ts 的 BRIDGE_BASE 保持一致
const bridge = process.env.NEXT_PUBLIC_BRIDGE_URL || 'http://127.0.0.1:8100'

/**
 * P1 安全修复：统一前端安全头 (CSP + X-Frame-Options + X-Content-Type-Options
 * + Referrer-Policy + Permissions-Policy + HSTS)。
 *
 * CSP 说明:
 *   - default-src 'self'                 默认只允许同源资源
 *   - script-src 'self' 'unsafe-inline' 'unsafe-eval'  Next.js dev/HMR 需要
 *   - connect-src 'self' + backend       API/XHR/SSE 目标
 *   - img-src 'self' data: blob:         支持截图 + 对象 URL
 *   - style-src 'self' 'unsafe-inline'   CSS modules + styled-jsx
 *   - frame-ancestors 'none'             防 clickjacking（等价 X-Frame-Options: DENY）
 *   - base-uri 'self'                    防 <base> 标签劫持
 *   - form-action 'self'                 防表单外跳
 *   - object-src 'none'                  防 Flash/Java 老插件
 *   - upgrade-insecure-requests          HTTPS-only (HTTP 部署时自动升级)
 */
const SECURITY_HEADERS = [
  // Content-Security-Policy — 防 XSS + 数据外泄
  {
    key: 'Content-Security-Policy',
    value: [
      "default-src 'self'",
      // Next.js 13+ dev mode + 部分 library 需要 unsafe-inline / unsafe-eval
      // 生产环境可收紧为 'self' + nonce（不在本次范围）
      "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: blob: https:",
      // backend 与 bridge 默认同址(8100) → 去重, 避免 CSP 里出现重复源
      `connect-src 'self' ${[...new Set([backend, bridge])].join(' ')} ws: wss:`,
      "font-src 'self' data:",
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "object-src 'none'",
      "media-src 'self' blob:",
      "worker-src 'self' blob:",
      process.env.NODE_ENV === 'production' ? 'upgrade-insecure-requests' : '',
    ].filter(Boolean).join('; '),
  },
  // X-Frame-Options — 防 clickjacking (CSP frame-ancestors 的 fallback)
  { key: 'X-Frame-Options', value: 'DENY' },
  // X-Content-Type-Options — 防 MIME 嗅探
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  // Referrer-Policy — 防 referer 泄露到第三方
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  // Permissions-Policy — 关掉浏览器敏感 API
  {
    key: 'Permissions-Policy',
    value: [
      'camera=()', 'microphone=()', 'geolocation=()',
      'payment=()', 'usb=()', 'magnetometer=()', 'gyroscope=()',
      'accelerometer=()',
    ].join(', '),
  },
  // HSTS — 仅 HTTPS 部署时有效（dev 无效，安全）
  ...(process.env.NODE_ENV === 'production' ? [{
    key: 'Strict-Transport-Security',
    value: 'max-age=63072000; includeSubDomains; preload',
  }] : []),
  // Cross-Origin-Opener-Policy — 防侧信道（tab 隔离）
  { key: 'Cross-Origin-Opener-Policy', value: 'same-origin' },
]

const nextConfig = {
  // v381: 这台 8GB 机器上默认会开 6-8 个 webpack worker，构建峰值直接把内存打爆，
  //   jetsam 用 SIGKILL 杀掉进程 —— 表现就是构建停在 "Creating an optimized
  //   production build ..." 后再无下文(exit 137)。限 2 个并发 worker。
  experimental: {
    cpus: Number(process.env.NEXT_BUILD_CPUS || 2),
    workerThreads: false,
  },
  images: {
    unoptimized: true,
  },
  // 允许 127.0.0.1 / localhost 访问 _next 静态资源，避免 dev 模式 Cross origin 警告
  allowedDevOrigins: ['127.0.0.1', 'localhost', '0.0.0.0'],
  // v311: 生产构建与 dev server 共用 .next 会互相破坏产物(构建被 dev 的 HMR 覆盖,
  // dev 又会读到半成品)。NEXT_DIST_DIR 可把生产构建产物落到独立目录,
  // 两者互不干扰 —— 这是本机能同时跑 dev(3030) 与 prod(3031)的唯一安全做法。
  ...(process.env.NEXT_DIST_DIR ? { distDir: process.env.NEXT_DIST_DIR } : {}),
  async rewrites() {
    return [
      // 本机后端统一由 8100(bridge) 托管；下面这批是桥接侧显式列出的路由，
      // 其余 /api/:path* 走兜底规则统一反代到 8100。
      { source: '/api/abnormal-attribution', destination: 'http://127.0.0.1:8100/api/abnormal-attribution' },
      { source: '/api/analysis/error-taxonomy', destination: 'http://127.0.0.1:8100/api/analysis/error-taxonomy' },
      { source: '/api/attribution/ai-verify-queue', destination: 'http://127.0.0.1:8100/api/attribution/ai-verify-queue' },
      { source: '/api/attribution/event-scatter', destination: 'http://127.0.0.1:8100/api/attribution/event-scatter' },
      // 多空持仓比（lsr_feed 写入 lsr_samples）只存在于 bridge(8100)
      { source: '/api/crypto/:path*', destination: 'http://127.0.0.1:8100/api/crypto/:path*' },
      { source: '/api/cot/assets', destination: 'http://127.0.0.1:8100/api/cot/assets' },
      { source: '/api/cot/emit-now', destination: 'http://127.0.0.1:8100/api/cot/emit-now' },
      { source: '/api/cot/heatmap', destination: 'http://127.0.0.1:8100/api/cot/heatmap' },
      { source: '/api/cot/pull-now', destination: 'http://127.0.0.1:8100/api/cot/pull-now' },
      { source: '/api/cot/recent', destination: 'http://127.0.0.1:8100/api/cot/recent' },
      { source: '/api/decision/auto-settle-now', destination: 'http://127.0.0.1:8100/api/decision/auto-settle-now' },
      { source: '/api/decision/list', destination: 'http://127.0.0.1:8100/api/decision/list' },
      { source: '/api/decision/record', destination: 'http://127.0.0.1:8100/api/decision/record' },
      { source: '/api/decision/settle-now', destination: 'http://127.0.0.1:8100/api/decision/settle-now' },
      { source: '/api/decision/stats', destination: 'http://127.0.0.1:8100/api/decision/stats' },
      { source: '/api/intraday/backfill', destination: 'http://127.0.0.1:8100/api/intraday/backfill' },
      { source: '/api/intraday/sync', destination: 'http://127.0.0.1:8100/api/intraday/sync' },
      { source: '/api/market/history/:path*', destination: 'http://127.0.0.1:8100/api/market/history/:path*' },
      // 报价条 /api/market/prices 由桥接侧提供，显式反代避免兜底 404。
      { source: '/api/market/prices', destination: 'http://127.0.0.1:8100/api/market/prices' },
      // events 系列在桥接侧已全量实现，显式反代避免事件流退回空态。
      { source: '/api/events', destination: 'http://127.0.0.1:8100/api/events' },
      { source: '/api/events/today', destination: 'http://127.0.0.1:8100/api/events/today' },
      { source: '/api/events/filter-stream', destination: 'http://127.0.0.1:8100/api/events/filter-stream' },
      { source: '/api/events/stream', destination: 'http://127.0.0.1:8100/api/events/stream' },
      { source: '/api/news/impact-evaluate', destination: 'http://127.0.0.1:8100/api/news/impact-evaluate' },
      { source: '/api/news/pull-now', destination: 'http://127.0.0.1:8100/api/news/pull-now' },
      { source: '/api/news/recent', destination: 'http://127.0.0.1:8100/api/news/recent' },
      { source: '/api/ohlcv/daily', destination: 'http://127.0.0.1:8100/api/ohlcv/daily' },
      { source: '/api/ohlcv/pattern-snapshot', destination: 'http://127.0.0.1:8100/api/ohlcv/pattern-snapshot' },
      { source: '/api/ohlcv/refresh-now', destination: 'http://127.0.0.1:8100/api/ohlcv/refresh-now' },
      { source: '/api/tech/anomaly', destination: 'http://127.0.0.1:8100/api/tech/anomaly' },
      { source: '/api/tech/anomaly-history', destination: 'http://127.0.0.1:8100/api/tech/anomaly-history' },
      { source: '/api/tech/signals', destination: 'http://127.0.0.1:8100/api/tech/signals' },
      { source: '/api/tech/structure', destination: 'http://127.0.0.1:8100/api/tech/structure' },
      { source: '/api/v237/calibration', destination: 'http://127.0.0.1:8100/api/v237/calibration' },
      { source: '/api/v237/judgment-quality', destination: 'http://127.0.0.1:8100/api/v237/judgment-quality' },
      { source: '/api/v237/missed-signals', destination: 'http://127.0.0.1:8100/api/v237/missed-signals' },
      { source: '/api/v237/missed-signals/by-dimension', destination: 'http://127.0.0.1:8100/api/v237/missed-signals/by-dimension' },
      { source: '/api/v237/reflection-dimension-map', destination: 'http://127.0.0.1:8100/api/v237/reflection-dimension-map' },
      { source: '/api/v237/reflection-dimension-stats', destination: 'http://127.0.0.1:8100/api/v237/reflection-dimension-stats' },
      { source: '/api/v291/attribution-stats', destination: 'http://127.0.0.1:8100/api/v291/attribution-stats' },
      { source: '/api/v291/causal-chains/evaluate-now', destination: 'http://127.0.0.1:8100/api/v291/causal-chains/evaluate-now' },
      { source: '/api/v291/causal-chains/feedback', destination: 'http://127.0.0.1:8100/api/v291/causal-chains/feedback' },
      { source: '/api/v291/causal-chains/feedback/backfill', destination: 'http://127.0.0.1:8100/api/v291/causal-chains/feedback/backfill' },
      { source: '/api/v291/causal-chains/score-batch', destination: 'http://127.0.0.1:8100/api/v291/causal-chains/score-batch' },
      { source: '/api/v291/causal-chains/score-single', destination: 'http://127.0.0.1:8100/api/v291/causal-chains/score-single' },
      { source: '/api/v291/causal-graph', destination: 'http://127.0.0.1:8100/api/v291/causal-graph' },
      { source: '/api/v291/critical-moves', destination: 'http://127.0.0.1:8100/api/v291/critical-moves' },
      { source: '/api/v291/cross-asset-chains', destination: 'http://127.0.0.1:8100/api/v291/cross-asset-chains' },
      { source: '/api/v291/decision-archive', destination: 'http://127.0.0.1:8100/api/v291/decision-archive' },
      { source: '/api/v291/event-move-attribution', destination: 'http://127.0.0.1:8100/api/v291/event-move-attribution' },
      { source: '/api/v291/event-move-attribution-pending', destination: 'http://127.0.0.1:8100/api/v291/event-move-attribution-pending' },
      { source: '/api/v291/event-move-attribution/rebuild', destination: 'http://127.0.0.1:8100/api/v291/event-move-attribution/rebuild' },
      { source: '/api/v291/funding-rate', destination: 'http://127.0.0.1:8100/api/v291/funding-rate' },
      { source: '/api/v291/gate-calibration', destination: 'http://127.0.0.1:8100/api/v291/gate-calibration' },
      { source: '/api/v291/market-regime', destination: 'http://127.0.0.1:8100/api/v291/market-regime' },
      { source: '/api/v291/move-attributions', destination: 'http://127.0.0.1:8100/api/v291/move-attributions' },
      { source: '/api/v291/orderbook-depth', destination: 'http://127.0.0.1:8100/api/v291/orderbook-depth' },
      { source: '/api/v291/reflections/summary', destination: 'http://127.0.0.1:8100/api/v291/reflections/summary' },
      { source: '/api/v291/shadow-ledger-summary', destination: 'http://127.0.0.1:8100/api/v291/shadow-ledger-summary' },
      { source: '/api/v291/stats', destination: 'http://127.0.0.1:8100/api/v291/stats' },
      { source: '/api/v189/market/all', destination: 'http://127.0.0.1:8100/api/v189/market/all' },
      { source: '/api/:path*', destination: `${backend}/api/:path*` },
      { source: '/uploads/:path*', destination: `${backend}/uploads/:path*` },
    ]
  },
  // P1: 注入安全头到所有响应
  async headers() {
    return [
      {
        source: '/:path*',
        headers: SECURITY_HEADERS,
      },
    ]
  },
}

export default nextConfig
