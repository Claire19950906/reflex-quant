'use client'

/**
 * app/crowd/page.tsx — 二级市场人群画像 / 权威度月榜 (v316 新增)
 *
 * 回答两类问题：
 *   「现在二级市场的人在关注什么、是哪一群人」→ 人群画像 9 维 + 权威度月榜
 *   「这些结论可不可信」→ 每条排名带权威度拆解；原始条目可点回原页面核验
 *
 * 数据来源：百度热搜·实时/财经榜、同花顺热门股榜、CoinGecko 趋势、Google News RSS
 * （全部真实抓取，采集失败如实显示，不做占位填充）
 */

import { CrowdPulsePanel } from '@/components/crowd/crowd-pulse-panel'

export default function CrowdPage() {
  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-col gap-1 border-b border-border pb-2">
        <h1 className="flex items-center gap-2 font-mono text-base font-bold tracking-wide text-foreground">
          <span aria-hidden>👥</span>
          人群画像 · 二级市场在关注什么
        </h1>
        <p className="font-mono text-[11px] text-muted-foreground">
          v316 Crowd Pulse · 5 个真实数据源（百度热搜 / 同花顺 / CoinGecko / Google News）·
          默认视图回答「关注什么话题 + 持什么观点」· 跨平台交叉验证定权威度 · 原始条目可回溯原页面
        </p>
      </div>

      <CrowdPulsePanel />
    </div>
  )
}
