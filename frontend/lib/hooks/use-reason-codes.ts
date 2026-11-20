'use client'

/**
 * useReasonCodes — 统一归因码字典（v170 P0+）
 *
 * 设计动机：attribution-review-panel.tsx 第 13 行硬编码：
 *   const REASON_NAMES: Record<number, string> = {
 *     1: '数据缺失', 2: '非活跃时段', ...
 *   }
 * 与后端 review_agent._REASON_NAMES 完全脱钩：后端加第 24 类前端立即失配。
 *
 * 抽出来后：
 *   const { names, version } = useReasonCodes()  // Record<number, string>
 *
 * 实现：后端 GET /review/reason-codes → { codes: { "1": "数据缺失", ... }, version: "v1" }
 * 失败/未上线时回退硬编码 fallback（保持向后兼容）。
 *
 * 注意：本 hook 不强依赖新 endpoint。如果后端暂时未上线 /review/reason-codes，
 * usePoll 内部 catch 掉，结果为空 → 自动走 fallback。
 */

import { useMemo } from 'react'
import { usePoll } from './use-poll'

export type ReasonCodesResponse = {
  codes?: Record<string, string>
  version?: string
}

// 后端 _REASON_NAMES 历史字典（与 backend review_agent.py 同步，作为 fallback）
// v202+ 当前 23 类。任何超过 23 的 ID 视为未知（前端显示「未知 (N)」）。
const FALLBACK_REASON_NAMES: Record<number, string> = {
  1: '数据缺失', 2: '非活跃时段', 3: '忽略关键因素', 4: '游戏规则变化',
  5: '不可监控信息', 6: '模型判断错误', 7: '信号质量差', 8: '时间窗口不匹配',
  9: '数据滞后', 10: '策略参数不匹配', 11: '市场状态误判', 12: '新闻重复/旧闻',
  13: '联动传导忽略', 14: '关键价位/技术位', 15: '仓位/止损设置不当', 16: '入场时机不当',
  17: '模型推理降级', 18: '数据错误', 19: '联动数据缺失/错误', 20: '成交滑点/成本',
  21: '系统通道故障', 22: '新闻已定价/抢跑', 23: '经验误用/过拟合',
}

export function useReasonCodes(): {
  names: Record<number, string>
  version: string | null
} {
  const { data } = usePoll<ReasonCodesResponse>(
    '/review/reason-codes',
    {
      // 长缓存：版本不变不会重拉；版本变了 backend 自己 invalidation
      intervalMs: 0,
      cache: 'default',
    },
  )

  return useMemo(() => {
    if (data?.codes) {
      // 后端返回 string 键，转 number 键
      const converted: Record<number, string> = {}
      for (const [k, v] of Object.entries(data.codes)) {
        const n = Number(k)
        if (Number.isFinite(n)) converted[n] = String(v)
      }
      return { names: converted, version: data.version ?? null }
    }
    return { names: FALLBACK_REASON_NAMES, version: null }
  }, [data])
}
