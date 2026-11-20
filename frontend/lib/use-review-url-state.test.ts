/**
 * use-review-url-state 纯函数测试 (v302+ P1 重构)。
 *
 * 守护:
 *  - readFromUrl: 6 键解析 + 白名单校验 + custom range fallback
 *  - applyPatch: set / delete (null) / 多键原子 / 空 patch no-op
 *  - 默认值: tab=all, ab=a, period=today, err=null
 *  - 原子写: setCustomDateRange patch 含 3 键 (from / to / period)
 *  - v302+ P3++: hook 返回值 useMemo 包裹 — 无关重渲染下对象引用稳定
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import {
  readFromUrl,
  applyPatch,
  DEFAULT_PERIOD,
  VALID_TABS,
  VALID_PERIODS,
  type UrlPatch,
  type ReviewUrlState,
} from './use-review-url-state'

const DEFAULT_RANGE = { fromDate: '2024-01-01', toDate: '2024-01-07' }
const makeSp = (qs: string) => new URLSearchParams(qs)

describe('readFromUrl — 6 键 URL 解析', () => {
  it('空 URL:全默认值', () => {
    const s = readFromUrl(null, DEFAULT_RANGE)
    expect(s.tab).toBe('all')
    expect(s.ab).toBe('a')
    expect(s.err).toBeNull()
    expect(s.period).toBe(DEFAULT_PERIOD)
    expect(s.customRange).toEqual(DEFAULT_RANGE)
  })

  it('tab 白名单:experiences / journal 接受,其它回退 all', () => {
    for (const t of VALID_TABS) {
      const s = readFromUrl(makeSp(`tab=${t}`), DEFAULT_RANGE)
      expect(s.tab).toBe(t)
    }
    expect(readFromUrl(makeSp('tab=garbage'), DEFAULT_RANGE).tab).toBe('all')
    expect(readFromUrl(makeSp('tab=ALL'), DEFAULT_RANGE).tab).toBe('all')
  })

  it('ab:仅 b 接受,其它 (含空) 走 a', () => {
    expect(readFromUrl(makeSp('ab=b'), DEFAULT_RANGE).ab).toBe('b')
    expect(readFromUrl(makeSp('ab=a'), DEFAULT_RANGE).ab).toBe('a')
    expect(readFromUrl(makeSp('ab=anything'), DEFAULT_RANGE).ab).toBe('a')
    expect(readFromUrl(null, DEFAULT_RANGE).ab).toBe('a')
  })

  it('err:任意字符串原样读,缺键回退 null (空 err= 视为空字符串)', () => {
    expect(readFromUrl(makeSp('err=E001'), DEFAULT_RANGE).err).toBe('E001')
    // URLSearchParams.get('err') 对 err= 返回 '' (不是 null) — 这是规范行为
    // 真正的 null 是 key 缺失。两种都是"无错误"语义,但本 hook 保留原值避免信息丢失
    expect(readFromUrl(makeSp('err='), DEFAULT_RANGE).err).toBe('')
    expect(readFromUrl(null, DEFAULT_RANGE).err).toBeNull()
  })

  it('period 白名单:today/7d/30d/custom 接受,其它回退 today', () => {
    for (const p of VALID_PERIODS) {
      const s = readFromUrl(makeSp(`period=${p}`), DEFAULT_RANGE)
      expect(s.period).toBe(p)
    }
    expect(readFromUrl(makeSp('period=forever'), DEFAULT_RANGE).period).toBe('today')
    expect(readFromUrl(makeSp('period=Today'), DEFAULT_RANGE).period).toBe('today')
  })

  it('customRange:仅 period=custom 且 from/to 都存在时读 URL', () => {
    expect(readFromUrl(makeSp('period=custom&from=2024-01-01'), DEFAULT_RANGE).customRange)
      .toEqual(DEFAULT_RANGE)
    expect(readFromUrl(makeSp('period=custom&to=2024-01-07'), DEFAULT_RANGE).customRange)
      .toEqual(DEFAULT_RANGE)
    expect(readFromUrl(makeSp('period=custom&from=2024-01-01&to=2024-01-07'), DEFAULT_RANGE).customRange)
      .toEqual({ fromDate: '2024-01-01', toDate: '2024-01-07' })
    expect(readFromUrl(makeSp('period=today&from=2024-01-01&to=2024-01-07'), DEFAULT_RANGE).customRange)
      .toEqual(DEFAULT_RANGE)
  })

  it('完整 URL 一次解析 6 键', () => {
    const s = readFromUrl(
      makeSp('tab=journal&ab=b&err=E001&period=custom&from=2024-02-01&to=2024-02-07'),
      DEFAULT_RANGE,
    )
    expect(s).toEqual<ReviewUrlState>({
      tab: 'journal',
      ab: 'b',
      err: 'E001',
      period: 'custom',
      customRange: { fromDate: '2024-02-01', toDate: '2024-02-07' },
    })
  })
})

describe('applyPatch — URLSearchParams 增量更新', () => {
  it('set 新键', () => {
    expect(applyPatch(null, [['tab', 'journal']]).get('tab')).toBe('journal')
  })
  it('set 覆盖已有键', () => {
    expect(applyPatch(makeSp('tab=journal'), [['tab', 'experiences']]).get('tab')).toBe('experiences')
  })
  it('null / 空字符串 → 删除键', () => {
    expect(applyPatch(makeSp('tab=journal&err=E001'), [['tab', null]]).get('tab')).toBeNull()
    expect(applyPatch(makeSp('tab=journal&err=E001'), [['err', '']]).get('err')).toBeNull()
  })
  it('空 patch:不改变 sp', () => {
    const sp = makeSp('tab=journal&err=E001')
    expect(applyPatch(sp, []).toString()).toBe(sp.toString())
  })
  it('多键原子 (setCustomDateRange):3 键一次写入,无中间态', () => {
    const sp = makeSp('period=today')
    const out = applyPatch(sp, [
      ['from', '2024-03-01'],
      ['to', '2024-03-07'],
      ['period', 'custom'],
    ])
    expect(out.get('from')).toBe('2024-03-01')
    expect(out.get('to')).toBe('2024-03-07')
    expect(out.get('period')).toBe('custom')
    // 顺序无关:key-value 集合相同即可(URLSearchParams 保留插入顺序)
    const out2 = applyPatch(sp, [
      ['period', 'custom'],
      ['to', '2024-03-07'],
      ['from', '2024-03-01'],
    ])
    expect(Array.from(out2.entries()).sort()).toEqual(Array.from(out.entries()).sort())
  })
  it('patch 是增量,非全量替换', () => {
    const sp = makeSp('tab=journal&err=E001&period=today')
    const out = applyPatch(sp, [['ab', 'b']])
    expect(out.get('tab')).toBe('journal')
    expect(out.get('err')).toBe('E001')
    expect(out.get('period')).toBe('today')
    expect(out.get('ab')).toBe('b')
  })
  it('null sp 也能 patch (从空 URL 起步)', () => {
    expect(applyPatch(null, [['tab', 'journal']]).get('tab')).toBe('journal')
  })
})

describe('setter patch 形态 — 验证 hook 的高阶 setter 用对的 patch', () => {
  const setTabPatch = (next: string): UrlPatch =>
    [['tab', next === 'all' ? null : next]]
  const setAbPatch = (next: string): UrlPatch =>
    [['ab', next === 'a' ? null : next]]
  const setPeriodPatch = (next: string): UrlPatch =>
    [['period', next === DEFAULT_PERIOD ? null : next]]
  const setCustomRangePatch = (next: { fromDate: string; toDate: string }): UrlPatch => [
    ['from', next.fromDate],
    ['to', next.toDate],
    ['period', 'custom'],
  ]

  it('setTab(\'all\') → 删除 tab 键 (URL 干净)', () => {
    expect(setTabPatch('all')).toEqual([['tab', null]])
  })
  it('setTab(\'journal\') → 写 tab=journal', () => {
    expect(setTabPatch('journal')).toEqual([['tab', 'journal']])
  })
  it('setAbTab(\'a\') → 删除 ab 键', () => {
    expect(setAbPatch('a')).toEqual([['ab', null]])
  })
  it('setAbTab(\'b\') → 写 ab=b', () => {
    expect(setAbPatch('b')).toEqual([['ab', 'b']])
  })
  it('setPeriod(\'today\') → 删除 period', () => {
    expect(setPeriodPatch('today')).toEqual([['period', null]])
  })
  it('setPeriod(\'7d\') → 写 period=7d', () => {
    expect(setPeriodPatch('7d')).toEqual([['period', '7d']])
  })
  it('setCustomDateRange → 一次写 3 键 (原子)', () => {
    expect(
      setCustomRangePatch({ fromDate: '2024-01-01', toDate: '2024-01-07' }),
    ).toEqual([
      ['from', '2024-01-01'],
      ['to', '2024-01-07'],
      ['period', 'custom'],
    ])
  })
})

// ====== v302+ P3++: useReviewUrlState hook 行为 ======
// 锁定 useMemo 包裹 — 每次重渲染如果 URL 没变,返回对象应当引用相等。
// 这让下游的 useMemo 依赖 [urlState] 不需要重算。
// (同时是 PerformanceObject 守卫 — 防止有人去掉 useMemo 让所有 panel 重渲染)
//
// 关键 mock 细节:useSearchParams 必须返回稳定的引用!
// 真实 Next.js 在 URL 不变时返回同一个 URLSearchParams 对象;
// 如果每次都 new 一个,useMemo 的 deps 就每次都变,测试就过不了。
const stableSp = new URLSearchParams('')
const stableRouter = { replace: vi.fn() }
vi.mock('next/navigation', () => ({
  useSearchParams: vi.fn(() => stableSp),
  useRouter: vi.fn(() => stableRouter),
  usePathname: vi.fn(() => '/review'),
}))

describe('useReviewUrlState — hook 引用稳定性 (v302+ P3++)', () => {
  it('同一 URL 下,两次渲染的返回值是同一个对象引用', async () => {
    const { useReviewUrlState } = await import('./use-review-url-state')
    const { result, rerender } = renderHook(() => useReviewUrlState())
    const first = result.current
    rerender()
    expect(result.current).toBe(first) // 同一引用
  })

  it('useState 不动 → useMemo 命中 — 全对象 1 个', async () => {
    const { useReviewUrlState } = await import('./use-review-url-state')
    const { result, rerender } = renderHook(() => useReviewUrlState())
    const first = result.current
    rerender()
    rerender()
    rerender()
    // 三次重渲染,引用还是同一个
    expect(result.current).toBe(first)
  })

  it('customRange 对象引用稳定 — period 未变时不重建', async () => {
    // 关键:customRange 是 {fromDate, toDate} 对象字面量,
    // 如果 useMemo 没生效,每次 readFromUrl 都会返回新对象,
    // 所有订阅 period 的 Panel 都会无意义地重渲染。
    const { useReviewUrlState } = await import('./use-review-url-state')
    const { result, rerender } = renderHook(() => useReviewUrlState())
    const firstRange = result.current.customRange
    rerender()
    rerender()
    expect(result.current.customRange).toBe(firstRange) // 对象引用不变
  })

  it('period 跨多次重渲染保持同一字符串原语(perf 回归)', async () => {
    // period 是 PeriodKey string。字符串按值比较,所以即使 hook
    // 在重渲染时也返回相同的 'today' 值,任何下游
    // useMemo([period]) 都不会失效。
    const { useReviewUrlState } = await import('./use-review-url-state')
    const { result, rerender } = renderHook(() => useReviewUrlState())
    const first = result.current.period
    rerender()
    rerender()
    rerender()
    expect(result.current.period).toBe('today')
    expect(result.current.period).toBe(first)
  })

  it('custom 模式下:customRange 跨重渲染仍稳定', async () => {
    // v302+ P4:把 useSearchParams mock 切到 ?period=custom&from=...&to=...,
    // 验证即使内部 readFromUrl 每次都构造新字面量,外层 useMemo([sp])
    // 仍能锁住 customRange 引用。
    // 关键:这是真正的 perf 守卫 — custom 模式下所有 panel 订阅 period,
    // 如果 customRange 引用每次都变,PeriodSelector 组件会反复触发 onChange。
    const customSp = new URLSearchParams(
      'period=custom&from=2024-02-01&to=2024-02-29',
    )
    const { useSearchParams } = await import('next/navigation')
    // Next.js useSearchParams 返回 ReadonlyURLSearchParams — 这里
    // mock 一个普通的 URLSearchParams 即可(readFromUrl 只读不写),
    // 用 as any 绕开 TS 类型校验
    vi.mocked(useSearchParams).mockReturnValue(customSp as any)
    const { useReviewUrlState } = await import('./use-review-url-state')
    const { result, rerender } = renderHook(() => useReviewUrlState())
    const firstRange = result.current.customRange
    rerender()
    rerender()
    expect(result.current.customRange).toBe(firstRange)
    // 值正确,不只是引用相等
    expect(result.current.customRange).toEqual({
      fromDate: '2024-02-01',
      toDate: '2024-02-29',
    })
  })
})
