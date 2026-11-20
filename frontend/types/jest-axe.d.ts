// v302+ P3: jest-axe 的类型声明 (因为它不带 d.ts 包)
//
// vitest 2.x 的模块声明比较脆弱,任何 declare module '@vitest/expect' augmentation
// 都会破坏原模块签名。所以这里只声明 jest-axe 模块本身;toHaveNoViolations matcher
// 的类型通过测试文件里的 a11yHasNoViolations() 辅助函数解决(把 AxeResults 类型
// 强转后再 assert)。

declare module 'jest-axe' {
  export const axe: <T extends Element = Element>(
    container: T,
    options?: Record<string, unknown>,
  ) => Promise<{
    violations: Array<{
      id: string
      impact: string | null
      tags: string[]
      description: string
      help: string
      helpUrl: string
      nodes: unknown[]
    }>
  }>

  export const toHaveNoViolations: () => {
    pass: boolean
    message: () => string
  }
}