// v275: 原 /calendar (宏观日历) 内容已并入 /discover#disc-macro
// 顶层入口只剩 /discover 一处, 此 URL 仅作深链兼容
import { redirect } from 'next/navigation'

export default function CalendarRedirect() {
  redirect('/discover#disc-macro')
}
