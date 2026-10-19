'use client'

/**
 * _shortcuts-help.tsx — 快捷键说明弹窗(v302+ P6)
 *  按 ? 打开,Esc 关闭。
 */

import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'

const SHORTCUTS: Array<{ keys: string; desc: string }> = [
  { keys: '1-9', desc: '跳转到第 N 个段位' },
  { keys: 'j / k', desc: '下一个 / 上一个段位' },
  { keys: 'g g', desc: '回到顶部(连续按两次 g)' },
  { keys: 'G', desc: '跳到底部' },
  { keys: 'e', desc: '展开 / 折叠全部段位' },
  { keys: 'c', desc: '复制当前视图为 Markdown' },
  { keys: '?', desc: '显示本帮助' },
  { keys: 'Esc', desc: '关闭弹窗 / 清除错误码' },
]

export function ShortcutsHelp({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  useEffect(() => {
    if (open) closeRef.current?.focus()
  }, [open])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="shortcuts-help-title"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-lg border border-border bg-background p-4 shadow-2xl"
      >
        <header className="flex items-center justify-between border-b border-border pb-2">
          <h2
            id="shortcuts-help-title"
            className="font-mono text-sm font-bold tracking-wide text-foreground"
          >
            键盘快捷键
          </h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-7 w-7 items-center justify-center rounded text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </header>
        <table className="w-full font-mono text-xs">
          <tbody>
            {SHORTCUTS.map((s) => (
              <tr key={s.keys} className="border-b border-border/40 last:border-0">
                <td className="py-1.5 pr-3 align-top">
                  <kbd className="inline-block min-w-12 rounded border border-border bg-secondary px-1.5 py-0.5 text-center text-foreground">
                    {s.keys}
                  </kbd>
                </td>
                <td className="py-1.5 align-top text-muted-foreground">{s.desc}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <footer className="mt-2 border-t border-border pt-2 text-[10px] text-muted-foreground">
          在文本输入框(搜索/笔记)内时,快捷键自动失效,不抢键。
        </footer>
      </div>
    </div>
  )
}
