'use client'

import { useEffect, useState } from 'react'
import { Globe2, Users, GitBranch, Clock, CheckCircle2, AlertCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 🌍 多机协作 · ⏰ 主力交易时间(参考因素)
 *
 * 用户原话:「多机协作」「主力交易时间」
 * 用户偏好(2026-07):多机是参考因素,不是重要决策因素。
 *
 * 两个部分:
 *   · 多机协作 — 在 Git/Codespaces/Live Share 三种方式中选最合适的(默认 Git)
 *   · 时区地图 — 显示当前 UTC + 三大主力盘开闭 + 时区切换警告
 *
 * 注:不试图调用任何 git 状态(浏览器里做不到),
 * 显示「提示卡片 + 推荐流程」即可。
 */

const TIMEZONE_PRESETS = [
  { id: 'asia',     label: '🌏 亚盘',  cities: ['东京 09:00', '香港 09:30', '新加坡 09:00'], utc: 8  },
  { id: 'europe',   label: '🌍 欧盘',  cities: ['伦敦 08:00', '法兰克福 09:00', '巴黎 09:00'], utc: 1  },
  { id: 'us',       label: '🌎 美盘',  cities: ['纽约 09:30', '纳斯达克 09:30', 'CME 09:00'], utc: -5 },
  { id: 'crypto',   label: '₿ 加密24×7', cities: ['BTC/USDT 24h', 'ETH/USDT 24h', 'SOL/USDT 24h'], utc: 0 },
]

const COLLAB_OPTIONS: { id: string; label: string; when: string; tip: string }[] = [
  { id: 'git',       label: '📦 Git push/pull',     when: '适合长期 / 异步',  tip: '两台机器都是全功能开发机,commit → push → 拉另一台同步' },
  { id: 'live',      label: '🖱 VS Code Live Share', when: '适合临时 / 实时',  tip: '你和另一台机器一起编辑(同一项目);cursor 实时同步' },
  { id: 'codespaces', label: '☁️ GitHub Codespaces', when: '适合出差 / 异构',  tip: '云端 IDE;在低配机器上也能跑全功能 dev 环境' },
]

function pad2(n: number) { return n.toString().padStart(2, '0') }

function currentUtc(): string {
  const d = new Date()
  return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())} UTC`
}

function sessionOpen(utc: number): 'open' | 'closed' {
  // 简单判断:UTC 0-23,主力盘是亚 1-9 UTC,欧 8-16 UTC,美 14-22 UTC
  const h = new Date().getUTCHours()
  switch (utc) {
    case 8:  return (h >= 0 && h < 9) ? 'open' : 'closed'
    case 1:  return (h >= 7 && h < 16) ? 'open' : 'closed'
    case -5: return (h >= 14 || h < 22) ? 'open' : 'closed'
    default: return 'open'
  }
}

export function CollabPanel() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="rounded border border-emerald-500/40 bg-emerald-500/5 p-3">
      <div className="flex items-center gap-2 font-mono text-xs">
        <Globe2 className="h-4 w-4 text-emerald-500" aria-hidden />
        <span className="font-bold uppercase tracking-widest text-emerald-700 dark:text-emerald-300">
          🌍 多机协作 · 时区参考
        </span>
        <span className="rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[9px] font-bold text-emerald-600 dark:text-emerald-400">
          参考因素 · 不参与决策
        </span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          <Clock className="mr-0.5 inline h-3 w-3" />
          {pad2(now.getUTCHours())}:{pad2(now.getUTCMinutes())} UTC
        </span>
      </div>

      <p className="mt-2 text-[11px] leading-7 text-muted-foreground">
        多机 = 你两台设备同步代码 / 一端;主力交易时间 = 决策时心里有数。
        <strong className="text-foreground">两者都按用户偏好只作为参考因素,不进入评分主线。</strong>
      </p>

      {/* 多机协作 */}
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
        {COLLAB_OPTIONS.map((o) => (
          <div key={o.id} className="rounded border border-border bg-card p-2 text-xs">
            <div className="flex items-center gap-1.5 font-mono font-bold text-foreground">
              <Users className="h-3 w-3" />
              {o.label}
            </div>
            <div className="mt-1 text-[10px] text-muted-foreground">{o.when}</div>
            <div className="mt-1 text-[10px] text-foreground/80">{o.tip}</div>
          </div>
        ))}
      </div>

      {/* 推荐流程 */}
      <div className="mt-2 rounded border border-emerald-500/30 bg-emerald-500/5 p-2 text-[11px]">
        <strong className="text-emerald-700 dark:text-emerald-300">✅ 推荐流程(两机开发)</strong>:
        <ol className="ml-4 mt-1 list-decimal text-[10px] text-muted-foreground">
          <li>主机:写代码 / 调试</li>
          <li><code className="rounded bg-background px-1 font-mono">git add . && git push</code> 同步到云端</li>
          <li>副机:<code className="rounded bg-background px-1 font-mono">git pull</code> 拉最新</li>
          <li>副机:做数据观察 / 后台 log 看 / 不在线时编辑器</li>
          <li>切换时反向 push/pull 同步</li>
        </ol>
      </div>

      {/* 时区地图 */}
      <div className="mt-3">
        <div className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
          <GitBranch className="h-3 w-3" /> 时区地图(主力盘 / 加密盘)
        </div>
        <div className="mt-1 grid grid-cols-1 gap-1.5 md:grid-cols-4">
          {TIMEZONE_PRESETS.map((tz) => {
            const status = tz.utc === 0 ? 'open' : sessionOpen(tz.utc)
            return (
              <div key={tz.id} className={cn(
                'rounded border p-2 text-[10px]',
                status === 'open' ? 'border-emerald-500/40 bg-emerald-500/10' : 'border-border bg-secondary/20',
              )}>
                <div className="flex items-center gap-1 font-mono font-bold text-foreground">
                  {status === 'open'
                    ? <CheckCircle2 className="h-3 w-3 text-emerald-500" />
                    : <AlertCircle className="h-3 w-3 text-muted-foreground" />}
                  {tz.label}
                  <span className={cn('ml-auto rounded border px-1 py-0.5 font-mono text-[9px]',
                    status === 'open'
                      ? 'border-emerald-500/40 bg-emerald-500/20 text-emerald-700 dark:text-emerald-300'
                      : 'border-border bg-secondary/30 text-muted-foreground')}>
                    {status === 'open' ? 'OPEN' : 'CLOSED'}
                  </span>
                </div>
                <ul className="mt-1 space-y-0.5 text-[9px] text-muted-foreground">
                  {tz.cities.map((c) => <li key={c}>{c}</li>)}
                </ul>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}