'use client'

import { useEffect, useState } from 'react'

/**
 * v394 · 短链：/d/11555 → /counterfactual?id=11555
 *
 * 为什么单独做一条路由：
 *  - 分享时 `/counterfactual?id=11555` 又长又容易被聊天软件截断；
 *  - `/d/11555` 短，粘到微信/飞书里不会被折行；
 *  - 用 replace 而不是 push，避免用户按返回键时在两条路由之间来回跳。
 */
export default function DecisionShortLink() {
  const [id, setId] = useState('')
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const seg = window.location.pathname.split('/').filter(Boolean).pop() || ''
    const n = Number(seg)
    if (Number.isFinite(n) && n > 0) {
      setId(String(n))
      const target = `/counterfactual?id=${n}`
      const timer = setTimeout(() => {
        try {
          window.location.replace(target)
        } catch {
          setFailed(true)
        }
      }, 60)
      return () => clearTimeout(timer)
    }
    setFailed(true)
    return undefined
  }, [])

  return (
    <main className="mx-auto flex w-full max-w-[720px] flex-1 flex-col items-center justify-center gap-3 p-8 text-center font-mono text-xs text-muted-foreground">
      {!failed && (
        <>
          <div className="text-sm text-foreground">正在打开 #{id || '…'} 的五条路径</div>
          <div>实际 / 不进场 / 反向 / 延迟 / 半仓</div>
        </>
      )}
      {failed && (
        <>
          <div className="text-sm text-foreground">短链格式不对</div>
          <div>正确形如 /d/11555，数字是决策编号（见历史回溯页每笔的 #号）。</div>
        </>
      )}
      {id && (
        <a
          href={`/counterfactual?id=${id}`}
          className="rounded border border-border px-3 py-1.5 text-foreground hover:border-primary hover:text-primary"
        >
          手动打开 →
        </a>
      )}
    </main>
  )
}
