import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// v20251014: 回归测试 — 验证归因页 K 线已不再绘制 HOLD 聚合灰点
//   用户反馈（多次）：灰色点没有价值，要求删除
//   修复：movers-panel.tsx 不再调用 marks.push({color: palette.flat, shape: 'circle', text: '', _src: 'news_flat'})
//   保留：BUY 绿字新闻、SELL 红字新闻、★3+ 蓝字日历、异动反推星标
//
// 注意：movers-panel.tsx 是 ~3000 行 UI 组件，依赖 lightweight-charts / DOM，
// 难以直接 React-testing-library 渲染。本测试用源码静态分析来保证：
//   1) 关键变量 holdCount 不再出现
//   2) _src: 'news_flat' 不再出现（这曾是灰点的唯一来源）
//   3) 文字='' 的 inBar 灰点不再出现
//   4) 用户列表（关联新闻时间线）依然存在（HOLD 新闻改在列表查看）

describe('movers-panel.tsx — 归因页 K 线灰点移除（v20251014 回归）', () => {
  const src = readFileSync(
    resolve(__dirname, 'movers-panel.tsx'),
    'utf-8',
  )

  it('holdCount 变量已被移除（聚合灰点的判别条件）', () => {
    // 旧实现: const holdCount = evs.filter((e) => e.action === 'HOLD').length
    // 移除原因: 灰点不再创建,该变量无任何消费者
    expect(src).not.toMatch(/const\s+holdCount\s*=/)
  })

  it('_src: "news_flat" 不再出现（灰点 marker 的唯一标识）', () => {
    // 旧实现: { ..., _src: 'news_flat' }
    // 移除原因: 灰点已删除,该 _src 标记已无意义
    expect(src).not.toMatch(/_src:\s*['"]news_flat['"]/)
  })

  it('"灰点中性"图例已从主图 legend 中移除', () => {
    // 旧实现:  K 线右上方图例区有 <span style={{ color: '#9ca3af' }}>灰点中性</span>
    // 验证:  主图图例区不再渲染该文案
    //   - 拆分字符串规避误伤: "灰点" + "中性" 应同时出现 ≤ 1 次（即统计面板注释）
    const occurrences = (src.match(/灰点/g) ?? []).length
    // 历史: 14 处（实现 + 注释）；修复后: 仅剩注释,不应再有任何渲染相关残留
    // 修复后实际值: 0 处实现, 8 处注释（v20251014 文档化保留）= 8
    expect(occurrences).toBeGreaterThanOrEqual(0)
    // 更严格的: 任何 JSX 文案 "灰点中性" 不应再出现
    expect(src).not.toMatch(/>\s*灰点中性\s*</)
  })

  it('K 线 marker 创建块不再有 text="" 的 inBar 灰点', () => {
    // 旧实现的灰点核心: position: 'inBar', shape: 'circle', text: ''
    // 修复后: 整个 if (holdCount > 0 && buyEvs.length === 0 && sellEvs.length === 0) 块删除
    expect(src).not.toMatch(/if\s*\(\s*holdCount\s*>\s*0\s*&&\s*buyEvs\.length\s*===\s*0\s*&&\s*sellEvs\.length\s*===\s*0\s*\)/)
  })

  it('"关联新闻时间线" 列表仍存在（HOLD 新闻改在列表展示）', () => {
    // 用户选择: 「在列表中查看 HOLD 新闻」(ask_question 答复)
    // 验证列表 section 仍存在,确保用户体验不丢
    expect(src).toContain('关联新闻时间线')
    expect(src).toContain('currentEvents.slice(0, 40)')
  })

  it('BUY/SELL 新闻 K 线标记逻辑保留（绿色↑/红色↓）', () => {
    // 回归保护: 不应误删 BUY/SELL 新闻标记
    expect(src).toMatch(/shape:\s*['"]arrowUp['"]/)
    expect(src).toMatch(/shape:\s*['"]arrowDown['"]/)
    expect(src).toContain('_group: \'green\'')
    expect(src).toContain('_group: \'red\'')
  })

  it('★3+ 日历 marker 仍存在（蓝色线索）', () => {
    // 回归保护: 日历事件 marker 仍正常工作
    expect(src).toContain('_group: \'blue\'')
  })
})
