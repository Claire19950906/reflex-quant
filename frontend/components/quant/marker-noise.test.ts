import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  pickImpactMarkers,
  pickAbnormalMarkers,
  impactDirection,
  impactMarkerColor,
  impactMarkerGroup,
  impactAbsPct,
  IMPACT_MIN_ABS_PCT,
  ABNORMAL_COOLDOWN_MS,
} from '@/components/quant/marker-noise'

// v318 回归测试 —— 归因页 K 线 marker 降噪 + 方向着色
//   用户反馈: "满屏红点"。根因三条:
//     1) impact marker 硬编码红色 '#ef4444' → 涨的跌的都红
//     2) impact marker 无最小幅度门槛 → 0.1% 的波动也画一个点
//     3) 异动圆点无门槛 / 无冷却 / 无去重 → 量能激增时连出几十个点
//   修复: marker-noise.ts 纯函数 + movers-panel.tsx 接入

const MIN = 60_000

describe('marker-noise — impact 方向判定', () => {
  it('方向来自涨跌幅符号, 不是 ev_action', () => {
    // 利空消息但价格是涨的 → 方向必须是 up (否则标反)
    expect(impactDirection({ event_time: 1, impact_pct: 2.5, move_direction: 'up' })).toBe('up')
    expect(impactDirection({ event_time: 1, impact_pct: -2.5, move_direction: 'down' })).toBe('down')
  })

  it('move_direction 缺失时回退到 impact_pct 的符号', () => {
    expect(impactDirection({ event_time: 1, impact_pct: 1.2 })).toBe('up')
    expect(impactDirection({ event_time: 1, impact_pct: -1.2 })).toBe('down')
    expect(impactDirection({ event_time: 1, impact_pct: 0 })).toBe('flat')
    expect(impactDirection({ event_time: 1 })).toBe('flat')
  })

  it('颜色涨绿跌红 (不再是硬编码红)', () => {
    const up = impactMarkerColor({ event_time: 1, impact_pct: 3 })
    const down = impactMarkerColor({ event_time: 1, impact_pct: -3 })
    expect(up).toBe('#22c55e')
    expect(down).toBe('#ef4444')
    expect(up).not.toBe(down)
  })

  it('图例分组随方向: 涨→green, 跌→red, 无方向→不归组', () => {
    expect(impactMarkerGroup({ event_time: 1, impact_pct: 1 })).toBe('green')
    expect(impactMarkerGroup({ event_time: 1, impact_pct: -1 })).toBe('red')
    expect(impactMarkerGroup({ event_time: 1, impact_pct: 0 })).toBeUndefined()
  })
})

describe('marker-noise — impact 幅度门槛 / 去重 / 上限', () => {
  it('幅度低于门槛的纯价格异动被丢弃', () => {
    const items = [
      { event_time: MIN, impact_pct: 0.2, impact_severity: 'low', event_title: '微小波动' },
      { event_time: 2 * MIN, impact_pct: 5.0, impact_severity: 'high', event_title: '大幅拉升' },
    ]
    const kept = pickImpactMarkers(items)
    expect(kept).toHaveLength(1)
    expect(kept[0].event_title).toBe('大幅拉升')
  })

  it('幅度为 0 但有真实新闻来源 + 高严重度 → 保留 (有消息本身是信息)', () => {
    const items = [
      {
        event_time: MIN,
        impact_pct: 0,
        impact_severity: 'high',
        event_title: '重大公告',
        event_backed: true,
      },
    ]
    expect(pickImpactMarkers(items)).toHaveLength(1)
  })

  it('同桶同标题去重 (worker 重复写入)', () => {
    const items = [
      { event_time: MIN, impact_pct: 3, impact_severity: 'high', event_title: '同一条' },
      { event_time: MIN + 1000, impact_pct: 3, impact_severity: 'high', event_title: '同一条' },
    ]
    const kept = pickImpactMarkers(items, { align: () => 1 })
    expect(kept).toHaveLength(1)
  })

  it('同一根 K 线最多 2 个', () => {
    const items = Array.from({ length: 6 }, (_, i) => ({
      event_time: MIN + i * 1000,
      impact_pct: 2 + i,
      impact_severity: 'medium',
      event_title: `事件${i}`,
    }))
    const kept = pickImpactMarkers(items, { align: () => 1 })
    expect(kept.length).toBeLessThanOrEqual(2)
  })

  it('全局上限按优先级截断 (severity → 幅度), 不是按时间', () => {
    const items = Array.from({ length: 10 }, (_, i) => ({
      event_time: MIN + i * MIN,
      impact_pct: 1 + i,
      impact_severity: i === 0 ? 'critical' : 'low',
      event_title: `事件${i}`,
    }))
    const kept = pickImpactMarkers(items, { align: (ts) => ts, maxTotal: 3 })
    expect(kept).toHaveLength(3)
    // critical 那条 (i=0, 最早但最严重) 必须保留
    expect(kept.some((k) => k.event_title === '事件0')).toBe(true)
  })

  it('时间窗外的条目被过滤', () => {
    const items = [
      { event_time: 1000, impact_pct: 5, impact_severity: 'high', event_title: '窗外' },
      { event_time: 5000, impact_pct: 5, impact_severity: 'high', event_title: '窗内' },
    ]
    const kept = pickImpactMarkers(items, { window: { from: 4000, to: 6000 } })
    expect(kept).toHaveLength(1)
    expect(kept[0].event_title).toBe('窗内')
  })

  it('空数组 / 非数组输入安全返回空', () => {
    expect(pickImpactMarkers([])).toEqual([])
    expect(pickImpactMarkers(null)).toEqual([])
    expect(pickImpactMarkers(undefined)).toEqual([])
  })

  it('impactAbsPct 取绝对值并对缺失值兜底', () => {
    expect(impactAbsPct({ event_time: 1, impact_pct: -3.2 })).toBeCloseTo(3.2)
    expect(impactAbsPct({ event_time: 1, move_pct: 2.5 })).toBeCloseTo(2.5)
    expect(impactAbsPct({ event_time: 1 })).toBe(0)
  })

  it('默认幅度门槛 = 0.3% (worker medium 分档线; 后端 impact_pct 已统一成百分数)', () => {
    expect(IMPACT_MIN_ABS_PCT).toBe(0.3)
  })

  it('impact_abs_pct 优先于 impact_pct (neutral 方向: 幅度有值但符号为 0)', () => {
    // 后端 v319: impact_pct 是**有符号**的 (neutral → 0), impact_abs_pct 才是幅度
    expect(impactAbsPct({ event_time: 1, impact_pct: 0, impact_abs_pct: 0.8 })).toBeCloseTo(0.8)
    expect(impactAbsPct({ event_time: 1, impact_pct: -1.2, impact_abs_pct: 1.2 })).toBeCloseTo(1.2)
  })

  it('neutral 方向的事件: 幅度够 → 保留, 但不着色 (无方向不归组)', () => {
    const items = [
      {
        event_time: MIN,
        impact_pct: 0,
        impact_abs_pct: 0.9,
        impact_severity: 'medium',
        move_direction: 'flat',
        event_title: '方向不明但确实动过',
      },
    ]
    const kept = pickImpactMarkers(items)
    expect(kept).toHaveLength(1)
    expect(impactMarkerGroup(kept[0] as any)).toBeUndefined()
    expect(impactMarkerColor(kept[0] as any)).toBe('#f59e0b')
  })

  it('幅度低于 0.3% 的事件被丢弃', () => {
    const items = [
      { event_time: MIN, impact_pct: 0.2, impact_abs_pct: 0.2, impact_severity: 'low', event_title: '噪声' },
      { event_time: 2 * MIN, impact_pct: 0.35, impact_abs_pct: 0.35, impact_severity: 'medium', event_title: '有效冲击' },
    ]
    const kept = pickImpactMarkers(items)
    expect(kept).toHaveLength(1)
    expect(kept[0].event_title).toBe('有效冲击')
  })
})

describe('marker-noise — 异动圆点门槛 / 冷却 / 上限', () => {
  it('涨跌幅低于门槛被丢弃', () => {
    const items = [
      { ts: MIN, pct_change: 0.05, trigger_type: 'swing' },
      { ts: 2 * MIN, pct_change: 3.0, trigger_type: 'swing' },
    ]
    const kept = pickAbnormalMarkers(items)
    expect(kept).toHaveLength(1)
    expect(kept[0].ts).toBe(2 * MIN)
  })

  it('无涨跌幅但量能异常 (vol_ratio≥2) 保留', () => {
    const items = [{ ts: MIN, pct_change: null, vol_ratio: 3.5, trigger_type: 'volume' }]
    expect(pickAbnormalMarkers(items)).toHaveLength(1)
  })

  it('量能正常且无涨跌幅 → 丢弃', () => {
    const items = [{ ts: MIN, pct_change: null, vol_ratio: 1.2, trigger_type: 'volume' }]
    expect(pickAbnormalMarkers(items)).toHaveLength(0)
  })

  it('同类型在冷却窗口内只留第一条', () => {
    const items = [
      { ts: MIN, pct_change: 2.0, trigger_type: 'volume' },
      { ts: MIN + 60_000, pct_change: 2.5, trigger_type: 'volume' },
      { ts: MIN + ABNORMAL_COOLDOWN_MS + 1_000, pct_change: 3.0, trigger_type: 'volume' },
    ]
    const kept = pickAbnormalMarkers(items)
    expect(kept).toHaveLength(2)
    expect(kept[0].ts).toBe(MIN)
    expect(kept[1].ts).toBe(MIN + ABNORMAL_COOLDOWN_MS + 1_000)
  })

  it('不同类型互不冷却', () => {
    const items = [
      { ts: MIN, pct_change: 2.0, trigger_type: 'volume' },
      { ts: MIN + 60_000, pct_change: 2.5, trigger_type: 'swing' },
    ]
    expect(pickAbnormalMarkers(items)).toHaveLength(2)
  })

  it('冷却以时间最早的那条为基准 (与入参顺序无关)', () => {
    const items = [
      { ts: MIN + 60_000, pct_change: 2.5, trigger_type: 'volume' },
      { ts: MIN, pct_change: 2.0, trigger_type: 'volume' },
    ]
    const kept = pickAbnormalMarkers(items)
    expect(kept).toHaveLength(1)
    expect(kept[0].ts).toBe(MIN)
  })

  it('同一根 K 线最多 3 个', () => {
    const items = Array.from({ length: 8 }, (_, i) => ({
      ts: MIN + i * 60 * 60 * 1000, // 间隔 1 小时 > 冷却 15 分钟
      pct_change: 2.0,
      trigger_type: 'volume',
    }))
    const kept = pickAbnormalMarkers(items, { align: () => 1 })
    expect(kept.length).toBeLessThanOrEqual(3)
  })
})

describe('movers-panel.tsx — 已接入降噪 (源码静态断言)', () => {
  const src = readFileSync(resolve(__dirname, 'movers-panel.tsx'), 'utf-8')

  it('引入 marker-noise 纯函数', () => {
    expect(src).toContain("from '@/components/quant/marker-noise'")
    expect(src).toContain('pickImpactMarkers')
    expect(src).toContain('pickAbnormalMarkers')
  })

  it('impact marker 不再硬编码红色', () => {
    // 旧实现: color: '#ef4444',  _group: 'red' as const
    const idx = src.indexOf('for (const it of keptImpacts)')
    expect(idx).toBeGreaterThan(-1)
    const impactBlock = src.slice(idx, idx + 1200)
    expect(impactBlock).not.toMatch(/color:\s*['"]#ef4444['"]/)
    expect(impactBlock).toContain('impactMarkerColor(it)')
    expect(impactBlock).toContain('impactMarkerGroup(it)')
  })

  it('异动圆点接入过滤 (keptAbnormalTs 判定)', () => {
    expect(src).toContain('keptAbnormalTs.has(Number(a?.ts ?? 0))')
  })
})

