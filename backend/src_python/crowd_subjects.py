# -*- coding: utf-8 -*-
"""crowd_subjects.py — v319「具体话题」层

为什么要有这一层
----------------
v318 的"话题与观点"视图把市场关注点收敛成**题材大类**（科技/AI、金融、消费…）。
分类是清楚的，但看不出**市场上到底在聊什么**。用户要的是具体话题：
英特尔暴跌 / 黄金 / 美国中期选举 / 三季报 / Binance 黑客 / 宇树科技。

做法
----
在题材之下加一层「主体（subject）」归并，一个主体 = 一张卡：
  1. 核心词典命中（公司/商品/宏观/主题/事件/指数，中英别名 → 规范中文名）
  2. 结构化信号（同花顺概念与个股名、CoinGecko 币种名）—— 数据源直接给的，最准
  3. 窗口内自动挖掘的高频短语 —— 数据驱动，保证当天冒出来的新话题不漏
同一主体在不同平台/媒体出现 → 归到一张卡，**跨源共识才算数**。

口径纪律
--------
  · 多空观点沿用 crowd_pulse 的关键词口径，**逐条重算**；
    不复用 crowd_topic_daily 的聚合值（那张表按题材聚合，方向会被抹平）
  · 分类（题材/市场/资产类别）作为卡片的**次要标签**，不作为主体
  · 所有数字都来自真实抓取的原始条目，可回溯到 url
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import crowd_pulse

# ─────────────────────────────── 核心主体词典 ───────────────────────────────
# (规范名, 别名列表, 类别标签)
# 规范名统一用中文；英文别名只用于命中，命中后显示中文规范名
# —— 这样"英特尔"(中文源) 和 "Intel"(英文源) 会归到同一张卡上。
CORE_SUBJECTS: List[Tuple[str, List[str], str]] = [
    # ── 美股科技公司 ──
    ("苹果", ["苹果", "Apple", "AAPL", "iPhone"], "公司"),
    ("微软", ["微软", "Microsoft", "MSFT"], "公司"),
    ("谷歌", ["谷歌", "Google", "Alphabet", "GOOGL"], "公司"),
    ("亚马逊", ["亚马逊", "Amazon", "AMZN"], "公司"),
    ("英伟达", ["英伟达", "NVIDIA", "Nvidia", "NVDA"], "公司"),
    ("特斯拉", ["特斯拉", "Tesla", "TSLA"], "公司"),
    ("英特尔", ["英特尔", "Intel", "INTC"], "公司"),
    ("AMD", ["AMD", "超微半导体"], "公司"),
    ("Arm", ["Arm", "ARM Holdings"], "公司"),
    ("台积电", ["台积电", "TSMC", "Taiwan Semiconductor"], "公司"),
    ("博通", ["博通", "Broadcom"], "公司"),
    ("高通", ["高通", "Qualcomm"], "公司"),
    ("美光", ["美光", "Micron"], "公司"),
    ("SK海力士", ["SK海力士", "SK Hynix", "海力士"], "公司"),
    ("Meta", ["Meta", "Facebook", "脸书"], "公司"),
    ("甲骨文", ["甲骨文", "Oracle"], "公司"),
    ("超微", ["超微", "Super Micro"], "公司"),
    ("Rivian", ["Rivian", "RIVN"], "公司"),
    ("Strategy", ["Strategy", "MicroStrategy", "MSTR"], "公司"),
    ("OpenAI", ["OpenAI", "ChatGPT"], "公司"),
    # ── A股公司 ──
    ("宁德时代", ["宁德时代", "CATL"], "A股"),
    ("比亚迪", ["比亚迪", "BYD"], "A股"),
    ("贵州茅台", ["贵州茅台", "茅台"], "A股"),
    ("中芯国际", ["中芯国际", "SMIC"], "A股"),
    ("寒武纪", ["寒武纪"], "A股"),
    ("长鑫存储", ["长鑫科技", "长鑫存储"], "A股"),
    ("中航沈飞", ["中航沈飞"], "A股"),
    ("工业富联", ["工业富联", "富士康"], "A股"),
    ("兆易创新", ["兆易创新"], "A股"),
    ("新易盛", ["新易盛"], "A股"),
    ("中际旭创", ["中际旭创"], "A股"),
    ("天孚通信", ["天孚通信"], "A股"),
    ("亨通光电", ["亨通光电"], "A股"),
    ("宇树科技", ["宇树科技"], "A股"),
    ("药明康德", ["药明康德"], "A股"),
    ("恒瑞医药", ["恒瑞医药"], "A股"),
    ("隆基绿能", ["隆基绿能"], "A股"),
    ("阳光电源", ["阳光电源"], "A股"),
    ("立讯精密", ["立讯精密"], "A股"),
    ("招商银行", ["招商银行"], "A股"),
    ("工商银行", ["工商银行"], "A股"),
    ("长沙银行", ["长沙银行"], "A股"),
    ("世纪华通", ["世纪华通"], "A股"),
    ("承德露露", ["承德露露"], "A股"),
    ("宏力达", ["宏力达"], "A股"),
    ("中信证券", ["中信证券"], "A股"),
    # ── 商品 ──
    ("黄金", ["黄金", "金价", "gold", "Gold", "XAU", "XAUUSD"], "商品"),
    ("白银", ["白银", "银价", "silver", "Silver", "XAG"], "商品"),
    ("原油", ["原油", "油价", "crude oil", "Crude Oil", "WTI", "Brent", "oil price"], "商品"),
    ("铜", ["铜价", "copper price", "铜价上涨", "铜价下跌"], "商品"),
    ("稀土", ["稀土", "rare earth"], "商品"),
    ("天然气", ["天然气", "natural gas"], "商品"),
    # ── 加密资产 ──
    ("比特币", ["比特币", "bitcoin", "Bitcoin", "BTC"], "加密"),
    ("以太坊", ["以太坊", "ethereum", "Ethereum", "ETH"], "加密"),
    ("Solana", ["Solana", "SOL"], "加密"),
    ("币安", ["币安", "Binance", "BINANCE"], "加密"),
    ("Coinbase", ["Coinbase"], "加密"),
    ("Tether", ["Tether", "USDT"], "加密"),
    ("比特币ETF", ["比特币ETF", "Bitcoin ETF", "spot bitcoin ETF", "Bitcoin ETFs"], "加密"),
    ("稳定币", ["稳定币", "stablecoin"], "加密"),
    # ── 宏观 / 事件 ──
    ("美联储", ["美联储", "Federal Reserve", "Fed", "the Fed", "鲍威尔", "Powell"], "宏观"),
    ("降息", ["降息", "降准", "rate cut", "rate cuts", "cuts rates", "lower rates"], "宏观"),
    ("加息", ["加息", "rate hike", "hikes rates", "raise rates"], "宏观"),
    ("美国CPI", ["CPI", "消费者物价指数", "consumer price"], "宏观"),
    ("美国非农", ["非农", "nonfarm", "non-farm", "payrolls"], "宏观"),
    ("美国中期选举", ["中期选举", "midterm", "midterms", "mid-term"], "宏观"),
    ("关税", ["关税", "tariff", "tariffs"], "宏观"),
    ("人民币汇率", ["人民币汇率", "人民币", "yuan"], "宏观"),
    ("霍尔木兹海峡", ["霍尔木兹", "Hormuz", "Strait of Hormuz"], "宏观"),
    ("伊朗", ["伊朗", "Iran", "Iranian"], "宏观"),
    ("俄乌", ["俄乌", "Russia", "Ukraine", "乌克兰"], "宏观"),
    ("特朗普", ["特朗普", "Trump"], "宏观"),
    ("日本央行", ["日本央行", "Bank of Japan", "BOJ", "日央行"], "宏观"),
    ("证监会", ["证监会", "中国证监会", "SEC"], "宏观"),
    ("美联储议息", ["FOMC", "议息", "利率决议"], "宏观"),
    ("欧洲央行", ["欧洲央行", "European Central Bank", "ECB"], "宏观"),
    ("白宫", ["白宫", "White House"], "宏观"),
    ("英国", ["英国", "UK", "Britain"], "宏观"),
    ("德国", ["德国", "Germany"], "宏观"),
    ("日本", ["日本", "Japan"], "宏观"),
    # ── 财报 / 事件 ──
    ("三季报", ["三季报", "三季度报", "第三季度报告"], "事件"),
    ("业绩预告", ["业绩预告"], "事件"),
    ("回购", ["回购", "buyback", "share repurchase"], "事件"),
    ("减持", ["减持", "stake sale"], "事件"),
    ("IPO", ["IPO", "上市", "首次公开发行"], "事件"),
    ("并购", ["并购", "收购", "merger", "acquisition"], "事件"),
    ("解禁", ["解禁", "限售股解禁"], "事件"),
    ("集采", ["集采", "集中采购"], "事件"),
    ("股东户数", ["股东户数"], "事件"),
    # ── A股主题 ──
    ("人形机器人", ["人形机器人", "机器人", "具身智能"], "主题"),
    ("光模块", ["光模块", "CPO", "共封装光学", "光通信"], "主题"),
    ("算力", ["算力", "AI算力", "智算", "数据中心"], "主题"),
    ("固态电池", ["固态电池"], "主题"),
    ("储能", ["储能"], "主题"),
    ("光伏", ["光伏"], "主题"),
    ("锂电", ["锂电", "碳酸锂"], "主题"),
    ("创新药", ["创新药"], "主题"),
    ("军工", ["军工", "国防", "航天"], "主题"),
    ("低空经济", ["低空经济"], "主题"),
    ("商业航天", ["商业航天"], "主题"),
    ("白酒", ["白酒"], "主题"),
    ("存储芯片", ["存储器", "存储芯片", "memory chip", "memory stocks"], "主题"),
    ("半导体", ["半导体", "芯片", "semiconductor"], "主题"),
    ("自动驾驶", ["自动驾驶", "智能驾驶", "智驾"], "主题"),
    ("大模型", ["大模型", "人工智能", "AI大模型"], "主题"),
    # ── 指数 ──
    ("上证指数", ["上证指数", "沪指", "上证综指"], "指数"),
    ("深证成指", ["深证成指", "深成指"], "指数"),
    ("创业板指", ["创业板指", "创业板指数"], "指数"),
    ("科创50", ["科创50", "科创板50"], "指数"),
    ("恒生指数", ["恒生指数", "恒指"], "指数"),
    ("纳斯达克", ["纳斯达克", "纳指", "Nasdaq"], "指数"),
    ("标普500", ["标普500", "标普", "S&P 500", "S&P 500"], "指数"),
    ("道琼斯", ["道琼斯", "道指", "Dow Jones", "Dow"], "指数"),
]


# ─────────────────────────────── 噪声过滤 ───────────────────────────────
# 标题里这些是**媒体名/栏目名/通用词**，不是"大家在聊的话题"。
# 百度热搜与 Google News 的标题都带尾巴（"… - 东方财富" / "…_环球股市"），
# 不切掉会把媒体名当成话题（原型里 "新浪财经" 出现 263 次就是这么来的）。

# 媒体/站点名（出现在标题尾部，作为分隔符后的署名）
MEDIA_NAMES: List[str] = [
    "新浪财经", "新浪网", "搜狐网", "证券之星", "凤凰网", "凤凰财经", "财联社",
    "东方财富", "同花顺财经", "同花顺", "格隆汇", "英为财情", "观点网",
    "经济参考报", "第一财经", "证券日报", "上海证券报", "中国证券报",
    "每日经济新闻", "投资快报", "环球股市", "短线策略", "股市必读",
    "财新", "经济日报", "人民日报", "新华", "央视", "网易财经", "腾讯财经",
    "BBC", "RFI", "Reuters", "Bloomberg", "CNBC", "Forbes", "Yahoo Finance",
    "CoinDesk", "CryptoSlate", "Crypto News", "CoinGecko", "Investing.com",
    "TradingView", "MarketWatch", "Seeking Alpha", "The Block", "Decrypter",
    "Zacks", "Barrons", "Business Insider", "Financial Times", "New York Times",
    "AP", "AFP", "NPR", "Sky News", "Al Jazeera", "Nikkei", "South China Morning Post",
    "电报君", "财闻", "提供", "提供者", "整理", "校对", "图片来源",
]

# 通用词：描述"市场在做什么"而不是"在聊什么"
GENERIC_CN: List[str] = [
    "财经", "股市", "市场", "证券", "股票", "指数", "板块", "资金", "公司",
    "集团", "经济", "金融", "投资", "交易", "价格", "行情", "今日", "昨日",
    "最新", "重磅", "独家", "分析", "解读", "观察", "评论", "回应", "表示",
    "发布", "官方", "记者", "视频", "图片", "直播", "业绩", "报告", "数据",
    "规模", "增速", "同比", "环比", "净利", "营收", "利润", "亏损", "盈利",
    "股东", "公告", "新闻", "消息", "资讯", "要闻", "前瞻", "展望", "策略",
    "复盘", "盘点", "总结", "时报", "早盘", "午盘", "收盘", "开盘", "盘中",
    "国内", "国际", "全球", "中国", "美国", "日本", "欧洲",
    "大涨", "大跌", "暴跌", "暴涨", "涨停", "跌停", "上涨", "下跌", "反弹",
    "拉升", "抛售", "震荡", "走强", "走弱", "领跌", "领涨", "异动", "调整",
    "披露", "提供", "出现", "进入", "时代", "什么", "怎么", "如何", "究竟",
    "被谁赚走了", "持续", "突然", "集体", "三大", "亿元", "万元", "万亿",
    "跌超", "涨超", "单季", "第四季度", "第三季度", "前三季度", "第一季",
    "美股", "A股", "港股", "科技", "概念", "主题", "龙头", "板块股", "概念股",
    "科技股", "周期股", "成长股", "价值股", "白马", "权重", "题材",
    "股份", "银行", "创业", "解析", "盘前", "盘后", "必读", "每日", "重挫",
    "持股", "持股数", "股东户", "中期", "必", "股盘", "单日", "当日", "近期",
    "年内", "全年", "月度", "财报", "业绩", "营收", "净利", "净亏损", "预增",
    "预减", "扭亏", "季报", "中报", "年报", "季度",
    "大涨", "大跌", "暴跌", "暴涨", "涨停", "跌停", "上涨", "下跌", "反弹",
    "拉升", "抛售", "震荡", "走强", "走弱", "领跌", "领涨", "异动", "调整",
    "逆势", "集体", "三大", "持续", "突然", "大幅", "小幅", "再度", "连续",
    "前三", "收跌", "收涨", "来了", "增长", "股指", "板指", "利好", "利空",
    "巨头", "资产", "市值", "基金", "期货", "现货", "主力", "散户", "机构",
    "北向", "外资", "内资", "融资", "融券", "余额", "仓位", "减仓", "加仓",
    "总市值", "总成交", "成交额", "成交量", "换手", "振幅", "开盘", "收盘",
    "板块", "个股", "行业", "产业链", "供应链", "上下游",
    "港交所", "纽交所",
    "数字", "时代", "全球", "国内", "世界", "各地", "多国", "各国",
    # ── 市场状态词（描述"行情在做什么"，不是在聊什么）──
    "美元", "收官", "预期", "估值", "万股", "降幅", "增幅", "信号", "新高",
    "股价", "多家", "流通", "爆发", "收评", "全线", "重大", "高开", "低开",
    "热门", "政策", "首席", "涨跌不一", "以来", "下降", "最大", "累计",
    "跌幅", "基本", "增量", "第一", "遭遇", "下周", "分红", "开始", "涨幅",
    "销量", "增持", "小时", "监管", "稳定", "回落", "长期", "分化", "重组",
    "积极", "抄底", "向好", "计划", "下挫", "跳水", "有望", "增加", "召开",
    "能源", "历史", "表现", "智能", "今天", "午评", "为何", "资本",
    "关于", "出手", "迎来", "部门", "亿美元", "利率", "居前", "显著",
    "启动", "机会", "发声", "曝光", "座谈会", "波动", "修复", "博弈",
    "一切正常", "却跌没了", "游戏王", "户均", "目标价", "个股", "午后",
    "上午", "下午", "当日", "本周", "上周", "年内", "个月", "全天", "日内",
    "收评", "午盘", "早盘", "复盘", "速睇", "美股讯号",
]

GENERIC_EN: List[str] = [
    "the", "and", "for", "after", "with", "from", "that", "this", "have",
    "has", "are", "was", "will", "can", "may", "says", "say", "said",
    "new", "now", "more", "most", "over", "under", "about",
    "amid", "ahead", "first", "last", "year", "years", "day", "days",
    "week", "weeks", "month", "months", "price", "prices", "market",
    "markets", "report", "reports", "news", "finance", "trading", "update",
    "forecast", "analysis", "outlook", "what", "why", "how", "billion",
    "million", "trillion", "dollar", "dollars", "stock", "stocks", "shares",
    "today", "us", "u.s", "reuters", "yahoo", "all", "here", "low", "one",
    "out", "net", "big", "top", "get", "gets", "got", "make", "makes", "made",
    "its", "it", "his", "her", "their", "our", "your", "who", "whom",
    "which", "while", "will", "would", "could", "should", "must", "may",
    "not", "but", "you", "your", "they", "them", "some", "any", "all",
    "each", "both", "few", "more", "most", "other", "such", "than", "then",
    "there", "these", "those", "through", "under", "very", "just", "also",
    "back", "down", "still", "even", "well", "much", "many", "since",
    "when", "where", "how", "why", "who", "not", "no", "yes", "own",
    "read", "reads", "hold", "holds", "held", "come", "comes", "came",
    "take", "takes", "give", "gives", "go", "goes", "going", "gone",
    "know", "knows", "knew", "think", "thinks", "want", "wants", "need",
    "needs", "use", "uses", "used", "find", "finds", "found", "call",
    "calls", "try", "tries", "ask", "asks", "work", "works", "worked",
    "seem", "seems", "look", "looks", "help", "helps", "talk", "talks",
    "turn", "turns", "start", "starts", "show", "shows", "hear", "hears",
    "play", "plays", "run", "runs", "move", "moves", "live", "lives",
    "believe", "believe", "bring", "brings", "happen", "happens", "write",
    "writes", "provide", "provides", "sit", "sits", "stand", "stands",
    "lose", "loses", "pay", "pays", "meet", "meets", "include", "includes",
    "continue", "continues", "set", "sets", "learn", "learns", "change",
    "changes", "lead", "leads", "understand", "understands", "watch",
    "watches", "follow", "follows", "stop", "stops", "create", "creates",
    "speak", "speaks", "read", "allow", "allows", "add", "adds", "spend",
    "spends", "grow", "grows", "walk", "walks", "win", "wins", "offer",
    "offers", "remember", "remembers", "love", "loves", "consider",
    "considers", "appear", "appears", "buy", "buys", "serve", "serves",
    "die", "dies", "send", "sends", "expect", "expects", "build", "builds",
    "stay", "stays", "fall", "falls", "cut", "cuts", "reach", "reaches",
    "kill", "kills", "remain", "remains", "suggest", "suggests", "raise",
    "raises", "pass", "passes", "sell", "sells", "require", "requires",
    "report", "decide", "decides", "pull", "pulls", "return", "returns",
    "explain", "explains", "hope", "hopes", "develop", "develops", "carry",
    "carries", "break", "breaks", "receive", "receives", "agree", "agrees",
    "support", "supports", "hit", "hits", "produce", "produces", "eat",
    "eats", "cover", "covers", "catch", "catches", "draw", "draws",
    "choose", "chooses", "cause", "causes", "point", "points", "listen",
    "listens", "realize", "realizes", "place", "places", "close", "closes",
    "involve", "involves", "increase", "increases", "decrease", "decreases",
    "rise", "rises", "fall", "falls", "gain", "gains", "lose", "loses",
    "open", "opens", "close", "closes", "drop", "drops", "jump", "jumps",
    "plan", "plans", "happen", "value", "values", "worth", "worries",
    "watch", "official", "officials", "media", "channel", "source",
    "sources", "click", "here", "read", "share", "tweet", "follow",
]

_CN_RE = re.compile(r"[一-鿿]+")
_HASHTAG_RE = re.compile(r"[#＃]([^#＃\s]{1,14})[#＃]")
_EN_RE = re.compile(r"[A-Za-z][A-Za-z'&.\-]*")
_NUM_RE = re.compile(r"\d")

# 别名 → 规范名 的反查表
_ALIAS_TO_CANON: Dict[str, Tuple[str, str]] = {}
for _canon, _aliases, _kind in CORE_SUBJECTS:
    for _a in _aliases:
        _ALIAS_TO_CANON.setdefault(_a.lower(), (_canon, _kind))

# 词典别名集合：SubjectMatcher 用它区分「固定词典」与「自动挖掘」两条匹配路径
_DICT_ALIAS_SET: set = set(_ALIAS_TO_CANON.keys())


def _strip_media(title: str) -> str:
    """切掉标题尾部的媒体署名（"… - 东方财富" / "…_环球股市"）。

    媒体名既出现在尾部，也可能在中间（"… - 手机新浪网"），
    所以除了切分隔符，还要把残留的媒体名整段删掉。
    """
    t = str(title or "")
    for sep in (" - ", " _ ", "｜", " | ", "——", "—"):
        if sep in t:
            head, tail = t.rsplit(sep, 1)
            if tail.strip() and len(tail.strip()) <= 24:
                t = head
                break
    # 去掉 hashtag 尾巴（"#美股#科技#AI2026"）
    t = re.sub(r"[#＃][^\s#＃]{1,12}", " ", t)
    # 删掉残留的媒体署名（"手机新浪网" → ""；"东方财富" → ""）
    for m in MEDIA_NAMES:
        if m and m in t:
            t = t.replace(m, " ")
    return t.strip()


def _is_noise_cn(gram: str) -> bool:
    """通用词判断。核心词典里的主体名不受此限制（三季报 vs 季报）。"""
    if _NUM_RE.search(gram):
        return True
    for w in GENERIC_CN:
        if w in gram:
            # 词典主体名优先：若 gram 本身就是词典别名，跳过通用词判定
            if gram in _ALIAS_TO_CANON:
                return False
            return True
    return False


# 说话人/栏目：冒号前缀是这些词时，它在引用某人或某栏目，不是主体
_SPEAKER_PREFIX = (
    "专家", "业内", "研究员", "分析师", "媒体", "记者", "编辑", "业内人士",
    "消息人士", "知情人士", "官方", "接近人士", "人士", "机构人士",
)

# 机构类词尾 → 归为"机构"而非"公司"
_ORG_SUFFIX = (
    "局", "委", "部", "厅", "署", "院", "所", "大学", "协会", "商会",
    "央行", "交易所", "研究院", "实验室", "政府", "总部",
)

# 组织感词表：冒号前缀带这些词才像"公司/机构名"
_ORG_HINTS = (
    "公司", "集团", "银行", "证券", "保险", "基金", "股份", "科技", "控股",
    "局", "委", "部", "厅", "署", "院", "所", "大学", "学院", "协会", "商会",
    "研究院", "实验室", "中心", "平台", "机构", "企业", "工厂", "总公司",
    "工作室", "事务所", "交易所", "央行", "商会", "工作室",
)

# 明显不是主体的高频短语（动作/副词/栏目）
_NOT_SUBJECT = (
    "发生了", "进一步", "一图速览", "一图", "速览", "登泰山", "董事长", "总经理",
    "负责人", "业内人士", "专家", "业内", "机构", "数据显示", "据了解", "据悉",
    "此外", "另外", "同时", "目前", "近日", "日前", "当日", "近期", "接下来",
    "事实上", "实际上", "总体来看", "值得注意的是", "需要注意的是",
)


# 挖掘主体的分类线索
_REGION_NAMES = (
    "广东", "江苏", "浙江", "山东", "河南", "四川", "湖北", "湖南", "安徽",
    "福建", "河北", "陕西", "辽宁", "江西", "云南", "贵州", "广西", "海南",
    "北京", "上海", "天津", "重庆", "日本", "韩国", "美国", "德国", "法国",
    "英国", "俄罗斯", "印度", "越南", "泰国", "新加坡", "澳大利亚", "加拿大",
    "欧洲", "亚洲", "非洲", "拉美", "中东", "东南亚",
)

_ORG_TAIL_NAMES = (
    "统计局", "证监会", "央行", "银保监", "交易所", "协会", "研究院", "实验室",
    "管理局", "监管局", "发改委", "教育部", "工信部", "财政部", "商务部",
    "国资委", "能源局", "药监局", "民航局", "海关总署", "央行分行",
)


def _prefix_subject_kind(name: str) -> Optional[str]:
    """冒号前缀是不是主体？是的话返回类别，否则 None。

    "博雅生物：…" → "公司"（公司名）
    "广东：…"     → "地区"（省份）
    "统计局：…"   → "机构"（政府机构）
    "专家：…"     → None（说话人，不是主体）
    """
    if not name:
        return None
    if any(s == name or s in name for s in _SPEAKER_PREFIX):
        return None
    if _is_generic_name(name):
        return None
    if any(name.endswith(s) for s in _ORG_SUFFIX):
        return "机构"
    if name in _REGION_NAMES:
        return "地区"
    if any(h in name for h in _ORG_HINTS):
        return "公司"
    # 2-4 字的短名，可能是简称（如"华为""宁德"）
    if 2 <= len(name) <= 4:
        return "公司"
    return None


def _is_structured_name(name: str) -> bool:
    """结构化信号专用的主体名判定（比文本挖掘宽松）。

    为什么不能复用 _is_generic_name：
    GENERIC_CN 是给**文本挖掘**设计的，里面有"时代""概念""板块"这类词 ——
    在自由文本里它们多半是噪音（"概念股""时代精神"），
    但在数据源给的**规范命名**里它们是名字的一部分：
        时代万恒（THS 个股名）
        氟化工概念 / 锂电池概念（THS 概念标签）
    套用同一张表会把这些正规主体误杀。

    这里只排除真正不是主体的东西：媒体名、栏目标签、纯数字。
    """
    if not name:
        return True
    if _NUM_RE.search(name):
        return True
    for w in MEDIA_NAMES:
        if w in name:
            return True
    for w in _SPEAKER_PREFIX:
        if name == w or w in name:
            return True
    # 栏目标签类后缀："…收评""…资讯""…盘前"
    for w in ("收评", "午评", "早评", "复盘", "资讯", "要闻", "快讯",
              "盘前", "盘后", "速览", "一图", "汇总", "盘点", "榜单", "排行"):
        if name.endswith(w) or name == w:
            return True
    return False


def _is_generic_name(name: str) -> bool:
    """名称里含通用词/媒体名 → 是栏目或状态词，不是主体。

    用于过滤结构化信号："美股资讯""A股收评""经济日报"都不是主体。
    """
    if not name:
        return True
    if _NUM_RE.search(name):
        return True
    for w in GENERIC_CN:
        if w in name:
            return True
    for w in MEDIA_NAMES:
        if w in name:
            return True
    for w in _NOT_SUBJECT:
        if name == w or w in name:
            return True
    return False


def _is_noise_en(gram: str) -> bool:
    if _NUM_RE.search(gram):
        return True
    parts = gram.split()
    if len(parts) == 1:
        w = parts[0].lower()
        return w in GENERIC_EN or len(w) < 4
    return any(p.lower() in GENERIC_EN for p in parts)


def _block_words() -> List[str]:
    """需要从标题里"挖掉"的词：通用词 + 媒体名。

    用它们做**跨度屏蔽**：候选短语只要与这些词的字符跨度重叠就丢弃。
    这一条规则同时解决了跨词边界的碎片：
      "A股三大指数" → "大指"（与"指数"跨度重叠）→ 丢弃
      "数据集"      → "数集"（与"数据"跨度重叠）→ 丢弃
      "手机新浪网"  → "手机"（与媒体名跨度重叠）→ 丢弃
    比"黑名单精确匹配"有效得多，因为碎片无法穷举。
    """
    words = set(GENERIC_CN) | set(MEDIA_NAMES)
    words.discard("")
    return sorted(words, key=len, reverse=True)


_BLOCK_RE = re.compile("|".join(re.escape(w) for w in _block_words()))


def _block_spans(text: str) -> List[Tuple[int, int]]:
    """返回文本里所有"屏蔽词"的字符跨度。"""
    if not text:
        return []
    return [(m.start(), m.end()) for m in _BLOCK_RE.finditer(text)]


def _overlaps(span: Tuple[int, int], blocks: List[Tuple[int, int]]) -> bool:
    s, e = span
    for bs, be in blocks:
        if bs < e and s < be:
            return True
    return False


def _normalize_title(title: str) -> str:
    """去掉媒体署名与 hashtag，保留正文。"""
    t = str(title or "")
    for sep in (" - ", " _ ", "｜", " | ", "——", "—"):
        if sep in t:
            head, tail = t.rsplit(sep, 1)
            if tail.strip() and len(tail.strip()) <= 24:
                t = head
                break
    t = re.sub(r"[#＃][^\s#＃]{1,12}", " ", t)
    return t.strip()


def _candidates_cn(text: str, blocks: List[Tuple[int, int]]) -> List[str]:
    """中文候选短语：2~6 字 n-gram，且不与屏蔽词跨度重叠。"""
    out: List[str] = []
    for seg in _CN_RE.finditer(text):
        s, e = seg.start(), seg.end()
        run = seg.group(0)
        n = len(run)
        for L in (2, 3, 4, 5, 6):
            if n < L:
                break
            for k in range(n - L + 1):
                g = run[k:k + L]
                if _overlaps((s + k, s + k + L), blocks):
                    continue
                # 通用词/媒体名的屏蔽已由 _block_spans 的跨度重叠完成
                # （GENERIC_CN ⊂ 屏蔽词集合），此处再逐词扫一遍纯属重复：
                # 5.5 万候选 × 100 个通用词 = 550 万次子串检查 → 白花 5 秒。
                if _NUM_RE.search(g):
                    continue
                out.append(g)
    return out


def _candidates_en(text: str) -> List[str]:
    """英文候选短语：只取**相邻两词**组合。

    单个英文词噪声太大（"hack" / "below" / "prediction" 都是 50+ 次的假话题），
    两词组合才稳定（"binance hack" / "crude oil" / "bitcoin price"）。
    单个英文实体由核心词典覆盖（bitcoin/ethereum/intel…），不靠挖掘。
    """
    toks = [w.lower() for w in _EN_RE.findall(text)]
    out: List[str] = []
    for a, b in zip(toks, toks[1:]):
        if len(a) < 4 or len(b) < 4:
            continue
        if a in GENERIC_EN or b in GENERIC_EN:
            continue
        out.append(a + " " + b)
    return out


# ─────────────────────────────── 主体归并 ───────────────────────────────

# 挖掘出的短语 → 规范名。未命中核心词典的，用短语本身当主体名（保留英文原样）。
def _suppress_by_cover(cands: Dict[str, set]) -> List[str]:
    """基于文档覆盖度的子串抑制 —— 消除"同一个话题被切碎的多个片段"。

    碎片示例：特尔 / 英特 / 英特尔、季度 / 季报 / 三季报、必读 / 日必读 / 每日必读。
    它们不是噪声词，而是同一话题的**不同切片**，同时存在会把提及数翻好几倍。

    规则：短串 g 若存在更长候选 h（g ⊂ h），且 g 的文档中 ≥85% 也出现在 h 里，
    则 g 被 h 吸收 → 丢弃 g。

    实现要点：**包含关系不是前缀关系**（"特尔" ⊂ "英特尔"），也不能只按前 2 字索引
    （"特尔" 与 "英特尔" 首字不同）。改为**双索引**：
      · 前 2 字符桶
      · 后 2 字符桶（子串的结尾未必与母串结尾相同，但绝大多数中文实体满足）
      两个桶的并集作为比对范围，避免 O(n²) 全量比对。
    """
    if not cands:
        return []

    def _keys(g: str) -> List[str]:
        if g.isascii():
            return ["#" + g.split(" ", 1)[0][:2]]
        return [g[:2], g[-2:]]

    index: Dict[str, List[str]] = defaultdict(list)
    for g in cands:
        for k in _keys(g):
            index[k].append(g)

    drop: set = set()
    for g, gd in cands.items():
        if not gd:
            continue
        need = len(gd) * 0.85
        cands_h: set = set()
        for k in _keys(g):
            cands_h.update(index.get(k, ()))
        for h in cands_h:
            if h == g or len(h) <= len(g):
                continue
            if g not in h:            # 子串包含检查（C 层字符串搜索，很快）
                continue
            if len(cands[h] & gd) >= need:
                drop.add(g)
                break
    return [g for g in cands if g not in drop]


def _structured_entities(rows: Sequence[Any]) -> Tuple[Dict[str, str], List[set]]:
    """从原始条目的**结构化字段**里抽主体 —— 数据源直接给的，最准。

    与"文本挖掘"相比，结构化信号有三个优势：
      1. 零误报：#英特尔# 就是英特尔，不用靠词频猜
      2. 零碎片：不会出现"英特尔盘""沪指跌"这种切歪的片段
      3. 覆盖长尾：新概念（脑机接口、钠离子电池）当天出现就能进列表

    返回 (主体名 → 类别, 每条命中的主体集合)。
    第二个返回值按 rows 的**下标**对齐，调用方直接 zip 即可。
    """
    kinds: Dict[str, str] = {}
    per_item: List[set] = [set() for _ in rows]

    def _add(name: str, kind: str) -> None:
        name = str(name or "").strip()
        if not name or len(name) > 24:
            return
        # 结构化信号用宽松判定（见 _is_structured_name 的说明）
        if _is_structured_name(name):
            return
        kinds.setdefault(name, kind)
        per_item[i].add(name)

    for i, r in enumerate(rows):
        src_name = r["source"] or ""
        title = r["title"] or ""
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}

        if src_name == "ths_hot":
            # 个股名（如 "多氟多"）与概念标签（如 "人形机器人"）
            nm = str(meta.get("name") or "").strip()
            if nm:
                _add(nm, "个股")
            for cn in (meta.get("concepts") or []):
                _add(str(cn), "概念")
        elif src_name == "gnews":
            # 标题里的 #话题标签#：#英特尔# #美股资讯# → 前者是主体，后者是栏目标签
            for tag in _HASHTAG_RE.findall(title):
                tag = tag.strip()
                if 2 <= len(tag) <= 14:
                    _add(tag, "话题")
        elif src_name == "baidu_finance":
            # "博雅生物：前三季度整体采浆量518.9吨" → 公司名在冒号前
            # "专家：…"/"业内：…" 的前缀是说话人 → 排除
            # "广东：…"/"统计局：…" 是真主体，只是类别不同
            if "：" in title:
                nm = title.split("：", 1)[0].strip()
                if 2 <= len(nm) <= 10:
                    k = _prefix_subject_kind(nm)
                    if k:
                        _add(nm, k)
        elif src_name == "coingecko":
            nm = str(meta.get("name") or "").strip()
            if nm:
                _add(nm, "加密")

    return kinds, per_item


def _classify_mined(phrase: str) -> str:
    """给挖掘出的主体分类（卡片上的次要标签）。"""
    if any(phrase.endswith(t) for t in _ORG_TAIL_NAMES):
        return "机构"
    if phrase in _REGION_NAMES:
        return "地区"
    return "议题"


def _canon_of_phrase(phrase: str) -> Optional[Tuple[str, str]]:
    """短语命中核心词典 → (规范名, 类别)；否则 None。

    两个方向都要认：
      · phrase == 别名                → 归该主体
      · phrase 是别名的子串（"三季" ⊂ "三季报"）→ 归该主体
    否则 "三季" 会和 "三季报" 变成两个主体，一条讨论被重复计数。
    """
    low = phrase.lower()
    if low in _ALIAS_TO_CANON:
        return _ALIAS_TO_CANON[low]
    best: Optional[Tuple[str, str]] = None
    best_len = 0
    for alias, (canon, kind) in _ALIAS_TO_CANON.items():
        if len(alias) > best_len and alias in low:
            best = (canon, kind)
            best_len = len(alias)
    return best


def build_subject_map(conn: sqlite3.Connection, start_day: str, end_day: str,
                       min_docs: int = 3, max_subjects: int = 300,
                       rows: Optional[Sequence[Any]] = None,
                       structured: Optional[Tuple[Dict[str, str], List[set]]] = None,
                       ) -> Tuple[Dict[str, List[str]], List[set], set, set]:
    """构造 主体名 → 别名列表 的映射。

    来源：
      1. 核心词典（固定别名）
      2. **结构化信号**（同花顺概念/个股名、gnews 话题标签、百度财经公司名前缀）
      3. 窗口内自动挖掘的高频短语（数据驱动，门槛严格）

    rows / structured 由调用方传入：
      · rows 必须是**主循环正在用的那批行**（含 source/meta_json），
        否则候选集与主循环行序对不上，匹配会张冠李戴。
      · structured 是 _structured_entities(rows) 的结果。
    """
    subj: Dict[str, List[str]] = defaultdict(list)
    kinds: Dict[str, str] = {}

    for canon, aliases, kind in CORE_SUBJECTS:
        subj[canon].extend(aliases)
        kinds[canon] = kind

    if rows is None:
        rows = conn.execute(
            "SELECT id, source, title, body, meta_json FROM crowd_source_items "
            "WHERE day BETWEEN ? AND ? ORDER BY collected_at DESC, id DESC",
            (start_day, end_day)).fetchall()

    # ── 结构化主体（数据源直接给的，最准）──────────────────────────
    # 这些主体是**逐条数据自带**的信号（THS 概念标签、#话题标签#、
    # 公司名前缀），匹配时直接按行取即可，不必进正则全文扫描 ——
    # 500+ 条别名进正则会让匹配从 0.3s 涨到 1.5s。
    if structured is None:
        structured = _structured_entities(rows)
    struct_kinds, struct_per_item = structured
    for name, kind in struct_kinds.items():
        if name in subj:
            continue
        subj[name].append(name)
        kinds.setdefault(name, kind)

    titles = [r["title"] or "" for r in rows]

    # ── 自动挖掘 ───────────────────────────────────────────
    # 流程：逐条标题生成候选短语 → 收集文档集合 → 子串抑制（去碎片）
    #      → 按频次过滤 → 映射到主体。
    # 关键：不做 O(n²) 两两比较；子串关系用 trie 找（见 _suppress_by_cover）。
    cand_docs: Dict[str, set] = defaultdict(set)
    cand_srcs: Dict[str, set] = defaultdict(set)
    per_item_cands: List[set] = []
    for i, raw in enumerate(titles):
        t = _normalize_title(raw)
        if not t:
            per_item_cands.append(set())
            continue
        blocks = _block_spans(t)
        seen = set(_candidates_cn(t, blocks))
        seen.update(_candidates_en(t))
        per_item_cands.append(seen)
        for g in seen:
            cand_docs[g].add(i)
            try:
                cand_srcs[g].add(rows[i]["source"] or "")
            except (IndexError, KeyError, TypeError):
                pass

    # 频次过滤 → 覆盖度抑制 → 最终候选
    freq_ok = {g: d for g, d in cand_docs.items() if len(d) >= min_docs}
    kept = set(_suppress_by_cover(freq_ok))

    # ── 质量门槛（v319 修订）─────────────────────────────────────
    # 纯频次门槛会放进大量"市场状态词"（收官/预期/降幅/高开/通宵）：
    # 它们描述的是**行情在做什么**，不是**大家在聊什么**。
    # 三道闸：
    #   1. 中文挖掘短语至少 3 字（2 字短语几乎全是状态词；
    #      有价值的 2 字主体——黄金/白酒/半导体——已在核心词典里）
    #   2. 不含通用词
    #   3. 中文短语需跨 ≥2 个来源：单源词多半是某条新闻的碎片
    #      （英文短语天然单源，crypto 新闻都来自 gnews_crypto，故不适用）
    def _quality_ok(g: str) -> bool:
        if g in _ALIAS_TO_CANON:
            return True
        if g.isascii():
            return True
        if len(g) < 3:
            return False
        if _is_generic_name(g):
            return False
        return len(cand_srcs.get(g, ())) >= 2

    kept = {g for g in kept if _quality_ok(g)}

    # ── 主体数量控制 ────────────────────────────────────────
    # 挖掘阶段会产出几千个候选（多为长尾碎片），若全部建进匹配器，
    # 每条标题都要扫上千个别名 → 单条 4ms、4600 条 = 19 秒。
    # 这里按"全局讨论量"截断：只有被足够多条讨论提到的主体才进匹配器。
    # 长尾话题本来就不该出现在 Top-N 列表里，丢弃不影响结论。
    kept_sorted = sorted(kept, key=lambda g: -len(cand_docs.get(g, ())))
    kept_sorted = kept_sorted[:max_subjects]

    for phrase in kept_sorted:
        canon = _canon_of_phrase(phrase)
        if canon:
            name, kind = canon
            if phrase not in subj[name]:
                subj[name].append(phrase)
            kinds.setdefault(name, kind)
        else:
            if phrase not in subj:
                subj[phrase].append(phrase)
                kinds.setdefault(phrase, _classify_mined(phrase))

    # 别名去重 + 去掉与其它主体重复的别名（同一别名只归一个主体，避免重复计数）
    owner: Dict[str, str] = {}
    for name in subj:
        for a in subj[name]:
            owner.setdefault(a.lower(), name)
    for name in list(subj):
        uniq = []
        for a in subj[name]:
            if owner.get(a.lower()) == name and a not in uniq:
                uniq.append(a)
        subj[name] = uniq
    subj = {k: v for k, v in subj.items() if v}

    # 挖掘短语（用于候选集直查）= 那些既进了主体、又不是核心词典的别名
    mined_aliases = {a.lower() for name, aliases in subj.items()
                     for a in aliases
                     if a.lower() not in _DICT_ALIAS_SET}
    # 结构化主体的别名集合：它们按行直接命中，不进正则。
    # **例外**：若某个别名本来就在核心词典里（如 英特尔 / 黄金），
    # 必须保留在正则路径 —— 否则正文里的提及会全部丢失
    # （英特尔 114 → 10 就是这么丢的）。
    structured_aliases = {a.lower() for name in struct_kinds
                          for a in subj.get(name, ())
                          if a.lower() not in _DICT_ALIAS_SET}
    return subj, per_item_cands, mined_aliases, structured_aliases


class SubjectMatcher:
    """把「主体 → 别名」编译成可快速复用的匹配器。

    性能要点：三类别名必须走不同路径，否则必然是几十秒：
      · 词典别名（~150 个固定）→ **一个合并正则**，最快
      · 中文挖掘短语（可能上千）→ **首字符索引**：扫描标题时按字符取候选子集
      · 英文挖掘短语（两词组合）→ **首词索引**：按词取候选子集
    早期版本把上千个分支全塞进一个 alternation 正则，Python 会逐分支回溯，
    4600 条标题要 57 秒；改成分级索引后降到亚秒。

    英文别名一律按**词边界**匹配，否则 "coin" 会命中 "bitcoin"，
    造出一个 900+ 次的假主体。
    """

    def __init__(self, subj: Dict[str, List[str]], mined_aliases: Optional[set] = None,
                 exclude_aliases: Optional[set] = None):
        """exclude_aliases：不进正则、也不做全文扫描的别名集合。

        用于**结构化主体**（THS 个股名/概念、gnews 话题标签、百度财经公司名前缀）。
        它们是"这条数据自带的主体"，按行直接命中即可；
        若塞进正则，500+ 条别名会让全文匹配从 0.3s 涨到 1.5s。
        """
        self.alias_to_subject: Dict[str, str] = {}
        for name, aliases in subj.items():
            for a in aliases:
                al = a.lower()
                if al and al not in self.alias_to_subject:
                    self.alias_to_subject[al] = name
        self.excluded = {a.lower() for a in (exclude_aliases or ())}

        # ── 词典别名（固定、少量）→ 合并正则 ──
        # 同花顺概念也走这里（数量少）；挖掘出的短语走候选集直查。
        mined = mined_aliases if mined_aliases is not None else set()
        dict_aliases = [al for al in self.alias_to_subject
                        if al not in mined and al not in self.excluded]
        parts: List[str] = []
        for al in dict_aliases:
            if al.isascii():
                parts.append(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(al))
            else:
                parts.append(re.escape(al))
        parts.sort(key=len, reverse=True)
        self.regex = re.compile("|".join(parts), re.IGNORECASE) if parts else None

        # ── 挖掘短语 → 反向索引（短语 → 主体） ──
        self.mined_to_subject: Dict[str, str] = {}
        for al in self.alias_to_subject:
            if al in mined:
                self.mined_to_subject[al] = self.alias_to_subject[al]


        # ── 挖掘短语 → 首字符/首词索引 ──
        self.cn_index: Dict[str, List[str]] = defaultdict(list)
        self.en_index: Dict[str, List[str]] = defaultdict(list)
        for al in self.alias_to_subject:
            if al in _DICT_ALIAS_SET:
                continue
            if al.isascii():
                self.en_index[al.split(" ", 1)[0][:3]].append(al)
            else:
                self.cn_index[al[:2]].append(al)

    def match_dict(self, blob: str) -> List[str]:
        """词典别名匹配（合并正则，一次扫描）。"""
        if not blob or self.regex is None:
            return []
        out: List[str] = []
        seen: set = set()
        for m in self.regex.finditer(blob):
            name = self.alias_to_subject.get(m.group(0).lower())
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def match_mined(self, cands: set) -> List[str]:
        """挖掘短语匹配：候选集直查（不再扫全文）。

        挖掘阶段已经算出了该标题包含的候选短语，
        这里只需判断哪些候选短语被保留成了主体。
        """
        if not cands or not self.mined_to_subject:
            return []
        out: List[str] = []
        seen: set = set()
        for g in cands:
            name = self.mined_to_subject.get(g)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def match(self, blob: str) -> List[str]:
        if not blob:
            return []
        low = blob.lower()
        out: List[str] = []
        seen: set = set()

        def add(name: Optional[str]) -> None:
            if name and name not in seen:
                seen.add(name)
                out.append(name)

        if self.regex is not None:
            for m in self.regex.finditer(blob):
                add(self.alias_to_subject.get(m.group(0).lower()))

        # 中文挖掘短语：按前 2 字取候选子集（比单字索引桶少 5 倍）
        for i in range(len(low) - 1):
            bucket = self.cn_index.get(low[i:i + 2])
            if not bucket:
                continue
            for al in bucket:
                if al in low:
                    add(self.alias_to_subject.get(al))

        # 英文挖掘短语：按首词前 3 字母取候选子集
        for w in re.findall(r"[a-z][a-z0-9'&.\-]*", low):
            bucket = self.en_index.get(w[:3])
            if not bucket:
                continue
            for al in bucket:
                if al in low:
                    add(self.alias_to_subject.get(al))
        return out


def _match_subjects(blob: str, subj: Dict[str, List[str]],
                    kinds: Dict[str, str]) -> List[str]:
    """兼容旧签名（逐主体 find）。主路径请用 SubjectMatcher。"""
    if not blob:
        return []
    low = blob.lower()
    hit: List[str] = []
    for name, aliases in subj.items():
        for a in aliases:
            al = a.lower()
            if al.isascii():
                if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(al), low):
                    hit.append(name)
                    break
            elif al in low:
                hit.append(name)
                break
    return hit


# ─────────────────────────────── 主体聚合视图 ───────────────────────────────

def _subject_acc() -> Dict[str, Any]:
    return {
        "mentions": 0, "heat": 0.0, "bull": 0, "bear": 0, "neutral": 0,
        "platforms": set(), "sources": set(), "days": set(),
        "themes": Counter(), "last_seen": "", "items": [],
        "authoritative": False,
    }


# ─────────────────────────────── 缓存 ───────────────────────────────
# 主体抽取要把窗口内几千条原始条目过一遍（≈8 秒）。
# 但数据一天只变若干次（采集周期），没必要每次请求都重算。
# 缓存键 = (窗口, 数据指纹)。数据指纹用「窗口内条数 + 最大 id + 最大 collected_at」，
# 采集新增数据 → 指纹变化 → 缓存自动失效，不需要手工清缓存。
_CACHE: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL = 900.0   # 兜底过期（15 分钟）


def _data_fingerprint(conn: sqlite3.Connection, start_day: str,
                      end_day: str) -> str:
    """窗口内数据指纹：条数 + 最大 id + 最大采集时间。"""
    r = conn.execute(
        "SELECT COUNT(*) AS n, MAX(id) AS mx, MAX(collected_at) AS ts "
        "FROM crowd_source_items WHERE day BETWEEN ? AND ?",
        (start_day, end_day)).fetchone()
    return "%s|%s|%s" % (r["n"], r["mx"], r["ts"])


def subject_overview_cached(conn: sqlite3.Connection, month: str = "", days: int = 0,
                            limit: int = 40, sort: str = "heat",
                            item_limit: int = 5, kind: str = "",
                            min_mentions: int = 3) -> Dict[str, Any]:
    """带缓存的 subject_overview（API 走这个）。"""
    last_row = conn.execute(
        "SELECT MAX(day) AS d FROM crowd_source_items").fetchone()
    last_day = (last_row["d"] if last_row else "") or ""
    days = max(0, int(days or 0))
    if days > 0:
        end_day = last_day
        start_day = crowd_pulse._shift_day(end_day, -(days - 1))
    else:
        month = month or last_day[:7]
        start_day = month + "-01"
        end_day = month + "-31"
    fp = _data_fingerprint(conn, start_day, end_day)
    # 缓存键必须带上所有影响结果的请求参数：
    #   · min_mentions 决定哪些主体进列表
    #   · item_limit  决定每张卡保留几条原话
    # 少了任何一个，换个参数就会命中不匹配的旧结果。
    key = (start_day, end_day, fp, min_mentions, item_limit, kind)
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL:
        # 缓存命中：聚合结果复用，只按请求参数重新排序/截断
        return _resort(hit[1], limit=limit, sort=sort)

    data = subject_overview(conn, month=month, days=days, limit=limit,
                            sort=sort, item_limit=item_limit, kind=kind,
                            min_mentions=min_mentions)
    # 缓存**全量**卡片（不截断），否则换个排序方式后 Top-N 会漏掉主体
    full = dict(data)
    full["topics"] = list(data.get("topics") or [])
    _CACHE[key] = (now, full)
    # 冷路径同样要排序+截断：subject_overview 为了缓存全量已不做截断，
    # 直接返回会出现"首次请求 345 条、之后 40 条"的口径不一致。
    return _resort(full, limit=limit, sort=sort)


def _resort(data: Dict[str, Any], limit: int, sort: str) -> Dict[str, Any]:
    """对缓存里的聚合结果重新排序与截断（排序参数是请求级的）。"""
    cards = list(data.get("topics") or [])
    order = {
        "heat": lambda c: (-c["heat_total"], -c["mentions"], c["display_name"]),
        "authority": lambda c: (-c["authority_score"], -c["mentions"], c["display_name"]),
        "mentions": lambda c: (-c["mentions"], -c["source_count"], c["display_name"]),
        "latest": lambda c: (c["last_seen"], -c["mentions"]),
        "bullish": lambda c: (-c["sentiment"]["bull_ratio"], -c["mentions"]),
    }.get(str(sort or "heat").lower(),
         lambda c: (-c["mentions"], -c["source_count"], c["display_name"]))
    cards.sort(key=order)
    cards = cards[:max(1, int(limit))]
    for i, c in enumerate(cards, 1):
        c["rank_no"] = i
    out = dict(data)
    out["topics"] = cards
    out["count"] = len(cards)
    out["sort"] = str(sort or "heat")
    return out


def subject_overview(conn: sqlite3.Connection, month: str = "", days: int = 0,
                     limit: int = 40, sort: str = "heat", item_limit: int = 5,
                     kind: str = "", min_mentions: int = 3) -> Dict[str, Any]:
    """具体话题 × 观点视图 —— 「市场上的人具体在聊什么、持什么观点」。

    与 crowd_pulse.topic_overview 的分工：
      · topic_overview   → 题材大类（科技/AI、金融、消费…）
      · subject_overview → 具体主体（英特尔、黄金、美国中期选举、Binance…）
    这里每张卡都是**一个具体议题**，题材/市场只是卡片上的次要标签。

    口径：
      · 每条原始条目（crowd_source_items）逐条匹配主体 → 逐条计多空
      · 平台数 = 提到该主体的**独立平台**数（跨源共识）
      · 热度 = 源给出的热度求和（与既有 heat 口径一致）
    """
    last_row = conn.execute(
        "SELECT MAX(day) AS d FROM crowd_source_items").fetchone()
    last_day = (last_row["d"] if last_row else "") or ""
    if not last_day:
        return {"month": month, "window": {}, "count": 0, "topics": [],
                "summary": {}, "note": "采集尚未产出任何原始条目"}

    days = max(0, int(days or 0))
    if days > 0:
        end_day = last_day
        start_day = crowd_pulse._shift_day(end_day, -(days - 1))
    else:
        month = month or last_day[:7]
        start_day = month + "-01"
        end_day = month + "-31"

    rows = conn.execute(
        """SELECT id, source, title, body, url, media_channel, heat,
                  published_at, collected_at, day, meta_json
             FROM crowd_source_items
            WHERE day BETWEEN ? AND ?
            ORDER BY collected_at DESC, id DESC""",
        (start_day, end_day)).fetchall()
    if not rows:
        return {"month": month,
                "window": {"start": start_day, "end": end_day, "days": days},
                "count": 0, "topics": [], "summary": {},
                "note": "该窗口内没有原始条目"}

    # 结构化主体先算：既作为主体来源，也作为每条数据的直接命中
    structured = _structured_entities(rows)
    struct_kinds, struct_per_item = structured

    # 权威主体 = 核心词典 ∪ 结构化信号。
    # 它们是"数据源确认过的主体"，不该按讨论频次（min_mentions）被砍掉：
    # THS 热榜里只上榜 1 次的个股也是有效主体。
    authoritative = {c for c, _a, _k in CORE_SUBJECTS} | set(struct_kinds)

    # rows 传进去 → 候选集与主循环严格同序同条（修行序错位）
    subj, per_item_cands, mined_aliases, structured_aliases = build_subject_map(
        conn, start_day, end_day, min_docs=3, rows=rows, structured=structured)
    kinds: Dict[str, str] = {}
    for canon, _aliases, k in CORE_SUBJECTS:
        kinds[canon] = k
    for name, k in struct_kinds.items():
        kinds.setdefault(name, k)
    for name in subj:
        kinds.setdefault(name, "议题")

    matcher = SubjectMatcher(subj, mined_aliases=mined_aliases,
                             exclude_aliases=structured_aliases)
    agg: Dict[str, Dict[str, Any]] = defaultdict(_subject_acc)
    max_items = max(0, int(item_limit or 0))

    for idx, r in enumerate(rows):
        title = r["title"] or ""
        body = r["body"] or ""
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        # 归一化标题：去掉媒体署名与 hashtag，避免把"新浪财经"当成话题
        norm_title = _normalize_title(title)
        blob = "%s %s %s" % (norm_title, body,
                             " ".join(str(x) for x in (meta.get("concepts") or [])))
        # 三路命中：
        #   1. 词典别名 → 合并正则
        #   2. 结构化信号 → 该条数据自带的主体（THS 概念/个股、话题标签、公司名前缀）
        #   3. 挖掘短语 → 候选集直查（不再扫全文）
        hits = matcher.match_dict(blob)
        for name in (struct_per_item[idx] if idx < len(struct_per_item) else ()):
            if name not in hits:
                hits.append(name)
        cands = per_item_cands[idx] if idx < len(per_item_cands) else ()
        if cands:
            for name in matcher.match_mined(cands):
                if name not in hits:
                    hits.append(name)
        if not hits:
            continue
        # 观点：与 crowd_pulse.normalize_item 同口径（标题关键词判定）
        bull = sum(1 for w in crowd_pulse._BULL_WORDS if w in blob)
        bear = sum(1 for w in crowd_pulse._BEAR_WORDS if w in blob)
        if bull > bear:
            sent = crowd_pulse.SENTIMENT_BULL
        elif bear > bull:
            sent = crowd_pulse.SENTIMENT_BEAR
        else:
            sent = crowd_pulse.SENTIMENT_NEUTRAL
        themes = crowd_pulse._themes_of(title, body, meta)
        platform = crowd_pulse.platform_of(r["source"])
        item = {
            "title": title, "url": r["url"] or "",
            "source": r["source"] or "", "media_channel": r["media_channel"] or "",
            "heat": float(r["heat"] or 0.0),
            "published_at": r["published_at"] or "",
            "collected_at": r["collected_at"] or "",
        }
        for h in hits:
            a = agg[h]
            a["mentions"] += 1
            a["heat"] += float(r["heat"] or 0.0)
            if sent == crowd_pulse.SENTIMENT_BULL:
                a["bull"] += 1
            elif sent == crowd_pulse.SENTIMENT_BEAR:
                a["bear"] += 1
            else:
                a["neutral"] += 1
            a["platforms"].add(platform)
            a["sources"].add(r["source"] or "")
            a["days"].add(r["day"] or "")
            for th in themes:
                a["themes"][th] += 1
            if h in authoritative:
                a["authoritative"] = True
            if (r["collected_at"] or "") > a["last_seen"]:
                a["last_seen"] = r["collected_at"] or ""
            if max_items and len(a["items"]) < max_items:
                a["items"].append(item)

    cards: List[Dict[str, Any]] = []
    for name, a in agg.items():
        # 质量门槛：只被提到 1~2 次的话题没有"市场关注度"可言，
        # 放进来只会污染列表（sort=latest/bullish 时尤其明显）。
        # 例外：**权威主体**（核心词典命中 / 数据源结构化信号）豁免 ——
        # THS 热榜里只上榜 1 次的个股、只被 #一次# 的话题标签，
        # 也是数据源确认过的主体，不该按讨论频次被砍掉。
        if a["mentions"] < min_mentions and not a["authoritative"]:
            continue
        stance, bull_ratio, bear_ratio = crowd_pulse._stance_of(
            a["bull"], a["bear"], a["neutral"])
        total = a["bull"] + a["bear"] + a["neutral"]
        platforms = sorted(p for p in a["platforms"] if p)
        cards.append({
            "topic_key": "subject:%s" % name,
            "display_name": name,
            "topic_kind": kinds.get(name, "议题"),
            "heat_total": round(a["heat"], 4),
            "authority_score": round(
                crowd_pulse.authority_score(
                    a["heat"], len(platforms), len(a["days"]), 0.0, 0.5), 2),
            "source_count": len(platforms),
            "days_active": len(a["days"]),
            "mentions": a["mentions"],
            "momentum": 0.0,
            "media_weight": 0.5,
            "last_seen": a["last_seen"],
            "platforms": platforms,
            "sources": sorted(s for s in a["sources"] if s),
            "platform_audiences": [crowd_pulse.PLATFORM_AUDIENCE.get(p, p)
                                   for p in platforms],
            "markets": [],
            "asset_classes": [],
            "themes": [t for t, _ in a["themes"].most_common(3)],
            "explain": "被 %d 个平台、%d 条讨论提到" % (len(platforms), a["mentions"]),
            "sentiment": {
                "bull": a["bull"], "bear": a["bear"], "neutral": a["neutral"],
                "total": total, "stance": stance,
                "bull_ratio": round(bull_ratio, 4),
                "bear_ratio": round(bear_ratio, 4),
                "net": a["bull"] - a["bear"],
            },
            "items": a["items"],
            "item_total": a["mentions"],
        })

    order = {
        "heat": lambda c: (-c["heat_total"], -c["mentions"], c["display_name"]),
        "authority": lambda c: (-c["authority_score"], -c["mentions"], c["display_name"]),
        "mentions": lambda c: (-c["mentions"], -c["source_count"], c["display_name"]),
        "latest": lambda c: (c["last_seen"], -c["mentions"]),
        "bullish": lambda c: (-c["sentiment"]["bull_ratio"], -c["mentions"]),
    }.get(str(sort or "heat").lower(),
         lambda c: (-c["mentions"], -c["source_count"], c["display_name"]))
    cards.sort(key=order)
    # 不在此处截断：缓存需要全量卡片（不同 sort 的 Top-N 不同），
    # 截断统一交给 subject_overview_cached._resort()
    for i, c in enumerate(cards, 1):
        c["rank_no"] = i

    # 窗口汇总：对事实行直接计数（不用 top-N 反推，避免重复放大）
    plat_cnt: Counter = Counter()
    theme_cnt: Counter = Counter()
    tot_bull = tot_bear = tot_neutral = 0
    for r in rows:
        plat_cnt[crowd_pulse.platform_of(r["source"])] += 1
    for c in cards:
        tot_bull += c["sentiment"]["bull"]
        tot_bear += c["sentiment"]["bear"]
        tot_neutral += c["sentiment"]["neutral"]
        for t in c["themes"]:
            theme_cnt[t] += c["sentiment"]["total"]
    stance, bull_ratio, bear_ratio = crowd_pulse._stance_of(
        tot_bull, tot_bear, tot_neutral)

    return {
        "month": month or last_day[:7],
        "window": {
            "start": start_day, "end": end_day, "days": days,
            "label": ("近 %d 天（%s ~ %s）" % (days, start_day, end_day))
                     if days > 0 else ("%s 全月" % (month or last_day[:7])),
        },
        "sort": str(sort or "heat"),
        "kind": kind or "subject",
        "count": len(cards),
        "topics": cards,
        "summary": {
            "mentions": sum(c["mentions"] for c in cards),
            "topic_count": len(cards),
            "platform_count": len(plat_cnt),
            "platforms": sorted(plat_cnt),
            "sentiment": {"bull": tot_bull, "bear": tot_bear, "neutral": tot_neutral,
                          "total": tot_bull + tot_bear + tot_neutral,
                          "stance": stance,
                          "bull_ratio": round(bull_ratio, 4),
                          "bear_ratio": round(bear_ratio, 4),
                          "net": tot_bull - tot_bear},
            "top_markets": [],
            "top_themes": [{"name": t, "mentions": n}
                           for t, n in theme_cnt.most_common(6)],
            "platform_mentions": [
                {"platform": p, "audience": crowd_pulse.PLATFORM_AUDIENCE.get(p, p),
                 "mentions": n}
                for p, n in plat_cnt.most_common()
            ],
        },
        "note": ("具体话题由标题/正文抽取：核心词典 + 同花顺概念 + 窗口内高频短语挖掘；"
                 "观点为标题关键词判定（看多词 %d / 看空词 %d），不是语义模型。"
                 % (len(crowd_pulse._BULL_WORDS), len(crowd_pulse._BEAR_WORDS))),
    }
