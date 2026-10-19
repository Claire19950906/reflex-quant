/// <reference types="vitest" />
import { defineConfig } from 'vitest/config'
import path from 'node:path'

export default defineConfig({
  test: {
    // jsdom: 让 DOM API(URLSearchParams / fetch / window.setTimeout)可用
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: [
      // 单元测试:lib/** + components/**(纯函数) + app/**(_section 等薄壳组件)
      'lib/**/*.test.{ts,tsx}',
      'components/**/*.test.{ts,tsx}',
      'app/**/*.test.{ts,tsx}',
    ],
    // use-api-poll 的 setInterval / setTimeout 测试需要可控的 timer
    // 这里用 modern fake timers(Vitest 默认),用 vi.advanceTimersByTime 推进
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      // 先把最高价值模块纳入覆盖率门槛 — 防止后续 PR 把它们绕开
      include: [
        'lib/api.ts',
        'lib/hooks/use-api-poll.ts',
        'lib/data-channel-standards.tsx',
        'components/replay/analysis-parse.ts',
      ],
      thresholds: {
        // 起步宽:v275+ 把门槛放宽到 25% — 让现有的纯逻辑模块(hook / 解析函数)
        // 被覆盖,大 UI 文件(data-channel-standards.tsx 有 619 行包含 badge / selector)
        // 允许先低覆盖,后续 PR 单独补测试再加严。
        lines: 25,
        functions: 25,
        branches: 50,
        statements: 25,
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
    },
  },
  // v302+ 启用 esbuild automatic JSX runtime,让 *.test.tsx 不用 import React
  esbuild: {
    jsx: 'automatic',
  },
})
