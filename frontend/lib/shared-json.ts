// lib/shared-json.ts — 同一 URL 的并发 GET 去重（v499）
//
// 背景：首页/反思页有 3 个组件各自 5s 轮询 /api/events/today（参数完全相同），
//   45s 内同一接口被调 15 次，数据库被读 3 倍。计数接口本身很轻，但叠上
//   /api/market/prices 的 3s 轮询后，整机读放大明显。
// 做法：按 URL 做 4s TTL + in-flight 合并；**缓存的是解析后的 JSON**（Response
//   的 body 只能消费一次，缓存 Response 会让第二个调用方抛 body already used）。
// 边界：只用于幂等的 GET 计数/摘要接口；失败不缓存（下次重新拉）。

type Entry = { at: number; v?: unknown; p?: Promise<unknown> }

const cache = new Map<string, Entry>()
const inflight = new Map<string, Promise<unknown>>()

export function sharedJson<T>(url: string, ttlMs = 4000, init?: RequestInit): Promise<T> {
  const now = Date.now()
  const hit = cache.get(url)
  if (hit && hit.v !== undefined && now - hit.at < ttlMs) return Promise.resolve(hit.v as T)

  const pending = inflight.get(url)
  if (pending) return pending as Promise<T>

  const p = fetch(url, init)
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<T>
    })
    .then((v) => {
      cache.set(url, { at: Date.now(), v })
      return v
    })
    .finally(() => {
      inflight.delete(url)
    })

  inflight.set(url, p as Promise<unknown>)
  // 失败时清掉 inflight 已由 finally 完成；这里的定时器只负责过期清理
  setTimeout(() => {
    const cur = cache.get(url)
    if (cur && Date.now() - cur.at >= ttlMs) cache.delete(url)
  }, ttlMs + 500)
  return p as Promise<T>
}
