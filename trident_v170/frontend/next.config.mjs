/** @type {import('next').NextConfig} */
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
      `connect-src 'self' ${backend} ${bridge} ws: wss:`,
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
  images: {
    unoptimized: true,
  },
  // 允许 127.0.0.1 / localhost 访问 _next 静态资源，避免 dev 模式 Cross origin 警告
  allowedDevOrigins: ['127.0.0.1', 'localhost', '0.0.0.0'],
  async rewrites() {
    return [
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
