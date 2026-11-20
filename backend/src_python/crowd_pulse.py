"""Crowd Pulse — 人群画像 / 权威度 / 月榜聚合层。

数据流
------
    crowd_sources.collect()   →  list[RawItem]（真实抓取）
            ↓  normalize_item()      归一化：资产类别 / 题材 / 市场 / 情绪 / 时段 / 影响力
            ↓  topic_key()           跨源话题归并（同一话题在不同源 → 同一 topic_key）
            ↓  persist_*()           落库（原始条目 + 每日快照 + 话题 + 画像 + 月榜）
            ↓  monthly_ranking() / audience_profile()   读榜

权威度（authority_score）如何算
------------------------------
所有输入都来自真实采集的计数，**不掺任何主观打分**：

    heat_total        热度总量（对数压缩，避免单个爆款霸榜）
    source_count      被多少个独立源同时提到（跨源交叉验证 —— 权威度的核心）
    days_active       当月出现在多少天（持续性）
    momentum          相对上一周期的热度变化（动能，新起话题加权）
    media_weight      提及媒体的历史权重（出现频次 × 跨源复现率）

月榜按 authority_score 降序；同时给出 authority_explain（人可读的解释），
确保任何一条排名都能被追问"凭什么"。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# ─────────────────────────────── 题材词典 ───────────────────────────────
# 命中即打标；一条可命中多个题材（多头关注是真实特征，不做互斥裁剪）。
THEME_RULES: List[Tuple[str, List[str]]] = [
    ("科技/AI", ["人工智能", "AI", "大模型", "算力", "英伟达", "GPU", "CPO", "光模块",
                 "智能体", "机器人", "自动驾驶", "智驾", "大厂", "科技股", "半导体",
                 "芯片", "存储", "光刻", "台积电", "算力租赁"]),
    ("新能源", ["新能源", "光伏", "锂电", "储能", "风电", "氢能", "固态电池", "充电桩",
                 "宁德时代", "比亚迪", "碳酸锂", "逆变器", "特高压"]),
    ("医药健康", ["医药", "创新药", "疫苗", "医疗", "集采", "CXO", "减肥药", "医保",
                 "生物医药", "中药", "器械"]),
    ("军工国防", ["军工", "国防", "航天", "导弹", "军贸", "航空发动机", "低空经济"]),
    ("地产基建", ["房地产", "楼市", "地产", "房价", "房企", "土地", "基建", "水泥",
                  "工程机械", "城中村"]),
    ("消费", ["消费", "白酒", "食品", "饮料", "零售", "免税", "旅游", "家电", "餐饮",
              "美妆", "育儿", "谷子经济"]),
    ("金融", ["银行", "券商", "保险", "基金", "信托", "金融", "理财", "保险资管"]),
    ("资源周期", ["黄金", "白银", "铜", "铝", "稀土", "煤炭", "石油", "原油", "有色",
                  "锂矿", "铁矿", "水泥", "黄金股", "油气"]),
    ("宏观政策", ["美联储", "降息", "加息", "通胀", "CPI", "PPI", "GDP", "关税", "汇率",
                  "人民币", "央行", "国债", "美联储主席", "鲍威尔", "非农", "刺激",
                  "政策", "会议", "峰会"]),
    ("汽车出行", ["汽车", "新能源车", "车企", "比亚迪", "特斯拉", "充电", "飞行汽车",
                  "商业航天"]),
    ("消费电子", ["消费电子", "手机", "苹果", "折叠屏", "PCB", "面板", "耳机"]),
    ("游戏传媒", ["游戏", "版号", "影视", "票房", "短剧", "动漫"]),
]

# ─────────────────────────────── 资产类别词典 ───────────────────────────────
ASSET_CLASS_RULES: List[Tuple[str, List[str]]] = [
    ("贵金属", ["黄金", "白银", "金价", "银价", "XAU", "XAG", "贵金属"]),
    ("原油能源", ["原油", "石油", "油价", "WTI", "布伦特", "天然气", "炼化", "煤炭"]),
    ("加密资产", ["比特币", "以太坊", "加密", "数字货币", "BTC", "ETH", "SOL",
                  "稳定币", "DeFi", "NFT"]),
    ("A股", ["沪", "深", "创业板", "科创板", "北交所", "A股", "涨停", "跌停",
             "龙虎榜", "沪深", "股东户数", "个股", "A股三大指数"]),
    ("港股", ["港股", "恒生", "香港", "H股"]),
    ("美股", ["纳斯达克", "道琼斯", "标普", "美股", "华尔街", "英伟达", "苹果",
              "特斯拉", "微软", "亚马逊", "谷歌"]),
    ("指数基金", ["沪深300", "上证", "深证", "创业板指", "恒生指数", "标普500",
                  "纳指", "ETF", "指数"]),
    ("公司动态", ["股东户数", "营收", "净利润", "财报", "减持", "增持", "回购",
                  "分红", "业绩", "中标", "订单", "定增", "并购", "IPO", "退市"]),
    ("板块概念", ["板块", "概念", "龙头", "题材", "产业链"]),
    ("宏观变量", ["GDP", "CPI", "PPI", "PMI", "社融", "信贷", "进出口", "外贸",
                  "失业率", "非农", "国债收益率", "汇率"]),
]

# ─────────────────────────────── 情绪词典 ───────────────────────────────
_BULL_WORDS = ["涨停", "大涨", "暴涨", "飙升", "新高", "反弹", "利好", "上涨", "拉升",
               "翻倍", "涨停潮", "领涨", "突破", "看好", "买入", "加仓", "牛市",
               "强势", "领涨股", "涨停板"]
_BEAR_WORDS = ["跌停", "大跌", "暴跌", "崩盘", "新低", "利空", "下跌", "跳水", "跌超",
               "腰斩", "清仓", "抛售", "熊市", "弱势", "闪崩", "爆雷", "退市", "停产"]

# ─────────────────────────────── 平台 → 人群定位 ───────────────────────────────
# 这是"人群"的第一层切分：不同平台天然聚集不同画像的人。
# 注意区分「平台」与「榜单」：百度实时榜和百度财经榜是**同一个平台**，
# 交叉验证只能算 1 次（否则同一家平台的两张榜会被误当成两个独立信源）。
SOURCE_PLATFORM: Dict[str, str] = {
    "baidu_hot": "baidu",
    "baidu_finance": "baidu",
    "ths_hot": "ths",
    "coingecko": "coingecko",
    "gnews": "gnews",
    # v317: 同一服务不同语料池 → 独立平台（否则与 gnews 形成虚假跨源共识）
    "gnews_crypto": "gnews_crypto",
}

PLATFORM_AUDIENCE: Dict[str, str] = {
    "baidu": "大众财经网民",
    "ths": "A股散户",
    "coingecko": "加密原生社区",
    "gnews": "财经媒体/机构",
    "gnews_crypto": "英文加密/商品媒体",
}


def platform_of(source: str) -> str:
    return SOURCE_PLATFORM.get(str(source or ""), str(source or "unknown"))

# 媒体渠道 → 专业度分层（用于媒体权威度）
_TIER_A = ["彭博", "路透", "Reuters", "Bloomberg", "华尔街日报", "WSJ", "金融时报",
           "FT", "纽约时报", "New York Times", "CNBC", "第一财经", "一财", "证券时报",
           "中国证券报", "上海证券报", "财联社", "新华", "人民日报", "经济日报"]
_TIER_B = ["Investing.com", "英为财情", "格隆汇", "东方财富", "同花顺", "新浪财经",
           "财网", "和讯", "雪球", "金十", "华尔街见闻", "远川", "晚点", "36氪"]


# 数据源直接给出的市场码 → 统一市场标签（避免 market 维度碎成 深A/沪A/创业板…）
SOURCE_MARKET_MAP: Dict[str, str] = {
    "深A": "A股", "沪A": "A股", "创业板": "A股", "科创板": "A股", "北交所": "A股",
    "沪B": "A股", "深B": "A股", "转债": "A股", "基金": "指数基金",
    "港股": "港股", "美股": "美股",
}


def media_tier(channel: str) -> str:
    """媒体权威分层：tier1 主流财经媒体 / tier2 垂直门户 / tier3 其他。"""
    c = str(channel or "")
    for kw in _TIER_A:
        if kw in c:
            return "tier1_主流财经媒体"
    for kw in _TIER_B:
        if kw in c:
            return "tier2_垂直门户"
    return "tier3_其他来源"


# ─────────────────────────────── 归一化 ───────────────────────────────

# 规则关键词的小写化缓存：原来每条 item 都会对 133 个关键词各调一次 .lower()
# （一次 4500 条的反查 = 60 万次 lower，profile 里占了 1s+）。
# 规则表是模块级常量，缓存命中后不再重复计算，语义完全不变。
_LOWER_RULES_CACHE: Dict[int, List[Tuple[str, List[str]]]] = {}


def _match_rules(text: str, rules: List[Tuple[str, List[str]]],
                 only: Optional[Set[str]] = None) -> List[str]:
    """命中所有规则标签（不做互斥裁剪 —— 多头关注本身就是真实特征）。

    only: 只关心其中若干标签时传进来（跳过其余规则，省掉无谓的字符串扫描）。
    """
    if not text:
        return []
    low = text.lower()
    key = id(rules)
    lowered = _LOWER_RULES_CACHE.get(key)
    if lowered is None:
        lowered = [(label, [k.lower() for k in kws]) for label, kws in rules]
        _LOWER_RULES_CACHE[key] = lowered
    hits: List[str] = []
    for label, kws in lowered:
        if only is not None and label not in only:
            continue
        for kw in kws:
            if kw in low:
                hits.append(label)
                break
    return hits


def _time_window(ts: str) -> str:
    """按 A 股交易时段切分（采集时刻为本地时间）。"""
    try:
        hour = int(str(ts)[11:13])
    except (ValueError, IndexError):
        return "未知时段"
    if 6 <= hour < 9:
        return "盘前(06-09)"
    if 9 <= hour < 11:
        return "早盘(09-11:30)"
    if 11 <= hour < 13:
        return "午盘(11:30-13:00)"
    if 13 <= hour < 15:
        return "尾盘(13-15)"
    if 15 <= hour < 18:
        return "盘后(15-18)"
    return "夜间(18-06)"


def _blob_of(title: str, body: str, meta: Dict[str, Any]) -> str:
    """参与题材/资产类别匹配的文本（标题 + 正文 + 概念标签）。"""
    return "%s %s %s" % (title, body,
                         " ".join(str(c) for c in (meta.get("concepts") or [])))


def _themes_of(title: str, body: str, meta: Dict[str, Any],
               only: Optional[Set[str]] = None) -> List[str]:
    """标题+正文+概念 → 命中的题材列表（normalize_item 与"原话反查"共用同一口径）。"""
    return _match_rules(_blob_of(title, body, meta), THEME_RULES, only=only)


def normalize_item(item: Any, heat_pct: float = 0.0) -> Dict[str, Any]:
    """RawItem → 归一化画像标签。

    返回 dict：platform / asset_class / themes / market / sentiment /
              time_window / influence_tier / media_channel / media_tier
    """
    title = str(getattr(item, "title", "") or "")
    body = str(getattr(item, "body", "") or "")
    meta = dict(getattr(item, "meta", {}) or {})
    source = str(getattr(item, "source", "") or "")
    author = str(getattr(item, "author", "") or "")
    blob = "%s %s %s" % (title, body, " ".join(str(c) for c in (meta.get("concepts") or [])))

    asset_classes = _match_rules(blob, ASSET_CLASS_RULES)
    themes = _match_rules(blob, THEME_RULES)

    # 市场判定：优先用数据源直接给的 market 码（同花顺 market=33→深A），
    # 文本关键词只是兜底 —— 否则 market 维度会碎成 深A/沪A/创业板/科创板…
    src_market = str(meta.get("market") or "")
    market = SOURCE_MARKET_MAP.get(src_market, "")
    if not market and src_market and not src_market.isdigit():
        market = src_market  # 源直接给的市场名（非数字），可用
    if market:
        if market not in asset_classes:
            asset_classes = [market] + asset_classes
    else:
        market = "其他"
        for mkt in ("A股", "港股", "美股", "加密资产", "原油能源", "贵金属",
                    "指数基金"):
            if mkt in asset_classes:
                market = mkt
                break
        if market == "其他":
            # 无明确市场时，用题材兜底成「题材:<x>」，比笼统的「其他」有信息量；
            # 仍然无从判断才留「泛财经/跨市场」。
            market = ("题材:%s" % themes[0]) if themes else "泛财经/跨市场"
    if not asset_classes:
        asset_classes = [market]

    bull = sum(1 for w in _BULL_WORDS if w in blob)
    bear = sum(1 for w in _BEAR_WORDS if w in blob)
    if bull > bear:
        sentiment = "看多"
    elif bear > bull:
        sentiment = "看空"
    else:
        sentiment = "中性/无方向"

    heat_pct = max(0.0, min(1.0, float(heat_pct or 0.0)))
    if heat_pct >= 0.9:
        influence = "头部关注(top10%)"
    elif heat_pct >= 0.6:
        influence = "中上关注"
    elif heat_pct >= 0.3:
        influence = "中段关注"
    else:
        influence = "长尾关注"

    return {
        "platform": PLATFORM_AUDIENCE.get(platform_of(source), source or "未知平台"),
        "source": source,
        "asset_class": asset_classes[0] if asset_classes else "未归类",
        "asset_classes": asset_classes,
        "themes": themes,
        "market": market,
        "sentiment": sentiment,
        "time_window": _time_window(str(getattr(item, "collected_at", "") or "")),
        "influence_tier": influence,
        "media_channel": author or PLATFORM_AUDIENCE.get(platform_of(source), "未标注"),
        "media_tier": media_tier(author) if author else "平台自有",
    }


# ─────────────────────────────── 话题归并 ───────────────────────────────

_PUNCT = re.compile(r"[\s,，。．、!！?？:：;；'\"“”‘’()（）\[\]【】<>《》/\\|~`@#\$%\^&\*\+=_\-]+")


def topic_key_for(item: Any, tags: Dict[str, Any]) -> str:
    """跨源话题归并键。

    标的类（同花顺个股 / CoinGecko 币种）→ 用代码/符号做键，天然跨源一致；
    话题类（百度热搜词 / 新闻标题）→ 用归一化后的标题文本做键。
    同花顺的概念标签（人形机器人 / CPO / 军工…）在 meta.concepts 里，
    它们是"人群在讨论什么"的最强信号，单列为概念话题。
    """
    meta = dict(getattr(item, "meta", {}) or {})
    source = str(getattr(item, "source", "") or "")
    title = str(getattr(item, "title", "") or "")

    if source == "ths_hot":
        code = str(meta.get("code") or "")
        if code:
            return "asset:%s:%s" % (meta.get("market") or "", code)
    if source == "coingecko":
        sym = str(meta.get("symbol") or "")
        if sym:
            return "asset:CRYPTO:%s" % sym

    concepts = meta.get("concepts") or []
    if concepts:
        return "concept:%s" % _PUNCT.sub("", str(concepts[0]))

    norm = _PUNCT.sub("", title).lower()
    return "text:%s" % norm[:64]


# ─────────────────────────────── 权威度 ───────────────────────────────

# 权威度权重（可解释、可审计；不是拍脑袋的分数，而是各项真实计数的映射）
W_HEAT = 8.0        # log1p(热度总量)
W_CROSS = 10.0      # 跨源共现（每多一个独立源提到，权威度显著上升）
W_DAYS = 2.0        # 持续出现天数
W_MOMENTUM = 6.0    # 动能（相对上一周期热度变化）
W_MEDIA = 4.0       # 媒体权重均值

MEDIA_TIER_WEIGHT = {
    "tier1_主流财经媒体": 1.0,
    "tier2_垂直门户": 0.7,
    "tier3_其他来源": 0.4,
    "平台自有": 0.5,
}


def authority_score(heat_total: float, source_count: int, days_active: int,
                    momentum: float, media_weight: float) -> float:
    """权威度 = 各真实计数的加权和（对数压缩热度 + 跨源交叉验证为主）。"""
    score = (W_HEAT * math.log1p(max(0.0, float(heat_total)))
             + W_CROSS * max(0, int(source_count))
             + W_DAYS * max(0, int(days_active))
             + W_MOMENTUM * float(momentum)
             + W_MEDIA * float(media_weight))
    return round(score, 4)


def authority_explain(heat_total: float, source_count: int, days_active: int,
                      momentum: float, media_weight: float,
                      sources: Iterable[str]) -> str:
    """人可读的权威度解释 —— 每条排名都能被追问「凭什么」。

    source_count 是**独立平台数**（不是榜单数）：百度实时榜与百度财经榜同属
    百度平台，交叉验证只能算一次，否则同一家平台的两张榜会被当成两个信源。
    """
    return ("热度总量 %.1f（log 压缩后 %.2f）×%s + 跨平台共现 %d 个平台[%s] ×%s + "
            "持续 %d 天 ×%s + 动能 %+.2f ×%s + 媒体权重均值 %.2f ×%s"
            % (heat_total, math.log1p(max(0.0, heat_total)), W_HEAT,
               source_count, ",".join(sorted(sources))[:60], W_CROSS,
               days_active, W_DAYS, momentum, W_MOMENTUM,
               media_weight, W_MEDIA))


# ─────────────────────────────── 落库 ───────────────────────────────

def _month_of(ts: str) -> str:
    s = str(ts or "")
    return s[:7] if len(s) >= 7 else ""


def _day_of(ts: str) -> str:
    s = str(ts or "")
    return s[:10] if len(s) >= 10 else ""


def _normalize_heat_by_source(items: List[Any]) -> Dict[int, float]:
    """各源内部按「名次」归一热度，返回 id(item) → 可比热度。

    为什么必须做（2026-10-30 实测）：
        同花顺 rate 是 50 万级、百度只有 0~1 的 rank 代理、Google News 根本没有
        热度值（恒 0）。量纲不统一直接相加，会让榜单退化成「同花顺热度榜」——
        实测 platform 维度的 share 会变成 ths_hot=1.000 / 其余=0.000，
        跨源交叉验证（权威度的核心）彻底失效。
    这里统一用「名次倒数」1/rank 作为可比热度：
        有 rank_no 的源（百度 index / 同花顺 order）用 rank_no；
        没有的源（Google News RSS 顺序即热度序、CoinGecko）用出现顺序。
    原始热度值**不丢** —— 仍写进 crowd_source_items.heat，可回溯核对。
    """
    out: Dict[int, float] = {}
    by_source: Dict[str, List[Any]] = defaultdict(list)
    for it in items:
        by_source[str(getattr(it, "source", "") or "")].append(it)
    for _src, group in by_source.items():
        # 排序依据：有 rank 的按 rank 升序；无 rank 的按原始热度降序（都为 0 时保持
        # 原始出现顺序 —— Google News RSS 的顺序本身就是热度序）。
        ordered = sorted(
            enumerate(group),
            key=lambda pair: (
                int(getattr(pair[1], "rank_no", 0) or 0)
                if int(getattr(pair[1], "rank_no", 0) or 0) > 0 else 10 ** 6,
                -float(getattr(pair[1], "heat", 0.0) or 0.0),
                pair[0],
            ),
        )
        for pos, (_orig_idx, it) in enumerate(ordered):
            rank = int(getattr(it, "rank_no", 0) or 0)
            effective = rank if rank > 0 else (pos + 1)
            out[id(it)] = round(1.0 / max(1, effective), 6)
    return out


def persist_items(conn: sqlite3.Connection, items: List[Any]) -> Dict[str, int]:
    """把 RawItem 写入 crowd_source_items + crowd_topic_daily。

    - crowd_source_items：原始条目（source+external_id 唯一，重复采集只更新不重复插入）
    - crowd_topic_daily：话题×日×源 的聚合快照（榜单的时间序列底座）
      这里的 heat 存的是**源内归一后的可比热度**（见 _normalize_heat_by_source）。
    """
    n_items = 0
    n_daily = 0
    heat_norm_map = _normalize_heat_by_source(items)
    for it in items:
        it.heat_norm = heat_norm_map.get(id(it), 0.0)
    # 影响力分层按**源内**分位算（跨源热度不可比）
    pct_by_source: Dict[str, List[float]] = defaultdict(list)
    for it in items:
        pct_by_source[str(getattr(it, "source", "") or "")].append(it.heat_norm)
    src_sorted = {s: sorted(v, reverse=True) for s, v in pct_by_source.items()}
    src_total = {s: max(1, len(v)) for s, v in pct_by_source.items()}

    for it in items:
        source = str(getattr(it, "source", "") or "")
        ext = str(getattr(it, "external_id", "") or "")
        if not source or not ext:
            continue
        collected = str(getattr(it, "collected_at", "") or "")
        day = _day_of(collected)
        month = _month_of(collected)
        if not day:
            continue
        arr = src_sorted.get(source, [])
        pos = arr.index(it.heat_norm) if it.heat_norm in arr else 0
        pct = 1.0 - (pos / src_total.get(source, 1))
        tags = normalize_item(it, heat_pct=pct)
        tkey = topic_key_for(it, tags)
        meta = dict(getattr(it, "meta", {}) or {})
        conn.execute(
            """INSERT INTO crowd_source_items
               (source, external_id, item_kind, title, body, url, media_channel,
                heat, rank_no, published_at, collected_at, day, month, topic_key,
                meta_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source, external_id) DO UPDATE SET
                 heat=excluded.heat, rank_no=excluded.rank_no,
                 collected_at=excluded.collected_at, topic_key=excluded.topic_key""",
            (source, ext, str(getattr(it, "kind", "") or ""),
             str(getattr(it, "title", "") or ""), str(getattr(it, "body", "") or ""),
             str(getattr(it, "url", "") or ""), tags["media_channel"],
             float(getattr(it, "heat", 0.0) or 0.0), int(getattr(it, "rank_no", 0) or 0),
             str(getattr(it, "published_at", "") or ""), collected, day, month, tkey,
             json.dumps(meta, ensure_ascii=False, default=str)),
        )
        n_items += 1
        kind = str(getattr(it, "kind", "") or "")
        tkind = ("asset" if kind in ("asset", "coin")
                 else ("news" if kind == "news" else "theme"))
        # 一条 item 同时贡献给：
        #   ① 细粒度话题键（个股 / 概念 / 完整文本）
        #   ② 题材聚合键 theme:<题材>
        # 细粒度键跨平台很难归并（不同源的同一事件标题措辞不同），
        # 题材层才是「多平台共识」真正能落地的地方 —— 权威度的跨平台权重靠它生效。
        topic_rows = [(tkey, str(getattr(it, "title", "") or ""), tkind)]
        for th in tags["themes"]:
            topic_rows.append(("theme:%s" % th, th, "theme"))
        for tk, dn, tk_kind in topic_rows:
            conn.execute(
                """INSERT INTO crowd_topic_daily
                   (topic_key, day, display_name, topic_kind, source, platform,
                    heat, rank_no, mentions, asset_class, themes_json, market,
                    sentiment, time_window, influence_tier, media_channel, media_tier)
                   VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)
                   ON CONFLICT(topic_key, day, source) DO UPDATE SET
                     heat=excluded.heat, rank_no=excluded.rank_no,
                     mentions=crowd_topic_daily.mentions+1,
                     asset_class=excluded.asset_class, themes_json=excluded.themes_json,
                     market=excluded.market, sentiment=excluded.sentiment,
                     time_window=excluded.time_window,
                     influence_tier=excluded.influence_tier,
                     media_channel=excluded.media_channel, media_tier=excluded.media_tier""",
                (tk, day, dn, tk_kind, source,
                 platform_of(source),
                 float(it.heat_norm or 0.0),
                 int(getattr(it, "rank_no", 0) or 0), tags["asset_class"],
                 json.dumps(tags["themes"], ensure_ascii=False), tags["market"],
                 tags["sentiment"], tags["time_window"], tags["influence_tier"],
                 tags["media_channel"], tags["media_tier"]),
            )
            n_daily += 1
    conn.commit()
    return {"items": n_items, "daily": n_daily}


def _prev_month(month: str) -> str:
    try:
        y, m = int(month[:4]), int(month[5:7])
        return "%04d-%02d" % (y - 1, 12) if m == 1 else "%04d-%02d" % (y, m - 1)
    except (ValueError, IndexError):
        return ""


# ─────────────────────────────── 月榜 ───────────────────────────────

def build_monthly_rankings(conn: sqlite3.Connection, month: str) -> int:
    """按月聚合成权威度榜单，写 crowd_monthly_rankings。"""
    rows = conn.execute(
        """SELECT topic_key,
                  MAX(display_name)  AS display_name,
                  MAX(topic_kind)    AS topic_kind,
                  SUM(heat)          AS heat_total,
                  COUNT(DISTINCT platform) AS source_count,
                  COUNT(DISTINCT day)    AS days_active,
                  GROUP_CONCAT(DISTINCT source) AS sources,
                  GROUP_CONCAT(DISTINCT platform) AS platforms,
                  GROUP_CONCAT(DISTINCT market) AS markets,
                  GROUP_CONCAT(DISTINCT asset_class) AS asset_classes,
                  AVG(media_tier_weight) AS media_weight_raw
             FROM (
                 SELECT d.*,
                        CASE d.media_tier
                          WHEN 'tier1_主流财经媒体' THEN 1.0
                          WHEN 'tier2_垂直门户'   THEN 0.7
                          WHEN 'tier3_其他来源'   THEN 0.4
                          ELSE 0.5 END AS media_tier_weight
                 FROM crowd_topic_daily d
                 WHERE d.day LIKE ?
             )
            GROUP BY topic_key""",
        (month + "-%",),
    ).fetchall()
    if not rows:
        return 0

    prev_month = _prev_month(month)
    # v385: 原来把**全部**话题都建成卡片（库里 15215 个话题），
    # 再用 1.5 万个占位符去查 `topic_key IN (...)` —— 这条 SQL 直接把
    # /api/crowd/topics 拖到 40 秒以上（实测 CPU 打满、前端超时 000）。
    # 只保留候选池：按 heat 排序后截断到上限，后面的排序/截断语义不变。
    # v398 修: 本函数签名没有 limit 入参（月榜按月聚合全量），v385 从别处抄来的
    # `int(limit or 40)` 在这里是未定义名 —— 只要有任一行数据就抛 NameError，
    # 月榜恒挂。月榜不需要按请求 limit 缩放，直接取候选池上限。
    _pool = 120
    rows.sort(key=lambda _r: float(_r["heat_total"] or 0.0), reverse=True)
    rows = rows[:_pool]

    prev_map: Dict[str, float] = {}
    if prev_month:
        for r in conn.execute(
                "SELECT topic_key, SUM(heat) AS h FROM crowd_topic_daily "
                "WHERE day LIKE ? GROUP BY topic_key", (prev_month + "-%",)):
            prev_map[r["topic_key"]] = float(r["h"] or 0.0)

    # 题材标签（themes_json 汇总去重）—— 之前这里写死 "[]"，
    # 导致月榜的「话题/标的」列只能退化去显示 asset_class 的拼接串。
    theme_map: Dict[str, List[str]] = {}
    theme_cnt: Dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute(
            "SELECT topic_key, themes_json FROM crowd_topic_daily WHERE day LIKE ?",
            (month + "-%",)):
        try:
            arr = json.loads(r["themes_json"] or "[]")
        except Exception:
            arr = []
        for t in arr:
            theme_cnt[r["topic_key"]][str(t)] += 1
    for tk, cnt in theme_cnt.items():
        theme_map[tk] = [t for t, _ in cnt.most_common(4)]

    scored: List[Dict[str, Any]] = []
    for r in rows:
        heat_total = float(r["heat_total"] or 0.0)
        prev = prev_map.get(r["topic_key"], 0.0)
        momentum = round((heat_total - prev) / max(prev, 1.0), 4) if prev > 0 else 0.0
        mw = float(r["media_weight_raw"] or 0.5)
        sources = [s for s in (r["sources"] or "").split(",") if s]
        platforms = [s for s in (r["platforms"] or "").split(",") if s]
        score = authority_score(heat_total, int(r["source_count"] or 0),
                                int(r["days_active"] or 0), momentum, mw)
        scored.append({
            "topic_key": r["topic_key"],
            "display_name": r["display_name"] or r["topic_key"],
            "topic_kind": r["topic_kind"] or "theme",
            "authority_score": score,
            "heat_total": heat_total,
            "source_count": int(r["source_count"] or 0),
            "days_active": int(r["days_active"] or 0),
            "momentum": momentum,
            "media_weight": mw,
            "sources": sources,
            "platforms": platforms,
            "explain": authority_explain(heat_total, int(r["source_count"] or 0),
                                         int(r["days_active"] or 0), momentum, mw,
                                         platforms or sources),
            "asset_class": r["asset_classes"] or "",
            "market": r["markets"] or "",
        })
    scored.sort(key=lambda x: (-x["authority_score"], x["display_name"]))
    conn.execute("DELETE FROM crowd_monthly_rankings WHERE month = ?", (month,))
    for rank, row in enumerate(scored, 1):
        conn.execute(
            """INSERT INTO crowd_monthly_rankings
               (month, topic_key, display_name, topic_kind, rank_no, authority_score,
                heat_total, source_count, days_active, momentum, media_weight,
                sources_json, explain, asset_class, themes_json, market)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (month, row["topic_key"], row["display_name"], row["topic_kind"], rank,
             row["authority_score"], row["heat_total"], row["source_count"],
             row["days_active"], row["momentum"], row["media_weight"],
             json.dumps(row["platforms"] or row["sources"], ensure_ascii=False),
             row["explain"],
             row["asset_class"],
             json.dumps(theme_map.get(row["topic_key"], []), ensure_ascii=False),
             row["market"]),
        )
    conn.commit()
    return len(scored)


# ─────────────────────────────── 人群画像 ───────────────────────────────

AUDIENCE_DIMENSIONS = [
    ("platform", "来源平台/人群圈层"),
    ("market", "市场偏好"),
    ("asset_class", "资产类别偏好"),
    ("theme", "题材偏好"),
    ("sentiment", "情绪倾向"),
    ("time_window", "参与时段"),
    ("influence_tier", "影响力分层"),
    ("media_tier", "媒体层级"),
    ("media_channel", "具体媒体/频道"),
]


def build_audience_segments(conn: sqlite3.Connection, month: str) -> int:
    """按 9 个维度切人群画像，写 crowd_audience_segments。"""
    rows = conn.execute(
        "SELECT * FROM crowd_topic_daily WHERE day LIKE ?", (month + "-%",)).fetchall()
    if not rows:
        return 0
    total_heat = sum(float(r["heat"] or 0.0) for r in rows) or 1.0
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _put(dim: str, seg: str, row: Any) -> None:
        b = buckets.setdefault((dim, seg),
                               {"count": 0, "heat": 0.0, "sources": set()})
        b["count"] += 1
        b["heat"] += float(row["heat"] or 0.0)
        b["sources"].add(row["source"] or "")

    for r in rows:
        _put("platform", r["platform"] or "未知", r)
        _put("market", r["market"] or "未标注", r)
        _put("asset_class", r["asset_class"] or "未归类", r)
        _put("sentiment", r["sentiment"] or "未标注", r)
        _put("time_window", r["time_window"] or "未标注", r)
        _put("influence_tier", r["influence_tier"] or "未标注", r)
        _put("media_tier", r["media_tier"] or "未标注", r)
        _put("media_channel", r["media_channel"] or "未标注", r)
        for th in json.loads(r["themes_json"] or "[]"):
            _put("theme", th, r)

    conn.execute("DELETE FROM crowd_audience_segments WHERE month = ?", (month,))
    written = 0
    for dim, _label in AUDIENCE_DIMENSIONS:
        dim_rows = [(k, v) for k, v in buckets.items() if k[0] == dim]
        dim_rows.sort(key=lambda kv: -kv[1]["heat"])
        for rank, ((_d, seg), b) in enumerate(dim_rows, 1):
            conn.execute(
                """INSERT INTO crowd_audience_segments
                   (month, dimension, segment, label, item_count, heat_total, share,
                    rank_no, sample_sources)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (month, dim, seg, seg, b["count"], round(b["heat"], 4),
                 round(b["heat"] / total_heat, 6), rank,
                 json.dumps(sorted(s for s in b["sources"] if s), ensure_ascii=False)),
            )
            written += 1
    conn.commit()
    return written


def build_source_authority(conn: sqlite3.Connection, month: str) -> int:
    """源权威度：按**源**聚合（不是按媒体拆行 —— gnews 有上百家媒体，
    拆开会让每行 items=1 且跨源复现数被重复累加，反而看不出源的总体权重）。

    指标：条目数、覆盖话题数、被其它平台复现的总次数、权威度分。
    """
    rows = conn.execute(
        """SELECT source, platform, COUNT(*) AS items,
                  COUNT(DISTINCT topic_key) AS topics,
                  COUNT(DISTINCT day) AS days,
                  GROUP_CONCAT(DISTINCT media_channel) AS channels
             FROM crowd_topic_daily WHERE day LIKE ?
            GROUP BY source, platform""",
        (month + "-%",)).fetchall()
    if not rows:
        return 0
    cross = {r["topic_key"]: r["n"] for r in conn.execute(
        """SELECT topic_key, COUNT(DISTINCT platform) AS n
             FROM crowd_topic_daily WHERE day LIKE ? GROUP BY topic_key""",
        (month + "-%",))}
    conn.execute("DELETE FROM crowd_source_authority WHERE month = ?", (month,))
    for r in rows:
        topics = int(r["topics"] or 0)
        items = int(r["items"] or 0)
        # 该源的话题被多少个**其它平台**同时提到（累加）
        cv = 0
        for t in conn.execute(
                "SELECT topic_key FROM crowd_topic_daily WHERE day LIKE ? AND source = ?",
                (month + "-%", r["source"])):
            cv += max(0, int(cross.get(t["topic_key"], 1)) - 1)
        avg_cross = (cv / topics) if topics else 0.0
        score = round(W_HEAT * math.log1p(items)
                      + W_CROSS * avg_cross
                      + W_DAYS * int(r["days"] or 0), 4)
        conn.execute(
            """INSERT INTO crowd_source_authority
               (month, source, media_channel, items, topics, cross_verified,
                authority_score)
               VALUES (?,?,?,?,?,?,?)""",
            (month, r["source"], str(r["channels"] or "")[:200], items, topics,
             int(cv), score),
        )
    conn.commit()
    return len(rows)


# ─────────────────────────────── 编排 ───────────────────────────────

def run_once(conn: sqlite3.Connection,
             sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """一次完整采集 → 落库 → 建榜 → 建画像。返回本次运行摘要（真实计数）。"""
    import crowd_sources

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    result = crowd_sources.collect(sources)
    items = result["items"]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for it in items:
        if not it.collected_at:
            it.collected_at = now
    month = _month_of(now)
    day = _day_of(now)

    written = persist_items(conn, items)
    ranked = build_monthly_rankings(conn, month)
    segments = build_audience_segments(conn, month)
    src_auth = build_source_authority(conn, month)

    finished = time.strftime("%Y-%m-%d %H:%M:%S")
    status = "ok" if not result["errors"] else "partial"
    conn.execute(
        """INSERT INTO crowd_source_runs
           (started_at, finished_at, month, day, sources_json, errors_json,
            items_total, status)
           VALUES (?,?,?,?,?,?,?,?)""",
        (started, finished, month, day,
         json.dumps(result["per_source"], ensure_ascii=False),
         json.dumps(result["errors"], ensure_ascii=False),
         len(items), status),
    )
    conn.commit()
    return {
        "month": month,
        "day": day,
        "items_total": len(items),
        "per_source": result["per_source"],
        "errors": result["errors"],
        "written": written,
        "ranked_topics": ranked,
        "audience_segments": segments,
        "source_authority_rows": src_auth,
        "status": status,
    }


# ─────────────────────────────── 查询 ───────────────────────────────

def monthly_ranking(conn: sqlite3.Connection, month: str = "",
                    limit: int = 50, topic_kind: str = "",
                    sort: str = "authority") -> List[Dict[str, Any]]:
    """月榜读出。

    sort = "authority"（默认）按权威度降序 —— 跨平台交叉验证优先；
    sort = "heat" 按热度总量降序 —— 「此刻各源最热的是什么」；
    sort = "momentum" 按动能降序 —— 「什么正在升温」。
    month 省略 = 最新有数据的月份。
    """
    if not month:
        row = conn.execute(
            "SELECT month FROM crowd_monthly_rankings GROUP BY month "
            "ORDER BY month DESC LIMIT 1").fetchone()
        month = row["month"] if row else ""
    if not month:
        return []
    sql = "SELECT * FROM crowd_monthly_rankings WHERE month = ?"
    args: List[Any] = [month]
    if topic_kind:
        sql += " AND topic_kind = ?"
        args.append(topic_kind)
    order = {
        "authority": "authority_score DESC",
        "heat": "heat_total DESC",
        "momentum": "momentum DESC",
    }.get(str(sort or "authority").lower(), "authority_score DESC")
    sql += " ORDER BY " + order + ", display_name LIMIT ?"
    args.append(max(1, int(limit)))
    out: List[Dict[str, Any]] = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["sources"] = json.loads(d.pop("sources_json") or "[]")
        d["themes"] = json.loads(d.pop("themes_json") or "[]")
        out.append(d)
    # rank_no 是建榜时的权威度名次；按其它维度排序时必须重排，
    # 否则会出现「输出顺序是热度榜、rank_no 却是权威度名次」的错位。
    if str(sort or "authority").lower() != "authority":
        for i, d in enumerate(out, 1):
            d["rank_no"] = i
            d["ranked_by"] = str(sort)
    else:
        for d in out:
            d["ranked_by"] = "authority"
    return out


def audience_profile(conn: sqlite3.Connection, month: str = "",
                     dimension: str = "") -> Dict[str, Any]:
    """人群画像读出：按维度分组的分群占比。"""
    if not month:
        row = conn.execute(
            "SELECT month FROM crowd_audience_segments GROUP BY month "
            "ORDER BY month DESC LIMIT 1").fetchone()
        month = row["month"] if row else ""
    if not month:
        return {"month": "", "dimensions": {}}
    sql = "SELECT * FROM crowd_audience_segments WHERE month = ?"
    args: List[Any] = [month]
    if dimension:
        sql += " AND dimension = ?"
        args.append(dimension)
    sql += " ORDER BY dimension, rank_no"
    dims: Dict[str, List[Dict[str, Any]]] = {}
    labels: Dict[str, str] = {}
    for r in conn.execute(sql, args):
        d = dict(r)
        d["sample_sources"] = json.loads(d.pop("sample_sources") or "[]")
        dims.setdefault(d["dimension"], []).append(d)
        labels[d["dimension"]] = d["dimension"]
    return {"month": month,
            "dimension_labels": {d: lbl for d, lbl in AUDIENCE_DIMENSIONS},
            "dimensions": dims}


def source_authority(conn: sqlite3.Connection, month: str = "") -> List[Dict[str, Any]]:
    """源/媒体权威度读出。"""
    if not month:
        row = conn.execute(
            "SELECT month FROM crowd_source_authority GROUP BY month "
            "ORDER BY month DESC LIMIT 1").fetchone()
        month = row["month"] if row else ""
    if not month:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM crowd_source_authority WHERE month = ? "
        "ORDER BY authority_score DESC", (month,))]


def latest_items(conn: sqlite3.Connection, limit: int = 100,
                 source: str = "") -> List[Dict[str, Any]]:
    """最近抓到的原始条目（可回溯到 url）。"""
    sql = ("SELECT source, item_kind, title, url, media_channel, heat, rank_no, "
           "published_at, collected_at, topic_key FROM crowd_source_items")
    args: List[Any] = []
    if source:
        sql += " WHERE source = ?"
        args.append(source)
    sql += " ORDER BY collected_at DESC, id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    return [dict(r) for r in conn.execute(sql, args)]


# ─────────────────────────── 话题 × 观点（人在关注什么 / 持什么观点） ───────────────────────────
#
# 为什么要有这一层：
#   月榜(crowd_monthly_rankings)是**指标表** —— 回答"哪个话题的权威度最高"，
#   但它只有分数、拼接串和 explain 字符串，回答不了人真正想问的两件事：
#     ① 市场上的人现在在关注什么话题（题材/市场/资产类别 + 谁在讨论）
#     ② 他们对这些话题持什么观点（看多/看空/中性）+ 具体说了什么（原话标题 + url）
#   所以这里按 topic_key 把 crowd_topic_daily(每日×来源的快照) 和
#   crowd_source_items(原始条目) 重新聚合成"话题卡片"。
#
# 口径声明（前端必须如实展示，不能吹）：
#   · 观点 = **标题/正文关键词命中**（_BULL_WORDS/_BEAR_WORDS），不是语义模型，
#     也不是"市场情绪指数"；命中 0 个方向词 = 中性/无方向。
#   · 权威度沿用 authority_score()，与月榜**同口径**，不另起一套分数。
#   · 跨平台数 = COUNT(DISTINCT platform)（同一家平台的多张榜只算一次）。

SENTIMENT_BULL = "看多"
SENTIMENT_BEAR = "看空"
SENTIMENT_NEUTRAL = "中性/无方向"

STANCE_BULL = "看多主导"
STANCE_BEAR = "看空主导"
STANCE_SPLIT = "多空分歧"
STANCE_NONE = "无明确方向"


def _split_concat(value: Optional[str]) -> List[str]:
    """GROUP_CONCAT(DISTINCT x) → 去重列表（保持首次出现顺序）。"""
    out: List[str] = []
    seen = set()
    for part in str(value or "").split(","):
        p = part.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _shift_day(day: str, delta: int) -> str:
    """YYYY-MM-DD 平移 delta 天（失败返回原值）。"""
    try:
        ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + delta * 86400
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except Exception:
        return day


def _stance_of(bull: int, bear: int, neutral: int) -> Tuple[str, float, float]:
    """(立场标签, 看多占比, 看空占比)。

    立场判定只在**有方向的条目**上做：bull+bear=0 → 无明确方向
    （不是"中性主导"—— 中性可能只是"标题里根本没提方向"）。
    """
    total = max(1, bull + bear + neutral)
    directed = bull + bear
    if directed == 0:
        return STANCE_NONE, bull / total, bear / total
    ratio = bull / float(bear) if bear > 0 else float("inf")
    if ratio >= 1.5:
        stance = STANCE_BULL
    elif ratio <= 0.67:
        stance = STANCE_BEAR
    else:
        stance = STANCE_SPLIT
    return stance, bull / total, bear / total


def topic_overview(conn: sqlite3.Connection, month: str = "", days: int = 0,
                   limit: int = 40, sort: str = "heat", item_limit: int = 5,
                   kind: str = "") -> Dict[str, Any]:
    """话题 × 观点视图 —— 「现在市场上的人在关注什么、持什么观点」。

    window 口径：
      · days > 0 → 最近 N 天（以库里最后一天为终点），最贴近"现在"
      · days = 0 → 整月（与月榜同口径）
    上一等长窗口用于算动能（momentum），与 build_monthly_rankings 同公式。

    kind 过滤（回答"话题"和"标的"是两回事）：
      · ""      → 全部
      · topic   → theme + news（大家在讨论什么）
      · asset   → asset（大家在买什么：个股/币种）
    """
    last_row = conn.execute("SELECT MAX(day) AS d FROM crowd_topic_daily").fetchone()
    last_day = (last_row["d"] if last_row else "") or ""
    if not month:
        month = last_day[:7] if last_day else ""
    if not last_day:
        return {"month": month, "window": {}, "count": 0, "topics": [],
                "summary": {}, "note": "采集尚未产出任何话题数据"}

    days = max(0, int(days or 0))
    if days > 0:
        end_day = last_day
        start_day = _shift_day(end_day, -(days - 1))
        prev_start = _shift_day(start_day, -(days - 1))
        prev_end = _shift_day(start_day, -1)
    else:
        start_day = month + "-01"
        end_day = month + "-31"
        prev_month = _prev_month(month)
        prev_start, prev_end = prev_month + "-01", prev_month + "-31"

    rows = conn.execute(
        """SELECT topic_key,
                  MAX(display_name)  AS display_name,
                  MAX(topic_kind)    AS topic_kind,
                  SUM(heat)          AS heat_total,
                  COUNT(DISTINCT platform) AS source_count,
                  COUNT(DISTINCT day) AS days_active,
                  COUNT(*)           AS mentions,
                  MAX(day)           AS last_seen,
                  GROUP_CONCAT(DISTINCT source)      AS sources,
                  GROUP_CONCAT(DISTINCT platform)    AS platforms,
                  GROUP_CONCAT(DISTINCT market)      AS markets,
                  GROUP_CONCAT(DISTINCT asset_class) AS asset_classes,
                  AVG(CASE media_tier
                        WHEN 'tier1_主流财经媒体' THEN 1.0
                        WHEN 'tier2_垂直门户'   THEN 0.7
                        WHEN 'tier3_其他来源'   THEN 0.4
                        ELSE 0.5 END) AS media_weight
             FROM crowd_topic_daily
            WHERE day BETWEEN ? AND ?
            GROUP BY topic_key""",
        (start_day, end_day)).fetchall()
    kind = str(kind or "").strip().lower()
    if kind:
        want = {"topic": ("theme", "news"), "asset": ("asset",)}.get(kind, (kind,))
        rows = [r for r in rows if (r["topic_kind"] or "") in want]
    if not rows:
        return {"month": month,
                "window": {"start": start_day, "end": end_day, "days": days},
                "count": 0, "topics": [], "summary": {},
                "note": "该窗口内没有符合条件的话题数据"}

    prev_map: Dict[str, float] = {}
    for r in conn.execute(
            "SELECT topic_key, SUM(heat) AS h FROM crowd_topic_daily "
            "WHERE day BETWEEN ? AND ? GROUP BY topic_key", (prev_start, prev_end)):
        prev_map[r["topic_key"]] = float(r["h"] or 0.0)

    cards: List[Dict[str, Any]] = []
    for r in rows:
        heat_total = float(r["heat_total"] or 0.0)
        prev = prev_map.get(r["topic_key"], 0.0)
        momentum = round((heat_total - prev) / max(prev, 1.0), 4) if prev > 0 else 0.0
        mw = float(r["media_weight"] or 0.5)
        platforms = _split_concat(r["platforms"])
        sources = _split_concat(r["sources"])
        score = authority_score(heat_total, int(r["source_count"] or 0),
                                int(r["days_active"] or 0), momentum, mw)
        cards.append({
            "topic_key": r["topic_key"],
            "display_name": r["display_name"] or r["topic_key"],
            "topic_kind": r["topic_kind"] or "theme",
            "heat_total": round(heat_total, 4),
            "authority_score": score,
            "source_count": int(r["source_count"] or 0),
            "days_active": int(r["days_active"] or 0),
            "mentions": int(r["mentions"] or 0),
            "momentum": momentum,
            "media_weight": round(mw, 3),
            "last_seen": r["last_seen"] or "",
            "platforms": platforms,
            "sources": sources,
            "platform_audiences": [PLATFORM_AUDIENCE.get(p, p) for p in platforms],
            "markets": _split_concat(r["markets"]),
            "asset_classes": _split_concat(r["asset_classes"]),
            "explain": authority_explain(heat_total, int(r["source_count"] or 0),
                                         int(r["days_active"] or 0), momentum, mw,
                                         platforms or sources),
        })

    # ── 观点分布（sentiment 真实计数） ──────────────────────────────
    keys = [c["topic_key"] for c in cards]
    topic_keys = set(keys)
    ph = ",".join("?" for _ in keys)
    sent: Dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute(
            "SELECT topic_key, sentiment, COUNT(*) AS n FROM crowd_topic_daily "
            "WHERE day BETWEEN ? AND ? AND topic_key IN (%s) "
            "GROUP BY topic_key, sentiment" % ph, [start_day, end_day] + keys):
        sent[r["topic_key"]][r["sentiment"] or SENTIMENT_NEUTRAL] += int(r["n"] or 0)

    # ── 题材标签（themes_json 汇总去重，按出现次数排序） ─────────────
    theme_cnt: Dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute(
            "SELECT topic_key, themes_json FROM crowd_topic_daily "
            "WHERE day BETWEEN ? AND ? AND topic_key IN (%s)" % ph,
            [start_day, end_day] + keys):
        try:
            arr = json.loads(r["themes_json"] or "[]")
        except Exception:
            arr = []
        for t in arr:
            theme_cnt[r["topic_key"]][str(t)] += 1

    # ── 代表性原话（最新条目，带 url 可回溯） ───────────────────────
    items_by_topic: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    item_total: Dict[str, int] = defaultdict(int)
    max_items = max(0, int(item_limit or 0))
    if max_items > 0:
        win_from = start_day + "T00:00:00"
        win_to = end_day + "T23:59:59"
        for r in conn.execute(
                "SELECT topic_key, title, url, source, media_channel, heat, "
                "published_at, collected_at FROM crowd_source_items "
                "WHERE collected_at BETWEEN ? AND ? "
                "ORDER BY collected_at DESC, id DESC", (win_from, win_to)):
            tk = r["topic_key"]
            if tk not in item_total and tk not in topic_keys:
                continue
            item_total[tk] += 1
            if len(items_by_topic[tk]) < max_items:
                items_by_topic[tk].append({
                    "title": r["title"] or "",
                    "url": r["url"] or "",
                    "source": r["source"] or "",
                    "media_channel": r["media_channel"] or "",
                    "heat": float(r["heat"] or 0.0),
                    "published_at": r["published_at"] or "",
                    "collected_at": r["collected_at"] or "",
                })

        # 题材聚合键（theme:<题材>）在原始条目表里**没有对应行** ——
        # 它是"命中该题材的所有条目"的聚合（persist_items 里为每个题材额外写了一行）。
        # 所以要反查：窗口内哪些条目的标题/正文/概念命中了该题材。
        needed = {c["topic_key"][len("theme:"):] for c in cards
                  if c["topic_key"].startswith("theme:")}
        if needed:
            # 合并正则预筛：一条 item 只需要扫一遍文本，就能判断是否可能命中任一题材
            # （逐题材扫 = 每条 item 扫 N 次；合并后只扫 1 次）。
            kws = [w for name, wl in THEME_RULES if name in needed for w in wl]
            prefilter = re.compile("|".join(re.escape(w) for w in kws), re.IGNORECASE) \
                if kws else None
            for r in conn.execute(
                    "SELECT title, body, url, source, media_channel, heat, "
                    "published_at, collected_at, meta_json FROM crowd_source_items "
                    "WHERE collected_at BETWEEN ? AND ? "
                    "ORDER BY collected_at DESC, id DESC", (win_from, win_to)):
                raw_meta = r["meta_json"] or ""
                meta: Dict[str, Any] = {}
                if '"concepts"' in raw_meta:
                    try:
                        loaded = json.loads(raw_meta)
                    except Exception:
                        loaded = None
                    if isinstance(loaded, dict):
                        meta = loaded
                blob = _blob_of(r["title"] or "", r["body"] or "", meta)
                if prefilter is not None and not prefilter.search(blob):
                    continue
                hit = needed & set(_match_rules(blob, THEME_RULES, only=needed))
                if not hit:
                    continue
                item = {
                    "title": r["title"] or "",
                    "url": r["url"] or "",
                    "source": r["source"] or "",
                    "media_channel": r["media_channel"] or "",
                    "heat": float(r["heat"] or 0.0),
                    "published_at": r["published_at"] or "",
                    "collected_at": r["collected_at"] or "",
                }
                for th in hit:
                    tk = "theme:%s" % th
                    item_total[tk] += 1
                    if len(items_by_topic[tk]) < max_items:
                        items_by_topic[tk].append(item)

    for c in cards:
        cnt = sent.get(c["topic_key"], Counter())
        bull = int(cnt.get(SENTIMENT_BULL, 0))
        bear = int(cnt.get(SENTIMENT_BEAR, 0))
        neutral = sum(v for k, v in cnt.items()
                      if k not in (SENTIMENT_BULL, SENTIMENT_BEAR))
        stance, bull_ratio, bear_ratio = _stance_of(bull, bear, neutral)
        c["sentiment"] = {
            "bull": bull, "bear": bear, "neutral": neutral,
            "total": bull + bear + neutral,
            "stance": stance,
            "bull_ratio": round(bull_ratio, 4),
            "bear_ratio": round(bear_ratio, 4),
            "net": bull - bear,
        }
        c["themes"] = [t for t, _ in theme_cnt.get(c["topic_key"], Counter()).most_common(4)]
        c["items"] = items_by_topic.get(c["topic_key"], [])
        c["item_total"] = item_total.get(c["topic_key"], 0)

    order = {
        "heat": lambda c: (-c["heat_total"], c["display_name"]),
        "authority": lambda c: (-c["authority_score"], c["display_name"]),
        "momentum": lambda c: (-c["momentum"], c["display_name"]),
        "latest": lambda c: (c["last_seen"], -c["heat_total"]),
        "bullish": lambda c: (-c["sentiment"]["bull_ratio"], -c["heat_total"]),
    }.get(str(sort or "heat").lower(), lambda c: (-c["heat_total"], c["display_name"]))
    cards.sort(key=order)
    cards = cards[:max(1, int(limit))]
    for i, c in enumerate(cards, 1):
        c["rank_no"] = i

    # ── 窗口整体：对事实行直接计数 ────────────────────────────────
    # 不用 top-N 话题反推：反推会把一个话题的 mentions 重复记到它的每个
    # 平台/题材上，数字会被放大（科技/AI 一个话题就能把 5 个平台各记 68）。
    plat_cnt: Counter = Counter()
    for r in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM crowd_topic_daily "
            "WHERE day BETWEEN ? AND ? GROUP BY platform", (start_day, end_day)):
        plat_cnt[r["platform"] or "未知平台"] = int(r["n"] or 0)

    market_cnt: Counter = Counter()
    for r in conn.execute(
            "SELECT market, COUNT(*) AS n FROM crowd_topic_daily "
            "WHERE day BETWEEN ? AND ? GROUP BY market", (start_day, end_day)):
        mkt = (r["market"] or "").strip()
        # "题材:xxx" 是"没有明确市场、按题材兜底"的产物，不是市场 —— 不混进市场分布
        if not mkt or mkt.startswith("题材:"):
            continue
        market_cnt[mkt] = int(r["n"] or 0)

    theme_all: Counter = Counter()
    for r in conn.execute(
            "SELECT themes_json FROM crowd_topic_daily WHERE day BETWEEN ? AND ?",
            (start_day, end_day)):
        try:
            arr = json.loads(r["themes_json"] or "[]")
        except Exception:
            arr = []
        for t in arr:
            theme_all[str(t)] += 1

    sent_all: Counter = Counter()
    for r in conn.execute(
            "SELECT sentiment, COUNT(*) AS n FROM crowd_topic_daily "
            "WHERE day BETWEEN ? AND ? GROUP BY sentiment", (start_day, end_day)):
        sent_all[r["sentiment"] or SENTIMENT_NEUTRAL] = int(r["n"] or 0)
    tot_bull = sent_all.get(SENTIMENT_BULL, 0)
    tot_bear = sent_all.get(SENTIMENT_BEAR, 0)
    tot_neutral = sum(v for k, v in sent_all.items()
                       if k not in (SENTIMENT_BULL, SENTIMENT_BEAR))
    tot_mentions = sum(sent_all.values())
    stance, bull_ratio, bear_ratio = _stance_of(tot_bull, tot_bear, tot_neutral)

    return {
        "month": month,
        "window": {
            "start": start_day, "end": end_day, "days": days,
            "label": ("近 %d 天（%s ~ %s）" % (days, start_day, end_day))
                     if days > 0 else ("%s 全月" % month),
        },
        "sort": str(sort or "heat"),
        "kind": kind or "all",
        "count": len(cards),
        "topics": cards,
        "summary": {
            "mentions": tot_mentions,
            "topic_count": len(cards),
            "platform_count": len(plat_cnt),
            "platforms": sorted(plat_cnt),
            "sentiment": {"bull": tot_bull, "bear": tot_bear, "neutral": tot_neutral,
                          "total": tot_bull + tot_bear + tot_neutral,
                          "stance": stance,
                          "bull_ratio": round(bull_ratio, 4),
                          "bear_ratio": round(bear_ratio, 4),
                          "net": tot_bull - tot_bear},
            "top_markets": [{"name": m, "mentions": n} for m, n in market_cnt.most_common(6)],
            "top_themes": [{"name": t, "mentions": n} for t, n in theme_all.most_common(6)],
            "platform_mentions": [
                {"platform": p, "audience": PLATFORM_AUDIENCE.get(p, p), "mentions": n}
                for p, n in plat_cnt.most_common()
            ],
        },
        "note": ("观点由标题/正文关键词判定（看多词 %d 个 / 看空词 %d 个），"
                 "不是语义模型；未命中方向词记为中性/无方向。"
                 % (len(_BULL_WORDS), len(_BEAR_WORDS))),
    }


# ─── topic_overview 进程内短期缓存（与 crowd_subjects 同口径）─────────────────
# 冷路径是对 2.7 万行 crowd_topic_daily 做多次 GROUP BY 聚合，裸跑约 11s；
# 浏览器 15s 轮询叠加连接竞争就会超时。结果在采集周期内基本不变，缓存 15 分钟。
_TOPIC_CACHE: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
_TOPIC_CACHE_TTL = 900.0


def topic_overview_cached(conn: sqlite3.Connection, month: str = "", days: int = 0,
                          limit: int = 40, sort: str = "heat", item_limit: int = 5,
                          kind: str = "") -> Dict[str, Any]:
    """带 15 分钟 TTL 缓存的 topic_overview（API 走这个）。"""
    days = max(0, int(days or 0))
    last_row = conn.execute("SELECT MAX(day) AS d FROM crowd_topic_daily").fetchone()
    last_day = (last_row["d"] if last_row else "") or ""
    if days > 0:
        end_day = last_day
        start_day = _shift_day(end_day, -(days - 1)) if end_day else ""
    else:
        m = month or (last_day[:7] if last_day else "")
        start_day = m + "-01"
        end_day = m + "-31"
    # 数据指纹：窗口内行数 + 最大 day，采集推进后指纹变化即自动失效
    fp_row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(day) AS mx FROM crowd_topic_daily "
        "WHERE day BETWEEN ? AND ?", (start_day, end_day)).fetchone()
    fp = "%s|%s" % (fp_row["n"], fp_row["mx"])
    key = (start_day, end_day, fp, item_limit, kind)
    now = time.time()
    hit = _TOPIC_CACHE.get(key)
    if hit and now - hit[0] < _TOPIC_CACHE_TTL:
        full = hit[1]
        return _topic_resort(full, limit=limit, sort=sort)

    data = topic_overview(conn, month=month, days=days, limit=limit, sort=sort,
                          item_limit=item_limit, kind=kind)
    full = dict(data)
    full["topics"] = list(data.get("topics") or [])
    _TOPIC_CACHE[key] = (now, full)
    return _topic_resort(full, limit=limit, sort=sort)


def _topic_resort(data: Dict[str, Any], limit: int, sort: str) -> Dict[str, Any]:
    """按请求排序/截断全量缓存卡片，避免换排序后 Top-N 漏主体。"""
    cards = list(data.get("topics") or [])
    order = {
        "heat": lambda c: (-c.get("heat_total", 0), c.get("display_name", "")),
        "authority": lambda c: (-c.get("authority_score", 0), c.get("display_name", "")),
        "momentum": lambda c: (-c.get("momentum", 0), c.get("display_name", "")),
        "latest": lambda c: (c.get("last_seen", ""), -c.get("heat_total", 0)),
        "bullish": lambda c: (-c.get("sentiment", {}).get("bull_ratio", 0),
                              -c.get("heat_total", 0)),
    }.get(str(sort or "heat").lower(),
         lambda c: (-c.get("heat_total", 0), c.get("display_name", "")))
    cards.sort(key=order)
    cards = cards[:max(1, int(limit))]
    for i, c in enumerate(cards, 1):
        c["rank_no"] = i
    out = dict(data)
    out["topics"] = cards
    out["count"] = len(cards)
    return out
