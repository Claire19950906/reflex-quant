// v275: 原 /flows (资金流动) 内容已并入 /discover#disc-flows
// v499: 深链里带 #fragment 会让客户端在内容挂载前就跳到锚点, 实测直接打开
//   /flows 得到一屏空白(要再刷一次才出内容)。改为跳到 /discover 顶部,
//   由页面自己的「资金流向」分组承接。
import { redirect } from 'next/navigation'

export default function FlowsRedirect() {
  redirect('/discover')
}
