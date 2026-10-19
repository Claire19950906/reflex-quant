// 多品种六段式的纯解析层（无 React / 无 JSX，便于离线测试与复用）。
//
// 背景：AI 输出的 reasoning_path 在【多品种新闻】里把各品种写在同段内
// （如 "[驱动力]BNB：…；BTC：…"），旧版按段渲染会把多品种混在一张卡里。
// 本模块负责把它拆成"每个品种自己的内容 + 跨品种共享内容"。
//
// 拆分依据（400 条真实决策实测的品种标注覆盖率）：
//   · 驱动力 77.9% / 水位博弈 83.7% / 反共识结论 83.5% → 按品种拆
//   · 跨资产资金 10.0% / 逻辑自检 16.0% → 本质是跨品种视角，不拆，作为共享段展示

// v302+ P1 重构: 非品种黑名单已抽取到 @/lib/constants (NOT_ASSET_TOKENS)
import { NOT_ASSET_TOKENS } from '@/lib/constants'

/** 六段全部按品种展示（原「跨资产资金 / 逻辑自检」曾作为共享段单独展示，
 *  用户要求改为各品种各自分析：这两段也走同一套按品种归属逻辑）。
 *
 *  「总结」放在最后：用户要求"有关系就要有分析 + 总结"，总结按品种分行，
 *  属于该品种的收口结论（列在七段推演之后）。 */
export const FIXED_STAGE_KEYS = [
  '总框架', '因素定位', '驱动力', '水位博弈', '跨资产资金', '反共识结论', '逻辑自检', '总结',
] as const

/** 保留空数组以兼容历史引用（不再有"共享段"概念）。 */
export const SHARED_STAGE_KEYS: readonly string[] = []

export const STAGE_HINTS: Record<string, string> = {
  '总框架': '因果图谱核心驱动 · 品种属性 · 增强/削弱新闻传导',
  '因素定位': '定位新闻命中哪条传导子链 · 1-2 条 · 作用变量与价格弹性',
  '驱动力': '资金面真正含义 · 什么在推动价格',
  '水位博弈': '周期位置 · 谁在获利/恐慌（含盘面结构若开启）',
  '盘面结构': '周期位置 · 谁在获利/恐慌（盘面结构子段,v176 旧段名归一兼容）',
  '反共识结论': '独立判断 · 数值见 sentiment_score',
  '跨资产资金': '原油/美债/黄金/Crypto 资金联动（跨品种共享）',
  '逻辑自检': '自洽性复核 · 矛盾则降级 HOLD（跨品种共享）',
  '总结': '该品种最终怎么看 · 依据 · 方向（必须与本品种下单方向一致）',
}

export type AnalysisStep = { key: string; label: string; hint: string; content: string }
export type StageItem = { label: string; text: string }
export type AssetTab = { asset: string; isMain: boolean }

/** 内置显示名（与后端 ASSET_DISPLAY 的常用子集一致） */
export const ASSET_NAMES: Record<string, string> = {
  XAU: '黄金', WTI: '原油', BTC: '比特币', ETH: '以太坊', SOL: 'Solana',
  NVDA: '英伟达', TSLA: '特斯拉', AAPL: '苹果', MSTR: '微策略',
  MSFT: '微软', AMZN: '亚马逊', GOOGL: '谷歌', COIN: 'Coinbase',
  PLTR: 'Palantir', META: 'Meta', SMCI: '美超微', MARA: 'Marathon', RIOT: 'Riot',
}

/** 中文品种别名 → 代码（AI 常混用中文名；仅作显示归一，不参与识别） */
/** 从正则匹配结果取品种 token（兼容带括号 [XAU] 与裸写 XAU 两种捕获组） */
function labelTokenOf(m: RegExpExecArray): string {
  return m[1] ?? m[2] ?? ''
}

/**
 * 中文品种名识别清单 —— 必须用**明确清单**，不能用通用中文正则。
 * 原因（500 条真实数据实测）：通用 [\u4e00-\u9fa5]{2,4} 命中的中文标签里，
 * 329 次是正文"因："、329 次"果："（纯噪音），另有"牛来"这类正文误判；
 * 而真实的中文品种名只有黄金/原油/白银等固定几个。两者不可兼得，故选清单。
 */
const CN_ASSET: Record<string, string> = {
  '黄金': 'XAU', '金价': 'XAU', '原油': 'WTI', '油价': 'WTI', '白银': 'XAG', '铜': 'COPPER',
  '比特币': 'BTC', '以太坊': 'ETH', '英伟达': 'NVDA', '特斯拉': 'TSLA',
  '苹果': 'AAPL', '谷歌': 'GOOGL', '微软': 'MSFT', '亚马逊': 'AMZN',
  '标普': 'SPX', '纳指': 'NDX', '天然气': 'NATGAS', '美元指数': 'DXY',
}

// 中文名分支：逐个中文品种名 + 必须紧跟冒号（1-4 字都覆盖，如"铜："、"天然气："）
const _CN_ALT = Object.keys(CN_ASSET)
  .sort((a, b) => b.length - a.length) // 长名优先，避免"美元指数"被"美元"截断
  .map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  .join('|')

// 段名标记宽松匹配：兼容 [驱动力] / 驱动力： / 1. 驱动力 —— 等写法
const _MARKER_RE = /(总框架|资产性质|主体识别|因素定位|因素排行|盘面结构分析|盘面结构|驱动力|水位博弈|跨资产资金|反共识结论|逻辑自检|总结)(?=[\s]*[:：—\-]|[\s]*[\]】]|[\r\n]|\s*$)/g

// v176.2 段名归一：后端已删除 [资产性质]/[因素排行]/[主体识别]，改用 [总框架]/[因素定位]。
// LLM 训练惯性可能仍输出旧段名 → 归一到新段名（与后端 ai_worker.py 的改写逻辑保持一致）。
const _STAGE_ALIAS: Record<string, string> = {
  '资产性质': '总框架',
  '主体识别': '总框架',
  '因素排行': '因素定位',
  '盘面结构分析': '盘面结构',
}

// 品种标签识别 —— 「结构化提取 + 非品种黑名单」，不用封闭的品种白名单。
// 原因：真实数据里出现过的品种远超内置清单（实测除 BTC/XAU/WTI/NVDA 等，
//       还有 BNB/ZEC/XAG/JPY/EUR/USD/SOFTBANK…），任何写死的清单都会漏。
// 结构条件：必须出现在【段首 / 分号后 / 换行后 / 段名标记后】，且为下面两种写法之一：
//       ① 代码：内容         ② [代码]：内容   ← 后者是 prompt 现在的硬约束格式
//   正文里的 "BTC 走势" 不含冒号，不会被误判；段名 [驱动力] 后面无冒号，也不会被当成品种。
// 注意 ] 】 也必须算边界：模型常写 "[驱动力][XAU]：…"，只认 ^/；/\n 会漏每段首个标签。
const _LABEL_RE = new RegExp(
  '(?:^|[；;]|\\n|[\\]】])[ \\t]*'
  + '(?:\\[(' + _CN_ALT + '|[A-Za-z][A-Za-z0-9/]{1,11})\\]|(' + _CN_ALT + '|[A-Za-z][A-Za-z0-9/]{1,11}))'
  + '\\s*[:：][ \\t]*',
  'g',
)

// 允许的"代码形状"：2-6 位大写字母，或 XXX/YYY 货币对
const _CODE_SHAPE = /^(?:[A-Z]{2,6}|[A-Z]{2,6}\/[A-Z]{2,6})$/

// v302+ P1 重构: 非品种黑名单已抽取到 @/lib/constants (NOT_ASSET_TOKENS)
// 此处保留 _NOT_ASSET 别名以兼容旧 import,内部走共享常量。
const _NOT_ASSET = NOT_ASSET_TOKENS

/** 把原始标签 token 归一为品种代码；判定为非品种则返回 '' */
export function normalizeLabel(raw: string): string {
  const t = (raw || '').trim()
  if (!t) return ''
  // 中文品种名 → 代码（清单内的必定是品种）
  const cn = CN_ASSET[t]
  if (cn) return cn
  // 非 ASCII 且不在清单内：不认（实测就是"因/果/牛来"这类噪音）
  if (!/^[\x00-\x7F]+$/.test(t)) return ''
  const up = t.toUpperCase()
  if (_NOT_ASSET.has(up)) return ''
  if (!_CODE_SHAPE.test(up)) return ''
  return up
}

export function normAsset(raw: string): string {
  const t = (raw || '').trim()
  return CN_ASSET[t] ?? t.toUpperCase()
}

export function assetLabelOf(asset: string): string {
  const a = normAsset(asset)
  return ASSET_NAMES[a] ?? a
}

/** 按 [段名] 标记切分 reasoning_path（兼容旧版 [盘面结构分析]） */
export function parseAnalysisSteps(path: string): AnalysisStep[] {
  if (!path || !path.trim()) return []
  const matches: { index: number; key: string; keyLen: number }[] = []
  let m: RegExpExecArray | null
  _MARKER_RE.lastIndex = 0
  while ((m = _MARKER_RE.exec(path)) !== null) {
    const key = _STAGE_ALIAS[m[1]] ?? m[1]
    matches.push({ index: m.index, key, keyLen: m[1].length })
  }
  if (!matches.length) return []
  const stages: AnalysisStep[] = []
  for (let i = 0; i < matches.length; i++) {
    let segStart = matches[i].index + matches[i].keyLen
    while (segStart < path.length && /[\s:：—\-]|[\]]/.test(path[segStart])) segStart++
    const segEnd = i + 1 < matches.length ? matches[i + 1].index : path.length
    let content = path.slice(segStart, segEnd).trim()
    content = content.replace(/[→\s[\]]+$/, '').replace(/\d+[.、．]\s*$/, '')
    const hint = STAGE_HINTS[matches[i].key]
    if (!hint) continue
    if (content) stages.push({ key: matches[i].key, label: matches[i].key, hint, content })
  }
  return stages
}

/**
 * 把一段内容按品种标签切分。
 * byAsset = 各品种自己的文字；unlabeled = 没有品种前缀的部分（总起句/全局判断）。
 */
export function splitStageByAsset(content: string): { byAsset: Map<string, string[]>; unlabeled: string } {
  const byAsset = new Map<string, string[]>()
  const unlabeledParts: string[] = []
  // 先剥掉可能残留的段名标记（如"[驱动力]"）：真实管道里 parseAnalysisSteps 已剥过，
  // 但直接调用本函数（测试 / 单段处理）时段名会残留在开头，污染"未标注"兜底文本。
  const text = (content || '').replace(
    /^\s*\[(总框架|资产性质|主体识别|因素定位|因素排行|盘面结构分析|盘面结构|驱动力|水位博弈|跨资产资金|反共识结论|逻辑自检|总结)\]\s*[：:]?\s*/,
    '',
  )
  _LABEL_RE.lastIndex = 0
  const hits: { idx: number; end: number; asset: string }[] = []
  let m: RegExpExecArray | null
  while ((m = _LABEL_RE.exec(text)) !== null) {
    const asset = normalizeLabel(labelTokenOf(m))
    // 非品种标签（RSI/USDT/DXY…）不切分：整段留给 unlabeled 兜底，不丢内容
    if (!asset) continue
    hits.push({ idx: m.index, end: m.index + m[0].length, asset })
  }
  if (!hits.length) {
    // 无品种标签时走"按句归属"兜底（模型常写成混排散文，见函数注释）。
    const bySentence = splitByMentionedAsset(text)
    if (bySentence.byAsset.size > 0) return bySentence
    return { byAsset, unlabeled: text.trim() }
  }
  if (hits[0].idx > 0) {
    const lead = text.slice(0, hits[0].idx).replace(/^[；;\s]+/, '').trim()
    if (lead) unlabeledParts.push(lead)
  }
  for (let i = 0; i < hits.length; i++) {
    const start = hits[i].end
    const end = i + 1 < hits.length ? hits[i + 1].idx : text.length
    const seg = text.slice(start, end).replace(/[；;\s]+$/, '').trim()
    if (!seg) continue
    const list = byAsset.get(hits[i].asset) ?? []
    list.push(seg)
    byAsset.set(hits[i].asset, list)
  }
  return { byAsset, unlabeled: unlabeledParts.join(' ') }
}

// 中文别名/商品说法 → 品种（按句归属时用；这些词出现在句子里就说明在讲那个品种）
const _MENTION_PATTERNS: Array<[RegExp, string]> = [
  [/油价|原油|布伦特|WTI|OPEC|供给中断|产量|库存/g, 'WTI'],
  [/金价|黄金|XAU|避险买盘|央行购金/g, 'XAU'],
  [/比特币|BTC|加密|大饼/g, 'BTC'],
  [/以太坊|ETH/g, 'ETH'],
]

/** 检测一段文本里提到的品种（含中文别名/商品说法），按出现次数降序 */
export function detectMentionedAssets(text: string): string[] {
  if (!text) return []
  // 先剥掉"数据字段引用"：BTC.CVD / XAU费率 / WTI.OI 这类是快照字段名，
  // 不表示"这一段在讲该品种"（实测跨资产资金段常这样引用，误判会把共享段拆给某品种）。
  const cleaned = text
    .replace(/\b[A-Z]{2,6}\.(?:CVD|OI|FUNDING|VWAP|RSI|ADX|ATR)\b/gi, '')
    .replace(/[A-Z]{2,6}(?:费率|资金费率|多空比|持仓量|爆仓)/g, '')
  const count = new Map<string, number>()
  for (const [re, asset] of _MENTION_PATTERNS) {
    const n = (cleaned.match(re) ?? []).length
    if (n > 0) count.set(asset, (count.get(asset) ?? 0) + n)
  }
  // 补充：显式代码全词匹配（如 NVDA/META，不在上表里的品种）
  for (const code of detectAssets(cleaned)) {
    count.set(code, (count.get(code) ?? 0) + 1)
  }
  return [...count.entries()].sort((a, b) => b[1] - a[1]).map(([a]) => a)
}

/**
 * 按句子把无标签的混排散文归属到品种。
 *
 * 为什么需要：模型常把多品种分析写成一段混排散文，例如
 *   "…推动国际油价上涨。同时…推动黄金价格上涨。"
 * 没有 `[代码]：` 前缀 → 按标签拆分时内容无所归属 → 每个品种六段都是空的
 * （实测 gpt-4o-mini 的多数输出如此）。
 *
 * 安全规则（宁可少归属，也不猜错造成混合）：
 *   句子恰好提到 1 个品种 → 归给它
 *   提到 0 个 或 ≥2 个品种 → 不归属，留在 unlabeled（内容不丢，但不塞给任何品种）
 */
export function splitByMentionedAsset(content: string): { byAsset: Map<string, string[]>; unlabeled: string } {
  const byAsset = new Map<string, string[]>()
  const rest: string[] = []
  const text = (content || '').trim()
  if (!text) return { byAsset, unlabeled: '' }
  // 按中文/英文句末切分；保留分隔符以便还原
  const sentences = text.split(/(?<=[。！？!?；;])/).map((s) => s.trim()).filter(Boolean)
  for (const s of sentences) {
    const mentioned = detectMentionedAssets(s)
    if (mentioned.length === 1) {
      const list = byAsset.get(mentioned[0]) ?? []
      list.push(s)
      byAsset.set(mentioned[0], list)
    } else {
      rest.push(s)
    }
  }
  return { byAsset, unlabeled: rest.join('') }
}

/** 从 reasoning_path 里检测出现的全部品种（按首次出现顺序） */
export function detectAssets(path: string): string[] {
  if (!path) return []
  const seen: string[] = []
  _LABEL_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = _LABEL_RE.exec(path)) !== null) {
    const a = normalizeLabel(labelTokenOf(m))
    if (a && !seen.includes(a)) seen.push(a)
  }
  return seen
}

/** 组装品种标签：主品种固定第一，随后副品种，最后补文本里检测到的其它品种 */
export function buildAssetTabs(mainAsset: string, siblingAssets: string[], path: string): AssetTab[] {
  const out: AssetTab[] = []
  const push = (asset: string, isMain: boolean) => {
    const a = normAsset(asset)
    // v212 fix: 'OTHER' 已从 MarketCategory 删除。'AGGREGATE' 也不是品种 token（代表无品种事件），
    // 应当在 norAsset() 里被 _NOT_ASSET 黑名单过滤掉，到不了这里。
    if (!a) return
    if (out.some((x) => x.asset === a)) return
    out.push({ asset: a, isMain })
  }
  // NONE 也作为主标签保留：它是真实的主品种取值（"无法归类"），
  // 过滤掉会让第一个副品种被标成"主"（错误的语义标记）。
  push(mainAsset, true)
  // 副品种一律 isMain=false —— 即使主品种被过滤掉，也不让副品种顶替"主"的身份
  for (const s of siblingAssets) push(s, false)
  for (const a of detectAssets(path)) push(a, false)
  return out
}

export type AssetReasoningLine = {
  /** 品种代码（已归一；识别不出则为空串） */
  asset: string
  /** 'main' = 主品种行 / 'sibling' = 副品种行 / null = 无前缀 */
  role: 'main' | 'sibling' | null
  /** 纯内容（已剥掉"主品种 X："前缀） */
  content: string
}

// 行首的"主品种 / 副品种"标记（只要标记本身，后接什么交给 normalizeLabel 处理）
const _AR_ROLE_RE = /^\s*(主品种|副品种)\s*/

/**
 * 归一 asset_reasoning 行首的"品种"部分。
 *
 * 为什么不用一个大正则：真实数据里的写法太杂 ——
 *   "主品种 NONE：" / "主品种 BTC(比特币)：" / "主品种 牛来（市值）：" /
 *   "主品种 BTT(BitTorrent)：" / "主品种 LAPTOP(Laptop 代币)：" / "主品种 黄金："
 * 单一正则的代码组（无论是否贪婪）都会在 "BTT(BitTorren" 或 "(Laptop 代币)" 处
 * 回溯失败而整行识别不出（实测三种格式全挂）。改成"先按分隔符切、再逐个归一"：
 * 取冒号前的内容，反复剥掉括号片段与"市值/代币"这类说明词，最后归一成品种代码。
 */
function parseAssetPrefix(line: string, allowBare: boolean): { asset: string; role: 'main' | 'sibling' | null; content: string } | null {
  const roleM = _AR_ROLE_RE.exec(line)
  let role: 'main' | 'sibling' | null = null
  let body = line
  if (roleM) {
    role = roleM[1] === '主品种' ? 'main' : 'sibling'
    body = line.slice(roleM[0].length)
  } else if (!allowBare) {
    return null
  }
  const colon = body.search(/[:：]/)
  if (colon <= 0) return null
  const head = body.slice(0, colon)
  const content = body.slice(colon + 1).trim()
  // 剥括号片段（中文名 / 说明），如 "BTC(比特币)"、"牛来（市值）"、"LAPTOP(Laptop 代币)"
  let codePart = head.replace(/[（(][^）)]*[）)]/g, '').trim()
  // 代码位置可能只有括号内容（如 "(比特币)："）→ 退回用括号里的中文名
  if (!codePart) codePart = (head.match(/[（(]([^）)]*)[）)]/) ?? [])[1]?.trim() ?? ''
  // 去掉"代币/市值"这类后缀词，留下品种名
  codePart = codePart.replace(/(代币|市值|币)$/u, '').trim()
  if (!codePart) return { asset: '', role, content }
  // 归一为品种代码；归一失败（不在中文清单、也不像代码，如 "牛来"、被黑名单拦下的 "NONE"）
  // 时保留原文 —— 这里已经明确标了"主品种/副品种"，说明它就是品种，
  // 因清单不全而丢弃反而会丢掉品种信息、破坏逐品种完整性。
  const normalized = normalizeLabel(codePart)
  return { asset: normalized || codePart, role, content }
}

/**
 * 解析 asset_reasoning（AI 逐品种解释"为什么这条新闻关联该品种"）。
 *
 * 与六段式的区别：它本身就是"一行一品种"的结构，无需切段。但真实数据格式很杂
 * （裸代码行 "BTC(比特币)："、中文代码 "牛来"、无代码 "主品种："），此处统一归一，
 * 避免各展示处各写一套正则导致标签丢失 / 品种漏记 / 重复占位行。
 */
export function parseAssetReasoning(text: string | null | undefined): AssetReasoningLine[] {
  if (!text || !text.trim()) return []
  const out: AssetReasoningLine[] = []
  for (const raw of text.split(/\n|；|;/)) {
    const line = raw.trim()
    if (!line) continue
    // 先按"主品种/副品种"前缀解析；无该前缀时（裸代码行 "BTC(比特币)："）也允许
    const parsed = parseAssetPrefix(line, true)
    if (parsed && (parsed.role !== null || parsed.asset)) {
      out.push(parsed)
      continue
    }
    out.push({ asset: '', role: null, content: line })
  }
  return out
}

/** 按品种拆分全部段：返回 perAsset 映射 + 未标注兜底 + 段原文 + 出现顺序。
 *
 * `raw` 是每段的**原始文本**：用于在"该品种这一段没有内容"时说明原因
 * （模型整段没写 / 这一段只在讲别的品种 / 该品种只在混写句里被提到），
 * 否则界面只能给一个空框，用户无从判断是数据抓不到还是模型没分析。
 */
export function buildPerAsset(
  contentByKey: Map<string, string>,
): {
  map: Map<string, Map<string, StageItem[]>>
  unlabeled: Map<string, string>
  raw: Map<string, string>
  order: string[]
} {
  const keys: string[] = [...FIXED_STAGE_KEYS, ...SHARED_STAGE_KEYS]
  const map = new Map<string, Map<string, StageItem[]>>()
  const unlabeled = new Map<string, string>()
  const rawByKey = new Map<string, string>()
  const order: string[] = []
  const ensure = (asset: string) => {
    let m = map.get(asset)
    if (!m) {
      m = new Map()
      map.set(asset, m)
    }
    if (!order.includes(asset)) order.push(asset)
    return m
  }
  for (const key of keys) {
    // 旧版"盘面结构"段并入"水位博弈"（prompt 语义即含盘面结构）
    const raw = contentByKey.get(key) ?? (key === '水位博弈' ? contentByKey.get('盘面结构') : undefined)
    if (!raw) continue
    rawByKey.set(key, raw)
    const { byAsset, unlabeled: un } = splitStageByAsset(raw)
    if (un) unlabeled.set(key, un)
    for (const [asset, parts] of byAsset) {
      ensure(asset).set(key, parts.map((t) => ({ label: '', text: t })))
    }
  }
  return { map, unlabeled, raw: rawByKey, order }
}
