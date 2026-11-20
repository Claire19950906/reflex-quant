#!/usr/bin/env python3
"""worker_config.py — ai_worker.py 里可独立存在的纯字面量常量（2026-11-18 抽）。

背景：ai_worker.py 14,885 行，开头 751 行全是模块级常量，把真正的逻辑推到 8,000 行之后。
这些常量是**纯字面量**（字符串/元组/列表/字典，不含函数调用、不读 config、不引用别的常量），
搬出来零风险，也不影响求值顺序。

只搬纯字面量这一批；依赖 os.getenv / config.X 的留在 ai_worker 原处 —— 它们跟运行环境绑定，
搬走反而增加耦合。ai_worker 用 `from engine.worker_config import *` 接回，所以
`ai_worker._SYSTEM_PROMPT` 这类老引用不受影响。
"""

__all__ = [
    "FIELD_WEIGHTS", "_NEUTRAL_SCORE_EPS", "_MAX_PENDING_RETRIES", "_MAIN_BATCH_ROTATE", "_MAIN_STICKY", "_BATCH_RETRY_COOLDOWN_SEC", "_BASELINE_REFRESH_TS", "_ACTIVE_BATCH_LEASE_S", "_ACTIVE_BATCH_HEARTBEAT_S", "_ACTIVE_BATCH_MAX_S", "_ACTIVE_BATCH_GEN", "_WATCHDOG_RECLAIM_SQL_BASE", "_GATE_REPLAY_BATCH", "_SCORE_ACTIONS", "_HARD_DATA_BLOCK_TTL_SEC", "_FACTOR_STOPWORDS", "_DEGRADE_TEXT_MARKS", "_EMPTY_RAW_MIN_CHARS", "_SKELETON_FIELDS", "_SKELETON_MIN_CHARS", "_CN_EARLIEST", "_CN_ORG_CHARS", "_CN_ORG_CHARS_NODIGIT", "_OPENROUTER_HEALTH_TTL", "_OPENROUTER_AUTO_PROBE", "_ANALYSIS_QUOTA_COOLDOWN_S", "_ANALYSIS_NET_COOLDOWN_S", "_ANALYSIS_QUOTA_FAIL_THRESHOLD", "_ANALYSIS_NET_FAIL_THRESHOLD", "_ANALYSIS_AUTH_COOLDOWN_S", "_ANALYSIS_QUOTA_COOLDOWN_LONG_S", "_POOL_ALERT_INTERVAL_S", "_STALL_ALERT_HOURS", "_STALL_STATE", "_SYSTEM_PROMPT_SUFFIX", "_STATIC_KNOWLEDGE", "_MARKET_STRUCTURE_BLOCK", "_SIX_STAGES", "_SIX_STAGE_RETRY_HINT", "_SCHEMA_AUTOFILL_MAP_ASSET_CLASS", "_SCHEMA_REQUIRED_FIELDS_RETRY_HINT", "_MAIN_FOCUS_RETRY_HINT", "_TWO_STEP_A_HINT", "_TWO_STEP_B_HINT_TMPL", "_RESCUE_WINDOW_SEC", "_RESCUE_STATE", "_RESCUE_NONE_STATE", "_RESCUE_SF_STATE", "_STARTUP_RECOVER_DONE_SQL", "_STARTUP_RESET_PENDING_SQL", "_STARTUP_FIX_PENDING_WITH_DECISION_SQL", "_ASSET_ONLY_KEYWORDS", "_SPECIFIC_ASSET_CATEGORY_HINTS", "_STEP_A_KEYS", "_STEP_B_KEYS", "_STEP_A_LEAN_SYSTEM", "_STEP_B_LEAN_SYSTEM", "_SUMMARY_LEAN_SYSTEM", "_SCHEMA_RESCUE_MIN_GAP", "_PER_ASSET_RETRY_HINT", "_COVERAGE_STAGES", "_NON_COVERAGE_STAGES", "_FOCUS_MIN_MENTIONED_STAGES", "_FOCUS_GATE_PREFIX", "_FOCUS_EXEMPT_ASSETS", "_FOCUS_EXEMPT_PATH_PREFIX", "_FOCUS_GATE_PREFIX_BY_ROLE", "_FAILED_ACTION", "_FAILED_GATE_PREFIX", "_CORE_SCOPE_ASSETS", "_DEGRADE_STALE_MARKERS", "_DEGRADE_STALE_SENTENCE", "_V174_FABRICATION_TOKENS", "_SUMMARY_MARKERS", "_SUMMARY_RETRY_HINT", "_DIRECTION_POS_WORDS", "_DIRECTION_NEG_WORDS", "_DIRECTION_RETRY_HINT", "_ASSET_TO_CATEGORY_MAP", "_ASSET_ALIASES", "_PHONE_PUSH_DEDUP_S", "_PHONE_PUSH_MIN_IMPACT", "_DOUBAO_SYSTEM_PROMPT", "_PERF_ASSETS", "_PERF_ACTIONS", "_PERF_PREDICTION_TYPES", "_PERF_LOOKBACK_DAYS", "_PERF_MIN_SAMPLE", "_KLINE_PERSIST_COOLDOWN_S", "_VERIFY_SOURCES", "_VERIFY_RELEVANCE_MIN", "CONSENSUS_GATE_ENABLED",
]

FIELD_WEIGHTS = {
    "reasoning_path":        0.25,  # 八段推理链(最重)
    "reasoning":             0.15,
    "asset_reasoning":       0.10,
    "asset_nature":          0.10,
    "factor_ranking":        0.10,
    "invalidation_condition":0.08,
    "verification":          0.05,
    "hard_data_evidence":    0.05,
    "data_anchor_ts":        0.03,
    "elasticity_window":     0.03,
    "low_impact":            0.02,
    "attention_score":       0.02,
    "impact_magnitude":      0.02,
}

_NEUTRAL_SCORE_EPS = 0.001

_MAX_PENDING_RETRIES = 3

_MAIN_BATCH_ROTATE = {"n": 0}

_MAIN_STICKY = {"idx": None, "since": 0.0}

_BATCH_RETRY_COOLDOWN_SEC = 900

_BASELINE_REFRESH_TS = 0.0

_ACTIVE_BATCH_LEASE_S = 1800.0

_ACTIVE_BATCH_HEARTBEAT_S = 30.0

_ACTIVE_BATCH_MAX_S = 3600.0

_ACTIVE_BATCH_GEN = 0

_WATCHDOG_RECLAIM_SQL_BASE = (
    "UPDATE raw_news SET retry_count = retry_count + 1,"
    " processing_at = datetime('now', 'localtime', '+' || ? || ' seconds'),"
    " status = CASE WHEN retry_count + 1 >= ? THEN 'FAILED' ELSE 'PENDING' END"
    " WHERE status='PROCESSING'"
    # v434：原来这里有一条 `processing_at IS NOT NULL` —— 本意是「没写认领时间的行
    # 不算卡死」，实际效果是它们**永远回收不到**：看门狗只能眼看着它们挂在那里，
    # 只有下次重启的启动复位（_STARTUP_RESET_PENDING_SQL）才清得掉。
    # 实测 2026-11-17 18:00–18:12 有 6 条（33918/33981/34008/34011/34017/34025）
    # 挂在 PROCESSING、processing_at 为空，19:14 重启后才消失。
    # 认领 SQL（_BATCH_CLAIM_FRESH_SQL）是**同一个 UPDATE** 同时写 status 与
    # processing_at，所以 NULL 只可能来自异常路径（批中途被杀 / 落库回写异常），
    # 不会误伤刚认领的行。这里改用新闻入库 ts（epoch 整数，无时区歧义）兜底算年龄；
    # ts 也缺失时按 1970 处理（异常行直接回收，retry_count 仍会把它推到 FAILED）。
    " AND COALESCE(processing_at,"
    "      datetime(COALESCE(ts, 0), 'unixepoch', 'localtime'))"
    "     < datetime('now', 'localtime', '-15 minutes')"
)

_GATE_REPLAY_BATCH = 50

_SCORE_ACTIONS = ("BUY", "SELL", "HOLD")

_HARD_DATA_BLOCK_TTL_SEC = 60.0

_FACTOR_STOPWORDS = ("门卫", "盘面", "仓位", "追单", "抢跑", "先验", "结论", "方向",
                      "事件类型", "摘要", "未异动", "跟单")

_DEGRADE_TEXT_MARKS = (
    "核心监控清单", "非交易信号处理", "按分析降级", "LLM 未返回可用分析",
    "降级原因", "不代表该品种无影响", "未形成八段推演",
)

_EMPTY_RAW_MIN_CHARS = 10

_SKELETON_FIELDS = ("reasoning", "reasoning_path", "asset_nature", "asset_reasoning")

_SKELETON_MIN_CHARS = 30

_CN_EARLIEST = (1990, 1, 1)  # 第②层：远古时间拒收

_CN_ORG_CHARS = r"([\u4e00-\u9fa5社A-Za-z0-9·\-]{2,12})"

_CN_ORG_CHARS_NODIGIT = r"([\u4e00-\u9fa5社A-Za-z·\-]{2,12})"

_OPENROUTER_HEALTH_TTL = 5.0

_OPENROUTER_AUTO_PROBE = False

_ANALYSIS_QUOTA_COOLDOWN_S = 600.0

_ANALYSIS_NET_COOLDOWN_S = 60.0

_ANALYSIS_QUOTA_FAIL_THRESHOLD = 1

_ANALYSIS_NET_FAIL_THRESHOLD = 3

# v494：auth 失败冷却 24h → 15min。实测一次瞬时 401/鉴权抖动就把整个通道
# 封一天（本次 minimax1/minimax2 双双熔断 24h+，引擎零认领、队列只涨不跌）。
# 密钥真失效时每分钟最多试错一次，成本可忽略；env 可调。
_ANALYSIS_AUTH_COOLDOWN_S = 900.0

_ANALYSIS_QUOTA_COOLDOWN_LONG_S = 1800.0

_POOL_ALERT_INTERVAL_S = 21600.0     # 6 小时

_STALL_ALERT_HOURS = 0.5           # 30 分钟没有新决策 → 判停摆

_STALL_STATE = {"last_check_ts": 0.0}

_SYSTEM_PROMPT_SUFFIX = """

# ═══ 必填字段（缺一即 schema 失败；无数据写占位，不留空）═══
- data_anchor_ts：结论依据的快照时间 "YYYY-MM-DD HH:MM:SS"，不知道写 "N/A"。
- hard_data_evidence（≤500字）：逐条结论对应的硬数据（品种+时间+数值）；无则 "N/A"，禁主观归因。
- elasticity_window：{"minutes":整数,"decay":"fast|medium|slow","peak_after_minutes":整数}；不知道填 {"minutes":0,"decay":"slow","peak_after_minutes":0}。
- verification（≤200字）：跨字段自检；一致写 "pass | 全字段一致"，矛盾写 "warn | 具体矛盾"。
- asset_nature：=[总框架] 核心驱动句的紧凑版（≤80字），禁"避险/抗通胀"套话。
- factor_ranking：从 [因素定位] 抽，rank 填原文排位，direction 只能 利多|利空|中性；只列真正命中的 1-2 条。
- snapshot_proximity：引用的快照与 [新闻时间] 的分钟差；无快照或无时间写 "N/A"。
- event_type_code：严格从 prompt 注入的【事件类型选项清单】选 1 个 code，禁自由文本；不属于清单时选最近一项并在 reasoning 说明。
- impact_curve：{"5m":x,"30m":x,"2h":x,"1d":x} 四键必须存在（值可为 0/null）。
- event_window_minutes：整数 [0,4320]（0=瞬时，4320=3天兜底）；不知道 "N/A"。
"""

_STATIC_KNOWLEDGE = """

# ═══ 静态知识附录（恒定内容，用于判盘推理与缓存复用）═══

## 一、三品种驱动结构
黄金：TIPS 实际利率第一（10bp 上行约压制金价 0.5%-0.9%），DXY 第二（负相关），央行购金/地缘溢价第三（刚性但弹性弱）。实际利率下行 + 央行购金 = 最强多头组合；实际利率上行 + 美元走强 = 最顺空头组合。
原油：库存与地缘航运第一（供给端事件弹性最大，断供预期往往一次到位），DXY 第二（强美元压需求预期），OI 拥挤度第三（多头拥挤时利好出尽风险高）。OPEC 减产从"传闻"到"落地"之间最容易抢跑，落地后常见反向。
加密：TIPS 与纳指第一（多数时间与科技股同向，强避险叙事下才切换为数字黄金），费率+OI+爆仓第二（衍生品结构决定短期方向），DXY 第三。

## 二、衍生品结构读法
未平仓量 OI 四象限：价涨+OI 涨=新增杠杆多头进场，趋势有资金加持但拥挤度上升；价涨+OI 跌=空头回补，反弹持续性差；价跌+OI 涨=新增空头进场，下跌动能强；价跌+OI 跌=多头止损去杠杆，常为阶段性见底信号。
资金费率：8h 费率 >0.03% 偏多拥挤，>0.1% 高度拥挤，反转概率显著上升；负费率绝对值同阈值镜像。连续 3 根同号代表单边情绪惯性，需警惕均值回归。
爆仓：连续集中爆多头=加速下跌；集中爆空头=快速拉盘。杠杆踩踏幅度在加密显著大于原油与黄金。
多空持仓比 ls_ratio/top_ls_ratio：大户与散户方向背离时，跟随大户；极端单边（>3 或 <0.33）常为反转前兆。

## 三、技术指标阈值
RSI：>70 超买、<30 超卖；加密高波动下易钝化，重点看背离而非绝对读数。
ADX：<20 无趋势震荡，>25 趋势成立；震荡市的突破信号需成交量确认。
VWAP：日内多空分水岭，现价持续在 VWAP 上方偏多、下方偏空；回踩 VWAP 不破是加仓点。
量能比：>1.5x 放量（趋势可信度上升），≈1x 存量博弈（信号易反复），<0.7x 缩量（突破多为假）。
ATR：用于设止损与仓位，加密 ATR 常为黄金的 2-3 倍。

## 四、跨资产联动与背离
实际利率下行→黄金先涨；若同时风险偏好回升（纳指涨），BTC 与黄金共振上涨。
地缘冲突推升原油→通胀预期抬升→利率维持高位→压制加密估值。
地缘缓和、原油回落→通胀预期回落、降息预期回升→黄金与 BTC 同时受益。
DXY 与风险资产多数时间负相关，与黄金多空不绝对：强美元 + 避险同时出现时，黄金可能逆美元上行。
单品种与其它品种明显分化（如 BTC 涨而黄金原油同跌）→ 独立行情或局部资金驱动，必须在 [反共识结论] 引用具体差异数据，不得笼统归因。

## 五、事件类型与弹性经验
货币政策决议：落地即定价，超额方向看措辞变化与点阵图；"官员表态"属观点类，只给短窗口。
CPI/NFP：偏离预期幅度决定弹性，超预期通常 30 分钟内走完 70% 幅度。
地缘军事：爆发首日弹性最大，随后衰减；缓和/停火为反向信号。
供给中断（OPEC/罢工/制裁）：真实断供与"预期断供"弹性差 2-3 倍，后者易反转。
监管与 ETF 类：获批为结构性利多，拒绝为结构性利空，落地后常反抽。
财报与公司事件：对**个股自身**是实质的基本面增量——估值历史分位、营收/EPS 同比、最近一次预期差、下次一致预期都已进方向因子（权重 0.08，慢变量、弱信号）；对**非股票品种**（BTC/黄金/原油/指数）不构成方向依据，只能经产业链或风险偏好间接传导，且必须写出具体链条，写不出链条就一句「与本品种无关」。
观点/预测/汇总类：已被市场消化或无可执行增量，按 HOLD 处理并说明理由。

## 六、常见误判与自检清单
误判 1：把"官员称可能降息"当降息落地——这是观点类，只给短窗口方向。
误判 2：把"已大涨"当利多延续——利好出尽，费率极端时反向风险高。
误判 3：把多品种都说一遍当覆盖——只写有独立传导链的，其余一句带过。
误判 4：用美国数据解释非美主体——国别不匹配时数据无效，写 N/A。
误判 5：技术面缺失就当没方向——基本面强信号仍可给方向，只是置信度下调。
误判 6：把聚合性市场信号（多家公司集体行动）当个股——归对应市场指数，action=HOLD。
误判 7：给 BTC/黄金/原油套市盈率或净利润——非公司标的没有公司级基本面，该因子对它们不可得，一律写 N/A，不得用市值/PE 类比编造。
自检清单：方向与评分一致否；主品种与 market_category 配对否；七段是否覆盖主品种；硬数据是否带时间与数值；反共识点是否有具体证据。

## 七、品种与产业链映射
黄金：珠宝需求（印度/中国节日季）、央行购金（世界黄金协会季度数据）、矿产供给（南非/澳洲扰动）、ETF 持仓（SPDR 流量）。矿业股对金价弹性约 1.5-2 倍。
原油：上游（页岩钻机数、OPEC+ 配额）、中游（管道/油轮运费、霍尔木兹与红海通行）、下游（裂解价差、炼厂开工）。油运受阻对油价的传导通常 1-3 个交易日到位。
加密：BTC 为流动性锚，ETH 与生态升级/质押率相关，SOL 与链上活跃度相关。ETF 净流入是最直接的增量资金证据；稳定币总供应增长代表场外资金进场。
股票：SKHYNIX 与 HBM/DRAM 价格周期强相关，涨价与扩产公告是最强催化；A 股看政策与流动性，美股看财报与指引，港股看南向资金与汇率。

## 八、时间窗口与信号半衰期
政策表态类：15-45 分钟，超过 1 小时基本失效。
数据发布类（CPI/NFP/PMI）：30 分钟内走完主要幅度，日内余波 2-4 小时。
供给中断类：首日弹性最大，2-3 天后若未持续升级则快速衰减。
监管与结构性事件（ETF/法案）：数日到数周，属趋势级而非脉冲级。
公司财报：当日超预期/低于预期主要在盘后与次日开盘定价，价格脉冲 1-2 个交易日内衰减，**脉冲本身不作延续信号**；但财报改变的是盈利一致预期，这一层是慢变量、可持续数周，所以「估值分位 + 盈利修正方向」可以作为个股的中期方向背景，与当日新闻方向相反时按弱信号处理。
经验法则：新闻发布 45 分钟后仍未出现价格跟随，视为未被市场认可，方向应降级或转 HOLD。

## 九、数据口径速查
tokens 与价格快照：close 为快照时刻最新成交价，涨跌幅为 24h 变化，RSI/ADX 基于 1H K 线。
funding 为最近一期 8h 资金费率；OI 为名义持仓（USD）；CVD 为累计成交量差，正=主动买盘占优。
ls_ratio 为全市场多空账户比，top_ls_ratio 为大户多空比；两者背离时以大户为准。
margin_debt 为美股保证金债务（月度，滞后 1-2 月），用于判断杠杆周期而非短线方向。
btc_etf_net 为现货 ETF 当日净流入（USD），连续 3 日为同向才具趋势意义。
cot_noncomm_net 为 CFTC 非商业净持仓（周度，滞后 3 日），反映投机资金构成。
fund.* 有两个口径，看行首分组：**美股财报**（美元，revenue_ttm/eps_ttm 是最近 4 个季度求和 TTM，revenue_yoy/eps_yoy 与去年同期 4 季相比，gross_margin=TTM 毛利/营收，next_earnings_days 来自财报日历，last_eps_surprise=(实际−预期)/|预期|）；**A股/港股财报**（人民币/港币，营收单位「亿」，累计口径已还原为 TTM，给 net_profit_yoy（净利累计同比）、roe(年化)、pe_ttm、pb 快照）。两者都是季报级、季度尺度，勿当日内信号。
A股/港股估值分位在样本 <60 时不给（表里就没有 stock.pe_pct 这类字段），此时只写"分位样本不足(N/A)"，不得用美股分位或行业均值顶上；PE≤0（亏损）写"PE不适用"并退用 PB。
target_asset 若是 CN_STOCK / HK_STOCK / US_STOCK / INDIVIDUAL_STOCK 这类**占位标签**（新闻未解析出具体代码）：禁止编造任何财报数字或归因到某家公司，只能写"该标的未解析到具体个股、无公司级财报（N/A）"；有具体代码（如 600519.SH / 00700.HK / NVDA）时才可以引财报。
SKHYNIX 归 STOCK 但韩国市场财报未接入：只可用 HBM/DRAM 价格周期与新闻，不得编造其营收/EPS。
eq.pe 附带的「分位」是本系统自己 1 年快照累积出来的分位（附样本数 n），样本 <60 条按铁律不作结论；PE≤0（亏损）不给分位。
任何口径缺失或单点异常，一律写"未接入(N/A)"或"数据不稳定(N/A)"，不得用近似值代替。

## 十、评分与方向一致性规则
asset_impact 与 sentiment_score 必须同向：impact 高但 score 接近 0 属自相矛盾，需要在 [逻辑自检] 说明。
HOLD 不等于中性：HOLD + 明显方向分（如 -0.5）= "看得懂方向但当前不交易"，这是允许且常见的组合。
同一新闻多品种：主品种给完整判断，副品种只在确有独立传导链时给，且方向必须与主品种的传导逻辑自洽。
置信度分级：高（数据齐、事件落地、方向与盘面一致）；中（部分数据缺失或观点类事件）；低（数据缺失且事件未落地）——低置信度一律倾向 HOLD。

## 十一、新闻源特性与噪音识别
快讯类源（金十/华尔街见闻等）：时效最快，但常见同一条消息多轮推送（"据悉"→"确认"→"解读"），后两轮多为情绪延续，不叠加新方向。
社媒转载源：标题党多，常省略主语与时间，必须先判主体与国别，再判性质。
官方源（央行/统计局/交易所公告）：可信度最高，落地即为事实事件；解读文章属观点类。
研报与汇总源：多为已消化信息，除非含未公开增量（目标价上调、评级变动），否则按 HOLD。
噪音特征：纯答疑、行情播报、赛事/娱乐、无主体转述、"或将/有望"无数据支撑、重复推送同一事件。
聚合特征："多家公司回购/增持/预增"属市场情绪信号，归对应指数并 HOLD，不逐家拆分。
识别原则：先问"这条信息改变了我对某个可交易品种的供需或风险偏好预期吗"，答否即为噪音。

## 十二、失效条件与仓位表达
invalidation_condition 写法：必须是可观测的价格或事件条件，如"XAU 站回 VWAP 2401 上方且 30 分钟不回落"、"OPEC 正式宣布减产"；禁止写"如果市场变化"这类不可验证表述。
止损参考：以 ATR 的 1.5-2 倍或最近结构低/高点为界，加密取 2-2.5 倍。
窗口匹配：分钟级事件配 5-30 分钟窗口，数据类事件配 2-4 小时，结构性事件配 1-3 天。
仓位倾向：高置信度 + 直接催化 = 标准仓位；中置信度 = 半仓并设紧止损；低置信度 = 只观察不建仓。
反向预案：任何方向结论都要写明"什么情况下这个判断失效"，这是 [逻辑自检] 段必须落地的内容。




"""

_MARKET_STRUCTURE_BLOCK = """

═══ 盘面结构分析（开关已开启，并入水位博弈段执行）═══
在水位博弈段内，结合本条新闻做多周期结构量化判断（快照提供 1d/4h/1h/15m
周期趋势、ATR、区间高低点与资金费率；标 N/A 的周期当不存在，禁止补造）：
  · 价位位置：当前价在区间位置（>80% 高位=突破敏感 / <20% 低位=支撑敏感 / 40-60% 中性）
  · 波动分档：ATR<0.5%=低波动(新闻冲击被放大) / 0.5-1.5%=中波动 / >1.5%=高波动(冲击钝化)
  · 周期匹配：按 expected_horizon 选主导周期——intraday 看 1H/15m、1-3d 看 4H/1H、1w+ 看 1d/4H
  · 新闻×结构：利多+临近阻力=突破机会 / 利多+突破回踩=回踩机会 / 利空+支撑下方=破位 /
              新闻方向+结构反向=结构阻力高警惕
  · 周期共振：多周期同向=强确认；冲突=降级 confidence、market_confirmation 设 negative
  · 结构评分(-2~+2) → 写入 market_confirmation（>0 positive / <0 negative / =0 unknown）
"""

_SIX_STAGES = ("总框架", "因素定位", "驱动力", "水位博弈", "跨资产资金", "反共识结论", "逻辑自检")

_SIX_STAGE_RETRY_HINT = (
    "【v176.3 铁命令】你上一条 reasoning_path 用的是旧段名（资产性质/因素排行/叙事逻辑/因果链分析等），已被系统作废。"
    "现在严格按 v176.3 新顺序重写 reasoning_path，逐段输出，每段以方括号段名开头："
    "[总框架] → [因素定位] → [驱动力] → [水位博弈] → [跨资产资金] → [反共识结论] → [逻辑自检] → [总结]"
    "禁止再出现 [资产性质] / [因素排行] / [主体识别] / [主体] / [叙事逻辑] / [因果链分析] 等旧段名 ——"
    "你只要再写一个旧段名，整个 JSON 直接判无效入库。"
    "七段缺一不可（信息不足的段写\"无明显信号\"），其它字段保持不变，只输出完整 JSON。"
)

_SCHEMA_AUTOFILL_MAP_ASSET_CLASS = {
    "CRYPTO": "高Beta",
    "GOLD": "避险/抗通胀",
    "OIL": "供给驱动",
    "MACRO": "宏观锚",
    "STOCK": "高Beta",
}

_SCHEMA_REQUIRED_FIELDS_RETRY_HINT = (
    "你上一条 JSON 顶层缺少必填字段：data_anchor_ts / hard_data_evidence / "
    "elasticity_window / verification / asset_classification / fund_driver / "
    "consensus_vs_us。这些字段就算没有真实数据也必须出现，"
    "用占位值填充（不知道就写 \"N/A\" / 数字 0 / 空对象）。"
    "请立即补全："
    "\"data_anchor_ts\": 取 prompt 快照时间（如 \"2026-09-21 11:05:00\"），不知道就 \"N/A\"；"
    "\"hard_data_evidence\": 列出你引用的真实数据（品种+时间+数值），没有就 \"N/A\"；"
    "\"elasticity_window\": {\"minutes\": <整数>, \"decay\": \"fast\"|\"medium\"|\"slow\", \"peak_after_minutes\": <整数>}；"
    "\"verification\": 跨字段自检结果，\"pass | 全字段一致\" 或 \"warn | <具体矛盾>\"；"
    "\"asset_classification\": 从\"避险/抗通胀|供给驱动|高Beta|宏观锚|政策敏感\"选 1 个；"
    "\"fund_driver\": 一句话驱动力总结，<=80 字；"
    "\"consensus_vs_us\": \"一致\" 或 \"反共识\"。"
    "只补缺失的字段，其它字段保持不变，重新输出完整 JSON。"
)

_MAIN_FOCUS_RETRY_HINT = (
    "你的 reasoning_path 七段里几乎没有针对你判定的主品种（target_asset）的分析 —— "
    "七段通篇在讲宏观/地区政治/别的资产，却没讲这个品种本身。规则："
    "【七段必须围绕 target_asset 展开】—— [水位博弈] 讲该品种的盘面位置（引用快照里"
    "该品种的 RSI/ADX/VWAP/量能）、[驱动力] 讲什么在"
    "推动该品种价格、[反共识结论] 给该品种的独立判断。禁止写与该品种无关的泛泛而谈。"
    "请重写 reasoning_path：每段都要落到 target_asset 上。"
    "如果你重新判断这条新闻其实与该品种没有明确传导链（例如会议/峰会/区域合作类新闻），"
    "那就把 target_asset 改为 \"NONE\"、suggested_action 改为 \"HOLD\"、"
    "sentiment_score 给中性区（-0.05~+0.05 之间的小数；若有方向倾向照常给 ±0.15 以上），"
    "并在七段里说明为何无可交易标的。"
    "其它字段保持不变，只输出完整 JSON。"
)

_TWO_STEP_A_HINT = (
    "【第一步·只锁标的与方向】只输出这 4 个键的 JSON，不要八段、不要解释、不要留空：\n"
    "target_asset（必须是具体品种代码：XAU/WTI/BTC/ETH/SOL 或美股代码，禁止 NONE/OTHER/空）\n"
    "suggested_action（BUY/SELL/HOLD）\nsentiment_score（-1~1）\n"
    "market_category（CRYPTO/GOLD/OIL/STOCK/MACRO/COMMODITY）\n"
    "若这条新闻确实没有任何可交易标的，target_asset 写 NONE 并在 reasoning 里一句话说明原因。"
)

_TWO_STEP_B_HINT_TMPL = (
    "【第二步·只补八段与证据】目标标的已确定为 {asset}（方向 {action}）。\n"
    "现在只输出这些键，每个都必须有实质内容，禁止空串/空数组/占位符：\n"
    "reasoning_path（必须按顺序包含 [总框架][因素定位][主体识别][因果链命中][驱动力][弹性窗口][自检][总结] 八段，"
    "每段至少一句完整中文，[总框架] 里必须写\"目标资产 {asset}\"）\n"
    "asset_nature / factor_ranking（非空数组）/ hard_data_evidence / verification / "
    "asset_classification / fund_driver / consensus_vs_us / reasoning（>=20 字）"
)

_RESCUE_WINDOW_SEC = 600

_RESCUE_STATE = {"window_start": 0.0, "used": 0}

_RESCUE_NONE_STATE = {"window_start": 0.0, "used": 0}

_RESCUE_SF_STATE = {"window_start": 0.0, "used": 0}

_STARTUP_RECOVER_DONE_SQL = (
    "UPDATE raw_news SET status='DONE', processing_at=NULL"
    " WHERE status='PROCESSING'"
    "   AND EXISTS (SELECT 1 FROM ai_decisions d WHERE d.news_id = raw_news.id)"
)

_STARTUP_RESET_PENDING_SQL = (
    "UPDATE raw_news SET status='PENDING', processing_at=NULL"
    " WHERE status='PROCESSING'"
    "   AND NOT EXISTS (SELECT 1 FROM ai_decisions d WHERE d.news_id = raw_news.id)"
)

_STARTUP_FIX_PENDING_WITH_DECISION_SQL = (
    "UPDATE raw_news SET status='DONE', processing_at=NULL"
    " WHERE status='PENDING'"
    "   AND EXISTS (SELECT 1 FROM ai_decisions d WHERE d.news_id = raw_news.id)"
)

_ASSET_ONLY_KEYWORDS = (
    "BTC", "比特币", "BITCOIN",
    "ETH", "以太坊", "ETHEREUM",
    "SOL", "SOLANA",
    "XAU", "黄金", "金价", "XAG", "白银", "贵金属",
    "WTI", "布伦特", "原油", "油价", "石油",
    "美股", "港股", "A股", "标普", "纳斯达克", "道琼斯",
)

_SPECIFIC_ASSET_CATEGORY_HINTS = (
    "XAU", "GOLD", "黄金", "XAG", "SILVER", "白银",
    "BTC", "BITCOIN", "比特币", "ETH", "ETHEREUM", "以太坊",
    "WTI", "BRENT", "原油", "OIL",
    "US_STOCK", "STOCK", "EQUITY", "美股", "股票", "港股", "A股",
    "SOL", "CRYPTO", "加密货币",
)

_STEP_A_KEYS = ("target_asset", "suggested_action", "sentiment_score", "market_category")

_STEP_B_KEYS = ("reasoning_path", "asset_nature", "factor_ranking", "hard_data_evidence",
                "verification", "asset_classification", "fund_driver", "consensus_vs_us",
                "reasoning", "no_trade_reason")

_STEP_A_LEAN_SYSTEM = (
    "你是量化交易分析助手。只输出一个 JSON 对象，不要解释、不要 markdown。\n"
    "必填 4 键：target_asset（具体品种代码：XAU/WTI/BTC/ETH/SOL 或美股代码；"
    "确实没有可交易标的才写 NONE）、suggested_action（BUY/SELL/HOLD）、"
    "sentiment_score（-1.0~1.0 的浮点）、market_category（CRYPTO/GOLD/OIL/STOCK/MACRO/COMMODITY）。\n"
    "判断规则：先看新闻主体是谁、影响哪条定价链，再给方向；方向不确定就用 HOLD，"
    "但 sentiment_score 必须给非零数值（确实中性才写 0）。"
)

_STEP_B_LEAN_SYSTEM = (
    "你是量化交易分析助手。目标标的已确定：{asset}，方向 {action}。"
    "现在只输出一个 JSON 对象，不要解释、不要 markdown，每个键都必须有实质中文内容。\n"
    "必填键：reasoning_path（必须按顺序原样含 [总框架][因素定位][驱动力][水位博弈]"
    "[跨资产资金][反共识结论][逻辑自检][总结] 八段，每段至少一句完整话，[总框架] 必须写“目标资产 {asset}”）、"
    "asset_nature、factor_ranking（非空数组，按权重排）、hard_data_evidence、verification、"
    "asset_classification、fund_driver、consensus_vs_us、reasoning（不少于 20 字）、"
    "no_trade_reason（可空字符串）。\n"
    "硬约束：不要改动已确定的标的与方向；数字与新闻里出现的数字必须一致，缺失就写“未披露”。"
)

_SUMMARY_LEAN_SYSTEM = (
    "你是量化交易分析助手。目标标的：{asset}，方向：{action}。"
    "只输出一个 JSON 对象，不要解释、不要 markdown。\n"
    "必填键：summary（按品种分行的 [总结] 段，2-4 句：为什么是这个方向、"
    "什么条件下会改判、当前最需要盯的一个变量）。\n"
    "硬约束：不得改动已给定的方向；不得编造新闻里没有的数字。"
)

_SCHEMA_RESCUE_MIN_GAP = 3

_PER_ASSET_RETRY_HINT = (
    "你的 reasoning_path 八段里缺少某些副品种的逐品种分析。规则："
    "sibling_decisions 里出现的每个品种，八段【每一段】都必须有它自己的行，"
    "格式为「[品种代码]：该品种的分析」（主品种行放最前）。"
    "请重写 reasoning_path：每个涉及的品种在每一段都有独立一行，"
    "写该品种【自己】的价位/逻辑/影响，禁止照抄主品种的句子换个股名。"
    "某品种在某段确实无数据/推不出结论时，也必须写出该行并说明具体原因"
    "（如「[XAU]：本次新闻与黄金无直接传导链，本段不适用」、"
    "「[WTI]：快照缺该品种 RSI 数据，无法判断水位（N/A）」），"
    "禁止省略该行、禁止只写\"无/暂无\"。"
    "另外：如果你判断某个副品种其实与新闻无关、说不出它自己的分析，"
    "就把它从 sibling_decisions 里删掉（宁可不列，也不要列了却不写分析）。"
    "其它字段保持不变，只输出完整 JSON。"
)

_COVERAGE_STAGES = ("总框架", "因素定位", "驱动力", "水位博弈", "跨资产资金", "反共识结论", "逻辑自检")

_NON_COVERAGE_STAGES = ("总结",)

_FOCUS_MIN_MENTIONED_STAGES = 1  # v176.3 fix: 从 2 降到 1。

_FOCUS_GATE_PREFIX = "分析未落到主品种"

_FOCUS_EXEMPT_ASSETS = ("", "NONE", "OTHER")

_FOCUS_EXEMPT_PATH_PREFIX = "关键词推断"

_FOCUS_GATE_PREFIX_BY_ROLE = {
    "主品种": "分析未落到主品种",
    "副品种": "分析未落到副品种",
}

_FAILED_ACTION = "FAILED"

_FAILED_GATE_PREFIX = "分析失败"

_CORE_SCOPE_ASSETS = ("BTC", "XAU", "WTI", "ETH", "SOL")

_DEGRADE_STALE_MARKERS = ("目标资产 NONE", "**不在**核心监控清单")

_DEGRADE_STALE_SENTENCE = "该标的**不在**核心监控清单（BTC/XAU/WTI/ETH/SOL），按非交易信号处理"

_V174_FABRICATION_TOKENS = ("fabricated", "fabrication", "价格幻觉", "幻觉")

_SUMMARY_MARKERS = ("[总结]", "【总结】", "总结：", "总结:")

_SUMMARY_RETRY_HINT = (
    "你的 reasoning_path 缺少 [总结] 段，或多品种时没有给每个品种单独一行总结。"
    "请在七段（[总框架]…[逻辑自检]）之后，补一段 [总结]，"
    "严格按品种分行（主品种 + 每个 sibling 各一行），格式：\n"
    "  [总结]\n"
    "  [品种代码]：该品种最终怎么看、依据是什么、方向是什么\n"
    "要求：① 每个品种都要有自己的总结行，禁止只总结主品种；"
    "② 每个品种的总结方向必须与它自己的 suggested_action 一致；"
    "③ 禁止\"综上所述/总体来看\"这类没有品种归属的空话；"
    "④ 七段里任何一段都不许留空，无数据的段写明具体原因。"
    "其它字段保持不变，只输出完整 JSON。"
)

_DIRECTION_POS_WORDS = (
    "看多", "偏多", "利多", "利好", "上涨", "上行", "反弹", "回升",
    "买入", "增持", "看涨", "bullish", "upside",
    # 资金/避险流向（八段分品种常用）
    "流入", "净流入", "增仓", "避险买盘", "买盘", "需求上升", "资金流入",
    "走强", "强势", "升破", "放量上涨", "多头占优",
    # 传导类动词（分品种行常见："避险推升金价" / "供给担忧提振油价"）
    "推升", "推涨", "提振", "推高", "带动上涨", "刺激上涨",
    # 情绪/需求类（实测高频，缺失会让"该品种看多"算成中性）
    "需求回暖", "需求改善", "买盘增加", "受益", "获支撑", "筑底", "企稳",
    "偏强", "转强",
)

_DIRECTION_NEG_WORDS = (
    "看空", "偏空", "利空", "下跌", "下行", "回落", "承压",
    "卖出", "减持", "抛售", "看跌", "bearish", "downside",
    # 资金/避险流向（八段分品种常用）
    "流出", "净流出", "减仓", "抛压", "卖盘", "需求走弱", "资金流出",
    "走弱", "弱势", "跌破", "放量下跌", "空头占优", "避险属性弱",
    "避险买盘消退", "避险需求下降", "避险情绪降温",
    # 传导类动词（分品种行常见："避险退潮压制金价" / "需求疲软拖累油价"）
    "压制", "拖累", "打压", "施压", "推低", "带动下跌", "刺激下跌",
    # 资金轮动/情绪类（实测高频：写"转为风险资产/负面/谨慎"就是在看空该品种）
    "转向风险资产", "风险偏好上升", "负面影响", "需求下降", "需求疲软",
    "前景不明", "情绪谨慎", "保持谨慎", "回调风险", "回调", "抛售压力", "卖压",
    "转弱", "偏弱",
)

_DIRECTION_RETRY_HINT = (
    "你上一条输出的 suggested_action 与 reasoning_path 七段推演的方向相反（自相矛盾）。"
    "请复核并修正：要么调整 suggested_action 使其与推演结论方向一致，"
    "要么修正推演内容使其支持 suggested_action 的方向。"
    "注意\"利好出尽/利空出尽\"等反向场景：推演中必须明确写出反向措辞（如\"利好出尽、"
    "警惕回落\"），禁止让推演方向词与 action 自相矛盾。"
    "其它字段保持不变，只输出完整 JSON。"
)

_ASSET_TO_CATEGORY_MAP = {
    "XAU": "GOLD", "WTI": "OIL", "BTC": "CRYPTO", "ETH": "CRYPTO", "SOL": "CRYPTO",
    "NVDA": "STOCK", "TSLA": "STOCK", "AAPL": "STOCK", "MSTR": "STOCK",
    "MSFT": "STOCK", "AMZN": "STOCK", "GOOGL": "STOCK", "COIN": "STOCK",
    "PLTR": "STOCK", "META": "STOCK", "SMCI": "STOCK", "MARA": "STOCK",
    "RIOT": "STOCK",
    # v175 品类扩展：A股 / 韩股海力士 / 个股兜底
    "SKHYNIX": "STOCK", "CN_STOCK": "STOCK", "US_STOCK": "STOCK", "INDIVIDUAL_STOCK": "STOCK",
}

_ASSET_ALIASES = [
    ("XAU", ("黄金", "金价", "xau", "gold")),
    ("WTI", ("原油", "油价", "石油", "油气", "wti", "oil", "布伦特")),
    ("BTC", ("比特币", "btc", "bitcoin")),
    ("ETH", ("以太坊", "eth", "ethereum")),
    ("SOL", ("sol", "solana", "索拉纳")),
    # 美股（币安永续可交易）
    ("NVDA", ("英伟达", "nvda", "nvidia")),
    ("TSLA", ("特斯拉", "tsla", "tesla")),
    ("AAPL", ("苹果", "aapl", "apple")),
    ("MSTR", ("微策略", "mstr", "microstrategy")),
    ("MSFT", ("微软", "msft", "microsoft")),
    ("AMZN", ("亚马逊", "amzn", "amazon")),
    ("GOOGL", ("谷歌", "googl", "google", "alphabet")),
    ("COIN", ("coinbase", "coin")),
    ("PLTR", ("palantir", "pltr", "帕兰提尔")),
    ("META", ("meta", "facebook", "脸书")),
    ("SMCI", ("smci", "美超微")),
    ("MARA", ("mara", "marathon")),
    ("RIOT", ("riot",)),
    # v175 品类扩展：韩股海力士 + A股
    ("SKHYNIX", ("海力士", "sk hynix", "skhynix", "hynix", "hbm", "高带宽内存", "000660")),
    ("CN_STOCK", ("a股", "上证", "深证", "沪深300", "沪指", "深成指", "创业板", "科创板", "中国股市")),
    # v211 P1-C：扩半导体个股 ticker（与海力士常被同时提及的产业链个股）
    ("MU",      ("美光", "micron", "mu")),
    ("INTC",    ("英特尔", "intel", "intc")),
    ("TSMC",    ("台积电", "tsmc", "2330.tw")),
    ("SAMSUNG", ("三星电子", "三星", "samsung", "005930")),
]

_PHONE_PUSH_DEDUP_S = 600.0              # 同内容 10 分钟内不重复推送

_PHONE_PUSH_MIN_IMPACT = 5.0

_DOUBAO_SYSTEM_PROMPT = """
你是华尔街资深原油/黄金/加密货币交易员，拥有 15 年实盘经验。
你的任务：快速扫读新闻，凭直觉判断这条消息对资产价格的短期方向。
不要给出分数，不要推理链条。只看方向。

规则：
  利多消息 → BUY
  利空消息 → SELL
  方向不明确或无关 → HOLD

JSON 输出：
{"suggested_action": "<BUY|SELL|HOLD>", "direct_reasoning": "<一句交易直觉,<=30字>"}

只输出 JSON。两个字段缺一不可。
"""

_PERF_ASSETS = ("BTC", "XAU")

_PERF_ACTIONS = ("BUY", "SELL")

_PERF_PREDICTION_TYPES = ("continuation", "reversal", "breakout")

_PERF_LOOKBACK_DAYS = 90

_PERF_MIN_SAMPLE = 8

_KLINE_PERSIST_COOLDOWN_S = 60.0         # 无快照时每 1 分钟最多 persist 一次（缩短 K 线断流时间）

_VERIFY_SOURCES = ("jin10_calendar", "fred")

_VERIFY_RELEVANCE_MIN = 0.8

CONSENSUS_GATE_ENABLED = True

