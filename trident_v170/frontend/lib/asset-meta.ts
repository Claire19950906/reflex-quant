/**
 * lib/asset-meta.ts — 资产代号 → 中文名 / 分类的单一来源 (v302+ P0)
 *
 * 背景:
 *   旧版本 (≤v301) 在 4 个组件里各定义一份 ASSET_META, 每次新增品种都要 4 处同步改,
 *   极易漏改(归因页 K 线 6 个、资产热力图 5 个、资产涨跌榜 11 个、事件热力图 5 个,
 *   数量不一致 — 已被 lint 标记为「设计错误」)。
 *
 * 设计:
 *   - 不可变 frozen Record,运行时禁止修改
 *   - 不放「兜底分类」(US_STOCK / CN_STOCK / INDIVIDUAL_STOCK):
 *     这三个值是聚合分类,不是品种代号本身,应走业务层的合并逻辑,不进常量表
 *   - 不放技术指标 / 货币符号(USD/USDT/...)— 那是 NOT_ASSET_TOKENS 的活
 *
 * 用法:
 *   import { ASSET_META } from '@/lib/asset-meta'
 *   <span>{ASSET_META[asset] ?? asset}</span>
 */

export const ASSET_META: Readonly<Record<string, string>> = Object.freeze({
  // 加密 11 个
  BTC: '比特币',
  ETH: '以太坊',
  SOL: 'Solana',
  XRP: '瑞波币',
  DOGE: '狗狗币',
  BNB: '币安币',
  AVAX: '雪崩',
  LINK: 'Chainlink',
  DOT: 'Polkadot',
  ADA: '卡尔达诺',
  LTC: '莱特币',

  // 贵金属 + 原油 + 大宗商品
  XAU: '黄金',
  XAG: '白银',
  WTI: 'WTI 原油',
  BRENT: 'Brent 原油',
  NG: '天然气',
  HG: '铜',
  SILVER: '白银',

  // 美股 + 加密相关个股
  NVDA: '英伟达',
  TSLA: '特斯拉',
  AAPL: '苹果',
  MSTR: 'MicroStrategy',
  MSFT: '微软',
  AMZN: '亚马逊',
  GOOGL: '谷歌',
  COIN: 'Coinbase',
  PLTR: 'Palantir',
  META: 'Meta',
  SMCI: '超微',
  MARA: 'Marathon',
  RIOT: 'Riot',
  AMD: 'AMD',
  NFLX: '奈飞',
  CRM: 'Salesforce',
  JPM: '摩根大通',
  DIS: '迪士尼',
  ORCL: '甲骨文',
  BA: '波音',
  GS: '高盛',
  INTC: '英特尔',
  V: 'Visa',

  // 海力士双线
  SKHYNIX: '韩国海力士',
  US_HYNIX: '美国海力士',
  MU: '美国海力士',

  // ── 聚合分类显示名 (Phase B 回灌兼容, 不在 trident 原表内) ──────────────
  // trident v302+ P0 的设计文档判定这三个「是聚合分类, 不是品种代号本身,
  // 应走业务层的合并逻辑」, 因此没放进上面的常量表。但金融版 ≤v301 的
  // asset-heatmap / asset-leaderboard / event-heatmap 三个组件各自的本地
  // ASSET_META 里是有这三个显示名的 —— 若 /api/market/movers 真返回聚合码,
  // 直接删掉会让界面显示裸 token(US_STOCK 而不是「美股」)。
  // 这里只补回显示名, 不改动品种代号本身的语义。
  US_STOCK: '美股',
  CN_STOCK: 'A股',
  INDIVIDUAL_STOCK: '个股',
})

/**
 * 安全取值: 找不到时回退到传入的资产代号(不要 fallback 成 undefined 让 UI 显示空白)。
 * 业务层常常传 SKHYNIX / US_HYNIX 这种新增的代号,只要还没在表里,也能稳定显示。
 */
export function assetDisplayName(asset: string): string {
  return ASSET_META[asset] ?? asset
}
