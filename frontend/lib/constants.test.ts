/**
 * @/lib/constants 共享常量测试 (v302+ P1 重构)。
 *
 * 守护:
 *  - NOT_ASSET_TOKENS: 包含原 analysis-parse.ts 的全部 token,Set/frozen 不可变
 *  - ERROR_CODE_NAME: 包含原 reflection-errors-panel.tsx 的全部码
 *  - 跨模块 import 一致性:同样的 token,同样能命中
 */
import { describe, it, expect } from 'vitest'
import { NOT_ASSET_TOKENS, ERROR_CODE_NAME } from './constants'

describe('NOT_ASSET_TOKENS — 非品种黑名单 (v302+ P1 抽取)', () => {
  it('USDT/USDC/BUSD/USD 在内 (稳定币/美元)', () => {
    expect(NOT_ASSET_TOKENS.has('USDT')).toBe(true)
    expect(NOT_ASSET_TOKENS.has('USDC')).toBe(true)
    expect(NOT_ASSET_TOKENS.has('BUSD')).toBe(true)
    expect(NOT_ASSET_TOKENS.has('USD')).toBe(true)
  })

  it('DXY/VIX/TIPS 在内 (美元指数/波动率/通胀保护)', () => {
    expect(NOT_ASSET_TOKENS.has('DXY')).toBe(true)
    expect(NOT_ASSET_TOKENS.has('VIX')).toBe(true)
    expect(NOT_ASSET_TOKENS.has('TIPS')).toBe(true)
  })

  it('技术指标 token 全部在内', () => {
    for (const t of ['CVD', 'OI', 'RSI', 'MACD', 'KDJ', 'BOLL', 'EMA', 'MA', 'ATR']) {
      expect(NOT_ASSET_TOKENS.has(t), `${t} should be in NOT_ASSET_TOKENS`).toBe(true)
    }
  })

  it('经济数据 token 全部在内', () => {
    for (const t of ['ETF', 'CPI', 'PPI', 'PCE', 'NFP', 'GDP', 'PMI', 'ISM', 'ADP']) {
      expect(NOT_ASSET_TOKENS.has(t), `${t} should be in NOT_ASSET_TOKENS`).toBe(true)
    }
  })

  it('央行/监管 token 全部在内', () => {
    for (const t of ['FOMC', 'OPEC', 'FED', 'ECB', 'BOJ', 'PBOC', 'SEC', 'CFTC', 'IMF']) {
      expect(NOT_ASSET_TOKENS.has(t), `${t} should be in NOT_ASSET_TOKENS`).toBe(true)
    }
  })

  it('OTHER/AGGREGATE 不应作为 token 出现 (MarketCategory 而非品种)', () => {
    // v212 fix: 'OTHER' 已从 MarketCategory 类型中删除
    expect(NOT_ASSET_TOKENS.has('OTHER')).toBe(false)
    // 'AGGREGATE' 是 MarketCategory 的合法值,但不是品种 token
    expect(NOT_ASSET_TOKENS.has('AGGREGATE')).toBe(false)
  })

  it('真实品种不应被误杀 (BTC/ETH/BNB 不在内)', () => {
    expect(NOT_ASSET_TOKENS.has('BTC')).toBe(false)
    expect(NOT_ASSET_TOKENS.has('ETH')).toBe(false)
    expect(NOT_ASSET_TOKENS.has('BNB')).toBe(false)
    expect(NOT_ASSET_TOKENS.has('SOL')).toBe(false)
  })

  it('不可变:Object.isFrozen', () => {
    // 双重防护:常量本身是 frozen,Set 引用也是 readonly
    expect(Object.isFrozen(NOT_ASSET_TOKENS)).toBe(true)
  })

  it('与原 analysis-parse.ts 的 _NOT_ASSET 完全一致 (迁移守恒)', () => {
    // 7+9+9+9+12+6 = 52 (分组见文件顶部注释)
    expect(NOT_ASSET_TOKENS.size).toBe(52)
  })
})

describe('ERROR_CODE_NAME — 错误码→中文名映射 (v302+ P1 抽取)', () => {
  it('v212 粗粒度码全部在内', () => {
    expect(ERROR_CODE_NAME.E001).toBe('数据缺失（粗)')
    expect(ERROR_CODE_NAME.E002).toBe('数据过期')
    expect(ERROR_CODE_NAME.E003).toBe('逻辑矛盾')
    expect(ERROR_CODE_NAME.E004).toBe('因果断裂')
    expect(ERROR_CODE_NAME.E005).toBe('规则冲突')
    expect(ERROR_CODE_NAME.E007).toBe('市场反向')
    expect(ERROR_CODE_NAME.E009).toBe('置信度虚高')
    expect(ERROR_CODE_NAME.E010).toBe('抢跑风险')
  })

  it('v225 E001 拆细子码全部在内', () => {
    expect(ERROR_CODE_NAME.E001a).toBe('缺品种事实')
    expect(ERROR_CODE_NAME.E001b).toBe('缺时间锚点')
    expect(ERROR_CODE_NAME.E001c).toBe('缺因子方向')
    expect(ERROR_CODE_NAME.E001d).toBe('缺事件链关联')
    expect(ERROR_CODE_NAME.E001e).toBe('缺跨资产验证')
    expect(ERROR_CODE_NAME.E001f).toBe('缺回测样本')
  })

  it('v189 事件逻辑链层全部在内 (最高权重)', () => {
    expect(ERROR_CODE_NAME.E014).toBe('推理链断裂')
    expect(ERROR_CODE_NAME.E015).toBe('概念错位/反共识缺位')
    expect(ERROR_CODE_NAME.E016).toBe('跨资产方向错')
    expect(ERROR_CODE_NAME.E017).toBe('跨资产无据传导')
    expect(ERROR_CODE_NAME.E018).toBe('历史反例方向反')
  })

  it('未声明的码 (如 E099) 不抛错,只是返回 undefined', () => {
    expect(ERROR_CODE_NAME.E099).toBeUndefined()
  })

  it('不可变:Object.isFrozen', () => {
    expect(Object.isFrozen(ERROR_CODE_NAME)).toBe(true)
  })

  it('迁移守恒: 19 个码 (8 粗 + 6 子 + 5 链层)', () => {
    expect(Object.keys(ERROR_CODE_NAME)).toHaveLength(19)
  })
})
