'use client'

/**
 * v302+ P6: /review 键盘快捷键 hook — 全局 keydown 监听,实现 power user 工作流。
 *
 * 设计要点:
 *  - 不在 input/textarea/contenteditable 上拦截:用户在搜索框/笔记里打字不受影响
 *  - 不与现有组件冲突: AbTablist 已经在 button 上拦 Arrow/Home/End,这里不再拦这些
 *  - 修饰键(Ctrl/Cmd/Alt/Meta)按下时一律不响应: 避免抢浏览器/系统的快捷键
 *  - 用 ref 回调报告按下的数字键: 调用方(用 useReviewSections 拿到 N 段)决定跳到哪
 *  - 单一 keydown listener (document),不挂 window: SSR 安全 + 不会重复触发
 *  - onCopyMarkdown / onToggleExpand / onShowHelp / onClose 全部回调: 测试和上层编排解耦
 *
 * 快捷键表:
 *   1-9  跳转到第 N 个可见 section(无 N 时不响应,比如只剩 7 段就只 1-7 有效)
 *   j    下一个 section
 *   k    上一个 section
 *   g g  回到顶部(连续两次 g,中间可短)
 *   G    跳到底部
 *   e    切换"全部展开/全部折叠"
 *   c    复制当前视图快照为 Markdown
 *   ?    弹出快捷键说明
 *   Esc  关闭弹窗 / 清除错误码
 *
 * 为什么不抽成 useHotkeys 通用 hook:
 *  - 只有 /review 用,抽出 N 个 prop 反而绕
 *  - 快捷键表是静态的(写在 docs / 帮助弹窗里),不配置化更易读
 *  - 项目 0 新依赖原则,不引 react-hotkeys-hook
 */

import { useEffect, useRef } from 'react'

export type ReviewShortcutsOptions = {
  /** 跳到第 N 个 section(index 0-based)。无 N 时(数组长度 < N)无操作 */
  onJumpToSection: (index: number) => void
  /** 总 section 数(用于过滤超出范围的 1-9 键) */
  sectionCount: number
  /** 当前是否处于"全部展开"状态(决定 e 键的语义) */
  allExpanded: boolean
  /** 切换"全部展开 / 全部折叠" */
  onToggleExpandAll: () => void
  /** 复制当前视图快照为 Markdown */
  onCopyMarkdown: () => void
  /** 显示快捷键帮助弹窗 */
  onShowHelp: () => void
  /** Esc: 关闭弹窗或清除错误码 */
  onEscape: () => void
  /** 启用 / 禁用(用 useState 控制,默认 true) */
  enabled?: boolean
}

/** 判断 target 是否在文本编辑区 — 这种情况不抢键 */
function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (target.isContentEditable) return true
  return false
}

/** 判断 key 是否"无修饰键的纯字符键" — 防止 Cmd+R / Alt+1 误触
 *  允许 Shift: 大写字母(G/j 等)只能通过 Shift 输入,不放开就触发不到 */
function isBareKey(e: KeyboardEvent): boolean {
  return !e.ctrlKey && !e.metaKey && !e.altKey
}

export function useReviewShortcuts(opts: ReviewShortcutsOptions) {
  const {
    onJumpToSection,
    sectionCount,
    onToggleExpandAll,
    onCopyMarkdown,
    onShowHelp,
    onEscape,
    enabled = true,
  } = opts
  const allExpanded = opts.allExpanded

  // 用 ref 持有最新回调,避免 keydown listener 每次 options 变化都重新挂载
  const cbRef = useRef({
    onJumpToSection,
    onToggleExpandAll,
    onCopyMarkdown,
    onShowHelp,
    onEscape,
  })
  cbRef.current = { onJumpToSection, onToggleExpandAll, onCopyMarkdown, onShowHelp, onEscape }

  // g g 双击检测(用 lastG 时间戳,2 秒内第二次 g 触发)
  const lastGRef = useRef<number>(0)

  useEffect(() => {
    if (!enabled) return
    function handle(e: KeyboardEvent) {
      if (isEditableTarget(e.target)) return
      if (!isBareKey(e)) return

      // 数字键 1-9
      if (/^[1-9]$/.test(e.key)) {
        const idx = Number(e.key) - 1
        if (idx < sectionCount) {
          e.preventDefault()
          cbRef.current.onJumpToSection(idx)
        }
        return
      }

      switch (e.key) {
        case 'j': {
          // j / k 没法在 hook 内知道当前 section index,也不应该知道 — 它只是个 "next"
          // 实际上我们让父组件在 onJumpToSection 内自己计算 currentIndex + 1
          e.preventDefault()
          cbRef.current.onJumpToSection(-1) // -1 = 哨兵,表示"下一个"
          break
        }
        case 'k': {
          e.preventDefault()
          cbRef.current.onJumpToSection(-2) // -2 = 哨兵,"上一个"
          break
        }
        case 'g': {
          const now = Date.now()
          if (now - lastGRef.current < 500) {
            e.preventDefault()
            cbRef.current.onJumpToSection(-3) // -3 = top
            lastGRef.current = 0
          } else {
            lastGRef.current = now
          }
          break
        }
        case 'G': {
          // shift+g → bottom
          e.preventDefault()
          cbRef.current.onJumpToSection(-4) // -4 = bottom
          break
        }
        case 'e': {
          e.preventDefault()
          cbRef.current.onToggleExpandAll()
          break
        }
        case 'c': {
          e.preventDefault()
          cbRef.current.onCopyMarkdown()
          break
        }
        case '?': {
          e.preventDefault()
          cbRef.current.onShowHelp()
          break
        }
        case 'Escape': {
          cbRef.current.onEscape()
          break
        }
        default:
          break
      }
    }

    document.addEventListener('keydown', handle)
    return () => document.removeEventListener('keydown', handle)
  }, [enabled, sectionCount, allExpanded, onToggleExpandAll])

  // 暴露 allExpanded 状态,供 UI 显示当前是否全部展开(给帮助弹窗用)
  return { allExpanded }
}
