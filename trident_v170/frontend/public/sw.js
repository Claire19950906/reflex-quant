// Trident Quant Platform — Service Worker (v281)
// 目的：让 /review / /replay / /connectivity 关键页可在弱网/离线时返回缓存壳，
// 实时数据仍依赖在线，但骨架页面与样式即时可读。
//
// 缓存策略：
//   - app shell（HTML / CSS / JS）：stale-while-revalidate
//   - /api/* 与 SSE 流：永远 network-first（数据绝不能给旧值）
//   - /api/connectivity/status / /api/sources/status：network-first 带 5s 兜底缓存
//
// v281+ 修复（2026-10-18）：bump CACHE_VERSION 清旧 cache + dev 模式不注册 SW。
// 旧 cache 残留导致浏览器仍引用 .next 删前的 webpack.js module id，
// `__webpack_require__` 拿到的 module 没 factory，触发
//   "Cannot read properties of undefined (reading 'call')" at options.factory

const CACHE_VERSION = 'trident-v282-restore-2026-10-18'
const SHELL_CACHE = `${CACHE_VERSION}-shell`
const DATA_CACHE = `${CACHE_VERSION}-data`

const SHELL_URLS = [
  '/',
  '/review',
  '/replay',
  '/connectivity',
  '/sources',
  '/discover',
  '/analysis-process',
  '/manifest.json',
  '/icon-light-32x32.png',
  '/icon-dark-32x32.png',
]

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS).catch(() => undefined)),
  )
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys()
      await Promise.all(
        keys.filter((k) => !k.startsWith(CACHE_VERSION)).map((k) => caches.delete(k)),
      )
      await self.clients.claim()
    })(),
  )
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)

  // SSE 流与 POST 请求：永远走网络，不缓存
  if (req.headers.get('accept')?.includes('text/event-stream')) return

  // /api/* 实时数据：network-first；失败时对 GET 类 API 回退到 DATA_CACHE（≤ 30s 内可用）
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.includes('/sse') || url.pathname.includes('/stream')) return
    event.respondWith(networkFirstWithCache(req, DATA_CACHE, 5_000))
    return
  }

  // app shell：stale-while-revalidate（先返缓存，后台更新）
  event.respondWith(staleWhileRevalidate(req, SHELL_CACHE))
})

async function networkFirstWithCache(req, cacheName, maxAgeMs) {
  const cache = await caches.open(cacheName)
  try {
    const fresh = await fetch(req)
    if (fresh.ok) cache.put(req, fresh.clone()).catch(() => undefined)
    return fresh
  } catch (err) {
    const cached = await cache.match(req)
    if (cached) {
      const dateHeader = cached.headers.get('date')
      const age = dateHeader ? Date.now() - new Date(dateHeader).getTime() : Infinity
      if (age <= maxAgeMs) return cached
    }
    return cached || new Response(JSON.stringify({ error: 'offline' }), {
      status: 503, headers: { 'Content-Type': 'application/json' },
    })
  }
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName)
  const cached = await cache.match(req)
  const fetchPromise = fetch(req).then((res) => {
    if (res.ok) cache.put(req, res.clone()).catch(() => undefined)
    return res
  }).catch(() => cached)
  return cached || fetchPromise
}