/**
 * 全局共享常量 (v302+ P1 重构)。
 *
 * 设计原则:
 *  1. 单一来源 (single source of truth) — 同一个 token/错误码只在一处定义
 *  2. 不可变 (frozen) — 运行时禁止修改
 *  3. 类型安全 — Set/Record 都是静态导入,IDE 跳转可达
 *  4. 跨模块共享 — 任何 frontend 模块都能从 '@/lib/constants' 拉
 *
 * 不应放的东西:
 *  - 与 UI 主题相关的 (颜色/字号/间距) — 走 Tailwind
 *  - 与 schema 相关的 (字段定义/校验规则) — 走 zod schema
 *  - 与某个具体页面的 — 留在页面文件
 */

/**
 * 非品种黑名单 (NOT_ASSET_TOKENS): 出现在标签位置但其实是指标/机构/口径词的 token。
 * 来源: 原 components/replay/analysis-parse.ts (v212 修复后)。
 * 用途: LLM 分析结果归一化时,排除这些 token,避免被错认为交易品种。
 *
 * 只放"几乎不可能是交易品种"的高频词,避免误杀真实品种 (如 CORE、API、EIA、NA)。
 */
export const NOT_ASSET_TOKENS: ReadonlySet<string> = Object.freeze(
  new Set([
    // 稳定币 / 美元 / 美元指数
    'USDT', 'USDC', 'BUSD', 'USD', 'DXY', 'VIX', 'TIPS',
    // 技术指标
    'CVD', 'OI', 'RSI', 'MACD', 'KDJ', 'BOLL', 'EMA', 'MA', 'ATR',
    // 资产类别 / 经济数据
    'ETF', 'CPI', 'PPI', 'PCE', 'NFP', 'GDP', 'PMI', 'ISM', 'ADP',
    // 央行 / 监管 / 机构
    'FOMC', 'OPEC', 'FED', 'ECB', 'BOJ', 'PBOC', 'SEC', 'CFTC', 'IMF',
    // 政策 / 缩略
    'QE', 'QT', 'YCC', 'IPO', 'CEO', 'CFO', 'AI', 'L1', 'L2', 'TVL', 'ATH', 'ATL',
    // 数据源 / 占位符
    'API', 'EIA', 'N/A', 'NA', 'NONE', 'CORE',
    // 'OTHER' 已从 MarketCategory 类型中删除 — 绝不可能作为真实品种出现在 AI 输出中
    // 'AGGREGATE' 是 MarketCategory 合法值(已通过筛选但未命中品种),但不是品种 token
  ]),
)

/**
 * 错误码→中文名映射 (ERROR_CODE_NAME): 反思面板"程序化校验"错误码到人类可读名的映射。
 * 来源: 原 components/replay/reflection-errors-panel.tsx (v225 拆细子码、v189 事件链层)。
 *
 * 范围: E001 / E001a-f / E002-E005 / E007 / E009 / E010 / E014-E018
 *
 * 用法: 错误码 → 人类可读名;不在表中则降级显示原码。
 */
export const ERROR_CODE_NAME: Readonly<Record<string, string>> = Object.freeze({
  // v212 粗粒度
  E001: '数据缺失（粗)', E002: '数据过期', E003: '逻辑矛盾', E004: '因果断裂',
  E005: '规则冲突', E007: '市场反向', E009: '置信度虚高', E010: '抢跑风险',
  // v225 E001 拆细到子码
  E001a: '缺品种事实', E001b: '缺时间锚点', E001c: '缺因子方向',
  E001d: '缺事件链关联', E001e: '缺跨资产验证', E001f: '缺回测样本',
  // v189 事件逻辑链层 (最高权重)
  E014: '推理链断裂', E015: '概念错位/反共识缺位', E016: '跨资产方向错',
  E017: '跨资产无据传导', E018: '历史反例方向反',
})
