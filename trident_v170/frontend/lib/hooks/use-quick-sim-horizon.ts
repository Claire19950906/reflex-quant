'use client'

/**
 * useQuickSimHorizon — 统一「快速评测持仓时长」配置（v170 P0+）
 *
 * 设计动机：review-panel.tsx 和 review-history-panel.tsx 各自
 *   useEffect(() => { fetch('/review/insights') ... 取 quick_sim_horizon_minutes })
 * 重复实现，且 endpoints 路径和字段名耦合到 insights 接口（耦合度高）。
 *
 * 抽出来后：
 *   const horizonMin = useQuickSimHorizon()  // 默认 20，回退 config.py
 *
 * 实现：基于已有 usePoll hook（lib/hooks/use-poll.ts）。
 * 注意：/review/insights 接口里顺带返回 quick_sim_horizon_minutes。
 * 将来若改独立 endpoint，替换 path 即可。
 */

import { usePoll } from './use-poll'

const DEFAULT_HORIZON_MIN = 20  // 与 backend config.py QUICK_SIM_HORIZON_MINUTES 默认对齐

export function useQuickSimHorizon(): number {
  // 只取一次（intervalMs=0），cache: default-store（配置项极少变，ETag 省带宽）
  const { data } = usePoll<{ quick_sim_horizon_minutes?: number }>(
    '/review/insights',
    {
      intervalMs: 0,
      cache: 'default',
      transform: (raw) => {
        // 后端可能返回 insights list（数组）或对象，要兜底
        const r = raw as { quick_sim_horizon_minutes?: number } | unknown
        if (r && typeof r === 'object' && 'quick_sim_horizon_minutes' in (r as object)) {
          return r as { quick_sim_horizon_minutes?: number }
        }
        return null
      },
    },
  )
  return data?.quick_sim_horizon_minutes ?? DEFAULT_HORIZON_MIN
}
