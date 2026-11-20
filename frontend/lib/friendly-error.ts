/**
 * 统一的「把技术错误翻成人话」工具（v345.9）
 *
 * 为什么集中到一处：
 *   全站 20+ 个面板都是 `catch (e) { setError(String(e)) }` 然后直接把 `{error}`
 *   打在页面上，于是页面上会出现
 *     `Failed to fetch http://127.0.0.1:8100/api/missed-signals/by-dimension`
 *     `请求超时 30s: http://127.0.0.1:8100/api/market/history/BTC/10m`
 *     `ECONNRESET`
 *   这些对用户既无信息量，又泄漏内部地址/端口。之前只在少数组件里各写一份，
 *   现在收敛成一个函数，新面板直接复用。
 *
 * 用法：
 *   import { friendlyError } from '@/lib/friendly-error'
 *   setErr(friendlyError(e))        // catch 里
 *   <div>{err}</div>                // 渲染时已经是人话
 */

const INTERNAL_URL = /http:\/\/[^\s)'"，,]+/g
const INTERNAL_HOST = /\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+\b/g

export function friendlyError(input: unknown): string {
  const raw = typeof input === 'string' ? input : (input as Error)?.message ?? String(input ?? '')

  // 先抹掉内部地址（顺序很重要：先抹整条 URL，再抹裸 host:port）
  const txt = raw
    .replace(INTERNAL_URL, '后端接口')
    .replace(INTERNAL_HOST, '后端')
    .replace(/\s+/g, ' ')
    .trim()

  if (!txt) return '暂时取不到数据，稍后会自动重试。'

  if (/超时|timeout|timed out|AbortError|aborted/i.test(txt)) {
    return '请求超时：后端这段时间没返回，可能是数据量大或服务正忙，稍等会自动重试。'
  }
  if (/\b5\d\d\b|Bad Gateway|Service Unavailable|Internal Server Error/i.test(txt)) {
    return '后端短暂异常（5xx）：多为服务重启中，稍等会自动恢复。'
  }
  if (/\b404\b|not found/i.test(txt)) {
    return '这些数据在当前后端版本里没有对应接口，已按缺失处理。'
  }
  if (/\b429\b|rate limit|too many requests/i.test(txt)) {
    return '上游限流（429）：数据源要求降频，稍后会自动重试。'
  }
  if (/ECONNRESET|ECONNREFUSED|socket hang up|Network request failed|Failed to fetch|NetworkError/i.test(txt)) {
    return '连接被重置：后端可能刚重启或网络抖动，稍后会自动重试。'
  }
  if (/certificate|SSL|TLS|CERT_/i.test(txt)) {
    return '上游证书/加密握手失败（多为对方站点限制），这条数据源暂时取不到。'
  }
  if (/ENOTFOUND|EAI_AGAIN|getaddrinfo|DNS/i.test(txt)) {
    return '域名解析失败（DNS）：这条数据源暂时取不到，稍后会自动重试。'
  }

  // 其余情况原样返回（但已抹掉内部地址），避免把有用信息也吃掉
  return txt
}

/** 只有需要「保留字段看细节」的地方（如 /connectivity 诊断页）才用它。 */
export function rawError(input: unknown): string {
  return typeof input === 'string' ? input : (input as Error)?.message ?? String(input ?? '')
}
