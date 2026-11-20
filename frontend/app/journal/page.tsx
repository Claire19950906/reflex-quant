// v170 Phase D：原 /journal (反思日记) 内容已并入 /review 顶部总览 +
// ReflectionErrorsPanel + ReviewHistoryPanel。统一入口指向 /review?tab=journal
// （tab 参数暂未启用，留作未来扩展；当前直接显示反思页全部组件）。
//
// 走 next/navigation redirect 是 server-side 行为，无需 'use client'。
import { redirect } from 'next/navigation'

export default function JournalRedirect() {
  redirect('/review?tab=journal')
}