// v275: 原 /flows (资金流动) 内容已并入 /discover#disc-flows
// 顶层入口只剩 /discover 一处, 此 URL 仅作深链兼容
import { redirect } from 'next/navigation'

export default function FlowsRedirect() {
  redirect('/discover#disc-flows')
}
