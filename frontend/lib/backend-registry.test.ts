/**
 * lib/backend-registry.test.ts
 *
 * 后端能力台账的不变量测试。
 *
 * 台账(backend-registry.ts)是「后端零引用端点 → 归位/下线」的唯一判定来源,
 * 一旦被改坏(端点重复登记、域指向不存在的页面、loader 与台账脱节),
 * 归位结论就会悄悄失真 —— 这类问题不会在编译期暴露, 只能靠测试守住。
 */

import { describe, it, expect } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import {
  BACKEND_DOMAINS,
  WIRED_DOMAINS,
  domainsForHome,
  findDuplicateEndpoints,
  getDomain,
  isWired,
  totalEndpointCount,
} from './backend-registry'
import { BACKEND_LOADERS } from './api-backend'

/** 通用面板(<BackendCapabilityPanel domain="...">)实际挂载的域 —— 本次归位的 8 个 */
const PANEL_MOUNTED_DOMAINS = [
  'beliefs',
  'signal-weights',
  'llm-stats',
  'ops-inspection',
  'exchange-mode',
  'reflection-deep',
  'journal',
  'strategies',
]

const FRONTEND_ROOT = path.resolve(__dirname, '..')

function collectTsxFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.next') continue
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) collectTsxFiles(full, acc)
    else if (/\.tsx$/.test(entry.name)) acc.push(full)
  }
  return acc
}

/** 扫源码: 找出所有 <BackendCapabilityPanel domain="KEY" 的挂载点 */
function scanPanelMounts(): Map<string, string[]> {
  const mounts = new Map<string, string[]>()
  const files = [
    ...collectTsxFiles(path.join(FRONTEND_ROOT, 'app')),
    ...collectTsxFiles(path.join(FRONTEND_ROOT, 'components')),
  ]
  for (const file of files) {
    // 面板组件自身的文件里有一段用法示例注释, 会误判成「挂载点」—— 排除
    if (file.endsWith(path.join('components', 'quant', 'backend-capability-panel.tsx'))) continue
    // 测试文件里的 JSX 是测试用例, 不是真实挂载点
    if (/\.(test|spec)\.tsx$/.test(file)) continue
    const src = fs.readFileSync(file, 'utf-8')
    const re = /<BackendCapabilityPanel[^>]*domain="([^"]+)"/g
    let m: RegExpExecArray | null
    while ((m = re.exec(src)) !== null) {
      const rel = path.relative(FRONTEND_ROOT, file)
      const list = mounts.get(m[1]) ?? []
      list.push(rel)
      mounts.set(m[1], list)
    }
  }
  return mounts
}

describe('backend-registry 台账不变量', () => {
  it('域 key 唯一', () => {
    const keys = BACKEND_DOMAINS.map((d) => d.key)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('端点不重复登记(一个端点只能属于一个域)', () => {
    expect(findDuplicateEndpoints()).toEqual([])
  })

  it('每个域都有判定依据与说明', () => {
    for (const d of BACKEND_DOMAINS) {
      expect(d.label, `域 ${d.key} 缺 label`).toBeTruthy()
      expect(d.summary.length, `域 ${d.key} 缺 summary`).toBeGreaterThan(0)
      expect(d.verdict.length, `域 ${d.key} 缺 verdict`).toBeGreaterThan(0)
      expect(['exposed', 'downline']).toContain(d.status)
    }
  })

  it('判定归位的域必须有宿主页, 判定下线的域不占界面位置', () => {
    for (const d of BACKEND_DOMAINS) {
      if (d.status === 'exposed') {
        expect(d.home, `域 ${d.key} 归位了但没写 home`).toBeTruthy()
        expect(d.home, `域 ${d.key} 的 home 不应是占位符`).not.toBe('—')
        expect(d.home.startsWith('/'), `域 ${d.key} 的 home 必须是路由`).toBe(true)
      } else {
        expect(d.home, `域 ${d.key} 下线了却还挂着界面位置`).toBe('—')
      }
    }
  })

  it('WIRED_DOMAINS 里的域必须真实存在', () => {
    for (const key of WIRED_DOMAINS) {
      expect(getDomain(key), `WIRED_DOMAINS 含未知域 ${key}`).toBeDefined()
      expect(isWired(key)).toBe(true)
    }
  })

  it('已接线的域必须在台账里标为 exposed', () => {
    for (const key of WIRED_DOMAINS) {
      expect(getDomain(key)?.status, `域 ${key} 已接线却不是 exposed`).toBe('exposed')
    }
  })

  it('loader 的 key 必须与台账域一一对应(不得有孤儿 loader)', () => {
    for (const key of Object.keys(BACKEND_LOADERS)) {
      expect(getDomain(key), `loader ${key} 在台账里没有对应域`).toBeDefined()
    }
  })

  it('已接线的域必须有 loader(面板才有数据可渲染)', () => {
    // 例外: 这些域不是在通用面板里渲染的, 或者本来就已在既有面板里 —
    //   paper-approvals → 专用面板 components/replay/reflection-improve-panel.tsx
    //   其余            → 本来就已在既有面板里(审计确认无需新增面板)
    const noLoader = [
      'paper-approvals',
      'kg-improvements',
      'causal-chains',
      'event-atoms',
      'ai-prompts',
      'analysis-process-page',
    ]
    for (const key of WIRED_DOMAINS) {
      if (noLoader.includes(key)) continue
      expect(BACKEND_LOADERS[key], `域 ${key} 标为已接线但没有 loader`).toBeDefined()
    }
  })

  it('domainsForHome 只返回该页面的 exposed 域', () => {
    const forReview = domainsForHome('/review')
    expect(forReview.length).toBeGreaterThan(0)
    for (const d of forReview) {
      expect(d.home).toBe('/review')
      expect(d.status).toBe('exposed')
    }
  })

  it('端点总数与台账规模一致(防止手改时漏同步)', () => {
    expect(totalEndpointCount()).toBe(
      BACKEND_DOMAINS.reduce((n, d) => n + d.endpoints.length, 0),
    )
  })

  // ══════════════════════════════════════════════════════════════════
  // 归位闭环: 台账说「已归位」, 源码里就必须真的挂上 —— 否则界面上零入口,
  // 而台账却显示「已暴露」, 这是最隐蔽的失真(编译期查不出来, 单测能查)。
  // ══════════════════════════════════════════════════════════════════
  it('通用面板实际挂载的域集合 = 预期集合', () => {
    const mounts = scanPanelMounts()
    const mounted = [...mounts.keys()].sort()
    expect(mounted).toEqual([...PANEL_MOUNTED_DOMAINS].sort())
  })

  it('每个挂载的域在台账里都存在且标为 exposed', () => {
    const mounts = scanPanelMounts()
    for (const key of mounts.keys()) {
      const d = getDomain(key)
      expect(d, `挂载了未登记的域 ${key}`).toBeDefined()
      expect(d!.status, `域 ${key} 已挂载却不是 exposed`).toBe('exposed')
      expect(isWired(key), `域 ${key} 已挂载却不在 WIRED_DOMAINS`).toBe(true)
      expect(BACKEND_LOADERS[key], `域 ${key} 已挂载却没有 loader`).toBeDefined()
    }
  })

  it('挂载点确实出现在该域声明的宿主页上(台账 home 与实际一致)', () => {
    const mounts = scanPanelMounts()
    for (const [key, files] of mounts) {
      const home = getDomain(key)!.home // 形如 /knowledge-graph
      const routeDir = path.join('app', home.replace(/^\//, ''))
      const routeFiles = collectTsxFiles(path.join(FRONTEND_ROOT, routeDir))
      const ok = files.some((f) => {
        // 直接挂在页面里
        if (f.startsWith(routeDir + path.sep)) return true
        // 挂在共享组件里 —— 该组件必须被这个页面 import
        const base = path.basename(f).replace(/\.tsx$/, '')
        return routeFiles.some((rf) => {
          const src = fs.readFileSync(rf, 'utf-8')
          return src.includes(`/${base}'`) || src.includes(`/${base}"`)
        })
      })
      expect(ok, `域 ${key} 挂载在 ${files.join(',')} 但 home 是 ${home}`).toBe(true)
    }
  })

  it('宿主页确实引入了通用面板组件(防止删 import 而留 JSX)', () => {
    const mounts = scanPanelMounts()
    for (const [key, files] of mounts) {
      for (const f of files) {
        const src = fs.readFileSync(path.join(FRONTEND_ROOT, f), 'utf-8')
        expect(
          src.includes("import { BackendCapabilityPanel }"),
          `${f} 用了 <BackendCapabilityPanel domain="${key}"> 但没 import 组件`,
        ).toBe(true)
      }
    }
  })
})
