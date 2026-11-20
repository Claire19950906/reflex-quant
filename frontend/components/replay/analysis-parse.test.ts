/**
 * analysis-parse 业务逻辑测试。
 *
 * 这些函数负责多品种 LLM 推理的拆段 / 品种归一 / 标签解析。
 * 是反思系统"分析页"的纯业务逻辑,适合单测覆盖:
 *  - 中文品种名归一(用明确清单而非通用 [一-龥] 正则,500 条真实数据训练)
 *  - 段名标记切分 + 段名归一(LLM 训练惯性偶尔写旧段名)
 *  - 多品种 [代码]：内容 切分 + "按句提及"兜底
 *  - 数据字段引用 BTC.CVD 不被误判为品种标签
 *  - asset_reasoning 复杂前缀(主品种 BTC(比特币)：)
 */

import { describe, it, expect } from 'vitest'
import {
  parseAnalysisSteps,
  splitStageByAsset,
  normalizeLabel,
  normAsset,
  detectAssets,
  detectMentionedAssets,
  parseAssetReasoning,
  STAGE_HINTS,
  FIXED_STAGE_KEYS,
} from './analysis-parse'

describe('normalizeLabel — 中文品种归一', () => {
  it('中文品种名归一到代码', () => {
    expect(normalizeLabel('黄金')).toBe('XAU')
    expect(normalizeLabel('金价')).toBe('XAU')
    expect(normalizeLabel('原油')).toBe('WTI')
    expect(normalizeLabel('油价')).toBe('WTI')
    expect(normalizeLabel('天然气')).toBe('NATGAS')
  })

  it('长中文名优先匹配(美元指数不应当作"美元")', () => {
    // 实测 500 条数据: [美元] 单独出现时不应匹配品种
    // 但 [美元指数] 应当匹配 DXY
    expect(normalizeLabel('美元指数')).toBe('DXY')
  })

  it('非 ASCII 不在清单 → 标为非品种(避免"因/果/牛来"误判)', () => {
    // 这是 v176 fix 的关键:通用 [\u4e00-\u9fa5]{2,4} 会把"因:"/"果:"当作品种
    expect(normalizeLabel('因')).toBe('')
    expect(normalizeLabel('果')).toBe('')
    expect(normalizeLabel('牛来')).toBe('')
  })

  it('合法英文代码原样大写', () => {
    expect(normalizeLabel('btc')).toBe('BTC')
    expect(normalizeLabel('NVDA')).toBe('NVDA')
    expect(normalizeLabel('xAu')).toBe('XAU')
  })

  it('黑名单 token(USDT/RSI)不当作品种', () => {
    expect(normalizeLabel('USDT')).toBe('')
    expect(normalizeLabel('RSI')).toBe('')
    expect(normalizeLabel('OI')).toBe('')
  })

  it('空值安全降级', () => {
    expect(normalizeLabel('')).toBe('')
    expect(normalizeLabel(null as any)).toBe('')
  })
})

describe('normAsset — 显示归一', () => {
  it('中文归一到代码', () => {
    expect(normAsset('黄金')).toBe('XAU')
  })

  it('代码原样大写', () => {
    expect(normAsset('btc')).toBe('BTC')
  })

  it('空白剪裁', () => {
    expect(normAsset('  XAU  ')).toBe('XAU')
  })
})

describe('parseAnalysisSteps — 段名切分', () => {
  it('按 [段名] 标记切分 reasoning_path', () => {
    const path = `[总框架] 因果链图谱主导
[因素定位] XAU 是核心驱动
[驱动力] 黄金上涨
[水位博弈] 周线突破
[总结] 看涨`
    const steps = parseAnalysisSteps(path)
    expect(steps).toHaveLength(5)
    expect(steps[0].key).toBe('总框架')
    expect(steps[1].key).toBe('因素定位')
    expect(steps[2].key).toBe('驱动力')
    expect(steps[3].key).toBe('水位博弈')
    expect(steps[4].key).toBe('总结')
  })

  it('旧段名归一到新段名(v176 模型训练惯性兼容)', () => {
    expect(parseAnalysisSteps('[资产性质] XX')[0]?.key).toBe('总框架')
    expect(parseAnalysisSteps('[主体识别] XX')[0]?.key).toBe('总框架')
    expect(parseAnalysisSteps('[因素排行] XX')[0]?.key).toBe('因素定位')
    expect(parseAnalysisSteps('[盘面结构分析] XX')[0]?.key).toBe('盘面结构')
  })

  it('空 / null 输入返回空数组', () => {
    expect(parseAnalysisSteps('')).toEqual([])
    expect(parseAnalysisSteps(null as any)).toEqual([])
    expect(parseAnalysisSteps('   ')).toEqual([])
  })

  it('每个 step 都拿到 hint', () => {
    const steps = parseAnalysisSteps('[总结] X')
    expect(steps[0].hint).toBe(STAGE_HINTS['总结'])
  })

  it('FIXED_STAGE_KEYS 8 段齐全', () => {
    expect(FIXED_STAGE_KEYS).toContain('总框架')
    expect(FIXED_STAGE_KEYS).toContain('因素定位')
    expect(FIXED_STAGE_KEYS).toContain('驱动力')
    expect(FIXED_STAGE_KEYS).toContain('水位博弈')
    expect(FIXED_STAGE_KEYS).toContain('跨资产资金')
    expect(FIXED_STAGE_KEYS).toContain('反共识结论')
    expect(FIXED_STAGE_KEYS).toContain('逻辑自检')
    expect(FIXED_STAGE_KEYS).toContain('总结')
  })
})

describe('splitStageByAsset — 多品种段落切分', () => {
  it('多品种 [代码]：内容 正确切分', () => {
    const content = `[XAU]：黄金上涨；[BTC]：比特币跟随；[ETH]：以太坊震荡`
    const r = splitStageByAsset(content)
    expect(r.byAsset.get('XAU')?.[0]).toContain('黄金上涨')
    expect(r.byAsset.get('BTC')?.[0]).toContain('比特币跟随')
    expect(r.byAsset.get('ETH')?.[0]).toContain('以太坊震荡')
  })

  it('无标签时按"句中提及品种数"兜底', () => {
    const r = splitStageByAsset('黄金价格因美联储放鹰而上涨。')
    expect(r.byAsset.get('XAU')?.[0]).toContain('黄金')
  })

  it('数据字段引用 BTC.CVD / XAU费率 不当作品种标签', () => {
    // 真实 bugfix:旧 regex 会把这些当作"在讲 XAU/BTC"
    // 期望:split 后内容留给 unlabeled,不进 byAsset
    const r = splitStageByAsset('BTC.CVD 显示空头主导,XAU费率走低')
    expect(r.byAsset.get('BTC')).toBeUndefined()
    expect(r.byAsset.get('XAU')).toBeUndefined()
    expect(r.unlabeled.length).toBeGreaterThan(0)
  })

  it('0 或 ≥2 品种提及 → 内容留 unlabeled(防混合)', () => {
    // 安全规则:宁可少归属也不猜错
    const r = splitStageByAsset('黄金上涨,比特币也涨了。')
    expect(r.byAsset.size).toBe(0)
    expect(r.unlabeled).toBeTruthy()
  })

  it('空输入返回空 Map + 空 unlabeled', () => {
    const r = splitStageByAsset('')
    expect(r.byAsset.size).toBe(0)
    expect(r.unlabeled).toBe('')
  })
})

describe('detectAssets / detectMentionedAssets', () => {
  it('detectAssets 按首次出现顺序 dedup', () => {
    // _LABEL_RE 要求 `[CODE]` 后必须接 [:：] 冒号才识别为品种标签。
    // 模型实际输出格式是 `[驱动力][XAU]：...`,这里按真实格式测。
    const path = '[驱动力][XAU]：黄金 ETF 流入 [水位博弈][XAU]：箱体上沿 [总结][BTC]：趋势看多'
    expect(detectAssets(path)).toEqual(['XAU', 'BTC'])
  })

  it('detectMentionedAssets 含中文别名与商品说法', () => {
    const txt = '黄金价格上涨,金价突破前高,中央银行购金增加'
    const list = detectMentionedAssets(txt)
    expect(list).toContain('XAU')
    expect(list[0]).toBe('XAU') // 出现次数最多排第一
  })
})

describe('parseAssetReasoning — 复杂前缀归一', () => {
  it('主品种 / 副品种 前缀解析', () => {
    const txt = `主品种 BTC：因为美联储放鹰
副品种 ETH：跟随下跌`
    const lines = parseAssetReasoning(txt)
    expect(lines).toHaveLength(2)
    expect(lines[0].asset).toBe('BTC')
    expect(lines[0].role).toBe('main')
    expect(lines[1].asset).toBe('ETH')
    expect(lines[1].role).toBe('sibling')
  })

  it('括号里的中文名(BTC(比特币))归一到 BTC', () => {
    const txt = '主品种 BTC(比特币)：内容'
    const lines = parseAssetReasoning(txt)
    expect(lines[0].asset).toBe('BTC')
  })

  it('代币 / 市值 后缀词剥除', () => {
    const txt = '主品种 LAPTOP(Laptop 代币)：内容'
    const lines = parseAssetReasoning(txt)
    expect(lines[0].asset).toBe('LAPTOP')
  })

  it('清单外但有"主品种"标记的归一失败时保留原文', () => {
    // 归一失败(不在中文清单,也不像代码,如"牛来")时
    // 不应丢品种信息,而是保留原文(避免破坏逐品种完整性)
    const txt = '主品种 牛来(市值)：内容'
    const lines = parseAssetReasoning(txt)
    expect(lines[0].asset).toBe('牛来')
    expect(lines[0].role).toBe('main')
  })

  it('空 / null 安全', () => {
    expect(parseAssetReasoning('')).toEqual([])
    expect(parseAssetReasoning(null as any)).toEqual([])
    expect(parseAssetReasoning(undefined as any)).toEqual([])
  })
})

