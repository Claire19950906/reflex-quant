// v170 Phase D：原 /experiences (经验规则库) 内容已并入 /review 顶部
// 「生效经验规则」卡 + AttributionReviewPanel。统一入口指向 /review?tab=experiences
// （tab 参数暂未启用，留作未来扩展；当前直接显示反思页全部组件）。
//
// 走 next/navigation redirect 是 server-side 行为，无需 'use client'。
import { redirect } from 'next/navigation'

export default function ExperiencesRedirect() {
  redirect('/review?tab=experiences')
}